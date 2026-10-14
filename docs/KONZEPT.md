# TwinRaid – Projektkonzept

> Arbeitstitel „TwinRaid“ · Version 0.8 · Stand 14.10.2026
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

- Die beiden Prozesse kommunizieren über einen Unix-Socket mit einer **festen, kleinen Befehlsliste**: `get_status`, `verify_password`, `wake_target`, `shutdown_target`, `vm_start`, `vm_stop`, `vm_force_stop`, `sync_start`, `sync_cancel`, `sync_confirm_deletions`.
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
| Tests | pytest (Backend), Vitest (Frontend); Mocks für alle externen Systeme |
| Qualität | ruff, mypy (strict), ESLint, Prettier; CI über GitHub Actions |
| Veröffentlichung | Image auf `ghcr.io`, gebaut und veröffentlicht nur von GitHub Actions, mit Herkunftsnachweis (siehe 5.8) |

### 3.4 Schnittstellen zu Unraid

| Aufgabe | Weg | Rechte |
|---|---|---|
| Groß online/offline | TCP-Check auf den WebGUI-Port aus `servers.target.webgui` | keine |
| Klein online/offline | keine Prüfung: TwinRaid läuft auf Klein, im Modus `real` gilt Klein deshalb immer als online (ab v0.3.2, siehe 4.1) | keine |
| Wake-on-LAN | UDP-Magic-Packet an die Broadcast-Adresse des Server-Netzes | keine |
| Gaming-VM Status | Unraid-API auf Groß (siehe 3.5) | API-Key nur mit `VMS:READ_ANY` (ab Phase 3) |
| Gaming-VM Start/Stop/Force-Stop | Unraid-API auf Groß | zusätzlich `VMS:UPDATE_ANY` (ab Phase 4) |
| Container stoppen/starten (Konsistenz beim Sync) | Unraid-API auf Klein; der Weg dorthin ohne „Host access to custom networks“ ist offen (E7) | API-Key nur mit Docker-Rechten |
| Daten kopieren | rsync über SSH nach Groß | SSH-Key „sync“, per Wrapper auf erlaubte Zielpfade beschränkt |
| Groß herunterfahren | SSH (oder Unraid-API, falls sie eine Shutdown-Funktion bietet, wird in Phase 4 geprüft) | SSH-Key „power“, darf ausschließlich den Shutdown-Befehl ausführen |

**Kein Docker-Socket. Kein Root-Shell-Zugang auf irgendeinem Server.**

### 3.5 Verbindung zur Unraid-API (ab Phase 3)

- **Adresse:** `<scheme>://<host>:<port>/graphql` mit den Werten aus `servers.<server>.host` und `servers.<server>.webgui`. Der Worker sendet eine GraphQL-Abfrage per POST mit dem Key im Header `x-api-key`. Für den VM-Status genügt `vms { domains { name state } }`.
- **Grenzen:** Zeitlimit 5 Sekunden, Antwort höchstens 256 KiB, Weiterleitungen werden nicht verfolgt.
- **`scheme: http`** (Unraid-Einstellung „SSL/TLS verwenden: Nein“): Der API-Key geht im Klartext durch das Server-Netz. Startlog (im Modus `real`) und Einstellungsseite zeigen deshalb deutlich: „Verbindung zu Unraid Groß unverschlüsselt, API-Key wird im Klartext übertragen“.
- **`scheme: https`** (Unraid-Einstellung „Ja“): Unraid nutzt dann ein selbstsigniertes Zertifikat, eine Prüfung über eine Zertifikatskette ist nicht möglich. TwinRaid vergleicht stattdessen den SHA-256-Fingerabdruck des Zertifikats mit einem bestätigten Wert (Vertrauen beim ersten Kontakt wie bei SSH). Bestätigt wird mit `twinraid-admin trust-api-cert target`: Der Befehl zeigt den Fingerabdruck, den Groß gerade ausliefert, zum Vergleich mit der Anzeige im Browser. Der Key wird erst gesendet, nachdem der Fingerabdruck geprüft ist.
- **Zertifikat geändert:** Erneuert Unraid sein Zertifikat, meldet TwinRaid „Zertifikat geändert“ und sendet den Key nicht mehr. Nach Prüfung im Browser wird der neue Fingerabdruck mit `twinraid-admin trust-api-cert target` bestätigt.
- **Umstellung von http auf https** ohne Codeänderung: in Unraid „Ja“ wählen, in `twinraid.yaml` `scheme: https` (Port 443) eintragen, Container neu starten, `twinraid-admin trust-api-cert target` ausführen. Die Anleitung steht in `docs/EINRICHTUNG.md`.
- Die Einstellung „Strikt“ (Zertifikat für myunraid.net) wird nicht unterstützt; sie bräuchte DNS-Auflösung über das Internet.
- **Ursachen bei Fehlern** (siehe 4.1): Server offline (TCP-Check scheitert), API nicht erreichbar, Unraid leitet auf HTTPS um, kein API-Key gesetzt, API-Key ungültig (Antwort 401 bzw. „UNAUTHENTICATED“), keine Leserechte (403 bzw. „FORBIDDEN“), Fingerabdruck nicht bestätigt, Zertifikat geändert, VM nicht gefunden, sonstiger API-Fehler.

---

## 4. Funktionen

### 4.1 Dashboard

Kacheln:

1. **Unraid Klein:** Status. Im Modus `real` immer „Online“ mit der Ursache „TwinRaid läuft auf diesem Server“ (siehe „Prüfung“).
2. **Unraid Groß:** Status mit den Buttons „Wecken“ und „Herunterfahren“.
3. **Gaming-VM:** Status mit den Buttons „Starten“ und „Herunterfahren“. Nach einem Timeout zusätzlich „Erzwingen“.
4. **Sync:** letzter Lauf (Ergebnis, Dauer, Datenmenge), nächster geplanter Lauf, Button „Jetzt synchronisieren“, Live-Fortschritt während eines Laufs.
5. **Verlauf:** Liste der letzten Aktionen und Sync-Läufe (Audit-Log).

Statuswerte: `Online`, `Offline`, `Startet …`, `Fährt herunter …`, `Unbekannt`. Zu jedem Status kann eine **Ursache** aus einer festen Liste kommen (z. B. „Server offline“, „API nicht erreichbar“, „API-Key ungültig“, „Zertifikat geändert“, „VM pausiert“; siehe 3.5). So ist bei einem Fehler klar unterscheidbar, ob der Server aus ist, die API nicht antwortet oder der Key nicht passt.

- **Prüfung:** Der Worker prüft alle 10 Sekunden. Groß per TCP-Check, die VM über die API nur, wenn Groß online ist. Ist Groß offline, gilt die VM als aus mit der Ursache „Server offline“. `get_status` liefert sofort den zuletzt geprüften Stand.
- **Klein (ab v0.3.2):** TwinRaid läuft auf Klein. Solange der Worker antwortet, ist Klein also online. Im Modus `real` entfällt deshalb der TCP-Check von Klein; die Kachel zeigt „Online“ mit der festen Ursache „TwinRaid läuft auf diesem Server“. Grund: Ohne „Host access to custom networks“ (siehe 5.1) erreicht der Container seinen eigenen Host nicht, ein TCP-Check würde Klein fälschlich als offline melden.
- **VM-Zustände** (libvirt über die Unraid-API): `RUNNING` und `IDLE` → Online; `PAUSED` → Online, Ursache „pausiert“; `PMSUSPENDED` → Online, Ursache „im Ruhezustand“; `SHUTDOWN` → Fährt herunter …; `SHUTOFF` → Offline; `CRASHED` → Offline, Ursache „abgestürzt“; `NOSTATE` und Unbekanntes → Unbekannt.
- **Live-Updates:** Die Oberfläche öffnet einen SSE-Datenstrom (`GET /api/events`, geschützt). Ein einziger Abfrage-Task im Web-Prozess fragt den Worker alle 2 Sekunden mit dem vorhandenen Befehl `get_status` und schickt Änderungen an alle offenen Datenströme. Es gibt dafür keinen neuen Worker-Befehl. Höchstens 32 Datenströme gleichzeitig.
- **Kopierjob:** Der Status enthält das Feld `job_running` („Kopierjob läuft“). Ab Phase 5 kommt es vom echten Sync. Bis dahin ist es im Modus `real` immer aus und im Simulationsmodus mit `twinraid-admin mock-job on|off` schaltbar. Es steuert die Ausnahme vom Inaktivitäts-Timeout (siehe 5.3).

Destruktive oder störende Aktionen (Herunterfahren, Force-Stop, Sync mit vielen Löschungen) erfordern einen Bestätigungsdialog.

### 4.2 Aktionen und Sperren

| Aktion | Verhalten / Sperre |
|---|---|
| Groß herunterfahren | gesperrt, solange ein Sync läuft; läuft die Gaming-VM, erscheint ein Dialog: „VM zuerst herunterfahren?“ |
| Gaming-VM starten | ist Groß offline: automatisch wecken, warten bis die API erreichbar ist (Timeout aus Config), dann VM starten |
| Sync starten | gesperrt, wenn bereits ein Sync läuft; ist Groß offline, wird es automatisch geweckt |
| Automatisches Herunterfahren nach Sync | nur wenn **der Sync den Server geweckt hat** UND die Gaming-VM nicht läuft UND währenddessen keine manuelle Aktion auf Groß stattfand |

**Broadcast-Adresse für Wake-on-LAN (ab Phase 4):** `servers.target.wol_broadcast` wird optional. Fehlt der Wert, verwendet der Worker die Broadcast-Adresse des eigenen Netzes (aus IP und Netzmaske der Schnittstelle), sofern `servers.target.host` in diesem Netz liegt. Liegt Groß in einem anderen Netz, ist die Angabe Pflicht und der Start scheitert sonst mit klarer Meldung. So muss der Wert nicht doppelt gepflegt werden.

Alle Aktionen laufen über eine zentrale Sperrlogik im Worker (eine Aktion pro Ressource gleichzeitig).

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
- **Nie gespeichert oder geloggt:** Passwörter, Session-IDs, CSRF-Tokens, Cookies, Request-Inhalte.
- Aufbewahrung 180 Tage und höchstens 50.000 Einträge; aufgeräumt wird beim Start und stündlich.
- Die Kachel „Verlauf“ zeigt die letzten 50 Einträge über eine geschützte Route.
- **Ab Phase 4:** Aktionen mit Außenwirkung (WoL, VM, Shutdown, Sync) protokolliert der Worker zusätzlich in einem eigenen Log unter `/data/worker`, das der Web-Prozess nicht verändern kann.

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
  - `twinraid-admin init-ssh-keys`: erzeugt ed25519-Keys „sync“ und „power“ und gibt die fertigen `authorized_keys`-Zeilen inklusive Einschränkungen aus.
  - `twinraid-admin trust-host-key target`: zeigt den SSH-Fingerabdruck von Groß zur Bestätigung an. Danach gilt strikte Hostkey-Prüfung.
- Secrets stehen niemals in der YAML-Datei, in Git, in Logs oder im Frontend.
- Die Befehle für SSH-Keys folgen in Phase 4, `set-api-key source` in Phase 5.
- Der Worker liest API-Key und Fingerabdruck bei jeder Prüfung neu; nach `set-api-key` oder `trust-api-cert` ist kein Neustart nötig.
- Die CLI läuft als root. Zugriffe auf die Datenbank des Web-Prozesses erledigt sie in einem Kindprozess als Web-Benutzer, weil root ohne `DAC_OVERRIDE` dort nicht schreiben darf. Der Container braucht dafür keine zusätzliche Capability.

### 5.6 Rechte auf den Unraid-Servern (Least Privilege)

- **API-Key Klein:** nur Docker-Steuerung (Container stoppen/starten, lesen).
- **API-Key Groß:** Ab Phase 3 ein Key **ohne Rolle** mit der einzigen Berechtigung `VMS:READ_ANY` (in der Unraid-Oberfläche: Ressource „VMS“, Aktion „Read (any)“). Die Rolle „Viewer“ ist zu breit: Sie darf laut Quellcode der Unraid-API alle Bereiche außer API-Keys lesen. Ab Phase 4 kommt `VMS:UPDATE_ANY` für Start und Stopp hinzu.
- **Rechte werden getestet:** In älteren API-Versionen gab es einen Fehler, durch den ein nur lesender Key Container und VMs starten und stoppen konnte (GitHub-Issue unraid/api #1723). In Phase 4 wird geprüft, dass ein Key ohne die nötigen Rechte tatsächlich abgewiesen wird.
- **SSH-Key „sync“ auf Groß:**
  `from="<Container-IP>",command="/bin/bash /boot/config/twinraid/guard.sh sync",no-pty,no-port-forwarding,no-agent-forwarding,no-X11-forwarding ssh-ed25519 …`
  `guard.sh` erlaubt ausschließlich rsync im Server-Modus mit Ziel in den konfigurierten Shares bzw. im Template-Ordner.
- **SSH-Key „power“ auf Groß:** gleiche Einschränkungen, erlaubt nur den sauberen Shutdown-Befehl.
- Die Wrapper-Skripte liegen im Repo unter `host-scripts/` und werden auf Groß nach `/boot/config/twinraid/` kopiert.
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
| `/data/secrets` | root | 0700 | Passwort-Hash; ab Phase 3 API-Key von Groß (`target-api-key`) und bestätigter Fingerabdruck seines Zertifikats (`target-api-cert.sha256`); ab Phase 4 SSH-Keys |
| `/data/tls` | root | 0700 | CA (`ca.pem`, `ca-key.pem`), Serverzertifikat (`server.pem`, `server-key.pem`), Vermerk des letzten CA-Wechsels (`ca-change.json`) |
| `/data/web` | root, Gruppe 10001 | 0770 | SQLite des Web-Prozesses (Datei `0600`, Besitzer 10001); Vermerk der letzten Erneuerungsanfrage `tls-renewal.json` |
| `/data/worker` (ab Phase 4) | root | 0700 | Aktionslog des Workers; der Web-Prozess kann es weder lesen noch verändern |
| `/run/twinraid-tls` | root, Gruppe 10001 | 0750 | Zertifikatskette, Serverschlüssel, CA-Zertifikat und `info.json` für den Web-Prozess, nur im RAM |
| `/run/twinraid` | root, Gruppe 10001 | 0750 | Worker-Socket, Herzschlag; im Simulationsmodus der Schalter `mock-job-running` |

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
- **Erlaubte Netze** (`web.allowed_networks`): mindestens ein Eintrag. Erlaubt sind Netze in CIDR-Schreibweise und einzelne Adressen (`/32` bzw. `/128` oder ohne Maske), gemischt. Gesetzte Host-Bits wie `192.168.10.50/24` sind ein Fehler, damit ein vermeintlich einzelnes Gerät nicht still das ganze Netz freigibt.
- **Sitzung:** `web.session_idle_minutes` ist optional (1 bis 720, Standard 10).
- **Port** (`web.port`): optional, Standard 8443.
- **TLS** (`web.tls`): `ip_addresses` ist optional (ohne Angabe gilt die erkannte Adresse, siehe 5.1). Ist es gesetzt, enthält es mindestens eine IP-Adresse, keine Multicast- oder unspezifizierte Adresse, ohne Duplikate. `hostnames` ist optional; erlaubt sind DNS-Namen nach RFC 1123 ohne Platzhalter, in Kleinbuchstaben gespeichert, ohne Duplikate. Die verwendeten Adressen und die Hostnamen bilden zusammen die erlaubten `Host`-Header und gehören zu den Werten, die nie öffentlich ausgeliefert werden.
- **Zeitplan** (`sync.schedule`): `days` enthält nur die Kürzel `mon`, `tue`, `wed`, `thu`, `fri`, `sat`, `sun`, ohne Duplikate und mindestens einen Tag. `time` hat das Format `HH:MM` (`00:00` bis `23:59`) und steht in Anführungszeichen. Ohne Anführungszeichen liest YAML manche Uhrzeiten wie `13:30` als Zahl; das wird mit einem klaren Hinweis abgelehnt. `timezone` ist eine gültige IANA-Zeitzone, die im Container vorhanden ist (z. B. `Europe/Berlin`); Sondereinträge wie `localtime` sind nicht erlaubt.

### 6.4 Betriebsmodus (`TWINRAID_MODE`)

- Pflichtangabe als Umgebungsvariable des Containers: `mock` oder `real`. Es gibt **keinen Standardwert**.
- Fehlt der Wert oder ist er ungültig, startet der Container nicht und meldet im Log klar, was fehlt und welche Werte erlaubt sind.
- Entwicklungs-Skripte, Tests und CI setzen `mock` ausdrücklich.
- **Mock-Modus:** Alle Integrationen mit Außenwirkung sind durch Simulationen ersetzt. Das Log warnt beim Start deutlich. Die Oberfläche zeigt dauerhaft ein auffälliges Banner „Simulationsmodus“. Die Statussimulation durchläuft einen festen Ablauf, der jeden Zustand und jede Ursache mindestens einmal zeigt, ab v0.3.2 auch „TwinRaid läuft auf diesem Server“ (ein Test prüft die Vollständigkeit); `job_running` wird mit `twinraid-admin mock-job on|off` geschaltet.
- **Modus `real` (ab Phase 3, nur lesend):** Erlaubt sind nur Status-Abfragen, also die TCP-Checks beider Server und der VM-Status über die Unraid-API. Alle Aktionsbefehle (`wake_target`, `shutdown_target`, `vm_start`, `vm_stop`, `vm_force_stop`, `sync_start`, `sync_cancel`, `sync_confirm_deletions`) lehnt der Worker im Modus `real` vor jeder Verarbeitung mit dem Fehlercode `not_available` ab, bis Phase 4 bzw. 5 sie freigibt. Die Oberfläche zeigt im Modus `real` das Banner „Nur Statusanzeige“.

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

- [ ] Groß: Wake-on-LAN im BIOS und für die Netzwerkkarte aktivieren (ggf. per `ethtool` im Unraid-Startskript)
- [ ] Groß: SSH aktivieren; `guard.sh` nach `/boot/config/twinraid/` kopieren; `authorized_keys`-Zeilen aus `twinraid-admin init-ssh-keys` eintragen
- [ ] Groß: API-Key ohne Rolle mit `VMS:READ_ANY` anlegen und mit `twinraid-admin set-api-key target` hinterlegen (ab Phase 4 zusätzlich `VMS:UPDATE_ANY`, ab Phase 5 Key für Klein, siehe 5.6)
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
| E7 | Wie steuert TwinRaid in Phase 5 die Container auf Klein, ohne dass „Host access to custom networks“ eingeschaltet wird? | **offen**, Optionen in 10.1; Entscheidung vor Beginn von Phase 5 |

### 10.1 E7: Unraid-API von Klein ohne Host access erreichen

**Ausgangslage:** TwinRaid hat eine eigene IP auf `br0` (macvlan mit Bridging). Ohne Host access erreichen sich Klein und der Container nicht (siehe 5.1). Der Status braucht Klein nicht mehr (siehe 4.1), der Konsistenzlauf beim Sync aber schon: Er stoppt und startet Container über die Unraid-API von Klein (siehe 3.4 und 4.3). Recherchiert am 14.10.2026 (Unraid 7.3.3, Docker 29.8.2); nichts davon ist umgesetzt oder auf Unraid getestet.

**A. Bridging aus, Docker-Netzwerk auf `eth0`, Host access an (Empfehlung von Unraid 6.12.4)**
- *So:* *Settings → Network Settings*: Bridging für `eth0` aus. Docker-Netzwerk `eth0` statt `br0` (macvlan direkt auf der Netzwerkkarte), Host access an; der Host nutzt dann `vhost0`.
- *Vorteile:* Weg, den Unraid selbst empfiehlt; macvlan auf einer physischen Schnittstelle gilt als zuverlässig. TwinRaid behält eigene IP und MAC und erreicht Klein direkt unter `servers.source.host`. Kein zusätzlicher Code.
- *Nachteile:* Betrifft alle Container mit eigener IP auf Klein (Netzwerk `br0` → `eth0`, Templates prüfen) und alle VMs auf Klein (Netzwerkquelle `vhost0` statt `br0`). Umbau mit kurzer Unterbrechung aller Dienste.

**B. ipvlan statt macvlan**
- *So:* *Settings → Docker → Docker custom network type* auf `ipvlan` (Standard von Unraid seit 6.11.5), dann Host access an.
- *Vorteile:* Keine macvlan-Call-Traces, Bridging bleibt, nur eine Einstellung.
- *Nachteile:* Alle Container teilen sich die MAC von Klein. Die feste MAC (siehe 5.1) und die Reservierung per MAC in UniFi funktionieren dann nicht mehr pro Container; UniFi sieht mehrere IPs hinter einer MAC. Unraid nennt eingeschränkte Funktion mit Netzwerkverwaltung von Ubiquiti. Betrifft ebenfalls alle Container mit eigener IP.

**C. Umweg über den Router**
- *C1 – Adresse von Klein in einem anderen VLAN, TwinRaid bleibt im Server-Netz:* Der Hinweg läuft über den Router, die Antwort aber nicht: Klein kennt das Server-Netz als direkt angeschlossen und antwortet über `br0` direkt an den Container. Wegen der macvlan-Trennung kommt die Antwort dort nicht an. Es ginge nur mit Policy-Routing auf Klein (eigene Routing-Tabelle für die VLAN-Adresse, in Unraid nur per Startskript). Fehleranfällig und schwer nachvollziehbar.
- *C2 – TwinRaid in ein eigenes VLAN* (Docker-Netzwerk `br0.<VLAN>`, Klein ohne eigene Adresse darin): Hin- und Rückweg laufen über den Router, das funktioniert. *Vorteile:* Docker-Einstellungen von Klein und die übrigen Container bleiben unverändert; die Router-Firewall kann TwinRaid genau begrenzen. *Nachteile:* VLAN in UniFi und auf Klein einrichten (Switchport mit Tag). Der gesamte Sync-Verkehr (bis etwa 1 TB) läuft durch den Router, dessen Routing-Leistung unter 2,5 GbE liegen kann. Wake-on-LAN muss ins Server-Netz, gerichtete Broadcasts über den Router sind meist gesperrt; das bräuchte eine eigene Lösung. Abhängig vom Router. Die eigene IP im Server-Netz (5.1) entfällt.

**D. Zweite Netzwerkkarte nur für Docker**
- *So:* Klein bekommt eine zweite Netzwerkkarte (`eth1`) im Server-Netz, ohne eigene Adresse und ohne Bridging. TwinRaid hängt per macvlan an `eth1`. Der Verkehr zu Klein geht über den Switch zu `eth0`/`br0` und zurück, die macvlan-Trennung greift nicht.
- *Vorteile:* Bridging, Host access und die übrigen Container bleiben unverändert. macvlan auf einer physischen Schnittstelle, wie Unraid es empfiehlt. Eigene IP und MAC bleiben, kein zusätzlicher Code.
- *Nachteile:* Zweite Netzwerkkarte, Switchport und Kabel nötig. Der Sync-Verkehr läuft über diese Karte, sie sollte also ebenfalls 2,5 GbE haben. Unraid rät in 6.12.4 bei seiner `eth0`-Lösung vom Aufteilen auf zwei Karten ab; hier wäre es bewusst nur für TwinRaid.

**E. Bridge-Netz mit Portfreigabe statt eigener IP**
- *So:* Container im Docker-Netz `bridge`, Port 8443 auf der IP von Klein freigegeben. Der Container erreicht Klein über dessen IP, der Verkehr bleibt im Host.
- *Vorteile:* Kein Umbau am Netzwerk, Unraid-Standard.
- *Nachteile:* Keine eigene IP und MAC; die Firewall-Regel zielt auf Klein. `web.tls.ip_addresses` muss die IP von Klein enthalten, die IP steht dann an zwei Stellen (siehe 5.1). Das Wake-on-LAN-Paket aus dem NAT-Netz kommt vermutlich nicht im Server-Netz an, weil Linux Broadcasts nicht weiterleitet (in Phase 4 zu prüfen). Der SSH-Key auf Groß lässt sich nur noch auf die IP von Klein beschränken (`from=`), nicht mehr auf TwinRaid allein. Ob Netzfilter und Rate-Limit die echte Client-IP sehen, hängt von der Portweiterleitung von Docker ab und muss geprüft werden.

**F. Zweites Netzwerk für den Container (`br0` plus Docker-Bridge zum Host)**
- *So:* TwinRaid bleibt mit eigener IP und MAC auf `br0` und hängt zusätzlich an einer internen Docker-Bridge, über deren Gateway er Klein erreicht.
- *Vorteile:* Eigene IP und MAC im Server-Netz bleiben; Unraid-Netzwerk und andere Container bleiben unverändert.
- *Nachteile:*
  - **IP-Erkennung:** `twinraid-init` findet dann zwei verwendbare Adressen und verweigert den Start (siehe 5.1). Lösung wäre `web.tls.ip_addresses` (die IP steht dann wieder an zwei Stellen) oder eine geänderte Erkennung, z. B. nur die Schnittstelle der Standardroute oder ausdrücklich ausgenommene Bridge-Adressen. Das wäre eine Konzeptänderung; auch die Warnung bei `172.16.0.0/12` müsste angepasst werden.
  - Unraid 7.3.3 hat im Template kein Feld für weitere Netzwerke (`ExtraNetworks` gibt es erst im Entwicklungszweig von Unraid). Mit `--network` in den Extra Parameters übernimmt Unraid das Netzwerk nicht mehr selbst und ignoriert die Felder für IP und MAC; beides stünde dann in den Extra Parameters. Alternativ `docker network connect` nach jedem Start per Skript.
  - Die Weboberfläche von Unraid und damit die API lauscht nur auf Schnittstellen `br*`, `bond*`, `eth*`, `wlan*` und `wg*` (`rc.library.source`). Die Docker-Bridge müsste unter *Settings → Network Settings → Interface Extra* ergänzt werden.
  - Die Standardroute muss über `br0` bleiben (bei Docker ab 28 über `gw-priority`).

**G. Unix-Socket der Unraid-API einbinden**
- *So:* Die Unraid-API lauscht auf Klein unter `/var/run/unraid-api.sock`; nginx leitet `/graphql` dorthin weiter. Nur diese eine Datei wird in den Container eingebunden, der Worker spricht GraphQL über den Socket.
- *Vorteile:* Kein Netzwerkweg nötig; Netzwerk von Klein und andere Container bleiben unverändert. Laut Quellcode der Unraid-API gilt am Socket dieselbe Anmeldung wie über das Netz (API-Key), die Rechte bleiben also über einen Key mit minimalen Rechten begrenzt (zu prüfen).
- *Nachteile:* Weicht von 3.4 und 3.5 ab (API über HTTP). Interne Schnittstelle, die sich mit Unraid-Updates ändern kann. Startet die API neu, legt sie den Socket neu an, und die eingebundene Datei zeigt ins Leere; dann ist ein Neustart des Containers nötig. Der Socket liegt im selben Ordner wie `/var/run/docker.sock`: Der Ordner selbst darf nie eingebunden werden (Sicherheitsregel 1). Startprüfung und Starttest müssten sicherstellen, dass am eingebundenen Pfad wirklich die Unraid-API antwortet und nicht Docker.

**H. Konsistenzlauf ohne Container-Stopp**
- *So:* `sync.stop_containers: false`; Phase B entfällt oder kopiert nur die Templates.
- *Vorteile:* Kein Zugriff auf Klein nötig, sofort möglich.
- *Nachteile:* appdata laufender Container, etwa SQLite-Datenbanken, kann inkonsistent auf Groß ankommen. Das Ziel „alle Container mit dem Stand des letzten Syncs sofort starten“ (siehe 1) ist dann nur eingeschränkt erreicht. Höchstens als Übergang denkbar.

**Verworfen:** Host access trotz macvlan mit Bridging einschalten (Risiko laut 5.1). Netzwerk `host` für TwinRaid: kein eigener Netzwerk-Namespace, alle Ports von Klein sichtbar, die IP-Erkennung fände viele Adressen.

**Vorläufige Einschätzung (nicht entschieden):** Mit dem geringsten Eingriff kommen D aus, falls eine zweite Netzwerkkarte vorhanden ist, und G, falls sich die Prüfungen bestätigen. Die von Unraid empfohlene Dauerlösung ist A, betrifft aber alle Container und VMs mit eigener IP auf Klein.

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
