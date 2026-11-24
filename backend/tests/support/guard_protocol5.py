"""Protokoll 5 des Wächters (Version 6, Konzept 3.6) für die Tests des Skripts.

TwinRaid selbst nimmt Protokoll 5 erst mit v0.6.1 an. Bis dahin prüft diese Hilfe die neuen
Teile streng (Feld notify, Auftrag notify in last, die neuen Ergebnisse) und gibt den Rest
als Status von Protokoll 4 an den Parser von TwinRaid weiter, damit jedes andere Feld dasselbe
Schema erfüllen muss wie bisher.
"""

import json
from typing import Any

from twinraid.integrations.source_guard import parse_status

# Ergebnisse des Auftrags notify (Konzept 3.6, „Ergebnisse“).
NOTIFY_RESULTS = frozenset(
    {"notified", "notify_off", "notify_invalid", "notify_limited", "notify_failed"}
)
# Ergebnisse, die jeder Auftrag haben kann, auch notify.
_ANY_ORDER = frozenset({"config_invalid", "misconfigured", "request_not_a_file", "seq_exhausted"})


def _no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    keys = [key for key, _ in pairs]
    if len(keys) != len(set(keys)):
        raise AssertionError(f"duplicate keys: {keys}")
    return dict(pairs)


def parse_status_5(raw: bytes) -> None:
    """Prüft einen Status von Version 6; wirft AssertionError oder InvalidStatusError."""
    # Any: JSON-Werte des Status, geprüft wie unten.
    data: dict[str, Any] = json.loads(raw.decode("ascii"), object_pairs_hook=_no_duplicates)
    assert (data.get("protocol"), data.get("version")) == (5, 6), data.get("protocol")
    notify = data.pop("notify")
    assert notify in ("on", "off"), notify
    last = data.get("last")
    if last is not None and last.get("order") == "notify":
        assert last["container"] is None, last
        assert last["result"] in NOTIFY_RESULTS | _ANY_ORDER, last
        # Stellvertreter von Protokoll 4, damit der Rest des Eintrags geprüft wird.
        data["last"] = {**last, "order": "ping", "result": "extended"}
    elif last is not None:
        assert last.get("result") not in NOTIFY_RESULTS, last
    data["protocol"] = 4
    data["version"] = 5
    parse_status(json.dumps(data, separators=(",", ":")).encode("ascii") + b"\n")
