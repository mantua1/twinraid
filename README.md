# TwinRaid

Weboberfläche in einem einzigen Docker-Container, die zwei Unraid-Server steuert: Wake-on-LAN, Statusanzeige, Gaming-VM und Spiegel-Backup per rsync.

> **Stand: Phase 3 (Status).** Dashboard mit Live-Status beider Server und der Gaming-VM, eigene CA für HTTPS, Login und alle Schutzmaßnahmen aus Phase 2. Im Modus `real` liest TwinRaid nur den Status; Aktionen wie Wecken oder Sync folgen in späteren Phasen.

- Konzept (verbindlich): [docs/KONZEPT.md](docs/KONZEPT.md)
- Installation auf Unraid: [docs/EINRICHTUNG.md](docs/EINRICHTUNG.md)
- Image: `ghcr.io/mantua1/twinraid` (nur amd64, mit Herkunftsnachweis)

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

Beim ersten Start erzeugt TwinRaid eine eigene CA und ein Serverzertifikat (Ablage unter `.run/data/tls`, für den Web-Prozess unter `.run/tls`) und schreibt die Fingerabdrücke ins Log. Ein selbstsigniertes Zertifikat aus Phase 2 wird dabei ersetzt. Das Passwort setzt du in einem zweiten Terminal:

```bash
cd backend && ./scripts/dev-admin.sh set-password
```

Danach im Browser: <https://127.0.0.1:8443>. Die Zertifikatswarnung einmal bestätigen oder die CA aus `.run/tls/ca.pem` im Browser importieren (Fingerabdruck mit dem Log vergleichen). Für Änderungen am Frontend mit sofortiger Vorschau zusätzlich `cd frontend && npm run dev` starten und <https://127.0.0.1:5173> öffnen.

Die Simulation durchläuft alle Zustände und Ursachen, ein Schritt dauert 20 Sekunden. Einen laufenden Kopierjob täuschst du mit `./scripts/dev-admin.sh mock-job on` vor (`off` beendet ihn).

### Container lokal

```bash
docker/run-local.sh                    # bauen und starten, https://127.0.0.1:8443
docker exec -it twinraid-dev twinraid-admin set-password   # zweites Terminal
docker/smoke-test.sh                   # Starttest mit allen Sicherheitsprüfungen
CHECK_EACH_CAP=1 docker/smoke-test.sh  # zusätzlich: jede Capability ist nötig
```

Alle Befehle der Admin-CLI stehen in [docs/EINRICHTUNG.md, Abschnitt 14](docs/EINRICHTUNG.md#14-befehle-der-admin-cli).

Tests sprechen nie echte Server an: Eine Sperre in `backend/tests/conftest.py` lässt nur Verbindungen zu `127.0.0.1` und zum nie gerouteten Testnetz `192.0.2.0/24` zu. Der Starttest prüft den Modus `real` ebenfalls nur mit Adressen aus diesem Testnetz.

## Release veröffentlichen

Images baut und veröffentlicht ausschließlich GitHub Actions (Workflow `CI`, Job *Publish image*). Jeder Push auf `main` erzeugt `ghcr.io/mantua1/twinraid:edge`. Ein Release entsteht über einen Versions-Tag:

1. Die Version in `backend/pyproject.toml`, `backend/src/twinraid/__init__.py` und `frontend/package.json` (dazu die beiden Versionsfelder oben in `frontend/package-lock.json` und `uv.lock` mit `uv lock`) auf die neue Version setzen, per Pull Request nach `main` bringen.
2. Nach dem Merge auf GitHub: *Releases → Draft a new release → Choose a tag*, neuen Tag wie `v0.3.0` eintragen, Ziel `main`, Titel und Beschreibung ausfüllen, *Publish release*.
3. Der Workflow prüft, dass der Tag in `main` liegt und zur Version im Code passt, veröffentlicht `0.3.0`, `0.3` und `latest`, erzeugt den Herkunftsnachweis und prüft, dass das Image ohne Login abrufbar ist.

**Beim allerersten Veröffentlichen schlägt der Job *Publish image* im letzten Schritt fehl. Das ist erwartet.** Neue Pakete sind auf ghcr.io zunächst privat. Das Image ist dann schon hochgeladen und hat seinen Herkunftsnachweis, aber der Schritt *Image must be pullable without login* meldet „cannot be pulled without login“. Einmalig öffentlich stellen:

1. GitHub → Profilbild oben rechts → **Your profile** → Reiter **Packages** → **twinraid**.
2. Rechts **Package settings** → ganz unten im Bereich **Danger Zone** → **Change visibility** → **Public** → den Paketnamen `twinraid` zur Bestätigung eintippen → **I understand the consequences, change package visibility**.
3. Reiter **Actions** → den fehlgeschlagenen Lauf öffnen → oben rechts **Re-run jobs** → **Re-run failed jobs**. Nur der Job *Publish image* läuft erneut; er lädt dasselbe Image noch einmal hoch (gleicher Digest) und meldet am Ende `ok: … is public`.

Der Neustart klappt innerhalb von 24 Stunden. Danach ist das zwischengespeicherte Image-Artefakt abgelaufen; dann stattdessen **Re-run all jobs** wählen. Ab dem zweiten Veröffentlichen ist das Paket öffentlich, und der Job läuft durch.

Herkunft prüfen: `gh attestation verify oci://ghcr.io/mantua1/twinraid:0.3.0 --owner mantua1`

## Lizenz

MIT, siehe [LICENSE](LICENSE).
