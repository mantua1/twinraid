# TwinRaid

Weboberfläche in einem einzigen Docker-Container, die zwei Unraid-Server steuert: Wake-on-LAN, Statusanzeige, Gaming-VM und Spiegel-Backup per rsync.

> **Stand: Phase 4 (Aktionen).** Dashboard mit Live-Status beider Server und der Gaming-VM, eigene CA für HTTPS, Login und alle Schutzmaßnahmen aus Phase 2. Dazu Groß wecken und herunterfahren sowie die Gaming-VM starten, herunterfahren und erzwingen. Im Modus `real` werden die Aktionen einzeln in `twinraid.yaml` unter `actions` freigeschaltet; der Sync folgt in Phase 5.

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

Die Simulation ist interaktiv: Groß und die VM starten ausgeschaltet, die Buttons wirken mit festen Wartezeiten (Groß bootet 20 s, der VM-Dienst braucht weitere 10 s). Weitere Schalter im zweiten Terminal (`off` schaltet jeweils wieder aus):

```bash
./scripts/dev-admin.sh mock-job on                # laufenden Kopierjob vortäuschen
./scripts/dev-admin.sh mock-tour on               # Rundlauf durch alle Zustände und Ursachen
./scripts/dev-admin.sh mock-fault vm-hang on      # VM reagiert nicht auf Herunterfahren
./scripts/dev-admin.sh mock-fault ssh-denied on   # guard.sh lehnt ab
./scripts/dev-admin.sh mock-fault no-boot on      # Groß reagiert nicht auf Wake-on-LAN
./scripts/dev-admin.sh mock-fault other-vm on     # auf Groß läuft eine weitere VM
./scripts/dev-admin.sh mock-fault vm-not-configured on  # power.conf auf Groß fehlt
./scripts/dev-admin.sh mock-fault libvirt-down on # VM-Dienst auf Groß nicht bereit
./scripts/dev-admin.sh mock-fault guard-outdated on  # guard.sh auf Groß zu alt
./scripts/dev-admin.sh mock-fault connect-plugin on  # Unraid Connect mit Flash Backup auf Groß
./scripts/dev-admin.sh show-action-log            # Aktionslog des Workers
```

### Container lokal

```bash
docker/run-local.sh                    # bauen und starten, https://127.0.0.1:8443
docker exec -it twinraid-dev twinraid-admin set-password   # zweites Terminal
docker/smoke-test.sh                   # Starttest mit allen Sicherheitsprüfungen
CHECK_EACH_CAP=1 docker/smoke-test.sh  # zusätzlich: jede Capability ist nötig
docker/ssh-test.sh                     # SSH und Aktionen gegen ein nachgebautes Groß
```

Alle Befehle der Admin-CLI stehen in [docs/EINRICHTUNG.md, Abschnitt 15](docs/EINRICHTUNG.md#15-befehle-der-admin-cli).

Tests sprechen nie echte Server an: Eine Sperre in `backend/tests/conftest.py` lässt nur Verbindungen (auch UDP wie Wake-on-LAN) zu `127.0.0.1` und zum nie gerouteten Testnetz `192.0.2.0/24` zu. Der Starttest prüft den Modus `real` ebenfalls nur mit Adressen aus diesem Testnetz. `docker/ssh-test.sh` startet ein nachgebautes Unraid Groß als Wegwerf-Container (`docker/fake-target`) in einem eigenen Docker-Netz und prüft dagegen `guard.sh`, die Einschränkungen der `authorized_keys`-Zeilen der Keys „status“ und „power“, die strikte Hostkey-Prüfung, die Sitzung `vm-watch` und alle Aktionen.

### openssh-client aktualisieren

Das Image enthält `openssh-client` aus Debian in einer exakt gepinnten Version (`ARG OPENSSH_VERSION` in `docker/Dockerfile`). Debian bietet nach einem Sicherheitsupdate oft nur noch die neue Version an. Dann bricht der Build mit einer Meldung wie dieser ab:

```text
ERROR: openssh-client 1:10.0p1-7+deb13u4 is no longer available from Debian.
ERROR: Debian now offers openssh-client 1:10.0p1-7+deb13u5.
ERROR: Set OPENSSH_VERSION=1:10.0p1-7+deb13u5 in docker/Dockerfile (see README).
```

So hebst du die Version an:

1. Einen Branch anlegen: `git switch -c update-openssh`
2. In `docker/Dockerfile` die Zeile `ARG OPENSSH_VERSION=…` auf die gemeldete Version ändern. Der SSH-Test übernimmt sie automatisch für das nachgebaute Groß.
3. Lokal prüfen: `docker build -f docker/Dockerfile -t twinraid:dev .`, danach `docker/smoke-test.sh` und `docker/ssh-test.sh`.
4. Committen (`git commit -am "build(docker): update openssh-client to …"`), pushen und einen Pull Request öffnen. Gemergt wird erst mit grüner CI.

Die aktuell angebotene Version zeigt auch: `docker run --rm python:3.14-slim-trixie sh -c 'apt-get update -qq && apt-cache policy openssh-client'`.

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
