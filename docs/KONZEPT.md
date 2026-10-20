# TwinRaid – Projektkonzept

> Arbeitstitel „TwinRaid“ · Version 0.14 · Stand 19.10.2026
> Dieses Dokument ist die verbindliche Grundlage für die Entwicklung. Änderungen am Konzept werden hier eingetragen, bevor sie umgesetzt werden.

---

## 1. Ziel

Eine Weboberfläche in **einem einzigen Docker-Container** auf dem stromsparenden Unraid-Server („Unraid Klein“). Darüber kann man:

- den stromhungrigen Unraid-Server („Unraid Groß“) per Wake-on-LAN starten und sauber herunterfahren,
- den Online-Status beider Server und der Gaming-VM sehen,
- die Gaming-VM auf Unraid Groß starten und stoppen,
- einen **Spiegel-Sync** der konfigurierten Shares von Klein nach Groß starten, inklusive Container-Daten (appdata). Klein hält dafür die freigegebenen Container kurz an, legt ZFS-Snapshots an und sendet sie mit `zfs send` nach Groß; jeder Lauf ist dort ein eigener Snapshot, ältere Stände bleiben 30 Tage erhalten (ab Phase 5, Entscheidung E25). Container-Vorlagen (Templates) folgen mit v0.5.4. Der Sync läuft manuell oder zeitgesteuert und zeigt Live-Fortschritt und Restzeit.

**Ziel des Syncs:** Fällt Unraid Klein aus, wird Unraid Groß manuell gebootet. Dort lassen sich alle Container mit dem Stand des letzten Syncs anlegen und starten; die Schritte stehen in `docs/WIEDERHERSTELLUNG.md` (siehe 4.6).

**Nicht-Ziele:** Zugriff aus dem Internet, Mehrbenutzerverwaltung, Bearbeiten der Konfiguration über die Weboberfläche, Sync in Gegenrichtung (Groß → Klein; den Rückweg nach einem Notbetrieb erledigt der Nutzer von Hand, siehe 4.6), Notbetrieb als Funktion von TwinRaid.

---

## 2. Ausgangslage

| | Unraid Klein | Unraid Groß |
|---|---|---|
| Rolle | läuft immer, Sync-Quelle | läuft bei Bedarf, Sync-Ziel und Gaming-Host |
| Version | Unraid OS 7.3.3 | Unraid OS 7.3.3 |
| Besonderheit | hostet den TwinRaid-Container | Gaming-VM (Bild und USB auf einen TV in einem anderen Raum) |
| Dateisystem (ab Phase 5) | ZFS-Pool `mainpool`; jede gesicherte Share ist ein eigenes Dataset `mainpool/<Share>` | ZFS-Pool `mainpool` nur für die Spiegel; VMs und `system` liegen auf einem anderen Pool, der nie gespiegelt wird |

- Beide Server liegen im selben Subnetz („Server-Netz“) und sind mit 2,5 GbE angebunden. Es sind maximal ca. 1 TB Daten zu synchronisieren.
- Die Weboberfläche muss auch aus einem anderen Subnetz („Client-Netz“) erreichbar sein. Das wird über eine UniFi-Firewall-Regel freigegeben.
- Auf Groß liegen die Spiegel unter `mainpool/twinraid-mirror/<Share>`. Erst im Notbetrieb entstehen dort Shares mit **denselben Namen** und unter **denselben Pfaden** `/mnt/mainpool/<Share>` wie auf Klein (Klone der Spiegel, siehe 4.6). Im Normalbetrieb gibt es auf Groß keine Share-Konfigurationen mit diesen Namen (Entscheidung E26).
- Auf Groß ist der **Docker-Dienst** im Normalbetrieb **ausgeschaltet**, damit es keine Konflikte mit Klein gibt. Auf Groß existieren keine eigenen appdata-Daten oder Templates, die geschützt werden müssen (E1).

---

## 3. Architektur

### 3.1 Überblick

```
  Client-Netz                     Server-Netz
  ┌───────────┐   HTTPS :8443
  │  Browser  │ ──────────────┐
  └───────────┘  (UniFi-Regel)│
                              ▼
  ┌───────────────────────── Unraid KLEIN (immer an) ────────────────────────┐
  │                                                                          │
  │   Container "TwinRaid"  (eigene IP auf br0, ohne Shares und Key „sync“)  │
  │   ┌────────────────────┐   Unix-Socket    ┌─────────────────────────┐    │
  │   │ Web-Prozess        │ ───────────────▶ │ Worker-Prozess          │    │
  │   │ unprivilegiert     │  feste Befehle   │ minimal privilegiert    │    │
  │   │ UI, Login, API,SSE │                  │ Status, WoL, SSH        ├──┐ │
  │   └────────────────────┘                  └───┬───────────────▲─────┘  │ │
  │                                  Auftrag (rw) │    Status (ro)│        │ │
  │  ─ ─ Host von Klein (root) ─ ─ ─ ─ ─ ─ ─ ─ ─ ─│─ ─ ─ ─ ─ ─ ─ ─│─ ─ ─ ─ │ │
  │   ┌───────────────────────────────────────────▼───────────────┴─────┐  │ │
  │   │ Wächter source-guard.sh: freigegebene Container anhalten und    │  │ │
  │   │ starten (Docker-CLI), ZFS-Snapshots in mainpool, zfs send       │  │ │
  │   └──────────────────────────────┬──────────────────────────────────┘  │ │
  └──────────────────────────────────┼─────────────────────────────────────┼─┘
                                     │ zfs send über SSH      WoL, SSH mit │
                                     │ Key „sync“            Keys „status“ │
                                     │ (ab v0.5.1)             und „power“ │
                                     ▼                                     ▼
  ┌───────────────────────── Unraid GROSS (bei Bedarf) ──────────────────────┐
  │   guard.sh sync → mainpool/twinraid-mirror/<Share> (schreibgeschützt)    │
  │   guard.sh status · power → Gaming-VM, weitere VMs, Herunterfahren       │
  │   VMs und system auf einem anderen Pool, der nie gespiegelt wird         │
  └──────────────────────────────────────────────────────────────────────────┘
```

Ab Phase 5 (v0.5.0) läuft auf dem Host von Klein der Wächter `source-guard.sh` (3.6). TwinRaid erreicht ihn nur über eine eingebundene Auftragsdatei und liest seinen Status. Shares, Docker und den Key „sync“ erreicht nur der Wächter; auf Groß nimmt `guard.sh sync` die Snapshots an (3.7).

### 3.2 Zwei Prozesse in einem Container (zentrale Sicherheitsgrenze)

| | Web-Prozess | Worker-Prozess |
|---|---|---|
| Aufgabe | Weboberfläche ausliefern, Login, Sessions, REST-API, Live-Updates (SSE) | alles mit Außenwirkung: Status-Checks, WoL, SSH, Aufträge an den Wächter auf Klein (ab v0.5.0), Zeitsteuerung |
| Benutzer | eigene unprivilegierte UID (z. B. 10001) | root im Container, nach dem Start nur mit Capability `DAC_READ_SEARCH` (darf alles lesen, nichts schreiben; ob sie ab v0.5.1 entfallen kann, prüft der Starttest, siehe 5.4) |
| Zugriff auf Secrets | **nein** | ja |
| Zugriff auf Share-Daten | **nein** | **nein** (ab Phase 5: die Daten sendet der Wächter auf Klein, siehe 3.6) |

- Die beiden Prozesse kommunizieren über einen Unix-Socket mit einer **festen, kleinen Befehlsliste**: `get_status`, `verify_password`, `wake_target`, `shutdown_target`, `shutdown_target_force_vm` (ab v0.4.1, siehe 4.2), `vm_start`, `vm_stop`, `vm_force_stop`, `sync_start`, `sync_dry_run` (ab v0.5.1, Entscheidung E19), `sync_cancel`, `sync_confirm_deletions`.
- Befehle haben **keine frei wählbaren Parameter** (keine Pfade, Hosts oder Shell-Befehle). Erlaubt sind höchstens IDs, die der Worker selbst vergeben hat (z. B. eine Job-ID).
- **Einzige Ausnahme `verify_password` (ab Phase 2):** Der Befehl trägt das eingegebene Passwort, höchstens 1024 Zeichen. Der Worker vergleicht es nur mit dem Argon2id-Hash und verwendet es nie als Pfad, Argument oder Logeintrag. So bleibt der Hash ein Secret, das nur der Worker lesen kann (siehe 5.3).
- **Drosselung im Worker:** Der Worker prüft immer nur ein Passwort gleichzeitig. Jede Prüfung dauert mindestens 1 Sekunde. Nach 5 Fehlversuchen in 15 Minuten wartet er vor jeder weiteren Prüfung zusätzlich, beginnend mit 1 Sekunde und verdoppelt bis höchstens 30 Sekunden. Ein erfolgreicher Login setzt die Zählung zurück, ebenso `twinraid-admin unlock`. So kann auch ein übernommener Web-Prozess nicht schnell durchprobieren.
- **Konsequenz:** Wird der Web-Prozess kompromittiert, kann ein Angreifer nur das tun, was auch die Buttons können. Er kann keine Keys stehlen, keine Daten lesen und keine Pfade ändern.
- **Pfadschutz:** Die Einbindungen des Wächters liegen unter `/srv/worker/source-guard` (ab v0.5.0), die Secrets unter `/data/secrets`; beide Ordner haben die Rechte `0700` und gehören root. Shares bindet TwinRaid seit E25 gar nicht mehr ein.
- **Aufträge an den Wächter (ab v0.5.0):** Nur der Worker schreibt Aufträge im festen Format aus 3.6 in die eingebundene Auftragsdatei und liest den Status des Wächters. Auch ein übernommener Worker kann damit nur anfordern, was der Wächter nach seinen eigenen Regeln erlaubt. Neue Auftragsarten nur nach Rückfrage, wie bei den Worker-Befehlen.

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
| Unraid-Steuerung | Groß: SSH mit erzwungenem Befehl über `guard.sh`, ohne API-Key (ab v0.4.3, Entscheidung E22); Klein: Wächter `source-guard.sh` auf dem Host mit der Docker-CLI, ohne API-Key (ab v0.5.0, Entscheidungen E7 und E13, siehe 3.6) |
| Kopieren | ZFS: Snapshots auf Klein, `zfs send` durch den Wächter über SSH, `zfs receive` über `guard.sh` auf Groß (ab v0.5.1, Entscheidung E25, siehe 3.6 und 3.7); kein rsync |
| Host-Skripte | `guard.sh` (Groß) und `source-guard.sh` (Klein) in Bash, nur mit den Bordmitteln von Unraid 7.3.3 (bash, coreutils, flock, logger, docker, zfs, ssh, virsh); kein Python auf den Servern; geprüft mit shellcheck |
| SSH-Client | `openssh-client` aus Debian, Version exakt gepinnt (ab Phase 4); das setuid-Programm `ssh-keysign` wird entfernt |
| Tests | pytest (Backend), Vitest (Frontend); Mocks für alle externen Systeme; in der CI zusätzlich der Wächter mit echtem Docker (ab v0.5.0) und echte ZFS-Pools in Dateien (`zfsutils-linux`, nur in der CI und exakt gepinnt; ab v0.5.0 für `zfs-check.sh`, ab v0.5.1 auch für Wächter und guard.sh) |
| Qualität | ruff, mypy (strict), ESLint, Prettier; CI über GitHub Actions |
| Veröffentlichung | Image auf `ghcr.io`, gebaut und veröffentlicht nur von GitHub Actions, mit Herkunftsnachweis (siehe 5.8) |

### 3.4 Schnittstellen zu Unraid

| Aufgabe | Weg | Rechte |
|---|---|---|
| Groß online/offline | TCP-Check auf den WebGUI-Port aus `servers.target.webgui`; es werden keine Daten gesendet | keine |
| Klein online/offline | keine Prüfung: TwinRaid läuft auf Klein, im Modus `real` gilt Klein deshalb immer als online (ab v0.3.2, siehe 4.1) | keine |
| Wake-on-LAN | UDP-Magic-Packet an die Broadcast-Adresse des Server-Netzes (siehe 4.2) | keine |
| Gaming-VM Status, weitere VMs | SSH: eine dauerhafte Sitzung `guard.sh status` → `vm-watch`, solange Groß online ist (ab v0.4.3, siehe 3.5) | SSH-Key „status“, darf nur `check` und `vm-watch` |
| Grenze des ZFS-Caches (ARC) von Groß | dieselbe Statuszeile von `vm-watch` meldet `zfs_arc_max` und `c_max` (ab v0.5.1, siehe 3.5) | SSH-Key „status“ |
| Gaming-VM starten und erzwingen | SSH: `guard.sh power` → `vm-start` bzw. `vm-force-stop` (`virsh start`, `virsh destroy`), ab v0.4.3; ab v0.5.1 prüft `vm-start` vorher den freien Arbeitsspeicher und erkennt belegte Geräte (siehe 3.5) | SSH-Key „power“ |
| Gaming-VM herunterfahren | SSH: `guard.sh power` → `vm-shutdown` (`virsh shutdown`), nur ACPI (ab Phase 4, Entscheidung E8) | SSH-Key „power“ |
| Container anhalten und starten (Konsistenz beim Sync) | Wächter `source-guard.sh` auf dem Host von Klein: TwinRaid schreibt einen Auftrag in die eingebundene Auftragsdatei, der Wächter ruft die Docker-CLI mit fester Argumentliste auf (ab v0.5.0, E7 und E13, siehe 3.6) | in TwinRaid keine; der Wächter läuft als root auf Klein und hält nur Container aus seiner Erlaubnisliste an |
| Snapshots und Senden | Wächter: `zfs snapshot`, `zfs diff` und `zfs send` auf Klein, über SSH an `guard.sh sync` auf Groß, dort `zfs receive` mit fester Befehlszeile (ab v0.5.1, E25, siehe 3.6 und 3.7) | SSH-Key „sync“, liegt nur auf dem Host von Klein; `guard.sh` empfängt nur in `mainpool/twinraid-mirror/<Name>` |
| Groß herunterfahren | SSH: `guard.sh` führt `/sbin/poweroff` aus, wie der Button in der Unraid-WebGUI (ab Phase 4); ab v0.4.3 nur, wenn alle VMs aus sind; ab v0.5.1 nicht während eines Empfangs | SSH-Key „power“, darf ausschließlich `check`, `vm-start`, `vm-shutdown`, `vm-force-stop` und `poweroff`, alle ohne Parameter (siehe 5.6) |

**Kein Docker-Socket und kein Key mit Docker-Rechten im Container. Kein Root-Shell-Zugang auf irgendeinem Server.** Welche VM gemeint ist, steht nur auf Groß in `/boot/config/twinraid/power.conf`; TwinRaid schickt guard.sh nie Parameter (ab v0.4.3).

**Ergebnisse der Prüfung in Phase 4** (Quellcode von Unraid-API v4.37.5 und Unraid-WebGUI 7.3):

- Die Unraid-API hat **keine Funktion zum Herunterfahren** des Servers. Groß wird deshalb per SSH heruntergefahren.
- `/sbin/poweroff` ist der saubere Weg: Er startet das Shutdown-Skript von Unraid, das das Array über `emhttpd` stoppt. Dabei stoppt Unraid Container und VMs geordnet (VMs mit der Einstellung *VM shutdown time-out*, Standard 60 s) und hängt die Platten aus; das gesamte Zeitlimit kommt aus *Disk Settings → Shutdown time-out* (Standard 90 s). Der frühere Befehl `powerdown` ist veraltet.
- **Warum die VM nicht über die API heruntergefahren wird (E8):** `vm.stop` sendet zwar ein ACPI-Signal, wartet aber nur 10 Sekunden und schaltet die VM dann selbst hart aus (`destroy`). Windows könnte so mitten im Herunterfahren abgeschaltet werden, etwa während eines Updates, und „Erzwingen erst nach `shutdown_timeout_minutes`“ ließe sich nicht umsetzen. `virsh shutdown` sendet nur das ACPI-Signal, wie „Stop“ in der Unraid-WebGUI. Der Key „power“ darf damit nur VMs herunterfahren; das ist weniger, als er mit `poweroff` ohnehin darf.
- `vm.start` und `vm.forceStop` verlangen `VMS:UPDATE_ANY`. Die Rechteprüfung greift in 4.37.5 auch für diese verschachtelten Befehle (`fieldResolverEnhancers: ['guards']`); der Fehler aus unraid/api #1723 ist dort behoben. In Phase 4 wurde das real geprüft.
- **Seit v0.4.3 (Entscheidung E22)** spricht TwinRaid die API von Groß gar nicht mehr an. `vm.start` ruft in der API libvirt `domain.create()` auf (bei pausierter VM `resume()`), `vm.forceStop` ruft `domain.destroy()` auf; `virsh start` und `virsh destroy` über guard.sh bewirken dasselbe. Grund: Unraid speichert API-Keys im Klartext auf dem USB-Stick (`/boot/config/plugins/dynamix.my.servers/keys`), mit Unraid Connect Flash Backup unverschlüsselt auch in der Cloud, und bei `scheme: http` ging der Key im Klartext durchs Server-Netz. Ein gestohlener Key durfte alle VMs steuern; der Key „power“ darf nur die eingetragene VM steuern und Groß nur ausschalten, wenn alle VMs aus sind.

### 3.5 VM-Status und VM-Aktionen über guard.sh (ab v0.4.3, Entscheidung E22)

Bis v0.4.2 fragte TwinRaid den VM-Status über die Unraid-API von Groß ab und startete bzw. erzwang die VM darüber, mit einem API-Key in `/data/secrets`. Seit v0.4.3 läuft alles über SSH und `guard.sh`; TwinRaid braucht für Groß keinen API-Key mehr (Begründung in 3.4).

**Welche VM:** Die Gaming-VM steht nur auf Groß in `/boot/config/twinraid/power.conf`: genau eine Zeile `vm <UUID>`, dazu höchstens Leerzeilen und Zeilen, die mit `#` beginnen. Die UUID steht in Unraid bei der VM (Bearbeiten, XML-Ansicht) oder kommt von `virsh domuuid "<Name>"`. Die Datei ist kein Secret; TwinRaid kann sie weder lesen noch ändern. Fehlt sie oder ist sie ungültig, meldet `vm-watch` `NOT_CONFIGURED`, und alle VM-Befehle werden abgelehnt. Eine andere VM wird auf Groß eingetragen, ohne TwinRaid zu ändern.

**guard.sh Version 3** (ab v0.5.1; Version 2 seit v0.4.3; im Repo `host-scripts/guard.sh`, auf Groß `/boot/config/twinraid/guard.sh`):

```
guard.sh status       Key „status“: kann nichts verändern
  check          → twinraid-guard 3 status ok
  vm-watch       → eine Statuszeile bei jeder Änderung, sonst alle 15 s

guard.sh power        Key „power“: nur für freigeschaltete Aktionen
  check          → twinraid-guard 3 power ok
  vm-start       → Speicherprüfung, dann virsh start --domain <UUID aus power.conf>
  vm-shutdown    → virsh shutdown --domain <UUID>   (nur ACPI, E8)
  vm-force-stop  → virsh destroy --domain <UUID>
  poweroff       → nur wenn alle VMs aus sind und kein Empfang läuft, dann /sbin/poweroff

guard.sh sync         Key „sync“ vom Host von Klein (siehe 3.7)
  check          → twinraid-guard 3 sync ok, dazu eine Zeile je Spiegel
  receive <Name> → zfs receive mit fester Befehlszeile nach mainpool/twinraid-mirror/<Name>
```

- Die Anfrage steht in `SSH_ORIGINAL_COMMAND` und wird nur mit festen Texten verglichen; Parameter gibt es nicht. Einzige Ausnahme ist `receive <Name>` im Modus `sync`: Der Name muss genau einer Zeile `mirror <Name>` in `mirror.conf` entsprechen (3.7). Alles andere endet mit Exit-Code 2 („denied“) und einem Eintrag im Syslog von Groß.
- Exit-Code 3 heißt: Die Anfrage ist erlaubt, eine Prüfung in guard.sh lehnt sie aber ab. Auf stderr steht dann `twinraid-guard: refused <Grund>` mit `vm-not-configured`, `vm-running`, `other-vms-running`, `vm-state-unknown` oder `watch-busy`; ab Version 3 zusätzlich `memory-low`, `device-busy` und `sync-running` (Modus `power`) sowie die Gründe des Modus `sync` aus 3.7.
- Höchstens zwei `vm-watch`-Schleifen laufen gleichzeitig (Sperrdateien `/var/run/twinraid-guard-watch.<1|2>.lock` mit `flock`). Eine dritte Sitzung lehnt guard.sh mit `watch-busy` ab; TwinRaid versucht es wie nach einem Abbruch erneut. Zwei Plätze genügen, weil eine alte Schleife nach einem Abbruch spätestens beim nächsten Lebenszeichen endet. So kann auch ein übernommener TwinRaid auf Groß nicht beliebig viele Schleifen starten.
- Jeder Aufruf von virsh läuft mit `LC_ALL=C` und einem Zeitlimit (`timeout`): 10 s für Abfragen, 90 s für `vm-start`, 60 s für `vm-force-stop`, 30 s für `vm-shutdown`. Ausgaben von virsh gibt guard.sh nie weiter; ab Version 3 liest es die Fehlermeldung von `virsh start` nur intern, um ein belegtes Gerät zu erkennen (siehe „Start der VM“).
- Ins Syslog kommen nur feste Texte und die geprüfte UUID, nie die Anfrage selbst. Einzige Ausnahme ab Version 3 ist die erste Fehlerzeile von `zfs receive`, bereinigt und gekürzt (3.7).
- **Version:** TwinRaid ab 0.4.3 verlangt guard.sh Version 2 (0.4.4 und 0.5.0 ändern guard.sh nicht), TwinRaid ab 0.5.1 Version 3. `check` und jede Statuszeile nennen die Version; ist sie älter, lautet die Ursache `guard_outdated` mit dem Hinweis, guard.sh neu zu kopieren. guard.sh Version 1 kennt den Modus `status` nicht und lehnt ab (`ssh_denied`); auch dann weist die Oberfläche auf das Kopieren hin. **Umstieg auf 0.5.1:** erst TwinRaid aktualisieren (die VM-Kachel zeigt dann „guard.sh zu alt“), dann guard.sh Version 3 kopieren, dann `twinraid-admin trust-host-key target` (beendet die Pause sofort). In umgekehrter Reihenfolge verstünde TwinRaid 0.5.0 die Zeile `watch 3` nicht.

**Statuszeile von `vm-watch`** (nur ASCII, höchstens 16 KiB), zuerst Version 2 (v0.4.3 bis v0.5.0), darunter Version 3 (ab v0.5.1, zusätzlich `arc_max` und `c_max`):

```
watch 2 vm=<Zustand> name=<Hex|-> others=<Anzahl|?> names=<Hex>,<Hex>…|- connect=<present|absent> flash_backup=<active|inactive|unknown>
watch 3 vm=<Zustand> name=<Hex|-> others=<Anzahl|?> names=<Hex>,<Hex>…|- connect=<present|absent> flash_backup=<active|inactive|unknown> arc_max=<Byte|-|?> c_max=<Byte|-|?>
```

- `vm`: Zustand der eingetragenen VM aus `virsh domstate`, in denselben Werten wie bisher die API: `RUNNING`, `IDLE`, `PAUSED`, `SHUTDOWN`, `SHUTOFF`, `CRASHED`, `PMSUSPENDED`, `NOSTATE`. Dazu `NOT_CONFIGURED` (`power.conf` fehlt oder ist ungültig), `NOT_FOUND` (keine VM mit dieser UUID), `LIBVIRT_DOWN` (der VM-Dienst läuft nicht) und `UNKNOWN` (virsh antwortet nicht innerhalb des Zeitlimits). Die Zuordnung zur Anzeige steht in 4.1.
- `name`: Name der eingetragenen VM, hex-kodiert. `gaming_vm.unraid_vm_name` ist ab v0.4.3 optional. Ist es gesetzt und weicht der Name ab, zeigt die VM-Kachel den Hinweis `vm_name_mismatch` („Auf Groß ist eine VM mit anderem Namen eingetragen“). Das fängt eine falsche UUID in `power.conf` ab.
- `others` und `names`: weitere VMs (Konzept 4.2), also alle außer der eingetragenen in jedem Zustand außer „shut off“. Die Namen sind hex-kodiert, höchstens 20, je höchstens 256 Byte. `others=?` heißt: Die Liste ist nicht lesbar (dann `names=-`).
- `connect` und `flash_backup`: Merkmale von Unraid Connect (siehe unten).
- `arc_max` und `c_max` (ab Version 3): die in Unraid eingetragene Grenze des ZFS-Caches (ARC) aus `/sys/module/zfs/parameters/zfs_arc_max` und die wirksame Obergrenze `c_max` aus `/proc/spl/kstat/zfs/arcstats`, jeweils in Byte als Dezimalzahl mit höchstens 20 Ziffern. `-`: ZFS ist auf Groß nicht geladen; `?`: nicht lesbar. Siehe „Grenze des ARC“ unten.
- Die Namen sind Text von Groß und eine bewusste Ausnahme von der Regel, dass kein fremder Text in den Status gelangt: TwinRaid dekodiert sie als UTF-8 und bereinigt sie (Steuer- und unsichtbare Formatzeichen werden ersetzt, höchstens 64 Zeichen je Name, höchstens 20 Namen), liefert sie nur über geschützte Routen aus und loggt sie nie; Aktionslog und Container-Log enthalten nur den festen Code `other_vms_running`.
- Eine Zeile, die nicht genau diesem Format entspricht, beendet die Sitzung (`watch_interrupted`).

**Takt und Anmeldungen:** eine dauerhafte Sitzung je Laufzeit von Groß statt einer Anmeldung je Abfrage. Abgefragt wird lokal auf Groß; über das Netz geht nur eine kurze Zeile.

| Was | wie oft | wer |
|---|---|---|
| Online-Prüfung von Groß (TCP auf den WebGUI-Port) | alle 10 s, Zeitlimit 2 s | Worker |
| Aufbau der Sitzung `vm-watch` mit dem Key „status“ | einmal, sobald Groß online ist; danach nur nach einem Abbruch (Neuversuche unten) | Worker |
| VM-Zustände lesen (`virsh list --all --uuid`, `virsh list --inactive --uuid`, `virsh domstate`) | alle 3 s | guard.sh, lokal |
| Namen weiterer VMs (`virsh domname`) | nur für VMs, die nicht aus sind | guard.sh |
| `power.conf` und Name der eingetragenen VM neu lesen; Merkmale von Unraid Connect und Werte des ARC | alle 15 s bzw. alle 60 s | guard.sh |
| Zeile an TwinRaid | bei jeder Änderung, sonst alle 15 s als Lebenszeichen | guard.sh |
| Sitzung gilt als hängend | nach 40 s ohne Zeile; eine tote Verbindung erkennt SSH schon nach 10–15 s (`ServerAliveInterval=5`, `ServerAliveCountMax=2`) | Worker |
| `get_status` | alle 2 s, nur solange ein Dashboard offen ist; liest nur den Stand im Worker | Web-Prozess |

- **Status sofort:** Jede Zeile mit einer Änderung löst sofort eine neue Statusprüfung im Worker aus; die Oberfläche wartet nicht auf die nächste reguläre Prüfung.
- **Abläufe ohne weitere Anmeldungen:** Auch die Abläufe aus 4.2 (VM starten, herunterfahren, erzwingen, Warten nach dem Wecken, Prüfung der VM-Liste vor `poweroff`) lesen nur den Stand der Sitzung. Eine eigene Anmeldung mit dem Key „power“ gibt es nur für den Befehl selbst (`vm-start`, `vm-shutdown`, `vm-force-stop`, `poweroff`). Im Normalbetrieb ergibt das eine Anmeldung je Start von Groß und eine je Aktion.
- **Bereitschaft nach dem Wecken:** Groß gilt als bereit, sobald `vm-watch` einen Zustand außer `LIBVIRT_DOWN` und `UNKNOWN` liefert. Der Schritt heißt `waiting_for_vm_service` („Wartet auf den VM-Dienst“, bis v0.4.2 `waiting_for_api`).
- **Syslog von Groß:** sshd schreibt je Anmeldung einige Zeilen; daran ändert TwinRaid nichts. guard.sh schreibt je Sitzung eine Zeile „vm-watch started“, eine ungültige `power.conf` einmal je Sitzung, und jede Aktion und Ablehnung einzeln. Einzelne Abfragen und Zustandswechsel schreibt es nicht; Start und Stopp der VMs protokolliert libvirt selbst.

**Wenn Groß aus ist, startet oder wegfällt:**

- **Aus:** Die TCP-Prüfung scheitert nach höchstens 2 s, die VM gilt als aus mit „Server offline“. TwinRaid versucht keine SSH-Verbindung.
- **Startet** (geweckt oder von Hand): Sobald die TCP-Prüfung klappt, baut der Worker die Sitzung auf. Lehnt sshd noch ab, versucht er es alle 3 s erneut, höchstens `boot_timeout_minutes` lang; die VM zeigt so lange „Verbindung wird aufgebaut“. Bis libvirt läuft, meldet `vm-watch` `LIBVIRT_DOWN`.
- **Fährt sauber herunter:** sshd beendet die Verbindung, der Worker prüft sofort per TCP. Nicht erreichbar: „Server offline“. Noch erreichbar: kurz „Unbekannt“ mit „Sitzung unterbrochen“ und neuer Versuch nach 5 s.
- **Fällt plötzlich weg** (Strom, Kabel): SSH bemerkt es nach 10–15 s, die TCP-Prüfung nach höchstens 12 s. Was zuerst kommt, beendet die Sitzung; der Worker beendet den ssh-Prozess sofort.
- **virsh hängt:** Das Zeitlimit bricht den Aufruf ab, die Zeile meldet `UNKNOWN`, die Schleife läuft weiter. So bleiben auf Groß keine hängenden Prozesse zurück.

**Neuversuche** (begrenzen die Anmeldungen auch im Fehlerfall):

- **Verbindung abgebrochen oder nicht zustande gekommen,** Groß per TCP erreichbar: neuer Versuch nach 5 s, 15 s und 60 s, danach alle 5 Minuten. Nach einer Sitzung, die länger als 60 s lief, beginnt die Folge von vorn. Bei einem Dauerfehler höchstens etwa 12 Anmeldungen pro Stunde.
- **Dauerhafte Fehler** (Key fehlt, Hostkey nicht bestätigt oder geändert, Key abgelehnt, guard.sh lehnt ab oder ist zu alt): kein automatischer Versuch, bis Groß neu startet (offline, dann online), sich Key oder bestätigter Hostkey ändern (`init-ssh-keys`, `trust-host-key`; der Worker erkennt die geänderte Datei), `trust-host-key` die Verbindung mit dem Key „status“ erfolgreich testet (ab v0.4.4, siehe unten) oder 30 Minuten vergangen sind. Höchstens 2 Anmeldungen pro Stunde.
- **Pause sofort beenden (ab v0.4.4):** Viele Ursachen behebt man auf Groß (Zeile in `authorized_keys`, guard.sh kopieren); dabei ändert sich in TwinRaid keine Datei. Besteht in `twinraid-admin trust-host-key` der Verbindungstest mit dem Key „status“, sendet die CLI dem Worker über s6 das Signal `SIGUSR2` (`s6-svc -2`, wie `SIGUSR1` bei `unlock`). Der Worker beendet dann jede Wartezeit der Sitzung und versucht es sofort, sofern Groß online ist und keine Sitzung läuft; eine laufende Sitzung bleibt unberührt. Das Signal trägt keine Daten, und nur root im Container kann es auslösen; der Web-Prozess kann es nicht senden. Es ist kein neuer Worker-Befehl.
- **Nächster Versuch im Status (ab v0.4.4):** Wartet der Worker nach einem Fehler, enthält der Status `watch_retry` mit dem Zeitpunkt des nächsten Versuchs (`at`, auf volle Sekunden aufgerundet) und ob es die Pause nach einem dauerhaften Fehler ist (`paused`). Die VM-Kachel zeigt „Nächster Versuch um hh:mm:ss“, bei einer Pause mit dem Hinweis, dass ein erfolgreiches `twinraid-admin trust-host-key target` sie sofort beendet. Während des Startfensters nach dem Wecken (Versuche alle 3 s, Anzeige „Verbindung wird aufgebaut“), während einer laufenden Sitzung und solange Groß offline ist, fehlt `watch_retry`.
- Eine ältere `vm-watch`-Schleife auf Groß endet spätestens beim nächsten Lebenszeichen (≤ 15 s), weil das Schreiben in die geschlossene Verbindung scheitert.
- Im Container-Log stehen Beginn, Ende und Fehler der Sitzung nur als feste Codes, gleiche Meldungen höchstens einmal pro Minute.

**Prüfung vor `poweroff` in guard.sh:** Unmittelbar vor `/sbin/poweroff` prüft guard.sh selbst: Die eingetragene VM ist „shut off“ oder „crashed“ (wie im Worker), jede andere VM ist „shut off“. Ohne gültige `power.conf` muss jede VM „shut off“ sein. Sonst endet guard.sh mit Exit-Code 3 (`vm-running`, `other-vms-running`, oder `vm-state-unknown`, wenn virsh nicht antwortet), schreibt einen Eintrag ins Syslog, und Groß bleibt an. TwinRaid ordnet das `vm_running`, `other_vms_running` bzw. `vm_service_unavailable` zu. So gilt „nie ausschalten, solange VMs laufen“ (4.2) auch gegen einen übernommenen TwinRaid. Ab Version 3 lehnt guard.sh `poweroff` außerdem ab, solange ein Empfang läuft (`sync-running`, TwinRaid: `sync_running`). Herunterfahren über die Unraid-Oberfläche bleibt immer möglich.

**Start der VM (ab Version 3, Entscheidung E25e):**

- **Speicherprüfung vor `vm-start`:** guard.sh liest „Max memory“ der eingetragenen VM aus `virsh dominfo`. Als frei gilt `MemAvailable` aus `/proc/meminfo` plus der Teil des ARC über `c_min` (aus `/proc/spl/kstat/zfs/arcstats`), weil ZFS diesen Teil bei Bedarf abgibt. Reicht das nicht für die VM plus 1 GiB Sicherheitsabstand, endet guard.sh mit Exit-Code 3 und `refused memory-low`, ohne `virsh start` aufzurufen. Lässt sich „Max memory“ nicht lesen (VM-Dienst aus, Zeitlimit), lehnt es mit `vm-state-unknown` ab; fehlt `MemAvailable`, mit `memory-low`. Ohne lesbare Werte des ARC zählt nur `MemAvailable`. Z1 hat auf beiden Servern (OpenZFS 2.4.4) gezeigt, dass `MemAvailable` den ARC nicht enthält; der ganze Teil über `c_min` zählt also zusätzlich, ohne doppelt gezählt zu werden. Ob der Abstand 1 oder 2 GiB beträgt, wird nach dem Test M1 mit den auf Groß gemessenen Zahlen entschieden (v0.5.1); bis dahin gilt 1 GiB. Das schützt laufende VMs vor einem Start aus TwinRaid; ein Start über die Unraid-Oberfläche wird nicht geprüft. VMs mit durchgereichten Geräten belegen ihren ganzen Speicher schon beim Start; ohne Swap beendet der Kernel sonst meist eine laufende VM.
- **Belegtes Gerät:** Scheitert `virsh start` und passt seine Fehlermeldung (mit `LC_ALL=C`) auf das feste Muster `is in use by`, endet guard.sh mit `refused device-busy`. Andere Fehler geben den Exit-Code von virsh weiter. Die Meldung selbst gibt guard.sh nie weiter und schreibt sie nie ins Syslog.
- **Anzeige in TwinRaid:** `vm_device_busy` „Ein durchgereichtes Gerät der VM, etwa die Grafikkarte, wird gerade von einer anderen VM benutzt.“ (solange weitere VMs laufen, mit ihren Namen); `vm_memory_low` „Auf Groß ist nicht genug Arbeitsspeicher frei, um die VM zu starten.“; andere Fehler von `vm-start` als `vm_start_failed` „Unraid konnte die VM nicht starten; den Grund nennt das libvirt-Log.“ statt des allgemeinen `remote_failed`.

**Grenze des ARC (ab Version 3, Entscheidung E25a):**

- Die Grenze des ZFS-Caches legt der Nutzer nur in Unraid fest (*Settings → Disk Settings → Tunable (zfs_arc_max)*). `twinraid.yaml` enthält dafür keinen Wert, und TwinRaid rechnet nicht mit Annahmen über Arbeitsspeicher oder VMs.
- Ist `arc_max` genau `0`, ist in Unraid keine Grenze eingetragen, und OpenZFS wählt sie selbst. Dann zeigt die Kachel Groß den gelben Hinweis `target_arc_unlimited` („Auf Groß ist keine Grenze für den ZFS-Cache eingetragen. Trage sie in Unraid unter Settings → Disk Settings ein.“). Bei jedem anderen Wert, bei `-` und bei `?` erscheint kein Hinweis.
- Die Einstellungsseite und `twinraid-admin sync-check` zeigen `arc_max` und `c_max` als Information.

**Warnung zu Unraid Connect:**

- **Merkmal:** die Datei `/boot/config/plugins/dynamix.unraid.net.plg`; genau sie prüft die Unraid-API 4.37.5 selbst (`isConnectPluginInstalled()`). Zusätzlich zählen `/var/log/plugins/dynamix.unraid.net.plg` (im laufenden Betrieb geladen) und der ältere Name `dynamix.unraid.net.staging.plg` an beiden Orten. Eines davon genügt für `connect=present`. Kein Merkmal ist der Ordner `/boot/config/plugins/dynamix.my.servers`: Seit Unraid 7.2 legt die eingebaute API dort ihre Keys ab.
- **Flash Backup:** aus `/var/local/emhttp/flashbackup.ini`, Schlüssel `activated`. Ein Wert außer leer, `0`, `false`, `no` und `off`: `active`; Datei oder Wert fehlt: `inactive`; nicht lesbar: `unknown`.
- **Anzeige:** ein gelber Hinweis in der Kachel von Groß, solange das Merkmal besteht (Hinweis `target_connect_plugin`; bei aktivem Flash Backup zusätzlich `target_flash_backup`). Nur feste Texte; TwinRaid liest keine Daten von Connect (Konto, Servername).
- Auf Klein prüft der Wächter `source-guard.sh` dieselben Merkmale (ab v0.5.0, siehe 3.6); die Kachel Klein zeigt dann `source_connect_plugin` bzw. `source_flash_backup`. Dort ist der Hinweis besonders wichtig, weil der Key „sync“ auf dem Boot-Medium von Klein liegt.

**HTTPS der WebGUI von Groß:** TwinRaid überträgt nichts mehr über die WebGUI von Groß; übrig bleibt die TCP-Prüfung, ob der Port offen ist. Ob Unraid dort HTTPS nutzt, spielt keine Rolle. `webgui.scheme` und `webgui.port` bleiben gültig und bestimmen nur den Port der Online-Prüfung (Standard 80 bei http, 443 bei https). Der bestätigte Fingerabdruck des Zertifikats und die Warnung „unverschlüsselt“ entfallen.

**Umstieg von v0.4.2:** `twinraid-init` löscht `target-api-key` und `target-api-cert.sha256` einmal und schreibt einen Hinweis ins Log, den Key auch in Unraid zu löschen (*Settings → Management Access → API Keys*). Die Schritte stehen in `docs/EINRICHTUNG.md`.

**Ursachen bei Fehlern** (siehe 4.1): Server offline, Verbindung wird aufgebaut (`watch_connecting`), Sitzung unterbrochen (`watch_interrupted`), SSH-Key „status“ fehlt (`ssh_key_missing`), Hostkey nicht bestätigt (`ssh_host_untrusted`), Hostkey geändert (`ssh_host_key_mismatch`), Key abgelehnt (`ssh_auth_failed`), SSH nicht erreichbar (`ssh_unreachable`), guard.sh lehnt ab (`ssh_denied`), guard.sh zu alt (`guard_outdated`), sonstiger SSH-Fehler (`ssh_failed`), VM nicht eingetragen (`vm_not_configured`), VM nicht gefunden (`vm_not_found`), VM-Dienst nicht bereit (`vm_service_unavailable`).

### 3.6 Wächter auf Klein (`source-guard.sh`, ab v0.5.0, Entscheidungen E7, E13, E14, E16–E18)

Der Wächter ist ein Bash-Skript, das als root auf dem Host von Klein läuft, nur mit den Bordmitteln von Unraid 7.3.3. Im Repo liegen `host-scripts/source-guard.sh` und `host-scripts/source-guard.conf.example`. TwinRaid gibt ihm nur Aufträge; was erlaubt ist, entscheidet der Wächter nach seiner eigenen Konfiguration auf Klein.

- **v0.5.0:** Container anhalten und wieder starten, Status, Merkmale von Unraid Connect (wie 3.5), lesender ZFS-Teil (Datasets vorhanden, eigenes Dataset von TwinRaid, freier Platz, Kompression). TwinRaid zeigt nur an.
- **v0.5.1:** Snapshots, Prüfen, Senden, Aufbewahrung auf Klein, `init-key` und `trust-target`.

**Ablage und Austausch**

| Pfad auf Klein | Inhalt | im Container | schreibt |
|---|---|---|---|
| `/boot/config/twinraid/source-guard.sh` | Skript | – | Nutzer (Kopie aus dem Repo) |
| `/boot/config/twinraid/source-guard.conf` | Erlaubnisliste der Container, Zeiten, Datasets, Ziel, Grenzen; keine Secrets | – | Nutzer |
| `/boot/config/twinraid/ssh-sync`, `ssh-sync.pub` | Key „sync“ (root, `0600`), erzeugt mit `source-guard.sh init-key` (ab v0.5.1) | – | Wächter |
| `/boot/config/twinraid/target-known-hosts` | bestätigter Hostkey von Groß (`source-guard.sh trust-target`, ab v0.5.1) | – | Wächter |
| `/run/twinraid-source-guard/in/` | eigenes tmpfs, 64 KiB, `0700` | – | Wächter, einmal beim ersten Start |
| `/run/twinraid-source-guard/in/request` | Auftragsdatei, 256 Byte | `/srv/worker/source-guard/request`, rw, einzelne Datei | nur TwinRaid |
| `/run/twinraid-source-guard/state/` | `status.json` | `/srv/worker/source-guard/state`, ro | nur Wächter |
| `/run/twinraid-source-guard/private/` | Instanz, letzte Auftragsnummer, offener Lauf und Job, Sperrdatei; nach einem Neustart von Klein leer | – | nur Wächter |

- **Eine Datei statt eines Ordners:** Der Container kann nur den Inhalt der Auftragsdatei ändern, aber keine Namen, Symlinks, FIFOs oder Unterordner anlegen; `rename` und `unlink` scheitern am Mount-Punkt mit `EBUSY`. Der Wächter liest nur diesen festen Pfad und prüft trotzdem bei jedem Lesen: reguläre Datei, kein Symlink, Besitzer root, ein Hardlink, höchstens 256 Byte. Er schreibt dort nie.
- **Eigenes Mini-tmpfs:** Über die Auftragsdatei kann der Container höchstens 64 KiB Arbeitsspeicher auf Klein belegen. `/run` statt appdata: kein Warten auf das Array, kein FUSE.
- **Antworten getrennt:** `state/` ist read-only eingebunden; TwinRaid prüft in `/proc/self/mountinfo`, dass es wirklich read-only ist, sonst gilt der Wächter als falsch eingerichtet (`misconfigured`).
- **Schreiben an Ort und Stelle:** Der Worker schreibt die Datei direkt; jede Zeile endet mit einer Prüfsumme, damit ein halb gelesener Auftrag nicht gilt.
- **Der Key „sync“ liegt nur auf Klein**, nie in TwinRaid. Er liegt auf dem Boot-Medium von Klein; die Freigabe „flash“ muss den Export „No“ haben, und Unraid Connect sollte nicht installiert sein (Hinweis in der Kachel Klein, siehe 3.5).

**Aufträge**

```
TRSG1 <Nr> begin <Prüfsumme>
TRSG1 <Nr> stop <Container> <Prüfsumme>
TRSG1 <Nr> ping <Prüfsumme>
TRSG1 <Nr> snapshot <Prüfsumme>
TRSG1 <Nr> finish <Prüfsumme>
TRSG1 <Nr> prepare <Prüfsumme>
TRSG1 <Nr> confirm <Prüfsumme>
TRSG1 <Nr> send <Prüfsumme>
TRSG1 <Nr> close <Prüfsumme>
```

- Genau eine ASCII-Zeile, mit Leerzeichen auf 255 Zeichen aufgefüllt, plus Zeilenende; ein einziger regulärer Ausdruck prüft sie, alles andere ergibt `rejected_format`.
- `<Nr>`: 1 bis 999 999 999, größer als die zuletzt angenommene. `<Container>`: Zeichen wie bei Docker, `[A-Za-z0-9][A-Za-z0-9_.-]{0,127}`; der Name dient nur zum Nachschlagen, an Docker geht die selbst ermittelte ID. `<Prüfsumme>`: die ersten 16 Hex-Zeichen von SHA-256 über den Text davor.
- Höchstens ein Auftrag pro Sekunde. Abgelehnte Aufträge stehen höchstens einmal pro Minute im Syslog, nie mit Inhalt.
- `stop` ist der einzige Auftrag mit einem Wert. Datasets, Ziel, Optionen und Snapshot-Namen kann TwinRaid nicht wählen.
- `snapshot`, `prepare`, `confirm`, `send` und `close` nimmt der Wächter ab v0.5.1 an; in v0.5.0 lehnt er sie mit `not_supported` ab.

**Regeln**

| Auftrag | nur, wenn … | Wirkung |
|---|---|---|
| `begin` | kein Lauf und kein Job offen (sonst `run_open` bzw. `job_open`); seit dem Ende des letzten Laufs mindestens `min_run_interval_minutes` (Standard 30, 10–1440), sonst `too_soon` | öffnet einen Lauf (Fenster für angehaltene Container) mit Höchstzeit und Herzschlag-Frist |
| `stop X` | Lauf offen; X steht in `allow`, nicht in `deny` und ist nicht TwinRaid selbst; der Container existiert | läuft X: ID merken, `docker stop --time <stop_timeout> -- <ID>`, Ergebnis prüfen; läuft X nicht: `not_running`, nicht gemerkt |
| `ping` | Lauf offen | verlängert die Herzschlag-Frist, nie die Höchstzeit |
| `snapshot` | kein Job offen; seit dem letzten Snapshot mindestens 30 min; Platzreserve auf `mainpool` und höchstens 50 eigene Snapshots je Dataset | ein Aufruf `zfs snapshot` für alle Datasets der Liste (ein Pool, also atomar), Name `twinraid-<UTC-Zeit>`, Eigenschaft `twinraid:created-by=source-guard`; öffnet einen Job. Sonst `snapshot_low_space`, `snapshot_limit` bzw. `too_soon`; scheitert `zfs snapshot`, `snapshot_failed` |
| `finish` | Lauf offen | startet in umgekehrter Reihenfolge die gemerkten Container, wenn dieselbe ID noch existiert und nicht läuft; schließt den Lauf |
| `prepare` | Job mit neuem Snapshot, Lauf geschlossen | im Kindprozess (unten): `guard.sh sync check` (Zustand der Spiegel, Basis-GUID, Platz, Empfang, Klon, Eigenschaften), je Dataset `zfs diff` gegen die Basis (nur gezählt: gelöscht, geändert, neu, umbenannt), `referenced` neu gegen Basis, Größe mit `zfs send -nvP`. Ergebnis im Job |
| `confirm` | `prepare` meldet mehr Löschungen als `max_deletions` oder `referenced` unter 50 % der Basis | gibt genau diesen Job frei |
| `send` | `prepare` fertig, Grenzen eingehalten oder freigegeben, Ziel bereit | im Kindprozess je Dataset `zfs send -L -c -e -v -P [-i <Basis>] @neu` an `ssh … receive <Name>`, beim ersten Mal (Spiegel laut `check` `absent`) vollständig; danach neue Basis mit Hold, Lesezeichen, alter Hold frei (`-P` ändert nur die Fortschrittszeilen, nicht den Strom) |
| `close` | Job offen | schließt den Job; eine laufende Übertragung bricht ab (auf Groß scheitert der Empfang, dort ändert sich nichts). Der Snapshot bleibt und läuft nach 7 Tagen ab |
| Totmannschalter | Herzschlag-Frist oder Höchstzeit des Laufs überschritten | wie `finish`, Grund `deadman_heartbeat` bzw. `deadman_max`, Warnung im Syslog |
| Job-Frist | Job ohne `prepare` oder `close` 30 min nach dem Snapshot, `prepare` länger als 30 min, Job ohne `send` oder `close` 30 min nach dem Ende von `prepare` (Warten auf Freigabe), Übertragung länger als 12 h | Wächter schließt den Job selbst (`job_timeout`) |

- **Ablauf eines Syncs:** `begin`, `stop` je Container, `snapshot`, `finish`; danach `prepare`, falls nötig `confirm`, dann `send` bei laufenden Containern. Mit `stop_containers: false` entfallen `begin`, `stop` und `finish`; der Snapshot ist dann absturzkonsistent. „Nur Probelauf“ ist `snapshot`, `prepare`, `close`.
- **Pause zwischen zwei Läufen:** Ohne sie könnte ein übernommener TwinRaid die freigegebenen Container mit aufeinanderfolgenden Läufen fast dauerhaft unten halten (je Lauf höchstens `max_stop_minutes`). Die Pause gilt nach jedem Lauf, gleich wie er endete. Der Status nennt den frühesten nächsten Lauf (`run_allowed_at`). Root auf Klein kann die Pause mit `source-guard.sh reset-run-interval` aufheben, etwa für die Tests in `docs/EINRICHTUNG.md`; TwinRaid kann das nicht.
- **Fristen nach Laufzeit:** Höchstzeit, Herzschlag-Frist und Pause zwischen Läufen misst der Wächter mit der Laufzeit des Systems (`/proc/uptime`), nicht mit der Uhrzeit; ein Sprung der Uhr, etwa durch NTP, verschiebt sie nicht. `private/` übersteht einen Neustart von Klein ohnehin nicht. Zeitpunkte im Status sind Uhrzeiten.
- **Start nur für selbst Angehaltenes:** Es gibt keinen Start-Auftrag. Ein inzwischen neu angelegter Container (andere ID) wird nicht gestartet und als `changed` gemeldet.
- **Selbstschutz:** Nie angehalten wird ein Container, der Pfade unter `/run/twinraid-source-guard` einbindet (auch in der Schreibweise `/var/run/twinraid-source-guard`, auf Unraid ein Link auf `/run`), dessen Image `ghcr.io/mantua1/twinraid` oder lokal `twinraid` heißt oder der das Label `org.opencontainers.image.source=https://github.com/mantua1/twinraid` trägt; steht so einer in `allow`, ist das ein Konfigurationsfehler. TwinRaid schickt für die eigene ID (aus `/proc/self/mountinfo`, siehe 10.1) zusätzlich nie `stop`.
- **Nie gesendet:** `system` und `domains` lehnt der Wächter fest ab, auch wenn sie in der Liste stehen; gesendet wird nie rekursiv (`-R`) und nie mit Eigenschaften (`-p`).
- **Docker nur mit fester Argumentliste:** `docker inspect` immer mit `--type container` und festem `--format`, `docker stop -t <stop_timeout>` und `docker start` nur mit der selbst ermittelten ID, jeweils mit `--` vor dem Namen bzw. der ID. Liefert Docker zu einem Namen einen Container mit anderem Namen (etwa über einen ID-Präfix), gilt er als nicht gefunden.

**Ergebnisse** (Feld `last.result`, feste Liste):

- für jeden Auftrag: `rejected_format` (Format, Länge oder Prüfsumme falsch; `last_seq` bleibt), `not_supported` (Auftrag erst ab v0.5.1), `seq_exhausted`, `config_invalid`, `misconfigured`, `docker_unavailable`;
- `begin`: `opened`, `run_open`, `job_open` (ab v0.5.1), `too_soon`; `ping`: `extended`, `no_run`; `finish`: `finished`, `no_run`;
- `stop`: `stopped`, `not_running`, `not_allowed` (nicht in `allow` oder in `deny`), `denied_self`, `not_found`, `stop_failed`, `no_run`;
- je Container beim Ende eines Laufs (`last_run`): `started`, `already_running`, `changed`, `gone`, `start_failed`; Grund des Endes: `finish`, `deadman_heartbeat`, `deadman_max`, `stop_command`. Ist Docker beim Ende nicht erreichbar, bleibt der Lauf offen, und der Wächter versucht es weiter.

**Snapshots auf Klein (ab v0.5.1, Entscheidung E25b)**

- **Basis:** der neueste eigene Snapshot je Dataset, dessen GUID zum neuesten Snapshot des Spiegels auf Groß passt. Der Wächter hält ihn mit `zfs hold twinraid-base` fest; nach einem erfolgreichen Senden bekommt die neue Basis zuerst den Hold, dann wird der alte freigegeben. Dazu legt er je Senden ein Lesezeichen `#twinraid-<Zeit>` an; fehlt der Basis-Snapshot doch einmal, sendet er inkrementell vom Lesezeichen.
- **Aufbewahrung:** Eigene Snapshots (Namensanfang und Eigenschaft) älter als `snapshot_keep_days` löscht der Wächter nach jedem erfolgreichen Senden und einmal täglich; nie die Basis, nie einen mit Hold oder Klon, nie mit `-r`, nie einen Namen ohne `@`. Fremde Snapshots fasst er nie an.
- **Statt vorzeitig zu löschen** lehnt der Wächter neue Snapshots ab: Platzreserve `snapshot_reserve_percent` von `mainpool`, höchstens 50 je Dataset, Mindestabstand 30 min. Ein übernommener TwinRaid kann so höchstens weitere Läufe blockieren, nichts löschen.
- **Nutzen:** Einzelne Dateien der letzten Tage liegen direkt auf Klein unter `/mnt/mainpool/<Share>/.zfs/snapshot/…`.
- **Kompression (Entscheidung E25c):** Der Nutzer stellt sie in Unraid in den Einstellungen des Pools ein („Compression on“); alle Datasets erben sie. Unraid setzt den Wert der obersten Ebene bei jedem Start auf diese Einstellung zurück. `on` und `lz4` gelten als gleichwertig; der Status meldet den Wert je Dataset.

**Job, Prüfen und Senden (ab v0.5.1, festgelegt am 20.10.2026)**

- **Kindprozess:** `prepare` und `send` können lange dauern (`zfs diff` über große Datasets, eine Übertragung über Stunden). Sie laufen deshalb in einem Kindprozess des Wächters in einer eigenen Sitzung (`setsid`); die Hauptschleife nimmt weiter Aufträge an (`ping`, `close`) und schreibt alle 5 s den Status mit dem Fortschritt. Beendet wird der Kindprozess immer mit allen Prozessen seiner Sitzung (aus `/proc/<PID>/stat`), weil `timeout` für `zfs` und `ssh` eigene Prozessgruppen anlegt; ein Signal an die Prozessgruppe erreichte sie nicht. Beide Aufträge antworten sofort (`preparing` bzw. `sending`), das Ergebnis steht im Job. Endet der Wächter (Neustart, `stop`, Signal), beendet er den Kindprozess vorher; ein Job in `preparing` oder `sending` gilt danach als gescheitert (`interrupted`). Ein Kindprozess, dessen Wächter fehlt, beendet sich selbst.
- **Job:** höchstens einer; `snapshot` öffnet ihn, seine Kennung (`job.id`) ist die Nummer dieses Auftrags. Zustände: `snapshot` (Snapshot angelegt), `preparing`, `prepared`, `awaiting_confirm`, `sending`, `done`, `failed`, `closed`; offen sind die ersten fünf. Ergebnis (`job.result`, sobald der Job nicht mehr offen ist): `ok`, `closed`, `interrupted`, `job_timeout`, `prepare_failed`, `send_failed`, `target_refused`, `target_modified`.
- **Grund** (`job.reason`, nur bei `send_failed` und `target_refused`): bei `target_refused` der Grund von guard.sh aus 3.7 mit `_` statt `-` (`name_denied`, `receive_busy`, `mirror_root_missing`, `receiver_id_in_use`, `target_exists`, `delegation_mismatch`, `props_mismatch`, `props_not_applied`, `clone_active`, `space_low`, `too_soon`, `received_properties`, `mount_mismatch`), jeder andere `refused_other`; bei `send_failed` ein Code von SSH (`key_missing`, `host_untrusted`, `host_key_mismatch`, `auth_failed`, `unreachable`, `denied`, `ssh_failed`), `base_missing` (Basis auf Klein nicht mehr da), `not_ready` (Dataset ohne Basis und nicht als erster Strom), `self_dataset_missing` (appdata, siehe unten) oder `send_error` (`zfs send` scheiterte auf Klein).
- **Fristen** (nach Laufzeit): `prepare` höchstens 30 min; danach ohne `send` oder `close` höchstens 30 min; eine Übertragung höchstens 12 h. Sonst schließt der Wächter den Job (`job_timeout`) und beendet einen Kindprozess. TwinRaid wartet auf `preparing` → `prepared` oder `awaiting_confirm` höchstens 30 min und auf `sending` → `done` höchstens 12 h; den Fortschritt zeigt es dabei an (4.4).
- **`snapshot`:** angenommen nur, wenn kein Job offen ist (`job_open`), ZFS bereit ist (`zfs_unavailable`), die Liste `dataset` nicht leer ist (`no_datasets`) und jedes Dataset existiert (`dataset_missing`), dazu die Grenzen aus der Tabelle oben (`too_soon`, `snapshot_low_space`, `snapshot_limit`). Scheitert `zfs snapshot`, ist das Ergebnis `snapshot_failed` und es entsteht kein Job. Der Mindestabstand von 30 min gilt nach Laufzeit seit dem letzten Snapshot dieses Systemstarts. Ein Aufruf `zfs snapshot -o twinraid:created-by=source-guard -- mainpool/<a>@<Name> mainpool/<b>@<Name> …`, Name `twinraid-<JJJJMMTT>T<hhmmss>Z` in UTC. Ergebnis `created`. Ohne `prepare` oder `close` schließt der Wächter den Job nach 30 min (`job_timeout`).
- **`prepare`:** nur im Zustand `snapshot` (sonst `no_job` bzw. `wrong_state`) und ohne offenen Lauf (`run_open`). Der Kindprozess fragt zuerst `guard.sh sync check` (3.7); das Ergebnis steht unter `target.check`. Danach je Dataset:
  - **Basis:** Spiegel `absent`: vollständiger Strom, keine Basis. Spiegel `mirror`: der eigene Snapshot, dessen GUID die neueste des Spiegels ist, sonst das eigene Lesezeichen mit dieser GUID, sonst `base_missing`. `foreign`: `target_exists`. Keine Zeile in `mirror.conf`: `not_in_mirror_conf`. Ohne Antwort von Groß bleibt die Basis offen. Diese Codes stehen als `problem` beim Dataset.
  - **appdata (E18):** Steht `appdata` im Job, prüft der Kindprozess das eigene Dataset von TwinRaid wie der Status (`docker inspect`, 4.5). Ist es nicht `found`, bekommt appdata das Problem `self_dataset_missing`; beim Senden prüft er es vor appdata noch einmal. Sonst lägen Konfiguration und Secrets von TwinRaid im Strom.
  - **Zählungen:** `zfs diff -H <Basis> <neu>` nur gezählt nach dem ersten Zeichen (`-`, `M`, `+`, `R`), nur mit einem Snapshot als Basis; mit einem Lesezeichen oder ohne Basis bleiben sie leer. Dazu `referenced` von Basis und neuem Snapshot und die Größe aus `zfs send -nvP -L -c -e [-i <Basis>] <neu>`.
  - **Freigabe nötig** (`awaiting_confirm`, mit `confirm_reasons`): Summe der Löschungen über `max_deletions` (`deletions`), ein Dataset mit `referenced` unter 50 % seiner Basis (`shrunk`) oder ein inkrementeller Strom vom Lesezeichen, der sich nicht zählen lässt (`no_diff`). Sonst `prepared`.
  - **Bereit zum Senden** (`send_ready`, sonst `blockers` aus einer festen Liste): `check` beantwortet (`target_unreachable`), `root=ok` (`mirror_root_missing`), `conf=ok` (`mirror_conf_invalid`), `deleg=ok` (`delegation_mismatch`), kein Empfang (`receive_busy`), keine Klone (`clone_active`), Platz für die Größe plus Reserve (`space_low`), jedes Dataset mit Basis oder als erster Strom (`base_missing`, `target_exists`, `not_in_mirror_conf`), appdata nur mit eigenem Dataset von TwinRaid (`self_dataset_missing`) und jeder vorhandene Spiegel mit `props=ok` (`props_mismatch`). Probleme auf Groß lassen `prepare` nicht scheitern, damit der Probelauf alles zeigt; nur ein Fehler auf Klein ergibt `failed` mit `prepare_failed`.
- **`confirm`:** nur in `awaiting_confirm`; dann `prepared` mit `confirmed: true` (`confirmed`, sonst `not_needed` oder `no_job`).
- **`send`:** nur in `prepared` und mit `send_ready` (sonst `wrong_state` bzw. `not_ready`). Der Kindprozess sendet die Datasets nacheinander; der Fortschritt kommt aus den Zeilen von `zfs send -v -P` (Bytes je Sekunde). Nach jedem erfolgreichen Dataset: Hold `twinraid-base` an den neuen Snapshot, Lesezeichen `#<Name des Snapshots>`, Hold der alten Basis frei. Scheitert ein Dataset, endet der Job mit dem Ergebnis aus der Antwort von guard.sh (Exit-Code 3: `target_refused` bzw. `target_modified`, sonst `send_failed`); die übrigen bekommen `skipped`, schon gesendete behalten ihre neue Basis. Ergebnis je Dataset (`datasets[].result`): `ok`, `skipped`, `base_missing`, `not_ready`, `send_failed`, `target_refused`, `target_modified`. Nach einem erfolgreichen Job: Aufbewahrung auf Klein.
- **`close`:** jeder offene Job (sonst `no_job`); ein Kindprozess wird beendet, auf Groß scheitert dann der Empfang. Ergebnis `closed`.
- **Ergebnisse der neuen Aufträge** (`last.result`): `snapshot`: `created`, `job_open`, `too_soon`, `snapshot_low_space`, `snapshot_limit`, `snapshot_failed`, `zfs_unavailable`, `no_datasets`, `dataset_missing`; `prepare`: `preparing`, `no_job`, `wrong_state`, `run_open`; `confirm`: `confirmed`, `not_needed`, `no_job`; `send`: `sending`, `no_job`, `wrong_state`, `not_ready`; `close`: `closed`, `no_job`.
- **SSH (Key „sync“):** feste Argumentliste `-F /dev/null -i <Key> -o BatchMode=yes -o IdentitiesOnly=yes -o StrictHostKeyChecking=yes -o UserKnownHostsFile=/boot/config/twinraid/target-known-hosts -o GlobalKnownHostsFile=/dev/null -o HostKeyAlgorithms=ssh-ed25519 -o ConnectTimeout=5 -o ServerAliveInterval=5 -o ServerAliveCountMax=3 -p <target_port> -l root -- <target_host> <Anfrage>`. Den Key kopiert der Wächter vor jeder Verbindung nach `private/` (tmpfs, `0600`), weil `ssh` einen Key mit zu weiten Rechten ablehnt und `/boot` als FAT keine Dateirechte speichert. Die Antwort von `check` prüft er Zeile für Zeile streng gegen das Format aus 3.7; Meldungen von ssh gibt er nie weiter, nur feste Codes. Ergebnis von `check` (`target.check.result`): `ok`, `target_not_configured` (kein `target_host`), `key_missing`, `host_untrusted` (auch ohne `target-known-hosts`), `host_key_mismatch`, `auth_failed`, `unreachable` (auch nach dem Zeitlimit von 60 s), `denied` (guard.sh lehnt ab, etwa Version 2), `bad_answer` (eine Zeile außerhalb des Formats oder Version unter 3), `ssh_failed`.
- **`init-key`:** legt den Key mit `ssh-keygen -t ed25519 -N '' -C twinraid-sync` an, falls er fehlt, und gibt die Zeile für Groß aus: `restrict,from="<IP von Klein>",command="/bin/bash /boot/config/twinraid/guard.sh sync" ssh-ed25519 … twinraid-sync`. Die IP von Klein ist die Absenderadresse auf dem Weg zu `target_host` (`ip route get`); dafür muss `target_host` eingetragen sein. `--new` ersetzt den Key.
- **`trust-target`:** holt den ed25519-Hostkey von Groß (`ssh-keyscan -t ed25519 -p <target_port>`), zeigt seinen SHA-256-Fingerabdruck zum Vergleich mit `ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub` auf Groß und speichert ihn nach `yes` (oder mit `--fingerprint SHA256:…`) in `target-known-hosts`; danach ein Test mit `check`.
- **Protokoll 2:** Der Status hat ab v0.5.1 das Protokoll 2 (neue Felder, siehe unten); TwinRaid ab 0.5.1 nimmt nur Protokoll 2 an und meldet bei Version 1 `protocol_mismatch` mit dem Hinweis, source-guard.sh Version 2 zu kopieren. Die Aufträge bleiben `TRSG1`.
- **Neue Felder im Status** (Zeiten in Sekunden seit 1970, GUIDs als Text aus höchstens 20 Ziffern, `null` für unbekannt):

  ```
  "job": null | {"id":<Nr>,"state":"…","result":"…"|null,"reason":"…"|null,
    "snapshot":"twinraid-…","created_at":<Zeit>,"deadline":<Zeit>|null,"confirmed":<bool>,
    "confirm_reasons":["deletions"|"shrunk"|"no_diff",…],"send_ready":<bool>|null,"blockers":["…",…],
    "datasets":[{"name":"…","mode":"full"|"incremental"|null,"base_guid":"…"|null,
      "base_type":"snapshot"|"bookmark"|null,"problem":"…"|null,"deleted":<n>|null,"modified":<n>|null,
      "added":<n>|null,"renamed":<n>|null,"referenced":<n>|null,"base_referenced":<n>|null,
      "size":<n>|null,"sent":<n>|null,"result":"…"|null}]}
  "target": {"configured":<bool>,"port":<n>,"key":<bool>,"known_host":<bool>,
    "check": null | {"at":<Zeit>,"result":"ok"|"…","summary":{…}|null,"mirrors":[{…},…]}}
  "zfs": {…, "datasets":[{…, "snapshots":<n>|null,"oldest_snapshot_at":<Zeit>|null,
    "base":{"guid":"…","created_at":<Zeit>}|null,"bookmark":<bool>|null}]}
  ```

  `summary` und `mirrors` übernehmen die Felder der Zeilen `summary` und `mirror` aus `check` (3.7) mit denselben Namen; Zahlen als Zahlen, GUIDs als Text, `-` und `?` als `null` (was davon gilt, zeigen `state` und `conf`).

**Konfiguration auf Klein**

```
# /boot/config/twinraid/source-guard.conf
allow twinraid-testcontainer       # Container, die angehalten werden dürfen
# deny <Name>                      # zusätzlich nie anhalten
max_stop_minutes 20                # Höchstzeit eines Laufs (5–120), E16
heartbeat_timeout_seconds 120      # Frist ohne ping (30–600), E16
stop_timeout_seconds 60            # docker stop vor SIGKILL (10–600), E16
min_run_interval_minutes 30        # Pause zwischen zwei Läufen (10–1440), E16
dataset twinraidtest               # mainpool/<Name> wird gespiegelt (ab v0.5.1)
max_deletions 1000                 # Löschgrenze je Job (Summe über die Datasets)
target_host 192.168.10.11          # Unraid Groß (Beispielwert)
target_port 22
snapshot_keep_days 7
snapshot_reserve_percent 15
```

Unbekannte oder fehlerhafte Zeilen: Der Wächter startet nicht, nennt den Grund im Syslog und meldet `config_invalid`. Änderungen gelten nach `source-guard.sh restart`. Der Pool ist fest `mainpool`; die Datasets liegen direkt darunter (`mainpool/<Name>`), ihre Namen folgen den Regeln für Share-Namen (6.3).

**Status für TwinRaid**

`state/status.json` wird alle 5 Sekunden und nach jedem Auftrag atomar neu geschrieben, höchstens 64 KiB, mit strengem Schema in TwinRaid (unbekannte Felder sind ein Fehler):

- **Allgemein:** `protocol`, `version`, `heartbeat_at`, `error`, `instance`, `started_at`, `last_seq`, `last` (Nummer, Auftrag, Ergebnis, Zeit), `host` (Connect installiert, Flash Backup).
- **Container:** `limits`, `allowed` (Name, ID, Zustand), offener `run` (Fristen, angehaltene Container), `last_run`.
- **ZFS:** je Dataset Basis (Zeit, GUID), Anzahl und Alter der eigenen Snapshots, Lesezeichen vorhanden, `referenced`, `compression`; eigenes Dataset von TwinRaid gefunden; freier Platz auf `mainpool`.
- **Job (ab v0.5.1):** Zustand (`snapshot`, `preparing`, `prepared`, `awaiting_confirm`, `sending`, `done`, `failed`, `closed`), Zählungen aus `zfs diff`, Größe, gesendete Bytes je Dataset, Ergebnis; das genaue Format steht in „Job, Prüfen und Senden“.
- **Ziel (ab v0.5.1):** Ergebnis des letzten `guard.sh sync check` (3.7): je Spiegel Zustand, letzter Snapshot (Zeit, GUID), Anzahl, ältester, Eigenschaften in Ordnung; freier Platz, Empfang läuft, Klon vorhanden.
- Container-Namen sind Text von Klein. TwinRaid bereinigt sie wie die VM-Namen (3.5), liefert sie nur über geschützte Routen aus und loggt sie nie.

**Aktiv** heißt: Herzschlag höchstens 30 Sekunden alt, Protokoll 1 (ab v0.5.1 Protokoll 2, siehe „Job, Prüfen und Senden“), kein `error`. `error` ist `config_invalid`, `misconfigured`, `request_not_a_file`, `state_lost` oder `docker_unavailable`, dazu `error_detail` aus einer festen Liste (etwa `request_is_directory` oder `allow_contains_twinraid`). `zfs_unavailable` steht im ZFS-Teil (`zfs.state`) und sperrt nur Snapshot und Senden (ab v0.5.1). Bei `config_invalid`, `misconfigured` und `request_not_a_file` führt der Wächter keinen Auftrag aus. Die Kachel Klein zeigt „Wächter: aktiv · 1 Container freigegeben“ mit dem kleinen Zusatz „Status wird alle 5 s geprüft.“, sonst „nicht aktiv seit hh:mm“ oder den Fehler, auch `zfs_unavailable` (ab v0.5.1; in v0.5.0 mit dem laufenden Alter des Herzschlags).

**Lesen in TwinRaid (ab v0.5.0)**

- Der Worker liest den Status alle 5 Sekunden. Vorher prüft er in `/proc/self/mountinfo` die beiden Einbindungen unter `/srv/worker/source-guard` (es zählt jeweils der oberste Eintrag) und mit `lstat` die Auftragsdatei.
- `status.json` liest er mit `O_NOFOLLOW` und `O_NONBLOCK`, nur als reguläre Datei bis 64 KiB. Doppelte Schlüssel, unbekannte Felder und Werte außerhalb der festen Listen sind ein Fehler. Das Alter des Herzschlags rechnet der Worker selbst aus; Container und Host teilen sich die Uhr.
- Zustand in TwinRaid (`source_guard.state`):

| Zustand | wann | Kachel Klein |
|---|---|---|
| `active` | Herzschlag höchstens 30 s alt, Protokoll 2 (bis v0.5.0: 1), kein `error` | „Wächter: aktiv · 1 Container freigegeben“, darunter klein „Status wird alle 5 s geprüft.“; das genaue Alter des Herzschlags steht nur unter Einstellungen → Wächter (ab v0.5.1) |
| `not_mounted` | keiner der beiden Pfade eingebunden | neutral „Wächter nicht eingerichtet“ mit Verweis auf `docs/EINRICHTUNG.md`; kein Fehler |
| `inactive` | `status.json` fehlt oder Herzschlag älter als 30 s | rot: „Wächter: nicht aktiv seit hh:mm“, mit Datum, wenn nicht heute (ohne Zeit, wenn es noch nie einen Status gab) |
| `error` | der Wächter meldet `error` (Liste oben) | rot: der Fehler mit `error_detail`, bei `config_invalid` mit der Zeilennummer |
| `misconfigured` | nur ein Pfad eingebunden (`state_not_mounted`, `request_not_mounted`), `state` beschreibbar (`state_writable`), Auftragsdatei read-only (`request_read_only`), ein Verzeichnis (`request_is_directory`) oder keine reguläre Datei (`request_not_regular`) | rot; bei `request_is_directory` „Wächter starten, dann TwinRaid neu starten.“ |
| `invalid_status` | `status.json` keine reguläre Datei, größer als 64 KiB, kein gültiges JSON, Schema verletzt, anderes Protokoll (`protocol_mismatch`) oder Herzschlag mehr als 30 s in der Zukunft | rot |

- **Nur aus einem frischen Status** (Herzschlag höchstens 30 s alt, auch bei `error`) kommen die Hinweise zu Unraid Connect (`source_connect_plugin`, `source_flash_backup`), die freigegebenen Container, der offene und der letzte Lauf, `run_allowed_at` und der ZFS-Teil. Von einem veralteten Status bleibt nur der Zeitpunkt des Herzschlags.
- **Kachel Klein außerdem:** ein offener Lauf als „N Container angehalten, spätestens wieder an um hh:mm“ (`max_deadline`); nach einem Lauf mit `start_failed`, `changed` oder `gone` eine Warnung mit den Namen dieser Container; eine ZFS-Zeile mit Datasets und freiem Platz. Gelb nur bei fehlendem Dataset, bei `compression=off` (`on`, `lz4` und jedes andere Verfahren gelten als in Ordnung, E25c) und bei `zfs_unavailable`. Der Hinweis zum eigenen Dataset von TwinRaid (`missing`, `outside`, `no_container`) erscheint nur, wenn `appdata` in der Dataset-Liste des Wächters steht; sonst hätte er bis Test Z4 keine Bedeutung.
- **Kachel Sync:** „Nächster Sync frühestens um hh:mm“ aus `run_allowed_at`, auf die volle Minute aufgerundet, damit ein Versuch zur angezeigten Zeit nicht an `too_soon` scheitert; ab v0.5.2 zugleich der Grund am gesperrten Button „Jetzt synchronisieren“ (`source_guard_too_soon`, 4.2).
- **Einstellungen:** Abschnitt „Wächter auf Klein“, nur lesend: Version, Grenzen, ob ein Ziel eingetragen ist (mit Port), freigegebene Container mit Zustand, Datasets mit `referenced` und Kompression, Anzahl abgelehnter Aufträge.
- **Eigene Container-ID:** aus `/proc/self/mountinfo` (10.1), genau eine ID, sonst unbekannt. In v0.5.0 dient sie nur der Gegenprüfung: Steht sie in `allowed`, zeigt die Kachel Klein den Hinweis `source_guard_allows_self` (der Wächter meldet das selbst als `allow_contains_twinraid`). Ab v0.5.1 schickt TwinRaid für sie nie `stop`.
- Im Container-Log stehen nur Zustandswechsel als feste Codes, nie Container- oder Dataset-Namen.

**Auftragsnummer und Neustarts**

- **Nur der Wächter führt die Nummer** (in `private/`, im Status als `last_seq` mit `instance`). TwinRaid speichert keine eigene. Ein Auftrag: Status lesen, Wächter aktiv, `n = last_seq + 1`, schreiben, auf `last.seq = n` warten (10 s, bei `stop` `stop_timeout` + 15 s). `prepare` und `send` antworten sofort; danach wartet TwinRaid auf den Zustand des Jobs (Fristen in „Job, Prüfen und Senden“). Ändert sich `instance` oder läuft die Frist ab, gilt der Auftrag als gescheitert; im Lauf folgt trotzdem `finish`.
- **Annahme:** Format oder Prüfsumme falsch: verworfen, `last_seq` bleibt. Nummer kleiner oder gleich `last_seq`: ignoriert. Größer: zuerst `last_seq` speichern, dann prüfen und ausführen; Lücken sind erlaubt. Obergrenze 999 999 999 (`seq_exhausted`).

| Fall | Wächter | TwinRaid | Container und Job |
|---|---|---|---|
| Klein startet neu (`/run` leer) | startet aus der go-Datei vor Docker; neue `instance`, `last_seq = 0`; Auftragsdatei leer angelegt; eigene Snapshots, Holds und Lesezeichen bleiben (sie liegen im Pool) | liest `last_seq = 0`, der nächste Auftrag hat die Nummer 1 | ein unterbrochener Lauf ist verloren; Unraid startet Container mit Autostart; ein Job ist weg, die Übertragung abgebrochen, Groß unverändert |
| Wächter startet neu, Klein läuft | liest `private/`: `instance`, `last_seq`, offener Lauf und Job bleiben; eine abgebrochene Übertragung gilt als gescheitert | sieht die Lücke im Herzschlag | gemerkte Container bleiben gemerkt; `finish` oder der Totmannschalter starten sie |
| `private/` fehlt, Auftragsdatei besteht (nur nach Eingriff von Hand) | neue `instance`; den vorgefundenen Auftrag führt er nie aus (`discarded_at_start`), Fehler `state_lost` | erkennt die neue `instance`; ein laufender Abschnitt endet mit `source_guard_lost` | das Dashboard nennt die Container, die TwinRaid anhalten ließ, und erklärt den Start in Unraid |
| TwinRaid startet neu | unverändert; eine laufende Übertragung läuft weiter | macht bei `last_seq + 1` weiter; offener Lauf: sofort `finish`; offener Job ohne Übertragung: `close` | Container werden gestartet; eine Übertragung endet normal und steht danach im Status |
| Wächter läuft nicht | – | Status „nicht aktiv“; Sync und Probelauf gesperrt (`source_guard_inactive`) | – |

**Start und Überwachung (Entscheidung E14)**

- Eine Zeile in `/boot/config/go` vor `emhttp`: `/bin/bash /boot/config/twinraid/source-guard.sh start`. `start` legt `/run/twinraid-source-guard` und das tmpfs an, nur wenn sie fehlen, und startet mit `setsid` eine Überwachungsschleife (Neustart nach 5 s, höchstens 10-mal in 10 min). `flock` verhindert eine zweite Instanz. Ab v0.5.1 wartet `start` höchstens 5 s, bis die Überwachungsschleife ihre PID geschrieben hat, damit `status` direkt danach stimmt; sonst meldet es, dass der Wächter sich noch nicht meldet (Exit-Code 0). Die Sperre hält nur die Überwachungsschleife; der Wächter selbst beendet sich, sobald seine Überwachungsschleife fehlt, und eine zweite Sperre verhindert, dass zwei Wächter gleichzeitig Aufträge lesen.
- Das tmpfs für `in/` hängt der Wächter mit `size=64k,mode=0700,nosuid,nodev,noexec` ein. Die Auftragsdatei legt er nur an, wenn sie fehlt, und ersetzt sie nie, weil TwinRaid genau diese Datei eingebunden hat.
- **Ordner statt Datei:** Startet TwinRaid, bevor der Wächter lief, legt Docker an der Stelle von `in/request` ein Verzeichnis an. Der Wächter erkennt das beim Start, legt kein tmpfs darüber und meldet `request_not_a_file` (`error_detail` `request_is_directory`); er nimmt dann keine Aufträge an. Ein TwinRaid-Container, der schon einmal mit der Datei lief, startet dann gar nicht: Docker lehnt es ab, ein Verzeichnis auf die vorhandene Datei einzuhängen (`not a directory: Are you trying to mount a directory onto a file`, in Unraid „Bad parameter“; beobachtet beim Test von v0.5.0). Nur ein neu angelegter Container startet; dann erkennt TwinRaid das Verzeichnis und meldet: „Wächter starten, dann TwinRaid neu starten.“ Behoben wird es in beiden Fällen von Hand: TwinRaid stoppen (falls es läuft), Verzeichnis entfernen, Wächter starten, die Auftragsdatei prüfen, TwinRaid starten, notfalls über Edit → Apply neu anlegen (`docs/EINRICHTUNG.md`).
- **Docker noch nicht bereit** (Start aus der go-Datei vor Docker): Der Wächter meldet `docker_unavailable`, schreibt weiter seinen Status und versucht es alle 5 Sekunden erneut, ohne abzubrechen.
- Der Wächter wartet auf `mainpool`, bevor er ZFS-Aufträge annimmt; bis dahin meldet er `zfs_unavailable`.
- Weitere Befehle: `stop` (vorher `finish`, falls ein Lauf offen ist; eine Übertragung wird abgebrochen), `restart`, `status`, `check-config`, `init-key`, `trust-target`, `selftest <Container>`, `selftest-deadman <Container>`, `reset-run-interval`. Ein Signal von außen, etwa beim Herunterfahren von Klein, beendet den Wächter ohne `finish`; angehaltene Container startet Unraid dann beim nächsten Start mit Autostart.
- `status.json` schreibt der Wächter atomar (temporäre Datei im selben Ordner, dann `mv`). Er erzeugt JSON nur aus geprüften Werten: Zahlen, Codes aus festen Listen und Namen, die er unmittelbar vor der Ausgabe noch einmal gegen die Zeichenregel von Docker bzw. von Share-Namen prüft.
- Syslog mit der Kennung `twinraid-source-guard`: Start, Stopp, jeder angenommene Auftrag (bei `stop` mit Container-Name), Snapshots, Senden, Löschen, Totmann-Fälle, Fehler; nie Inhalte der Auftragsdatei.
- **SSH-Client im Wächter (ab v0.5.1):** feste Argumentliste wie im Worker (5.6) mit eigenem `UserKnownHostsFile=/boot/config/twinraid/target-known-hosts` und dem Key „sync“.

### 3.7 Spiegel auf Groß (`guard.sh sync`, ab v0.5.1, Entscheidungen E25 und E26)

**Pool und Ort**

- Auf Groß liegen die Spiegel im ZFS-Pool `mainpool` unter `mainpool/twinraid-mirror/<Name>`. `mainpool/twinraid-mirror` ist eine Unraid-Share, die der Nutzer anlegt (Primärspeicher `mainpool`, kein Sekundärspeicher, Export „No“).
- guard.sh empfängt nur dorthin: Pool und Wurzel stehen fest im Code, `<Name>` ist ein einzelner Bestandteil aus `mirror.conf`. Andere Pools auf Groß, etwa der Pool für VMs und `system`, erreicht guard.sh nie, auch nicht mit einer veränderten `mirror.conf`. Fehlt `mainpool/twinraid-mirror` oder ist es kein Dateisystem im Pool `mainpool`, lehnt guard.sh ab (`mirror-root-missing`).
- guard.sh legt Datasets nur durch `zfs receive` an, liest keine Share-Konfigurationen, importiert keine Pools und listet nur unterhalb von `mainpool/twinraid-mirror`.

**`mirror.conf`** (`/boot/config/twinraid/mirror.conf` auf Groß, kein Secret, Besitzer root, kein Symlink): Zeilen `mirror <Name>` mit den Zeichen von Share-Namen (6.3), höchstens 20; `system`, `domains` und `zfs-check` (das Test-Dataset von zfs-check.sh, 5.6) in jeder Schreibweise immer abgelehnt; dazu optional und je höchstens einmal `keep_days` (30–3650, Standard 30; so gilt „frühestens nach 30 Tagen“ aus 4.5 bei jedem Wert), `reserve_percent` (5–50, Standard 15) und `min_interval_minutes` (10–1440, Standard 10). Aufbau wie `source-guard.conf`: Schlüssel und Wert durch Leerzeichen getrennt, `#` beginnt einen Kommentar, höchstens 200 Zeilen. Eine fehlerhafte Zeile macht die ganze Datei ungültig (Syslog mit der Nummer der Zeile); dann lehnt guard.sh jeden Empfang mit `name-denied` ab, und `check` meldet `conf=invalid` (fehlt die Datei: `conf=missing`). Status und Herunterfahren stört das nicht.

**Empfang**

```
zfs receive -u \
  -o readonly=on -o exec=off -o setuid=off -o devices=off \
  -o primarycache=metadata -o secondarycache=all -o compression=lz4 -o atime=off \
  -o xattr=sa -o acltype=posix -o recordsize=128K -o snapdir=hidden \
  -o sync=standard -o dedup=off -o copies=1 -o checksum=on \
  -o quota=none -o refquota=none -o reservation=none -o refreservation=none \
  -o mountpoint=/mnt/mainpool/twinraid-mirror/<Name> -o canmount=on \
  -o sharesmb=off -o sharenfs=off -o twinraid:mirror=1 \
  mainpool/twinraid-mirror/<Name>
```

- **Fest:** kein `-F`, kein `-c` (Heilen), kein `-s` (Fortsetzen), kein `-d` oder `-e`, kein `-x`. Der Name kommt nur aus `mirror.conf`. Aus dem Strom übernimmt Groß nur die Daten und die Eigenschaften, die beim Anlegen festgelegt werden (`normalization`, `utf8only`, `casesensitivity`).
- **Alle Eigenschaften mit `-o`, kein `-x` (ergänzt am 18.10.2026):** Z1 in der CI zeigte, dass `-x` bei einem Strom mit Eigenschaften (`zfs send -p`) nicht zuverlässig wirkt: `quota` und `mountpoint` kamen trotz `-x` aus dem Strom, `-o` wirkte immer. Deshalb setzt guard.sh jede Eigenschaft, die für Schutz, Platz oder Einhängen zählt, mit `-o` auf einen festen Wert, bei **jedem** Empfang und direkt am Spiegel (Quelle `local`), nicht über Vererbung von `twinraid-mirror` oder `mainpool`. Z1 hat bestätigt, dass jedes `-o` auch beim inkrementellen Empfang angenommen wird. Auf OpenZFS 2.4.4 (Unraid 7.3.3) wirkte `-x` im Test Z1 beim vollständigen und beim inkrementellen Strom; die festen `-o` bleiben trotzdem, weil sie auf allen Versionen wirken. Weiterer Grund für feste Werte am Spiegel: Unraid setzt Eigenschaften der obersten Ebene eines Pools bei jedem Start auf die Pool-Einstellungen zurück (beobachtet für `compression`; ein `zfs set` am Terminal hält dort nur bis zum Neustart). `compression=on` und `lz4` gelten als gleichwertig (Unraid stellt „Compression on“ ein, OpenZFS nimmt dafür lz4). Eine abweichende feste Eigenschaft meldet `check` (`props=mismatch`); ab v0.5.1 empfängt guard.sh dann nicht mehr, sondern sperrt (`props-not-applied`, siehe unten).
- **Prüfung vor dem Einhängen (ergänzt am 18.10.2026):** guard.sh empfängt mit `-u` und hängt dabei nicht ein. Danach darf keine Eigenschaft des Spiegels, seiner Snapshots und seiner Kinder die Quelle `received` haben (ein Aufruf `zfs get -H -r -t filesystem,volume,snapshot -s received all`), und der Spiegel darf keine Kinder haben (ergänzt am 19.10.2026). Kinder bringt nur ein Strom von `zfs send -R` mit: Er enthält immer Eigenschaften, und das feste `-o canmount=on` gilt nur für den Spiegel, weil `canmount` nicht vererbt wird; ein Kind könnte sonst beim nächsten Start von Groß eingehängt werden. Es gilt eine Positivliste, keine Sperrliste: Ausnahmen stehen fest im Code, jede mit Begründung, und die Liste ist leer, solange keine nötig ist; ein Strom des Wächters (ohne `-p`) enthält keine Eigenschaften.
  - Trifft das nicht zu, bleibt der Spiegel ausgehängt; ein schon eingehängter Spiegel (inkrementeller Empfang) wird ausgehängt. guard.sh setzt dann `canmount=noauto` am Spiegel und an jedem Kind (ebenfalls ausgehängt), damit sie auch nach einem Neustart von Groß ausgehängt bleiben, endet mit Exit-Code 3 (`refused received-properties`) und schreibt ins Syslog; `check` meldet `props=mismatch`, TwinRaid zeigt das an. Weitere Empfänge in diesen Spiegel lehnt guard.sh ab (`props-mismatch`), solange `canmount` nicht `on` ist, eine Eigenschaft die Quelle `received` hat oder der Spiegel Kinder hat; aufheben kann das nur das Zurücksetzen von Hand (unten; Kinder löscht root dort einzeln wie verdächtige Snapshots).
  - **Feste Werte angekommen (ab v0.5.1, Pflicht):** Danach prüft guard.sh nach jedem Empfang, dass alle festen Werte der Empfangszeile mit Quelle `local` am Spiegel stehen, auch wenn es als root empfängt. Grund: Fehlt dem Empfang ein Recht (E27), übergeht `zfs receive` das betreffende `-o` mit Exit-Code 0 und nur einer Warnung („cannot receive … property“); der Wert hat dann die Quelle `default` oder `inherited`, nicht `received`. Die Prüfung auf `received` bemerkte das nicht, und guard.sh hinge einen Spiegel ohne `readonly=on` und `exec=off` ein. Fehlt ein fester Wert, sperrt guard.sh wie bei `received-properties` (aushängen, `canmount=noauto`), endet mit Exit-Code 3 (`refused props-not-applied`) und schreibt ins Syslog; weitere Empfänge lehnt es mit `props-mismatch` ab. Z1b und die Tests von guard.sh enthalten einen Fall mit absichtlich fehlendem Recht, der genau so gesperrt werden muss.
  - **Auch vor jedem Empfang** prüft guard.sh am vorhandenen Spiegel, dass alle festen Werte mit Quelle `local` stehen; sonst sperrt es ebenso und empfängt nicht (`props-not-applied`). Grund: Das delegierte Recht `readonly` erlaubt grundsätzlich auch `zfs set readonly=off`, ebenso die übrigen Rechte für die Eigenschaften (in Z1b scheiterte `readonly=off` nur, weil der Spiegel eingehängt war). Die festen Werte schützt deshalb diese Prüfung, nicht die Delegation.
  - Sonst hängt guard.sh den Spiegel ein, nur mit `zfs mount -- mainpool/twinraid-mirror/<Name>`, und prüft mit `findmnt`, dass er genau unter `/mnt/mainpool/twinraid-mirror/<Name>` hängt. Sonst hängt es ihn wieder aus und lehnt ab (`mount-mismatch`).
  - Grund: Ohne `-u` könnte ein Strom von einem übernommenen Klein den Spiegel beim Empfang an beliebiger Stelle auf Groß einhängen oder das Einhängen über Eigenschaften beeinflussen, die guard.sh nicht kennt. Mit `-u` und der Prüfung davor wirken solche Werte nie.
- **Zurücksetzen nach `props-mismatch` (ergänzt am 18.10.2026):** Kurzfassung; die Schritte mit fertigen Befehlen stehen ab v0.5.1 in `docs/WIEDERHERSTELLUNG.md` (Abschnitt „Spiegel gesperrt“).
  1. **Ursache auf Klein zuerst klären.** Ein Strom mit Eigenschaften kommt nie vom Wächter; praktisch heißt das, Klein ist übernommen. Erst wenn Klein wieder vertrauenswürdig ist, geht es auf Groß weiter.
  2. **Auf Groß den Fund ansehen:** die Eigenschaften mit Quelle `received` am Spiegel und an seinen Snapshots und die Einträge im Syslog von Groß.
  3. **Verdächtige Snapshots von Hand löschen,** alle nach dem letzten guten, einzeln; dann `zfs rollback` auf den letzten guten (er ist dann der neueste, also ohne `-r`). Gibt es keinen guten (der erste Empfang war schon der verdächtige), wird der Spiegel von Hand gelöscht; der nächste Sync sendet dann vollständig.
  4. **Werte prüfen:** `zfs inherit` nur für genau die Eigenschaften, die `zfs get -s received` am Spiegel und an seinen Snapshots gemeldet hat, nie für die festen Eigenschaften der Empfangszeile (sonst fiele etwa `readonly=on` weg). Danach muss `check` alle festen Werte mit Quelle `local` melden und `props=ok` (bis auf `canmount`).
  5. **Sperre aufheben:** `zfs set canmount=on mainpool/twinraid-mirror/<Name>`. Das kann nur root auf Groß; TwinRaid und Klein haben dafür keinen Befehl. guard.sh hängt den Spiegel beim nächsten Empfang nach der Prüfung selbst ein.
  6. **Danach** läuft der Sync inkrementell von der Basis weiter, also vom letzten guten Snapshot.
  - **Nach `props-not-applied`** liegt die Ursache meist in der Delegation (E27), nicht auf Klein: Delegation nach der Anleitung korrigieren (`check` meldet `deleg=ok`), die festen Werte von Hand mit `zfs set` setzen (fertiger Befehl in `docs/WIEDERHERSTELLUNG.md`), bis `check` `props=ok` meldet (bis auf `canmount`). Snapshots müssen nicht gelöscht werden, solange keine Eigenschaft die Quelle `received` hat; dann weiter mit Schritt 5.
- **Empfang ohne root (ab v0.5.1, Entscheidung E27; Test Z1b in der CI und auf Groß mit OpenZFS 2.4.4 bestanden):** guard.sh startet `zfs receive` mit `setpriv` als eigenen Benutzer mit der festen UID und GID 64000, ohne Eintrag in `/etc/passwd` und `/etc/group`, ohne Gruppen, mit leerem Bounding-Set und `no_new_privs`. Nie verwendet wird `nobody` (99) oder eine andere UID, unter der auf Unraid Samba oder Container laufen. Der Kernel lässt diesen Benutzer dann nur anhängen, unabhängig von guard.sh: kein `-F`, kein Rollback, kein Löschen, nichts außerhalb von `twinraid-mirror`.
  - **Delegation:** Der Nutzer richtet sie einmal von Hand ein, mit einem fertigen Befehl aus `docs/EINRICHTUNG.md` und einem Prüfbefehl mit der erwarteten Ausgabe. Es sind genau die 28 Rechte, die Z1b in der CI und auf Groß einzeln als nötig gemessen hat, nur für UID 64000 und nur als „Local+Descendent“ auf `mainpool/twinraid-mirror`: `acltype`, `atime`, `canmount`, `checksum`, `compression`, `copies`, `create`, `dedup`, `devices`, `exec`, `mount`, `mountpoint`, `primarycache`, `quota`, `readonly`, `receive:append`, `recordsize`, `refquota`, `refreservation`, `reservation`, `secondarycache`, `setuid`, `sharenfs`, `sharesmb`, `snapdir`, `sync`, `userprop`, `xattr`. Ohne die Rechte für die Eigenschaften übergeht `zfs receive` jedes `-o` mit Exit-Code 0. Die Delegation steht im Pool und übersteht Neustarts. guard.sh ändert sie nie.
  - **Prüfung der Delegation** (vor jedem Empfang und in `check`): guard.sh liest `zfs allow` für `mainpool/twinraid-mirror` und für den Spiegel; die Ausgabe zeigt auch, was auf übergeordneten Datasets vergeben ist. Erlaubt ist genau diese Ausgabe aus drei Zeilen: die Kopfzeile `---- Permissions on mainpool/twinraid-mirror ` (mit `-` auf 70 Zeichen aufgefüllt), die Zeile `Local+Descendent permissions:` und eine Zeile aus Tabulator, `user (unknown: 64000)`, Leerzeichen und den Rechten mit Kommas, deren Rechte als Menge den 28 festen gleichen (`zfs allow` sortiert sie alphabetisch, nicht in unserer Reihenfolge). Das Format stammt aus `print_fs_perms` in `zfs_main.c` (OpenZFS 2.4); zfs-check.sh zeigt die Zeile gefiltert an, ohne Tabulator und Klammern (korrigiert am 19.10.2026). Alles andere ist ein Fehler: weitere Benutzer, Gruppen oder `everyone`, Permission-Sets (`@…`), „Create time permissions“, „Local permissions“ oder „Descendent permissions“, Rechte auf `mainpool` (die sonst vererbt würden) oder auf einem Spiegel. Dann endet guard.sh mit Exit-Code 3 (`refused delegation-mismatch`) und empfängt nicht; einen Rückfall auf root gibt es nicht.
  - **UID und GID frei:** Hat 64000 einen Eintrag in `/etc/passwd` oder `/etc/group` (geprüft am Zahlenfeld, nicht per Textsuche), lehnt guard.sh ab (`refused receiver-id-in-use`): Dann wäre der Benutzer nicht mehr nur der von TwinRaid, und `zfs allow` zeigte einen Namen statt `(unknown: 64000)`. So muss der Parser nur diese eine Form kennen. Geprüft werden in `/etc/passwd` die Felder UID und GID, in `/etc/group` das Feld GID.
  - **Als root bleiben:** die Vorprüfungen, die Prüfung vor dem Einhängen und die Sperre mit `canmount=noauto`, `zfs mount` mit `findmnt`, `twinraid:received` und die Aufbewahrung.
  - **Meldungen von `zfs receive`:** Die erste Fehlerzeile (etwa „cannot receive readonly property … permission denied“) schreibt guard.sh ins Syslog von Groß, beschränkt auf druckbare ASCII-Zeichen und 200 Zeichen. An Klein geht nur die feste Zeile `twinraid-guard: refused <Grund>`; in den Status des Wächters und nach TwinRaid gelangt der Text nie.
- **Gescheiterter Empfang (ergänzt am 19.10.2026):** Endet `zfs receive` nicht mit 0, laufen die Prüfungen nach dem Empfang trotzdem, denn ein Strom mit mehreren Snapshots (`-I`) kann einen Teil davon schon angelegt haben; ein Fund sperrt wie oben. Danach endet guard.sh mit Exit-Code 1 ohne Text, die Aufbewahrung entfällt. Steht in der Meldung von zfs das feste Muster `has been modified` (Ziel seit dem letzten Snapshot verändert), endet es mit `refused target-modified` (TwinRaid: `target_modified`). Dieselbe Meldung gibt OpenZFS, wenn die Basis des Stroms ein älterer Snapshot des Spiegels ist: Seit dieser Basis hat sich das Ziel durch die neueren Snapshots verändert (`recv_begin_check_existing_impl`, in der CI geprüft). Eine Basis, die der Spiegel gar nicht hat, ergibt Exit-Code 1. Während des Empfangs ignoriert guard.sh `SIGHUP`, damit die Prüfungen auch nach einer abgebrochenen Verbindung laufen; eine geschlossene Verbindung beendet `zfs receive` über das Ende des Stroms.
- **Vorher geprüft** (sonst Exit-Code 3 mit festem Grund): Name in `mirror.conf` (`name-denied`), Wurzel vorhanden (`mirror-root-missing`), an der Stelle des Spiegels kein fremdes Dataset (`target-exists`, E26), keine Eigenschaft aus einem früheren Strom, keine Kinder und `canmount` ist `on`, also keine Sperre (`props-mismatch`), freier Platz über der Reserve (`space-low`: `available` von `mainpool/twinraid-mirror` liegt unter `reserve_percent` Prozent von `used` plus `available` des Pools), Mindestabstand je Spiegel (`too-soon`: `twinraid:received` mit Quelle `local` am neuesten Snapshot ist jünger als `min_interval_minutes`; ein Wert in der Zukunft, etwa nach einer zurückgestellten Uhr, sperrt nicht), kein Klon eines Snapshots der Spiegel aus `mirror.conf` (`clone-active`), kein anderer Empfang (`receive-busy`: `flock` auf `/var/run/twinraid-guard-receive.lock`; die Sperre hält auch `zfs receive` selbst, nur lesend geöffnet), alle festen Werte am vorhandenen Spiegel (`props-not-applied`), UID und GID 64000 frei (`receiver-id-in-use`) und die Delegation (`delegation-mismatch`). `poweroff` nimmt dieselbe Sperre (`sync-running`) und gibt sie nicht wieder frei: `/sbin/poweroff` ist auf Unraid ein Link auf `halt` von sysvinit (auf Groß geprüft), das nur an init übergibt und zurückkehrt, während Unraid noch herunterfährt. Deshalb erbt ein Hintergrundprozess (`sleep 900`, ohne offene Ein- und Ausgabe, ignoriert `SIGHUP`) den Deskriptor und hält die Sperre, bis Unraid ihn beim Herunterfahren beendet oder die 15 Minuten ablaufen, die auch TwinRaid auf das Ausschalten wartet (ergänzt am 20.10.2026).
- **Nachher:** Eigenschaft `twinraid:received=<Zeit von Groß in Sekunden seit 1970>` an jedem Snapshot, den es vor dem Empfang noch nicht gab (Quelle `local`), dann, nur nach einem erfolgreichen Empfang, die Aufbewahrung für alle Spiegel aus `mirror.conf`, die in Ordnung sind (ein gesperrter Spiegel bleibt unberührt): Gelöscht werden nur Snapshots der Spiegel aus `mirror.conf`, älter als `keep_days` laut `twinraid:received` **mit Quelle `local`** und zugleich laut `creation`, nie die drei neuesten, nie einer mit Klon, nie mit `-r`, nie ein Name ohne `@`. Einen Snapshot ohne `twinraid:received` mit Quelle `local` löscht guard.sh nie. Klein und TwinRaid haben dafür keinen Befehl. **Zwei Uhren (ergänzt am 20.10.2026):** `twinraid:received` kommt von der Uhr von Groß, `creation` von der Uhr von Klein (ein Empfang übernimmt sie). Erst wenn beide älter als `keep_days` sind, wird gelöscht; eine nach vorn gesprungene Uhr auf einem der beiden Server allein löscht so nichts vorzeitig.
- **Nur Quelle `local` zählt:** Ein Strom mit `-p` oder `-I` kann `twinraid:mirror` (E26) und `twinraid:received` mitbringen, dann mit Quelle `received`. Beide zählen nur mit Quelle `local`. Sonst könnte ein gefälschtes altes `twinraid:received` an einem Zwischen-Snapshot die Aufbewahrung zu frühem Löschen bringen.
- **`check`:** gibt `twinraid-guard 3 sync ok` aus, danach je Zeile aus `mirror.conf` eine Zeile mit Zustand (`absent`, `mirror`, `foreign`), neuestem Snapshot (Zeit, GUID), Anzahl und ältestem Snapshot und `props=ok|mismatch` (`mismatch`, wenn eine feste Eigenschaft abweicht, eine Eigenschaft die Quelle `received` hat oder der Spiegel Kinder hat), dazu eine Zeile mit freiem Platz, laufendem Empfang, vorhandenen Klonen und `deleg=ok|mismatch|id-in-use`. Festes Format, nur ASCII; `check` verändert nichts. Format (festgelegt am 19.10.2026; Zeiten in Sekunden seit 1970, Größen in Byte, `-` für „gibt es nicht“, `?` für „nicht lesbar“):

  ```
  twinraid-guard 3 sync ok
  mirror <Name> state=<absent|mirror|foreign> props=<ok|mismatch|-> count=<Anzahl> newest_guid=<GUID|-> newest_created=<Zeit|-> newest_received=<Zeit|-> oldest_created=<Zeit|->
  summary root=<ok|missing> conf=<ok|missing|invalid> deleg=<ok|mismatch|id-in-use> receive=<idle|busy> clones=<Anzahl|?> available=<Byte|?> size=<Byte|?> reserve_percent=<Zahl|-> keep_days=<Zahl|-> min_interval_minutes=<Zahl|->
  ```

  - `newest_created` und `oldest_created` sind die Eigenschaft `creation` der Snapshots; ein Empfang übernimmt sie von Klein. `newest_received` ist `twinraid:received` mit Quelle `local` am neuesten Snapshot. Die GUID ist dieselbe wie auf Klein; daran erkennt der Wächter die Basis (3.6). Snapshot-Namen gibt `check` nicht aus.
  - `deleg` prüft die Wurzel und jeden vorhandenen Spiegel; `clones` zählt die Klone von Snapshots der Spiegel aus `mirror.conf`; `available` gilt für `mainpool/twinraid-mirror`, `size` ist `used` plus `available` des Pools. `summary` steht immer am Ende; fehlt die Zeile, war die Antwort unvollständig.

| Fall ohne `-F` | Verhalten | Beleg |
|---|---|---|
| Snapshots löschen, die der Sender nicht hat | geht nur mit `-F` | zfs-receive(8) |
| Ziel seit dem letzten Snapshot verändert | Abbruch (`ETXTBSY`) | `dmu_recv.c`, `recv_begin_check_existing_impl` |
| Vollständiger Strom in ein vorhandenes Dataset | Abbruch (`EEXIST`) | ebenda |
| Snapshot mit vorhandenem Namen | Abbruch (`EEXIST`) | ebenda |
| Inkrementeller Strom | nur wenn der neueste Snapshot des Ziels die Basis ist; hängt einen Snapshot an | zfs-receive(8) |
| Fremdes Dataset an der Stelle des Spiegels | guard.sh lehnt vor dem Empfang ab (`target-exists`) | E26 |
| Strom mit Eigenschaften (`-p`, `-I`) | Werte für Schutz, Platz und Einhängen wirken nicht (alle mit `-o` fest); jede übrige Eigenschaft aus dem Strom erkennt die Prüfung vor dem Einhängen, der Spiegel bleibt ausgehängt | Z1 |
| Strom mit Kindern (`-R`) | Kinder werden angelegt, aber nicht eingehängt (`-u`); die Prüfung vor dem Einhängen findet sie, Spiegel und Kinder bleiben mit `canmount=noauto` ausgehängt | zfs-receive(8), Tests von guard.sh |
| Recht für ein `-o` fehlt (E27) | `zfs receive` endet mit Exit-Code 0, der Wert wird nicht gesetzt (Quelle `default` oder `inherited`); die Prüfung nach dem Empfang sperrt (`props-not-applied`) | Z1b |

Ein Dataset, das auf Klein gelöscht und neu angelegt wurde, kann den Spiegel deshalb nicht ersetzen. `readonly=on` verhindert zusätzlich, dass schon ein Lesen mit atime das Ziel „verändert“. `receive:append` (zfs-allow(8)) trägt unter Linux als zweite Schranke: Z1 hat auf OpenZFS 2.4.4 gezeigt, dass ein Benutzer ohne root damit inkrementell empfangen kann, `-F` und das Löschen eines Snapshots aber verhindert werden. Geprüft war dort nur ein Empfang ohne die festen `-o`; den Empfang mit der vollen Befehlszeile, vollständig und inkrementell, prüft Z1b (E27).

**Erster Empfang und Datasets, die Unraid selbst anlegt (Entscheidung E26)**

- Unraid legt für Shares Datasets auf oberster Ebene eines Pools an (`<Pool>/<Share>`), auch ohne Zutun, wenn es die Share-Konfiguration schon gibt, und zwar auch auf einem anderen Pool als dem eingetragenen, wenn dieser fehlt. Beobachtet auf Groß: `mainpool/isos` entstand, obwohl in der Konfiguration ein nicht mehr vorhandener Pool steht; vermutlich hat der ISO-Ordner des VM-Managers (`/mnt/user/isos`) es ausgelöst. Die Spiegel liegen eine Ebene tiefer. Z3 prüft nach einem Neustart von Groß und nach Stopp und Start der Pools, dass Unraid auf keinem Pool Datasets mit gespiegelten Namen und unterhalb von `mainpool/twinraid-mirror` nichts anlegt, auch nicht beim Start des VM-Managers.
- **Erster Empfang:** Der Wächter sendet einen vollständigen Strom nur, wenn `check` den Spiegel als `absent` meldet, sonst inkrementell von der Basis. guard.sh nimmt einen Empfang in ein vorhandenes Dataset nur an, wenn es ein eigener Spiegel ist (Eigenschaft `twinraid:mirror=1` mit Quelle `local`, gesetzt beim ersten Empfang); sonst endet es mit `refused target-exists`, bevor `zfs receive` startet. guard.sh löscht nie ein Dataset, das es nicht selbst empfangen hat, und ersetzt nichts. TwinRaid zeigt `target_exists` mit dem Verweis auf `docs/EINRICHTUNG.md`: Dataset ansehen und von Hand entfernen.
- **Normalbetrieb:** Auf Groß gibt es keine Share-Konfigurationen (`/boot/config/shares/<Name>.cfg`) mit den Namen der gespiegelten Shares. Umstellen genügt nicht, weil Unraid auf einen anderen Pool ausweichen kann; der Nutzer löscht sie bei der Einrichtung, nachdem er geprüft hat, dass die Shares leer sind. Kein Pfad in Unraid zeigt auf eine solche Share, insbesondere nicht der ISO-Ordner des VM-Managers. Er kann nach dem ersten Sync von isos auf den Spiegel `/mnt/mainpool/twinraid-mirror/isos/` zeigen; dort sind aber keine Downloads möglich (etwa des VirtIO-ISOs), weil der Spiegel schreibgeschützt ist.
- **Notbetrieb:** Die Klone entstehen auf oberster Ebene (`mainpool/<Share>`), also genau dort, wo Unraid Datasets für Shares anlegt. `docs/WIEDERHERSTELLUNG.md` legt zuerst die Klone an und danach die Shares neu an, mit Primärspeicher `mainpool`; hat Unraid trotzdem schon ein Dataset angelegt, wird es vorher geprüft (keine Dateien, keine Snapshots, keine Kinder) und von Hand entfernt (4.6).

**Angriffsfläche im Kernel:** Den Strom wertet der Kernel von Groß aus. Erreichbar ist der Empfänger nur mit dem Key „sync“ von der Adresse des Hosts Klein, praktisch also nur mit root auf Klein. Gegenmaßnahmen: Updates von Unraid zeitnah auf beiden Servern (gleiche OpenZFS-Version; Unraid 7.3.3 bringt OpenZFS 2.4.4); keine Roh- und keine verschlüsselten Ströme; kein `-c` beim Empfang (CVE-2026-49430 betrifft den Heilmodus); feste Befehlszeile; Empfang höchstens alle 10 min je Spiegel. Gegen root auf Klein mit einer Kernel-Lücke hilft nur eine Kopie, die Klein nicht erreicht; sie ist nicht Teil des Projekts.

**Speicher:** Ein Empfang schreibt den ganzen Strom durch den ARC; die Spiegel haben deshalb `primarycache=metadata`. Die Grenze des ARC trägt der Nutzer in Unraid ein (3.5).

**Grenzen bei einem Angriff**

- **Übernommener TwinRaid-Container:** kann Läufe auslösen (einer zur Zeit, Abstand 30 min im Wächter, je Spiegel ein Empfang je 10 min), Löschungen freigeben wie der Button (ältere Stände bleiben in den Snapshots) und freigegebene Container auf Klein je Lauf bis zu 20 min anhalten, danach frühestens nach 30 min wieder. Er kann nicht: gespiegelte Daten lesen, nach Groß schreiben, Datasets, Ziel oder Optionen wählen, Snapshots auf Klein oder Groß löschen, andere Container anhalten oder starten, Container aktualisieren, pausieren oder ihre Logs lesen.
- **Übernommenes Klein (root):** hat Key und Daten und kann selbst gebaute Ströme an den Empfänger im Kernel schicken. Ältere Snapshots auf Groß kann es nicht löschen (kein `-F`, kein Befehl dafür); mit E27 verhindert das zusätzlich der Kernel, weil der Empfang nur anhängen darf. Gegen ein übernommenes Klein schützen nur diese Snapshots.

---

## 4. Funktionen

### 4.1 Dashboard

Kacheln:

1. **Unraid Klein:** Status. Im Modus `real` immer „Online“ mit der Ursache „TwinRaid läuft auf diesem Server“ (siehe „Prüfung“). Ab v0.5.0 zusätzlich der Wächter (3.6, „Lesen in TwinRaid“): „Wächter: aktiv · 1 Container freigegeben“ mit „Status wird alle 5 s geprüft.“ (ab v0.5.1), sonst „nicht aktiv seit hh:mm“, der Fehler oder neutral „Wächter nicht eingerichtet“, dazu die Hinweise zu Unraid Connect auf Klein, ein offener Lauf und der ZFS-Teil.
2. **Unraid Groß:** Status mit den Buttons „Wecken“ und „Herunterfahren“. Laufen außer der Gaming-VM weitere VMs (jeder Zustand außer „shut off“, wie in 4.2), nennt die Kachel sie dauerhaft: „Weitere laufende VMs: …“ (ab v0.4.4, nur Anzeige, keine Steuerung). Ausgeschaltete VMs erscheinen nicht; ist die Liste unbekannt oder Groß aus, entfällt die Zeile. Ab v0.5.1 erscheint ein gelber Hinweis, wenn auf Groß keine Grenze für den ZFS-Cache eingetragen ist (3.5).
3. **Gaming-VM:** Status mit den Buttons „Starten“ und „Herunterfahren“. Nach einem Timeout zusätzlich „Erzwingen“: Der Button erscheint, sobald ein Herunterfahren läuft oder gescheitert ist, und wird erst `shutdown_timeout_minutes` nach dem letzten Versuch aktiv (siehe 4.2). Bis dahin steht darunter „Erzwingen: möglich ab hh:mm:ss“, auch während das Herunterfahren noch läuft (ab v0.4.4). Ist ein Herunterfahren von Groß an der VM gescheitert, zeitweise zusätzlich „Erzwingen und danach Server herunterfahren“ (ab v0.4.1, siehe 4.2).
4. **Sync:** in v0.5.0 nur „Nächster Sync frühestens um hh:mm“, solange die Pause des Wächters läuft (3.6). Ab v0.5.1: letzter Lauf (Ergebnis, Dauer, Datenmenge), nächster geplanter Lauf (bis Phase 6 „Zeitplan aus (ab Phase 6)“), Buttons „Jetzt synchronisieren“ und „Nur Probelauf“ (in v0.5.1 ist „Jetzt synchronisieren“ sichtbar, aber gesperrt mit „kommt mit v0.5.2“), während eines Laufs, auch eines Probelaufs, „Abbrechen“, Live-Fortschritt (4.4), Zählungen je Share (gelöscht, geändert, neu, umbenannt), Dialog zur Freigabe von Löschungen; ab v0.5.3 die Snapshots beider Seiten.
5. **Verlauf:** Liste der letzten Aktionen und Sync-Läufe (Audit-Log).

Statuswerte: `Online`, `Offline`, `Startet …`, `Fährt herunter …`, `Unbekannt`. Zu jedem Status kann eine **Ursache** aus einer festen Liste kommen (z. B. „Server offline“, „VM-Dienst nicht bereit“, „Key abgelehnt“, „VM pausiert“; siehe 3.5). So ist bei einem Fehler klar unterscheidbar, ob der Server aus ist, der VM-Dienst nicht antwortet oder der Key nicht passt.

- **Prüfung:** Der Worker prüft Groß alle 10 Sekunden per TCP-Check. Die VM kommt aus der Sitzung `vm-watch`, solange Groß online ist (ab v0.4.3, siehe 3.5); jede Änderung löst sofort eine neue Prüfung aus. Ist Groß offline, gilt die VM als aus mit der Ursache „Server offline“. `get_status` liefert sofort den zuletzt geprüften Stand.
- **Klein (ab v0.3.2):** TwinRaid läuft auf Klein. Solange der Worker antwortet, ist Klein also online. Im Modus `real` entfällt deshalb der TCP-Check von Klein; die Kachel zeigt „Online“ mit der festen Ursache „TwinRaid läuft auf diesem Server“. Grund: Ohne „Host access to custom networks“ (siehe 5.1) erreicht der Container seinen eigenen Host nicht, ein TCP-Check würde Klein fälschlich als offline melden.
- **VM-Zustände** (libvirt, ab v0.4.3 über guard.sh): `RUNNING` und `IDLE` → Online; `PAUSED` → Online, Ursache „pausiert“; `PMSUSPENDED` → Online, Ursache „im Ruhezustand“; `SHUTDOWN` → Fährt herunter …; `SHUTOFF` → Offline; `CRASHED` → Offline, Ursache „abgestürzt“; `NOSTATE` und Unbekanntes → Unbekannt. Dazu von guard.sh: `NOT_CONFIGURED` → Unbekannt, Ursache „VM auf Groß nicht eingetragen“; `NOT_FOUND` → Unbekannt, Ursache „VM nicht gefunden“; `LIBVIRT_DOWN` und `UNKNOWN` → Unbekannt, Ursache „VM-Dienst nicht bereit“.
- **Hinweise (ab v0.4.3):** Der Status enthält eine Liste fester Hinweise (`notices`): `target_connect_plugin` und `target_flash_backup` für die Kachel Groß, `vm_name_mismatch` für die VM-Kachel (siehe 3.5); ab v0.5.0 `source_connect_plugin`, `source_flash_backup` und `source_guard_allows_self` für die Kachel Klein (3.6), ab v0.5.1 `target_arc_unlimited` für die Kachel Groß (3.5). Sie ändern weder Zustand noch Verfügbarkeit der Aktionen.
- **Live-Updates:** Die Oberfläche öffnet einen SSE-Datenstrom (`GET /api/events`, geschützt). Ein einziger Abfrage-Task im Web-Prozess fragt den Worker alle 2 Sekunden mit dem vorhandenen Befehl `get_status` und schickt Änderungen an alle offenen Datenströme. Es gibt dafür keinen neuen Worker-Befehl. Höchstens 32 Datenströme gleichzeitig.
- **Kopierjob:** Der Status enthält das Feld `job_running` („Kopierjob läuft“). Ab v0.5.1 kommt es vom echten Job des Wächters (Probelauf, Warten auf Freigabe, Senden), im Simulationsmodus vom simulierten Sync; `twinraid-admin mock-job` entfällt dann. Bis dahin ist es im Modus `real` immer aus und im Simulationsmodus mit `twinraid-admin mock-job on|off` schaltbar. Es steuert die Ausnahme vom Inaktivitäts-Timeout (siehe 5.3).

Destruktive oder störende Aktionen (Herunterfahren, Force-Stop, Sync mit vielen Löschungen) erfordern einen Bestätigungsdialog. Ab Phase 4: Groß herunterfahren, VM herunterfahren und Erzwingen. Ab v0.5.2: Freigabe von Löschungen und Abbrechen eines Syncs. Wecken, VM starten, „Jetzt synchronisieren“ und „Nur Probelauf“ laufen ohne Dialog.

### 4.2 Aktionen und Sperren

| Aktion | Verhalten / Sperre |
|---|---|
| Groß wecken | nur wenn Groß offline ist. Magic Packet senden, dann „Startet …“, bis Groß per TCP erreichbar ist und der VM-Dienst über `vm-watch` antwortet (bis v0.4.2: die API), höchstens `boot_timeout_minutes` |
| Groß herunterfahren | gesperrt, solange ein Sync läuft, solange der Zustand der VM oder die Liste der VMs unbekannt ist und solange außer der Gaming-VM weitere VMs auf Groß laufen (ab v0.4.2, siehe unten). Läuft die Gaming-VM, fragt der Dialog „VM zuerst herunterfahren?“. Bestätigen fährt erst die VM herunter (höchstens `shutdown_timeout_minutes`), danach den Server. Bleibt die VM an, bricht TwinRaid ab, der Server läuft weiter und „Erzwingen“ wird angeboten. Ein sofortiges Herunterfahren an der laufenden VM vorbei gibt es nicht (Entscheidung E10). |
| Gaming-VM starten | ist Groß offline: automatisch wecken, warten bis der VM-Dienst antwortet (`boot_timeout_minutes`), dann VM starten (`vm-start`). Auch bei laufendem Groß wartet der Worker bis zu dieser Zeit auf den VM-Dienst, falls Groß gerade erst gestartet ist |
| Gaming-VM herunterfahren | ACPI-Signal per SSH, dann „Fährt herunter …“, bis die VM aus ist, höchstens `shutdown_timeout_minutes`. Danach Meldung „reagiert nicht“ und „Erzwingen“ |
| Gaming-VM erzwingen | erst `shutdown_timeout_minutes` nach dem letzten erfolgreich gesendeten ACPI-Signal und nur, solange die VM noch läuft; per SSH (`vm-force-stop`, bis v0.4.2 über die API) |
| Erzwingen und danach Server herunterfahren (ab v0.4.1) | nur nach einem Herunterfahren von Groß, das abgebrochen wurde, weil die VM nicht rechtzeitig herunterfuhr; höchstens 10 Minuten lang und nur, solange sich nichts geändert hat (siehe unten) |
| Sync starten (ab v0.5.2) | gesperrt, wenn bereits ein Sync läuft, wenn der Wächter nicht aktiv ist (`source_guard_inactive`), mit `stop_containers: true` solange die Pause des Wächters zwischen zwei Läufen gilt (`source_guard_too_soon` mit der Uhrzeit aus `run_allowed_at`, 3.6), wenn die drei Ebenen nicht zusammenpassen (`levels_mismatch`, siehe 4.3), wenn appdata gespiegelt wird und das eigene Dataset von TwinRaid fehlt (`self_dataset_missing`) und solange auf Groß ein Klon eines Spiegels besteht (`target_clone_active`). Ist Groß offline, wird es automatisch geweckt |
| Nur Probelauf (ab v0.5.1) | Sperren wie bei „Sync starten“; schreibt nichts nach Groß und endet nach der Prüfung (4.3). Ist Groß aus, weckt TwinRaid es nur mit `actions.wake`, sonst ist der Probelauf gesperrt (`target_offline`); danach bleibt Groß an (Herunterfahren nach einem Lauf erst ab Phase 6) |
| Automatisches Herunterfahren nach Sync | nur wenn **der Sync den Server geweckt hat** UND die Gaming-VM nicht läuft UND keine weitere VM auf Groß läuft (Schutz vor weiteren VMs, siehe unten) UND währenddessen keine manuelle Aktion auf Groß stattfand |

**Broadcast-Adresse für Wake-on-LAN (ab Phase 4):** `servers.target.wol_broadcast` wird optional. Fehlt der Wert, verwendet der Worker die Broadcast-Adresse des eigenen Netzes (aus IP und Netzmaske der Schnittstelle), sofern `servers.target.host` in diesem Netz liegt. Liegt Groß in einem anderen Netz, ist die Angabe Pflicht: Ist `actions.wake` freigeschaltet, startet der Container sonst nicht und nennt den Grund; ohne Freischaltung warnt das Startlog nur. So muss der Wert nicht doppelt gepflegt werden. Das Magic Packet (6 × `FF`, dann 16 × die MAC von Groß) geht dreimal an UDP-Port 9.

Alle Aktionen laufen über eine zentrale Sperrlogik im Worker (eine Aktion pro Ressource gleichzeitig).

**Sperrlogik und Abläufe (ab Phase 4):**

- **Ressourcen** sind Groß, die VM und ab v0.5.1 der Sync. Groß wecken belegt Groß, VM herunterfahren und Erzwingen belegen die VM, VM starten, Groß herunterfahren und „Erzwingen und danach Server herunterfahren“ belegen beide. Sync und Probelauf belegen den Sync, für das Wecken zusätzlich Groß. Ist eine Ressource belegt, wird der Befehl abgelehnt; es gibt keine Warteschlange. Während eines Syncs sind „Groß herunterfahren“ und „Erzwingen und danach Server herunterfahren“ gesperrt (`sync_running`); VM-Aktionen bleiben möglich.
- **Verfügbarkeit:** Der Worker berechnet für jede Aktion, ob sie gerade möglich ist, und sonst den Grund aus einer festen Liste: `not_enabled`, `busy`, `sync_running`, `target_online`, `target_offline`, `target_unknown`, `vm_running`, `vm_off`, `vm_unknown`, `force_not_yet`, `not_offered` (ab v0.4.1: kein gültiges Angebot für „Erzwingen und danach Server herunterfahren“), `other_vms_running` (ab v0.4.2: auf Groß laufen weitere VMs), `tour`; ab v0.5.1 für Sync und Probelauf zusätzlich `source_guard_inactive`, `levels_mismatch`, `self_dataset_missing` und `target_clone_active`, ab v0.5.2 für den Sync `source_guard_too_soon` (der Probelauf öffnet keinen Lauf und ist von der Pause nicht betroffen). Er liefert das mit `get_status` aus und prüft bei jedem Befehl erneut. Die Oberfläche deaktiviert Buttons nur nach dieser Angabe und nennt die Gründe, die nicht schon aus dem Status hervorgehen. Für „Erzwingen“ geht `force_not_yet` dem Grund `busy` vor, solange die Wartezeit läuft und Groß und die VM noch als laufend gemeldet werden (ab v0.4.4): Während „VM herunterfahren“ oder „Groß herunterfahren“ die VM belegt, nennt die Oberfläche so die Uhrzeit, ab der Erzwingen möglich ist. Ist die VM schon aus, bleibt es bei `busy` bzw. `vm_off`.
- **Zwischenzustände:** Solange eine Aktion läuft, meldet der Status „Startet …“ bzw. „Fährt herunter …“ und den aktuellen Schritt: `waking`, `waiting_for_vm_service` (bis v0.4.2 `waiting_for_api`), `starting_vm`, `stopping_vm`, `forcing_vm`, `shutting_down`. Während Groß nach dem Wecken bootet, zeigt die VM die Ursache „Wartet auf den Server“ (`target_starting`).
- **Ergebnis:** Je Ressource meldet der Status das letzte Ergebnis aus einer festen Liste (siehe Aktionslog in 5.3). Es bleibt bis zur nächsten Aktion auf dieser Ressource bzw. bis zum Neustart des Workers sichtbar.
- **Meldung und Hinweis (ab v0.4.1):** Die Oberfläche zeigt zu jedem Ergebnis die Meldung als Verlauf. Ein Hinweis, was jetzt zu tun ist, erscheint nur, solange er nach dem aktuellen Status zutrifft: „Erzwingen ist jetzt möglich“ nur, solange Erzwingen angeboten wird; der Verweis auf „Erzwingen und danach Server herunterfahren“ nur, solange das Angebot gilt; „Wake-on-LAN prüfen“ nach `boot_timeout` nur, solange Groß aus ist; der Hinweis nach `shutdown_timeout` nur, solange Groß an ist. Hinweise, die sich nicht prüfen lassen (z. B. guard.sh lehnt ab), bleiben bis zur nächsten Aktion stehen. Eine Ablehnung beim Absenden („eine andere Aktion läuft gerade“) verschwindet mit dem nächsten Live-Status.
- **Erzwingen:** Den Zeitpunkt des letzten ACPI-Signals kennt nur der Worker im Speicher. Ist die VM aus, wird er gelöscht. Nach einem Neustart des Workers muss erneut „Herunterfahren“ gewählt werden.

**Erzwingen und danach Server herunterfahren (ab v0.4.1, Ergänzung zu E10):**

- **Angebot:** Entsteht nur, wenn „Groß herunterfahren“ abbricht, weil die VM nicht innerhalb von `shutdown_timeout_minutes` herunterfuhr (Ergebnis `vm_stop_timeout`). Ein gescheitertes normales „VM herunterfahren“ erzeugt kein Angebot. Das Angebot kennt nur der Worker im Speicher; der Status meldet, bis wann es gilt (`forced_shutdown_until`).
- **Gültigkeit:** höchstens 10 Minuten nach dem Abbruch. Das reicht, um die Meldung zu lesen und nachzusehen, ob jemand die VM gerade benutzt oder Windows Updates installiert; danach kann die Lage eine andere sein, und ein neuer sauberer Versuch erzeugt ein neues Angebot.
- **Verfällt** außerdem, sobald die VM nicht mehr als laufend gemeldet wird, Groß nicht mehr online ist, irgendeine andere Aktion startet (auch das einfache „Erzwingen“), der Worker neu startet, oder mit der Ausführung selbst (einmalig nutzbar).
- **Befehl:** `shutdown_target_force_vm` ohne Parameter. Der Worker nimmt ihn nur an, solange sein eigenes Angebot gilt. Ein übernommener Web-Prozess gewinnt damit nichts, was er nicht schon mit `vm_force_stop` und `shutdown_target` könnte.
- **Ablauf:** VM erzwingen (`vm-force-stop`) und warten, bis sie aus ist (höchstens 30 s); ist sie danach nicht aus, bricht TwinRaid ab und fährt Groß nicht herunter. Dann Groß sauber herunterfahren wie bei „Groß herunterfahren“.
- **Bestätigung:** Der Dialog warnt deutlich, dass Erzwingen für die VM wie Stecker ziehen ist: Nicht gespeicherte Daten gehen verloren, auch laufende Windows-Updates werden unterbrochen.
- **Aktionslog:** `shutdown_target_force_vm` vom Auslöser `dashboard`, darin automatisch zuerst `vm_force_stop` und danach `shutdown_target` (jeweils mit `part_of`).
- **Feste Zeiten:** Prüfabstand während einer Aktion 3 s; Bestätigung nach `vm-start` höchstens 60 s, nach `vm-force-stop` höchstens 30 s; nach `poweroff` höchstens 15 min, bis Groß nicht mehr erreichbar ist.
- **Nach jeder Aktion** prüft der Worker den Status sofort neu, damit die Oberfläche nicht auf die nächste reguläre Prüfung wartet.

**Schutz vor weiteren VMs beim Herunterfahren von Groß (ab v0.4.2, Ergänzung zu E10):**

- Laufen auf Groß außer der Gaming-VM weitere VMs, schaltet TwinRaid Groß nicht aus. Das gilt für „Groß herunterfahren“, für „Erzwingen und danach Server herunterfahren“ und ab Phase 6 für das automatische Herunterfahren nach einem Sync.
- Grundlage ist die VM-Liste der Sitzung `vm-watch` (3.5); es braucht keine weiteren Rechte. Als laufend gilt jeder Zustand außer `SHUTOFF`. Ab v0.4.3 prüft guard.sh zusätzlich selbst unmittelbar vor `poweroff` (3.5); so gilt der Schutz auch gegen einen übernommenen TwinRaid.
- **Anzeige:** Der Button ist gesperrt (`other_vms_running`). Die Begründung nennt den Server und die VMs: „Auf <Servername> laufen noch weitere VMs: <Namen>. Bitte fahre sie zuerst über die Unraid-Oberfläche herunter. Zur Sicherheit schaltet TwinRaid den Server nicht aus, solange andere VMs laufen, damit keine Daten verloren gehen.“
- **Erneute Prüfung im Worker:** unmittelbar vor `poweroff`, also auch nach dem Herunterfahren der Gaming-VM; bei der erzwungenen Variante zusätzlich vor dem Erzwingen, damit die Gaming-VM nicht umsonst hart ausgeschaltet wird. Läuft dann eine weitere VM, bricht der Ablauf mit `other_vms_running` ab; lässt sich die Liste nicht lesen, mit der Ursache aus 3.5 (z. B. `vm_service_unavailable`). Lehnt guard.sh das Ausschalten ab, gilt dasselbe (`vm_running`, `other_vms_running` oder `vm_service_unavailable`). Groß bleibt in allen Fällen an; das Ergebnis steht im Status und im Aktionslog.
- **Liste unbekannt** (z. B. VM-Dienst nicht bereit): Herunterfahren bleibt gesperrt (`vm_unknown`), wie bei unbekanntem Zustand der Gaming-VM.

**Aktionen im Echtbetrieb einzeln freischalten (Entscheidung E9):** In `twinraid.yaml` gibt es den optionalen Abschnitt `actions` mit `wake`, `vm` und `shutdown`, ab v0.5.1 zusätzlich `sync_dry_run` und `sync` (Entscheidung E19), alle standardmäßig aus. Er gilt nur im Modus `real`; im Simulationsmodus sind alle Aktionen verfügbar.

| Aktion | braucht |
|---|---|
| Groß wecken | `wake` |
| Groß herunterfahren | `shutdown`; läuft die VM, zusätzlich `vm` |
| VM starten | `vm`; ist Groß aus, zusätzlich `wake` |
| VM herunterfahren, Erzwingen | `vm` |
| Erzwingen und danach Server herunterfahren | `vm` und `shutdown` |
| Nur Probelauf (ab v0.5.1) | `sync_dry_run` oder `sync`; ist Groß aus, zusätzlich `wake` |
| Sync starten (ab v0.5.2) | `sync`; ist Groß aus, zusätzlich `wake` |

Eine nicht freigeschaltete Aktion lehnt der Worker mit `not_available` ab, die Oberfläche zeigt „nicht freigeschaltet“. Sind alle Aktionen aus, bleibt das Banner „Nur Statusanzeige“. Änderungen wirken nach einem Neustart des Containers. Die Sync-Befehle bleiben bis v0.5.1 (Probelauf) bzw. v0.5.2 (Sync) gesperrt.

### 4.3 Sync-Ablauf (ab v0.5.1, Entscheidung E25)

Den Ablauf steuert der Worker über Aufträge an den Wächter (3.6); TwinRaid selbst liest und schreibt keine Share-Daten.

1. **Vorabprüfung:** Sperren aus 4.2; Wächter aktiv; die **drei Ebenen** passen zusammen: `sync.shares` in `twinraid.yaml`, Zeilen `dataset` in `source-guard.conf` auf Klein und Zeilen `mirror` in `mirror.conf` auf Groß (laut letztem `check`), sonst `levels_mismatch`; eigenes Dataset von TwinRaid gefunden, wenn appdata dabei ist (E18). Ist Groß aus: wecken (braucht `actions.wake`) und bis `boot_timeout_minutes` warten.
2. **Snapshot:** mit `stop_containers: true`: `begin`, `stop` je Kandidat (in der Erlaubnisliste des Wächters, laufend, nicht in `never_stop`, nicht der eigene Container), `ping` alle 15 s, `snapshot`, `finish` (immer, auch nach Fehlern); mit `stop_containers: false` nur `snapshot` (absturzkonsistent). Alle Shares stammen aus demselben Zeitpunkt; die Container sind nur für den Snapshot angehalten.
3. **Prüfen:** `prepare`. Anzeige je Share: gelöscht, geändert, neu, umbenannt, Größe; dazu der Zustand der Spiegel auf Groß. Meldet `check` an der Stelle eines Spiegels ein fremdes Dataset, endet der Lauf mit `target_refused` (Grund `target_exists`, E26). „Nur Probelauf“ endet hier mit `close`.
4. **Löschschutz:** Mehr Löschungen als `max_deletions` (in `source-guard.conf`) oder `referenced` unter 50 % der Basis: Bei einem manuellen Lauf gibt der Nutzer die Löschungen mit `sync_confirm_deletions` frei (Wächter: `confirm`) oder lehnt mit `sync_cancel` ab (`close`); ohne Antwort schließt der Wächter den Job nach 30 min. Geplante Läufe (Phase 6) brechen ab.
5. **Senden:** `send`, je Share inkrementell von der Basis, beim ersten Mal vollständig; die Container laufen dabei.
6. **Abschluss:** neue Basis auf Klein, neuer Snapshot je Spiegel auf Groß; Ergebnis ins Aktionslog und in die Sync-Historie (5.3), Status sofort neu prüfen.
7. **Optional (Phase 6):** Groß herunterfahren (Bedingungen siehe 4.2, einschließlich des Schutzes vor weiteren VMs).

- **Abbruch:** `sync_cancel` schickt `close`. Auf Groß bleibt der alte Stand, der neue Snapshot auf Klein läuft nach `snapshot_keep_days` ab. Wiederaufnahme heißt: neuer Lauf, der das ganze Delta erneut sendet (kein Fortsetzen über ein Resume-Token).
- **Sperren:** Der Sync belegt die Ressource „Sync“, beim Wecken zusätzlich Groß (4.2); guard.sh lehnt `poweroff` während eines Empfangs zusätzlich selbst ab.
- **Sitzungen:** `job_running` (4.1) ist gesetzt, solange ein Job läuft (Probelauf, Warten auf Freigabe, Senden).
- **Delta:** Folgeläufe senden nur die seit der Basis geänderten Blöcke, ohne alle Dateien zu durchsuchen. Der erste Lauf überträgt alles.
- **Vorlagen:** `copy_templates` hat bis v0.5.4 keine Wirkung (4.7).

### 4.4 Fortschritt und Restzeit

- Die Gesamtmenge stammt aus `zfs send -nvP` beim Prüfen: tatsächlich zu sendende Bytes je Share, nicht die Größe der Shares.
- Den Fortschritt meldet der Wächter im Status: gesendete Bytes je Share aus der sekündlichen Ausgabe von `zfs send -v`; der Status wird alle 5 s neu geschrieben.
- **Alle 15 Sekunden** wird die Restzeit neu berechnet: verbleibende Bytes geteilt durch den gleitenden Durchschnitt der Übertragungsrate der letzten 60 Sekunden.
- Anzeige: aktueller Schritt (Snapshot, Prüfen, Warten auf Freigabe, Senden), aktuelle Share, Prozent, GB übertragen / gesamt, MB/s, Restzeit, voraussichtliches Ende (Uhrzeit).

### 4.5 Schutzmechanismen beim Spiegeln

- **Verbotene Shares:** `system` und `domains` können niemals gespiegelt werden. Konfiguration (Validierungsfehler beim Start), Wächter und `mirror.conf` lehnen sie unabhängig voneinander fest ab; gesendet wird nie rekursiv. Grund: Dort liegen docker.img, libvirt.img und die Disks der VMs. Auf Groß liegen VMs und `system` zudem auf einem anderen Pool, den guard.sh nie erreicht (3.7).
- **Leere oder ersetzte Quelle:** Fällt `referenced` eines Datasets unter 50 % der Basis, braucht der Lauf eine Freigabe. Ein auf Klein gelöschtes und neu angelegtes Dataset kann den Spiegel nicht ersetzen, weil Groß einen vollständigen Strom nie in einen vorhandenen Spiegel annimmt.
- **Löschgrenze:** `max_deletions` in `source-guard.conf`: Zeilen „-“ aus `zfs diff`, Summe über alle Datasets, durchgesetzt im Wächter; TwinRaid zeigt den Wert an. Freigegebene Löschungen bleiben in den Snapshots auf Groß erhalten.
- **Versionen statt geschützter Zielpfade:** Jeder Empfang ist ein Snapshot. guard.sh löscht sie frühestens nach 30 Tagen und nie die drei neuesten; auf Klein bleiben die Basis und die Snapshots der letzten 7 Tage. Weder TwinRaid noch Klein haben einen Befehl zum Löschen. `protect_on_target` entfällt.
- **Spiegel schreibgeschützt:** `readonly=on`, `exec=off`, `setuid=off`, `devices=off`, direkt am Spiegel bei jedem Empfang gesetzt (3.7). Eigenschaften aus einem Strom wirken nie: alle wichtigen sind mit `-o` fest, und der Spiegel wird erst nach der Prüfung eingehängt. Ein verändertes Ziel lässt den nächsten Empfang scheitern, statt überschrieben zu werden.
- **Ziel begrenzt:** nur `mainpool/twinraid-mirror/<Name aus mirror.conf>` mit fester Befehlszeile, kein `-F`; ein fremdes Dataset an dieser Stelle wird nie ersetzt oder gelöscht (E26).
- **Ausschlüsse:** nur als eigenes Dataset unterhalb der Share; ein nicht rekursiver Strom enthält keine Kinder. `exclude` entfällt.
- **Templates (ab v0.5.4):** werden ohne Löschen übernommen; eigene Templates auf Groß bleiben erhalten. Der Weg wird vor v0.5.4 festgelegt.
- **Eigener Ordner von TwinRaid (Entscheidung E18):** Konfiguration, Secrets und CA-Schlüssel liegen auf Klein in einem eigenen Dataset `mainpool/appdata/twinraid`; ein nicht rekursiver Strom von appdata enthält es nie. Der Wächter prüft per `docker inspect`, dass `/config` und `/data` von TwinRaid darin liegen; sonst sendet er appdata nicht (`self_dataset_missing`). Der Eintrag `appdata/twinraid` in `exclude` ist nicht mehr nötig.
- **Eigener Container:** TwinRaid wird nie angehalten. Der Wächter erkennt den Container an Image, Label und Einbindungen (3.6), und TwinRaid schickt für die eigene ID nie `stop`. `never_stop` gilt nur für andere Container; der Eintrag `twinraid` entfällt.

### 4.6 Notfall-Wiederherstellung

Die Anleitung für den Ernstfall steht als eigenes, kurzes Dokument in `docs/WIEDERHERSTELLUNG.md` (ab v0.5.3, ohne private Daten, nur „Klein“ und „Groß“); `docs/EINRICHTUNG.md` verweist darauf. TwinRaid hat dafür keine Funktion und schreibt außer den Spiegeln nichts nach Groß. Die frühere Notfall-Info auf Groß (`last-sync.json` und eine Kopie der Anleitung in `appdata/_twinraid/`) entfällt (Entscheidung E25d): Den letzten Stand zeigen die Snapshots selbst.

Inhalt der Anleitung, mit fertigen Befehlen:

- **Letzten Stand finden:** die Snapshots der Spiegel auf Groß mit `twinraid:received` auflisten.
- **Spiegel gesperrt (`props-mismatch`):** die Schritte zum Zurücksetzen aus 3.7, schon ab v0.5.1.
- **Einzelne Dateien zurückholen:** auf Klein aus `/mnt/mainpool/<Share>/.zfs/snapshot/…` (letzte 7 Tage), auf Groß aus dem Spiegel unter `/mnt/mainpool/twinraid-mirror/<Share>` und seinen Snapshots (30 Tage).
- **Notbetrieb starten:** Docker-Dienst auf Groß einschalten (im Normalbetrieb aus). Fehlt dort das Docker-Abbild (`docker.img`), legt Unraid ein neues an; die Images werden dann beim Anlegen der Container neu geladen, dafür braucht Groß Internet. Je Share einen Klon des neuesten Spiegel-Snapshots als `mainpool/<Share>` anlegen, mit ausdrücklich gesetzten Eigenschaften (`readonly=off`, `exec=on`, `setuid=off`, `devices=off`, `primarycache=all`, `compression=lz4`, `atime=off`, `xattr=sa`, `acltype=posix`), weil ein Klon sonst von der obersten Ebene erbt, die Unraid verwaltet. Damit liegen die Daten unter denselben Pfaden `/mnt/mainpool/<Share>` wie auf Klein. Hat Unraid an dieser Stelle schon ein Dataset angelegt, wird es vorher geprüft und entfernt (E26, siehe 3.7). Danach die Shares in Unraid neu anlegen, mit Primärspeicher `mainpool` (im Normalbetrieb gibt es dafür keine Konfiguration, E26), und die Container aus den Vorlagen anlegen (Templates: ab v0.5.4). Der Spiegel selbst bleibt unverändert; solange Klone bestehen, lehnt guard.sh jeden Empfang ab (`clone-active`).
- **setuid und Gerätedateien** bleiben im Klon aus: Images liegen nicht in appdata, und durchgereichte Geräte kommen aus `/dev` des Hosts. Vor dem ersten echten Spiegel von appdata prüft die Anleitung auf Klein mit `find`, ob es solche Dateien gibt; braucht ein Container es doch, wird `setuid` am Klon eingeschaltet.
- **Rückweg nach Klein:** TwinRaid auf Klein stoppen (kein Sync während des Rückwegs), Container auf Groß stoppen, einen Snapshot des Klons anlegen und ihn mit dem eigenen Admin-Zugang (nicht mit den Keys von TwinRaid) inkrementell von der gemeinsamen Basis nach Klein senden; dort empfängt `zfs receive -F`. Das ist die einzige Stelle mit `-F`, und sie wird nur von Hand ausgeführt (`CLAUDE.md`, Regel 7). Was auf Klein nach der Basis geschrieben wurde, und Snapshots von Klein, die neuer sind als die Basis, gehen dabei verloren und müssen vorher gesichert werden. Danach Container auf Klein starten, TwinRaid starten, Docker-Dienst auf Groß ausschalten, Klone löschen (nie die Spiegel) und die Share-Konfigurationen auf Groß wieder löschen (E26). Der Sync läuft mit derselben Basis weiter.
- Geübt wird das einmal mit der Test-Share (Test Z5 in `docs/EINRICHTUNG.md`).

### 4.7 Vorabprüfung der Container-Templates (ab v0.5.4)

Im Notbetrieb liegen die gespiegelten Shares auf Groß unter denselben Pfaden `/mnt/mainpool/<Share>` wie auf Klein (4.6). Geprüft wird deshalb, ob ein Template absolute Pfade außerhalb von `/mnt/user/…` verwendet, die nicht in einer gespiegelten Share liegen (etwa auf einem anderen Pool), und ob benutzerdefinierte Docker-Netzwerke aus den Templates auf Groß existieren. Das Ergebnis erscheint als Hinweis im Probelauf. Empfehlung bleibt `/mnt/user/…`; Unraid-Shares im Modus „exklusiv“ vermeiden dabei den Performance-Nachteil von `/mnt/user`.

Die Templates liegen auf Klein auf dem Boot-Medium und enthalten Secrets anderer Container; TwinRaid bindet sie nicht ein. Wie sie nach Groß kommen und wer sie prüft (voraussichtlich der Wächter in ein eigenes Dataset unter appdata), wird vor v0.5.4 entschieden.

### 4.8 Zeitsteuerung

- Zeitplan als Wochentage plus Uhrzeit in einer festen Zeitzone (z. B. sonntags 03:00, `Europe/Berlin`), optional mit Wecken und Herunterfahren. Ein Cron-Ausdruck wird bewusst nicht verwendet, weil er schwer lesbar ist und Bibliotheken den Wochentag unterschiedlich zählen.
- Die Uhrzeit gilt als Ortszeit der angegebenen Zeitzone. Das Verhalten an den Tagen der Zeitumstellung wird in Phase 6 festgelegt.
- Der manuelle Sync bleibt jederzeit möglich (außer während eines laufenden Syncs).
- Der nächste geplante Lauf wird im Dashboard und im Startlog mit Wochentag und Uhrzeit angezeigt (ab Phase 6).
- In Phase 5 wird der Zeitplan nicht ausgeführt: `schedule.enabled: true` ergibt nur eine Warnung im Startlog, die Kachel Sync zeigt „Zeitplan aus (ab Phase 6)“.
- Fehler lösen optional eine Unraid-Benachrichtigung aus. Der Weg wird in Phase 6 festgelegt; einen API-Key für Groß gibt es seit v0.4.3 nicht mehr (E22), möglich wäre Unraids Skript `notify` über den Wächter auf Klein.

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
  - Für den Status spielt das keine Rolle (siehe 4.1). Container auf Klein steuert ab Phase 5 der Wächter auf dem Host (E7, siehe 3.6); TwinRaid braucht dafür keine Netzverbindung zu Klein.

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
- Von Phase 3 bis v0.4.2 zusätzlich die CLI-Befehle `set-api-key` und `trust-api-cert`. Seit v0.4.3 gibt es sie nicht mehr; alte Einträge bleiben lesbar.
- Ab v0.3.1 zusätzlich die Bestätigung des Hinweises zum CA-Wechsel.
- Ab Phase 4 zusätzlich jede Anforderung einer Aktion über die Oberfläche (`wake_target`, `shutdown_target`, `vm_start`, `vm_stop`, `vm_force_stop`, ab v0.4.1 `shutdown_target_force_vm`) mit dem Ergebnis `requested` (vom Worker angenommen) oder `rejected`, sowie die CLI-Befehle `init-ssh-keys` und `trust-host-key`. Wie die Aktion ausging, steht im Aktionslog des Workers.
- Ab v0.5.1 zusätzlich die Anforderungen `sync_dry_run`, `sync_start`, `sync_cancel` und `sync_confirm_deletions` (`requested` oder `rejected`) und der CLI-Befehl `sync-check`.
- **Nie gespeichert oder geloggt:** Passwörter, Session-IDs, CSRF-Tokens, Cookies, Request-Inhalte.
- Aufbewahrung 180 Tage und höchstens 50.000 Einträge; aufgeräumt wird beim Start und stündlich.
- Die Kachel „Verlauf“ zeigt die letzten 50 Einträge über eine geschützte Route.
- **Ab Phase 4:** Aktionen mit Außenwirkung (WoL, VM, Shutdown, Sync) protokolliert der Worker zusätzlich in einem eigenen Log unter `/data/worker`, das der Web-Prozess nicht verändern kann.

**Aktionslog des Workers (ab Phase 4)**

- Datei `/data/worker/actions.jsonl`, eine JSON-Zeile pro Eintrag. Ordner `0700` und Datei `0600`, beide root: Der Web-Prozess kann das Log weder lesen noch verändern.
- Felder: `at` (UTC), `id` (vom Worker vergeben, verbindet Beginn und Ende), `action`, `trigger` (`dashboard` oder `automatic`; bei `automatic` zusätzlich `part_of` mit der auslösenden Aktion, z. B. das Wecken beim VM-Start), `result` (`started` oder das Ergebnis aus einer festen Liste). Ab v0.5.1 kommen die Aktionen `sync_dry_run` und `sync_start` hinzu, darin automatisch `wake_target`, `source_guard_stop` (mit der Anzahl angehaltener Container, ohne Namen) und `source_guard_finish`; bei `target_refused` steht der Grund im Feld `reason` (feste Liste).
- Ergebnisse: `success`, `wol_failed`, `boot_timeout`, `vm_not_found`, `vm_not_configured`, `vm_service_unavailable`, `vm_start_timeout`, `vm_stop_timeout`, `ssh_key_missing`, `ssh_host_untrusted`, `ssh_host_key_mismatch`, `ssh_auth_failed`, `ssh_unreachable`, `ssh_denied`, `ssh_failed`, `guard_outdated`, `watch_interrupted` (der Stand der Sitzung `vm-watch` fehlte), `remote_failed`, `shutdown_timeout`, `vm_running` (guard.sh lehnte `poweroff` ab, weil die Gaming-VM lief), `other_vms_running` (ab v0.4.2: Groß wurde nicht ausgeschaltet, weil weitere VMs liefen), `interrupted` (Worker beendet), `internal_error`. Neu in v0.4.3 sind `vm_not_configured`, `vm_service_unavailable`, `guard_outdated`, `watch_interrupted` und `vm_running`; bis v0.4.2 gab es stattdessen die API-Ursachen (`api_unreachable`, `api_redirected`, `api_key_missing`, `api_key_invalid`, `api_forbidden`, `api_cert_untrusted`, `api_cert_changed`, `api_error`), alte Einträge bleiben lesbar. Ab v0.5.1 kommen hinzu: `dry_run_success`, `cancelled`, `deletions_declined`, `confirmation_timeout`, `source_guard_inactive`, `source_guard_lost`, `container_restart_failed`, `snapshot_low_space`, `snapshot_limit`, `too_soon`, `base_missing`, `target_refused` (Grund `space_low`, `too_soon`, `clone_active`, `receive_busy`, `name_denied`, `target_exists`, `mirror_root_missing`, `received_properties`, `props_not_applied`, `props_mismatch`, `mount_mismatch`, `delegation_mismatch` oder `receiver_id_in_use`), `target_modified`, `send_failed`, `job_timeout` sowie für den VM-Start `vm_device_busy`, `vm_memory_low` und `vm_start_failed`. Kein Feld übernimmt Text aus fremden Antworten; Werte aus dem Status des Wächters übernimmt TwinRaid nur nach strengem Schema.
- Jeder Eintrag steht zusätzlich im Container-Log. Ab 1 MiB wird die Datei nach `actions.jsonl.1` umbenannt (genau eine ältere Datei).
- Anzeige mit `twinraid-admin show-action-log`.

**Sync-Historie (ab v0.5.2)**

- Datei `/data/worker/sync-history.jsonl`, eine JSON-Zeile je Lauf (Probelauf oder Sync): Beginn, Ende, Art, Ergebnis, je Share gesendete Bytes und Zählungen aus `zfs diff`. Rechte, Größengrenze und Umbenennen wie beim Aktionslog.
- Die Kachel Sync zeigt die letzten 20 Läufe über `get_status`; `twinraid-admin show-sync-log [--lines N]` zeigt die Datei.
- Container- und Snapshot-Namen stehen nur im Syslog von Klein (Kennung `twinraid-source-guard`), Vorgänge auf Groß im Syslog von Groß (`twinraid-guard`).

### 5.4 Container-Härtung

- `--read-only`, beschreibbar sind nur `/data` und ein `tmpfs` für `/tmp`.
- `--cap-drop=ALL`. Nur die beim Start nötigen Capabilities werden hinzugefügt; danach behält der Worker nur `DAC_READ_SEARCH`.
- `--security-opt=no-new-privileges`.
- Nicht `--privileged`, kein Docker-Socket.
- Keine Datenmounts; ab v0.5.0 nur die Einbindungen des Wächters unter `/srv/worker/source-guard` (Auftragsdatei rw, Status ro, siehe 3.6 und 6.1).
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
| `DAC_READ_SEARCH` | ursprünglich: Worker liest Shares und Templates trotz fremder Dateirechte; seit E25 bindet TwinRaid keine Shares mehr ein. `twinraid-init` liest damit einen eigenen Schlüssel unter `/config/tls` (5.2) | Worker | bisher ohne Wirkung; der Starttest in v0.5.1 prüft, ob der Worker sie abgeben kann |

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
  - `twinraid-admin set-api-key target` und `twinraid-admin trust-api-cert target` (Phase 3 bis v0.4.2) entfallen ab v0.4.3 (E22): TwinRaid braucht für Groß keinen API-Key mehr.
  - `twinraid-admin mock-job on|off` (Phase 3, nur im Simulationsmodus): schaltet das Feld `job_running` der Simulation, um die Sitzungsregel aus 5.3 auszuprobieren. Entfällt ab v0.5.1; dann setzt der simulierte Sync das Feld.
  - `twinraid-admin init-ssh-keys [--key status|power] [--new]`: erzeugt ed25519-Keys und gibt die fertigen `authorized_keys`-Zeilen inklusive Einschränkungen aus. Ohne `--key` die Keys, die die Konfiguration braucht: „status“ immer (ab v0.4.3), „power“, wenn `actions.vm` oder `actions.shutdown` freigeschaltet ist (im Simulationsmodus beide). Ein Key wird nur angelegt, wenn er fehlt; sonst gibt der Befehl die Zeile zum vorhandenen Key erneut aus. `--new` ersetzt den mit `--key` genannten Key (die Zeile auf Groß muss dann ebenfalls ersetzt werden) und ist nur zusammen mit `--key` erlaubt. Die Zeile enthält `from=` mit der verwendeten Adresse des Containers (siehe 5.1). Den Key „sync“ erzeugt nicht TwinRaid, sondern der Wächter auf dem Host von Klein (`source-guard.sh init-key`, ab v0.5.1); er liegt nie im Container.
  - `twinraid-admin trust-host-key target`: holt den ed25519-Hostkey von Groß (`ssh-keyscan`), zeigt seinen SHA-256-Fingerabdruck zum Vergleich mit `ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub` auf Groß und speichert ihn nach Bestätigung mit `yes`. Für Skripte kann der erwartete Fingerabdruck mit `--fingerprint` übergeben werden. Danach gilt strikte Hostkey-Prüfung. Für jeden vorhandenen Key („status“, „power“) folgt ein Verbindungstest mit `check`, der nichts verändert und die Version von guard.sh prüft. Besteht der Test mit dem Key „status“, beendet der Befehl eine Wartezeit der Sitzung `vm-watch` sofort (ab v0.4.4, siehe 3.5); das gilt auch, wenn der Hostkey schon bestätigt war. Nur im Modus `real`.
  - `twinraid-admin show-action-log [--lines N]` (Phase 4): zeigt die letzten Einträge des Aktionslogs (siehe 5.3).
  - `twinraid-admin mock-tour on|off` und `twinraid-admin mock-fault <Name> on|off` (Phase 4, nur im Simulationsmodus, Namen siehe 6.4).
  - `twinraid-admin sync-check` (ab v0.5.1, nur Modus `real`, ändert nichts): zeigt die drei Ebenen (4.3), den Wächter, das eigene Dataset von TwinRaid, den Zustand der Spiegel auf Groß sowie `zfs_arc_max` und `c_max` von Groß als Tabelle.
  - `twinraid-admin show-sync-log [--lines N]` (ab v0.5.2): zeigt die letzten Einträge der Sync-Historie (5.3).
- Secrets stehen niemals in der YAML-Datei, in Git, in Logs oder im Frontend.
- Die Befehle für den SSH-Key „power“ kommen in Phase 4, der Key „status“ in v0.4.3, der Key „sync“ in v0.5.1 auf dem Host von Klein (3.6).
- Der Worker liest SSH-Keys und den bestätigten Hostkey bei jedem Verbindungsaufbau neu; nach `init-ssh-keys` oder `trust-host-key` ist kein Neustart nötig. Eine geänderte Datei beendet auch die Pause nach einem dauerhaften Fehler der Sitzung `vm-watch` (3.5).
- **Secrets auf Klein (ab v0.5.1):** Der Key „sync“ (`/boot/config/twinraid/ssh-sync`, root, `0600`) und der bestätigte Hostkey von Groß (`target-known-hosts`) liegen nur auf dem Boot-Medium von Klein. Die Freigabe „flash“ hat den Export „No“; ist Unraid Connect installiert, warnt die Kachel Klein (3.5).
- Die CLI läuft als root. Zugriffe auf die Datenbank des Web-Prozesses erledigt sie in einem Kindprozess als Web-Benutzer, weil root ohne `DAC_OVERRIDE` dort nicht schreiben darf. Der Container braucht dafür keine zusätzliche Capability.

### 5.6 Rechte auf den Unraid-Servern (Least Privilege)

- **API-Key Klein:** entfällt (Entscheidung E13). Der Wächter ruft auf dem Host die Docker-CLI mit fester Argumentliste auf (`docker inspect`, `docker stop --time`, `docker start`, nur mit selbst ermittelten IDs). Der Container bekommt nie den Docker-Socket und nie einen Key mit Docker-Rechten.
- **API-Key Groß:** entfällt ab v0.4.3 (Entscheidung E22, siehe 3.4 und 3.5). Von Phase 3 bis v0.4.2 war es ein Key **ohne Rolle** mit `VMS:READ_ANY`, ab Phase 4 zusätzlich `VMS:UPDATE_ANY`; in Phase 4 wurde real geprüft, dass ein Key ohne diese Rechte abgewiesen wird (Fehler unraid/api #1723 behoben). Nach der Umstellung wird der Key in Unraid gelöscht (*Settings → Management Access → API Keys*).
- **SSH-Key „status“ auf Groß (ab v0.4.3):**
  `restrict,from="<Container-IP>",command="/bin/bash /boot/config/twinraid/guard.sh status" ssh-ed25519 … twinraid-status`
  - `guard.sh status` erlaubt genau zwei Anfragen: `check` (gibt `twinraid-guard <Version> status ok` aus) und `vm-watch` (Statuszeilen, siehe 3.5). Beide verändern nichts.
  - Mit diesem Key läuft die dauerhafte Sitzung für den Status. Auch eine Installation ohne freigeschaltete Aktionen („Nur Statusanzeige“) braucht damit keinen Key, der etwas verändern kann. Ein Fehler in `vm-watch` kann über diese Verbindung nichts auslösen.
- **SSH-Key „sync“ auf Groß (ab v0.5.1):**
  `restrict,from="<IP des Hosts Klein>",command="/bin/bash /boot/config/twinraid/guard.sh sync" ssh-ed25519 … twinraid-sync`
  - Der Key liegt auf dem Host von Klein, nicht im Container; `from=` nennt deshalb die Adresse des Hosts Klein, nicht die von TwinRaid. `source-guard.sh init-key` erzeugt ihn und gibt die fertige Zeile aus.
  - `guard.sh sync` erlaubt genau zwei Anfragen: `check` (verändert nichts) und `receive <Name>` mit fester Befehlszeile nach `mainpool/twinraid-mirror/<Name>` (3.7). Bis v0.5.1 lehnt der Modus `sync` alles ab.
- **SSH-Key „power“ auf Groß (ab Phase 4):**
  `restrict,from="<Container-IP>",command="/bin/bash /boot/config/twinraid/guard.sh power" ssh-ed25519 … twinraid-power`
  - `restrict` schaltet Terminal, Port-, Agent- und X11-Weiterleitung sowie `~/.ssh/rc` ab, auch Funktionen, die künftige OpenSSH-Versionen hinzufügen. Es ersetzt die vier einzelnen `no-…`-Optionen (Entscheidung E12).
  - Unraids eigene Prüfung beim Speichern (*Users → root → SSH authorized keys*, `checkKey()` in `UserEdit.page` von Unraid 7.3) akzeptiert diese Zeile; gespeichert wird sie unverändert. Geprüft in Phase 4.
  - `guard.sh power` erlaubt ab v0.4.3 genau fünf Anfragen in `SSH_ORIGINAL_COMMAND`, alle ohne Parameter, die nur mit festen Texten verglichen und nie als Befehl ausgewertet werden: `check` (Verbindungstest, gibt `twinraid-guard <Version> power ok` aus), `vm-start`, `vm-shutdown` (nur ACPI), `vm-force-stop` und `poweroff` (`/sbin/poweroff`, nur wenn alle VMs aus sind). Die VM-Befehle gelten nur der VM aus `power.conf` (3.5). Alles andere endet mit Exit-Code 2 und einem Eintrag im Syslog von Groß. Bis v0.4.2 waren es `check`, `poweroff` und `vm-shutdown <UUID>`.
  - Der Key „power“ wird nur für Aktionen benutzt und ist nur nötig, wenn `actions.vm` oder `actions.shutdown` freigeschaltet ist. Er darf die eingetragene VM starten, herunterfahren und erzwingen; das ist weniger, als der frühere API-Key durfte (alle VMs). Groß darf er nur ausschalten, wenn keine VM läuft.
- **Dauerhaft gespeichert** wird die Zeile in `/boot/config/ssh/root/authorized_keys`; seit Unraid 6.9 ist `/root/.ssh` ein Link dorthin. Bearbeitet wird sie in der WebGUI unter *Users → root → SSH authorized keys*.
- **SSH-Client im Worker:** feste Argumentliste mit `-F /dev/null`, `BatchMode=yes`, `IdentitiesOnly=yes`, `StrictHostKeyChecking=yes`, eigener `UserKnownHostsFile` in `/data/secrets`, `GlobalKnownHostsFile=/dev/null`, `HostKeyAlgorithms=ssh-ed25519`, `ConnectTimeout=5`, ohne Passwort-Anmeldung. Fehler werden anhand von Exit-Code und festen Meldungen von OpenSSH einer Ursache zugeordnet; die Ausgabe selbst wird nicht geloggt.
- Die Host-Skripte liegen im Repo unter `host-scripts/` und werden nach `/boot/config/twinraid/` kopiert: `guard.sh` auf Groß (dort liegen auch `power.conf` und ab v0.5.1 `mirror.conf`), `source-guard.sh` auf Klein (3.6). `/boot` ist FAT32, Dateien dort sind nicht ausführbar; deshalb der Aufruf über `/bin/bash`. Bootet Groß intern (ab Unraid 7.3, Boot-Pool mit ZFS), liegt `/boot` nicht auf einem Stick; der Aufruf über `/bin/bash` funktioniert dort ebenso.
- **Test ohne echte Server:** Die CI startet einen Wegwerf-Container als nachgebautes Groß (Image per Digest gepinnt, `openssh-server` in derselben gepinnten Version wie der Client, nur im Test) mit `guard.sh`, einem nachgebauten `virsh` mit allen Zuständen, weiteren VMs und Hängern und einem Platzhalter für `poweroff`. Geprüft werden `from=`, `restrict`, die erzwungenen Befehlszeilen beider Keys, die strikte Hostkey-Prüfung, die Sitzung `vm-watch` (Zustände, Lebenszeichen, Ende ohne übrig bleibende Prozesse, Neuverbindung), die Prüfung vor `poweroff`, die Merkmale von Unraid Connect und alle Abläufe aus 4.2 (`docker/ssh-test.sh`). Ab v0.5.1 kommen die Werte des ARC, `memory-low`, `device-busy` und der Modus `sync` mit einem nachgebauten `zfs` hinzu (`docker/fake-target/zfs`, wie `virsh` auch in den pytest-Tests von guard.sh). Den Modus `sync` mit echtem ZFS prüft `docker/guard-sync-test.sh` in der CI mit Pools in Dateien (ab v0.5.1): Empfang als UID 64000 mit der Delegation aus der Anleitung, `check`, jede Ablehnung, Sperre bei Eigenschaften, Kindern und fehlendem Recht, Einhängen, Aufbewahrung mit zurückgestellten Zeiten, Klon-Sperre und `poweroff` während eines Empfangs.
- **Test des Wächters (ab v0.5.0):** `docker/source-guard-test.sh` startet den Wächter in der CI auf dem Runner mit echtem Docker und einem Testcontainer und prüft Abläufe, Angriffe aus dem Container (Symlink, rename, unlink, übergroße Datei, falsche Prüfsumme, Shell-Zeichen, nicht freigegebene Container, TwinRaid nach Name, Image, Label und Einbindung, alte Auftragsnummer, Schreiben in `state/`), Totmannschalter, Neustarts und Merkmale von Unraid Connect. Ab v0.5.1 prüft `docker/source-guard-sync-test.sh` den Wächter Version 2 mit echten ZFS-Pools in Dateien (`zfsutils-linux`, nur in der CI) und dem echten guard.sh als Gegenseite (statt ssh eine Hilfe, die die feste Argumentliste prüft und guard.sh wie sshd startet): Snapshot, `prepare` vollständig und inkrementell mit Zählungen, `send` mit neuer Basis, Freigabe, Senden vom Lesezeichen, fremder Snapshot auf Groß, Ablehnungen von guard.sh, `close` und Neustart während einer Übertragung, Aufbewahrung auf Klein. Die pytest-Tests des Wächters (`backend/tests/test_source_guard_jobs.py`) decken mit nachgebautem `zfs` und `ssh` jeden Code, jede Frist und `init-key` und `trust-target` ab.
- **Machbarkeitstest Z1 (ab v0.5.0):** `host-scripts/zfs-check.sh` prüft auf den echten Servern, wie sich OpenZFS bei genau den Befehlen von Wächter und guard.sh verhält: Snapshot mehrerer Datasets in einem Aufruf, Senden mit `-L -c -e`, Empfang mit der festen Befehlszeile aus 3.7 (auch jedes `-o` einzeln beim inkrementellen Empfang), alle Eigenschaften mit Quelle `local`, die Prüfung vor dem Einhängen, ein absichtlich böser Strom (mit `-p` und `-I`: `mountpoint` woandershin, `quota`, `refquota`, `reservation`, `sharesmb`, `twinraid:mirror` und ein altes `twinraid:received` an einem Zwischen-Snapshot; auf Groß ohne `sharesmb`, damit ZFS dort keine Samba-Freigabe an Unraid vorbei versucht), die Sperre mit `canmount=noauto` und das Zurücksetzen danach, ob `-x` auf dem Server wirkt (nur als Befund), was ohne `-F` scheitert (verändertes Ziel, vollständiger Strom in Vorhandenes, vorhandener Snapshot-Name), ein Strom mit `-p`, `zfs diff`, die Größenschätzung, Lesezeichen, Hold und Klone mit den Eigenschaften aus 4.6.
  - **Groß** (`zfs-check.sh target`, erst nach dem Schritt „Groß prüfen“, weil es `mainpool/twinraid-mirror` braucht): arbeitet im echten `mainpool`, aber nur in einem eigenen Test-Dataset `mainpool/twinraid-mirror/zfs-check` (Eigenschaft `twinraid:zfs-check` mit Laufkennung); es bricht ab, wenn das Dataset schon besteht. Es bricht auch ab, wenn `zfs allow mainpool/twinraid-mirror` etwas ausgibt (ergänzt am 20.10.2026): Mit der Delegation für guard.sh (E27) erbte das Test-Dataset alle 28 Rechte, und Z1b sowie die Prüfung von `receive:append` lieferten falsche Ergebnisse, etwa bei einem neuen Lauf nach einem Update von OpenZFS. Vorher wird die Delegation mit `zfs unallow -u 64000 mainpool/twinraid-mirror` entfernt und danach nach `docs/EINRICHTUNG.md` neu gesetzt. Es misst außerdem mit 512 MiB Zufallsdaten (lz4 kann sie nicht verkleinern) in einem Dataset mit `primarycache=all`, ob `MemAvailable` den ARC schon enthält (Grundlage für die Speicherprüfung in 3.5); dafür sollten alle VMs aus sein.
  - **Klein** (`zfs-check.sh source`): prüft vorher den freien Arbeitsspeicher und Platz in `/tmp`, legt dort einen Pool in einer Datei an (1 GiB, nur im Arbeitsspeicher) mit `cachefile=none` und eindeutigem Namen und zerstört am Ende nur diesen Pool; die echten Pools auf Klein berührt es nicht.
  - **receive:append:** Ob es unter Linux als zweite Sperre trägt, prüft das Skript mit einem Benutzer ohne root, ab v0.5.1 mit der UID 64000 wie guard.sh (nie `nobody`). Die Rechte (`receive:append` mit den laut zfs-allow(8) nötigen `mount` und `create`) vergibt es nur unterhalb von `.../zfs-check`, nie auf `twinraid-mirror`, und entfernt sie am Ende mit `zfs unallow`. Nur hier steht `zfs receive -F`: als Benutzer ohne root und auf dem eigenen Test-Dataset, denn genau das soll receive:append verhindern.
  - **Z1b (ab v0.5.1, E27):** Empfang als UID 64000 mit der vollen festen Befehlszeile aus 3.7, einmal vollständig in ein neues Dataset und einmal inkrementell. Das Skript beginnt mit `receive:append,create,mount` und allen Rechten für die festen `-o` und streicht dann ein Recht nach dem anderen, solange der Empfang noch klappt; heraus kommt die kleinste nötige Menge, die guard.sh prüft. Danach als root Einhängen mit `findmnt`, `twinraid:received` und Löschen eines Snapshots nach Regel, als UID 64000 die Gegenprobe (`-F`, `destroy`, `rollback`) und was der Benutzer mit den delegierten Rechten per `zfs set` ändern könnte. Es nennt, ob `zfs allow` die numerische UID annimmt, und die Ausgabe von `zfs allow`. In der CI und auf Groß; maßgeblich ist Groß.
  - **Aufräumen:** Am Ende und bei jedem Abbruch löscht es nur Objekte mit der eigenen Kennung, einzeln und ohne `-r`, und nennt alles, was übrig bleibt. Reste nach `kill -9` entfernt `zfs-check.sh cleanup target|source`, wieder nur eigene. Die Ausgabe enthält nur Codes und Zahlen.
  - In der CI prüft `docker/zfs-check-test.sh` das Skript mit Pools in Dateien auf dem Runner, darunter ein Pool `mainpool` mit `twinraid-mirror` wie auf Groß: Durchlauf in beiden Arten, danach nichts übrig, keine Rechte auf `twinraid-mirror`, fremde Daten unberührt, Abbruch mit SIGTERM und `kill -9`. Maßgeblich für das Verhalten von OpenZFS 2.4 auf Unraid bleibt der Lauf auf den Servern.

### 5.7 Regeln für den Code

Siehe `CLAUDE.md`, Abschnitt „Unverhandelbare Sicherheitsregeln“.

### 5.8 Öffentliches Repository

Das Repo ist öffentlich auf GitHub, das Container-Image wird öffentlich über die GitHub Container Registry (`ghcr.io`) verteilt. Deshalb gilt:

- Im Repo stehen nur Beispielwerte (`config.example.yaml`), nie echte IPs, MAC-Adressen, Hostnamen oder Secrets. Ebenso wenig Fingerabdrücke, UUIDs, Namen echter VMs, Container, Shares und Pools (erlaubt sind `mainpool`, die Unraid-Standardnamen wie `appdata`, `domains`, `isos` und `system` sowie die Testnamen `twinraidtest` und `twinraid-testcontainer`) oder Größen der echten Server. In Commits, PRs, Issues und Testprotokollen heißen die Server nur „Klein“ und „Groß“.
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
| `/data/secrets` | root | 0700 | Passwort-Hash; ab Phase 4 SSH-Key „power“ (`ssh-power`, `ssh-power.pub`) und bestätigter Hostkey von Groß (`target-ssh-known-hosts`); ab v0.4.3 SSH-Key „status“ (`ssh-status`, `ssh-status.pub`); alle Dateien `0600`. Von Phase 3 bis v0.4.2 lagen hier API-Key und Fingerabdruck von Groß (`target-api-key`, `target-api-cert.sha256`); `twinraid-init` löscht sie ab v0.4.3 einmal mit Hinweis im Log |
| `/data/tls` | root | 0700 | CA (`ca.pem`, `ca-key.pem`), Serverzertifikat (`server.pem`, `server-key.pem`), Vermerk des letzten CA-Wechsels (`ca-change.json`) |
| `/data/web` | root, Gruppe 10001 | 0770 | SQLite des Web-Prozesses (Datei `0600`, Besitzer 10001); Vermerk der letzten Erneuerungsanfrage `tls-renewal.json` |
| `/data/worker` (ab Phase 4) | root | 0700 | Aktionslog des Workers (`actions.jsonl`, `actions.jsonl.1`), ab v0.5.2 Sync-Historie (`sync-history.jsonl`, `sync-history.jsonl.1`), Dateien `0600`; der Web-Prozess kann sie weder lesen noch verändern |
| `/run/twinraid-tls` | root, Gruppe 10001 | 0750 | Zertifikatskette, Serverschlüssel, CA-Zertifikat und `info.json` für den Web-Prozess, nur im RAM |
| `/run/twinraid` | root, Gruppe 10001 | 0750 | Worker-Socket, Herzschlag; im Simulationsmodus die Schalter `mock-job-running` (bis v0.5.0), `mock-tour` und `mock-fault-<Name>` (nur root kann sie anlegen) |
| `/srv/worker/source-guard` (ab v0.5.0) | root | 0700, wie `/srv/worker` | vom Host eingebunden: Auftragsdatei `request` (rw) und `state/` mit `status.json` (ro) des Wächters (3.6); liegt nicht in `/data` |

## 6. Konfiguration

### 6.1 Volumes (Unraid-Template)

| Host-Pfad (Klein) | Container-Pfad | Modus | Inhalt |
|---|---|---|---|
| `/mnt/user/appdata/twinraid/config` | `/config` | ro | `twinraid.yaml`, optional `tls/` |
| `/mnt/user/appdata/twinraid/data` | `/data` | rw | SQLite, Secrets, generiertes Zertifikat |
| `/run/twinraid-source-guard/in/request` | `/srv/worker/source-guard/request` | rw, einzelne Datei | Auftragsdatei des Wächters (ab v0.5.0, siehe 3.6) |
| `/run/twinraid-source-guard/state` | `/srv/worker/source-guard/state` | ro | Status des Wächters (ab v0.5.0) |

Im Template stehen beide Einträge mit leerem Wert und leerem Standardwert; die Pfade `/run/…` (nicht `/var/run/…`) nennt nur die Beschreibung. Sonst könnten Unraid oder Community Applications das leere Feld beim ersten Anlegen mit dem Standardwert vorbelegen, und Docker legte das Verzeichnis an. Der Nutzer trägt die Pfade erst ein, wenn der Wächter läuft (`docs/EINRICHTUNG.md`); bis dahin zeigt TwinRaid „Wächter nicht eingerichtet“. Dass Unraid einen leeren Pfad auslässt, prüfen die Einrichtungstests; lässt Unraid ihn nicht weg, fügt die Anleitung die beiden Einträge erst nach dem Start des Wächters hinzu. TwinRaid bindet keine Shares, kein `/boot` und keinen Templates-Ordner ein (E25); die früher geplanten Einbindungen `/srv/worker/source` und `/srv/worker/templates` entfallen. Die beiden Pfade des Wächters legt der Wächter beim Start an. Deshalb wird er vor den Pfaden im Template eingerichtet (`docs/EINRICHTUNG.md`); sonst legt Docker einen Ordner statt der Datei an (`request_not_a_file`). Ab Test Z4 (v0.5.0) liegen `/config` und `/data` in einem eigenen Dataset `mainpool/appdata/twinraid` (E18); die Pfade im Template bleiben gleich.

### 6.2 Beispiel `twinraid.yaml`

```yaml
# TwinRaid-Konfiguration – wird read-only nach /config/twinraid.yaml gemountet.
# Secrets (Passwort, SSH-Keys) gehören NICHT hierher (siehe 5.5).

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
    webgui:                    # ohne Wirkung: Klein wird nicht geprüft (Konzept 4.1)
      scheme: http
      port: 80                 # optional, Standard 80 bei http, 443 bei https
  target:                      # Unraid Groß
    name: "Unraid Groß"
    host: 192.168.10.11
    webgui:                    # nur für die Online-Prüfung (TCP auf diesen Port)
      scheme: http             # wie „SSL/TLS verwenden“ in Unraid: Nein → http, Ja → https
      port: 80                 # optional, Standard 80 bei http, 443 bei https
    mac: "AA:BB:CC:DD:EE:FF"
    # wol_broadcast: 192.168.10.255  # optional; ohne Angabe aus dem eigenen Netz abgeleitet
    boot_timeout_minutes: 5
    # ssh_port: 22               # optional, Standard 22 (Status, VM und Herunterfahren per SSH)

gaming_vm:
  display_name: "Gaming-VM"
  unraid_vm_name: "Windows 11 Gaming"  # optional: Abgleich mit power.conf auf Groß
  shutdown_timeout_minutes: 3  # danach wird "Erzwingen" angeboten

actions:                       # optional; nur im Modus real, Standard: alles aus (siehe 4.2)
  wake: false                  # Groß wecken
  vm: false                    # VM starten, herunterfahren, erzwingen
  shutdown: false              # Groß herunterfahren
  sync_dry_run: false          # „Nur Probelauf“, schreibt nie nach Groß (ab v0.5.1)
  sync: false                  # echter Sync, erlaubt auch den Probelauf (ab v0.5.2)

sync:
  shares:                      # Datasets mainpool/<Share> auf Klein, Spiegel
    - appdata                  #   mainpool/twinraid-mirror/<Share> auf Groß;
    - dokumente                #   dieselben Namen in source-guard.conf (Klein)
    - medien                   #   und mirror.conf (Groß), siehe 4.3
  stop_containers: true        # Container der Erlaubnisliste des Wächters für den Snapshot anhalten
  never_stop: []               # zusätzlich nie anhalten; TwinRaid selbst ist immer ausgenommen
  copy_templates: false        # ohne Wirkung bis v0.5.4
  # Die Löschgrenze (max_deletions) steht in source-guard.conf auf Klein.
  schedule:                    # wird erst ab Phase 6 ausgeführt
    enabled: true
    timezone: "Europe/Berlin"  # IANA-Zeitzone
    days: [sun]                # mon, tue, wed, thu, fri, sat, sun
    time: "03:00"              # HH:MM, immer in Anführungszeichen
    wake_target: true
    shutdown_after: true       # nur wenn der Sync geweckt hat und die VM aus ist
```

### 6.3 Validierung

Die Konfiguration wird beim Start mit Pydantic vollständig validiert: Pflichtfelder, IP- und MAC-Formate, Zeitplan, verbotene Shares (`system`, `domains`). Ist die Konfiguration ungültig, startet der Container **nicht** und meldet im Log genau, was falsch ist. Die Weboberfläche zeigt die aktive Konfiguration nur lesend an.

Ergänzende Regeln:

- **Laden:** YAML wird ausschließlich mit `yaml.safe_load` gelesen. Unbekannte Schlüssel sind ein Fehler, damit Tippfehler auffallen.
- **Share-Namen** (`shares`): Prüfung über eine Positivliste erlaubter Zeichen. Erlaubt sind die Buchstaben `A–Z` und `a–z`, Ziffern, Punkt, Unterstrich und Bindestrich. Das erste Zeichen muss ein Buchstabe oder eine Ziffer sein, höchstens 64 Zeichen. Damit sind Schrägstrich, `..`, führendes Minus, Leerzeichen und Steuerzeichen ausgeschlossen. `system` und `domains` sind unabhängig von Groß- und Kleinschreibung verboten. Doppelte Einträge sind ein Fehler. Die Namen sind zugleich die Namen der Datasets `mainpool/<Share>` auf Klein und der Spiegel auf Groß; die erlaubten Zeichen sind auch in ZFS gültig. Wächter und guard.sh prüfen ihre Listen nach denselben Regeln.
- **Entfallene Schlüssel (ab v0.5.1, E25):** `appdata_share` (kein zweiter Lauf über appdata), `exclude` und `protect_on_target` (Ausschlüsse nur als eigenes Dataset, Schutz durch Snapshots, siehe 4.5) und `max_deletions` (steht in `source-guard.conf`). Damit eine ältere Konfiguration den Container nicht stoppt, werden `appdata_share`, `max_deletions` und leere Listen bis Version 1.0 mit einer Warnung im Startlog ignoriert. Nicht leere `exclude` oder `protect_on_target` sind ein Fehler mit Hinweis, weil sie einen Schutz vortäuschen würden, den es nicht mehr gibt.
- **Container-Namen** in `never_stop`: nur die von Docker erlaubten Zeichen.
- **WebGUI** (`servers.<server>.webgui`, ab Phase 3): `scheme` ist `http` oder `https`; `port` ist optional (1 bis 65535, Standard 80 bzw. 443). Ab v0.4.3 bestimmt es nur den Port der Online-Prüfung von Groß; bei Klein hat es keine Wirkung.
- **Gaming-VM** (`gaming_vm`): `unraid_vm_name` ist ab v0.4.3 optional (Abgleich mit dem Namen der VM aus `power.conf`, siehe 3.5); ist es gesetzt, gelten dieselben Zeichenregeln wie bisher.
- **Groß** (ab Phase 4): `wol_broadcast` ist optional (IPv4-Adresse, siehe 4.2); `ssh_port` ist optional (1 bis 65535, Standard 22).
- **Aktionen** (`actions`, ab Phase 4): optional; `wake`, `vm`, `shutdown` und ab v0.5.1 `sync_dry_run` und `sync` sind jeweils `true` oder `false`, Standard `false`.
- **Erlaubte Netze** (`web.allowed_networks`): mindestens ein Eintrag. Erlaubt sind Netze in CIDR-Schreibweise und einzelne Adressen (`/32` bzw. `/128` oder ohne Maske), gemischt. Gesetzte Host-Bits wie `192.168.10.50/24` sind ein Fehler, damit ein vermeintlich einzelnes Gerät nicht still das ganze Netz freigibt.
- **Sitzung:** `web.session_idle_minutes` ist optional (1 bis 720, Standard 10).
- **Port** (`web.port`): optional, Standard 8443.
- **TLS** (`web.tls`): `ip_addresses` ist optional (ohne Angabe gilt die erkannte Adresse, siehe 5.1). Ist es gesetzt, enthält es mindestens eine IP-Adresse, keine Multicast- oder unspezifizierte Adresse, ohne Duplikate. `hostnames` ist optional; erlaubt sind DNS-Namen nach RFC 1123 ohne Platzhalter, in Kleinbuchstaben gespeichert, ohne Duplikate. Die verwendeten Adressen und die Hostnamen bilden zusammen die erlaubten `Host`-Header und gehören zu den Werten, die nie öffentlich ausgeliefert werden.
- **Zeitplan** (`sync.schedule`): `days` enthält nur die Kürzel `mon`, `tue`, `wed`, `thu`, `fri`, `sat`, `sun`, ohne Duplikate und mindestens einen Tag. `time` hat das Format `HH:MM` (`00:00` bis `23:59`) und steht in Anführungszeichen. Ohne Anführungszeichen liest YAML manche Uhrzeiten wie `13:30` als Zahl; das wird mit einem klaren Hinweis abgelehnt. `timezone` ist eine gültige IANA-Zeitzone, die im Container vorhanden ist (z. B. `Europe/Berlin`); Sondereinträge wie `localtime` sind nicht erlaubt. In Phase 5 ergibt `enabled: true` nur eine Warnung (4.8).

### 6.4 Betriebsmodus (`TWINRAID_MODE`)

- Pflichtangabe als Umgebungsvariable des Containers: `mock` oder `real`. Es gibt **keinen Standardwert**.
- Fehlt der Wert oder ist er ungültig, startet der Container nicht und meldet im Log klar, was fehlt und welche Werte erlaubt sind.
- Entwicklungs-Skripte, Tests und CI setzen `mock` ausdrücklich.
- **Mock-Modus:** Alle Integrationen mit Außenwirkung sind durch Simulationen ersetzt. Das Log warnt beim Start deutlich. Die Oberfläche zeigt dauerhaft ein auffälliges Banner „Simulationsmodus“. `job_running` wird bis v0.5.0 mit `twinraid-admin mock-job on|off` geschaltet, ab v0.5.1 setzt es der simulierte Sync.
  - **Interaktive Simulation (ab Phase 4, Entscheidung E11):** Groß und die VM starten ausgeschaltet, Klein ist online („TwinRaid läuft auf diesem Server“). Aktionen wirken mit festen Wartezeiten: Groß bootet 20 s, danach braucht der VM-Dienst weitere 10 s; die VM startet in 3 s und fährt in 8 s herunter; Groß fährt in 15 s herunter. Ab v0.5.0 ist der Wächter simuliert (aktiv, ein freigegebener Container `twinraid-testcontainer`, Dataset `twinraidtest` mit Kompression `on`), ab v0.5.1 auch Probelauf und Sync mit festen Wartezeiten und Zahlen, die mit v0.5.1 festgelegt werden.
  - **Fester Rundlauf:** Die bisherige Statussimulation durchläuft einen festen Ablauf, der jeden Zustand und jede Ursache mindestens einmal zeigt, ab v0.3.2 auch „TwinRaid läuft auf diesem Server“, ab v0.4.4 bei Fehlern der Statusverbindung auch den nächsten Versuch, ab v0.5.0 alle Zustände und Fehler des Wächters, die Hinweise zu Unraid Connect auf Klein, einen offenen und einen gescheiterten Lauf mit der Pause danach und ZFS nicht verfügbar (ein Test prüft die Vollständigkeit). Ab Phase 4 wird sie mit `twinraid-admin mock-tour on|off` eingeschaltet; währenddessen sind Aktionen mit dem Grund `tour` gesperrt.
  - **Fehler simulieren:** `twinraid-admin mock-fault <Name> on|off` mit `vm-hang` (VM reagiert nicht auf Herunterfahren), `ssh-denied` (`guard.sh` lehnt ab), `no-boot` (Groß reagiert nicht auf Wake-on-LAN), ab v0.4.2 `other-vm` (auf Groß läuft eine weitere VM) und ab v0.4.3 `vm-not-configured` (`power.conf` fehlt), `libvirt-down` (VM-Dienst nicht bereit), `guard-outdated` (guard.sh zu alt) und `connect-plugin` (Unraid Connect mit Flash Backup auf Groß), ab v0.4.4 `status-key-rejected` (Groß lehnt den Key „status“ ab), ab v0.5.0 `source-guard-inactive` (Wächter läuft nicht), `source-guard-lost` (Wächter hat seinen Zustand verloren) und `source-guard-start-failed` (ein angehaltener Container startet nicht wieder; danach läuft die Pause bis zum nächsten Lauf), ab v0.5.1 `sync-deletions` (viele Löschungen), `sync-empty-source` (Quelle fast leer), `sync-target-refused` (guard.sh lehnt den Empfang ab), `target-exists` (fremdes Dataset an der Stelle eines Spiegels), `target-clone` (Klon auf Groß), `sync-send-error`, `sync-slow`, `vm-device-busy`, `vm-memory-low` und `arc-unlimited` (keine ARC-Grenze auf Groß). Bei `guard-outdated` und `status-key-rejected` zeigt die Simulation die Pause wie im Modus `real` mit dem nächsten Versuch nach 30 Minuten; das Ausschalten des Fehlers wirkt wie ein erfolgreiches `trust-host-key`. `api-forbidden` entfällt mit v0.4.3.
- **Modus `real` (ab Phase 3 nur lesend, ab Phase 4 mit freigeschalteten Aktionen):** Status-Abfragen sind immer erlaubt, also der TCP-Check von Groß und der VM-Status über die Sitzung `vm-watch` mit dem Key „status“ (ab v0.4.3; bis v0.4.2 über die Unraid-API), ab v0.5.0 auch das Lesen des Status des Wächters. Die Aktionen `wake_target`, `shutdown_target`, `shutdown_target_force_vm`, `vm_start`, `vm_stop` und `vm_force_stop` sind nur verfügbar, wenn sie in `actions` freigeschaltet sind (siehe 4.2); sonst lehnt der Worker sie vor jeder Verarbeitung mit dem Fehlercode `not_available` ab. `sync_dry_run` ist ab v0.5.1 mit `actions.sync_dry_run` oder `actions.sync` verfügbar, `sync_start` ab v0.5.2 mit `actions.sync`; `sync_cancel` und `sync_confirm_deletions` gelten nur für einen laufenden Job. Vorher lehnt der Worker sie mit `not_available` ab. Ist keine Aktion freigeschaltet, zeigt die Oberfläche das Banner „Nur Statusanzeige“.

---

## 7. Projektstruktur

```
twinraid/
├── CLAUDE.md                  # Regeln für Claude Code (lädt dieses Konzept)
├── README.md
├── docs/
│   ├── KONZEPT.md             # dieses Dokument
│   ├── EINRICHTUNG.md         # Installation auf Unraid und CA auf Windows/Linux/Android (ab Phase 3)
│   └── WIEDERHERSTELLUNG.md   # Notfall-Anleitung (ab v0.5.1 gesperrter Spiegel, ab v0.5.3 vollständig)
├── backend/
│   ├── pyproject.toml
│   ├── src/twinraid/
│   │   ├── config/            # YAML laden und validieren (Pydantic)
│   │   ├── web/               # FastAPI: Routen, Auth, Sessions, CSRF, Netzfilter, SSE
│   │   ├── worker/            # Befehlsannahme, Sperrlogik, Zeitsteuerung
│   │   ├── integrations/      # Wächter (Aufträge, Status), SSH, WoL, Netzwerk-Checks
│   │   │   └── mocks/         # Simulationen für Entwicklung und Tests
│   │   ├── sync/              # Sync über den Wächter: Ablauf, Fortschritt, Restzeit, Schutzprüfungen
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
│   ├── source-guard-test.sh   # Test des Wächters mit echtem Docker (ab v0.5.0)
│   ├── zfs-check-test.sh      # Test von zfs-check.sh mit Pools in Dateien (ab v0.5.0)
│   ├── guard-sync-test.sh     # Test von guard.sh sync mit Pools in Dateien (ab v0.5.1)
│   ├── source-guard-sync-test.sh  # Wächter 2 und guard.sh mit Pools in Dateien (ab v0.5.1)
│   └── unraid-template.xml    # Unraid-Template (ab Phase 3)
├── host-scripts/
│   ├── guard.sh               # SSH-Wrapper für Unraid Groß
│   ├── source-guard.sh        # Wächter für Unraid Klein (ab v0.5.0)
│   ├── source-guard.conf.example
│   ├── twinraid-testcontainer.xml  # Vorlage für den Testcontainer (ab v0.5.0)
│   ├── zfs-check.sh           # Machbarkeitstest Z1 (ab v0.5.0)
│   └── testdata.sh            # Testdaten für die Test-Share (ab v0.5.1)
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
| 5 | Sync über den Wächter auf Klein und ZFS (E25): v0.5.0 Konzept 0.13 und Wächter (Container-Teil, TwinRaid zeigt nur an); v0.5.1 ZFS-Teil, guard.sh Version 3, „Nur Probelauf“; v0.5.2 echter Sync der Test-Share mit Löschschutz, Fortschritt, Abbruch und Historie; v0.5.3 Snapshots anzeigen und `docs/WIEDERHERSTELLUNG.md`; v0.5.4 Templates (bleibt bis zur Freigabe aus) | Erst Sync einer Test-Share, danach vollständiger Sync aller Shares erfolgreich (Merkliste 10.2) |
| 6 | Zeitsteuerung, Auto-Wecken/-Herunterfahren, Benachrichtigungen | Ein geplanter Lauf läuft unbeaufsichtigt durch |
| 7 | Unraid-Template und Einrichtungsanleitung fertigstellen, unabhängiges Security-Review | Installation auf Unraid nach Anleitung möglich; Review-Befunde behoben |

---

## 9. Einrichtung auf Unraid (Checkliste)

Die Schritt-für-Schritt-Anleitung steht in `docs/EINRICHTUNG.md` (erste Fassung ab Phase 3, fertig in Phase 7).

- [ ] Groß: Wake-on-LAN im BIOS aktivieren („Wake on LAN“ bzw. „Power On by PCI-E“ an, „ErP/EuP“ aus) und `ethtool -s eth0 wol g` in `/boot/config/go` eintragen
- [ ] Groß: Array startet automatisch (*Settings → Disk Settings → Enable auto start*), sonst sind VMs nach dem Wecken nicht verfügbar
- [ ] Gaming-VM (Windows): Energieoptionen → „Beim Drücken des Netzschalters: Herunterfahren“, sonst reagiert sie nicht auf das ACPI-Signal
- [ ] Groß: SSH aktivieren; `guard.sh` (Version 2, ab v0.5.1 Version 3) nach `/boot/config/twinraid/` kopieren; `power.conf` mit der UUID der Gaming-VM anlegen; die Zeilen aus `twinraid-admin init-ssh-keys` (Key „status“, für freigeschaltete Aktionen auch „power“) unter *Users → root → SSH authorized keys* eintragen; Hostkey mit `twinraid-admin trust-host-key target` bestätigen
- [ ] Groß: einen API-Key von TwinRaid aus v0.4.2 oder früher in Unraid löschen (ab v0.4.3 nicht mehr nötig)
- [ ] Aktionen einzeln in `actions` freischalten und nach dem Testprotokoll in `docs/EINRICHTUNG.md` prüfen
- [ ] Klein (ab v0.5.0): gesicherte Shares als Datasets in `mainpool`, Kompression in den Pool-Einstellungen an; eigenes Dataset `mainpool/appdata/twinraid` für TwinRaid (E18, Test Z4)
- [ ] Klein (ab v0.5.0): Wächter `source-guard.sh` und `source-guard.conf` nach `/boot/config/twinraid/`, Zeile in `/boot/config/go`, einmal starten; erst danach die zwei Pfade im Template von TwinRaid ergänzen
- [ ] Groß (ab v0.5.0): Pool `mainpool` (ZFS) nur für die Spiegel, VMs und `system` auf einem anderen Pool; Share `twinraid-mirror` (Primärspeicher `mainpool`, Export „No“); keine Share-Konfigurationen mit den Namen der gespiegelten Shares und kein Pfad des VM-Managers dorthin (E26); Docker-Dienst aus; `zfs_arc_max` in Unraid eingetragen (*Settings → Disk Settings*)
- [ ] Groß (ab v0.5.1): erst TwinRaid 0.5.1, dann `guard.sh` Version 3; `mirror.conf` und die Zeile für den Key „sync“ aus `source-guard.sh init-key`; mit E27 die Delegation für UID 64000 auf `mainpool/twinraid-mirror` (Test Z1b vorher); Hostkey von Groß auf Klein mit `source-guard.sh trust-target` bestätigen
- [ ] Klein: „Host access to custom networks“ in den Docker-Einstellungen **nicht** aktivieren (siehe 5.1)
- [ ] Klein: Container-Templates prüfen: absolute Pfade nur unter `/mnt/user/…` oder in gespiegelten Shares (4.7)
- [ ] UniFi: feste IP für den Container, Firewall-Regel Client-Netz → Container-IP:8443
- [ ] Klein: `/mnt/user/appdata/twinraid/data` gehört root und ist nur für root beschreibbar (siehe 5.9)
- [ ] Feste IP nur im Template („Fixed IP address“); das Startlog zeigt `TLS address … (detected on …)` mit dieser IP
- [ ] Feste, lokal administrierte MAC im Template („Fixed MAC address“); in UniFi die IP per MAC reservieren; MAC vor und nach einem Update vergleichen
- [ ] `web.allowed_networks`: im Server-Netz nur einzelne Geräte als `/32`, andere Netze ganz, wenn die Router-Firewall sie regelt
- [ ] Container installieren, Konfiguration anlegen, Passwort setzen, Hostkey von Groß bestätigen
- [ ] Geräte: CA-Zertifikat auf Windows, Linux und Android für Chrome bzw. Edge und Firefox installieren (Test in Issue #3)

---

## 10. Offene Entscheidungen

| Nr. | Frage | Stand |
|---|---|---|
| E1 | Laufen auf Groß eigene Container mit Daten in `appdata` oder mit eigenen Templates? | **entschieden:** nein; der Docker-Dienst auf Groß ist im Normalbetrieb aus. Der früher auf Groß geschützte Ordner `appdata/_twinraid` entfällt mit E25 (4.6) |
| E2 | GitHub-Repo öffentlich oder privat? | **entschieden:** öffentlich, Image über `ghcr.io` (siehe 5.8) |
| E3 | HTTPS-Variante | **entschieden:** eigene kleine CA mit Namensbeschränkung und separatem Serverzertifikat (ab Phase 3, vorher selbstsigniert), eigenes Zertifikat optional mountbar (siehe 5.2) |
| E4 | Projektname (Arbeitstitel „TwinRaid“) | offen |
| E5 | Prozessverwaltung im Container (s6-overlay oder supervisord) | **entschieden:** s6-overlay mit read-only Root und minimalen Capabilities (siehe 5.4) |
| E6 | Versionen und Image-Tags | **entschieden:** SemVer, `latest` nur für Releases, `edge` für `main` (siehe 5.8) |
| E7 | Wie steuert TwinRaid in Phase 5 die Container auf Klein, ohne dass „Host access to custom networks“ eingeschaltet wird? | **entschieden am 16.10.2026:** Option I, ein Wächter auf dem Host von Klein mit Auftragsdatei; TwinRaid kann nur anfordern. Umgesetzt als `source-guard.sh` (3.6), Abwägung in 10.1 |
| E8 | Wie wird die Gaming-VM heruntergefahren? | **entschieden:** per SSH mit dem Key „power“ (`virsh shutdown`, nur ACPI); Starten und Erzwingen über die API, weil `vm.stop` der API nach 10 s selbst erzwingt (siehe 3.4); seit v0.4.3 laufen auch Starten und Erzwingen per SSH über guard.sh (E22) |
| E9 | Wie werden Aktionen im Echtbetrieb schrittweise freigeschaltet? | **entschieden:** Abschnitt `actions` in `twinraid.yaml` mit `wake`, `vm`, `shutdown`, Standard aus (siehe 4.2) |
| E10 | Groß herunterfahren bei laufender VM | **entschieden:** Dialog „VM zuerst herunterfahren?“, dann VM und danach Server; weiterhin kein sofortiges Herunterfahren an der laufenden VM vorbei. **Ergänzt in v0.4.1:** Nur nach einem gescheiterten sauberen Versuch und ausdrücklicher Bestätigung bietet TwinRaid zeitlich begrenzt „Erzwingen und danach Server herunterfahren“ an (siehe 4.2). **Ergänzt in v0.4.2:** Laufen außer der Gaming-VM weitere VMs auf Groß, schaltet TwinRaid den Server gar nicht aus, weder manuell noch erzwungen noch automatisch nach einem Sync (siehe 4.2) |
| E11 | Simulationsmodus mit Aktionen | **entschieden:** interaktive Simulation als Standard, fester Rundlauf mit `mock-tour`, Fehler mit `mock-fault` (siehe 6.4) |
| E12 | Kleinere Punkte aus Phase 4 | **entschieden:** `wol_broadcast` optional (Startabbruch nur bei freigeschaltetem Wecken), `ssh_port` optional, 60 s für API-Aktionen, `restrict` in `authorized_keys` |
| E13 | Wie steuert der Wächter Docker? | **entschieden:** Docker-CLI auf dem Host (`docker inspect`, `docker stop --time`, `docker start`), feste Argumente, nur selbst ermittelte IDs; kein API-Key. Regel 1 in `CLAUDE.md` ist entsprechend gefasst |
| E14 | Wie wird der Wächter gestartet und überwacht? | **entschieden:** Zeile in `/boot/config/go` vor `emhttp`, eigene Überwachungsschleife, `flock` gegen eine zweite Instanz (3.6) |
| E15 | Datenweg für den Sync (rsync-Daemon über SSH mit chroot) | **ersetzt** durch E25 |
| E16 | Zeiten des Totmannschalters | **entschieden:** Höchstzeit eines Laufs 20 min (einstellbar 5–120), Herzschlag 120 s (30–600), `docker stop` 60 s (10–600); ergänzt am 18.10.2026: zwischen zwei Läufen mindestens 30 min (10–1440), Fristen nach Laufzeit des Systems |
| E17 | Was gilt, wenn der Wächter nicht aktiv ist? | **entschieden:** Sync und Probelauf sind vor dem Start gesperrt (`source_guard_inactive`); seit E25 braucht jeder Sync den Wächter, weil nur er sendet |
| E18 | Wie bleibt der eigene appdata-Ordner von TwinRaid aus dem Spiegel? | **entschieden:** eigenes Dataset `mainpool/appdata/twinraid`; der Wächter prüft per `docker inspect`, dass `/config` und `/data` darin liegen, sonst sendet er appdata nicht (`self_dataset_missing`, siehe 4.5) |
| E19 | Freischaltung des Syncs und neuer Worker-Befehl | **entschieden:** Schalter `actions.sync_dry_run` und `actions.sync`; Worker-Befehl `sync_dry_run` ohne Parameter |
| E20 | Quell-Mounts je Share für rsync | **ersetzt** durch E25: TwinRaid bindet keine Shares ein |
| E21 | ACLs und xattrs mit rsync erst nach einem Test | **ersetzt** durch E25: `zfs send` überträgt sie unverändert; Test 3 prüft das |
| E22 | Kein Secret von TwinRaid auf einem USB-Stick: VM-Status, VM-Start und Erzwingen weiter über die API oder über SSH? | **entschieden:** über SSH und guard.sh, als v0.4.3 vor Phase 5. Eigener Key „status“ für die dauerhafte Statussitzung, Key „power“ nur für Aktionen, die VM steht nur in `power.conf` auf Groß, guard.sh prüft vor `poweroff` selbst; kein API-Key mehr, HTTPS von Groß spielt keine Rolle mehr (siehe 3.5) |
| E23 | Snapshots auf Groß, damit der Spiegel Löschungen, Fehler oder Verschlüsselung auf Klein nicht unwiderruflich überträgt | **erledigt** durch E25: Jeder Empfang ist ein Snapshot; guard.sh bewahrt sie 30 Tage auf und löscht sie selbst (3.7) |
| E24 | Sync aus einem ZFS-Snapshot auf Klein statt Konsistenzlauf (Phase B)? | **aufgegangen** in E25: Der Wächter legt den Snapshot an und sendet ihn selbst, ohne ihn einzuhängen |
| E25 | Sync mit ZFS auf beiden Seiten (`zfs send` und `zfs receive`) statt rsync? | **entschieden am 17.10.2026:** ja. Der Wächter auf Klein sendet selbst (Key „sync“ nur auf dem Host von Klein), guard.sh empfängt ohne `-F` in schreibgeschützte Spiegel `mainpool/twinraid-mirror/<Share>` (3.6, 3.7). Ersetzt E15, E20, E21 und E23; E24 geht darin auf |
| E25a | Eigenschaften und ARC auf Groß | **entschieden:** alle Eigenschaften für Schutz, Platz und Einhängen (Liste in 3.7) **direkt am Spiegel mit `-o`** bei jedem Empfang, kein `-x`; Empfang mit `-u` und Prüfung vor dem Einhängen, keine Eigenschaft mit Quelle `received` (ergänzt am 18.10.2026: weil Unraid die oberste Ebene eines Pools beim Start zurücksetzt und weil `-x` in Z1 nicht zuverlässig wirkte). Die Grenze des ARC steht nur in Unraid (`zfs_arc_max`), nicht in `twinraid.yaml`; TwinRaid zeigt nur einen Hinweis, wenn sie 0 ist (geändert am 18.10.2026; vorher fest 2 GiB) |
| E25b | Snapshots auf Klein | **entschieden:** Basis immer (Hold und Lesezeichen), dazu 7 Tage; gelöscht nur vom Wächter nach Regel, nie auf Auftrag von TwinRaid |
| E25c | Kompression auf Klein | **entschieden:** über die Pool-Einstellung „Compression on“ in Unraid, alle Datasets erben sie; `on` und `lz4` gelten als gleichwertig (geändert am 18.10.2026; vorher `zfs set compression=lz4`) |
| E25d | Notbetrieb | **entschieden:** nur als Anleitung in `docs/WIEDERHERSTELLUNG.md` mit einmaligem Test; Klone mit ausdrücklich gesetzten Eigenschaften; keine Notfall-Info auf Groß (4.6) |
| E25e | VM-Start auf Groß | **entschieden:** guard.sh Version 3 prüft vor `vm-start` den freien Speicher (`memory-low`) und erkennt belegte Geräte (`device-busy`), ab v0.5.1 (3.5) |
| E25f | Flash Backup beim Senden | **entschieden:** kein Warten und keine Sperre; die Hinweise zu Unraid Connect zeigen eine Installation auf beiden Servern an |
| E26 | Wie geht der erste Empfang mit Datasets um, die Unraid selbst für Shares anlegt? | **entschieden am 18.10.2026:** A + D. guard.sh nimmt einen Empfang in ein vorhandenes Dataset nur an, wenn es ein eigener Spiegel ist (`twinraid:mirror`), sonst `target-exists`; es löscht oder ersetzt nie etwas. Auf Groß gibt es im Normalbetrieb keine Share-Konfigurationen mit den Namen der gespiegelten Shares, weil Unraid sonst auch auf anderen Pools Datasets anlegt; die Anleitung zum Notbetrieb legt die Shares neu mit Primärspeicher `mainpool` an und prüft und entfernt vorher ein von Unraid angelegtes Dataset (3.7, 4.6) |
| E27 | Empfang auf Groß als Benutzer ohne root mit `receive:append` als zweite Sperre? | **entschieden am 19.10.2026:** ja (Option B), nachdem Z1b in der CI und auf Groß bestanden ist. Feste UID/GID 64000 ohne Eintrag in `/etc/passwd` und `/etc/group` (sonst `receiver-id-in-use`), nie `nobody` oder eine UID von Samba oder Containern; genau 28 Rechte als Local+Descendent auf `mainpool/twinraid-mirror`, vom Nutzer von Hand eingerichtet, von guard.sh vor jedem Empfang als Menge verglichen und nie geändert (`delegation-mismatch`); feste Werte vor und nach jedem Empfang geprüft (`props-not-applied`); kein Rückfall auf root (3.7) |

Bewusst später entschieden werden: der Weg der Templates (vor v0.5.4), das Verhalten an den Tagen der Zeitumstellung und der Weg der Benachrichtigungen (Phase 6) sowie ein Fortsetzen abgebrochener Übertragungen über ein Resume-Token (nur bei Bedarf).

### 10.1 E7: Container auf Klein stoppen und starten

**Entschieden am 16.10.2026: Option I.** Die endgültige Form steht in 3.6: Der Wächter heißt `source-guard.sh`, steuert Docker über die CLI statt über die API (E13), tauscht Aufträge über eine einzelne Datei auf einem eigenen tmpfs unter `/run` aus statt über einen Ordner in `/data/worker` und sendet seit E25 auch die Snapshots. Der folgende Text ist die Abwägung vor der Entscheidung (Stand 16.10.2026) und bleibt zur Nachvollziehbarkeit stehen.

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
- *Nachteile:* Startet die Unraid-API neu, legt sie den Socket neu an, und die eingebundene Datei zeigt ins Leere → Neustart des Containers nötig. Abweichung vom bisherigen Weg über HTTP (bis v0.4.2 für Groß, siehe 3.5).

**I. Wächter auf Klein mit Auftragsdateien (neu)**
- *So:* Ein kleines Skript auf Klein (Ablage in `/boot/config/twinraid/`, gestartet aus `/boot/config/go`, wartet auf das Array) prüft alle 1–2 s einen festen Ordner im Datenordner von TwinRaid (`/mnt/user/appdata/twinraid/data/worker/docker-requests`, auf dem Host root-Rechte). Der Worker legt dort Aufträge in festem Format ab („stop `<Name>`“, „start `<Name>`“). Der Wächter führt nur diese beiden Befehle über die Unraid-API am lokalen Socket aus und schreibt das Ergebnis zurück. Der Key mit `DOCKER:READ_ANY`/`DOCKER:UPDATE_ANY` liegt **nur auf Klein** (root, `0600`), nie in TwinRaid.
- *Grenzen im Wächter, fest im Code:* nur `start` und `stop`; Namen nach den Regeln von Docker geprüft und in der Containerliste vorhanden; Container mit dem Image `ghcr.io/mantua1/twinraid` werden nie gestoppt; `start` nur für Container, die der Wächter selbst innerhalb desselben Laufs gestoppt hat; jeder Auftrag im Syslog von Klein.
- *Einstellungen:* Unraid: Skript ablegen, eine Zeile in `/boot/config/go`, API-Key für Klein anlegen (ohne Rolle, nur Docker) und beim Wächter hinterlegen. Netzwerk und UniFi: nichts. Kein zusätzliches Volume für TwinRaid (der Datenordner ist schon eingebunden).
- *Sicherheit:* Erfüllt Anforderung 1 auch bei einem übernommenen Container: TwinRaid kann nur Start und Stopp anfordern, keine Updates, kein Pausieren, keine Logs lesen. Der Wächter läuft als root auf Klein und muss so klein und streng sein wie `guard.sh`. Die Steuerung läuft weiterhin über die Unraid-API (CLAUDE.md, Regel 1), nur nicht aus dem Container.
- *Nachteile:* ein zusätzliches Programm auf Klein (Pflege bei Updates, wie `guard.sh`); läuft der Wächter nicht, findet kein Container-Stopp statt (TwinRaid erkennt das an fehlenden Antworten und lässt Phase B mit Warnung aus); bis zu 2 s Verzögerung je Auftrag. JSON-Verarbeitung auf Klein am besten mit dem vorhandenen PHP von Unraid statt Bash.

**H. Konsistenzlauf ohne Container-Stopp**
- `sync.stop_containers: false`; keine Rechte, keine Änderung. appdata laufender Container (z. B. SQLite) kann inkonsistent auf Groß ankommen. Höchstens als Übergang.

**Verworfen:** Host access trotz macvlan mit Bridging (Risiko laut 5.1); Netzwerk `host` für TwinRaid; Docker-Socket-Proxy (gibt TwinRaid über einen zweiten Container Zugriff auf `docker.sock`, widerspricht Regel 1; Freigaben nur je API-Bereich, nicht je Befehl); Weg über Groß (Groß müsste die API von Klein aufrufen).

**Entscheidung:** **Option I**. Sie ist die einzige, die alle drei Anforderungen erfüllt: kein Eingriff ins Netzwerk, keine Auswirkung auf andere Container, und TwinRaid selbst kann nur Start und Stopp anfordern. Alle Optionen, bei denen TwinRaid die API von Klein selbst aufruft (A bis G), brauchen `DOCKER:UPDATE_ANY` und erlauben damit auch Image-Updates und Pausieren. Als einfachere Rückfallebene bleibt **G** (keine Netzänderung, aber der breite Key liegt in TwinRaid), bis zur Umsetzung von I oder als Übergang **H**.

### 10.2 Merkliste vor der ersten echten Share

Bevor eine echte Share gespiegelt wird (nach v0.5.2), gilt:

- Die Machbarkeitstests Z1, Z3 und Z4 und die Tests 0 bis 8 aus `docs/EINRICHTUNG.md` sind bestanden und in den PRs festgehalten (nur „Klein“ und „Groß“).
- Auf Groß ist `zfs_arc_max` eingetragen (kein Hinweis `target_arc_unlimited`); die gleichzeitig laufenden VMs, der ARC und Unraid passen in den Arbeitsspeicher.
- In `mainpool` auf Groß liegen nur `twinraid-mirror` und die Spiegel; vorübergehende Datasets (etwa aus einem Umzug) sind gelöscht. Es gibt keine Share-Konfigurationen mit den Namen der gespiegelten Shares, kein Pool hat Datasets mit diesen Namen, und der ISO-Ordner des VM-Managers zeigt nicht auf eine solche Share (E26).
- Platz auf Groß: die Menge laut Probelauf plus Reserve; nach dem ersten Lauf ist `mainpool` höchstens zu 80 % belegt.
- Für appdata: eigenes Dataset von TwinRaid gefunden; Erlaubnisliste des Wächters mit den echten Containern; `never_stop` geprüft; das Ergebnis der Suche nach setuid- und Gerätedateien (4.6) angesehen.
- Templates: Bis v0.5.4 ist der Notbetrieb ohne Vorlagen nur eingeschränkt möglich; das ist bewusst so, bis der Weg entschieden ist.
- Je Share alle drei Ebenen ergänzen (4.3), eine Share nach der anderen, appdata erst mit der echten Erlaubnisliste des Wächters. Danach ist Phase 5 fertig.

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
- Der Test des Wächters (`docker/source-guard-test.sh`) und die Tests mit echten ZFS-Pools in Dateien (`zfsutils-linux`) laufen in der CI auf dem Runner mit root; lokal laufen sie nur, soweit das ohne root und ohne ZFS geht.
