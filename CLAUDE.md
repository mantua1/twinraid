# CLAUDE.md – TwinRaid

TwinRaid steuert zwei Unraid-Server über eine Weboberfläche in einem einzigen Docker-Container: Wake-on-LAN, Statusanzeige, Gaming-VM und Spiegel-Backup per rsync.

Das vollständige Konzept ist verbindlich und wird hier eingebunden:

@docs/KONZEPT.md

Wenn eine Anfrage dem Konzept widerspricht: nachfragen, nicht eigenmächtig abweichen. Konzeptänderungen zuerst in `docs/KONZEPT.md` eintragen, dann umsetzen.

## Unverhandelbare Sicherheitsregeln

1. **Der TwinRaid-Container mountet oder verwendet niemals den Docker-Socket** und bekommt nie einen Key mit Docker-Rechten. Container auf Klein hält ausschließlich der Wächter auf dem Host an und startet sie wieder; er nutzt dafür die Docker-CLI des Hosts mit fester Argumentliste (Konzept 3.6, E13).
2. **Konfiguration ist read-only.** Die Weboberfläche zeigt die Konfiguration nur an. Es gibt keine Endpunkte, die Pfade, Hosts, Shares, Befehle oder Konfigurationswerte entgegennehmen.
3. **Keine Shell-Strings.** Externe Programme (ssh, ssh-keyscan) nur mit `subprocess` und Argumentlisten, niemals `shell=True`. Argumente stammen ausschließlich aus der validierten Konfiguration. In den Host-Skripten unter `host-scripts/` gilt dasselbe: Anfragen nur mit festen Texten oder einem einzigen regulären Ausdruck prüfen und nie auswerten (kein `eval`), Variablen immer in Anführungszeichen, `--` vor Namen, shellcheck ohne Befund.
4. **Prozesstrennung einhalten.** Der Web-Prozess hat keinen Zugriff auf Secrets und Share-Daten. Alles mit Außenwirkung läuft über den Worker und dessen feste Befehlsliste (siehe Konzept 3.2). Neue Worker-Befehle, neue Aufträge an den Wächter und neue Anfragen an guard.sh nur nach Rückfrage.
5. **Alles hinter Login.** Jede Route außer Login und `/health` erfordert eine gültige Session. Zustandsändernde Requests erfordern ein CSRF-Token.
6. **Secrets** niemals loggen, niemals ans Frontend senden, niemals committen. `.gitignore` entsprechend pflegen.
7. **Zerstörerische Aktionen** (Spiegeln mit Löschungen, Löschen von Snapshots, Shutdown, Force-Stop) nur mit den Schutzmechanismen aus Konzept 3.6, 3.7, 4.2 und 4.5. Im Code (TwinRaid, Wächter, guard.sh) nie `zfs receive -F` und nie `zfs destroy` mit `-r` oder `-R`; Snapshots löschen nur Wächter und guard.sh nach ihren festen Regeln, nie auf Auftrag von TwinRaid. Einzige Ausnahme ist der Rückweg nach einem Notbetrieb, den der Nutzer von Hand nach `docs/WIEDERHERSTELLUNG.md` ausführt.
8. **Abhängigkeiten:** keine neuen Pakete ohne Rückfrage; Versionen immer pinnen.
9. **Keine externen Ressourcen** im Frontend (keine CDNs, keine externen Schriften).

## Tech-Stack

- Backend: Python 3.14, FastAPI, Pydantic v2, `uv`
- Frontend: React + TypeScript + Vite, Tailwind CSS, shadcn/ui, react-i18next (DE/EN)
- Host-Skripte: Bash mit den Bordmitteln von Unraid (kein Python auf den Servern)
- Tests: pytest (Backend), Vitest (Frontend); Host-Skripte in der CI gegen echtes Docker und ZFS-Pools in Dateien
- Qualität: ruff, mypy (strict), ESLint, Prettier, shellcheck

## Arbeitsweise

- Wir arbeiten in Phasen (Konzept, Abschnitt 8). Nur an der aktuellen Phase arbeiten.
- Vor größeren Änderungen einen kurzen Plan vorstellen und auf Bestätigung warten.
- Jede Integration mit Außenwirkung (Wächter, SSH, ZFS, WoL) bekommt einen Mock. Entwicklung und Tests laufen standardmäßig mit `TWINRAID_MODE=mock`.
- **Echte Server nur ansprechen, wenn der Nutzer es ausdrücklich freigibt.**
- Nach jeder Änderung Tests und Linter ausführen; erst danach als fertig melden.
- Kleine, thematische Commits.
- Der Nutzer kann etwas Python und C#, ist aber kein Profi-Entwickler. Befehle, die er selbst ausführen soll (Git, Docker, Terminal), Schritt für Schritt erklären.

## Konventionen

- Code, Bezeichner und Commit-Messages auf Englisch; Kommentare und Dokumentation auf Deutsch.
- Typ-Annotationen überall; `Any` nur mit begründendem Kommentar.
- Struktur gemäß Konzept, Abschnitt 7. Keine neuen Top-Level-Ordner ohne Rückfrage.
- Alle Texte der Oberfläche über i18n-Dateien, nie fest im Code.
- **Namen der echten Server:** In Repo, Code, Doku, Commits, PRs, Issues und Testprotokollen heißen die Server nur „Klein“ und „Groß“. Keine echten IP- oder MAC-Adressen, Hostnamen, Fingerabdrücke oder UUIDs, keine Namen echter VMs, Container, Shares oder Pools und keine Größen der echten Server. Erlaubt sind `mainpool`, die Unraid-Standardnamen (`appdata`, `domains`, `isos`, `system`) und die Testnamen (`twinraidtest`, `twinraid-testcontainer`). Private Angaben stehen nur im Plan.
