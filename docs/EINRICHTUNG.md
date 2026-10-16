# TwinRaid auf Unraid einrichten

> **Stand: Version 0.4.1.** Diese Version zeigt den Status beider Server und der Gaming-VM an und kann Unraid Groß wecken und herunterfahren sowie die Gaming-VM starten, herunterfahren und erzwingen. Die Aktionen werden einzeln freigeschaltet (Abschnitt 11). Der Sync folgt in einer späteren Version. Die Anleitung wird mit jeder Version erweitert und ist in Phase 7 fertig.

Diese Anleitung führt Schritt für Schritt durch die Installation auf **Unraid Klein** (dem stromsparenden Server, der immer läuft). Am Ende:

- läuft TwinRaid als Container mit eigener IP- und MAC-Adresse im Server-Netz,
- ist die Weboberfläche per HTTPS aus dem Server-Netz und dem Client-Netz erreichbar,
- vertrauen deine Geräte (Windows, Linux, Android) dem Zertifikat ohne Warnung,
- zeigt das Dashboard den echten Status von Unraid Klein, Unraid Groß und der Gaming-VM,
- kannst du Groß und die Gaming-VM aus dem Dashboard heraus starten und herunterfahren (Abschnitt 11).

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
11. [Aktionen einrichten und testen](#11-aktionen-einrichten-und-testen)
12. [Später: Unraid Groß auf HTTPS umstellen](#12-später-unraid-groß-auf-https-umstellen)
13. [Fehlerbilder und Abhilfe](#13-fehlerbilder-und-abhilfe)
14. [Updates einspielen und Herkunft prüfen](#14-updates-einspielen-und-herkunft-prüfen)
15. [Befehle der Admin-CLI](#15-befehle-der-admin-cli)

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
| MAC-Adresse von Unraid Groß | `AA:BB:CC:DD:EE:FF` | Unraid Groß: *Settings → Network Settings* (für Wake-on-LAN) |
| Broadcast-Adresse des Server-Netzes | `192.168.10.255` | Nur nötig, wenn Groß in einem anderen Netz liegt als TwinRaid. Sonst leitet TwinRaid sie selbst ab. Bei `/24` die letzte Adresse des Netzes |
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

> **Vorsicht mit „New Permissions“ und „Docker Safe New Perms“.** Das Unraid-Werkzeug *Tools → New Permissions* und *Docker Safe New Perms* aus dem Plugin *Fix Common Problems* setzen Besitzer und Rechte auf `nobody:users` und Schreibrechte für alle zurück. *New Permissions* tut das für alle gewählten Shares und Datenträger, auch für `appdata`. *Docker Safe New Perms* lässt `appdata` normalerweise aus, aber nur, solange es in seinen Einstellungen ausgenommen ist. Erwischt eines der Werkzeuge den Datenordner von TwinRaid, startet der Container danach nicht mehr. Wie du das reparierst, steht in Abschnitt 13 unter „Rechte des Datenordners zurückgesetzt“.

> **Hinweis zu `appdata`:** TwinRaid speichert eine kleine SQLite-Datenbank. Am zuverlässigsten liegt `appdata` auf einem Pool (z. B. Cache) mit der Share-Einstellung *Exclusive access*. Bei den Unraid-Standardeinstellungen ist das meist schon so.

**3.2 Beispielkonfiguration herunterladen.** Die Version im Pfad (`v0.4.1`) passt zur installierten Version.

```bash
wget -O /mnt/user/appdata/twinraid/config/twinraid.yaml \
  https://raw.githubusercontent.com/mantua1/twinraid/v0.4.1/config.example.yaml
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
- `servers.target.mac`: die MAC von Groß für Wake-on-LAN. `wol_broadcast` bleibt auskommentiert, solange Groß im selben Netz wie TwinRaid liegt; TwinRaid leitet die Broadcast-Adresse dann selbst ab.
- `gaming_vm.unraid_vm_name`: der Name der VM genau wie im Reiter **VMs**.
- `sync.shares`: deine Shares. Der Sync selbst kommt erst in einer späteren Version, die Werte werden aber schon geprüft.
- `actions`: alles auf `false` lassen. Die Aktionen schaltest du erst in Abschnitt 11 einzeln frei.

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
  https://raw.githubusercontent.com/mantua1/twinraid/v0.4.1/docker/unraid-template.xml
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
- `TLS address: 192.168.10.20 (detected on eth0)` mit deiner festen IP. Steht hier eine andere Adresse oder eine Warnung **PROBABLY THE WRONG NETWORK TYPE**, ist im Template nicht `Custom: br0` gewählt (siehe Abschnitt 13).
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

Hat sich die MAC geändert, ist das Feld **Fixed MAC address** leer oder die Unraid-Version älter als 7.3. Ohne feste MAC vergibt Docker bei jedem Start eine neue. Wiederhole den Vergleich nach dem ersten echten Update (Abschnitt 14).

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

> Für das Starten und Erzwingen der VM bekommt dieser Key später zusätzlich das Recht `VMS:UPDATE_ANY`. Das passiert erst in Abschnitt 11.5, **nach** dem Rechtetest mit dem Lese-Key (11.4).

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

Klappt es nicht, nennt der Befehl die Ursache, etwa einen falschen Key, fehlende Rechte oder einen falsch geschriebenen VM-Namen (dann listet er die vorhandenen VMs auf). Hilfe dazu in Abschnitt 13.

**10.3 Dashboard prüfen:** Im Browser neu laden. Statt des gelben Banners steht oben **Nur Statusanzeige**, solange keine Aktion freigeschaltet ist (Abschnitt 11). Die Kacheln zeigen innerhalb von etwa 10 Sekunden den echten Zustand. Unraid Klein steht immer auf **Online** mit dem Hinweis „TwinRaid läuft auf diesem Server“ (siehe Schritt 2). Ist Unraid Groß aus, steht bei der VM „Server offline“.

> **Hinweis bei `scheme: http`:** Solange Unraid Groß auf *Use SSL/TLS: No* steht, geht der API-Key unverschlüsselt durch das Server-Netz. TwinRaid zeigt das im Startlog und unter **Einstellungen → Verbindung zu Unraid Groß** an. Abschnitt 12 beschreibt die Umstellung auf HTTPS.

---

## 11. Aktionen einrichten und testen

Ab Version 0.4.0 kann TwinRaid Unraid Groß wecken und herunterfahren und die Gaming-VM starten, herunterfahren und erzwingen. Jede Aktion wird im Echtbetrieb **einzeln** in `twinraid.yaml` freigeschaltet. So probierst du sie Schritt für Schritt aus, ohne dass eine noch nicht geprüfte Aktion versehentlich ausgelöst wird.

```yaml
actions:
  wake: false        # Groß wecken (Wake-on-LAN)
  vm: false          # VM starten, herunterfahren, erzwingen
  shutdown: false    # Groß herunterfahren
```

| Aktion | Weg | braucht |
|---|---|---|
| Groß wecken | Magic Packet an die Broadcast-Adresse des Server-Netzes | `wake` |
| VM starten | Unraid-API (`VMS: Update (any)`); ist Groß aus, wird es vorher geweckt | `vm` (bei ausgeschaltetem Groß zusätzlich `wake`) |
| VM herunterfahren | SSH-Key „power“: `guard.sh` sendet nur das Signal zum Herunterfahren (wie der Netzschalter) | `vm` |
| VM erzwingen | Unraid-API, frühestens `shutdown_timeout_minutes` nach „Herunterfahren“ | `vm` |
| Groß herunterfahren | SSH-Key „power“: `guard.sh` ruft `/sbin/poweroff` auf (wie der Button in der Unraid-Oberfläche); läuft die VM, wird sie zuerst heruntergefahren | `shutdown` (bei laufender VM zusätzlich `vm`) |
| Erzwingen und danach Server herunterfahren (ab 0.4.1) | nur bis zu 10 Minuten nach einem Herunterfahren von Groß, das an der VM gescheitert ist: VM über die API erzwingen, dann Groß wie oben herunterfahren | `vm` und `shutdown` |

Nach jeder Änderung an `twinraid.yaml` den Container neu starten (Reiter **Docker** → *TwinRaid* → **Restart**). Jede Aktion steht im Aktionslog des Workers; anzeigen in der **Container-Konsole** mit `twinraid-admin show-action-log`.

> **Warum fährt TwinRaid die VM per SSH herunter und nicht über die API?** Der Befehl `vm.stop` der Unraid-API wartet nur 10 Sekunden und schaltet die VM danach selbst hart aus. Windows könnte so mitten im Herunterfahren abgeschaltet werden, etwa während eines Updates. Über SSH sendet TwinRaid nur das Signal und wartet selbst die eingestellte Zeit ab; erst danach bietet es „Erzwingen“ an.

### 11.1 Vorbereitung auf Unraid Groß

**Wake-on-LAN im BIOS.** Groß neu starten und das BIOS/UEFI öffnen (meist mit `Entf` oder `F2` beim Start). Die Bezeichnungen hängen vom Hersteller ab:

- **Wake on LAN** bzw. **Power On By PCI-E**, **Resume by PCI-E Device** oder **PME Event Wake Up**: **an**.
- **ErP Ready** bzw. **EuP 2013**: **aus**. Sonst bekommt die Netzwerkkarte im ausgeschalteten Zustand keinen Strom.
- Falls vorhanden **Deep Sleep**: **aus**.

**Wake-on-LAN für die Netzwerkkarte.** Im **Unraid-Terminal von Groß**:

```bash
ethtool eth0 | grep -i wake-on
```

Erwartet sind zwei Zeilen, zum Beispiel:

```text
	Supports Wake-on: pumbg
	Wake-on: g
```

Die erste Zeile muss ein `g` enthalten (die Karte kann Wake-on-LAN). Steht in der zweiten Zeile `d` (aus), schaltest du es ein und trägst es dauerhaft ein, damit es jeden Neustart übersteht. Unraid führt die Datei `/boot/config/go` bei jedem Start aus:

```bash
ethtool -s eth0 wol g
nano /boot/config/go
```

In `nano` ans Ende diese Zeile anfügen, dann `Strg+O`, `Enter`, `Strg+X`:

```bash
/usr/sbin/ethtool -s eth0 wol g
```

`eth0` ist die physische Netzwerkkarte, nicht `br0` oder `bond0`. Hast du mehrere Karten, ist es die, deren Kabel zum Switch geht (*Settings → Network Settings*). Nach dem nächsten Neustart von Groß zeigt `ethtool eth0 | grep -i wake-on` wieder `Wake-on: g`.

> Nach einem Stromausfall reagieren manche Mainboards erst nach dem nächsten normalen Start wieder auf Wake-on-LAN. Das lässt sich im BIOS oft unter **Restore on AC Power Loss** einstellen.

**Array automatisch starten.** *Settings → Disk Settings* → **Enable auto start:** `Yes`. VMs gibt es erst, wenn das Array läuft. Ohne Auto-Start wartet „VM starten“ nach dem Wecken vergeblich und meldet nach `boot_timeout_minutes` „nicht rechtzeitig bereit“. Ebenso muss unter *Settings → VM Manager* **Enable VMs** auf `Yes` stehen.

**Windows-VM: Netzschalter auf „Herunterfahren“.** In der Gaming-VM unter Windows: *Systemsteuerung → Energieoptionen → Auswählen, was beim Drücken des Netzschalters geschehen soll* → **Beim Drücken des Netzschalters:** **Herunterfahren** (für *Netzbetrieb*) → **Änderungen speichern**. Sonst reagiert die VM nicht auf das Signal von TwinRaid und fährt nicht herunter.

**SSH auf Groß einschalten und `guard.sh` ablegen.**

1. *Settings → Management Access* → **Use SSH:** `Yes`, Port `22` lassen → **Apply**. (Bei einem anderen Port diesen in `twinraid.yaml` als `servers.target.ssh_port` eintragen.)
2. Im **Unraid-Terminal von Groß** das Wächter-Skript holen (die Version im Pfad passt zur installierten TwinRaid-Version):

   ```bash
   mkdir -p /boot/config/twinraid
   wget -O /boot/config/twinraid/guard.sh \
     https://raw.githubusercontent.com/mantua1/twinraid/v0.4.1/host-scripts/guard.sh
   head -3 /boot/config/twinraid/guard.sh
   ```

   Die ersten beiden Zeilen lauten `#!/bin/bash` und `# TwinRaid: SSH-Wächter auf Unraid Groß (Konzept 5.6).` Ausführbar machen ist nicht nötig (und auf dem USB-Stick auch nicht möglich): Unraid ruft das Skript über `/bin/bash` auf.

> **Was darf der SSH-Key „power“?** Nur drei Dinge: einen Verbindungstest (`check`), Groß herunterfahren (`/sbin/poweroff`) und eine VM per Signal herunterfahren (`virsh shutdown` mit der ID der VM). Alles andere lehnt `guard.sh` ab und schreibt es ins Syslog von Groß (*Tools → System Log*, Kennung `twinraid-guard`). Der Key gilt nur von der IP von TwinRaid aus, ohne Terminal und ohne Weiterleitungen.

### 11.2 Optional: mit einer Test-VM üben

Für die VM-Tests kannst du statt der Gaming-VM vorübergehend eine Test-VM verwenden. Dafür ist keine neue TwinRaid-Version nötig: TwinRaid sucht die VM bei jeder Aktion über ihren Namen, und `guard.sh` arbeitet mit jeder VM.

1. In `twinraid.yaml` bei `gaming_vm` den Namen der Test-VM eintragen, genau wie im Reiter **VMs** von Groß. Für kürzere Wartezeiten zusätzlich die Wartezeit auf 1 Minute setzen:

   ```yaml
   gaming_vm:
     display_name: "Gaming-VM"
     unraid_vm_name: "Test-VM"
     shutdown_timeout_minutes: 1
   ```

2. Container neu starten. Die VM-Kachel zeigt jetzt den Zustand der Test-VM.

Für den Test von **Erzwingen** brauchst du eine VM, die auf das Signal zum Herunterfahren **nicht** reagiert. Am einfachsten stellst du in einer Windows-Test-VM den Netzschalter vorübergehend auf **Nichts unternehmen** (siehe 11.1). Alternativ reagiert eine VM ohne Betriebssystem, die in der UEFI-Shell steht, in der Regel ebenfalls nicht.

Am Ende (11.15) trägst du die Gaming-VM wieder ein.

### 11.3 Testprotokoll: Ausgangslage

TwinRaid 0.4.0 läuft im Echtbetrieb, `actions` fehlt in `twinraid.yaml` oder steht überall auf `false`. Die vorhandene Konfiguration bleibt gültig. Steht dort `wol_broadcast`, kannst du die Zeile löschen; TwinRaid leitet die Adresse dann selbst ab.

| Prüfen | Erwartet |
|---|---|
| Container-Log | `Read-only real mode: status checks only, all actions are locked.` und `Wake-on-LAN broadcast address: 192.168.10.255 (…)` |
| Dashboard | Banner **Nur Statusanzeige**; die Buttons sind grau mit dem Hinweis „in twinraid.yaml unter actions nicht freigeschaltet“ |
| **Einstellungen → Aktionen** | alle drei Gruppen „gesperrt“ |

### 11.4 Test 1: Rechtetest mit dem Lese-Key

Der API-Key aus Schritt 9 darf nur VMs lesen. Bevor du ihn erweiterst, prüfst du, dass Unraid einen solchen Key beim Starten einer VM wirklich abweist (in älteren Versionen der Unraid-API gab es einen Fehler, durch den ein lesender Key VMs starten konnte).

1. In `twinraid.yaml` eintragen und den Container neu starten:

   ```yaml
   actions:
     vm: true
   ```

2. Im Container-Log steht `Real mode: these actions are enabled in twinraid.yaml: vm`. Das Banner **Nur Statusanzeige** ist weg.
3. Groß ist an, die (Test-)VM ist aus. In der VM-Kachel **Starten** klicken.

| Erwartet | |
|---|---|
| VM-Kachel | kurz **Startet …**, dann rot: „Zuletzt: VM starten – Keine Berechtigung: Der API-Key darf VMs nicht starten oder ausschalten. In Unraid die Berechtigung VMS: Update (any) ergänzen.“ |
| Unraid Groß, Reiter **VMs** | die VM ist weiterhin **aus** |
| Kachel **Verlauf** | „VM starten“ mit dem Ergebnis „angefordert“ |
| `twinraid-admin show-action-log` | `vm_start  dashboard: started` und danach `vm_start  dashboard: api_forbidden` |

> **Startet die VM trotzdem, hier aufhören.** Dann setzt die Unraid-API die Rechte nicht durch. Die VM in Unraid wieder ausschalten, `vm: false` setzen und das Ergebnis melden.

### 11.5 Test 2: API-Key für Starten und Erzwingen erweitern

**Variante A: vorhandenen Key ergänzen** (Unraid-Oberfläche von Groß): *Settings → Management Access* → Bereich **API Keys** → Key **TwinRaid Status** → **Edit** → bei **Permissions** für die Ressource **VMS** zusätzlich **Update (any)** auswählen, **Read (any)** bleibt → speichern. Der Key selbst ändert sich nicht; in TwinRaid ist nichts zu tun.

**Variante B: neuen Key anlegen** (Unraid-Terminal von Groß):

```bash
unraid-api apikey --create --name "TwinRaid" \
  --permissions "VMS:READ_ANY,VMS:UPDATE_ANY" --description "TwinRaid: VMs lesen, starten, erzwingen" --json
```

Den Wert hinter `"key"` mit `twinraid-admin set-api-key target` in TwinRaid hinterlegen (wie in 10.2) und den alten Key in Unraid löschen.

> **Unraid-Eigenheit beim Speichern:** Ein Key nur mit einzelnen Rechten und ohne Rolle lässt sich manchmal erst im zweiten Versuch speichern. Die CLI meldet dann `Failed to save API key: Invalid data structure`, die Oberfläche einmal „At least one role or one permission is required“. **Einfach erneut versuchen** (Seite neu laden, Rechte erneut auswählen). **Nie auf die Rolle *Viewer* oder *Admin* ausweichen:** *Viewer* darf fast alles auf dem Server lesen, *Admin* alles ändern.

**Prüfen:** In der VM-Kachel **Starten** klicken. Erwartet: **Startet …**, dann **Online** und „Zuletzt: VM starten – erfolgreich“. In Unraid läuft die VM.

### 11.6 Test 3: SSH-Key „power“ einrichten

Das Herunterfahren der VM und von Groß läuft über SSH. Deshalb kommt der SSH-Key vor den nächsten Tests.

1. In der **Container-Konsole**:

   ```bash
   twinraid-admin init-ssh-keys
   ```

   Die Ausgabe enthält **eine** lange Zeile, die so beginnt (mit deiner IP von TwinRaid):

   ```text
   restrict,from="192.168.10.20",command="/bin/bash /boot/config/twinraid/guard.sh power" ssh-ed25519 AAAA… twinraid-power
   ```

   Die ganze Zeile markieren und kopieren. Sie enthält nur den öffentlichen Teil des Keys; der private bleibt in TwinRaid. Rufst du den Befehl erneut auf, erscheint dieselbe Zeile.
2. In der Unraid-Oberfläche von **Groß**: *Users* → **root** → Feld **SSH authorized keys**. Die Zeile in eine **neue Zeile** einfügen (vorhandene Zeilen bleiben stehen) → **Save**. Unraid speichert sie dauerhaft auf dem USB-Stick (`/boot/config/ssh/root/authorized_keys`).
3. Im **Unraid-Terminal von Groß** den Fingerabdruck des SSH-Servers anzeigen:

   ```bash
   ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub
   ```

   Ausgabe zum Beispiel `256 SHA256:Xy3…Qk root@Gross (ED25519)`. Den Teil `SHA256:…` brauchst du gleich.
4. In der **Container-Konsole**:

   ```bash
   twinraid-admin trust-host-key target
   ```

   TwinRaid zeigt `ED25519 SHA256:…`. Mit Schritt 3 vergleichen; nur wenn beide gleich sind, `yes` eingeben.

| Erwartet | |
|---|---|
| Ausgabe | `Host key confirmed. From now on TwinRaid only accepts exactly this key.` und `OK: guard.sh answers (twinraid-guard 1 power ok).` |
| Unraid Groß | nichts passiert; der Test verändert nichts |

Meldet der Befehl einen Fehler, steht die Abhilfe dabei, zum Beispiel bei abgelehntem Key (`Users → root`), ausgeschaltetem SSH oder fehlendem `guard.sh`. Siehe auch Abschnitt 13.

### 11.7 Test 4: VM starten, herunterfahren und erzwingen

1. **Starten** (VM ist aus): **Startet …** → **Online**, „Zuletzt: VM starten – erfolgreich“.
2. **Herunterfahren** klicken. Es erscheint die Frage „… herunterfahren?“ mit der Wartezeit → **Herunterfahren**.

   | Erwartet | |
   |---|---|
   | VM-Kachel | **Fährt herunter …** („Die VM fährt herunter …“), dann **Offline**, „Zuletzt: VM herunterfahren – erfolgreich“ |
   | Unraid Groß, *Tools → System Log* | `twinraid-guard: ACPI shutdown of VM … requested by TwinRaid` |

3. **Erzwingen:** Die VM darf jetzt nicht auf das Signal reagieren (siehe 11.2). VM **Starten**, dann **Herunterfahren** und bestätigen.

   | Erwartet | |
   |---|---|
   | sofort | **Fährt herunter …** und ein grauer Button **Erzwingen** mit „Erzwingen: möglich ab hh:mm:ss“ |
   | nach `shutdown_timeout_minutes` | rot: „Zuletzt: VM herunterfahren – Die VM hat sich nicht rechtzeitig heruntergefahren.“ und darunter „„Erzwingen“ ist jetzt möglich.“; **Erzwingen** ist aktiv. „Erzwingen und danach Server herunterfahren“ erscheint hier **nicht** (nur nach einem Herunterfahren von Groß) |
   | **Erzwingen** → **Jetzt erzwingen** | die VM ist sofort **Offline**, „Zuletzt: VM erzwingen – erfolgreich“ |

4. In der Test-VM den Netzschalter wieder auf **Herunterfahren** stellen.

### 11.8 Test 5: Unraid Groß herunterfahren

1. In `twinraid.yaml` zusätzlich `shutdown: true` eintragen, Container neu starten.
2. Die VM ist aus. In der Kachel **Unraid Groß** **Herunterfahren** klicken → Frage „Unraid Groß herunterfahren?“ → **Herunterfahren**.

| Erwartet | |
|---|---|
| Kachel Groß | **Fährt herunter …** („Der Server fährt herunter …“), nach ein bis drei Minuten **Offline** mit „Nicht erreichbar“ und „Zuletzt: Server herunterfahren – erfolgreich“ |
| Unraid Groß | ist aus |
| beim nächsten Start von Groß (Test 6) | Unraid startet **keine** Paritätsprüfung wegen eines unsauberen Herunterfahrens (Reiter **Main**) |

### 11.9 Test 6: Unraid Groß wecken

1. In `twinraid.yaml` zusätzlich `wake: true` eintragen, Container neu starten.
2. Groß ist aus. In der Kachel **Unraid Groß** **Wecken** klicken (ohne Rückfrage).

| Erwartet | |
|---|---|
| Kachel Groß | **Startet …** („Magic Packet gesendet, der Server startet …“), dann „Wartet, bis Unraid bereit ist …“, schließlich **Online** und „Zuletzt: Server wecken – erfolgreich“ |
| Kachel VM | während des Starts „Wartet, bis der Server bereit ist“, danach **Offline** |

Steht nach `boot_timeout_minutes` „Der Server war nicht rechtzeitig bereit“: Ist Groß gar nicht angegangen, die Wake-on-LAN-Vorbereitung aus 11.1 prüfen. Ist Groß an, aber das Array nicht gestartet, den Auto-Start prüfen; dauert der Start einfach länger, `boot_timeout_minutes` erhöhen.

### 11.10 Test 7: VM starten, während Groß aus ist

1. Groß herunterfahren wie in Test 5 und warten, bis die Kachel **Offline** zeigt.
2. In der VM-Kachel **Starten** klicken. Der Button ist aktiv, weil Wecken freigeschaltet ist.

| Erwartet | |
|---|---|
| Kachel Groß | **Startet …** bis Groß bereit ist |
| Kachel VM | **Startet …** mit „Wartet, bis der Server bereit ist“, dann „Die VM wird gestartet …“, schließlich **Online** |
| beide Kacheln | „Zuletzt: VM starten – erfolgreich“ |
| `twinraid-admin show-action-log` | `vm_start  dashboard: started`, `wake_target  automatic (part of vm_start): started` und `… success`, `vm_start  dashboard: success` |

### 11.11 Test 8: Groß herunterfahren, während die VM läuft

1. Die VM läuft (aus Test 7). In der Kachel **Unraid Groß** **Herunterfahren** klicken.
2. Die Frage lautet jetzt **„VM zuerst herunterfahren?“**: „… läuft. TwinRaid fährt zuerst die VM herunter und danach Unraid Groß …“ → **VM und Server herunterfahren**.

| Erwartet | |
|---|---|
| Kachel VM | **Fährt herunter …**, dann **Offline** |
| Kachel Groß | danach **Fährt herunter …**, dann **Offline** |
| `twinraid-admin show-action-log` | `vm_stop  automatic (part of shutdown_target): success` und `shutdown_target  dashboard: success` |

Reagiert die VM nicht (zum Beispiel weil Windows gerade Updates installiert), bricht TwinRaid nach `shutdown_timeout_minutes` ab: Groß bleibt **an**, die Meldung lautet „Die VM hat sich nicht rechtzeitig heruntergefahren.“ In der VM-Kachel sind dann **Erzwingen** und, 10 Minuten lang, **Erzwingen und danach Server herunterfahren** möglich (Test 9).

### 11.12 Test 9: Erzwingen und danach Server herunterfahren

Ab Version 0.4.1. Du brauchst die Test-VM, die nicht auf das Herunterfahren reagiert (siehe 11.2), `actions.vm` und `actions.shutdown`.

1. Test-VM **Starten**, bis sie **Online** ist.
2. In der Kachel **Unraid Groß** **Herunterfahren** → **VM und Server herunterfahren**.
3. Nach `shutdown_timeout_minutes` bricht TwinRaid ab.

   | Erwartet | |
   |---|---|
   | Kachel Groß | **Online**; rot „Zuletzt: Server herunterfahren – Die VM hat sich nicht rechtzeitig heruntergefahren.“, darunter „In der Kachel <Name der VM>: „Erzwingen und danach Server herunterfahren“ ist bis hh:mm möglich.“ |
   | Kachel VM | **Online**; Buttons **Erzwingen** und **Erzwingen und danach Server herunterfahren**, darunter „… gültig bis hh:mm“ (10 Minuten nach dem Abbruch) |

4. **Erzwingen und danach Server herunterfahren** klicken. Der Dialog warnt, dass Erzwingen für die VM wie Stecker ziehen ist. Zuerst **Abbrechen**: nichts passiert, die Buttons bleiben. Dann erneut klicken und **VM erzwingen und Server herunterfahren** bestätigen.

   | Erwartet | |
   |---|---|
   | Kachel VM | sofort **Fährt herunter …** („Die VM wird ausgeschaltet …“), dann **Offline** |
   | Kachel Groß | danach **Fährt herunter …**, dann **Offline**; „Zuletzt: VM erzwingen und Server herunterfahren – erfolgreich“ |
   | `twinraid-admin show-action-log` | `shutdown_target_force_vm  dashboard: started`, `vm_force_stop  automatic (part of shutdown_target_force_vm): success`, `shutdown_target  automatic (part of shutdown_target_force_vm): success`, `shutdown_target_force_vm  dashboard: success` |
   | Kachel **Verlauf** | „VM erzwingen und Server herunterfahren“ mit dem Ergebnis „angefordert“ |

### 11.13 Test 10: Die Option verfällt

Groß wieder **Wecken**. Für jede Variante zuerst wie in Test 9, Schritte 1 bis 3, ein Herunterfahren an der Test-VM scheitern lassen; der Button **Erzwingen und danach Server herunterfahren** ist dann da.

| Variante | Erwartet |
|---|---|
| **Ablauf:** 10 Minuten warten | Der Button verschwindet. In beiden Kacheln steht statt des Hinweises auf die Option jetzt „„Erzwingen“ ist jetzt möglich.“, denn das einfache **Erzwingen** bleibt möglich |
| **Andere Aktion:** in der VM-Kachel nur **Erzwingen** | Die VM ist aus, der Button ist weg. Die Kachel Groß zeigt „Zuletzt: Server herunterfahren – Die VM hat sich nicht rechtzeitig heruntergefahren.“ grau und **ohne** Hinweis auf „Erzwingen“ (das war der Fehler aus 0.4.0) |
| **Zustand ändert sich:** die Test-VM in Unraid selbst ausschalten (Reiter **VMs** → Symbol der VM → **Force Stop**; auf „Stop“ reagiert sie ja nicht) | Nach etwa 10 Sekunden ist die VM **Offline** und der Button weg; er kommt auch nicht wieder, wenn du die VM erneut startest |
| **Neustart:** den Container neu starten (Reiter **Docker** → *TwinRaid* → **Restart**) | Nach dem Neustart gibt es den Button nicht mehr; das Ergebnis des Abbruchs ist ebenfalls weg |

Klickt jemand den Button genau in dem Moment, in dem das Angebot verfällt, lehnt TwinRaid ab: „Nicht ausgeführt: nur kurz nach einem an der VM gescheiterten Herunterfahren des Servers möglich.“ Die Meldung verschwindet mit der nächsten Statusänderung.

### 11.14 Test 8 zur Kontrolle wiederholen

Mit der Gaming-VM (Netzschalter auf **Herunterfahren**, siehe 11.1) Test 8 aus 11.11 wiederholen. Erwartet wie dort: erst die VM **Offline**, dann Groß **Offline**, „Zuletzt: Server herunterfahren – erfolgreich“. Die Option „Erzwingen und danach Server herunterfahren“ erscheint dabei **nicht**.

### 11.15 Abschluss

1. In `twinraid.yaml` wieder die Gaming-VM eintragen (`unraid_vm_name`) und `shutdown_timeout_minutes` auf den gewünschten Wert setzen (Standard 3), Container neu starten.
2. Mit der Gaming-VM einmal **Starten** und **Herunterfahren**.
3. `twinraid-admin show-action-log` ansehen: Jede Aktion steht dort mit Beginn und Ergebnis.
4. Die Ergebnisse bitte im Pull Request festhalten (für 0.4.0 besonders Test 1, für 0.4.1 die Tests 9 und 10).

---

## 12. Später: Unraid Groß auf HTTPS umstellen

Dafür ist keine neue TwinRaid-Version nötig, nur Einstellungen.

**12.1 In Unraid Groß:** *Settings → Management Access* → **Use SSL/TLS** auf **Yes** → **Apply**. Die Unraid-Oberfläche lädt neu und ist danach per `https://` erreichbar; der Browser warnt, weil Unraid ein selbstsigniertes Zertifikat nutzt. (**Strict** wird von TwinRaid nicht unterstützt.)

**12.2 Fingerabdruck von Unraid Groß ermitteln** (zum Vergleich in 11.4). Am sichersten direkt auf dem Server, im **Unraid-Terminal von Groß**:

```bash
ls /boot/config/ssl/certs/
```

Es erscheint eine Datei, deren Name auf `_unraid_bundle.pem` endet, zum Beispiel `Tower_unraid_bundle.pem` (der erste Teil ist der Servername). Diesen Namen im nächsten Befehl einsetzen:

```bash
openssl x509 -in /boot/config/ssl/certs/Tower_unraid_bundle.pem -noout -fingerprint -sha256
```

Alternativ im Browser: `https://<IP von Groß>` öffnen, auf die Warnung bzw. das Schloss klicken, Zertifikat anzeigen, **SHA-256-Fingerabdruck** ablesen.

**12.3 In TwinRaid:** In `twinraid.yaml` (Unraid-Terminal von Klein, `nano /mnt/user/appdata/twinraid/config/twinraid.yaml`) bei `servers.target.webgui` eintragen:

```yaml
    webgui:
      scheme: https
      port: 443
```

Speichern und den Container neu starten (Reiter **Docker** → *TwinRaid* → **Restart**). Bis zum nächsten Schritt zeigt die VM-Kachel „Zertifikat von Unraid noch nicht bestätigt“.

**12.4 Zertifikat bestätigen:** In der **Container-Konsole**:

```bash
twinraid-admin trust-api-cert target
```

Der Befehl zeigt den Fingerabdruck, den Unraid Groß gerade ausliefert. Vergleiche ihn mit 12.2. Nur wenn er gleich ist, `yes` eingeben. Danach folgt wieder die Testabfrage. Unter **Einstellungen → Verbindung zu Unraid Groß** steht jetzt „Verschlüsselt“.

**Wenn Unraid sein Zertifikat später erneuert** (zum Beispiel nach einer Neuinstallation oder einer Änderung des Servernamens), zeigt die VM-Kachel **„Zertifikat geändert“**. TwinRaid sendet den API-Key dann nicht mehr. Prüfe den neuen Fingerabdruck wie in 12.2 und bestätige ihn wie in 12.4 mit `twinraid-admin trust-api-cert target`. Hast du das Zertifikat nicht selbst geändert, bestätige nichts und finde zuerst heraus, warum es anders ist.

---

## 13. Fehlerbilder und Abhilfe

| Anzeige im Dashboard | Bedeutung | Abhilfe |
|---|---|---|
| Klein: **TwinRaid läuft auf diesem Server** | Normal im Echtbetrieb: TwinRaid läuft auf Klein und prüft Klein deshalb nicht | Nichts zu tun. „Host access to custom networks“ bleibt aus (Schritt 2) |
| Browser zeigt nur **forbidden** | Das Gerät steht nicht in `web.allowed_networks`; im Container-Log steht `Rejected request from … not in web.allowed_networks` | IP des Geräts eintragen (im Server-Netz einzeln als `/32`, Schritt 3.3) und den Container neu starten |
| Log: `… has host bits set` | Ein Eintrag in `allowed_networks` wie `192.168.10.50/24` | Einzelnes Gerät als `192.168.10.50/32`, ganzes Netz als `192.168.10.0/24` schreiben; die Meldung nennt beide Varianten |
| TwinRaid aus dem Unraid-Terminal von Klein nicht erreichbar (`ping`, `curl`) | Normal ohne „Host access to custom networks“ | Nichts zu tun; von anderen Geräten aus testen |
| MAC nach Update oder Neustart anders, UniFi zeigt ein neues Gerät | Feld **Fixed MAC address** im Template leer oder Unraid älter als 7.3 | MAC aus Schritt 1 im Template eintragen (Schritt 4.2), danach Schritt 5.4 |
| Groß: **Nicht erreichbar**, VM: **Server offline** | Unraid Groß ist aus oder nicht erreichbar | Normal, wenn Groß aus ist. Sonst IP und `webgui` in `twinraid.yaml` prüfen |
| VM: **API nicht erreichbar** | Groß antwortet, die API aber nicht | `webgui.scheme` und `port` prüfen; Groß gerade erst gestartet? Kurz warten |
| VM: **Unraid leitet auf HTTPS um** | Groß steht auf *Use SSL/TLS: Yes*, TwinRaid nutzt `http` | Abschnitt 12.3 und 12.4 |
| VM: **Kein API-Key gesetzt** | Schritt 10.2 fehlt | `twinraid-admin set-api-key target` |
| VM: **API-Key ungültig** | Key falsch kopiert oder in Unraid gelöscht | Neuen Key anlegen (Schritt 9) und neu setzen |
| VM: **API-Key hat keine Leserechte für VMs** | Key ohne `VMS: Read (any)` | Rechte in Unraid ergänzen oder neuen Key anlegen |
| VM: **Zertifikat von Unraid noch nicht bestätigt** | `https` eingestellt, Fingerabdruck fehlt | Abschnitt 12.4 |
| VM: **Zertifikat geändert** | Unraid liefert ein anderes Zertifikat als bestätigt | Abschnitt 12, letzter Absatz |
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
| Buttons grau mit „in twinraid.yaml unter actions nicht freigeschaltet“ | Aktion im Echtbetrieb noch nicht freigeschaltet | In `twinraid.yaml` unter `actions` freischalten (Abschnitt 11) und den Container neu starten |
| „Keine Berechtigung: Der API-Key darf VMs nicht starten oder ausschalten …“ | Der Key hat nur `VMS: Read (any)` | Abschnitt 11.5: `VMS: Update (any)` ergänzen |
| „Kein SSH-Key „power““ | Schritt 11.6 fehlt | `twinraid-admin init-ssh-keys` und Zeile auf Groß eintragen |
| „Der SSH-Hostkey des Servers ist nicht bestätigt“ | Hostkey von Groß noch nicht bestätigt | `twinraid-admin trust-host-key target` (11.6) |
| „Der SSH-Hostkey des Servers hat sich geändert“ | Groß zeigt einen anderen SSH-Hostkey als bestätigt; TwinRaid sendet nichts mehr | Hast du Groß neu eingerichtet oder den USB-Stick ersetzt, den Fingerabdruck wie in 11.6 Schritt 3 prüfen und neu bestätigen. Sonst nichts bestätigen und herausfinden, warum er anders ist |
| „Der Server lehnt den SSH-Key ab“ | Zeile fehlt unter *Users → root → SSH authorized keys*, ist abgeschnitten, oder TwinRaid hat eine andere IP als in `from=` | `twinraid-admin init-ssh-keys` erneut ausführen (gibt die Zeile wieder aus) und die Zeile auf Groß ersetzen |
| „SSH auf dem Server ist nicht erreichbar“ | SSH auf Groß aus oder anderer Port | *Settings → Management Access → Use SSH* einschalten; abweichenden Port als `servers.target.ssh_port` eintragen |
| „guard.sh auf dem Server hat den Befehl abgelehnt“ | `guard.sh` auf Groß ist veraltet | `guard.sh` wie in 11.1 neu holen (Version im Pfad = installierte Version) |
| „Der Befehl ist auf dem Server fehlgeschlagen“ | `guard.sh` fehlt unter `/boot/config/twinraid/guard.sh`, oder `virsh` meldet einen Fehler | Ablage von `guard.sh` prüfen (11.1); Syslog von Groß ansehen (*Tools → System Log*, `twinraid-guard`) |
| „Der Server war nicht rechtzeitig bereit“ | Groß ist nach dem Wecken nicht (rechtzeitig) gestartet, oder das Array startet nicht automatisch | Wake-on-LAN im BIOS und mit `ethtool` prüfen, Auto-Start des Arrays einschalten (11.1); bei langem Start `boot_timeout_minutes` erhöhen |
| „Die VM hat sich nicht rechtzeitig heruntergefahren“ | Die VM reagiert nicht auf das Signal (Netzschalter-Einstellung in Windows, laufende Updates) | Netzschalter auf „Herunterfahren“ stellen (11.1), `shutdown_timeout_minutes` erhöhen oder **Erzwingen**; nach einem gescheiterten Herunterfahren von Groß 10 Minuten lang auch **Erzwingen und danach Server herunterfahren** (11.12) |
| Meldung grau, ohne Hinweis darunter | Das Ergebnis ist nur noch Verlauf: Der Hinweis (z. B. auf „Erzwingen“) trifft nicht mehr zu, etwa weil die VM inzwischen aus ist | Nichts zu tun |
| Herunterfahren gesperrt: „der Zustand der VM ist unbekannt“ | TwinRaid kann die VM über die API gerade nicht lesen | Ursache in der VM-Kachel beheben (z. B. API-Key) oder Groß ausnahmsweise über die Unraid-Oberfläche herunterfahren |
| „Der Server ist nach 15 Minuten noch erreichbar“ | Groß hängt beim Herunterfahren | Unraid-Oberfläche von Groß ansehen; *Disk Settings → Shutdown time-out* prüfen |
| Log: `cannot derive the Wake-on-LAN broadcast address`, Container startet nicht | Wecken ist freigeschaltet, aber Groß liegt nicht im Netz von TwinRaid | `servers.target.wol_broadcast` eintragen (Broadcast-Adresse des Netzes von Groß) |
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

## 14. Updates einspielen und Herkunft prüfen

- Unraid prüft regelmäßig, ob es eine neue Version gibt. Im Reiter **Docker** steht dann bei TwinRaid **update ready**. Klick auf das Symbol → **Update**. Daten und Einstellungen bleiben erhalten, die MAC dank des Felds **Fixed MAC address** ebenso. Nach dem ersten Update einmal wie in Schritt 5.4 vergleichen.
- Das Template verwendet `ghcr.io/mantua1/twinraid:latest`. Dieser Tag zeigt immer auf das neueste Release. Wer eine Version festhalten möchte, trägt unter *Repository* zum Beispiel `ghcr.io/mantua1/twinraid:0.3` ein.
- Jedes Image wird ausschließlich von GitHub Actions aus diesem Repository gebaut und hat einen signierten Herkunftsnachweis. Prüfen kannst du ihn auf einem PC mit der GitHub-CLI (`gh`):

```bash
gh attestation verify oci://ghcr.io/mantua1/twinraid:0.4.1 --owner mantua1
```

Steht am Ende `Verification succeeded!`, stammt das Image nachweislich aus diesem Repository.

---

## 15. Befehle der Admin-CLI

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
| `twinraid-admin init-ssh-keys` | SSH-Key „power“ erzeugen (falls noch keiner da ist) und die Zeile für *Users → root → SSH authorized keys* auf Groß ausgeben; `--new` ersetzt den Key |
| `twinraid-admin trust-host-key target` | SSH-Hostkey von Unraid Groß bestätigen und die Verbindung zu `guard.sh` testen (ändert nichts auf Groß) |
| `twinraid-admin show-action-log` | Aktionslog des Workers anzeigen (`--lines 100` für mehr Einträge) |
| `twinraid-admin mock-job on` / `off` | Nur im Simulationsmodus: einen laufenden Kopierjob vortäuschen |
| `twinraid-admin mock-tour on` / `off` | Nur im Simulationsmodus: alle Zustände und Ursachen nacheinander zeigen (Aktionen sind dann gesperrt) |
| `twinraid-admin mock-fault <Name> on` / `off` | Nur im Simulationsmodus: Fehler vortäuschen, `<Name>` ist `vm-hang`, `api-forbidden`, `ssh-denied` oder `no-boot` |
