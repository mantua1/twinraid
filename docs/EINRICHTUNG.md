# TwinRaid auf Unraid einrichten

> **Stand: Version 0.3.2 (erste Fassung).** Diese Version zeigt den Status beider Server und der Gaming-VM an. Aktionen wie Wecken, Herunterfahren, VM starten oder Sync folgen in späteren Versionen. Die Anleitung wird mit jeder Version erweitert und ist in Phase 7 fertig.

Diese Anleitung führt Schritt für Schritt durch die Installation auf **Unraid Klein** (dem stromsparenden Server, der immer läuft). Am Ende:

- läuft TwinRaid als Container mit eigener IP- und MAC-Adresse im Server-Netz,
- ist die Weboberfläche per HTTPS aus dem Server-Netz und dem Client-Netz erreichbar,
- vertrauen deine Geräte (Windows, Linux, Android) dem Zertifikat ohne Warnung,
- zeigt das Dashboard den echten Status von Unraid Klein, Unraid Groß und der Gaming-VM.

Die Begriffe und Sicherheitsregeln stehen in [KONZEPT.md](KONZEPT.md).

## Inhalt

1. [Vorbereitung: Werte notieren](#1-vorbereitung-werte-notieren)
2. [Unraid Klein: „Host access to custom networks“ ausgeschaltet lassen](#2-unraid-klein-host-access-to-custom-networks-ausgeschaltet-lassen)
3. [Ordner und Konfiguration anlegen](#3-ordner-und-konfiguration-anlegen)
4. [Container installieren (zuerst im Simulationsmodus)](#4-container-installieren-zuerst-im-simulationsmodus)
5. [Erster Start: Log prüfen und Passwort setzen](#5-erster-start-log-prüfen-und-passwort-setzen)
6. [UniFi: IP reservieren und Zugriff aus dem Client-Netz erlauben](#6-unifi-ip-reservieren-und-zugriff-aus-dem-client-netz-erlauben)
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
| **Feste IP für TwinRaid** | `192.168.10.20` | Selbst wählen: eine freie Adresse im Server-Netz **außerhalb** des DHCP-Bereichs. Sie wird **nur im Unraid-Template** eingetragen (Schritt 4), nirgends sonst. |
| **Feste MAC für TwinRaid** | `02:5e:3a:91:c4:07` | Selbst erzeugen (Befehl unten). Sie wird **nur im Unraid-Template** eingetragen (Schritt 4) und in UniFi für die Reservierung verwendet (Schritt 6). |
| Geräte im Server-Netz, die TwinRaid aufrufen dürfen | `192.168.10.50` (Admin-PC) | Einzelne Geräte, die im Server-Netz stehen, z. B. dein PC. Sie kommen als `/32` in die Konfiguration (Schritt 3.3). |
| IP von Unraid Klein | `192.168.10.10` | Unraid Klein: *Settings → Network Settings* |
| IP von Unraid Groß | `192.168.10.11` | Unraid Groß: *Settings → Network Settings* |
| MAC-Adresse von Unraid Groß | `AA:BB:CC:DD:EE:FF` | Unraid Groß: *Settings → Network Settings* (wird erst für Wake-on-LAN gebraucht, muss aber schon in der Konfiguration stehen) |
| Broadcast-Adresse des Server-Netzes | `192.168.10.255` | Bei `/24` die letzte Adresse des Netzes |
| SSL-Einstellung beider Server | `No` | Unraid: *Settings → Management Access → Use SSL/TLS* |
| Name der Gaming-VM | `Windows 11 Gaming` | Unraid Groß: Reiter **VMs**, genau so geschrieben |
| Shares zum Spiegeln | `appdata`, `dokumente`, `medien` | Reiter **Shares**; `system` und `domains` sind nie erlaubt |

**Prüfen, ob die feste IP frei ist:** Im Unraid-Terminal `ping -c 3 192.168.10.20` (deine Adresse). Kommt *keine* Antwort („100% packet loss“), ist sie frei. Achtung: Container mit eigener IP auf Unraid Klein selbst antworten hier nie (siehe Schritt 2). Sieh deshalb zusätzlich im Reiter **Docker** (oben rechts **Advanced View**) nach, ob ein Container die Adresse schon verwendet.

**MAC-Adresse erzeugen:** Im Unraid-Terminal (oder auf einem Linux-PC):

```bash
printf '02:%02x:%02x:%02x:%02x:%02x\n' $(od -An -N5 -tu1 /dev/urandom)
```

Das Ergebnis sieht aus wie `02:5e:3a:91:c4:07` und ist bei jedem Aufruf anders. Notiere es einmal und verwende immer dieselbe Adresse. Die `02` am Anfang kennzeichnet eine selbst vergebene Adresse („lokal administriert“), die mit keiner echten Netzwerkkarte kollidiert. Die MAC gehört wie die IP nur in das Template auf Unraid Klein, nicht in `twinraid.yaml` und nicht ins Repository.

> **Warum eine feste MAC?** Ohne feste Angabe bekommt der Container von Docker (ab Version 28, Unraid 7.3.3 hat 29.8.2) bei jedem Start eine neue, zufällige MAC. Eine Reservierung in UniFi würde dann nach dem nächsten Neustart oder Update nicht mehr passen, und UniFi zeigt jedes Mal ein neues Gerät.

---

## 2. Unraid Klein: „Host access to custom networks“ ausgeschaltet lassen

TwinRaid braucht diese Einstellung nicht. **Nicht einschalten.**

**Kontrolle:** *Settings → Docker*, oben rechts **Advanced View**. Dort sollte stehen:

- **Docker custom network type:** `macvlan`
- **Host access to custom networks:** `Disabled`

Nichts ändern, nur ansehen.

> **Warum ausgeschaltet?** Auf Unraid Klein hängen Container mit eigener IP per macvlan an der Bridge `br0`. Laut den Release Notes von Unraid 6.12.4 ist macvlan auf einer Bridge unzuverlässig und kann zu Call Traces (Kernel-Fehlermeldungen bis hin zu Abstürzen) führen. „Host access“ legt auf `br0` zusätzlich eine eigene macvlan-Schnittstelle für den Server selbst an und leitet dessen Verkehr darüber. Die von Unraid empfohlene Abhilfe (Bridging aus, Netzwerk `eth0`) würde alle Container mit eigener IP auf Klein betreffen.

**Was das für TwinRaid bedeutet:**

- Container mit eigener IP und Unraid Klein selbst erreichen sich gegenseitig nicht. Aus dem Unraid-Terminal von Klein bekommst du von TwinRaid deshalb keine Antwort (`ping`, `curl`). Das ist normal. Von deinem PC aus ist TwinRaid ganz normal erreichbar.
- Die Kachel **Unraid Klein** zeigt im Echtbetrieb immer **Online** mit dem Hinweis **TwinRaid läuft auf diesem Server**. TwinRaid läuft ja auf Klein; solange du das Dashboard siehst, ist Klein an.
- Für den späteren Sync muss TwinRaid Container auf Klein stoppen und starten. Wie das ohne Host access geht, ist noch offen (Konzept, Entscheidung E7). In dieser Version wird das nicht gebraucht.

---

## 3. Ordner und Konfiguration anlegen

Im **Unraid-Terminal von Unraid Klein**:

**3.1 Ordner anlegen.** Der Datenordner enthält das Passwort, die Schlüssel der CA und später den API-Key. Er muss **root gehören** und darf **nur für root beschreibbar** sein. Sonst startet TwinRaid aus Sicherheitsgründen nicht.

> **Den Ordner nicht über den Unraid-Dateimanager, eine Netzwerkfreigabe (SMB) oder den Windows-Explorer anlegen.** So angelegte Ordner gehören `nobody:users` und sind für alle beschreibbar (`0777`). TwinRaid verweigert dann den Start mit der Meldung `/data is owned by uid 99, expected uid 0 (root)`. Lege die Ordner deshalb mit den folgenden Befehlen im Terminal an.

```bash
mkdir -p /mnt/user/appdata/twinraid/config /mnt/user/appdata/twinraid/data
chown root:root /mnt/user/appdata/twinraid/data
chmod 755 /mnt/user/appdata/twinraid/data
```

**Kontrolle:**

```bash
ls -ld /mnt/user/appdata/twinraid/data
```

Die Zeile muss mit `drwxr-xr-x` beginnen und `root root` enthalten, zum Beispiel:

```text
drwxr-xr-x 1 root root 0 Oct 13 20:00 /mnt/user/appdata/twinraid/data
```

Steht dort `nobody users` oder beginnt die Zeile mit `drwxrwxrwx`, die beiden Befehle `chown` und `chmod` von oben wiederholen.

> **Vorsicht mit „New Permissions“ und „Docker Safe New Perms“.** Das Unraid-Werkzeug *Tools → New Permissions* und *Docker Safe New Perms* aus dem Plugin *Fix Common Problems* setzen Besitzer und Rechte auf `nobody:users` und Schreibrechte für alle zurück. *New Permissions* tut das für alle gewählten Shares und Datenträger, auch für `appdata`. *Docker Safe New Perms* lässt `appdata` normalerweise aus, aber nur, solange es in seinen Einstellungen ausgenommen ist. Erwischt eines der Werkzeuge den Datenordner von TwinRaid, startet der Container danach nicht mehr. Wie du das reparierst, steht in Abschnitt 12 unter „Rechte des Datenordners zurückgesetzt“.

> **Hinweis zu `appdata`:** TwinRaid speichert eine kleine SQLite-Datenbank. Am zuverlässigsten liegt `appdata` auf einem Pool (z. B. Cache) mit der Share-Einstellung *Exclusive access*. Bei den Unraid-Standardeinstellungen ist das meist schon so.

**3.2 Beispielkonfiguration herunterladen.** Die Version im Pfad (`v0.3.2`) passt zur installierten Version.

```bash
wget -O /mnt/user/appdata/twinraid/config/twinraid.yaml \
  https://raw.githubusercontent.com/mantua1/twinraid/v0.3.2/config.example.yaml
chmod 644 /mnt/user/appdata/twinraid/config/twinraid.yaml
```

**3.3 Konfiguration anpassen.** Öffne die Datei im Editor `nano`:

```bash
nano /mnt/user/appdata/twinraid/config/twinraid.yaml
```

Ersetze die Beispielwerte durch deine Werte aus Schritt 1:

- `web.allowed_networks`: Nur Geräte aus diesen Einträgen dürfen TwinRaid aufrufen. Ein Eintrag ist ein ganzes Netz (`192.168.20.0/24`) oder ein einzelnes Gerät (`192.168.10.50/32`). Beides lässt sich mischen. Siehe Kasten unten.
- **Die IP von TwinRaid trägst du hier nicht ein.** Sie steht nur im Unraid-Template (Schritt 4); TwinRaid erkennt sie beim Start selbst und verwendet sie für Zertifikat und Adressprüfung. Die Zeilen `# ip_addresses:` bleiben auskommentiert.
- `web.tls.hostnames`: leer lassen (`[]`), außer du hast für TwinRaid einen Namen im DNS eingerichtet.
- `web.port`: bleibt auskommentiert (Standard 8443, passend zum Template).
- `servers.source.host` und `servers.target.host`: IPs von Klein und Groß.
- `servers.*.webgui.scheme`: `http`, wenn bei dem Server *Use SSL/TLS* auf **No** steht; `https` bei **Yes**. Den Port kannst du weglassen (80 bzw. 443).
- `servers.target.mac` und `servers.target.wol_broadcast`.
- `gaming_vm.unraid_vm_name`: der Name der VM genau wie im Reiter **VMs**.
- `sync.shares`: deine Shares. Der Sync selbst kommt erst in einer späteren Version, die Werte werden aber schon geprüft.

Speichern mit `Strg+O`, dann `Enter`; beenden mit `Strg+X`.

> **Welche Netze eintragen?** Eine Router-Firewall wie die von UniFi filtert nur Verkehr **zwischen** Netzen, nicht **innerhalb** eines Netzes. Im Server-Netz, in dem TwinRaid selbst steht, stehen auch andere Container mit eigener IP und VMs direkt daneben. Die Router-Firewall sieht deren Zugriffe auf TwinRaid gar nicht. Trag im Server-Netz deshalb **nur einzelne Geräte als `/32`** ein, die TwinRaid wirklich aufrufen sollen (z. B. deinen Admin-PC). Andere Netze wie das Client-Netz kannst du **ganz** eintragen, weil dort die Firewall-Regel aus Schritt 6 den Zugriff regelt.
>
> ```yaml
>   allowed_networks:
>     - 192.168.10.50/32         # Server-Netz: nur der Admin-PC
>     - 192.168.20.0/24          # Client-Netz: ganz, die UniFi-Regel regelt den Zugriff
> ```
>
> Ein einzelnes Gerät **immer mit `/32`** schreiben (oder ganz ohne Maske). `192.168.10.50/24` ist ein Fehler: Die Maske meint das ganze Netz, TwinRaid startet dann nicht und nennt beide richtigen Schreibweisen.

> **Wichtig:** Einrückungen sind Leerzeichen, keine Tabulatoren. Uhrzeiten stehen in Anführungszeichen (`time: "03:00"`). Ist etwas falsch, startet der Container nicht und nennt im Log genau die Stelle.

---

## 4. Container installieren (zuerst im Simulationsmodus)

Im Simulationsmodus spricht TwinRaid keine echten Server an. So prüfst du zuerst in Ruhe Login, Zertifikat und Netzwerk.

**4.1 Template herunterladen** (Unraid-Terminal auf Klein):

```bash
wget -O /boot/config/plugins/dockerMan/templates-user/my-TwinRaid.xml \
  https://raw.githubusercontent.com/mantua1/twinraid/v0.3.2/docker/unraid-template.xml
```

**4.2 Container anlegen:**

1. Reiter **Docker** → unten **Add Container**.
2. Bei **Template** unter *User templates* den Eintrag **TwinRaid** wählen.
3. **Network Type:** `Custom: br0` (ist vorausgewählt).
4. **Fixed IP address:** die feste IP für TwinRaid aus Schritt 1. Das ist die einzige Stelle, an der die IP steht.
5. **Fixed MAC address** (direkt darunter, ab Unraid 7.3): die MAC aus Schritt 1. Auch sie steht nur hier.
6. **Betriebsmodus:** `mock` lassen.
7. **Konfiguration** und **Daten:** die vorgegebenen Pfade lassen (`/mnt/user/appdata/twinraid/config` und `…/data`).
8. **Apply** klicken.

Unraid übergibt Netzwerk, IP und MAC zusammen an Docker. In der Ausgabe nach **Apply** steht deshalb eine Zeile mit `--network='name=br0,ip=…,mac-address=…'`. Die MAC bitte **nicht** zusätzlich als `--mac-address` in die *Extra Parameters* schreiben; das Feld ist der vorgesehene Weg.

Die zusätzlichen Docker-Parameter (*Extra Parameters*, nur in der *Advanced View* sichtbar) härten den Container: schreibgeschütztes Dateisystem, nur fünf nötige Rechte, kein Rechteausbau. Bitte nicht ändern.

---

## 5. Erster Start: Log prüfen und Passwort setzen

**5.1 Log ansehen:** Reiter **Docker** → Symbol *TwinRaid* → **Logs**. Du solltest unter anderem sehen:

- einen deutlichen Hinweis **SIMULATION MODE ACTIVE**,
- `TLS address: 192.168.10.20 (detected on eth0)` mit deiner festen IP. Steht hier eine andere Adresse oder eine Warnung **PROBABLY THE WRONG NETWORK TYPE**, ist im Template nicht `Custom: br0` gewählt (siehe Abschnitt 12).
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

**5.4 MAC-Adresse prüfen, vor und nach einer Neuanlage.** So stellst du sicher, dass die MAC auch Updates übersteht. Im **Unraid-Terminal von Klein**:

1. Aktuelle MAC anzeigen:

   ```bash
   docker inspect --format '{{range .NetworkSettings.Networks}}{{.MacAddress}}{{end}}' TwinRaid
   ```

   Die Ausgabe muss genau die MAC aus Schritt 1 sein. (Alternativ zeigt der Reiter **Docker** in der **Advanced View** die MAC jedes Containers.)
2. Eine Neuanlage auslösen, wie sie auch bei jedem Update passiert: Reiter **Docker** → Symbol *TwinRaid* → **Edit** → ohne Änderung **Apply**. Unraid löscht den Container und legt ihn neu an.
3. Den Befehl aus 1. wiederholen. Die MAC muss **gleich** geblieben sein.
4. Zusätzlich einmal **Restart** (Symbol *TwinRaid* → **Restart**) und erneut vergleichen.

Hat sich die MAC geändert, ist das Feld **Fixed MAC address** leer oder die Unraid-Version älter als 7.3. Ohne feste MAC vergibt Docker bei jedem Start eine neue. Wiederhole den Vergleich nach dem ersten echten Update (Abschnitt 13).

---

## 6. UniFi: IP reservieren und Zugriff aus dem Client-Netz erlauben

**6.1 Feste IP per MAC reservieren.** TwinRaid nutzt kein DHCP; die IP steht fest im Template. Die Reservierung in UniFi sorgt dafür, dass der Router diese IP nie an ein anderes Gerät vergibt und TwinRaid immer als dasselbe Gerät führt.

1. UniFi: **Client Devices** → das Gerät mit der MAC aus Schritt 1 öffnen. (Steht es noch nicht in der Liste, zuerst TwinRaid einmal im Browser aufrufen, Schritt 7 Punkt 1, und dann hierher zurückkehren.)
2. **Settings** → **Fixed IP Address** einschalten, die feste IP für TwinRaid eintragen. Optional als Namen `TwinRaid` vergeben.
3. **Apply**.

**6.2 Firewall-Regel für das Client-Netz.** Aus dem Server-Netz dürfen nur die Geräte zugreifen, die in `web.allowed_networks` stehen (Schritt 3.3). Für das Client-Netz brauchst du eine Firewall-Regel. Es reicht genau ein Port.

**UniFi Network ab Version 9 (Zone-Based Firewall):** *Settings → Policy Engine → Zones → Create Policy*

- **Source:** Zone mit dem Client-Netz (bzw. das Client-Netz)
- **Destination:** Zone mit dem Server-Netz, **IP** = feste IP für TwinRaid, **Port** = `8443`
- **Protocol:** TCP
- **Action:** Allow

**Ältere UniFi-Versionen:** *Settings → Security → Traffic & Firewall Rules → Create Entry*, Typ *LAN In*, Aktion *Accept*, Quelle Client-Netz, Ziel IP-Adresse von TwinRaid, Port `8443`, Protokoll TCP. Die Regel muss vor einer Regel stehen, die den Verkehr zwischen den Netzen sperrt.

**Keine Portweiterleitung im Router einrichten.** TwinRaid ist nicht für das Internet gedacht.

---

## 7. Erste Anmeldung und CA-Zertifikat herunterladen

1. Im Browser `https://192.168.10.20:8443` aufrufen (deine feste IP). Das Gerät muss in `web.allowed_networks` stehen (Schritt 3.3), sonst zeigt der Browser nur `forbidden`.
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

**10.3 Dashboard prüfen:** Im Browser neu laden. Statt des gelben Banners steht oben **Nur Statusanzeige**. Die Kacheln zeigen innerhalb von etwa 10 Sekunden den echten Zustand. Unraid Klein steht immer auf **Online** mit dem Hinweis „TwinRaid läuft auf diesem Server“ (siehe Schritt 2). Ist Unraid Groß aus, steht bei der VM „Server offline“.

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
| Klein: **TwinRaid läuft auf diesem Server** | Normal im Echtbetrieb: TwinRaid läuft auf Klein und prüft Klein deshalb nicht | Nichts zu tun. „Host access to custom networks“ bleibt aus (Schritt 2) |
| Browser zeigt nur **forbidden** | Das Gerät steht nicht in `web.allowed_networks`; im Container-Log steht `Rejected request from … not in web.allowed_networks` | IP des Geräts eintragen (im Server-Netz einzeln als `/32`, Schritt 3.3) und den Container neu starten |
| Log: `… has host bits set` | Ein Eintrag in `allowed_networks` wie `192.168.10.50/24` | Einzelnes Gerät als `192.168.10.50/32`, ganzes Netz als `192.168.10.0/24` schreiben; die Meldung nennt beide Varianten |
| TwinRaid aus dem Unraid-Terminal von Klein nicht erreichbar (`ping`, `curl`) | Normal ohne „Host access to custom networks“ | Nichts zu tun; von anderen Geräten aus testen |
| MAC nach Update oder Neustart anders, UniFi zeigt ein neues Gerät | Feld **Fixed MAC address** im Template leer oder Unraid älter als 7.3 | MAC aus Schritt 1 im Template eintragen (Schritt 4.2), danach Schritt 5.4 |
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
| Browser warnt trotz installierter CA | Falsche Datei installiert, alte CA installiert oder Aufruf über eine andere Adresse | Fingerabdruck vergleichen; Aufruf genau über die Adresse, die unter **Einstellungen → Adresse** steht |
| Dashboard: **Neue TwinRaid-CA – auf allen Geräten neu installieren** | Die CA wurde ersetzt, meist weil im Template eine andere feste IP eingetragen wurde | Neues CA-Zertifikat herunterladen und auf jedem Gerät installieren (Schritt 8), das alte entfernen. Danach im Dashboard **Erledigt – Hinweis bestätigen** klicken. Der Hinweis bleibt bis dahin sichtbar. |
| Log: `No usable IPv4 address found` | Der Container hat keine eigene Adresse, z. B. Netzwerk „None“ | Im Template **Network Type** `Custom: br0` und **Fixed IP address** eintragen |
| Log: `Several usable IPv4 addresses found` | Der Container hängt an mehreren Netzwerken | Im Template nur `Custom: br0` verwenden; keine weiteren Netzwerke verbinden |
| Log: **PROBABLY THE WRONG NETWORK TYPE**, Adresse beginnt mit `172.` | Im Template ist `Bridge` statt `Custom: br0` gewählt; die Adresse ist nur innerhalb von Docker gültig | Im Template **Network Type** `Custom: br0` und **Fixed IP address** wählen. (Wer bewusst Bridge nutzt, trägt die IP von Unraid Klein in `web.tls.ip_addresses` ein.) |
| Container startet nicht | Fehler in Konfiguration oder Rechten | Ende des Container-Logs lesen; dort steht der Grund und meist der Befehl zur Reparatur. Bei `owned by uid 99` oder `writable by group or others`: unten „Rechte des Datenordners zurückgesetzt“ |
| Unraid kann das Image nicht laden (`denied` oder `unauthorized`) | Das Paket auf ghcr.io ist noch privat | Nur direkt nach der ersten Veröffentlichung möglich: Paket öffentlich stellen (README, Abschnitt „Release veröffentlichen“) |

### Rechte des Datenordners zurückgesetzt

Nach *New Permissions* oder *Docker Safe New Perms*, oder wenn der Ordner über eine Freigabe angelegt wurde, meldet das Container-Log zum Beispiel:

```text
/data is owned by uid 99, expected uid 0 (root). On the Unraid host run: chown root:root …
/data/secrets is owned by uid 99, expected uid 0. Remove it on the host or give it back to root (chown root).
```

So reparierst du es im **Unraid-Terminal von Klein**. Der Container darf dabei laufen oder gestoppt sein.

```bash
D=/mnt/user/appdata/twinraid/data
chown -R root:root "$D"
chmod 755 "$D"
# Die Datenbank gehört dem Web-Prozess (Benutzer 10001); gibt es sie noch nicht, meldet der Befehl nur „No such file“.
chown 10001:10001 "$D"/web/web.sqlite*
ls -ld "$D"
```

Danach den Container neu starten (Reiter **Docker** → *TwinRaid* → **Restart**). Die übrigen Rechte stellt TwinRaid beim Start selbst richtig ein (`secrets` und `tls` nur für root, `web` für die Gruppe des Web-Prozesses, Dateien `0600`). Das Log nennt jede Korrektur mit `Data directory: …`.

> **Waren die Dateien für alle lesbar?** Die Werkzeuge geben allen Benutzern Lesezugriff. Ist `appdata` als Netzwerkfreigabe für andere Personen erreichbar, sicherheitshalber neu erzeugen: eine neue CA mit `twinraid-admin renew-cert --new-ca` (danach auf den Geräten neu installieren), einen neuen API-Key auf Unraid Groß (Schritt 9) und ein neues Passwort mit `twinraid-admin set-password`.

**Abgemeldet nach 10 Minuten:** Ohne Klick endet die Sitzung nach `session_idle_minutes` (Standard 10). Ein offenes Dashboard zählt nicht als Aktivität; nur während ein Kopierjob läuft, bleibt die Sitzung mit offenem Dashboard bestehen (ab Phase 5). Den Wert kannst du in `twinraid.yaml` erhöhen (höchstens 720).

**Gesperrt nach falschen Passwörtern:** Nach 5 Fehlversuchen in 15 Minuten ist die Anmeldung von diesem Gerät 15 Minuten gesperrt. Sofort aufheben: `twinraid-admin unlock` in der Container-Konsole.

---

## 13. Updates einspielen und Herkunft prüfen

- Unraid prüft regelmäßig, ob es eine neue Version gibt. Im Reiter **Docker** steht dann bei TwinRaid **update ready**. Klick auf das Symbol → **Update**. Daten und Einstellungen bleiben erhalten, die MAC dank des Felds **Fixed MAC address** ebenso. Nach dem ersten Update einmal wie in Schritt 5.4 vergleichen.
- Das Template verwendet `ghcr.io/mantua1/twinraid:latest`. Dieser Tag zeigt immer auf das neueste Release. Wer eine Version festhalten möchte, trägt unter *Repository* zum Beispiel `ghcr.io/mantua1/twinraid:0.3` ein.
- Jedes Image wird ausschließlich von GitHub Actions aus diesem Repository gebaut und hat einen signierten Herkunftsnachweis. Prüfen kannst du ihn auf einem PC mit der GitHub-CLI (`gh`):

```bash
gh attestation verify oci://ghcr.io/mantua1/twinraid:0.3.2 --owner mantua1
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
