# TwinRaid – Projektkonzept

> Arbeitstitel „TwinRaid“ · Version 0.4 · Stand 13.10.2026
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
                          WoL · Unraid-API (VM) · SSH (rsync, Shutdown)
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

- Die beiden Prozesse kommunizieren über einen Unix-Socket mit einer **festen, kleinen Befehlsliste**: `get_status`, `wake_target`, `shutdown_target`, `vm_start`, `vm_stop`, `vm_force_stop`, `sync_start`, `sync_cancel`, `sync_confirm_deletions`.
- Befehle haben **keine frei wählbaren Parameter** (keine Pfade, Hosts oder Shell-Befehle). Erlaubt sind höchstens IDs, die der Worker selbst vergeben hat (z. B. eine Job-ID).
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
| Tests | pytest (Backend), Vitest (Frontend); Mocks für alle externen Systeme |
| Qualität | ruff, mypy (strict), ESLint, Prettier; CI über GitHub Actions |

### 3.4 Schnittstellen zu Unraid

| Aufgabe | Weg | Rechte |
|---|---|---|
| Server online/offline | TCP-Check auf SSH- bzw. WebGUI-Port | keine |
| Wake-on-LAN | UDP-Magic-Packet an die Broadcast-Adresse des Server-Netzes | keine |
| Gaming-VM Status/Start/Stop/Force-Stop | Unraid-API auf Groß | API-Key nur mit VM-Rechten |
| Container stoppen/starten (Konsistenz beim Sync) | Unraid-API auf Klein | API-Key nur mit Docker-Rechten |
| Daten kopieren | rsync über SSH nach Groß | SSH-Key „sync“, per Wrapper auf erlaubte Zielpfade beschränkt |
| Groß herunterfahren | SSH (oder Unraid-API, falls sie eine Shutdown-Funktion bietet, wird in Phase 4 geprüft) | SSH-Key „power“, darf ausschließlich den Shutdown-Befehl ausführen |

**Kein Docker-Socket. Kein Root-Shell-Zugang auf irgendeinem Server.**

---

## 4. Funktionen

### 4.1 Dashboard

Kacheln:

1. **Unraid Klein:** Status (sollte immer online sein).
2. **Unraid Groß:** Status mit den Buttons „Wecken“ und „Herunterfahren“.
3. **Gaming-VM:** Status mit den Buttons „Starten“ und „Herunterfahren“. Nach einem Timeout zusätzlich „Erzwingen“.
4. **Sync:** letzter Lauf (Ergebnis, Dauer, Datenmenge), nächster geplanter Lauf, Button „Jetzt synchronisieren“, Live-Fortschritt während eines Laufs.
5. **Verlauf:** Liste der letzten Aktionen und Sync-Läufe (Audit-Log).

Statuswerte: `Online`, `Offline`, `Startet …`, `Fährt herunter …`, `Unbekannt`. Der Worker prüft alle 10 Sekunden; die Oberfläche bekommt Änderungen per SSE.

Destruktive oder störende Aktionen (Herunterfahren, Force-Stop, Sync mit vielen Löschungen) erfordern einen Bestätigungsdialog.

### 4.2 Aktionen und Sperren

| Aktion | Verhalten / Sperre |
|---|---|
| Groß herunterfahren | gesperrt, solange ein Sync läuft; läuft die Gaming-VM, erscheint ein Dialog: „VM zuerst herunterfahren?“ |
| Gaming-VM starten | ist Groß offline: automatisch wecken, warten bis die API erreichbar ist (Timeout aus Config), dann VM starten |
| Sync starten | gesperrt, wenn bereits ein Sync läuft; ist Groß offline, wird es automatisch geweckt |
| Automatisches Herunterfahren nach Sync | nur wenn **der Sync den Server geweckt hat** UND die Gaming-VM nicht läuft UND währenddessen keine manuelle Aktion auf Groß stattfand |

Alle Aktionen laufen über eine zentrale Sperrlogik im Worker (eine Aktion pro Ressource gleichzeitig).

### 4.3 Sync-Ablauf

1. **Vorabprüfung:** Groß erreichbar (sonst wecken); SSH-Hostkey stimmt; alle Quell-Shares existieren und sind nicht leer; Templates auf Pool-Pfade prüfen (siehe 4.7).
2. **Dry-Run** (`rsync --dry-run --stats`) pro Share: ermittelt Anzahl und Größe der zu übertragenden Dateien sowie die Anzahl der Löschungen.
3. **Löschschutz:** Würden mehr als `max_deletions` Dateien gelöscht, gilt:
   - Bei manuellem Lauf: Anzeige der Zahl und Freigabe durch den Nutzer.
   - Bei geplantem Lauf: Abbruch mit Benachrichtigung.
4. **Phase A – Hauptkopie bei laufenden Containern:** alle Shares inklusive appdata spiegeln (`rsync -aHAX --delete --numeric-ids …`). Hier fällt die meiste Datenmenge an.
5. **Phase B – Konsistenzlauf:** laufende Container merken, stoppen (außer `never_stop`), appdata erneut spiegeln (nur noch das Delta, typischerweise Sekunden bis wenige Minuten), Container-Templates kopieren.
6. **Container wieder starten:** genau die, die vorher liefen, auch bei einem Fehler in Phase B (`try/finally`).
7. **Abschluss:** Notfall-Info auf Groß schreiben (siehe 4.6), Ergebnis in die Historie eintragen.
8. **Optional:** Groß herunterfahren (Bedingungen siehe 4.2).

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
- **In der App:** Erlaubte Netze kommen aus der Konfiguration. Anfragen von anderen Adressen werden vor jeder Verarbeitung mit 403 abgewiesen. `X-Forwarded-For` wird ignoriert, außer ein Reverse-Proxy ist explizit als vertrauenswürdig konfiguriert.
- **Unraid Klein:** In den Docker-Einstellungen „Host access to custom networks“ aktivieren. Sonst erreicht der Container mit eigener IP seinen eigenen Host (API von Klein) nicht.

### 5.2 HTTPS

- **Entschieden: nur HTTPS mit selbstsigniertem Zertifikat.** Beim ersten Start erzeugt der Container das Zertifikat (in `/data/tls`) und schreibt den Fingerabdruck ins Container-Log, damit man ihn mit der Browser-Anzeige vergleichen kann.
- **Eigenschaften des Zertifikats**, damit es auch als vertrauenswürdig installiert werden kann (inklusive iOS/macOS):
  - Gültigkeit höchstens 825 Tage.
  - Subject Alternative Name mit der Container-IP und optional einem Hostnamen aus der Config.
  - Extended Key Usage `serverAuth`.
  - Kein CA-Zertifikat. Es kann nur sich selbst bestätigen; wird es auf einem Gerät als vertrauenswürdig installiert, entsteht kein Risiko für andere Webseiten.
- **Beständigkeit:** Das Zertifikat bleibt über Neustarts und Image-Updates gleich. Neu erzeugt wird es nur bei Ablauf oder auf Befehl (`twinraid-admin renew-cert`).
- **Ablaufwarnung:** Das Dashboard warnt 30 Tage vor Ablauf. Die automatische Erneuerung erfolgt beim Ablauf und erfordert einmalig eine neue Bestätigung bzw. Installation auf den Geräten.
- **Download:** Das Zertifikat (nur der öffentliche Teil) ist nach dem Login unter „Einstellungen“ herunterladbar, um es auf Geräten als vertrauenswürdig zu installieren. Anleitung pro Gerätetyp folgt in `docs/EINRICHTUNG.md`.
- **Optional:** ein eigenes Zertifikat unter `/config/tls` mounten (z. B. später Let's Encrypt über DNS-Challenge). Dafür ist keine Codeänderung nötig.
- Alle Ressourcen (Schriften, Skripte, Icons) sind im Image enthalten; es werden keine externen CDNs geladen. Das ermöglicht eine strikte Content-Security-Policy.

### 5.3 Login und Sessions

- Ein Benutzer. Das **Passwort wird ausschließlich über die Admin-CLI im Container gesetzt** (`twinraid-admin set-password`). Es gibt keine Ersteinrichtung über die Webseite, die ein Angreifer im Netz zuerst ausführen könnte.
- Passwort-Hash mit Argon2id.
- Session-Cookie mit `HttpOnly`, `Secure` und `SameSite=Strict`. Inaktivitäts-Timeout 30 Minuten (konfigurierbar), maximale Lebensdauer 12 Stunden.
- CSRF-Token für alle zustandsändernden Requests.
- Rate-Limit: nach 5 Fehlversuchen 15 Minuten Sperre (pro IP und global).
- Security-Header: CSP, `X-Frame-Options: DENY`, `Referrer-Policy: no-referrer`, `X-Content-Type-Options: nosniff`.
- Audit-Log: jede Aktion mit Zeitstempel und Client-IP.
- Ohne Login erreichbar sind nur die Login-Seite und `/health` (liefert nur „ok“).
- **Geschützt per Default:** Jede Route ist automatisch geschützt. Öffentliche Routen müssen im Code ausdrücklich markiert werden. Öffentlich sind nur `/health`, später die Login-Route und die Auslieferung der Oberfläche selbst (`index.html`, JavaScript, CSS, Icon). Die Oberfläche ist öffentlich, weil sie die Login-Seite enthält. Sie enthält keine Daten; alle Daten kommen über geschützte Routen unter `/api`.
- **Keine Konfigurationswerte in öffentlichen Dateien:** Die ausgelieferten Frontend-Dateien und alle anderen öffentlichen Routen enthalten keine Werte aus der Konfiguration, also keine IP-Adressen, Netze, MAC-Adressen, Hostnamen, Share-Namen, Pfade, Container-, VM- oder Servernamen. Solche Werte gibt es nur über geschützte Routen unter `/api`. Abgesichert durch einen Backend-Test und durch den Container-Starttest, der eine Konfiguration mit eindeutigen Testwerten verwendet und alle öffentlich ausgelieferten Dateien danach durchsucht.
- Unbekannte Pfade werden wie geschützte Routen behandelt. Ohne Session gibt es also 401 statt 404.
- Ein Test stellt sicher, dass es außer den markierten Routen keine ungeschützte Route gibt.
- **Bis Phase 2:** An der Stelle der Login-Prüfung steht ein Platzhalter. Er lässt Anfragen nur im Mock-Modus durch (siehe 6.4).

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
  - `twinraid-admin set-password`
  - `twinraid-admin init-ssh-keys`: erzeugt ed25519-Keys „sync“ und „power“ und gibt die fertigen `authorized_keys`-Zeilen inklusive Einschränkungen aus.
  - `twinraid-admin set-api-key source|target`
  - `twinraid-admin trust-host-key target`: zeigt den SSH-Fingerabdruck von Groß zur Bestätigung an. Danach gilt strikte Hostkey-Prüfung.
- Secrets stehen niemals in der YAML-Datei, in Git, in Logs oder im Frontend.

### 5.6 Rechte auf den Unraid-Servern (Least Privilege)

- **API-Key Klein:** nur Docker-Steuerung (Container stoppen/starten, lesen).
- **API-Key Groß:** nur VM-Steuerung (VMs starten/stoppen, lesen).
- **Rechte werden getestet:** In älteren API-Versionen gab es einen Fehler, durch den ein nur lesender Key Container und VMs starten und stoppen konnte (GitHub-Issue unraid/api #1723). In Phase 4 wird geprüft, dass ein Key ohne die nötigen Rechte tatsächlich abgewiesen wird.
- **SSH-Key „sync“ auf Groß:**
  `from="<Container-IP>",command="/bin/bash /boot/config/twinraid/guard.sh sync",no-pty,no-port-forwarding,no-agent-forwarding,no-X11-forwarding ssh-ed25519 …`
  `guard.sh` erlaubt ausschließlich rsync im Server-Modus mit Ziel in den konfigurierten Shares bzw. im Template-Ordner.
- **SSH-Key „power“ auf Groß:** gleiche Einschränkungen, erlaubt nur den sauberen Shutdown-Befehl.
- Die Wrapper-Skripte liegen im Repo unter `host-scripts/` und werden auf Groß nach `/boot/config/twinraid/` kopiert.
- Die Unraid-API wird per HTTPS angesprochen, falls in Unraid aktiviert. Sonst läuft sie per HTTP nur innerhalb des Server-Netzes (bekanntes Restrisiko, dokumentiert).

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

---

## 6. Konfiguration

### 6.1 Volumes (Unraid-Template)

| Host-Pfad (Klein) | Container-Pfad | Modus | Inhalt |
|---|---|---|---|
| `/mnt/user/appdata/twinraid/config` | `/config` | ro | `twinraid.yaml`, optional `tls/` |
| `/mnt/user/appdata/twinraid/data` | `/data` | rw | SQLite, Secrets, generiertes Zertifikat |
| `/mnt/user` | `/srv/worker/source` | ro | Quell-Shares |
| `/boot/config/plugins/dockerMan/templates-user` | `/srv/worker/templates` | ro | Container-Templates |

### 6.2 Beispiel `twinraid.yaml`

```yaml
# TwinRaid-Konfiguration – wird read-only nach /config/twinraid.yaml gemountet.
# Secrets (Passwort, API-Keys, SSH-Keys) gehören NICHT hierher (siehe 5.5).

web:
  port: 8443
  allowed_networks:            # nur Clients aus diesen Netzen dürfen zugreifen
    - 192.168.10.0/24          # Server-Netz
    - 192.168.20.0/24          # Client-Netz
  session_idle_minutes: 30
  default_language: de         # de | en

servers:
  source:                      # Unraid Klein – hier läuft der Container
    name: "Unraid Klein"
    host: 192.168.10.10
  target:                      # Unraid Groß
    name: "Unraid Groß"
    host: 192.168.10.11
    mac: "AA:BB:CC:DD:EE:FF"
    wol_broadcast: 192.168.10.255
    boot_timeout_minutes: 5

gaming_vm:
  display_name: "Gaming-VM"
  unraid_vm_name: "Windows 11 Gaming"
  shutdown_timeout_minutes: 3  # danach wird "Erzwingen" angeboten

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
    time: "03:00"              # HH:MM, Anführungszeichen sind Pflicht
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
- **Zeitplan** (`sync.schedule`): `days` enthält nur die Kürzel `mon`, `tue`, `wed`, `thu`, `fri`, `sat`, `sun`, ohne Duplikate und mindestens einen Tag. `time` hat das Format `HH:MM` (`00:00` bis `23:59`) und steht in Anführungszeichen, weil YAML ein unquotiertes `03:00` als Zahl liest. `timezone` ist eine gültige IANA-Zeitzone, die im Container vorhanden ist (z. B. `Europe/Berlin`); Sondereinträge wie `localtime` sind nicht erlaubt.

### 6.4 Betriebsmodus (`TWINRAID_MODE`)

- Pflichtangabe als Umgebungsvariable des Containers: `mock` oder `real`. Es gibt **keinen Standardwert**.
- Fehlt der Wert oder ist er ungültig, startet der Container nicht und meldet im Log klar, was fehlt und welche Werte erlaubt sind.
- Entwicklungs-Skripte, Tests und CI setzen `mock` ausdrücklich.
- **Mock-Modus:** Alle Integrationen mit Außenwirkung sind durch Simulationen ersetzt. Das Log warnt beim Start deutlich. Die Oberfläche zeigt dauerhaft ein auffälliges Banner „Simulationsmodus“.
- Solange es keine echten Integrationen gibt, lehnt der Container auch `real` ab und startet nicht.

---

## 7. Projektstruktur

```
twinraid/
├── CLAUDE.md                  # Regeln für Claude Code (lädt dieses Konzept)
├── README.md
├── docs/
│   ├── KONZEPT.md             # dieses Dokument
│   ├── EINRICHTUNG.md         # Installation auf Unraid (Phase 7)
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
│   └── unraid-template.xml
├── host-scripts/
│   └── guard.sh               # SSH-Wrapper für Unraid Groß
├── config.example.yaml
└── .github/
    ├── workflows/             # CI: Tests, Lint, Image-Build
    └── dependabot.yml
```

---

## 8. Phasenplan

Jede Phase endet mit lauffähigem, getestetem Stand und einem Commit auf GitHub. Bis einschließlich Phase 3 wird ausschließlich gegen Mocks entwickelt. Echte Server werden erst angesprochen, wenn der Nutzer es ausdrücklich freigibt.

| Phase | Inhalt | Fertig, wenn … |
|---|---|---|
| 0 | GitHub-Repo, VS Code, Claude Code, `CLAUDE.md`, Konzept | Repo existiert mit Doku, erster Commit ist gepusht |
| 1 | Grundgerüst: Backend- und Frontend-Skelett, Dockerfile mit beiden Prozessen, Config laden und validieren, Mock-Modus, DE/EN, Dark/Light, CI | Container startet lokal im Mock-Modus, Dashboard (leer) sichtbar, CI grün |
| 2 | Sicherheit: HTTPS, Admin-CLI, Login, Sessions, CSRF, Netzfilter, Rate-Limit, Security-Header, Audit-Log | Ohne Login ist nichts erreichbar; jede Schutzmaßnahme hat einen Test |
| 3 | Status: Server-Checks, VM-Status, Live-Updates per SSE | Dashboard zeigt Status im Mock-Modus korrekt und live |
| 4 | Aktionen: WoL, VM Start/Stop/Force, Shutdown Groß, Sperrlogik, Auto-Wecken; erster Test gegen echte Server | Alle Buttons funktionieren auf den echten Servern; Rechte der API-Keys sind geprüft |
| 5 | Sync: Dry-Run, rsync, Fortschritt/Restzeit, Container stoppen/starten, Templates, Schutzmechanismen, Historie | Erst Sync einer Test-Share, danach vollständiger Sync erfolgreich |
| 6 | Zeitsteuerung, Auto-Wecken/-Herunterfahren, Benachrichtigungen, Notfall-Info auf Groß | Ein geplanter Lauf läuft unbeaufsichtigt durch |
| 7 | Unraid-Template, Einrichtungsanleitung, Image-Build per GitHub Actions, unabhängiges Security-Review | Installation auf Unraid nach Anleitung möglich; Review-Befunde behoben |

---

## 9. Einrichtung auf Unraid (Checkliste, wird in Phase 7 ausgearbeitet)

- [ ] Groß: Wake-on-LAN im BIOS und für die Netzwerkkarte aktivieren (ggf. per `ethtool` im Unraid-Startskript)
- [ ] Groß: SSH aktivieren; `guard.sh` nach `/boot/config/twinraid/` kopieren; `authorized_keys`-Zeilen aus `twinraid-admin init-ssh-keys` eintragen
- [ ] Beide: API-Keys mit minimalen Rechten anlegen (siehe 5.6)
- [ ] Klein: „Host access to custom networks“ in den Docker-Einstellungen aktivieren
- [ ] Klein: Container-Templates auf Pool-Pfade prüfen und auf `/mnt/user/…` umstellen
- [ ] UniFi: feste IP für den Container, Firewall-Regel Client-Netz → Container-IP:8443
- [ ] Container installieren, Konfiguration anlegen, Passwort setzen, Hostkey von Groß bestätigen

---

## 10. Offene Entscheidungen

| Nr. | Frage | Stand |
|---|---|---|
| E1 | Laufen auf Groß eigene Container mit Daten in `appdata` oder mit eigenen Templates? | **entschieden:** nein, Container auf Groß sind aus; geschützt wird nur `appdata/_twinraid` |
| E2 | GitHub-Repo öffentlich oder privat? | **entschieden:** öffentlich, Image über `ghcr.io` (siehe 5.8) |
| E3 | HTTPS-Variante | **entschieden:** selbstsigniert, eigenes Zertifikat optional mountbar (siehe 5.2) |
| E4 | Projektname (Arbeitstitel „TwinRaid“) | offen |
| E5 | Prozessverwaltung im Container (s6-overlay oder supervisord) | **entschieden:** s6-overlay mit read-only Root und minimalen Capabilities (siehe 5.4) |

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
