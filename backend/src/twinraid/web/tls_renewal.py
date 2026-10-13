"""Automatische Erneuerung des Serverzertifikats im laufenden Betrieb (Konzept 5.2).

Der Web-Prozess hat keinen Zugriff auf den CA-Schlüssel. Ist eine Erneuerung
fällig, vermerkt er den Zeitpunkt und beendet sich geordnet. s6 startet ihn
neu; vorher erneuert das Startskript des Web-Dienstes als root das
Zertifikat (twinraid-init --refresh-tls).

Keine Neustart-Schleife: Der Web-Prozess beendet sich höchstens einmal in
24 Stunden für eine Erneuerung. Der Vermerk liegt in /data/web und übersteht
Neustarts. Scheitert die Erneuerung, läuft das bisherige Zertifikat weiter.
"""

import json
import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

from twinraid.shared.files import write_file_atomic
from twinraid.shared.tls import TlsError, read_runtime, server_renewal_due

logger = logging.getLogger(__name__)

CHECK_INTERVAL_SECONDS = 60.0 * 60.0
MIN_RESTART_INTERVAL = timedelta(hours=24)


def _utc_now() -> datetime:
    return datetime.now(UTC)


class RenewalWatcher:
    def __init__(
        self,
        runtime_tls_dir: Path,
        marker: Path,
        request_restart: Callable[[], None],
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self._runtime_tls_dir = runtime_tls_dir
        self._marker = marker
        self._request_restart = request_restart
        self._clock = clock

    def last_request(self) -> datetime | None:
        """Zeitpunkt der letzten Erneuerungsanfrage; None, wenn keiner vermerkt ist."""
        try:
            raw = json.loads(self._marker.read_text())
            return datetime.fromisoformat(raw["requested_at"])
        except FileNotFoundError:
            return None
        except OSError, ValueError, KeyError, TypeError:
            logger.warning(
                "The TLS renewal note %s is unreadable; it will be rewritten.", self._marker
            )
            return None

    def check(self) -> bool:
        """Prüft einmal. Gibt True zurück, wenn ein Neustart angefordert wurde."""
        try:
            state = read_runtime(self._runtime_tls_dir)
        except TlsError:
            return False
        # Ein eigenes Zertifikat erneuert TwinRaid nicht.
        if state.source != "ca" or state.ca is None:
            return False
        now = self._clock()
        if not server_renewal_due(state.leaf, state.ca, now):
            return False
        last = self.last_request()
        if last is not None and now - last < MIN_RESTART_INTERVAL:
            return False
        try:
            note = json.dumps({"requested_at": now.isoformat()}).encode()
            write_file_atomic(self._marker, note, 0o600)
        except OSError as exc:
            # Ohne Vermerk keine Garantie gegen eine Neustart-Schleife: nicht beenden.
            logger.error("Cannot write the TLS renewal note %s: %s", self._marker, exc)
            return False
        logger.warning(
            "The TLS server certificate expires on %s. Restarting the web process to renew it.",
            state.leaf.not_valid_after_utc.date().isoformat(),
        )
        self._request_restart()
        return True
