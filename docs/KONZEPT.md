# TwinRaid – Projektkonzept

> Arbeitstitel „TwinRaid“ · Version 0.11 · Stand 16.10.2026
> Dieses Dokument ist die verbindliche Grundlage für die Entwicklung. Änderungen am Konzept werden hier eingetragen, bevor sie umgesetzt werden.

---

## 1. Ziel

Eine Weboberfläche in **einem einzigen Docker-Container** auf dem stromsparenden Unraid-Server („Unraid Klein“). Darüber kann man:

- den stromhungrigen Unraid-Server („Unraid Groß“) per Wake-on-LAN starten und sauber herunterfahren,
- den Online-Status beider Server und der Gaming-VM sehen,
- die Gaming-VM auf Unraid Groß starten und stoppen,
- einen **Spiegel-Sync** aller konfigurierten Shares von Klein nach Groß starten, inklusive Container-Daten (appdata) und Container-Vorlagen (Templates). Der Sync läuft manuell oder zeitgesteuert und zeigt Live-Fortschritt und Restzeit.

**Ziel des Syncs:** Fällt Unraid Klein aus, wird Unraid Groß manuell gebootet. Dort lassen sich alle Container mit dem Stand des letzten Syncs sofort anlegen und starten.

**Nicht-Ziele:** Zugriff aus dem Internet, Mehrbenutzerverwaltung, Bearbeiten der Konfiguration über die Weboberfläche, Sync in Gegenrichtung (Groß → Klein).

---

## 2. Ausgangslage

| | Unraid Klein | Unraid Groß |
|---|---|---|
| Rolle | läuft immer, Sync-Quelle | läuft bei Bedarf, Sync-Ziel und Gaming-Host |
| Version | Unraid OS 7.3.3 | Unraid OS 7.3.3 |
| Besonderheit | hostet den TwinRaid-Container | Gaming-VM (Bild und USB auf einen TV in einem anderen Raum) |

- Beide Server liegen im selben Subnetz („Server-Netz“) und sind mit 2,5 GbE angebunden. Es sind maximal ca. 1 TB Daten zu synchronisieren.
- Die Weboberfläche muss auch aus einem anderen Subnetz („Client-Netz“) erreichbar sein. Das wird über eine UniFi-Firewall-Regel freigegeben.
- Die Shares haben auf beiden Systemen **identische Namen**, liegen aber auf **unterschiedlichen Pools**.
- Auf Groß sind die Container im Normalbetrieb **ausgeschaltet**, damit es keine Konflikte mit Klein gibt. Auf Groß existieren keine eigenen appdata-Daten oder Templates, die geschützt werden müssen.

---

## 3. Architektur

### 3.1 Überblick

```
  Client-Netz                     Server-Netz
  ┌───────────┐   HTTPS :8443
  │  Browser  │ ──────────────┐
  └───────────┘  (UniFi-Regel)│
                              ▼
  ┌──────────────────────── Unraid KLEIN (immer an) ────────────────────────┐
  │                                                                          │
  │   Container "TwinRaid"  (eigene IP auf br0)                              │
  │   ┌────────────────────┐   Unix-Socket    ┌─────────────────────────┐    │
  │   │ Web-Prozess        │ ───────────────▶ │ Worker-Prozess          │    │
  │   │ unprivilegiert     │  feste Befehle   │ minimal privilegiert    │    │
  │   │ UI, Login, API,SSE │                  │ Status, WoL, API, rsync │    │
  │   └────────────────────┘                  └───────────┬─────────────┘    │
  │                                                       │                  │
  │   Shares (read-only gemountet) ◀──────────────────────┤                  │
  │   Unraid-API Klein (Container stop/start) ◀───────────┤                  │
  └───────────────────────────────────────────────────────┼──────────────────┘
                                                          │
        WoL · Unraid-API (VM-Status, Start, Erzwingen) · SSH (Shutdown, VM-ACPI, rsync)
                                                          ▼
  ┌──────────────────────── Unraid GROSS (bei Bedarf) ──────────────────────┐
  │   Gaming-VM   ·   gespiegelte Shares   ·   Container-Templates           │
  └──────────────────────────────────────────────────────────────────────────┘
```

### 3.2 Zwei Prozesse in einem Container (zentrale Sicherheitsgrenze)

| | Web-Prozess | Worker-Prozess |
|---|---|---|
| Aufgabe | Weboberfläche ausliefern, Login, Sessions, REST-API, Live-Updates (SSE) | alles mit Außenwirkung: Status-Checks, WoL, Unraid-API, SSH/rsync, Zeitsteuerung |
| Benutzer | eigene unprivilegierte UID (z. B. 10001) | root im Container, nach dem Start nur mit Capability `DAC_READ_SEARCH` (darf alles lesen, nichts schreiben) |
| Zugriff auf Secrets | **nein** | ja |
| Zugriff auf Share-Daten | **nein** | nur lesend |

- Die beiden Prozesse kommunizieren über einen Unix-Socket mit einer **festen, kleinen Befehlsliste**: `get_status`, `verify_password`, `wake_target`, `shutdown_target`, `shutdown_target_force_vm` (ab v0.4.1, siehe 4.2), `vm_start`, `vm_stop`, `vm_force_stop`, `sync_start`, `sync_cancel`, `sync_confirm_deletions`.
- Befehle haben **keine frei wählbaren Parameter** (keine Pfade, Hosts oder Shell-Befehle). Erlaubt sind höchstens IDs, die der Worker selbst vergeben hat (z. B. eine Job-ID).
- **Einzige Ausnahme `verify_password` (ab Phase 2):** Der Befehl trägt das eingegebene Passwort, höchstens 1024 Zeichen. Der Worker vergleicht es nur mit dem Argon2id-Hash und verwendet es nie als Pfad, Argument oder Logeintrag. So bleibt der Hash ein Secret, das nur der Worker lesen kann (siehe 5.3).
- **Drosselung im Worker:** Der Worker prüft immer nur ein Passwort gleichzeitig. Jede Prüfung dauert mindestens 1 Sekunde. Nach 5 Fehlversuchen in 15 Minuten wartet er vor jeder weiteren Prüfung zusätzlich, beginnend mit 1 Sekunde und verdoppelt bis höchstens 30 Sekunden. Ein erfolgreicher Login setzt die Zählung zurück, ebenso `twinraid-admin unlock`. So kann auch ein übernommener Web-Prozess nicht schnell durchprobieren.
- **Konsequenz:** Wird der Web-Prozess kompromittiert, kann ein Angreifer nur das tun, was auch die Buttons können. Er kann keine Keys stehlen, keine Daten lesen und keine Pfade ändern.
- **Pfadschutz:** Datenmounts und Secrets liegen unter `/srv/worker` (Rechte `0700`, Besitzer root). Selbst Dateien, die auf den Shares für alle lesbar sind, kann der Web-Prozess dadurch nicht erreichen.

### 3.3 Technik

| Bereich | Wahl |
|---|---|
| Backend | Python 3.14 (lokal über `uv` verwaltet, im Container dasselbe Slim-Image), FastAPI, Pydantic v2, Paketverwaltung mit `uv` |
| Frontend | React + TypeScript + Vite, Tailwind CSS, shadcn/ui, Dark/Light-Mode, mobiltauglich |
| Sprachen der Oberfläche | Deutsch / Englisch umschaltbar (react-i18next) |
| Live-Updates | Server-Sent Events (SSE) |
| Persistenz | SQLite in `/data` (Job-Historie, Sessions, Audit-Log) |
| Zeitsteuerung | im Worker; Zeitplan aus Wochentagen, Uhrzeit und Zeitzone (siehe 4.8); die technische Umsetzung wird in Phase 6 festgelegt |
| Prozessverwaltung | s6-overlay mit read-only Root (entschieden, siehe 5.4) |
| Unraid-Steuerung | offizielle Unraid-GraphQL-API (in Unraid 7.3.3 enthalten), API-Keys mit minimalen Rechten |
| Kopieren | `rsync` über SSH |
| SSH-Client | `openssh-client` aus Debian, Version exakt gepinnt (ab Phase 4); das setuid-Programm `ssh-keysign` wird entfernt |
| Tests | pytest (Backend), Vitest (Frontend); Mocks für alle externen Systeme |
| Qualität | ruff, mypy (strict), ESLint, Prettier; CI über GitHub Actions |
| Veröffentlichung | Image auf `ghcr.io`, gebaut und veröffentlicht nur von GitHub Actions, mit Herkunftsnachweis (siehe 5.8) |

### 3.4 Schnittstellen zu Unraid

| Aufgabe | Weg | Rechte |
|---|---|---|
| Groß online/offline | TCP-Check auf den WebGUI-Port aus `servers.target.webgui` | keine |
| Klein online/offline | keine Prüfung: TwinRaid läuft auf Klein, im Modus `real` gilt Klein deshalb immer als online (ab v0.3.2, siehe 4.1) | keine |
| Wake-on-LAN | UDP-Magic-Packet an die Broadcast-Adresse des Server-Netzes (siehe 4.2) | keine |
| Gaming-VM Status | Unraid-API auf Groß (siehe 3.5) | API-Key nur mit `VMS:READ_ANY` (ab Phase 3) |
| Gaming-VM starten und erzwingen | Unraid-API auf Groß (`vm.start`, `vm.forceStop`) | zusätzlich `VMS:UPDATE_ANY` (ab Phase 4) |
| Gaming-VM herunterfahren | SSH: `guard.sh` führt `virsh shutdown <UUID>` aus, nur ACPI (ab Phase 4, Entscheidung E8) | SSH-Key „power“ |
| Container stoppen/starten (Konsistenz beim Sync) | Unraid-API auf Klein; der Weg dorthin ohne „Host access to custom networks“ ist offen (E7) | API-Key nur mit Docker-Rechten |
| Daten kopieren | rsync über SSH nach Groß | SSH-Key „sync“, per Wrapper auf erlaubte Zielpfade beschränkt |
| Groß herunterfahren | SSH: `guard.sh` führt `/sbin/poweroff` aus, wie der Button in der Unraid-WebGUI (ab Phase 4) | SSH-Key „power“, darf ausschließlich `check`, `poweroff` und `vm-shutdown <UUID>` (siehe 5.6) |

**Kein Docker-Socket. Kein Root-Shell-Zugang auf irgendeinem Server.**

**Ergebnisse der Prüfung in Phase 4** (Quellcode von Unraid-API v4.37.5 und Unraid-WebGUI 7.3):

- Die Unraid-API hat **keine Funktion zum Herunterfahren** des Servers. Groß wird deshalb per SSH heruntergefahren.
- `/sbin/poweroff` ist der saubere Weg: Er startet das Shutdown-Skript von Unraid, das das Array über `emhttpd` stoppt. Dabei stoppt Unraid Container und VMs geordnet (VMs mit der Einstellung *VM shutdown time-out*, Standard 60 s) und hängt die Platten aus; das gesamte Zeitlimit kommt aus *Disk Settings → Shutdown time-out* (Standard 90 s). Der frühere Befehl `powerdown` ist veraltet.
- **Warum die VM nicht über die API heruntergefahren wird (E8):** `vm.stop` sendet zwar ein ACPI-Signal, wartet aber nur 10 Sekunden und schaltet die VM dann selbst hart aus (`destroy`). Windows könnte so mitten im Herunterfahren abgeschaltet werden, etwa während eines Updates, und „Erzwingen erst nach `shutdown_timeout_minutes`“ ließe sich nicht umsetzen. `virsh shutdown` sendet nur das ACPI-Signal, wie „Stop“ in der Unraid-WebGUI. Der Key „power“ darf damit nur VMs herunterfahren; das ist weniger, als er mit `poweroff` ohnehin darf.
- `vm.start` und `vm.forceStop` verlangen `VMS:UPDATE_ANY`. Die Rechteprüfung greift in 4.37.5 auch für diese verschachtelten Befehle (`fieldResolverEnhancers: ['guards']`); der Fehler aus unraid/api #1723 ist dort behoben. Geprüft wird trotzdem real (siehe 5.6).

### 3.5 Verbindung zur Unraid-API (ab Phase 3)

- **Adresse:** `<scheme>://<host>:<port>/graphql` mit den Werten aus `servers.<server>.host` und `servers.<server>.webgui`. Der Worker sendet eine GraphQL-Abfrage per POST mit dem Key im Header `x-api-key`. Für den VM-Status genügt `vms { domains { name state } }`.
- **Aktionen (ab Phase 4):** Der Worker fragt `vms { domains { id name state } }` ab und sucht die VM bei jeder Aktion neu über `gaming_vm.unraid_vm_name`. Die ID (`<Server-ID>:<UUID>`) geht an `vm { start(id: …) }` bzw. `vm { forceStop(id: …) }`, die UUID daraus an `guard.sh` (siehe 5.6). Eine andere VM lässt sich so ohne Codeänderung eintragen (Konfiguration ändern, Container neu starten).
- **Grenzen:** Zeitlimit 5 Sekunden für Abfragen und 60 Sekunden für `vm.start` und `vm.forceStop` (das Starten einer VM mit durchgereichter Grafikkarte kann dauern), Antwort höchstens 256 KiB, Weiterleitungen werden nicht verfolgt.
- **`scheme: http`** (Unraid-Einstellung „SSL/TLS verwenden: Nein“): Der API-Key geht im Klartext durch das Server-Netz. Startlog (im Modus `real`) und Einstellungsseite zeigen deshalb deutlich: „Verbindung zu Unraid Groß unverschlüsselt, API-Key wird im Klartext übertragen“.
- **`scheme: https`** (Unraid-Einstellung „Ja“): Unraid nutzt dann ein selbstsigniertes Zertifikat, eine Prüfung über eine Zertifikatskette ist nicht möglich. TwinRaid vergleicht stattdessen den SHA-256-Fingerabdruck des Zertifikats mit einem bestätigten Wert (Vertrauen beim ersten Kontakt wie bei SSH). Bestätigt wird mit `twinraid-admin trust-api-cert target`: Der Befehl zeigt den Fingerabdruck, den Groß gerade ausliefert, zum Vergleich mit der Anzeige im Browser. Der Key wird erst gesendet, nachdem der Fingerabdruck geprüft ist.
- **Zertifikat geändert:** Erneuert Unraid sein Zertifikat, meldet TwinRaid „Zertifikat geändert“ und sendet den Key nicht mehr. Nach Prüfung im Browser wird der neue Fingerabdruck mit `twinraid-admin trust-api-cert target` bestätigt.
- **Umstellung von http auf https** ohne Codeänderung: in Unraid „Ja“ wählen, in `twinraid.yaml` `scheme: https` (Port 443) eintragen, Container neu starten, `twinraid-admin trust-api-cert target` ausführen. Die Anleitung steht in `docs/EINRICHTUNG.md`.
- Die Einstellung „Strikt“ (Zertifikat für myunraid.net) wird nicht unterstützt; sie bräuchte DNS-Auflösung über das Internet.
- **Weitere VMs (ab v0.4.2):** Aus derselben Abfrage bestimmt der Worker, welche VMs außer `gaming_vm.unraid_vm_name` laufen (jeder Zustand außer `SHUTOFF`, also auch pausiert, im Leerlauf, im Ruhezustand oder abgestürzt). Dafür genügt der Lese-Key. Die Namen dieser VMs sind Text aus der Antwort von Unraid und eine bewusste Ausnahme von der Regel, dass kein fremder Text in den Status gelangt: Sie werden bereinigt (Steuer- und unsichtbare Formatzeichen werden ersetzt, höchstens 64 Zeichen je Name, höchstens 20 Namen), nur über geschützte Routen ausgeliefert und nie geloggt; Aktionslog und Container-Log enthalten nur den festen Code `other_vms_running`.
- **Ursachen bei Fehlern** (siehe 4.1): Server offline (TCP-Check scheitert), API nicht erreichbar, Unraid leitet auf HTTPS um, kein API-Key gesetzt, API-Key ungültig (Antwort 401 bzw. „UNAUTHENTICATED“), keine Leserechte (403 bzw. „FORBIDDEN“), Fingerabdruck nicht bestätigt, Zertifikat geändert, VM nicht gefunden, sonstiger API-Fehler.

---

## 4. Funktionen

### 4.1 Dashboard

Kacheln:

1. **Unraid Klein:** Status. Im Modus `real` immer „Online“ mit der Ursache „TwinRaid läuft auf diesem Server“ (siehe „Prüfung“).
2. **Unraid Groß:** Status mit den Buttons „Wecken“ und „Herunterfahren“.
3. **Gaming-VM:** Status mit den Buttons „Starten“ und „Herunterfahren“. Nach einem Timeout zusätzlich „Erzwingen“: Der Button erscheint, sobald ein Herunterfahren läuft oder gescheitert ist, und wird erst `shutdown_timeout_minutes` nach dem letzten Versuch aktiv (siehe 4.2). Ist ein Herunterfahren von Groß an der VM gescheitert, zeitweise zusätzlich „Erzwingen und danach Server herunterfahren“ (ab v0.4.1, siehe 4.2).
4. **Sync:** letzter Lauf (Ergebnis, Dauer, Datenmenge), nächster geplanter Lauf, Button „Jetzt synchronisieren“, Live-Fortschritt während eines Laufs.
5. **Verlauf:** Liste der letzten Aktionen und Sync-Läufe (Audit-Log).

Statuswerte: `Online`, `Offline`, `Startet …`, `Fährt herunter …`, `Unbekannt`. Zu jedem Status kann eine **Ursache** aus einer festen Liste kommen (z. B. „Server offline“, „API nicht erreichbar“, „API-Key ungültig“, „Zertifikat geändert“, „VM pausiert“; siehe 3.5). So ist bei einem Fehler klar unterscheidbar, ob der Server aus ist, die API nicht antwortet oder der Key nicht passt.

- **Prüfung:** Der Worker prüft alle 10 Sekunden. Groß per TCP-Check, die VM über die API nur, wenn Groß online ist. Ist Groß offline, gilt die VM als aus mit der Ursache „Server offline“. `get_status` liefert sofort den zuletzt geprüften Stand.
- **Klein (ab v0.3.2):** TwinRaid läuft auf Klein. Solange der Worker antwortet, ist Klein also online. Im Modus `real` entfällt deshalb der TCP-Check von Klein; die Kachel zeigt „Online“ mit der festen Ursache „TwinRaid läuft auf diesem Server“. Grund: Ohne „Host access to custom networks“ (siehe 5.1) erreicht der Container seinen eigenen Host nicht, ein TCP-Check würde Klein fälschlich als offline melden.
- **VM-Zustände** (libvirt über die Unraid-API): `RUNNING` und `IDLE` → Online; `PAUSED` → Online, Ursache „pausiert“; `PMSUSPENDED` → Online, Ursache „im Ruhezustand“; `SHUTDOWN` → Fährt herunter …; `SHUTOFF` → Offline; `CRASHED` → Offline, Ursache „abgestürzt“; `NOSTATE` und Unbekanntes → Unbekannt.
- **Live-Updates:** Die Oberfläche öffnet einen SSE-Datenstrom (`GET /api/events`, geschützt). Ein einziger Abfrage-Task im Web-Prozess fragt den Worker alle 2 Sekunden mit dem vorhandenen Befehl `get_status` und schickt Änderungen an alle offenen Datenströme. Es gibt dafür keinen neuen Worker-Befehl. Höchstens 32 Datenströme gleichzeitig.
- **Kopierjob:** Der Status enthält das Feld `job_running` („Kopierjob läuft“). Ab Phase 5 kommt es vom echten Sync. Bis dahin ist es im Modus `real` immer aus und im Simulationsmodus mit `twinraid-admin mock-job on|off` schaltbar. Es steuert die Ausnahme vom Inaktivitäts-Timeout (siehe 5.3).

Destruktive oder störende Aktionen (Herunterfahren, Force-Stop, Sync mit vielen Löschungen) erfordern einen Bestätigungsdialog. Ab Phase 4: Groß herunterfahren, VM herunterfahren und Erzwingen. Wecken und VM starten laufen ohne Dialog.

### 4.2 Aktionen und Sperren

| Aktion | Verhalten / Sperre |
|---|---|
| Groß wecken | nur wenn Groß offline ist. Magic Packet senden, dann „Startet …“, bis Groß per TCP erreichbar ist und die API antwortet, höchstens `boot_timeout_minutes` |
| Groß herunterfahren | gesperrt, solange ein Sync läuft, solange der Zustand der VM oder die Liste der VMs unbekannt ist und solange außer der Gaming-VM weitere VMs auf Groß laufen (ab v0.4.2, siehe unten). Läuft die Gaming-VM, fragt der Dialog „VM zuerst herunterfahren?“. Bestätigen fährt erst die VM herunter (höchstens `shutdown_timeout_minutes`), danach den Server. Bleibt die VM an, bricht TwinRaid ab, der Server läuft weiter und „Erzwingen“ wird angeboten. Ein sofortiges Herunterfahren an der laufenden VM vorbei gibt es nicht (Entscheidung E10). |
| Gaming-VM starten | ist Groß offline: automatisch wecken, warten bis die API die VM kennt (`boot_timeout_minutes`), dann VM starten. Auch bei laufendem Groß wartet der Worker bis zu dieser Zeit auf die API, falls Groß gerade erst gestartet ist |
| Gaming-VM herunterfahren | ACPI-Signal per SSH, dann „Fährt herunter …“, bis die VM aus ist, höchstens `shutdown_timeout_minutes`. Danach Meldung „reagiert nicht“ und „Erzwingen“ |
| Gaming-VM erzwingen | erst `shutdown_timeout_minutes` nach dem letzten erfolgreich gesendeten ACPI-Signal und nur, solange die VM noch läuft; über die API (`vm.forceStop`) |
| Erzwingen und danach Server herunterfahren (ab v0.4.1) | nur nach einem Herunterfahren von Groß, das abgebrochen wurde, weil die VM nicht rechtzeitig herunterfuhr; höchstens 10 Minuten lang und nur, solange sich nichts geändert hat (siehe unten) |
| Sync starten | gesperrt, wenn bereits ein Sync läuft; ist Groß offline, wird es automatisch geweckt |
| Automatisches Herunterfahren nach Sync | nur wenn **der Sync den Server geweckt hat** UND die Gaming-VM nicht läuft UND keine weitere VM auf Groß läuft (Schutz vor weiteren VMs, siehe unten) UND währenddessen keine manuelle Aktion auf Groß stattfand |

**Broadcast-Adresse für Wake-on-LAN (ab Phase 4):** `servers.target.wol_broadcast` wird optional. Fehlt der Wert, verwendet der Worker die Broadcast-Adresse des eigenen Netzes (aus IP und Netzmaske der Schnittstelle), sofern `servers.target.host` in diesem Netz liegt. Liegt Groß in einem anderen Netz, ist die Angabe Pflicht: Ist `actions.wake` freigeschaltet, startet der Container sonst nicht und nennt den Grund; ohne Freischaltung warnt das Startlog nur. So muss der Wert nicht doppelt gepflegt werden. Das Magic Packet (6 × `FF`, dann 16 × die MAC von Groß) geht dreimal an UDP-Port 9.

Alle Aktionen laufen über eine zentrale Sperrlogik im Worker (eine Aktion pro Ressource gleichzeitig).

**Sperrlogik und Abläufe (ab Phase 4):**

- **Ressourcen** sind Groß und die VM. Groß wecken belegt Groß, VM herunterfahren und Erzwingen belegen die VM, VM starten, Groß herunterfahren und „Erzwingen und danach Server herunterfahren“ belegen beide. Ist eine Ressource belegt, wird der Befehl abgelehnt; es gibt keine Warteschlange.
- **Verfügbarkeit:** Der Worker berechnet für jede Aktion, ob sie gerade möglich ist, und sonst den Grund aus einer festen Liste: `not_enabled`, `busy`, `sync_running`, `target_online`, `target_offline`, `target_unknown`, `vm_running`, `vm_off`, `vm_unknown`, `force_not_yet`, `not_offered` (ab v0.4.1: kein gültiges Angebot für „Erzwingen und danach Server herunterfahren“), `other_vms_running` (ab v0.4.2: auf Groß laufen weitere VMs), `tour`. Er liefert das mit `get_status` aus und prüft bei jedem Befehl erneut. Die Oberfläche deaktiviert Buttons nur nach dieser Angabe und nennt die Gründe, die nicht schon aus dem Status hervorgehen.
- **Zwischenzustände:** Solange eine Aktion läuft, meldet der Status „Startet …“ bzw. „Fährt herunter …“ und den aktuellen Schritt: `waking`, `waiting_for_api`, `starting_vm`, `stopping_vm`, `forcing_vm`, `shutting_down`. Während Groß nach dem Wecken bootet, zeigt die VM die Ursache „Wartet auf den Server“ (`target_starting`).
- **Ergebnis:** Je Ressource meldet der Status das letzte Ergebnis aus einer festen Liste (siehe Aktionslog in 5.3). Es bleibt bis zur nächsten Aktion auf dieser Ressource bzw. bis zum Neustart des Workers sichtbar.
- **Meldung und Hinweis (ab v0.4.1):** Die Oberfläche zeigt zu jedem Ergebnis die Meldung als Verlauf. Ein Hinweis, was jetzt zu tun ist, erscheint nur, solange er nach dem aktuellen Status zutrifft: „Erzwingen ist jetzt möglich“ nur, solange Erzwingen angeboten wird; der Verweis auf „Erzwingen und danach Server herunterfahren“ nur, solange das Angebot gilt; „Wake-on-LAN prüfen“ nach `boot_timeout` nur, solange Groß aus ist; der Hinweis nach `shutdown_timeout` nur, solange Groß an ist. Hinweise, die sich nicht prüfen lassen (z. B. fehlende Rechte des API-Keys), bleiben bis zur nächsten Aktion stehen. Eine Ablehnung beim Absenden („eine andere Aktion läuft gerade“) verschwindet mit dem nächsten Live-Status.
- **Erzwingen:** Den Zeitpunkt des letzten ACPI-Signals kennt nur der Worker im Speicher. Ist die VM aus, wird er gelöscht. Nach einem Neustart des Workers muss erneut „Herunterfahren“ gewählt werden.

**Erzwingen und danach Server herunterfahren (ab v0.4.1, Ergänzung zu E10):**

- **Angebot:** Entsteht nur, wenn „Groß herunterfahren“ abbricht, weil die VM nicht innerhalb von `shutdown_timeout_minutes` herunterfuhr (Ergebnis `vm_stop_timeout`). Ein gescheitertes normales „VM herunterfahren“ erzeugt kein Angebot. Das Angebot kennt nur der Worker im Speicher; der Status meldet, bis wann es gilt (`forced_shutdown_until`).
- **Gültigkeit:** höchstens 10 Minuten nach dem Abbruch. Das reicht, um die Meldung zu lesen und nachzusehen, ob jemand die VM gerade benutzt oder Windows Updates installiert; danach kann die Lage eine andere sein, und ein neuer sauberer Versuch erzeugt ein neues Angebot.
- **Verfällt** außerdem, sobald die VM nicht mehr als laufend gemeldet wird, Groß nicht mehr online ist, irgendeine andere Aktion startet (auch das einfache „Erzwingen“), der Worker neu startet, oder mit der Ausführung selbst (einmalig nutzbar).
- **Befehl:** `shutdown_target_force_vm` ohne Parameter. Der Worker nimmt ihn nur an, solange sein eigenes Angebot gilt. Ein übernommener Web-Prozess gewinnt damit nichts, was er nicht schon mit `vm_force_stop` und `shutdown_target` könnte.
- **Ablauf:** VM über die API erzwingen und warten, bis sie aus ist (höchstens 30 s); ist sie danach nicht aus, bricht TwinRaid ab und fährt Groß nicht herunter. Dann Groß sauber herunterfahren wie bei „Groß herunterfahren“.
- **Bestätigung:** Der Dialog warnt deutlich, dass Erzwingen für die VM wie Stecker ziehen ist: Nicht gespeicherte Daten gehen verloren, auch laufende Windows-Updates werden unterbrochen.
- **Aktionslog:** `shutdown_target_force_vm` vom Auslöser `dashboard`, darin automatisch zuerst `vm_force_stop` und danach `shutdown_target` (jeweils mit `part_of`).
- **Feste Zeiten:** Prüfabstand während einer Aktion 3 s; Bestätigung nach `vm.start` höchstens 60 s, nach `vm.forceStop` höchstens 30 s; nach `poweroff` höchstens 15 min, bis Groß nicht mehr erreichbar ist.
- **Nach jeder Aktion** prüft der Worker den Status sofort neu, damit die Oberfläche nicht auf die nächste reguläre Prüfung wartet.

**Schutz vor weiteren VMs beim Herunterfahren von Groß (ab v0.4.2, Ergänzung zu E10):**

- Laufen auf Groß außer der Gaming-VM weitere VMs, schaltet TwinRaid Groß nicht aus. Das gilt für „Groß herunterfahren“, für „Erzwingen und danach Server herunterfahren“ und ab Phase 6 für das automatische Herunterfahren nach einem Sync.
- Grundlage ist die VM-Liste aus 3.5; es braucht keine weiteren Rechte. Als laufend gilt jeder Zustand außer `SHUTOFF`.
- **Anzeige:** Der Button ist gesperrt (`other_vms_running`). Die Begründung nennt den Server und die VMs: „Auf <Servername> laufen noch weitere VMs: <Namen>. Bitte fahre sie zuerst über die Unraid-Oberfläche herunter. Zur Sicherheit schaltet TwinRaid den Server nicht aus, solange andere VMs laufen, damit keine Daten verloren gehen.“
- **Erneute Prüfung im Worker:** unmittelbar vor `poweroff`, also auch nach dem Herunterfahren der Gaming-VM; bei der erzwungenen Variante zusätzlich vor dem Erzwingen, damit die Gaming-VM nicht umsonst hart ausgeschaltet wird. Läuft dann eine weitere VM, bricht der Ablauf mit `other_vms_running` ab; lässt sich die Liste nicht lesen, mit der Ursache aus 3.5 (z. B. `api_unreachable`). Groß bleibt in beiden Fällen an; das Ergebnis steht im Status und im Aktionslog.
- **Liste unbekannt** (z. B. API-Fehler): Herunterfahren bleibt gesperrt (`vm_unknown`), wie bei unbekanntem Zustand der Gaming-VM.

**Aktionen im Echtbetrieb einzeln freischalten (Entscheidung E9):** In `twinraid.yaml` gibt es den optionalen Abschnitt `actions` mit `wake`, `vm` und `shutdown`, alle standardmäßig aus. Er gilt nur im Modus `real`; im Simulationsmodus sind alle Aktionen verfügbar.

| Aktion | braucht |
|---|---|
| Groß wecken | `wake` |
| Groß herunterfahren | `shutdown`; läuft die VM, zusätzlich `vm` |
| VM starten | `vm`; ist Groß aus, zusätzlich `wake` |
| VM herunterfahren, Erzwingen | `vm` |
| Erzwingen und danach Server herunterfahren | `vm` und `shutdown` |

Eine nicht freigeschaltete Aktion lehnt der Worker mit `not_available` ab, die Oberfläche zeigt „nicht freigeschaltet“. Sind alle Aktionen aus, bleibt das Banner „Nur Statusanzeige“. Änderungen wirken nach einem Neustart des Containers. Die Sync-Befehle bleiben bis Phase 5 gesperrt.

### 4.3 Sync-Ablauf

1. **Vorabprüfung:** Groß erreichbar (sonst wecken); SSH-Hostkey stimmt; alle Quell-Shares existieren und sind nicht leer; Templates auf Pool-Pfade prüfen (siehe 4.7).
2. **Dry-Run** (`rsync --dry-run --stats`) pro Share: ermittelt Anzahl und Größe der zu übertragenden Dateien sowie die Anzahl der Löschungen.
3. **Löschschutz:** Würden mehr als `max_deletions` Dateien gelöscht, gilt:
   - Bei manuellem Lauf: Anzeige der Zahl und Freigabe durch den Nutzer.
   - Bei geplantem Lauf: Abbruch mit Benachrichtigung.
4. **Phase A – Hauptkopie bei laufenden Containern:** alle Shares inklusive appdata spiegeln (`rsync -aHAX --delete --numeric-ids …`). Hier fällt die meiste Datenmenge an.
5. **Phase B – Konsistenzlauf:** laufende Container merken, stoppen (außer `never_stop` und dem eigenen Container), appdata erneut spiegeln (nur noch das Delta, typischerweise Sekunden bis wenige Minuten), Container-Templates kopieren.
6. **Container wieder starten:** genau die, die vorher liefen, auch bei einem Fehler in Phase B (`try/finally`).
7. **Abschluss:** Notfall-Info auf Groß schreiben (siehe 4.6), Ergebnis in die Historie eintragen.
8. **Optional:** Groß herunterfahren (Bedingungen siehe 4.2, einschließlich des Schutzes vor weiteren VMs).

**Delta:** rsync überträgt grundsätzlich nur geänderte Dateien, bei geänderten Dateien über das Netzwerk nur die geänderten Blöcke. Folge-Syncs sind deshalb deutlich schneller als der erste.

### 4.4 Fortschritt und Restzeit

- Die Gesamtmenge stammt aus dem Dry-Run: tatsächlich zu übertragende Bytes und Dateien, nicht die Gesamtgröße der Shares.
- Der Fortschritt kommt aus `rsync --info=progress2 --no-inc-recursive`, das zeilenweise ausgewertet wird.
- **Alle 15 Sekunden** wird die Restzeit neu berechnet: verbleibende Bytes geteilt durch den gleitenden Durchschnitt der Übertragungsrate der letzten 60 Sekunden.
- Anzeige: aktuelle Phase, aktuelle Share, Prozent, GB übertragen / gesamt, MB/s, Restzeit, voraussichtliches Ende (Uhrzeit).

### 4.5 Schutzmechanismen beim Spiegeln

- **Verbotene Shares:** `system` und `domains` können niemals als Sync-Share konfiguriert werden (Validierungsfehler beim Start). Grund: Ein Spiegel mit `--delete` würde auf Groß docker.img, libvirt.img und die Disk der Gaming-VM zerstören.
- **Leere-Quelle-Schutz:** Ist eine Quell-Share leer oder nicht gemountet, wird der Sync abgebrochen. Sonst würde der Spiegel alles auf dem Ziel löschen.
- **Löschgrenze:** `max_deletions` (siehe 4.3).
- **Geschützte Zielpfade:** `protect_on_target` wird auf Groß nie gelöscht (rsync-Filter `protect`).
- **Ausschlüsse:** `exclude` wird nicht kopiert (z. B. die eigene appdata von TwinRaid).
- **Templates** werden **ohne** `--delete` kopiert. Eigene Templates auf Groß bleiben erhalten.
- **Eigener Ordner von TwinRaid (ab Phase 5):** Der appdata-Ordner von TwinRaid mit Konfiguration, Secrets und CA-Schlüssel wird beim Sync **immer** ausgeschlossen, fest im Code und unabhängig von `exclude`. Grund: Der Worker kann diese Dateien dank `DAC_READ_SEARCH` lesen und würde sie sonst nach Groß kopieren. Den Ort auf dem Host ermittelt der Worker aus den Mounts von `/config` und `/data`; das genaue Verfahren wird in Phase 5 festgelegt. Liegt der Ordner in einer Share, die gesichert wird, warnt die Konfigurationsprüfung beim Start. Der Eintrag `appdata/twinraid` in `exclude` ist dann nicht mehr nötig.
- **Eigener Container (ab Phase 5):** TwinRaid stoppt sich beim Konsistenzlauf nie selbst. Den eigenen Container erkennt der Worker automatisch (z. B. über die Container-ID in der Unraid-API); das Verfahren wird in Phase 5 festgelegt. `never_stop` gilt dann nur noch für andere Container, der Eintrag `twinraid` entfällt.

### 4.6 Notfall-Wiederherstellung

Nach jedem erfolgreichen Sync schreibt der Worker nach `appdata/_twinraid/` auf Groß (geschützt):

- `last-sync.json`: Zeitpunkt, Shares, Datenmenge, Liste der Container mit Image-Tags,
- `WIEDERHERSTELLUNG.md`: Schritt-für-Schritt-Anleitung, um Groß als Ersatz für Klein in Betrieb zu nehmen. Kurzform: booten → Docker-Dienst bzw. Container aktivieren → „Container hinzufügen“ → Benutzer-Vorlage wählen → Übernehmen. Die Anleitung enthält auch den Rückweg: Sobald Klein wieder läuft, werden die Container auf Groß wieder ausgeschaltet. Wurden auf Groß in der Zwischenzeit Daten verändert, müssen sie vor dem nächsten Sync manuell zurückgeholt werden, sonst überschreibt der Spiegel sie.

Zusätzlich liegt die Anleitung im Repo unter `docs/WIEDERHERSTELLUNG.md`.

### 4.7 Vorabprüfung der Container-Templates

Da die Pools auf beiden Servern unterschiedlich sind, prüft der Worker alle Templates auf Pfade wie `/mnt/<pool>/…`, die es auf Groß nicht gibt, und warnt. Empfehlung: In den Templates `/mnt/user/…` verwenden. Unraid-Shares im Modus „exklusiv“ vermeiden dabei den Performance-Nachteil von `/mnt/user`. Ebenso wird geprüft, ob benutzerdefinierte Docker-Netzwerke aus den Templates auf Groß existieren.

### 4.8 Zeitsteuerung

- Zeitplan als Wochentage plus Uhrzeit in einer festen Zeitzone (z. B. sonntags 03:00, `Europe/Berlin`), optional mit Wecken und Herunterfahren. Ein Cron-Ausdruck wird bewusst nicht verwendet, weil er schwer lesbar ist und Bibliotheken den Wochentag unterschiedlich zählen.
- Die Uhrzeit gilt als Ortszeit der angegebenen Zeitzone. Das Verhalten an den Tagen der Zeitumstellung wird in Phase 6 festgelegt.
- Der manuelle Sync bleibt jederzeit möglich (außer während eines laufenden Syncs).
- Der nächste geplante Lauf wird im Dashboard und im Startlog mit Wochentag und Uhrzeit angezeigt (ab Phase 6).
- Fehler lösen optional eine Unraid-Benachrichtigung über die API aus.

---

## 5. Sicherheitskonzept

### 5.1 Netzwerk

- Keine Portweiterleitung im Router. Kein Zugriff aus dem Internet.
- Der Container bekommt eine **eigene IP im Server-Netz** (Unraid-Netzwerk `br0`, IP fest vergeben).
- **UniFi-Firewall:**
  - Client-Netz → Container-IP nur TCP 8443 erlauben.
  - Optional: Container → Internet sperren. Er braucht zur Laufzeit kein Internet; Images lädt der Docker-Dienst des Hosts.
- **In der App:** Erlaubte Netze kommen aus der Konfiguration (`web.allowed_networks`). Einträge sind ganze Netze (z. B. `192.168.20.0/24`) oder einzelne Adressen (`192.168.10.50/32` oder ohne Maske, IPv6 mit `/128`), beliebig gemischt. Anfragen von anderen Adressen werden vor jeder Verarbeitung mit 403 abgewiesen, auch für `/health` und die statischen Dateien. Maßgeblich ist die IP der TCP-Verbindung; IPv4-Adressen in IPv6-Schreibweise werden vorher umgewandelt. `X-Forwarded-For` wird ignoriert, außer ein Reverse-Proxy ist explizit als vertrauenswürdig konfiguriert (bisher nicht vorgesehen). Abweisungen stehen höchstens einmal pro Minute und IP im Container-Log.
- **Erlaubte Netze richtig wählen (ab v0.3.2):** Eine Router-Firewall filtert nur Verkehr zwischen Netzen, nicht innerhalb eines Netzes. Im Netz, in dem TwinRaid selbst steht (Server-Netz), werden deshalb nur einzelne Geräte als `/32` eingetragen. Sonst könnten andere Container mit eigener IP und VMs, die direkt daneben stehen, TwinRaid ungehindert aufrufen. Andere Netze können ganz eingetragen werden, wenn die Router-Firewall den Zugriff dort regelt (siehe „UniFi-Firewall“).
- **Host-Prüfung (ab Phase 2):** Der `Host`-Header muss eine der verwendeten Adressen (siehe „Eigene Adresse“) oder einer der Namen aus `web.tls.hostnames` sein, sonst 400. Das verhindert DNS-Rebinding, bei dem eine fremde Webseite über einen eigenen Hostnamen auf den Container zugreift.
- **Eigene Adresse (ab v0.3.1):** Die IP des Containers wird nur an einer Stelle festgelegt, im Unraid-Template (Netzwerk `br0`, „Fixed IP address“).
  - `twinraid-init` erkennt beim Start die IPv4-Adressen der Netzwerkschnittstellen im Container. Nie verwendet werden Loopback (`127.0.0.0/8`), Link-Local (`169.254.0.0/16`), Multicast und `0.0.0.0`. IPv6 wird nicht erkannt; IPv6-Adressen nennt das Log nur, sie lassen sich über `web.tls.ip_addresses` ergänzen.
  - Genau eine verwendbare Adresse: Sie gilt für Serverzertifikat (SAN), Namensbeschränkung der CA und Host-Prüfung.
  - Keine oder mehrere verwendbare Adressen: Der Container startet nicht. Die Meldung nennt alle gefundenen Adressen mit Schnittstelle und die Lösung (im Template „Custom: br0“ mit „Fixed IP address“ wählen oder `web.tls.ip_addresses` setzen).
  - Liegt die erkannte Adresse in `172.16.0.0/12` (typisch für Docker-interne Netze), warnt das Startlog deutlich, dass im Template vermutlich „Bridge“ statt „Custom: br0“ gewählt ist. Der Start wird nicht abgebrochen, weil Bridge-Netze mit gesetztem `web.tls.ip_addresses` erlaubt bleiben.
  - Ist `web.tls.ip_addresses` gesetzt, gilt es statt der Erkennung (z. B. 127.0.0.1 für die lokale Entwicklung oder die Host-IP bei Bridge-Netzen).
  - `twinraid-init` legt die verwendeten Adressen und ihre Herkunft (erkannt mit Schnittstelle, oder konfiguriert) in `/run/twinraid-tls/info.json` ab. Der Web-Prozess übernimmt sie von dort und erkennt nichts selbst; so gilt in allen Prozessen dieselbe Entscheidung. Die Admin-CLI verwendet dieselbe Erkennung.
  - Startlog und Einstellungsseite zeigen die verwendete Adresse und ob sie erkannt oder konfiguriert ist. Die erkannte Adresse ist wie alle Konfigurationswerte nur über geschützte Routen erhältlich.
- **Feste MAC-Adresse (ab v0.3.2):** Ohne feste Angabe vergibt Docker ab Version 28 bei jedem Start eines Containers eine neue zufällige MAC-Adresse, auch bei macvlan. Unraid 7.3 stellt die bisherige MAC nur beim Neustart des Docker-Dienstes wieder her, nicht beim Neuanlegen (Update, „Apply“). Damit eine Reservierung im Router (UniFi: feste IP per MAC) dauerhaft passt, bekommt der Container im Unraid-Template eine feste, lokal administrierte MAC im Feld „Fixed MAC address“ (ab Unraid 7.3). Unraid übergibt sie zusammen mit Netzwerk und IP an Docker (`--network=name=br0,ip=…,mac-address=…`).
  - Die echte MAC steht wie die IP nur im Template auf Klein, nie im Repo. Das Template im Repo enthält das Feld `<MyMAC/>` leer.
  - Erzeugt wird sie zufällig mit dem Präfix `02` (lokal administriert, Unicast), siehe `docs/EINRICHTUNG.md`.
  - Geprüft (lokal, Docker 29.9 rootless, Bridge- und internes macvlan-Netz): Ohne feste MAC wechselt sie bei Neuanlage und bei jedem Neustart. Mit fester MAC bleibt sie erhalten, sowohl mit `--network=name=…,ip=…,mac-address=…` als auch mit `--ip` und `--mac-address`. Das Verhalten von Unraid ist aus den Release Notes 7.3.0 und dem Quellcode recherchiert, nicht auf Unraid getestet. Bei der Installation wird die MAC vor und nach einem Update verglichen.
- **Unraid Klein: „Host access to custom networks“ bleibt aus (ab v0.3.2).** Auf Klein läuft Docker mit macvlan auf der Bridge `br0`. Laut Unraid (Release Notes 6.12.4) ist macvlan mit einer Bridge als Parent-Schnittstelle unzuverlässig und führt zu Call Traces. Empfohlen werden ipvlan oder Bridging aus mit dem Netzwerk `eth0`; beides betrifft alle Container mit eigener IP auf Klein. Host access legt auf `br0` eine weitere macvlan-Schnittstelle (`shim-br0`) an und leitet den Verkehr des Hosts darüber (`rc.docker` in Unraid 7.3). Deshalb bleibt die Einstellung aus.
  - Folge: Container mit eigener IP auf `br0` und Klein selbst erreichen sich gegenseitig nicht (Trennung bei macvlan). Das betrifft auch Tests aus dem Unraid-Terminal von Klein, z. B. `ping` auf die IP von TwinRaid.
  - Für den Status spielt das keine Rolle (siehe 4.1). Wie TwinRaid ab Phase 5 die Container auf Klein über die Unraid-API steuert, ist offen (E7).

### 5.2 HTTPS

- **Entschieden: nur HTTPS mit einer kleinen eigenen CA** (ab Phase 3, Entscheidung E3). TwinRaid erzeugt beim ersten Start eine Zertifizierungsstelle (CA), deren Namensbeschränkung nur die eigenen Adressen zulässt, und signiert damit ein separates Serverzertifikat. Auf den Geräten wird einmalig nur das CA-Zertifikat installiert. Danach erscheint keine Warnung mehr, auch nicht, wenn sich das Serverzertifikat erneuert.
- **Warum keine einfache Selbstsignierung mehr** (Phase 2, Recherche in Issue #3): Android installiert nur Zertifikate mit `CA:TRUE` als vertrauenswürdig (Android-Quellcode, CertInstaller), Firefox importiert ebenfalls nur solche als Zertifizierungsstelle. Ein CA-Zertifikat direkt als Serverzertifikat lehnt Firefox ab. Deshalb gibt es zwei Zertifikate.
- **Zielgeräte:** Windows, Linux und Android, jeweils mit Chrome bzw. Edge und Firefox. iOS und macOS werden nicht eingesetzt.

**CA-Zertifikat**

- ECDSA P-256, Signatur SHA-256, Gültigkeit **10 Jahre**. Begründung: Jeder CA-Wechsel bedeutet, alle Geräte neu einzurichten. Ein gestohlener CA-Schlüssel taugt wegen der Namensbeschränkung nur für die eigenen Adressen von TwinRaid, und wer an den Schlüssel kommt, hat ohnehin root im Container. Eine kürzere Laufzeit brächte kaum Sicherheit, aber viel Aufwand.
- `BasicConstraints CA:TRUE` mit `pathLenConstraint 0` (kritisch), `KeyUsage keyCertSign, cRLSign` (kritisch), `ExtendedKeyUsage serverAuth` als zusätzliche Begrenzung, Subject Key Identifier.
- `NameConstraints` (kritisch): erlaubt nur die verwendeten IP-Adressen (erkannt oder aus `web.tls.ip_addresses`, siehe 5.1; IPv4 mit Maske /32, IPv6 mit /128) und die Namen aus `web.tls.hostnames`. Ein DNS-Name erlaubt nach RFC 5280 auch seine Unternamen. Ohne Hostnamen sind DNS-Namen nur unter `invalid` erlaubt (reserviert nach RFC 6761), weil nach RFC 5280 sonst jeder DNS-Name erlaubt wäre.
- Name „TwinRaid CA <JJJJ-MM-TT>“ mit dem Erstellungsdatum, damit sich die CA in den Zertifikatslisten der Geräte wiederfinden lässt.
- **Neu erzeugt** wird die CA nur, wenn sie fehlt, unbrauchbar oder abgelaufen ist, wenn ihre Namensbeschränkung nicht mehr zu den verwendeten Adressen und `web.tls.hostnames` passt (z. B. nach einer neuen IP im Template), oder auf Befehl (`twinraid-admin renew-cert --new-ca`). 90 Tage vor Ablauf warnt das Dashboard.
- **CA-Wechsel unübersehbar (ab v0.3.1):** Wird eine bestehende CA ersetzt (jeder Grund außer der ersten Erzeugung), schreibt `twinraid-init` einen auffälligen mehrzeiligen Block ins Log und vermerkt den Wechsel (Zeitpunkt, Grund, Fingerabdruck der neuen CA) in `/data/tls/ca-change.json`. Das Dashboard zeigt einen Hinweis mit Datum und Grund und der Aufforderung, das neue CA-Zertifikat auf allen Geräten zu installieren. Er bleibt sichtbar, bis er in der Oberfläche per Klick bestätigt wird. Die Bestätigung gilt für genau diese CA (Fingerabdruck), steht in der Datenbank des Web-Prozesses und im Audit-Log. Der zugehörige Endpunkt nimmt keine Daten entgegen und erfordert wie jeder zustandsändernde Request das CSRF-Token.

**Serverzertifikat**

- ECDSA P-256, Signatur SHA-256, von der CA signiert, Gültigkeit 825 Tage, höchstens bis zum Ablauf der CA.
- `BasicConstraints CA:FALSE` (kritisch), `KeyUsage digitalSignature` (kritisch), `ExtendedKeyUsage serverAuth`, Subject und Authority Key Identifier.
- **Subject leer**, Subject Alternative Name aus den verwendeten IP-Adressen und `web.tls.hostnames`. Bei leerem Subject muss die SAN-Erweiterung nach RFC 5280 **kritisch** sein. Das leere Subject verhindert, dass ein Prüfprogramm einen Namen im Subject gegen die Namensbeschränkung prüft. Der Container-Starttest prüft beides.
- Der Web-Prozess liefert Serverzertifikat und CA-Zertifikat als Kette aus.

**Erneuerung des Serverzertifikats**

- Erneuert wird, wenn weniger als 60 Tage übrig sind, wenn die SAN nicht mehr zu `web.tls` passen, wenn das Zertifikat nicht von der aktuellen CA stammt oder auf Befehl (`twinraid-admin renew-cert`). Die Geräte merken davon nichts.
- Beim Container-Start erledigt das `twinraid-init`.
- Im laufenden Betrieb prüft der Web-Prozess stündlich die Restlaufzeit. Ist eine Erneuerung fällig, vermerkt er den Zeitpunkt in `/data/web/tls-renewal.json` und beendet sich geordnet. s6 startet ihn neu; das Startskript des Web-Dienstes führt vorher als root die Erneuerung aus (`twinraid-init --refresh-tls`, gleiche Rechte wie `twinraid-init`). So liegt der CA-Schlüssel nie in einem dauerhaft laufenden Prozess. Sessions bleiben erhalten, offene Live-Datenströme verbinden sich neu.
- **Keine Neustart-Schleife:** Der Web-Prozess beendet sich höchstens einmal in 24 Stunden für eine Erneuerung. Der Vermerk liegt in `/data/web`, weil nur der Web-Prozess dort schreiben darf und die Entscheidung trifft; er übersteht Neustarts. Scheitert die Erneuerung, läuft das bisherige Zertifikat weiter, das Log nennt den Grund und das Dashboard zeigt die Warnung „automatische Erneuerung fehlgeschlagen“. Ab 30 Tagen vor Ablauf warnt das Dashboard in jedem Fall.

**Ablage und Rechte**

- CA, Serverzertifikat und beide Schlüssel liegen in `/data/tls` (root, `0700`, Dateien `0600`). Den CA-Schlüssel benutzen nur `twinraid-init` und `twinraid-admin renew-cert`.
- Der Startdienst stellt dem Web-Prozess unter `/run/twinraid-tls` bereit (nur im RAM; Verzeichnis `0750`, Gruppe 10001): die Kette aus Server- und CA-Zertifikat (`cert.pem`), den Serverschlüssel (`key.pem`, `0640`), das CA-Zertifikat (`ca.pem`) und `info.json` (Herkunft, Ergebnis der letzten Erneuerung). Der Serverschlüssel ist das einzige Secret, das der Web-Prozess hält, weil er TLS selbst abwickelt. An `/data/secrets` und `/data/tls` kommt er nicht heran.

**Übergang vom selbstsignierten Zertifikat aus Phase 2**

- Beim ersten Start mit Phase 3 erkennt `twinraid-init` das alte Paar (`/data/tls/cert.pem` und `key.pem`), erzeugt CA und Serverzertifikat, löscht das alte Paar und schreibt einen Hinweis ins Log.
- Auf den Geräten wird die alte Ausnahme bzw. das alte installierte Zertifikat entfernt und einmalig die CA installiert (siehe `docs/EINRICHTUNG.md`).

**Eigenes Zertifikat**

- Liegen `/config/tls/cert.pem` und `/config/tls/key.pem` vor, haben sie Vorrang, z. B. später Let's Encrypt über DNS-Challenge. Der Startdienst prüft, dass beide lesbar sind, zusammenpassen und das Zertifikat nicht abgelaufen ist; sonst startet der Container nicht. Auf dem Host darf der Schlüssel root gehören und `0600` haben.
- CA und Serverzertifikat werden trotzdem gepflegt, damit ein Wechsel zurück ohne Neueinrichtung der Geräte möglich ist. Der CA-Download entfällt in dieser Zeit mit einem Hinweis; die automatische Erneuerung betrifft das eigene Zertifikat nicht.

**Anzeige und Download**

- Startlog und `twinraid-admin show-cert-fingerprint`: SHA-256-Fingerabdrücke von CA und Serverzertifikat, für die CA zusätzlich SHA-1, weil Windows bei der Installation nur diesen anzeigt.
- Einstellungen (nach dem Login): CA-Zertifikat herunterladen als `twinraid-ca.crt` im DER-Format, das Android sicher erkennt. Es wird als normaler Download ausgeliefert, damit Android-Browser nicht selbst versuchen, es zu installieren. Dazu die Fingerabdrücke und die Gültigkeit von CA und Serverzertifikat.
- Dashboard: Warnung 90 Tage vor Ablauf der CA und 30 Tage vor Ablauf des Serverzertifikats.

**Protokoll**

- **Nur HTTPS** auf `web.port`, TLS 1.2 und 1.3, keine schwachen Cipher. Es gibt keinen HTTP-Port; HTTP-Anfragen an den Port scheitern am TLS-Handshake.
- **Kein HSTS:** Browser ignorieren HSTS bei IP-Adressen. Bei einem Hostnamen würde HSTS nach einem CA-Wechsel die Warnung unüberspringbar machen und den Nutzer aussperren. Ohne HTTP-Port bringt HSTS keinen Gewinn.
- Alle Ressourcen (Schriften, Skripte, Icons) sind im Image enthalten; es werden keine externen CDNs geladen. Das ermöglicht eine strikte Content-Security-Policy.

**Geräte** (Anleitung in `docs/EINRICHTUNG.md`, Test in Issue #3)

- Windows: CA in „Vertrauenswürdige Stammzertifizierungsstellen“ des Benutzers. Edge und Chrome nutzen diesen Speicher, Firefox übernimmt selbst installierte CAs ab Version 120 automatisch.
- Linux: Chrome über seine Zertifikatsverwaltung, Firefox über den eigenen Import als Zertifizierungsstelle.
- Android: Installation als CA-Zertifikat in den Systemeinstellungen; Chrome und Firefox (ab Version 120) übernehmen sie. Android zeigt danach dauerhaft einen Hinweis, dass das Netzwerk überwacht werden könnte.
- Chrome erzwingt Namensbeschränkungen in lokal installierten Vertrauensankern (Chrome-Unternehmensrichtlinien). Für Firefox und Android wird das im Test geprüft.

### 5.3 Login und Sessions

- Ein Benutzer, Login nur mit Passwort. Das **Passwort wird ausschließlich über die Admin-CLI im Container gesetzt** (`twinraid-admin set-password`). Es gibt keine Ersteinrichtung über die Webseite, die ein Angreifer im Netz zuerst ausführen könnte.
- Ohne Login erreichbar sind nur `/health` (liefert nur „ok“), die Oberfläche selbst (`index.html`, JavaScript, CSS, Icon), `GET /api/auth/state` und `POST /api/auth/login`.
- **Geschützt per Default:** Jede Route ist automatisch geschützt. Öffentliche Routen müssen im Code ausdrücklich markiert werden. Die Oberfläche ist öffentlich, weil sie die Login-Seite enthält. Sie enthält keine Daten; alle Daten kommen über geschützte Routen unter `/api`.
- **Keine Konfigurationswerte in öffentlichen Dateien:** Die ausgelieferten Frontend-Dateien und alle anderen öffentlichen Routen enthalten keine Werte aus der Konfiguration, also keine IP-Adressen, Netze, MAC-Adressen, Hostnamen, Share-Namen, Pfade, Container-, VM- oder Servernamen. Solche Werte gibt es nur über geschützte Routen unter `/api`. Abgesichert durch einen Backend-Test und durch den Container-Starttest, der eine Konfiguration mit eindeutigen Testwerten verwendet und alle öffentlich ausgelieferten Dateien danach durchsucht.
- `GET /api/auth/state` liefert nur, ob eine Session besteht, ob der Simulationsmodus aktiv ist und ob ein Passwort gesetzt ist. Die Login-Seite zeigt damit das Banner „Simulationsmodus“ und, falls kein Passwort gesetzt ist, den Hinweis auf `twinraid-admin set-password`. Ihre Sprache kommt aus der gespeicherten Wahl oder der Browsersprache.
- Unbekannte Pfade werden wie geschützte Routen behandelt. Ohne Session gibt es also 401 statt 404.
- Ein Test stellt sicher, dass es außer den markierten Routen keine ungeschützte Route gibt.

**Passwort**

- Der Argon2id-Hash liegt in `/data/secrets/password.argon2` (Datei `0600`, root). Der Worker prüft ihn über `verify_password` (siehe 3.2). Grund: Der Hash ist ein Secret, und der Web-Prozess darf keine Secrets lesen. Ein übernommener Web-Prozess kann ihn daher nicht für einen Offline-Angriff kopieren; das schützt auch bei Wiederverwendung des Passworts an anderer Stelle.
- Parameter nach RFC 9106 (zweite Empfehlung): 64 MiB Speicher, 3 Durchläufe, Parallelität 4, Salt 16 Byte, Hash 32 Byte.
- Länge mindestens 12, höchstens 1024 Zeichen, keine Steuerzeichen.
- Der Vergleich ist zeitkonstant. Ist kein Passwort gesetzt, läuft trotzdem eine Prüfung gegen einen Dummy-Hash; jeder Login schlägt dann fehl. Session-IDs und CSRF-Tokens werden mit `hmac.compare_digest` verglichen.

**Sessions**

- Serverseitig in der SQLite-Datenbank des Web-Prozesses (`/data/web`). Die Session-ID hat 256 Bit Zufall und wird in der Datenbank nur als SHA-256-Hash gespeichert.
- Cookie `__Host-twinraid_session` mit `HttpOnly`, `Secure`, `SameSite=Strict`, `Path=/`, ohne `Domain`.
- Inaktivitäts-Timeout `web.session_idle_minutes` (Standard **10 Minuten**), maximale Lebensdauer fest 12 Stunden.
- **Live-Datenstrom (SSE) und Inaktivität:** Ein offener Datenstrom (`GET /api/events`) zählt nicht als Aktivität. Auch sein Aufbau und automatische Neuverbindungen verlängern die Session nicht. Alle 15 Sekunden prüft er, ob seine Session noch gilt, und beendet sich sonst; die Oberfläche zeigt dann den Login.
- **Ausnahme während eines Kopierjobs:** Solange `job_running` gesetzt ist (siehe 4.1), wird eine Session nicht wegen Inaktivität beendet, aber nur, wenn für sie ein Dashboard tatsächlich geöffnet ist, also ein Datenstrom dieser Session läuft. Der Datenstrom setzt dazu alle 15 Sekunden die letzte Aktivität. Sessions ohne offenes Dashboard laufen normal ab.
- **Nach dem Ende des Jobs** setzt der Datenstrom die letzte Aktivität einmal auf das Ende des Jobs. Die 10 Minuten beginnen dann neu, damit das Ergebnis noch sichtbar ist.
- Die maximale Lebensdauer von 12 Stunden gilt immer, auch während eines Jobs.
- Ein Logout oder Ablauf der Session beendet nie den Kopierjob. Der Web-Prozess sendet beim Ende einer Session keinen Befehl an den Worker; ein Test sichert das ab.
- Jeder Login erzeugt eine neue Session-ID; ein mitgebrachtes Cookie wird verworfen. Logout löscht die Session in der Datenbank und das Cookie. `twinraid-admin set-password` beendet alle Sessions.

**CSRF und Herkunft**

- Jede Session hat ein eigenes CSRF-Token (256 Bit). Die Oberfläche holt es nach dem Login über eine geschützte Route und sendet es bei jedem POST, PUT, PATCH und DELETE im Header `X-CSRF-Token`.
- Zusätzlich muss bei diesen Requests der `Origin`-Header vorhanden sein und genau zur eigenen Adresse passen, auch beim Login.
- Dazu kommt die Host-Prüfung gegen DNS-Rebinding (siehe 5.1).

**Rate-Limit**

- **Pro IP:** 5 Fehlversuche in 15 Minuten sperren diese IP für 15 Minuten (Antwort 429, keine Passwortprüfung). IPv6-Adressen werden pro /64-Netz gezählt.
- **Global ohne harte Sperre:** Ab 20 Fehlversuchen insgesamt in 15 Minuten wartet jeder Login-Versuch 2 Sekunden länger. Höchstens 2 Prüfungen laufen gleichzeitig; weitere Versuche bekommen sofort 429 mit `Retry-After`. Von einer nicht gesperrten IP kommt der Nutzer also immer herein, schlimmstenfalls nach kurzer Wartezeit.
- Dazu kommt die Drosselung im Worker (siehe 3.2).
- Ein erfolgreicher Login setzt die Zähler der eigenen IP zurück. Sperren liegen in SQLite und überstehen einen Neustart. **Notfall:** `twinraid-admin unlock` hebt alle Sperren und Zähler sofort auf, auch die im Worker.

**Security-Header** (an jeder Antwort, auch 401, 403, 413 und 429)

```
Content-Security-Policy: default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; font-src 'self'; connect-src 'self'; manifest-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'; object-src 'none'
X-Frame-Options: DENY
Referrer-Policy: no-referrer
X-Content-Type-Options: nosniff
Permissions-Policy: camera=(), microphone=(), geolocation=(), usb=(), payment=()
Cross-Origin-Opener-Policy: same-origin
Cross-Origin-Resource-Policy: same-origin
```

- `/api`-Antworten haben zusätzlich `Cache-Control: no-store`.
- Keine Inline-Skripte und keine Inline-Styles im HTML.
- Requests über 16 KiB werden mit 413 abgewiesen.

**Audit-Log**

- Tabelle in der SQLite-Datenbank des Web-Prozesses: Zeit (UTC), Client-IP, Aktion, Ergebnis. Aktion und Ergebnis stammen aus festen Listen; kein Feld übernimmt Text aus Requests.
- Phase 2 protokolliert: Login erfolgreich, fehlgeschlagen, gesperrt oder ohne gesetztes Passwort; Beginn einer Sperre; Logout; abgelaufene Session; fehlgeschlagene CSRF- oder Origin-Prüfung; die CLI-Befehle `set-password`, `renew-cert` und `unlock`. Gleiche Abweisungen derselben IP werden höchstens einmal pro Minute eingetragen.
- Ab Phase 3 zusätzlich die CLI-Befehle `set-api-key` und `trust-api-cert`.
- Ab v0.3.1 zusätzlich die Bestätigung des Hinweises zum CA-Wechsel.
- Ab Phase 4 zusätzlich jede Anforderung einer Aktion über die Oberfläche (`wake_target`, `shutdown_target`, `vm_start`, `vm_stop`, `vm_force_stop`, ab v0.4.1 `shutdown_target_force_vm`) mit dem Ergebnis `requested` (vom Worker angenommen) oder `rejected`, sowie die CLI-Befehle `init-ssh-keys` und `trust-host-key`. Wie die Aktion ausging, steht im Aktionslog des Workers.
- **Nie gespeichert oder geloggt:** Passwörter, Session-IDs, CSRF-Tokens, Cookies, Request-Inhalte.
- Aufbewahrung 180 Tage und höchstens 50.000 Einträge; aufgeräumt wird beim Start und stündlich.
- Die Kachel „Verlauf“ zeigt die letzten 50 Einträge über eine geschützte Route.
- **Ab Phase 4:** Aktionen mit Außenwirkung (WoL, VM, Shutdown, Sync) protokolliert der Worker zusätzlich in einem eigenen Log unter `/data/worker`, das der Web-Prozess nicht verändern kann.

**Aktionslog des Workers (ab Phase 4)**

- Datei `/data/worker/actions.jsonl`, eine JSON-Zeile pro Eintrag. Ordner `0700` und Datei `0600`, beide root: Der Web-Prozess kann das Log weder lesen noch verändern.
- Felder: `at` (UTC), `id` (vom Worker vergeben, verbindet Beginn und Ende), `action`, `trigger` (`dashboard` oder `automatic`; bei `automatic` zusätzlich `part_of` mit der auslösenden Aktion, z. B. das Wecken beim VM-Start), `result` (`started` oder das Ergebnis aus einer festen Liste).
- Ergebnisse: `success`, `wol_failed`, `boot_timeout`, die API-Ursachen aus 3.5 (`api_unreachable`, `api_redirected`, `api_key_missing`, `api_key_invalid`, `api_forbidden`, `api_cert_untrusted`, `api_cert_changed`, `api_error`, `vm_not_found`), `vm_start_timeout`, `vm_stop_timeout`, `ssh_key_missing`, `ssh_host_untrusted`, `ssh_host_key_mismatch`, `ssh_auth_failed`, `ssh_unreachable`, `ssh_denied`, `ssh_failed`, `remote_failed`, `shutdown_timeout`, `other_vms_running` (ab v0.4.2: Groß wurde nicht ausgeschaltet, weil weitere VMs liefen), `interrupted` (Worker beendet), `internal_error`. Kein Feld übernimmt Text aus fremden Antworten.
- Jeder Eintrag steht zusätzlich im Container-Log. Ab 1 MiB wird die Datei nach `actions.jsonl.1` umbenannt (genau eine ältere Datei).
- Anzeige mit `twinraid-admin show-action-log`.

### 5.4 Container-Härtung

- `--read-only`, beschreibbar sind nur `/data` und ein `tmpfs` für `/tmp`.
- `--cap-drop=ALL`. Nur die beim Start nötigen Capabilities werden hinzugefügt; danach behält der Worker nur `DAC_READ_SEARCH`.
- `--security-opt=no-new-privileges`.
- Nicht `--privileged`, kein Docker-Socket.
- Datenmounts read-only unter `/srv/worker` (siehe 3.2).
- Schlankes Basis-Image mit gepinnter Version, regelmäßige Neubauten, Dependabot, optional Image-Scan (Trivy) in der CI.

#### Prozessverwaltung mit s6-overlay (Entscheidung E5)

- s6-overlay startet und überwacht beide Prozesse. Es ist für Container gebaut, sehr klein und braucht keinen zusätzlichen Interpreter, der als root läuft.
- **Read-only Root:** `S6_READ_ONLY_ROOT=1`. `/run` ist ein `tmpfs` und darf nicht `noexec` sein, weil s6 dort seine Dienstverzeichnisse anlegt und ausführt. `/tmp` ist ebenfalls ein `tmpfs`.
- **Startprüfung:** Ein einmaliger Dienst prüft vor dem Start der beiden Prozesse den Betriebsmodus (siehe 6.4) und die Konfiguration (siehe 6.3). Er läuft bereits als unprivilegierter Web-Benutzer. Schlägt er fehl, beendet sich der Container (`S6_BEHAVIOUR_IF_STAGE2_FAILS=2`).
- **Startdienst `twinraid-init` (ab Phase 2):** Läuft nach der Startprüfung als root mit GID 10001, Bounding-Set nur `DAC_READ_SEARCH` (zum Lesen eines eigenen Schlüssels unter `/config/tls`). Er prüft und korrigiert die Datenablage (siehe 5.9) und richtet TLS ein (siehe 5.2). Schlägt er fehl, startet der Container nicht. Ab Phase 3 ruft auch das Startskript des Web-Dienstes vor jedem Start des Web-Prozesses `twinraid-init --refresh-tls` mit denselben Rechten auf, um ein fälliges Serverzertifikat zu erneuern. Scheitert das, startet der Web-Prozess mit dem bisherigen Zertifikat.
- **Healthcheck (ab Phase 2):** Der Docker-Healthcheck läuft als Web-Benutzer ohne Capabilities und sendet keine HTTP-Anfrage. Er baut nur eine TLS-Verbindung zu `127.0.0.1:<web.port>` auf und vergleicht das ausgelieferte Serverzertifikat mit dem ersten Zertifikat in `/run/twinraid-tls/cert.pem`. Der Netzfilter arbeitet auf HTTP-Ebene und wird dadurch nicht berührt; es gibt keine Ausnahme für Loopback. Zusätzlich prüft er, dass der Worker seinen Herzschlag (`/run/twinraid/worker.heartbeat`, alle 10 Sekunden) aktualisiert.
- **Rechte abgeben:** Beide Prozesse werden über `setpriv` gestartet, jeweils mit `no_new_privs`.
  - Web-Prozess: UID und GID 10001, keine zusätzlichen Gruppen, keine Capabilities, leeres Bounding-Set.
  - Worker: UID 0, GID 10001, keine zusätzlichen Gruppen, Bounding-Set nur `DAC_READ_SEARCH`, `umask 077`. Die GID 10001 dient nur dazu, dass der Socket der Gruppe des Web-Prozesses gehört. Wegen `umask 077` legt der Worker keine Dateien an, die diese Gruppe lesen kann.
- **Unix-Socket:** `/run/twinraid/worker.sock`. Verzeichnis `0750`, Socket `0660`, Gruppe 10001. Der Worker prüft zusätzlich per `SO_PEERCRED`, dass der Aufrufer die UID des Web-Prozesses hat, und weist alle anderen ab.
- **Capabilities:** Der Container startet mit `--cap-drop=ALL` und bekommt nur die folgenden Capabilities zurück. Ein Starttest prüft die Liste: Der Container startet, beide Prozesse laufen mit den erwarteten Rechten und der Container stoppt sauber. Die Prozesse von s6 selbst behalten diese Capabilities, führen aber keine Aktionen mit Außenwirkung aus.

| Capability | wofür | wer behält sie nach dem Start | Starttest ohne sie |
|---|---|---|---|
| `SETUID` | Web-Prozess und Startprüfung auf UID 10001 umstellen | nur s6 | Container startet nicht |
| `SETGID` | Gruppen von Web-Prozess, Startprüfung und Worker setzen | nur s6 | Container startet nicht |
| `SETPCAP` | Bounding-Set von Web-Prozess und Worker verkleinern | nur s6 | Container startet nicht |
| `KILL` | s6 darf dem Web-Prozess (andere UID) beim Stoppen Signale senden | nur s6 | Start klappt, Stoppen nur noch per SIGKILL |
| `DAC_READ_SEARCH` | Worker liest Shares und Templates trotz fremder Dateirechte | Worker | in Phase 1 ohne Wirkung, ab Phase 5 nötig |

Bestätigt durch den Starttest `docker/smoke-test.sh` (Phase 1). Mit `CHECK_EACH_CAP=1` lässt er jede Capability einzeln weg. Nicht nötig sind insbesondere `CHOWN`, `FOWNER` und `DAC_OVERRIDE`.

Startoptionen für Docker, entsprechend später im Unraid-Template:

```
--read-only
--tmpfs /run:rw,exec,nosuid,nodev,mode=0755,size=16m
--tmpfs /tmp:rw,noexec,nosuid,nodev,size=16m
--cap-drop=ALL
--cap-add=SETUID --cap-add=SETGID --cap-add=SETPCAP --cap-add=KILL --cap-add=DAC_READ_SEARCH
--security-opt=no-new-privileges
-e TWINRAID_MODE=…
```

### 5.5 Secrets

- Gespeichert in `/data/secrets` (Rechte `0700`, root). Nur der Worker hat Zugriff.
- Angelegt über die Admin-CLI im Container (aufrufbar über die Container-Konsole in Unraid, also nur für Unraid-Admins):
  - `twinraid-admin set-password`: fragt das Passwort zweimal ohne Anzeige ab, nie als Argument (Shell-Historie). Für Skripte und Tests gibt es `--stdin`. Beendet alle Sessions.
  - `twinraid-admin show-cert-fingerprint`, `twinraid-admin unlock` (Phase 2, siehe 5.2 und 5.3).
  - `twinraid-admin renew-cert` erneuert das Serverzertifikat (Geräte merken nichts); `renew-cert --new-ca` erzeugt eine neue CA, die auf allen Geräten neu installiert werden muss (Phase 3, siehe 5.2).
  - `twinraid-admin set-api-key target` (Phase 3): fragt den API-Key von Groß ohne Anzeige ab (für Skripte `--stdin`), prüft das Format wie Unraid (nur `A–Z`, `a–z`, Ziffern, `-` und `_`, höchstens 512 Zeichen) und speichert ihn. Bei `scheme: https` ohne bestätigten Fingerabdruck folgt die Bestätigung wie bei `trust-api-cert`. Im Modus `real` macht der Befehl danach eine Testabfrage und meldet verständlich, was passt und was nicht. `set-api-key source` folgt in Phase 5.
  - `twinraid-admin trust-api-cert target` (Phase 3): zeigt den SHA-256-Fingerabdruck des Zertifikats, das Groß gerade ausliefert, und speichert ihn nach Bestätigung (siehe 3.5). Für Skripte kann der erwartete Fingerabdruck mit `--fingerprint` übergeben werden; er muss genau übereinstimmen. Nur im Modus `real` und nur bei `scheme: https`.
  - `twinraid-admin mock-job on|off` (Phase 3, nur im Simulationsmodus): schaltet das Feld `job_running` der Simulation, um die Sitzungsregel aus 5.3 auszuprobieren.
  - `twinraid-admin init-ssh-keys`: erzeugt ed25519-Keys „sync“ und „power“ und gibt die fertigen `authorized_keys`-Zeilen inklusive Einschränkungen aus. In Phase 4 nur „power“: Der Key wird nur angelegt, wenn er fehlt; sonst gibt der Befehl die Zeile zum vorhandenen Key erneut aus. `--new` ersetzt ihn (die Zeile auf Groß muss dann ebenfalls ersetzt werden). Die Zeile enthält `from=` mit der verwendeten Adresse des Containers (siehe 5.1).
  - `twinraid-admin trust-host-key target`: holt den ed25519-Hostkey von Groß (`ssh-keyscan`), zeigt seinen SHA-256-Fingerabdruck zum Vergleich mit `ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub` auf Groß und speichert ihn nach Bestätigung mit `yes`. Für Skripte kann der erwartete Fingerabdruck mit `--fingerprint` übergeben werden. Danach gilt strikte Hostkey-Prüfung. Ist der Key „power“ vorhanden, folgt ein Verbindungstest mit `check`, der nichts verändert. Nur im Modus `real`.
  - `twinraid-admin show-action-log [--lines N]` (Phase 4): zeigt die letzten Einträge des Aktionslogs (siehe 5.3).
  - `twinraid-admin mock-tour on|off` und `twinraid-admin mock-fault vm-hang|api-forbidden|ssh-denied|no-boot on|off` (Phase 4, nur im Simulationsmodus, siehe 6.4).
- Secrets stehen niemals in der YAML-Datei, in Git, in Logs oder im Frontend.
- Die Befehle für den SSH-Key „power“ kommen in Phase 4, der Key „sync“ und `set-api-key source` in Phase 5.
- Der Worker liest API-Key und Fingerabdruck bei jeder Prüfung neu; nach `set-api-key` oder `trust-api-cert` ist kein Neustart nötig.
- Die CLI läuft als root. Zugriffe auf die Datenbank des Web-Prozesses erledigt sie in einem Kindprozess als Web-Benutzer, weil root ohne `DAC_OVERRIDE` dort nicht schreiben darf. Der Container braucht dafür keine zusätzliche Capability.

### 5.6 Rechte auf den Unraid-Servern (Least Privilege)

- **API-Key Klein:** nur Docker-Steuerung (Container stoppen/starten, lesen).
- **API-Key Groß:** Ab Phase 3 ein Key **ohne Rolle** mit der einzigen Berechtigung `VMS:READ_ANY` (in der Unraid-Oberfläche: Ressource „VMS“, Aktion „Read (any)“). Die Rolle „Viewer“ ist zu breit: Sie darf laut Quellcode der Unraid-API alle Bereiche außer API-Keys lesen. Ab Phase 4 kommt `VMS:UPDATE_ANY` für Start und Stopp hinzu.
- **Rechte werden getestet:** In älteren API-Versionen gab es einen Fehler, durch den ein nur lesender Key Container und VMs starten und stoppen konnte (GitHub-Issue unraid/api #1723). In Phase 4 wird geprüft, dass ein Key ohne die nötigen Rechte tatsächlich abgewiesen wird.
- **SSH-Key „sync“ auf Groß (Phase 5):**
  `restrict,from="<Container-IP>",command="/bin/bash /boot/config/twinraid/guard.sh sync" ssh-ed25519 …`
  `guard.sh` erlaubt ausschließlich rsync im Server-Modus mit Ziel in den konfigurierten Shares bzw. im Template-Ordner. Bis dahin lehnt der Modus `sync` alles ab.
- **SSH-Key „power“ auf Groß (ab Phase 4):**
  `restrict,from="<Container-IP>",command="/bin/bash /boot/config/twinraid/guard.sh power" ssh-ed25519 … twinraid-power`
  - `restrict` schaltet Terminal, Port-, Agent- und X11-Weiterleitung sowie `~/.ssh/rc` ab, auch Funktionen, die künftige OpenSSH-Versionen hinzufügen. Es ersetzt die vier einzelnen `no-…`-Optionen (Entscheidung E12).
  - Unraids eigene Prüfung beim Speichern (*Users → root → SSH authorized keys*, `checkKey()` in `UserEdit.page` von Unraid 7.3) akzeptiert diese Zeile; gespeichert wird sie unverändert. Geprüft in Phase 4.
  - `guard.sh power` erlaubt genau drei Anfragen in `SSH_ORIGINAL_COMMAND`, die nur mit festen Mustern verglichen und nie als Befehl ausgewertet werden: `check` (Verbindungstest, gibt `twinraid-guard <Version> power ok` aus), `poweroff` (`/sbin/poweroff`) und `vm-shutdown <UUID>` (`/usr/bin/virsh shutdown --domain <UUID>`, die UUID wird streng geprüft). Alles andere endet mit Exit-Code 2 und einem Eintrag im Syslog von Groß.
- **Dauerhaft gespeichert** wird die Zeile in `/boot/config/ssh/root/authorized_keys`; seit Unraid 6.9 ist `/root/.ssh` ein Link dorthin. Bearbeitet wird sie in der WebGUI unter *Users → root → SSH authorized keys*.
- **SSH-Client im Worker:** feste Argumentliste mit `-F /dev/null`, `BatchMode=yes`, `IdentitiesOnly=yes`, `StrictHostKeyChecking=yes`, eigener `UserKnownHostsFile` in `/data/secrets`, `GlobalKnownHostsFile=/dev/null`, `HostKeyAlgorithms=ssh-ed25519`, `ConnectTimeout=5`, ohne Passwort-Anmeldung. Fehler werden anhand von Exit-Code und festen Meldungen von OpenSSH einer Ursache zugeordnet; die Ausgabe selbst wird nicht geloggt.
- Die Wrapper-Skripte liegen im Repo unter `host-scripts/` und werden auf Groß nach `/boot/config/twinraid/` kopiert. `/boot` ist FAT32, Dateien dort sind nicht ausführbar; deshalb der Aufruf über `/bin/bash`.
- **Test ohne echte Server:** Die CI startet einen Wegwerf-Container als nachgebautes Groß (Image per Digest gepinnt, `openssh-server` in derselben gepinnten Version wie der Client, nur im Test) mit `guard.sh`, einer nachgebauten Unraid-API und Platzhaltern für `poweroff` und `virsh`. Geprüft werden `from=`, `restrict`, die erzwungene Befehlszeile, die strikte Hostkey-Prüfung und die Abläufe Herunterfahren, VM herunterfahren und Erzwingen (`docker/ssh-test.sh`).
- Die Unraid-API wird per HTTPS mit bestätigtem Fingerabdruck angesprochen, falls in Unraid aktiviert. Sonst läuft sie per HTTP nur innerhalb des Server-Netzes (bekanntes Restrisiko, deutlich angezeigt, siehe 3.5).

### 5.7 Regeln für den Code

Siehe `CLAUDE.md`, Abschnitt „Unverhandelbare Sicherheitsregeln“.

### 5.8 Öffentliches Repository

Das Repo ist öffentlich auf GitHub, das Container-Image wird öffentlich über die GitHub Container Registry (`ghcr.io`) verteilt. Deshalb gilt:

- Im Repo stehen nur Beispielwerte (`config.example.yaml`), nie echte IPs, MAC-Adressen, Hostnamen oder Secrets.
- `.gitignore` schließt `twinraid.yaml`, `*.key`, `*.pem`, `.env` und `data/` aus.
- In GitHub aktiviert: Secret Scanning mit Push Protection, Dependabot-Warnungen und -Updates.
- Commits über die anonymisierte GitHub-E-Mail-Adresse (`…@users.noreply.github.com`).
- Ab Phase 1: Branch-Schutz für `main`, Änderungen nur mit grüner CI.
- Images werden ausschließlich von GitHub Actions gebaut und veröffentlicht, nicht vom lokalen PC.

**Image-Veröffentlichung (ab Phase 3)**

- **Versionen** nach SemVer `MAJOR.MINOR.PATCH`. Bis 1.0 entspricht MINOR der Phase (0.3.x ist Phase 3). Die Version steht in `backend/pyproject.toml`, `backend/src/twinraid/__init__.py` und `frontend/package.json`; ein Test prüft, dass alle drei gleich sind.
- **Tags:**

| Auslöser | Image-Tags |
|---|---|
| Git-Tag `v0.3.0` | `0.3.0`, `0.3`, `latest` (ab 1.0 zusätzlich `1`) |
| Git-Tag mit Vorabversion, z. B. `v0.3.0-rc.1` | nur `0.3.0-rc.1` |
| Push auf `main` | `edge`, `sha-<7 Zeichen>` |

- **Unraid:** Das Template nutzt `ghcr.io/mantua1/twinraid:latest`. Unraid vergleicht den Digest hinter diesem Tag mit dem installierten Image und meldet „Update verfügbar“ also nur bei echten Releases. `:edge` dient zum Testen des aktuellen Stands von `main`, `:0.3` oder `:0.3.0` zum Festhalten einer Version.
- **Nur aus `main` oder über Versions-Tags:** Pull Requests bauen und testen nur, ohne Anmeldung an der Registry und ohne Schreibrechte. Ein Versions-Tag wird nur veröffentlicht, wenn sein Commit in `main` liegt und die Version im Tag mit der Version im Code übereinstimmt.
- **Dasselbe Image:** Veröffentlicht wird genau das Image, das im selben Workflow-Lauf den Container-Starttest bestanden hat. Der Veröffentlichungs-Job ist der einzige mit Schreibrechten (`packages`, `id-token`, `attestations`).
- **Nur `linux/amd64`**, weil Unraid darauf läuft. Das Image ist ein einfaches Manifest ohne zusätzliche BuildKit-Attestierungen.
- **Herkunftsnachweis:** `actions/attest` erzeugt eine SLSA-Build-Provenance, signiert über Sigstore, und legt sie an der Registry ab. Prüfbar mit `gh attestation verify oci://ghcr.io/mantua1/twinraid:<version> --owner mantua1`.
- **Öffentlich abrufbar:** Nach dem Push prüft der Workflow ohne Anmeldung, dass das Image abrufbar ist und den erwarteten Digest hat. Neue Pakete sind auf ghcr.io zunächst privat; beim ersten Release schlägt diese Prüfung deshalb fehl, bis das Paket in GitHub auf öffentlich gestellt ist (Anleitung im README).

---

### 5.9 Datenablage unter `/data` (ab Phase 2)

- `/data` muss root gehören und darf weder für die Gruppe noch für alle beschreibbar sein. Sonst verweigert der Container den Start und nennt den Befehl zur Reparatur auf dem Host (`chown root:root …` und `chmod 755 …`). Ein von Docker angelegtes Verzeichnis erfüllt das.
- `twinraid-init` legt die Unterordner an, prüft bei jedem Start Besitzer, Gruppe und Rechte und korrigiert sie, soweit das ohne zusätzliche Capabilities geht (Rechte und Gruppe als Besitzer). Symlinks und fremde Besitzer führen zum Abbruch mit Meldung.

| Pfad | Besitzer | Rechte | Inhalt |
|---|---|---|---|
| `/data/secrets` | root | 0700 | Passwort-Hash; ab Phase 3 API-Key von Groß (`target-api-key`) und bestätigter Fingerabdruck seines Zertifikats (`target-api-cert.sha256`); ab Phase 4 SSH-Key „power“ (`ssh-power`, `ssh-power.pub`) und bestätigter Hostkey von Groß (`target-ssh-known-hosts`), alle Dateien `0600` |
| `/data/tls` | root | 0700 | CA (`ca.pem`, `ca-key.pem`), Serverzertifikat (`server.pem`, `server-key.pem`), Vermerk des letzten CA-Wechsels (`ca-change.json`) |
| `/data/web` | root, Gruppe 10001 | 0770 | SQLite des Web-Prozesses (Datei `0600`, Besitzer 10001); Vermerk der letzten Erneuerungsanfrage `tls-renewal.json` |
| `/data/worker` (ab Phase 4) | root | 0700 | Aktionslog des Workers (`actions.jsonl`, `actions.jsonl.1`, Dateien `0600`); der Web-Prozess kann es weder lesen noch verändern |
| `/run/twinraid-tls` | root, Gruppe 10001 | 0750 | Zertifikatskette, Serverschlüssel, CA-Zertifikat und `info.json` für den Web-Prozess, nur im RAM |
| `/run/twinraid` | root, Gruppe 10001 | 0750 | Worker-Socket, Herzschlag; im Simulationsmodus die Schalter `mock-job-running`, `mock-tour` und `mock-fault-<Name>` (nur root kann sie anlegen) |

## 6. Konfiguration

### 6.1 Volumes (Unraid-Template)

| Host-Pfad (Klein) | Container-Pfad | Modus | Inhalt |
|---|---|---|---|
| `/mnt/user/appdata/twinraid/config` | `/config` | ro | `twinraid.yaml`, optional `tls/` |
| `/mnt/user/appdata/twinraid/data` | `/data` | rw | SQLite, Secrets, generiertes Zertifikat |
| `/mnt/user` | `/srv/worker/source` | ro | Quell-Shares (erst ab Phase 5) |
| `/boot/config/plugins/dockerMan/templates-user` | `/srv/worker/templates` | ro | Container-Templates (erst ab Phase 5) |

Shares und Templates werden erst gemountet, wenn der Sync sie braucht. Vorher hat TwinRaid keinen Zugriff darauf.

### 6.2 Beispiel `twinraid.yaml`

```yaml
# TwinRaid-Konfiguration – wird read-only nach /config/twinraid.yaml gemountet.
# Secrets (Passwort, API-Keys, SSH-Keys) gehören NICHT hierher (siehe 5.5).

web:
  # port: 8443                 # optional, Standard 8443; nicht ändern
  allowed_networks:            # nur diese Clients dürfen zugreifen (siehe 5.1)
    - 192.168.10.50/32         # Server-Netz: nur einzelne Geräte, z. B. der Admin-PC
    - 192.168.20.0/24          # Client-Netz: ganz, die Router-Firewall regelt den Zugriff
  session_idle_minutes: 10     # optional, Standard 10
  default_language: de         # de | en
  tls:
    # ip_addresses:            # optional; ohne Angabe die IP aus dem Unraid-Template
    #   - 192.168.10.20        #   („Fixed IP address“), automatisch erkannt
    hostnames: []              # optional, z. B. twinraid.home.arpa

servers:
  source:                      # Unraid Klein – hier läuft der Container
    name: "Unraid Klein"
    host: 192.168.10.10
    webgui:                    # wie „SSL/TLS verwenden“ in Unraid: Nein → http, Ja → https
      scheme: http
      port: 80                 # optional, Standard 80 bei http, 443 bei https
  target:                      # Unraid Groß
    name: "Unraid Groß"
    host: 192.168.10.11
    webgui:
      scheme: http
      port: 80
    mac: "AA:BB:CC:DD:EE:FF"
    # wol_broadcast: 192.168.10.255  # optional; ohne Angabe aus dem eigenen Netz abgeleitet
    boot_timeout_minutes: 5
    # ssh_port: 22               # optional, Standard 22 (Herunterfahren per SSH)

gaming_vm:
  display_name: "Gaming-VM"
  unraid_vm_name: "Windows 11 Gaming"
  shutdown_timeout_minutes: 3  # danach wird "Erzwingen" angeboten

actions:                       # optional; nur im Modus real, Standard: alles aus (siehe 4.2)
  wake: false                  # Groß wecken
  vm: false                    # VM starten, herunterfahren, erzwingen
  shutdown: false              # Groß herunterfahren

sync:
  shares:                      # identische Namen auf beiden Servern
    - appdata
    - dokumente
    - medien
  appdata_share: appdata
  exclude:                     # wird nicht kopiert (relativ zu /mnt/user)
    - appdata/twinraid
  protect_on_target:           # wird auf Groß nie gelöscht
    - appdata/_twinraid
  max_deletions: 1000
  stop_containers: true
  never_stop:                  # werden für den Konsistenzlauf nie gestoppt
    - twinraid
  copy_templates: true
  schedule:
    enabled: true
    timezone: "Europe/Berlin"  # IANA-Zeitzone
    days: [sun]                # mon, tue, wed, thu, fri, sat, sun
    time: "03:00"              # HH:MM, immer in Anführungszeichen
    wake_target: true
    shutdown_after: true       # nur wenn der Sync geweckt hat und die VM aus ist
```

### 6.3 Validierung

Die Konfiguration wird beim Start mit Pydantic vollständig validiert: Pflichtfelder, IP- und MAC-Formate, Zeitplan, verbotene Shares (`system`, `domains`), Überschneidungen von `exclude` und `shares`. Ist die Konfiguration ungültig, startet der Container **nicht** und meldet im Log genau, was falsch ist. Die Weboberfläche zeigt die aktive Konfiguration nur lesend an.

Ergänzende Regeln:

- **Laden:** YAML wird ausschließlich mit `yaml.safe_load` gelesen. Unbekannte Schlüssel sind ein Fehler, damit Tippfehler auffallen.
- **Share-Namen** (`shares`, `appdata_share`): Prüfung über eine Positivliste erlaubter Zeichen. Erlaubt sind die Buchstaben `A–Z` und `a–z`, Ziffern, Punkt, Unterstrich und Bindestrich. Das erste Zeichen muss ein Buchstabe oder eine Ziffer sein, höchstens 64 Zeichen. Damit sind Schrägstrich, `..`, führendes Minus, Leerzeichen und Steuerzeichen ausgeschlossen. `system` und `domains` sind unabhängig von Groß- und Kleinschreibung verboten. Doppelte Einträge sind ein Fehler. `appdata_share` muss in `shares` enthalten sein.
- **Pfade** (`exclude`, `protect_on_target`): relativ zu `/mnt/user`, also ohne führenden Schrägstrich. Nicht erlaubt sind `..`, `.` und leere Bestandteile, Bestandteile mit führendem Minus sowie Steuer- und unsichtbare Formatzeichen. Zusätzlich verboten sind die Platzhalterzeichen `*`, `?`, `[`, `]` und der Backslash, weil rsync sie in Filterregeln als Muster auswertet. Der erste Bestandteil muss eine konfigurierte Share sein. Eine ganze Share darf nicht ausgeschlossen werden; dafür wird sie aus `shares` entfernt.
- **Container-Namen** in `never_stop`: nur die von Docker erlaubten Zeichen.
- **WebGUI** (`servers.<server>.webgui`, ab Phase 3): `scheme` ist `http` oder `https`; `port` ist optional (1 bis 65535, Standard 80 bzw. 443).
- **Groß** (ab Phase 4): `wol_broadcast` ist optional (IPv4-Adresse, siehe 4.2); `ssh_port` ist optional (1 bis 65535, Standard 22).
- **Aktionen** (`actions`, ab Phase 4): optional; `wake`, `vm` und `shutdown` sind jeweils `true` oder `false`, Standard `false`.
- **Erlaubte Netze** (`web.allowed_networks`): mindestens ein Eintrag. Erlaubt sind Netze in CIDR-Schreibweise und einzelne Adressen (`/32` bzw. `/128` oder ohne Maske), gemischt. Gesetzte Host-Bits wie `192.168.10.50/24` sind ein Fehler, damit ein vermeintlich einzelnes Gerät nicht still das ganze Netz freigibt.
- **Sitzung:** `web.session_idle_minutes` ist optional (1 bis 720, Standard 10).
- **Port** (`web.port`): optional, Standard 8443.
- **TLS** (`web.tls`): `ip_addresses` ist optional (ohne Angabe gilt die erkannte Adresse, siehe 5.1). Ist es gesetzt, enthält es mindestens eine IP-Adresse, keine Multicast- oder unspezifizierte Adresse, ohne Duplikate. `hostnames` ist optional; erlaubt sind DNS-Namen nach RFC 1123 ohne Platzhalter, in Kleinbuchstaben gespeichert, ohne Duplikate. Die verwendeten Adressen und die Hostnamen bilden zusammen die erlaubten `Host`-Header und gehören zu den Werten, die nie öffentlich ausgeliefert werden.
- **Zeitplan** (`sync.schedule`): `days` enthält nur die Kürzel `mon`, `tue`, `wed`, `thu`, `fri`, `sat`, `sun`, ohne Duplikate und mindestens einen Tag. `time` hat das Format `HH:MM` (`00:00` bis `23:59`) und steht in Anführungszeichen. Ohne Anführungszeichen liest YAML manche Uhrzeiten wie `13:30` als Zahl; das wird mit einem klaren Hinweis abgelehnt. `timezone` ist eine gültige IANA-Zeitzone, die im Container vorhanden ist (z. B. `Europe/Berlin`); Sondereinträge wie `localtime` sind nicht erlaubt.

### 6.4 Betriebsmodus (`TWINRAID_MODE`)

- Pflichtangabe als Umgebungsvariable des Containers: `mock` oder `real`. Es gibt **keinen Standardwert**.
- Fehlt der Wert oder ist er ungültig, startet der Container nicht und meldet im Log klar, was fehlt und welche Werte erlaubt sind.
- Entwicklungs-Skripte, Tests und CI setzen `mock` ausdrücklich.
- **Mock-Modus:** Alle Integrationen mit Außenwirkung sind durch Simulationen ersetzt. Das Log warnt beim Start deutlich. Die Oberfläche zeigt dauerhaft ein auffälliges Banner „Simulationsmodus“. `job_running` wird mit `twinraid-admin mock-job on|off` geschaltet.
  - **Interaktive Simulation (ab Phase 4, Entscheidung E11):** Groß und die VM starten ausgeschaltet, Klein ist online („TwinRaid läuft auf diesem Server“). Aktionen wirken mit festen Wartezeiten: Groß bootet 20 s, danach braucht die API weitere 10 s; die VM startet in 3 s und fährt in 8 s herunter; Groß fährt in 15 s herunter.
  - **Fester Rundlauf:** Die bisherige Statussimulation durchläuft einen festen Ablauf, der jeden Zustand und jede Ursache mindestens einmal zeigt, ab v0.3.2 auch „TwinRaid läuft auf diesem Server“ (ein Test prüft die Vollständigkeit). Ab Phase 4 wird sie mit `twinraid-admin mock-tour on|off` eingeschaltet; währenddessen sind Aktionen mit dem Grund `tour` gesperrt.
  - **Fehler simulieren:** `twinraid-admin mock-fault <Name> on|off` mit `vm-hang` (VM reagiert nicht auf Herunterfahren), `api-forbidden` (API-Key ohne `VMS:UPDATE_ANY`), `ssh-denied` (`guard.sh` lehnt ab), `no-boot` (Groß reagiert nicht auf Wake-on-LAN) und ab v0.4.2 `other-vm` (auf Groß läuft eine weitere VM).
- **Modus `real` (ab Phase 3 nur lesend, ab Phase 4 mit freigeschalteten Aktionen):** Status-Abfragen sind immer erlaubt, also der TCP-Check von Groß und der VM-Status über die Unraid-API. Die Aktionen `wake_target`, `shutdown_target`, `shutdown_target_force_vm`, `vm_start`, `vm_stop` und `vm_force_stop` sind nur verfügbar, wenn sie in `actions` freigeschaltet sind (siehe 4.2); sonst lehnt der Worker sie vor jeder Verarbeitung mit dem Fehlercode `not_available` ab. Die Sync-Befehle (`sync_start`, `sync_cancel`, `sync_confirm_deletions`) bleiben bis Phase 5 mit `not_available` gesperrt. Ist keine Aktion freigeschaltet, zeigt die Oberfläche das Banner „Nur Statusanzeige“.

---

## 7. Projektstruktur

```
twinraid/
├── CLAUDE.md                  # Regeln für Claude Code (lädt dieses Konzept)
├── README.md
├── docs/
│   ├── KONZEPT.md             # dieses Dokument
│   ├── EINRICHTUNG.md         # Installation auf Unraid und CA auf Windows/Linux/Android (ab Phase 3)
│   └── WIEDERHERSTELLUNG.md   # Notfall-Anleitung (Phase 6)
├── backend/
│   ├── pyproject.toml
│   ├── src/twinraid/
│   │   ├── config/            # YAML laden und validieren (Pydantic)
│   │   ├── web/               # FastAPI: Routen, Auth, Sessions, CSRF, Netzfilter, SSE
│   │   ├── worker/            # Befehlsannahme, Sperrlogik, Zeitsteuerung
│   │   ├── integrations/      # Unraid-API, SSH, WoL, Netzwerk-Checks
│   │   │   └── mocks/         # Simulationen für Entwicklung und Tests
│   │   ├── sync/              # rsync-Steuerung, Fortschritt, Restzeit, Schutzmechanismen
│   │   ├── shared/            # gemeinsame Modelle (Befehle, Status, Ereignisse)
│   │   └── admin_cli.py       # twinraid-admin
│   └── tests/
├── frontend/
│   ├── package.json
│   └── src/
│       ├── api/               # typisierter API-Client
│       ├── components/
│       ├── pages/
│       └── i18n/              # de.json, en.json
├── docker/
│   ├── Dockerfile             # Multi-Stage: Frontend bauen → Backend-Image
│   ├── rootfs/                # Init-/Prozess-Konfiguration
│   ├── fake-target/           # nachgebautes Unraid Groß, nur für den SSH-Test (ab Phase 4)
│   ├── ssh-test.sh            # SSH-Test gegen fake-target (ab Phase 4)
│   └── unraid-template.xml    # Unraid-Template (ab Phase 3)
├── host-scripts/
│   └── guard.sh               # SSH-Wrapper für Unraid Groß
├── config.example.yaml
└── .github/
    ├── workflows/             # CI: Tests, Lint, Image-Build, Veröffentlichung auf ghcr.io
    └── dependabot.yml
```

---

## 8. Phasenplan

Jede Phase endet mit lauffähigem, getestetem Stand und einem Commit auf GitHub. Entwickelt und getestet wird immer gegen Mocks. Ab Phase 3 darf der Modus `real` den Status lesen; echte Server werden erst angesprochen, wenn der Nutzer es ausdrücklich freigibt.

| Phase | Inhalt | Fertig, wenn … |
|---|---|---|
| 0 | GitHub-Repo, VS Code, Claude Code, `CLAUDE.md`, Konzept | Repo existiert mit Doku, erster Commit ist gepusht |
| 1 | Grundgerüst: Backend- und Frontend-Skelett, Dockerfile mit beiden Prozessen, Config laden und validieren, Mock-Modus, DE/EN, Dark/Light, CI | Container startet lokal im Mock-Modus, Dashboard (leer) sichtbar, CI grün |
| 2 | Sicherheit: HTTPS, Admin-CLI, Login, Sessions, CSRF, Netzfilter, Rate-Limit, Security-Header, Audit-Log | Ohne Login ist nichts erreichbar; jede Schutzmaßnahme hat einen Test |
| 3 | Status: Server-Checks, VM-Status, Live-Updates per SSE; eigene CA mit automatischer Erneuerung des Serverzertifikats; Image-Veröffentlichung auf ghcr.io mit Herkunftsnachweis (aus Phase 7 vorgezogen); Modus `real` nur lesend mit `set-api-key target` und `trust-api-cert target`; erste Fassung von `docs/EINRICHTUNG.md` und Unraid-Template | Dashboard zeigt Status im Mock-Modus korrekt und live; Release v0.3.0 ist ohne Login von ghcr.io abrufbar und sein Herkunftsnachweis prüfbar |
| 4 | Aktionen: WoL, VM Start/Stop/Force, Shutdown Groß, Sperrlogik, Auto-Wecken, eigenes Aktionslog des Workers (siehe 5.3); erste Aktionen gegen echte Server | Alle Buttons funktionieren auf den echten Servern; Rechte der API-Keys sind geprüft; jede Aktion steht im Aktionslog des Workers |
| 5 | Sync: Dry-Run, rsync, Fortschritt/Restzeit, Container stoppen/starten, Templates, Schutzmechanismen, Historie | Erst Sync einer Test-Share, danach vollständiger Sync erfolgreich |
| 6 | Zeitsteuerung, Auto-Wecken/-Herunterfahren, Benachrichtigungen, Notfall-Info auf Groß | Ein geplanter Lauf läuft unbeaufsichtigt durch |
| 7 | Unraid-Template und Einrichtungsanleitung fertigstellen, unabhängiges Security-Review | Installation auf Unraid nach Anleitung möglich; Review-Befunde behoben |

---

## 9. Einrichtung auf Unraid (Checkliste)

Die Schritt-für-Schritt-Anleitung steht in `docs/EINRICHTUNG.md` (erste Fassung ab Phase 3, fertig in Phase 7).

- [ ] Groß: Wake-on-LAN im BIOS aktivieren („Wake on LAN“ bzw. „Power On by PCI-E“ an, „ErP/EuP“ aus) und `ethtool -s eth0 wol g` in `/boot/config/go` eintragen
- [ ] Groß: Array startet automatisch (*Settings → Disk Settings → Enable auto start*), sonst sind VMs nach dem Wecken nicht verfügbar
- [ ] Gaming-VM (Windows): Energieoptionen → „Beim Drücken des Netzschalters: Herunterfahren“, sonst reagiert sie nicht auf das ACPI-Signal
- [ ] Groß: SSH aktivieren; `guard.sh` nach `/boot/config/twinraid/` kopieren; `authorized_keys`-Zeilen aus `twinraid-admin init-ssh-keys` unter *Users → root → SSH authorized keys* eintragen; Hostkey mit `twinraid-admin trust-host-key target` bestätigen
- [ ] Groß: API-Key ohne Rolle mit `VMS:READ_ANY` anlegen und mit `twinraid-admin set-api-key target` hinterlegen (ab Phase 4 zusätzlich `VMS:UPDATE_ANY`, vorher Rechtetest mit dem Lese-Key; ab Phase 5 Key für Klein, siehe 5.6)
- [ ] Aktionen einzeln in `actions` freischalten und nach dem Testprotokoll in `docs/EINRICHTUNG.md` prüfen
- [ ] Klein: „Host access to custom networks“ in den Docker-Einstellungen **nicht** aktivieren (siehe 5.1)
- [ ] Klein: Container-Templates auf Pool-Pfade prüfen und auf `/mnt/user/…` umstellen
- [ ] UniFi: feste IP für den Container, Firewall-Regel Client-Netz → Container-IP:8443
- [ ] Klein: `/mnt/user/appdata/twinraid/data` gehört root und ist nur für root beschreibbar (siehe 5.9)
- [ ] Feste IP nur im Template („Fixed IP address“); das Startlog zeigt `TLS address … (detected on …)` mit dieser IP
- [ ] Feste, lokal administrierte MAC im Template („Fixed MAC address“); in UniFi die IP per MAC reservieren; MAC vor und nach einem Update vergleichen
- [ ] `web.allowed_networks`: im Server-Netz nur einzelne Geräte als `/32`, andere Netze ganz, wenn die Router-Firewall sie regelt
- [ ] Container installieren, Konfiguration anlegen, Passwort setzen, bei `https` Zertifikat von Groß bestätigen (`trust-api-cert`), ab Phase 4 Hostkey von Groß bestätigen
- [ ] Geräte: CA-Zertifikat auf Windows, Linux und Android für Chrome bzw. Edge und Firefox installieren (Test in Issue #3)

---

## 10. Offene Entscheidungen

| Nr. | Frage | Stand |
|---|---|---|
| E1 | Laufen auf Groß eigene Container mit Daten in `appdata` oder mit eigenen Templates? | **entschieden:** nein, Container auf Groß sind aus; geschützt wird nur `appdata/_twinraid` |
| E2 | GitHub-Repo öffentlich oder privat? | **entschieden:** öffentlich, Image über `ghcr.io` (siehe 5.8) |
| E3 | HTTPS-Variante | **entschieden:** eigene kleine CA mit Namensbeschränkung und separatem Serverzertifikat (ab Phase 3, vorher selbstsigniert), eigenes Zertifikat optional mountbar (siehe 5.2) |
| E4 | Projektname (Arbeitstitel „TwinRaid“) | offen |
| E5 | Prozessverwaltung im Container (s6-overlay oder supervisord) | **entschieden:** s6-overlay mit read-only Root und minimalen Capabilities (siehe 5.4) |
| E6 | Versionen und Image-Tags | **entschieden:** SemVer, `latest` nur für Releases, `edge` für `main` (siehe 5.8) |
| E7 | Wie steuert TwinRaid in Phase 5 die Container auf Klein, ohne dass „Host access to custom networks“ eingeschaltet wird? | **offen**, Optionen und Empfehlung (Option I) in 10.1, überarbeitet in v0.4.2; Entscheidung vor Beginn von Phase 5 |
| E8 | Wie wird die Gaming-VM heruntergefahren? | **entschieden:** per SSH mit dem Key „power“ (`virsh shutdown`, nur ACPI); Starten und Erzwingen über die API, weil `vm.stop` der API nach 10 s selbst erzwingt (siehe 3.4) |
| E9 | Wie werden Aktionen im Echtbetrieb schrittweise freigeschaltet? | **entschieden:** Abschnitt `actions` in `twinraid.yaml` mit `wake`, `vm`, `shutdown`, Standard aus (siehe 4.2) |
| E10 | Groß herunterfahren bei laufender VM | **entschieden:** Dialog „VM zuerst herunterfahren?“, dann VM und danach Server; weiterhin kein sofortiges Herunterfahren an der laufenden VM vorbei. **Ergänzt in v0.4.1:** Nur nach einem gescheiterten sauberen Versuch und ausdrücklicher Bestätigung bietet TwinRaid zeitlich begrenzt „Erzwingen und danach Server herunterfahren“ an (siehe 4.2). **Ergänzt in v0.4.2:** Laufen außer der Gaming-VM weitere VMs auf Groß, schaltet TwinRaid den Server gar nicht aus, weder manuell noch erzwungen noch automatisch nach einem Sync (siehe 4.2) |
| E11 | Simulationsmodus mit Aktionen | **entschieden:** interaktive Simulation als Standard, fester Rundlauf mit `mock-tour`, Fehler mit `mock-fault` (siehe 6.4) |
| E12 | Kleinere Punkte aus Phase 4 | **entschieden:** `wol_broadcast` optional (Startabbruch nur bei freigeschaltetem Wecken), `ssh_port` optional, 60 s für API-Aktionen, `restrict` in `authorized_keys` |

### 10.1 E7: Container auf Klein stoppen und starten

**Ausgangslage:** TwinRaid hat eine eigene IP auf `br0` (macvlan mit Bridging). Ohne Host access erreichen sich Klein und der Container nicht (siehe 5.1). Der Konsistenzlauf beim Sync muss aber Container auf Klein stoppen und wieder starten (siehe 3.4 und 4.3). Überarbeitet am 16.10.2026 anhand des Quellcodes von Unraid-API v4.37.5 und Unraid-WebGUI 7.3; nichts davon ist umgesetzt oder auf Unraid getestet.

**Anforderungen des Nutzers (16.10.2026):**

1. TwinRaid darf Container auf Klein **nur stoppen und starten**: keine Parameter ändern, keine Image-Updates, nichts löschen, nichts anlegen.
2. TwinRaid erkennt seinen eigenen Container zuverlässig und stoppt ihn nie, fest im Code und unabhängig von der Konfiguration.
3. „Host access to custom networks“ bleibt aus. Änderungen, die alle anderen Container auf Klein betreffen (Bridging aus, ipvlan), möglichst vermeiden.

**Befund: Was `DOCKER:UPDATE_ANY` erlaubt (Unraid-API v4.37.5)**

Die Rechte der API bestehen nur aus Ressource und Aktion (Casbin-Regel `obj` und `act` müssen exakt passen); es gibt keine Einschränkung auf einzelne Befehle oder Container. `UPDATE_OWN` ist ein eigener Wert und gewährt kein `UPDATE_ANY`. Eine engere Berechtigung als `DOCKER:UPDATE_ANY` gibt es für Start und Stopp nicht.

| Befehl der API (`docker { … }` bzw. `Mutation`) | Recht | Bemerkung |
|---|---|---|
| `start`, `stop` | `DOCKER:UPDATE_ANY` | gebraucht |
| `restart`, `pause`, `unpause` | `DOCKER:UPDATE_ANY` | nicht gebraucht |
| `updateContainer`, `updateContainers`, `updateAllContainers` | `DOCKER:UPDATE_ANY` | **Image-Update**: neues Image ziehen und Container neu anlegen |
| `updateAutostartConfiguration`, Ordner und Ansicht (`createDockerFolder` …), `syncDockerTemplatePaths`, `resetDockerTemplateMappings` | `DOCKER:UPDATE_ANY` | nur mit Feature-Flag `ENABLE_NEXT_DOCKER_RELEASE`; ob Unraid 7.3.3 es setzt, ist nicht geprüft |
| `removeContainer` (auch mit Image) | `DOCKER:DELETE_ANY` | nicht enthalten |
| Container anlegen, Parameter ändern, Befehle im Container ausführen | – | gibt es in der API nicht |
| Containerliste mit Labels, Mounts, Netzwerk, Ports, Befehlszeile; **Container-Logs** (`logs`); Statistiken | `DOCKER:READ_ANY` | zum Finden der Container nötig; die Logs können Geheimnisse enthalten |

**Restrisiko eines gestohlenen Keys** mit `DOCKER:READ_ANY` und `DOCKER:UPDATE_ANY`: Container stoppen, starten, neu starten oder pausieren (Ausfall von Diensten), alle Container auf das neueste Image aktualisieren (ungewollte Versionswechsel, bei einem kompromittierten Image auch Schadcode), Container-Logs und Konfigurationsdetails lesen. Nicht möglich: Container löschen, anlegen, Parameter ändern, Befehle im Container ausführen. Begrenzen lässt es sich so:

- Der Key liegt nur beim Worker in `/data/secrets` (wie der Key für Groß) und wird nie über das Netz im Klartext übertragen (HTTPS mit bestätigtem Fingerabdruck oder Unix-Socket).
- Eigener Key nur mit `DOCKER:READ_ANY` und `DOCKER:UPDATE_ANY`, ohne Rolle, nur für TwinRaid.
- **Besser:** Der Key bleibt gar nicht in TwinRaid, sondern bei einem Wächter auf Klein, der nur Start und Stopp ausführt (Option I). Dann erfüllt TwinRaid Anforderung 1 auch dann, wenn der Container übernommen wird.

**Eigenen Container erkennen (für alle Optionen)**

- Die vollständige Container-ID steht in `/proc/self/mountinfo` in den Pfaden `…/containers/<64 Hex-Zeichen>/hostname`, `hosts` und `resolv.conf`; geprüft mit Docker 29 (bei cgroup v2 steht in `/proc/self/cgroup` nur `0::/`, der Hostname ist nur die Kurzform und kann im Template überschrieben werden).
- Diese ID muss genau einmal in der Containerliste der API vorkommen. Fehlt sie oder ist sie mehrdeutig, stoppt TwinRaid **keinen** Container (Phase B entfällt mit Warnung).
- Die eigene ID wird fest im Code ausgenommen, unabhängig von `never_stop`. Option I prüft zusätzlich auf Klein selbst (Image `ghcr.io/mantua1/twinraid`).

**Optionen im Vergleich**

| | Netzwerk ändert sich | andere Container betroffen | Aufwand | Rechte in TwinRaid |
|---|---|---|---|---|
| A. Bridging aus, Netzwerk `eth0`, Host access an | ja, grundlegend | **ja, alle mit eigener IP und alle VMs** | mittel (Umbau mit Unterbrechung) | `DOCKER:UPDATE_ANY` |
| B. ipvlan, Host access an | ja | **ja, alle mit eigener IP** (eine MAC für alle) | klein | `DOCKER:UPDATE_ANY` |
| C2. TwinRaid in eigenem VLAN | ja (VLAN, Switchport, Firewall) | nein | groß | `DOCKER:UPDATE_ANY` |
| D. Zweite Netzwerkkarte nur für TwinRaid | ja (zweite Karte im Server-Netz) | nein | mittel (Hardware) | `DOCKER:UPDATE_ANY` |
| E. Bridge-Netz mit Portfreigabe | nein, aber TwinRaid verliert eigene IP | nein | klein | `DOCKER:UPDATE_ANY` |
| F. Zweites Netzwerk (`br0` + Docker-Bridge) | klein (Interface Extra) | nein | mittel | `DOCKER:UPDATE_ANY` |
| G. Unix-Socket der API einbinden | nein | nein | klein | `DOCKER:UPDATE_ANY` |
| **I. Wächter auf Klein mit Auftragsdateien** | **nein** | **nein** | mittel (Skript auf Klein) | **nur „Start“ und „Stopp“ anfordern** |
| H. Kein Container-Stopp | nein | nein | keiner | keine |

**A. Bridging aus, Docker-Netzwerk `eth0`, Host access an** (Empfehlung von Unraid 6.12.4)
- *Einstellungen:* Unraid: *Settings → Network Settings* → Bridging für `eth0` aus; *Settings → Docker* → Netzwerk `eth0`, Host access an; alle Container mit eigener IP auf `eth0` umstellen, VMs auf `vhost0`. UniFi: nichts.
- *Sicherheit:* TwinRaid erreicht Klein direkt; Key mit `DOCKER:UPDATE_ANY` in TwinRaid.
- *Bewertung:* betrifft alle Container und VMs auf Klein → widerspricht Anforderung 3.

**B. ipvlan statt macvlan, Host access an**
- *Einstellungen:* *Settings → Docker* → Netzwerktyp `ipvlan`, Host access an. UniFi: Reservierungen per MAC funktionieren nicht mehr pro Container.
- *Bewertung:* alle Container teilen die MAC von Klein, die feste MAC von TwinRaid (5.1) entfällt → widerspricht Anforderung 3.

**C1. Adresse von Klein in einem anderen VLAN** – verworfen (asymmetrisches Routing, nur mit Policy-Routing per Startskript).

**C2. TwinRaid in ein eigenes VLAN** (`br0.<VLAN>`, Klein ohne Adresse darin)
- *Einstellungen:* UniFi: neues VLAN, Switchport von Klein mit Tag, Firewall-Regeln (TwinRaid → Klein:443/80, → Groß:22/80/443, Client-Netz → TwinRaid:8443). Unraid: VLAN-Schnittstelle in *Network Settings*, Docker-Netzwerk `br0.<VLAN>`, neue IP und MAC im Template; `web.allowed_networks` anpassen.
- *Sicherheit:* Die Router-Firewall begrenzt TwinRaid genau; Key mit `DOCKER:UPDATE_ANY` in TwinRaid.
- *Nachteile:* Der Sync (bis 1 TB) läuft durch den Router; Wake-on-LAN muss ins Server-Netz, gerichtete Broadcasts sind meist gesperrt; die eigene IP im Server-Netz (5.1) entfällt.

**D. Zweite Netzwerkkarte nur für TwinRaid**
- *Einstellungen:* zweite Karte (2,5 GbE) mit Kabel zum Switch. Unraid: `eth1` ohne IP und ohne Bridging, eigenes Docker-Netzwerk auf `eth1`, im Template statt `br0` wählen. UniFi: Switchport im Server-Netz, Reservierung bleibt per MAC.
- *Sicherheit:* wie heute, Key mit `DOCKER:UPDATE_ANY` in TwinRaid.
- *Bewertung:* sauber und ohne Code, aber Hardware nötig; erfüllt Anforderung 1 nicht.

**E. Bridge-Netz mit Portfreigabe statt eigener IP**
- *Einstellungen:* Template auf `bridge`, Port 8443 freigeben, `web.tls.ip_addresses` = IP von Klein, neue CA auf allen Geräten; UniFi-Regel auf die IP von Klein.
- *Nachteile:* Wake-on-LAN aus dem NAT-Netz erreicht das Server-Netz nicht (Linux leitet Broadcasts nicht weiter, `bc_forwarding` müsste auf Klein geändert werden); `from=` beim SSH-Key nur noch auf die IP von Klein; keine eigene IP und MAC mehr.

**F. Zweites Netzwerk für TwinRaid (`br0` plus Docker-Bridge zu Klein)**
- *Einstellungen:* Unraid 7.3.3 hat kein Template-Feld für ein zweites Netzwerk; `--network` in den *Extra Parameters* oder `docker network connect` nach jedem Start per Skript. Die Unraid-Oberfläche (und damit die API) lauscht nur auf Schnittstellen nach dem Muster `^(br|bond|eth|wlan|wg)[0-9]+` (`rc.library.source`); Docker-Bridges heißen `br-<hash>` und passen nicht → unter *Settings → Network Settings → Interface Extra* ergänzen. Die Standardroute muss über `br0` bleiben (`gw-priority`).
- *Code:* Die IP-Erkennung (5.1) fände zwei Adressen → Erkennung über die Schnittstelle der Standardroute nötig (Konzeptänderung).
- *Zu prüfen:* ob ein `--internal`-Netz mit Docker 29 den Host über das Gateway erreicht.

**G. Unix-Socket der Unraid-API einbinden** (`/var/run/unraid-api.sock`)
- *Einstellungen:* Im Template nur diese eine Datei einbinden (nie den Ordner `/var/run`, dort liegt auch `docker.sock`). Netzwerk und UniFi bleiben unverändert.
- *Sicherheit:* Am Socket gilt dieselbe Anmeldung wie über das Netz (API-Key, alternativ Cookie oder lokale Sitzung). Key mit `DOCKER:UPDATE_ANY` in TwinRaid. Startprüfung und Starttest müssen sicherstellen, dass hinter dem Socket die Unraid-API antwortet und nicht Docker.
- *Nachteile:* Startet die Unraid-API neu, legt sie den Socket neu an, und die eingebundene Datei zeigt ins Leere → Neustart des Containers nötig. Abweichung von 3.5 (API über HTTP).

**I. Wächter auf Klein mit Auftragsdateien (neu)**
- *So:* Ein kleines Skript auf Klein (Ablage in `/boot/config/twinraid/`, gestartet aus `/boot/config/go`, wartet auf das Array) prüft alle 1–2 s einen festen Ordner im Datenordner von TwinRaid (`/mnt/user/appdata/twinraid/data/worker/docker-requests`, auf dem Host root-Rechte). Der Worker legt dort Aufträge in festem Format ab („stop `<Name>`“, „start `<Name>`“). Der Wächter führt nur diese beiden Befehle über die Unraid-API am lokalen Socket aus und schreibt das Ergebnis zurück. Der Key mit `DOCKER:READ_ANY`/`DOCKER:UPDATE_ANY` liegt **nur auf Klein** (root, `0600`), nie in TwinRaid.
- *Grenzen im Wächter, fest im Code:* nur `start` und `stop`; Namen nach den Regeln von Docker geprüft und in der Containerliste vorhanden; Container mit dem Image `ghcr.io/mantua1/twinraid` werden nie gestoppt; `start` nur für Container, die der Wächter selbst innerhalb desselben Laufs gestoppt hat; jeder Auftrag im Syslog von Klein.
- *Einstellungen:* Unraid: Skript ablegen, eine Zeile in `/boot/config/go`, API-Key für Klein anlegen (ohne Rolle, nur Docker) und beim Wächter hinterlegen. Netzwerk und UniFi: nichts. Kein zusätzliches Volume für TwinRaid (der Datenordner ist schon eingebunden).
- *Sicherheit:* Erfüllt Anforderung 1 auch bei einem übernommenen Container: TwinRaid kann nur Start und Stopp anfordern, keine Updates, kein Pausieren, keine Logs lesen. Der Wächter läuft als root auf Klein und muss so klein und streng sein wie `guard.sh`. Die Steuerung läuft weiterhin über die Unraid-API (CLAUDE.md, Regel 1), nur nicht aus dem Container.
- *Nachteile:* ein zusätzliches Programm auf Klein (Pflege bei Updates, wie `guard.sh`); läuft der Wächter nicht, findet kein Container-Stopp statt (TwinRaid erkennt das an fehlenden Antworten und lässt Phase B mit Warnung aus); bis zu 2 s Verzögerung je Auftrag. JSON-Verarbeitung auf Klein am besten mit dem vorhandenen PHP von Unraid statt Bash.

**H. Konsistenzlauf ohne Container-Stopp**
- `sync.stop_containers: false`; keine Rechte, keine Änderung. appdata laufender Container (z. B. SQLite) kann inkonsistent auf Groß ankommen. Höchstens als Übergang.

**Verworfen:** Host access trotz macvlan mit Bridging (Risiko laut 5.1); Netzwerk `host` für TwinRaid; Docker-Socket-Proxy (gibt TwinRaid über einen zweiten Container Zugriff auf `docker.sock`, widerspricht Regel 1; Freigaben nur je API-Bereich, nicht je Befehl); Weg über Groß (Groß müsste die API von Klein aufrufen).

**Empfehlung (Entscheidung steht aus):** **Option I**. Sie ist die einzige, die alle drei Anforderungen erfüllt: kein Eingriff ins Netzwerk, keine Auswirkung auf andere Container, und TwinRaid selbst kann nur Start und Stopp anfordern. Alle Optionen, bei denen TwinRaid die API von Klein selbst aufruft (A bis G), brauchen `DOCKER:UPDATE_ANY` und erlauben damit auch Image-Updates und Pausieren. Als einfachere Rückfallebene bleibt **G** (keine Netzänderung, aber der breite Key liegt in TwinRaid), bis zur Umsetzung von I oder als Übergang **H**.

---

## 11. Entwicklungsumgebung

- Entwicklungs-PC: Linux Mint 22.3 (Basis Ubuntu 24.04 „noble“).
- Python 3.14, von `uv` verwaltet (`backend/.python-version`). Node.js 24 LTS über nvm.
- **Docker im Rootless-Modus:** Docker-Kontext `rootless`, Socket `/run/user/1000/docker.sock`. Die Gruppe `docker` ist bewusst nicht eingerichtet. Docker-Befehle laufen ohne `sudo` gegen den Rootless-Daemon.
- Folgen für lokale Container-Tests:
  - root im Container entspricht dem eigenen Benutzer auf dem Host. Andere UIDs im Container, etwa 10001, werden auf Hilfs-UIDs des Hosts (subuid) abgebildet. Gemountete Dateien wie die Konfiguration müssen deshalb für alle lesbar sein (`o+r`).
  - Capabilities gelten nur innerhalb des User-Namespace des Rootless-Daemons. Der Starttest mit `--cap-drop=ALL` prüft trotzdem dieselbe Liste wie auf Unraid. Die CI führt denselben Test zusätzlich mit normalem Docker (rootful) aus.
  - Ports werden lokal nur an `127.0.0.1` gebunden.
- Lokale Container-Tests laufen immer mit `TWINRAID_MODE=mock`.
