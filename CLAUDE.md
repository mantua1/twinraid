# CLAUDE.md – TwinRaid

TwinRaid steuert zwei Unraid-Server über eine Weboberfläche in einem einzigen Docker-Container: Wake-on-LAN, Statusanzeige, Gaming-VM und Spiegel-Backup per rsync.

Das vollständige Konzept ist verbindlich und wird hier eingebunden:

@docs/KONZEPT.md

Wenn eine Anfrage dem Konzept widerspricht: nachfragen, nicht eigenmächtig abweichen. Konzeptänderungen zuerst in `docs/KONZEPT.md` eintragen, dann umsetzen.

## Unverhandelbare Sicherheitsregeln

1. **Niemals** den Docker-Socket mounten oder verwenden. Container werden ausschließlich über die Unraid-API gesteuert.
2. **Konfiguration ist read-only.** Die Weboberfläche zeigt die Konfiguration nur an. Es gibt keine Endpunkte, die Pfade, Hosts, Shares, Befehle oder Konfigurationswerte entgegennehmen.
3. **Keine Shell-Strings.** Externe Programme (rsync, ssh) nur mit `subprocess` und Argumentlisten, niemals `shell=True`. Argumente stammen ausschließlich aus der validierten Konfiguration.
4. **Prozesstrennung einhalten.** Der Web-Prozess hat keinen Zugriff auf Secrets und Share-Daten. Alles mit Außenwirkung läuft über den Worker und dessen feste Befehlsliste (siehe Konzept 3.2). Neue Worker-Befehle nur nach Rückfrage.
5. **Alles hinter Login.** Jede Route außer Login und `/health` erfordert eine gültige Session. Zustandsändernde Requests erfordern ein CSRF-Token.
6. **Secrets** niemals loggen, niemals ans Frontend senden, niemals committen. `.gitignore` entsprechend pflegen.
7. **Zerstörerische Aktionen** (rsync mit `--delete`, Shutdown, Force-Stop) nur mit den Schutzmechanismen aus Konzept 4.2 und 4.5.
8. **Abhängigkeiten:** keine neuen Pakete ohne Rückfrage; Versionen immer pinnen.
9. **Keine externen Ressourcen** im Frontend (keine CDNs, keine externen Schriften).

## Tech-Stack

- Backend: Python 3.12+, FastAPI, Pydantic v2, `uv`
- Frontend: React + TypeScript + Vite, Tailwind CSS, shadcn/ui, react-i18next (DE/EN)
- Tests: pytest (Backend), Vitest (Frontend)
- Qualität: ruff, mypy (strict), ESLint, Prettier

## Arbeitsweise

- Wir arbeiten in Phasen (Konzept, Abschnitt 8). Nur an der aktuellen Phase arbeiten.
- Vor größeren Änderungen einen kurzen Plan vorstellen und auf Bestätigung warten.
- Jede Integration mit Außenwirkung (Unraid-API, SSH, rsync, WoL) bekommt einen Mock. Entwicklung und Tests laufen standardmäßig mit `TWINRAID_MODE=mock`.
- **Echte Server nur ansprechen, wenn der Nutzer es ausdrücklich freigibt.**
- Nach jeder Änderung Tests und Linter ausführen; erst danach als fertig melden.
- Kleine, thematische Commits.
- Der Nutzer kann etwas Python und C#, ist aber kein Profi-Entwickler. Befehle, die er selbst ausführen soll (Git, Docker, Terminal), Schritt für Schritt erklären.

## Konventionen

- Code, Bezeichner und Commit-Messages auf Englisch; Kommentare und Dokumentation auf Deutsch.
- Typ-Annotationen überall; `Any` nur mit begründendem Kommentar.
- Struktur gemäß Konzept, Abschnitt 7. Keine neuen Top-Level-Ordner ohne Rückfrage.
- Alle Texte der Oberfläche über i18n-Dateien, nie fest im Code.
