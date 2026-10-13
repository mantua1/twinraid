# TwinRaid

Weboberfläche in einem einzigen Docker-Container, die zwei Unraid-Server steuert: Wake-on-LAN, Statusanzeige, Gaming-VM und Spiegel-Backup per rsync.

> **Stand: Phase 1 (Grundgerüst).** Es gibt noch keinen Login und keine echten Serverzugriffe. Diese Version nur lokal im Simulationsmodus verwenden.

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

Danach im Browser: <http://127.0.0.1:8443>. Für Änderungen am Frontend mit sofortiger Vorschau zusätzlich in einem zweiten Terminal `cd frontend && npm run dev` starten und <http://127.0.0.1:5173> öffnen.

### Container lokal

```bash
docker/run-local.sh                    # bauen und starten, http://127.0.0.1:8443
docker/smoke-test.sh                   # Starttest mit allen Sicherheitsprüfungen
CHECK_EACH_CAP=1 docker/smoke-test.sh  # zusätzlich: jede Capability ist nötig
```

## Lizenz

MIT, siehe [LICENSE](LICENSE).
