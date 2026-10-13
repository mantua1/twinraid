# TwinRaid

Weboberfläche in einem einzigen Docker-Container, die zwei Unraid-Server steuert: Wake-on-LAN, Statusanzeige, Gaming-VM und Spiegel-Backup per rsync.

> **Stand: Phase 2 (Sicherheit).** HTTPS, Login, Sessions, CSRF-Schutz, Netzfilter, Rate-Limit und Audit-Log sind umgesetzt. Echte Server werden noch nicht angesprochen; diese Version läuft nur im Simulationsmodus.

Das verbindliche Konzept steht in [docs/KONZEPT.md](docs/KONZEPT.md).

## Lokale Entwicklung

Voraussetzungen: [uv](https://docs.astral.sh/uv/), Node.js 24 (über nvm), Docker (rootless genügt).

### Backend prüfen

```bash
cd backend
uv sync
uv run pytest
uv run ruff check
uv run ruff format --check
uv run mypy
```

### Frontend prüfen

```bash
cd frontend
npm ci
npm test
npm run lint
npm run format:check
npm run typecheck
npm run build
```

### Ohne Container starten (Simulationsmodus)

```bash
cd frontend && npm run build
cd ../backend && ./scripts/dev.sh
```

Beim ersten Start erzeugt TwinRaid ein selbstsigniertes Zertifikat unter `.run/tls` und schreibt den Fingerabdruck ins Log. Das Passwort setzt du in einem zweiten Terminal:

```bash
cd backend && ./scripts/dev-admin.sh set-password
```

Danach im Browser: <https://127.0.0.1:8443>. Die Zertifikatswarnung des Browsers einmal bestätigen, nachdem du den Fingerabdruck mit dem Log verglichen hast. Für Änderungen am Frontend mit sofortiger Vorschau zusätzlich `cd frontend && npm run dev` starten und <https://127.0.0.1:5173> öffnen.

### Container lokal

```bash
docker/run-local.sh                    # bauen und starten, https://127.0.0.1:8443
docker exec -it twinraid-dev twinraid-admin set-password   # zweites Terminal
docker/smoke-test.sh                   # Starttest mit allen Sicherheitsprüfungen
CHECK_EACH_CAP=1 docker/smoke-test.sh  # zusätzlich: jede Capability ist nötig
```

Weitere Befehle der Admin-CLI im Container: `twinraid-admin show-cert-fingerprint`, `twinraid-admin renew-cert` und `twinraid-admin unlock` (hebt Login-Sperren auf).

## Lizenz

MIT, siehe [LICENSE](LICENSE).
