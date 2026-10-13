"""Liest UID, GID und Capabilities der TwinRaid-Prozesse im Container.

Wird vom Starttest per "docker exec ... python3 -c" ausgeführt und gibt JSON aus.
"""

import json
from pathlib import Path

FIELDS = ("Uid", "Gid", "Groups", "CapInh", "CapPrm", "CapEff", "CapBnd", "CapAmb", "NoNewPrivs")
result: dict[str, dict[str, str]] = {}
for proc in Path("/proc").iterdir():
    if not proc.name.isdigit():
        continue
    try:
        cmdline = (proc / "cmdline").read_bytes().replace(b"\0", b" ").decode()
        status = (proc / "status").read_text()
    except OSError:
        continue
    for name in ("twinraid-worker", "twinraid-web"):
        if f"/bin/{name}" in cmdline and "python" in cmdline:
            values = dict(line.split(":", 1) for line in status.splitlines() if ":" in line)
            entry = {key: values[key].strip() for key in FIELDS}
            # Bei Uid und Gid nur die reale ID (erste Spalte).
            entry["Uid"] = entry["Uid"].split()[0]
            entry["Gid"] = entry["Gid"].split()[0]
            result[name] = entry
print(json.dumps(result))
