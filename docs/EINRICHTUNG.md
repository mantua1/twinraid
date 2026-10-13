# TwinRaid auf Unraid einrichten

> **Stand: Version 0.3 (erste Fassung).** Diese Version zeigt den Status beider Server und der Gaming-VM an. Aktionen wie Wecken, Herunterfahren, VM starten oder Sync folgen in späteren Versionen. Die Anleitung wird mit jeder Version erweitert und ist in Phase 7 fertig.

Diese Anleitung führt Schritt für Schritt durch die Installation auf **Unraid Klein** (dem stromsparenden Server, der immer läuft). Am Ende:

- läuft TwinRaid als Container mit eigener IP-Adresse im Server-Netz,
- ist die Weboberfläche per HTTPS aus dem Server-Netz und dem Client-Netz erreichbar,
- vertrauen deine Geräte (Windows, Linux, Android) dem Zertifikat ohne Warnung,
- zeigt das Dashboard den echten Status von Unraid Klein, Unraid Groß und der Gaming-VM.

Die Begriffe und Sicherheitsregeln stehen in [KONZEPT.md](KONZEPT.md).

## Inhalt

1. [Vorbereitung: Werte notieren](#1-vorbereitung-werte-notieren)
2. [Unraid Klein: Zugriff auf den eigenen Host erlauben](#2-unraid-klein-zugriff-auf-den-eigenen-host-erlauben)
3. [Ordner und Konfiguration anlegen](#3-ordner-und-konfiguration-anlegen)
4. [Container installieren (zuerst im Simulationsmodus)](#4-container-installieren-zuerst-im-simulationsmodus)
5. [Erster Start: Log prüfen und Passwort setzen](#5-erster-start-log-prüfen-und-passwort-setzen)
6. [UniFi: Zugriff aus dem Client-Netz erlauben](#6-unifi-zugriff-aus-dem-client-netz-erlauben)
7. [Erste Anmeldung und CA-Zertifikat herunterladen](#7-erste-anmeldung-und-ca-zertifikat-herunterladen)
8. [CA-Zertifikat auf den Geräten installieren](#8-ca-zertifikat-auf-den-geräten-installieren)
9. [Unraid Groß: API-Key nur zum Lesen anlegen](#9-unraid-groß-api-key-nur-zum-lesen-anlegen)
10. [In den Echtbetrieb wechseln](#10-in-den-echtbetrieb-wechseln)
11. [Später: Unraid Groß auf HTTPS umstellen](#11-später-unraid-groß-auf-https-umstellen)
12. [Fehlerbilder und Abhilfe](#12-fehlerbilder-und-abhilfe)
13. [Updates einspielen und Herkunft prüfen](#13-updates-einspielen-und-herkunft-prüfen)
14. [Befehle der Admin-CLI](#14-befehle-der-admin-cli)

### So sind die Schritte zu lesen

- **Unraid-Terminal:** In der Unraid-Oberfläche oben rechts auf das Symbol `>_` klicken. Es öffnet sich ein Fenster mit einer Kommandozeile auf dem Server.
- **Container-Konsole:** Reiter **Docker**, auf das Symbol des Containers *TwinRaid* klicken, dann **Console**. Dort laufen die Befehle `twinraid-admin …` innerhalb des Containers.
- Befehle in grauen Kästen kopierst du vollständig und fügst sie ein (im Terminal mit Rechtsklick oder `Strg+Umschalt+V`), dann `Enter`.
- Die Bezeichnungen in der Unraid-Oberfläche stehen hier auf Englisch, wie in der Standardsprache. In der deutschen Oberfläche heißen sie sinngemäß gleich.

---

## 1. Vorbereitung: Werte notieren

Notiere dir vorab diese Werte. Die Beispiele passen zur Beispielkonfiguration; deine Werte sind andere.

| Wert | Beispiel | Wo du ihn findest |
|---|---|---|
| Server-Netz | `192.168.10.0/24` | UniFi: *Settings → Networks* |
| Client-Netz | `192.168.20.0/24` | UniFi: *Settings → Networks* |
| **Feste IP für TwinRaid** | `192.168.10.20` | Selbst wählen: eine freie Adresse im Server-Netz **außerhalb** des DHCP-Bereichs |
| IP von Unraid Klein | `192.168.10.10` | Unraid Klein: *Settings → Network Settings* |
| IP von Unraid Groß | `192.168.10.11` | Unraid Groß: *Settings → Network Settings* |
| MAC-Adresse von Unraid Groß | `AA:BB:CC:DD:EE:FF` | Unraid Groß: *Settings → Network Settings* (wird erst für Wake-on-LAN gebraucht, muss aber schon in der Konfiguration stehen) |
| Broadcast-Adresse des Server-Netzes | `192.168.10.255` | Bei `/24` die letzte Adresse des Netzes |
| SSL-Einstellung beider Server | `No` | Unraid: *Settings → Management Access → Use SSL/TLS* |
| Name der Gaming-VM | `Windows 11 Gaming` | Unraid Groß: Reiter **VMs**, genau so geschrieben |
| Shares zum Spiegeln | `appdata`, `dokumente`, `medien` | Reiter **Shares**; `system` und `domains` sind nie erlaubt |

**Prüfen, ob die feste IP frei ist:** Im Unraid-Terminal `ping -c 3 192.168.10.20` (deine Adresse). Kommt *keine* Antwort („100% packet loss“), ist sie frei.

---

## 2. Unraid Klein: Zugriff auf den eigenen Host erlauben

TwinRaid bekommt eine eigene IP. Ohne diese Einstellung kann der Container seinen eigenen Host (Unraid Klein) nicht erreichen, und Klein würde als „offline“ angezeigt.

1. *Settings → Docker* öffnen.
2. Oben rechts **Advanced View** einschalten.
3. **Enable Docker** auf **No** stellen und **Apply** klicken. (Alle Container stoppen kurz.)
4. **Host access to custom networks** auf **Enabled** stellen.
5. **Enable Docker** wieder auf **Yes** stellen und **Apply** klicken.

---

## 3. Ordner und Konfiguration anlegen

Im **Unraid-Terminal von Unraid Klein**:

**3.1 Ordner anlegen.** Der Datenordner muss root gehören und darf nur für root beschreibbar sein. Sonst startet TwinRaid aus Sicherheitsgründen nicht.

```bash
mkdir -p /mnt/user/appdata/twinraid/config /mnt/user/appdata/twinraid/data
chown root:root /mnt/user/appdata/twinraid/data
chmod 755 /mnt/user/appdata/twinraid/data
```

> **Hinweis zu `appdata`:** TwinRaid speichert eine kleine SQLite-Datenbank. Am zuverlässigsten liegt `appdata` auf einem Pool (z. B. Cache) mit der Share-Einstellung *Exclusive access*. Bei den Unraid-Standardeinstellungen ist das meist schon so.

**3.2 Beispielkonfiguration herunterladen.** Die Version im Pfad (`v0.3.0`) passt zur installierten Version.

```bash
wget -O /mnt/user/appdata/twinraid/config/twinraid.yaml \
  https://raw.githubusercontent.com/mantua1/twinraid/v0.3.0/config.example.yaml
chmod 644 /mnt/user/appdata/twinraid/config/twinraid.yaml
```

**3.3 Konfiguration anpassen.** Öffne die Datei im Editor `nano`:

```bash
nano /mnt/user/appdata/twinraid/config/twinraid.yaml
```

Ersetze die Beispielwerte durch deine Werte aus Schritt 1:

- `web.allowed_networks`: Server-Netz und Client-Netz. Nur Geräte aus diesen Netzen dürfen TwinRaid aufrufen.
- `web.tls.ip_addresses`: die **feste IP für TwinRaid**. Sie kommt ins Zertifikat.
- `web.tls.hostnames`: leer lassen (`[]`), außer du hast für TwinRaid einen Namen im DNS eingerichtet.
- `servers.source.host` und `servers.target.host`: IPs von Klein und Groß.
- `servers.*.webgui.scheme`: `http`, wenn bei dem Server *Use SSL/TLS* auf **No** steht; `https` bei **Yes**. Den Port kannst du weglassen (80 bzw. 443).
- `servers.target.mac` und `servers.target.wol_broadcast`.
- `gaming_vm.unraid_vm_name`: der Name der VM genau wie im Reiter **VMs**.
- `sync.shares`: deine Shares. Der Sync selbst kommt erst in einer späteren Version, die Werte werden aber schon geprüft.

Speichern mit `Strg+O`, dann `Enter`; beenden mit `Strg+X`.

> **Wichtig:** Einrückungen sind Leerzeichen, keine Tabulatoren. Uhrzeiten stehen in Anführungszeichen (`time: "03:00"`). Ist etwas falsch, startet der Container nicht und nennt im Log genau die Stelle.

---

## 4. Container installieren (zuerst im Simulationsmodus)

Im Simulationsmodus spricht TwinRaid keine echten Server an. So prüfst du zuerst in Ruhe Login, Zertifikat und Netzwerk.

**4.1 Template herunterladen** (Unraid-Terminal auf Klein):

```bash
wget -O /boot/config/plugins/dockerMan/templates-user/my-TwinRaid.xml \
  https://raw.githubusercontent.com/mantua1/twinraid/v0.3.0/docker/unraid-template.xml
```

**4.2 Container anlegen:**

1. Reiter **Docker** → unten **Add Container**.
2. Bei **Template** unter *User templates* den Eintrag **TwinRaid** wählen.
3. **Network Type:** `Custom: br0` (ist vorausgewählt).
4. **Fixed IP address:** die feste IP für TwinRaid aus Schritt 1.
5. **Betriebsmodus:** `mock` lassen.
6. **Konfiguration** und **Daten:** die vorgegebenen Pfade lassen (`/mnt/user/appdata/twinraid/config` und `…/data`).
7. **Apply** klicken.

Die zusätzlichen Docker-Parameter (*Extra Parameters*, nur in der *Advanced View* sichtbar) härten den Container: schreibgeschütztes Dateisystem, nur fünf nötige Rechte, kein Rechteausbau. Bitte nicht ändern.

---

## 5. Erster Start: Log prüfen und Passwort setzen

**5.1 Log ansehen:** Reiter **Docker** → Symbol *TwinRaid* → **Logs**. Du solltest unter anderem sehen:

- einen deutlichen Hinweis **SIMULATION MODE ACTIVE**,
- `Generated the TwinRaid CA. Install its certificate once on your devices.`
- `TLS CA SHA-256 fingerprint: …` und `TLS CA SHA-1 fingerprint (shown by Windows): …`
- `No password is set. Run inside the container: twinraid-admin set-password`

Bleibt der Container nicht an, steht der Grund am Ende des Logs (z. B. ein Fehler in `twinraid.yaml` oder falsche Rechte am Datenordner mit dem passenden Reparaturbefehl).

**5.2 Passwort setzen:** In der **Container-Konsole**:

```bash
twinraid-admin set-password
```

Das Passwort wird zweimal ohne Anzeige abgefragt. Es muss mindestens 12 Zeichen haben. Es gibt keine Einrichtung über die Webseite; nur wer Zugriff auf die Unraid-Oberfläche hat, kann das Passwort setzen.

**5.3 Fingerabdruck notieren:** Ebenfalls in der Container-Konsole:

```bash
twinraid-admin show-cert-fingerprint
```

Lass dieses Fenster offen oder notiere die Werte unter *CA certificate*. Du brauchst sie in Schritt 8, um zu prüfen, dass du wirklich das CA-Zertifikat von TwinRaid installierst.

---

## 6. UniFi: Zugriff aus dem Client-Netz erlauben

Aus dem Server-Netz ist TwinRaid sofort erreichbar. Für das Client-Netz brauchst du eine Firewall-Regel. Es reicht genau ein Port.

**UniFi Network ab Version 9 (Zone-Based Firewall):** *Settings → Policy Engine → Zones → Create Policy*

- **Source:** Zone mit dem Client-Netz (bzw. das Client-Netz)
- **Destination:** Zone mit dem Server-Netz, **IP** = feste IP für TwinRaid, **Port** = `8443`
- **Protocol:** TCP
- **Action:** Allow

**Ältere UniFi-Versionen:** *Settings → Security → Traffic & Firewall Rules → Create Entry*, Typ *LAN In*, Aktion *Accept*, Quelle Client-Netz, Ziel IP-Adresse von TwinRaid, Port `8443`, Protokoll TCP. Die Regel muss vor einer Regel stehen, die den Verkehr zwischen den Netzen sperrt.

**Keine Portweiterleitung im Router einrichten.** TwinRaid ist nicht für das Internet gedacht.

---

## 7. Erste Anmeldung und CA-Zertifikat herunterladen

1. Im Browser `https://192.168.10.20:8443` aufrufen (deine feste IP).
2. Der Browser warnt, weil er die TwinRaid-CA noch nicht kennt. **Nur dieses eine Mal** die Warnung bestätigen (Chrome/Edge: *Erweitert → Weiter zu …*; Firefox: *Erweitert → Risiko akzeptieren und fortfahren*).
3. Mit dem Passwort aus Schritt 5 anmelden. Oben siehst du das gelbe Banner **Simulationsmodus**.
4. **Einstellungen** → Abschnitt **CA-Zertifikat** → **CA-Zertifikat herunterladen**. Die Datei heißt `twinraid-ca.crt`.
5. Vergleiche den **SHA-256-Fingerabdruck** auf der Seite mit dem Wert aus `twinraid-admin show-cert-fingerprint` (Schritt 5.3). Sie müssen genau gleich sein.

> **Warum eine CA?** TwinRaid erzeugt eine kleine eigene Zertifizierungsstelle (CA), die nur für die Adressen von TwinRaid gilt, und damit ein Serverzertifikat. Du installierst einmal nur die CA. Das Serverzertifikat erneuert sich alle gut zwei Jahre automatisch, ohne dass du auf den Geräten etwas tun musst. Selbst wenn jemand den CA-Schlüssel stehlen würde, könnte er damit keine anderen Webseiten fälschen.

---

## 8. CA-Zertifikat auf den Geräten installieren

Vor jeder Installation den Fingerabdruck vergleichen (Schritt 5.3). Danach den Browser vollständig schließen und neu öffnen und `https://<IP von TwinRaid>:8443` aufrufen: Es darf keine Warnung mehr erscheinen.

> **Falls du früher schon eine Ausnahme für TwinRaid bestätigt oder ein älteres Zertifikat installiert hast:** dieses zuerst entfernen (Firefox: *Einstellungen → Datenschutz & Sicherheit → Zertifikate anzeigen → Server*; Windows: `certmgr.msc`, Eintrag *TwinRaid* löschen).

### 8.1 Windows: Edge und Chrome

1. Doppelklick auf `twinraid-ca.crt` → **Zertifikat installieren…**
2. Speicherort **Aktueller Benutzer** → **Weiter**.
3. **Alle Zertifikate in folgendem Speicher speichern** → **Durchsuchen…** → **Vertrauenswürdige Stammzertifizierungsstellen** → **OK** → **Weiter** → **Fertig stellen**.
4. Windows zeigt eine Sicherheitswarnung mit einem **Fingerabdruck (sha1)**. Vergleiche ihn mit dem **SHA-1**-Wert aus `twinraid-admin show-cert-fingerprint`. Nur wenn er gleich ist: **Ja**.
5. Edge und Chrome vollständig schließen und neu öffnen.

### 8.2 Windows: Firefox

Firefox übernimmt ab Version 120 selbst installierte Zertifizierungsstellen von Windows automatisch. Nach Schritt 8.1 also nur Firefox neu starten.

Erscheint trotzdem eine Warnung: *Einstellungen → Datenschutz & Sicherheit → Zertifikate* und den Haken bei der Einstellung setzen, die Firefox erlaubt, Drittanbieter-Stammzertifikaten automatisch zu vertrauen. Alternativ wie unter Linux (8.4) direkt in Firefox importieren.

### 8.3 Linux: Chrome

Chrome hat unter Linux eine eigene Zertifikatsverwaltung:

1. Chrome: *Einstellungen → Datenschutz und Sicherheit → Sicherheit → Zertifikate verwalten*.
2. Bereich für **selbst installierte** bzw. **lokale** Zertifikate → **Vertrauenswürdige Zertifikate** → **Importieren** → `twinraid-ca.crt` wählen. Falls gefragt: als Zertifizierungsstelle für Websites vertrauen.
3. Chrome neu starten.

Falls die Menüpunkte bei deiner Chrome-Version anders heißen, geht es auch im Terminal (Paket `libnss3-tools` nötig). Zuerst den Fingerabdruck anzeigen und vergleichen, dann importieren:

```bash
openssl x509 -inform der -in ~/Downloads/twinraid-ca.crt -noout -fingerprint -sha256
certutil -d sql:$HOME/.pki/nssdb -A -t "C,," -n "TwinRaid CA" -i ~/Downloads/twinraid-ca.crt
```

### 8.4 Linux: Firefox

1. *Einstellungen → Datenschutz & Sicherheit → Zertifikate anzeigen…*
2. Reiter **Zertifizierungsstellen** → **Importieren…** → `twinraid-ca.crt` wählen.
3. Haken bei **Dieser CA vertrauen, um Websites zu identifizieren**. Mit **Ansehen** kannst du vorher den SHA-256-Fingerabdruck prüfen.
4. **OK**, Firefox neu starten.

### 8.5 Android: Chrome und Firefox

1. In Chrome auf dem Handy TwinRaid öffnen, die Warnung einmal bestätigen, anmelden und unter **Einstellungen** das CA-Zertifikat herunterladen. Es landet im Ordner *Downloads*.
2. In den **Android-Einstellungen** suchen nach **„Zertifikat installieren“** oder **„CA-Zertifikat“**. Der Weg hängt vom Hersteller ab, zum Beispiel:
   - Pixel/Android ohne Anpassungen: *Sicherheit und Datenschutz → Weitere Sicherheitseinstellungen → Verschlüsselung und Anmeldedaten → Zertifikat installieren → CA-Zertifikat*
   - Samsung: *Sicherheit und Datenschutz → Weitere Sicherheitseinstellungen → Vom Gerätespeicher installieren → CA-Zertifikat*
3. Die Warnung mit **Trotzdem installieren** bestätigen, `twinraid-ca.crt` wählen und mit PIN oder Fingerabdruck bestätigen.
4. Prüfen: *… → Vertrauenswürdige Anmeldedaten → Nutzer* → Eintrag **TwinRaid CA \<Datum\>** antippen. Der angezeigte SHA-256-Fingerabdruck muss zu Schritt 5.3 passen.
5. Chrome neu starten. **Firefox** übernimmt ab Version 120 die selbst installierten CAs von Android ebenfalls.

Erscheint in Firefox auf Android trotzdem eine Warnung: *Einstellungen → Über Firefox*, fünfmal auf das Firefox-Logo tippen, zurück zu den Einstellungen, *Geheime Einstellungen* → **Use third party CA certificates** einschalten.

> Android zeigt nach der Installation dauerhaft einen Hinweis wie „Netzwerk wird möglicherweise überwacht“. Das ist bei jeder selbst installierten CA so. Die TwinRaid-CA gilt nur für die Adressen von TwinRaid.

Die Ergebnisse dieser Schritte werden in GitHub-Issue #3 gesammelt. Wenn bei dir etwas anders aussieht, trag es dort bitte ein.

---

## 9. Unraid Groß: API-Key nur zum Lesen anlegen

TwinRaid liest den Status der Gaming-VM über die Unraid-API. Dafür braucht es einen API-Key, der **nur VMs lesen** darf. Die Rolle *Viewer* ist dafür zu breit: Sie darf fast alles auf dem Server lesen.

**Variante A: Unraid-Oberfläche von Unraid Groß**

1. *Settings → Management Access* → Bereich **API Keys** → **Create API Key** (oder ähnlich beschriftet).
2. **Name:** `TwinRaid Status` (nur Buchstaben, Ziffern und Leerzeichen).
3. **Roles:** keine auswählen.
4. **Permissions:** Ressource **VMS**, Aktion **Read (any)** bzw. `READ_ANY`. Nichts weiter.
5. Anlegen und den angezeigten Key kopieren. Er wird nur einmal vollständig angezeigt.

**Variante B: Unraid-Terminal von Unraid Groß** (gleiches Ergebnis):

```bash
unraid-api apikey --create --name "TwinRaid Status" \
  --permissions "VMS:READ_ANY" --description "TwinRaid: VM-Status lesen" --json
```

Den Wert hinter `"key"` kopieren (ohne Anführungszeichen).

> In Phase 4 bekommt dieser Key zusätzlich das Recht, VMs zu starten und zu stoppen (`VMS:UPDATE_ANY`). Bis dahin genügt Lesen.

---

## 10. In den Echtbetrieb wechseln

**10.1 Modus umstellen:** Reiter **Docker** → Symbol *TwinRaid* → **Edit** → **Betriebsmodus** auf `real` → **Apply**. Unraid legt den Container neu an; Passwort, Zertifikate und Einstellungen bleiben erhalten.

**10.2 API-Key hinterlegen:** In der **Container-Konsole**:

```bash
twinraid-admin set-api-key target
```

Den Key aus Schritt 9 einfügen (die Eingabe bleibt unsichtbar) und `Enter` drücken. TwinRaid speichert den Key und macht sofort eine Testabfrage. Erwartet wird zum Beispiel:

```text
OK: the API key works. VM 'Windows 11 Gaming' is SHUTOFF.
```

Klappt es nicht, nennt der Befehl die Ursache, etwa einen falschen Key, fehlende Rechte oder einen falsch geschriebenen VM-Namen (dann listet er die vorhandenen VMs auf). Hilfe dazu in Abschnitt 12.

**10.3 Dashboard prüfen:** Im Browser neu laden. Statt des gelben Banners steht oben **Nur Statusanzeige**. Die Kacheln zeigen innerhalb von etwa 10 Sekunden den echten Zustand. Ist Unraid Groß aus, steht bei der VM „Server offline“.

> **Hinweis bei `scheme: http`:** Solange Unraid Groß auf *Use SSL/TLS: No* steht, geht der API-Key unverschlüsselt durch das Server-Netz. TwinRaid zeigt das im Startlog und unter **Einstellungen → Verbindung zu Unraid Groß** an. Abschnitt 11 beschreibt die Umstellung auf HTTPS.

---

## 11. Später: Unraid Groß auf HTTPS umstellen

Dafür ist keine neue TwinRaid-Version nötig, nur Einstellungen.

**11.1 In Unraid Groß:** *Settings → Management Access* → **Use SSL/TLS** auf **Yes** → **Apply**. Die Unraid-Oberfläche lädt neu und ist danach per `https://` erreichbar; der Browser warnt, weil Unraid ein selbstsigniertes Zertifikat nutzt. (**Strict** wird von TwinRaid nicht unterstützt.)

**11.2 Fingerabdruck von Unraid Groß ermitteln** (zum Vergleich in 11.4). Am sichersten direkt auf dem Server, im **Unraid-Terminal von Groß**:

```bash
ls /boot/config/ssl/certs/
```

Es erscheint eine Datei, deren Name auf `_unraid_bundle.pem` endet, zum Beispiel `Tower_unraid_bundle.pem` (der erste Teil ist der Servername). Diesen Namen im nächsten Befehl einsetzen:

```bash
openssl x509 -in /boot/config/ssl/certs/Tower_unraid_bundle.pem -noout -fingerprint -sha256
```

Alternativ im Browser: `https://<IP von Groß>` öffnen, auf die Warnung bzw. das Schloss klicken, Zertifikat anzeigen, **SHA-256-Fingerabdruck** ablesen.

**11.3 In TwinRaid:** In `twinraid.yaml` (Unraid-Terminal von Klein, `nano /mnt/user/appdata/twinraid/config/twinraid.yaml`) bei `servers.target.webgui` eintragen:

```yaml
    webgui:
      scheme: https
      port: 443
```

Speichern und den Container neu starten (Reiter **Docker** → *TwinRaid* → **Restart**). Bis zum nächsten Schritt zeigt die VM-Kachel „Zertifikat von Unraid noch nicht bestätigt“.

**11.4 Zertifikat bestätigen:** In der **Container-Konsole**:

```bash
twinraid-admin trust-api-cert target
```

Der Befehl zeigt den Fingerabdruck, den Unraid Groß gerade ausliefert. Vergleiche ihn mit 11.2. Nur wenn er gleich ist, `yes` eingeben. Danach folgt wieder die Testabfrage. Unter **Einstellungen → Verbindung zu Unraid Groß** steht jetzt „Verschlüsselt“.

**Wenn Unraid sein Zertifikat später erneuert** (zum Beispiel nach einer Neuinstallation oder einer Änderung des Servernamens), zeigt die VM-Kachel **„Zertifikat geändert“**. TwinRaid sendet den API-Key dann nicht mehr. Prüfe den neuen Fingerabdruck wie in 11.2 und bestätige ihn wie in 11.4 mit `twinraid-admin trust-api-cert target`. Hast du das Zertifikat nicht selbst geändert, bestätige nichts und finde zuerst heraus, warum es anders ist.

---

## 12. Fehlerbilder und Abhilfe

| Anzeige im Dashboard | Bedeutung | Abhilfe |
|---|---|---|
| Klein: **Nicht erreichbar** | TwinRaid erreicht seinen eigenen Host nicht | Schritt 2 (Host access to custom networks); `servers.source.host` und `webgui` prüfen |
| Groß: **Nicht erreichbar**, VM: **Server offline** | Unraid Groß ist aus oder nicht erreichbar | Normal, wenn Groß aus ist. Sonst IP und `webgui` in `twinraid.yaml` prüfen |
| VM: **API nicht erreichbar** | Groß antwortet, die API aber nicht | `webgui.scheme` und `port` prüfen; Groß gerade erst gestartet? Kurz warten |
| VM: **Unraid leitet auf HTTPS um** | Groß steht auf *Use SSL/TLS: Yes*, TwinRaid nutzt `http` | Abschnitt 11.3 und 11.4 |
| VM: **Kein API-Key gesetzt** | Schritt 10.2 fehlt | `twinraid-admin set-api-key target` |
| VM: **API-Key ungültig** | Key falsch kopiert oder in Unraid gelöscht | Neuen Key anlegen (Schritt 9) und neu setzen |
| VM: **API-Key hat keine Leserechte für VMs** | Key ohne `VMS: Read (any)` | Rechte in Unraid ergänzen oder neuen Key anlegen |
| VM: **Zertifikat von Unraid noch nicht bestätigt** | `https` eingestellt, Fingerabdruck fehlt | Abschnitt 11.4 |
| VM: **Zertifikat geändert** | Unraid liefert ein anderes Zertifikat als bestätigt | Abschnitt 11, letzter Absatz |
| VM: **VM nicht gefunden** | `gaming_vm.unraid_vm_name` passt nicht | Namen genau wie im Reiter **VMs** eintragen, Container neu starten |
| **Der Status ist gerade nicht verfügbar** | Interner Teil von TwinRaid antwortet nicht | Container-Log prüfen, Container neu starten |
| **Live-Verbindung ist unterbrochen** | Verbindung zum Browser kurz weg (z. B. Neustart) | Verbindet sich von selbst neu |
| **Automatische Erneuerung … fehlgeschlagen** | Neues Serverzertifikat konnte nicht erzeugt werden | Container-Log prüfen; `twinraid-admin renew-cert` |
| Browser warnt trotz installierter CA | Falsche Datei installiert oder Adresse nicht in `web.tls` | Fingerabdruck vergleichen; Aufruf genau über die IP aus `web.tls.ip_addresses` |
| Container startet nicht | Fehler in Konfiguration oder Rechten | Ende des Container-Logs lesen; dort steht der Grund und meist der Befehl zur Reparatur |

**Abgemeldet nach 10 Minuten:** Ohne Klick endet die Sitzung nach `session_idle_minutes` (Standard 10). Ein offenes Dashboard zählt nicht als Aktivität; nur während ein Kopierjob läuft, bleibt die Sitzung mit offenem Dashboard bestehen (ab Phase 5). Den Wert kannst du in `twinraid.yaml` erhöhen (höchstens 720).

**Gesperrt nach falschen Passwörtern:** Nach 5 Fehlversuchen in 15 Minuten ist die Anmeldung von diesem Gerät 15 Minuten gesperrt. Sofort aufheben: `twinraid-admin unlock` in der Container-Konsole.

---

## 13. Updates einspielen und Herkunft prüfen

- Unraid prüft regelmäßig, ob es eine neue Version gibt. Im Reiter **Docker** steht dann bei TwinRaid **update ready**. Klick auf das Symbol → **Update**. Daten und Einstellungen bleiben erhalten.
- Das Template verwendet `ghcr.io/mantua1/twinraid:latest`. Dieser Tag zeigt immer auf das neueste Release. Wer eine Version festhalten möchte, trägt unter *Repository* zum Beispiel `ghcr.io/mantua1/twinraid:0.3` ein.
- Jedes Image wird ausschließlich von GitHub Actions aus diesem Repository gebaut und hat einen signierten Herkunftsnachweis. Prüfen kannst du ihn auf einem PC mit der GitHub-CLI (`gh`):

```bash
gh attestation verify oci://ghcr.io/mantua1/twinraid:0.3.0 --owner mantua1
```

Steht am Ende `Verification succeeded!`, stammt das Image nachweislich aus diesem Repository.

---

## 14. Befehle der Admin-CLI

Alle Befehle laufen in der **Container-Konsole**.

| Befehl | Wirkung |
|---|---|
| `twinraid-admin set-password` | Passwort setzen; beendet alle Sitzungen |
| `twinraid-admin unlock` | Login-Sperren sofort aufheben |
| `twinraid-admin show-cert-fingerprint` | Fingerabdrücke von CA und Serverzertifikat anzeigen |
| `twinraid-admin renew-cert` | Serverzertifikat sofort erneuern (Geräte merken nichts) |
| `twinraid-admin renew-cert --new-ca` | Neue CA erzeugen; danach muss sie auf **allen Geräten** neu installiert werden |
| `twinraid-admin set-api-key target` | API-Key von Unraid Groß speichern und testen |
| `twinraid-admin trust-api-cert target` | HTTPS-Zertifikat von Unraid Groß bestätigen |
| `twinraid-admin mock-job on` / `off` | Nur im Simulationsmodus: einen laufenden Kopierjob vortäuschen |
