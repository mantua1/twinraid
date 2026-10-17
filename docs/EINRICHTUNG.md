# TwinRaid auf Unraid einrichten

> **Stand: Version 0.4.4.** Diese Version zeigt den Status beider Server und der Gaming-VM an und kann Unraid Groß wecken und herunterfahren sowie die Gaming-VM starten, herunterfahren und erzwingen. Status und VM laufen über SSH und `guard.sh` auf Groß; einen API-Key von Unraid braucht TwinRaid seit 0.4.3 nicht mehr. Die Aktionen werden einzeln freigeschaltet (Abschnitt 11). Wer von 0.4.2 kommt, beginnt mit Abschnitt 12; von 0.4.3 auf 0.4.4 genügt das Update (Abschnitt 14). Der Sync folgt in einer späteren Version. Die Anleitung wird mit jeder Version erweitert und ist in Phase 7 fertig.

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
9. [Unraid Groß: SSH, guard.sh und power.conf vorbereiten](#9-unraid-groß-ssh-guardsh-und-powerconf-vorbereiten)
10. [In den Echtbetrieb wechseln](#10-in-den-echtbetrieb-wechseln)
11. [Aktionen einrichten und testen](#11-aktionen-einrichten-und-testen)
12. [Von 0.4.2 auf 0.4.3 umstellen](#12-von-042-auf-043-umstellen)
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
| Port der Unraid-Oberfläche von Groß | `80` | Unraid Groß: *Settings → Management Access*. Bei *Use SSL/TLS* **No** der HTTP-Port (Standard 80), bei **Yes** der HTTPS-Port (Standard 443). TwinRaid prüft daran nur, ob Groß an ist. |
| Name der Gaming-VM | `Windows 11 Gaming` | Unraid Groß: Reiter **VMs**, genau so geschrieben. Daraus ermittelst du in Schritt 9.3 ihre UUID. |
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

**3.1 Ordner anlegen.** Der Datenordner enthält das Passwort, die Schlüssel der CA und später die SSH-Keys. Er muss **root gehören** und darf **nur für root beschreibbar** sein. Sonst startet TwinRaid aus Sicherheitsgründen nicht.

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

**3.2 Beispielkonfiguration herunterladen.** Die Version im Pfad (`v0.4.4`) passt zur installierten Version.

```bash
wget -O /mnt/user/appdata/twinraid/config/twinraid.yaml \
  https://raw.githubusercontent.com/mantua1/twinraid/v0.4.4/config.example.yaml
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
- `servers.target.webgui`: der Port der Unraid-Oberfläche von Groß. TwinRaid baut dorthin nur eine Verbindung auf, um zu sehen, ob Groß an ist; es überträgt darüber nichts. `scheme: http` steht für Port 80, `scheme: https` für 443; ein anderer Port kommt als `port:` dazu. Ob Unraid HTTPS nutzt, spielt für TwinRaid keine Rolle. Bei `servers.source.webgui` (Klein) hat der Wert keine Wirkung, weil TwinRaid Klein nicht prüft.
- `servers.target.mac`: die MAC von Groß für Wake-on-LAN. `wol_broadcast` bleibt auskommentiert, solange Groß im selben Netz wie TwinRaid liegt; TwinRaid leitet die Broadcast-Adresse dann selbst ab.
- `gaming_vm.unraid_vm_name`: optional, der Name der VM genau wie im Reiter **VMs**. Welche VM gemeint ist, steht seit 0.4.3 nur auf Groß in `power.conf` (Schritt 9.3); der Name dient TwinRaid nur zum Abgleich. Passt er nicht, zeigt die VM-Kachel einen Hinweis.
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
  https://raw.githubusercontent.com/mantua1/twinraid/v0.4.4/docker/unraid-template.xml
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

## 9. Unraid Groß: SSH, guard.sh und power.conf vorbereiten

TwinRaid liest den Zustand der Gaming-VM per SSH: Solange Groß läuft, hält es eine Verbindung mit dem Key „status“ offen. Auf Groß beantwortet `guard.sh` diese Verbindung; das Skript fragt dort alle 3 Sekunden ab und schickt eine kurze Zeile, wenn sich etwas ändert. Welche VM gemeint ist, steht nur auf Groß in der Datei `power.conf`.

**9.1 SSH einschalten.** Unraid-Oberfläche von **Groß**: *Settings → Management Access* → **Use SSH:** `Yes`, Port `22` lassen → **Apply**. (Bei einem anderen Port diesen in `twinraid.yaml` als `servers.target.ssh_port` eintragen.)

**9.2 `guard.sh` holen.** Im **Unraid-Terminal von Groß** (die Version im Pfad passt zur installierten TwinRaid-Version):

```bash
mkdir -p /boot/config/twinraid
wget -O /boot/config/twinraid/guard.sh \
  https://raw.githubusercontent.com/mantua1/twinraid/v0.4.4/host-scripts/guard.sh
head -2 /boot/config/twinraid/guard.sh
```

Die beiden Zeilen lauten `#!/bin/bash` und `# TwinRaid: SSH-Wächter auf Unraid Groß, Version 2 (Konzept 3.5 und 5.6).` Ausführbar machen ist nicht nötig: Unraid ruft das Skript über `/bin/bash` auf.

**9.3 Die Gaming-VM in `power.conf` eintragen.** `virsh` kennt jede VM unter einer festen UUID. Im **Unraid-Terminal von Groß**, mit dem Namen deiner VM genau wie im Reiter **VMs**:

```bash
uuid="$(virsh domuuid "Windows 11 Gaming")" && printf 'vm %s\n' "$uuid" > /boot/config/twinraid/power.conf
cat /boot/config/twinraid/power.conf
```

Die Ausgabe ist eine einzige Zeile wie `vm 0b5c4e1a-2f3d-4c5b-8a9e-112233445566`. Meldet `virsh` „failed to get domain“, ist der Name anders geschrieben; dann wird die Datei nicht angelegt. Die UUID steht auch in der Unraid-Oberfläche: Reiter **VMs** → VM anklicken → **Edit** → oben rechts von *Form View* auf *XML View* umschalten → Zeile `<uuid>…</uuid>`.

`power.conf` enthält kein Geheimnis. TwinRaid kann sie weder lesen noch ändern; es sendet `guard.sh` nie den Namen oder die UUID einer VM. Eine andere VM trägst du ein, indem du die Datei auf Groß änderst; `guard.sh` liest sie spätestens nach 15 Sekunden neu, TwinRaid musst du dafür nicht neu starten.

> **Was dürfen die Keys?** Der Key **„status“** darf nur zwei Dinge: einen Verbindungstest (`check`) und die Statusabfrage (`vm-watch`); er kann nichts verändern. Der Key **„power“** (für die Aktionen, Abschnitt 11) darf die VM aus `power.conf` starten, per Signal herunterfahren und erzwingen sowie Groß herunterfahren, Letzteres nur, wenn keine VM mehr läuft. Keine Anfrage nimmt Parameter an. Alles andere lehnt `guard.sh` ab und schreibt es ins Syslog von Groß (*Tools → System Log*, Kennung `twinraid-guard`). Beide Keys gelten nur von der IP von TwinRaid aus, ohne Terminal und ohne Weiterleitungen.

---

## 10. In den Echtbetrieb wechseln

**10.1 Modus umstellen:** Reiter **Docker** → Symbol *TwinRaid* → **Edit** → **Betriebsmodus** auf `real` → **Apply**. Unraid legt den Container neu an; Passwort, Zertifikate und Einstellungen bleiben erhalten. Die VM-Kachel zeigt danach „Kein SSH-Key „status““; das ändert der nächste Schritt.

**10.2 Key „status“ einrichten.**

1. In der **Container-Konsole**:

   ```bash
   twinraid-admin init-ssh-keys
   ```

   Solange keine Aktion freigeschaltet ist, gibt der Befehl **eine** lange Zeile aus, die so beginnt (mit deiner IP von TwinRaid):

   ```text
   restrict,from="192.168.10.20",command="/bin/bash /boot/config/twinraid/guard.sh status" ssh-ed25519 AAAA… twinraid-status
   ```

   Die ganze Zeile markieren und kopieren. Sie enthält nur den öffentlichen Teil des Keys; der private bleibt in TwinRaid. Rufst du den Befehl erneut auf, erscheint dieselbe Zeile.
2. In der Unraid-Oberfläche von **Groß**: *Users* → **root** → Feld **SSH authorized keys**. Die Zeile in eine **neue Zeile** einfügen (vorhandene Zeilen bleiben stehen) → **Save**.
3. Im **Unraid-Terminal von Groß** den Fingerabdruck des SSH-Servers anzeigen:

   ```bash
   ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub
   ```

   Ausgabe zum Beispiel `256 SHA256:Xy3…Qk root@Gross (ED25519)`. Den Teil `SHA256:…` brauchst du gleich.
4. In der **Container-Konsole**:

   ```bash
   twinraid-admin trust-host-key target
   ```

   TwinRaid zeigt `ED25519 SHA256:…`. Mit Schritt 3 vergleichen; nur wenn beide gleich sind, `yes` eingeben. Erwartet: `Host key confirmed. From now on TwinRaid only accepts exactly this key.`, `OK: guard.sh answers (twinraid-guard 2 status ok).` und `TwinRaid connects with the key 'status' right away.`

   Meldet der Test stattdessen `The server rejects the key 'status'`, fehlt die Zeile aus Schritt 2 auf Groß noch oder ist abgeschnitten. Dann Schritt 2 nachholen und diesen Befehl wiederholen (der Hostkey ist dann schon bestätigt). Seit 0.4.4 verbindet sich TwinRaid nach dem bestandenen Test sofort; vorher wartete es nach einer Ablehnung bis zu 30 Minuten.

**10.3 Dashboard prüfen:** Im Browser neu laden. Statt des gelben Banners steht oben **Nur Statusanzeige**, solange keine Aktion freigeschaltet ist (Abschnitt 11). Unraid Klein steht immer auf **Online** mit dem Hinweis „TwinRaid läuft auf diesem Server“ (siehe Schritt 2). Ist Unraid Groß aus, steht bei der VM „Server offline“. Ist Groß an, zeigt die VM-Kachel nach wenigen Sekunden ihren Zustand; ändert er sich, folgt die Anzeige nach höchstens etwa 3 Sekunden.

> **Anmeldungen im Syslog von Groß:** TwinRaid meldet sich mit dem Key „status“ einmal an, sobald Groß an ist, und hält die Verbindung offen. Im Syslog von Groß stehen dazu die Anmeldung von `sshd` und eine Zeile `twinraid-guard: vm-watch started`. Weitere Anmeldungen gibt es nur für Aktionen (Key „power“) und nach einem Abbruch der Verbindung.

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
| VM starten | SSH-Key „power“: `guard.sh` startet die VM aus `power.conf` (`virsh start`); ist Groß aus, wird es vorher geweckt | `vm` (bei ausgeschaltetem Groß zusätzlich `wake`) |
| VM herunterfahren | SSH-Key „power“: `guard.sh` sendet nur das Signal zum Herunterfahren (wie der Netzschalter) | `vm` |
| VM erzwingen | SSH-Key „power“ (`virsh destroy`), frühestens `shutdown_timeout_minutes` nach „Herunterfahren“ | `vm` |
| Groß herunterfahren | SSH-Key „power“: `guard.sh` ruft `/sbin/poweroff` auf (wie der Button in der Unraid-Oberfläche); läuft die VM, wird sie zuerst heruntergefahren. Laufen außer der Gaming-VM weitere VMs, schaltet TwinRaid Groß nicht aus (ab 0.4.2); seit 0.4.3 prüft `guard.sh` das unmittelbar vor dem Ausschalten noch einmal selbst | `shutdown` (bei laufender VM zusätzlich `vm`) |
| Erzwingen und danach Server herunterfahren (ab 0.4.1) | nur bis zu 10 Minuten nach einem Herunterfahren von Groß, das an der VM gescheitert ist: VM erzwingen, dann Groß wie oben herunterfahren | `vm` und `shutdown` |

Nach jeder Änderung an `twinraid.yaml` den Container neu starten (Reiter **Docker** → *TwinRaid* → **Restart**). Jede Aktion steht im Aktionslog des Workers; anzeigen in der **Container-Konsole** mit `twinraid-admin show-action-log`.

> **Warum per SSH und nicht über die Unraid-API?** Bis 0.4.2 nutzte TwinRaid die API mit einem API-Key. Unraid speichert solche Keys im Klartext auf dem USB-Stick, und ein Key mit VM-Rechten darf alle VMs steuern. Der Key „power“ darf nur die VM aus `power.conf` steuern und Groß nur ausschalten, wenn keine VM läuft. Außerdem wartet der Befehl `vm.stop` der API nur 10 Sekunden und schaltet die VM danach selbst hart aus. Über SSH sendet TwinRaid nur das Signal und wartet selbst die eingestellte Zeit ab; erst danach bietet es „Erzwingen“ an.

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

**SSH, `guard.sh` und `power.conf`** sind seit Abschnitt 9 eingerichtet. Für die Aktionen kommt in Test 3 (11.6) der Key „power“ dazu.

### 11.2 Optional: mit einer Test-VM üben

Für die VM-Tests kannst du statt der Gaming-VM vorübergehend eine Test-VM verwenden. Dafür ist keine neue TwinRaid-Version nötig.

1. Im **Unraid-Terminal von Groß** die Test-VM in `power.conf` eintragen (Name wie im Reiter **VMs**):

   ```bash
   uuid="$(virsh domuuid "Test-VM")" && printf 'vm %s\n' "$uuid" > /boot/config/twinraid/power.conf
   cat /boot/config/twinraid/power.conf
   ```

2. In `twinraid.yaml` bei `gaming_vm` den Namen der Test-VM eintragen (sonst zeigt die VM-Kachel den Hinweis, dass der Name nicht passt) und für kürzere Wartezeiten die Wartezeit auf 1 Minute setzen, dann den Container neu starten:

   ```yaml
   gaming_vm:
     display_name: "Gaming-VM"
     unraid_vm_name: "Test-VM"
     shutdown_timeout_minutes: 1
   ```

3. Die VM-Kachel zeigt jetzt den Zustand der Test-VM.

Für den Test von **Erzwingen** brauchst du eine VM, die auf das Signal zum Herunterfahren **nicht** reagiert. Am einfachsten stellst du in einer Windows-Test-VM den Netzschalter vorübergehend auf **Nichts unternehmen** (siehe 11.1). Alternativ reagiert eine VM ohne Betriebssystem, die in der UEFI-Shell steht, in der Regel ebenfalls nicht.

Am Ende (11.24) trägst du die Gaming-VM wieder ein. Für Test 11 (11.14) brauchst du die Gaming-VM und die Test-VM gleichzeitig.

### 11.3 Testprotokoll: Ausgangslage

TwinRaid 0.4.3 oder neuer läuft im Echtbetrieb, der Key „status“ ist eingerichtet (Abschnitt 10 bzw. 12), `actions` fehlt in `twinraid.yaml` oder steht überall auf `false`. Steht dort `wol_broadcast`, kannst du die Zeile löschen; TwinRaid leitet die Adresse dann selbst ab.

| Prüfen | Erwartet |
|---|---|
| Container-Log | `Read-only real mode: status checks only, all actions are locked.` und `Wake-on-LAN broadcast address: 192.168.10.255 (…)` |
| Dashboard | Banner **Nur Statusanzeige**; die Buttons sind grau mit dem Hinweis „in twinraid.yaml unter actions nicht freigeschaltet“ |
| **Einstellungen → Aktionen** | alle drei Gruppen „gesperrt“ |
| **Einstellungen → Verbindung zu Unraid Groß** | „Status und VM-Aktionen laufen per SSH über guard.sh …“; kein Hinweis auf eine unverschlüsselte Verbindung |

**Reihenfolge für 0.4.3:** G1, G2, dann die Tests 3 bis 9, aus Test 10 die Varianten „Andere Aktion“ und „Zustand ändert sich“, Test 11, Test 8 zur Kontrolle, G3 bis G6, G7 nur wenn du willst, G8 nur, wenn sich zwei VMs eine Grafikkarte teilen. Erst mit der Test-VM in `power.conf`, zum Schluss mit der Gaming-VM.

**Für 0.4.4** genügen: Test 4 Schritt 3 (Uhrzeit bei „Erzwingen“, 11.7), Test 11 Schritte 1 und 4 (Zeile „Weitere laufende VMs“, 11.14) und G9 (Wartezeit nach abgelehntem Key „status“, 11.22). `guard.sh`, `power.conf` und die Keys bleiben unverändert.

### 11.4 Test G1: guard.sh direkt auf Groß prüfen

Diese Prüfungen laufen im **Unraid-Terminal von Groß** und verändern nichts. Sie zeigen, dass `guard.sh` nur feste Anfragen ohne Parameter annimmt und der Modus „status“ nichts auslösen kann. Nach jedem Befehl zeigt `echo $?` den Exit-Code.

```bash
G=/boot/config/twinraid/guard.sh
SSH_ORIGINAL_COMMAND=check bash $G status; echo $?
SSH_ORIGINAL_COMMAND=poweroff bash $G status; echo $?
SSH_ORIGINAL_COMMAND=vm-start bash $G status; echo $?
SSH_ORIGINAL_COMMAND="vm-shutdown 0b5c4e1a-2f3d-4c5b-8a9e-112233445566" bash $G power; echo $?
SSH_ORIGINAL_COMMAND="check; reboot" bash $G power; echo $?
SSH_ORIGINAL_COMMAND=vm-watch timeout 5 bash $G status; echo $?
```

| Befehl | Erwartet |
|---|---|
| `check` im Modus status | `twinraid-guard 2 status ok`, Exit `0` |
| `poweroff` und `vm-start` im Modus status | `twinraid-guard: denied`, Exit `2`; Groß bleibt an, die VM aus |
| `vm-shutdown <UUID>` im Modus power (mit Parameter) | `twinraid-guard: denied`, Exit `2` |
| `check; reboot` | `twinraid-guard: denied`, Exit `2` |
| `vm-watch` (5 Sekunden lang) | eine Zeile wie `watch 2 vm=SHUTOFF name=… others=0 names=- connect=absent flash_backup=inactive`, dann Exit `124` (beendet von `timeout`) |
| *Tools → System Log* | Einträge `twinraid-guard: denied: …` ohne den Text der Anfrage |

**`power.conf` fehlt:** Die Datei kurz umbenennen und die Statusabfrage wiederholen, danach zurückbenennen:

```bash
mv /boot/config/twinraid/power.conf /boot/config/twinraid/power.conf.bak
SSH_ORIGINAL_COMMAND=vm-watch timeout 5 bash $G status
mv /boot/config/twinraid/power.conf.bak /boot/config/twinraid/power.conf
```

Erwartet: `watch 2 vm=NOT_CONFIGURED name=- others=? names=- …`. Weil `guard.sh` die Datei alle 15 Sekunden neu liest, kann das Dashboard in dieser Zeit kurz „VM auf dem Server nicht eingetragen“ zeigen; danach zeigt es wieder den Zustand der VM.

### 11.5 Test G2: Status ohne API-Key

1. In `power.conf` steht die Test-VM (11.2). Die VM-Kachel zeigt ihren Zustand; starte und stoppe die Test-VM einmal in der Unraid-Oberfläche: Die Kachel folgt nach wenigen Sekunden.
2. Den alten API-Key von TwinRaid in Unraid löschen: *Settings → Management Access* → Bereich **API Keys** → Key **TwinRaid Status** (bzw. **TwinRaid**) → löschen.
3. Im **Unraid-Terminal von Groß** prüfen, dass kein Key von TwinRaid mehr auf dem Stick liegt:

   ```bash
   grep -l -i twinraid /boot/config/plugins/dynamix.my.servers/keys/* 2>/dev/null || echo "kein Key von TwinRaid"
   ```

| Erwartet | |
|---|---|
| Ausgabe | `kein Key von TwinRaid` |
| Dashboard | unverändert: Der Status der VM kommt weiter, denn er braucht keinen API-Key |
| Container-Log von TwinRaid (einmal beim ersten Start von 0.4.3) | `Removed the Unraid API key of Unraid Groß …` |

### 11.6 Test 3: SSH-Key „power“ einrichten

Alle Aktionen außer Wecken laufen über den Key „power“. Deshalb kommt er vor den nächsten Tests. Wer von 0.4.2 kommt, hat ihn schon: Key und Zeile auf Groß bleiben, wie sie sind; dann nur Schritt 1 (zum Vergleich) und Schritt 3.

1. In `twinraid.yaml` eintragen und den Container neu starten:

   ```yaml
   actions:
     vm: true
   ```

   Im Container-Log steht `Real mode: these actions are enabled in twinraid.yaml: vm`. Das Banner **Nur Statusanzeige** ist weg.
2. In der **Container-Konsole**:

   ```bash
   twinraid-admin init-ssh-keys --key power
   ```

   Die Ausgabe enthält eine lange Zeile, die so beginnt (mit deiner IP von TwinRaid):

   ```text
   restrict,from="192.168.10.20",command="/bin/bash /boot/config/twinraid/guard.sh power" ssh-ed25519 AAAA… twinraid-power
   ```

   In der Unraid-Oberfläche von **Groß**: *Users* → **root** → Feld **SSH authorized keys** → die Zeile in eine **neue Zeile** einfügen (die Zeile für „status“ bleibt stehen) → **Save**. Unraid speichert sie dauerhaft (`/boot/config/ssh/root/authorized_keys`).
3. In der **Container-Konsole**:

   ```bash
   twinraid-admin trust-host-key target
   ```

   Der Hostkey ist schon bestätigt (Abschnitt 10.2); TwinRaid zeigt ihn trotzdem zum Vergleich an. Mit `yes` bestätigen.

| Erwartet | |
|---|---|
| Ausgabe | `This host key was already confirmed.`, `OK: guard.sh answers (twinraid-guard 2 status ok).`, `OK: guard.sh answers (twinraid-guard 2 power ok).` und ab 0.4.4 `TwinRaid connects with the key 'status' right away.` |
| Unraid Groß | nichts passiert; der Test verändert nichts |

Meldet der Befehl einen Fehler, steht die Abhilfe dabei, zum Beispiel bei abgelehntem Key (`Users → root`), ausgeschaltetem SSH oder fehlendem `guard.sh`. Siehe auch Abschnitt 13.

### 11.7 Test 4: VM starten, herunterfahren und erzwingen

1. **Starten** (VM ist aus): **Startet …** → **Online**, „Zuletzt: VM starten – erfolgreich“.
2. **Herunterfahren** klicken. Es erscheint die Frage „… herunterfahren?“ mit der Wartezeit → **Herunterfahren**.

   | Erwartet | |
   |---|---|
   | VM-Kachel | **Fährt herunter …** („Die VM fährt herunter …“), dann **Offline**, „Zuletzt: VM herunterfahren – erfolgreich“ |
   | Unraid Groß, *Tools → System Log* | `twinraid-guard: vm-start of VM … requested by TwinRaid` und `twinraid-guard: ACPI shutdown of VM … requested by TwinRaid` (mit der UUID aus `power.conf`) |

3. **Erzwingen:** Die VM darf jetzt nicht auf das Signal reagieren (siehe 11.2). VM **Starten**, dann **Herunterfahren** und bestätigen.

   | Erwartet | |
   |---|---|
   | sofort | **Fährt herunter …** und ein grauer Button **Erzwingen** mit „Erzwingen: möglich ab hh:mm:ss“ (die Uhrzeit fehlte in 0.4.3 und kommt seit 0.4.4) |
   | nach `shutdown_timeout_minutes` | rot: „Zuletzt: VM herunterfahren – Die VM hat sich nicht rechtzeitig heruntergefahren.“ und darunter „„Erzwingen“ ist jetzt möglich.“; **Erzwingen** ist aktiv. „Erzwingen und danach Server herunterfahren“ erscheint hier **nicht** (nur nach einem Herunterfahren von Groß) |
   | **Erzwingen** → **Jetzt erzwingen** | die VM ist sofort **Offline**, „Zuletzt: VM erzwingen – erfolgreich“; im Syslog von Groß `twinraid-guard: forced stop of VM … requested by TwinRaid` |

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
| Kachel Groß | **Startet …** („Magic Packet gesendet, der Server startet …“), dann „Wartet auf den VM-Dienst …“, schließlich **Online** und „Zuletzt: Server wecken – erfolgreich“ |
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

### 11.14 Test 11: Eine weitere VM läuft

Ab Version 0.4.2. In `power.conf` (und in `twinraid.yaml` bei `unraid_vm_name`) ist wieder die **Gaming-VM** eingetragen, die Test-VM gibt es zusätzlich auf Groß. `actions.vm` und `actions.shutdown` sind freigeschaltet, Groß ist an.

1. Die Gaming-VM ist aus. Die **Test-VM** in der Unraid-Oberfläche von Groß starten (Reiter **VMs** → Symbol der Test-VM → **Start**).

   | Erwartet (nach wenigen Sekunden) | |
   |---|---|
   | Kachel Groß, unter dem Status | „Weitere laufende VMs: Test-VM“ (ab 0.4.4, nur Anzeige) |
   | Kachel Groß | **Herunterfahren** ist grau; darunter „Herunterfahren: Auf Unraid Groß laufen noch weitere VMs: Test-VM. Bitte fahre sie zuerst über die Unraid-Oberfläche herunter. Zur Sicherheit schaltet TwinRaid den Server nicht aus, solange andere VMs laufen, damit keine Daten verloren gehen.“ |
   | Sprache auf **EN** | derselbe Hinweis auf Englisch |

2. Die Test-VM in Unraid **pausieren** (**Pause**). Der Button bleibt gesperrt: Auch eine pausierte VM zählt als laufend.
3. **Prüfung unmittelbar vor dem Ausschalten** (nur mit der Test-VM, sie wird dabei gestartet, während Groß herunterfahren soll):
   1. Die Test-VM ausschalten (**Force Stop**), bis der Button **Herunterfahren** wieder aktiv ist.
   2. Die **Gaming-VM starten** und warten, bis sie **Online** ist.
   3. In der Kachel Groß **Herunterfahren** → **VM und Server herunterfahren**.
   4. Sobald die VM-Kachel **Fährt herunter …** zeigt, sofort die **Test-VM** in Unraid starten.

   | Erwartet | |
   |---|---|
   | Kachel VM | die Gaming-VM fährt herunter und ist **Offline** |
   | Kachel Groß | bleibt **Online**; rot „Zuletzt: Server herunterfahren – Der Server wurde nicht ausgeschaltet, weil noch weitere VMs liefen.“; **Herunterfahren** grau mit dem Hinweis aus Schritt 1 |
   | `twinraid-admin show-action-log` | `vm_stop  automatic (part of shutdown_target): success` und `shutdown_target  dashboard: other_vms_running` |

   Warst du zu langsam und Groß fährt schon herunter, Groß wecken und den Schritt wiederholen. Unraid fährt die gerade gestartete Test-VM beim Herunterfahren selbst herunter.
4. Die Test-VM in Unraid ausschalten (**Force Stop**). Nach wenigen Sekunden ist **Herunterfahren** wieder aktiv, die Meldung aus Schritt 3 ist nur noch grau, und die Zeile „Weitere laufende VMs“ ist weg: Ausgeschaltete VMs zeigt TwinRaid nicht an.

### 11.15 Test 8 zur Kontrolle wiederholen

Mit der Gaming-VM (Netzschalter auf **Herunterfahren**, siehe 11.1) Test 8 aus 11.11 wiederholen. Erwartet wie dort: erst die VM **Offline**, dann Groß **Offline**, „Zuletzt: Server herunterfahren – erfolgreich“. Die Option „Erzwingen und danach Server herunterfahren“ erscheint dabei **nicht**.

### 11.16 Test G3: Neustart von Groß und Anmeldungen

1. Groß in der Unraid-Oberfläche von Groß neu starten (*Main* → **Reboot**).
2. Die VM-Kachel beobachten.
3. Nach dem Neustart das Dashboard 30 Minuten offen lassen, ohne eine Aktion auszulösen. Dann im Syslog von Groß (*Tools → System Log*) nachsehen.

| Erwartet | |
|---|---|
| während Groß herunterfährt | nach höchstens etwa 12 Sekunden „Server offline“; davor höchstens kurz „Statusverbindung unterbrochen“, keine Meldung zu Keys oder Hostkey |
| nach dem Start | kurz „Statusverbindung wird aufgebaut …“ und „VM-Dienst nicht bereit“, dann der Zustand der VM, ohne dass du etwas tust |
| Syslog nach 30 Minuten | genau eine Anmeldung mit dem Key „status“ (Zeile von `sshd` mit `Accepted publickey`) und genau eine Zeile `twinraid-guard: vm-watch started` |

### 11.17 Test G4: VM pausiert, VM-Dienst aus

1. Die Test-VM starten und in Unraid **pausieren** (**Pause**). Erwartet: VM-Kachel **Online** mit „Pausiert“. Wieder fortsetzen (**Resume**).
2. Alle VMs ausschalten. Dann den VM-Dienst stoppen: *Settings → VM Manager* → **Enable VMs:** `No` → **Apply**.

   | Erwartet | |
   |---|---|
   | VM-Kachel | **Unbekannt** mit „VM-Dienst nicht bereit“ |
   | Buttons | **Starten**, **Herunterfahren** (VM und Groß) sind gesperrt |

3. Den VM-Dienst wieder einschalten (**Enable VMs:** `Yes`). Nach wenigen Sekunden zeigt die Kachel wieder den Zustand der VM.

### 11.18 Test G5: Hinweis zu Unraid Connect

Unraid-Oberfläche von Groß: *Plugins* ansehen. Ist dort **Unraid Connect** installiert, zeigt die Kachel **Unraid Groß** einen gelben Hinweis („Auf Unraid Groß ist das Plugin Unraid Connect installiert …“), ebenso **Einstellungen → Verbindung zu Unraid Groß**; ist Flash Backup aktiv, steht ein zweiter Satz dabei. Ist Connect nicht installiert, gibt es keinen Hinweis. Die Anzeige muss zur Plugins-Seite passen. (Den Fall „installiert“ prüfen die CI und die Simulation mit `twinraid-admin mock-fault connect-plugin on`; eine Attrappe auf dem Stick anzulegen wäre riskant, weil Unraid sie beim nächsten Start zu installieren versucht.)

### 11.19 Test G6: Nur Statusanzeige

1. In `twinraid.yaml` alle `actions` auf `false` setzen, Container neu starten. Das Banner **Nur Statusanzeige** erscheint.
2. Groß einmal neu starten (wie G3) und danach 10 Minuten warten.

| Erwartet | |
|---|---|
| Dashboard | Der Status läuft wie gewohnt |
| Syslog von Groß | Anmeldungen nur mit dem Fingerabdruck des Keys „status“, keine mit „power“. Die Fingerabdrücke der Keys zeigt `ssh-keygen -lf /data/secrets/ssh-status.pub` bzw. `…/ssh-power.pub` in der Container-Konsole |

Danach die Aktionen wieder freischalten, die du nutzen willst.

### 11.20 Test G7 (optional): Prüfung vor poweroff direkt auf Groß

Nur mit laufender **Test-VM** und nur, wenn dir die Prüfung in der CI nicht reicht. **Versagt der Schutz, fährt Groß sauber herunter.** Im **Unraid-Terminal von Groß**:

```bash
SSH_ORIGINAL_COMMAND=poweroff bash /boot/config/twinraid/guard.sh power; echo $?
```

Erwartet: `twinraid-guard: refused vm-running` (steht die Test-VM in `power.conf`) bzw. `twinraid-guard: refused other-vms-running` (steht dort die Gaming-VM), Exit `3`. Groß bleibt an; im Syslog steht `twinraid-guard: refused: …`.

### 11.21 Test G8: Eine andere VM belegt die Grafikkarte

Nur, wenn auf Groß eine zweite VM dieselbe durchgereichte Grafikkarte nutzt wie die Gaming-VM und beide nie gleichzeitig laufen. TwinRaid kennt diese Zuordnung nicht und sperrt **Starten** deshalb nicht; den Start lehnt libvirt auf Groß ab. Der Test prüft, dass die zweite VM dabei nichts abbekommt.

Ausgangslage: In `power.conf` (und in `twinraid.yaml` bei `unraid_vm_name`) steht die **Gaming-VM**, `actions.vm` ist freigeschaltet, Groß ist an, die Gaming-VM ist aus.

> **Vorher in der zweiten VM alles speichern.** Lehnt libvirt den Start wider Erwarten nicht ab, nimmt die Gaming-VM der zweiten VM die Grafikkarte weg; die zweite VM verhält sich dann wie nach einem Ausfall der Grafikkarte.

1. Prüfen, dass beide VMs wirklich dieselbe Karte haben: Reiter **VMs** → Symbol der VM → **Edit**, bei beiden VMs zeigen **Graphics Card** und, falls eingetragen, **Sound Card** dieselben Geräte.
2. Die **zweite VM** in der Unraid-Oberfläche von Groß starten (Reiter **VMs** → Symbol → **Start**) und warten, bis sie ein Bild zeigt.
3. In TwinRaid in der VM-Kachel **Starten** klicken.

   | Erwartet | |
   |---|---|
   | VM-Kachel | kurz **Startet …**, nach wenigen Sekunden wieder **Offline**; rot „Zuletzt: VM starten – Der Befehl ist auf dem Server fehlgeschlagen.“ |
   | Kachel Groß | bleibt **Online**; **Herunterfahren** ist grau mit dem Hinweis auf weitere VMs und dem Namen der zweiten VM (wie in Test 11) |
   | zweite VM | läuft ohne Unterbrechung weiter: Bild, Ton, USB-Geräte und Programme in der VM arbeiten wie vorher; im Reiter **VMs** weiterhin **Started** |
   | `twinraid-admin show-action-log` | `vm_start  dashboard: remote_failed` |
   | Syslog von Groß (*Tools → System Log*) | `twinraid-guard: vm-start of VM … requested by TwinRaid` |
   | Grund, im **Unraid-Terminal von Groß** | `grep -i 'in use' /var/log/libvirt/libvirtd.log \| tail -n 3` zeigt eine Zeile mit `Requested operation is not valid: PCI device … is in use by driver QEMU, domain …` |

   Die Meldung in TwinRaid ist allgemein, weil `guard.sh` Ausgaben von `virsh` nie an TwinRaid weitergibt (Konzept 3.5); den genauen Grund nennt nur das Log von libvirt.
4. Die zweite VM in Unraid herunterfahren (**Stop**) und warten, bis sie aus ist. Dann in TwinRaid **Starten**: Die Gaming-VM startet normal, „Zuletzt: VM starten – erfolgreich“. Danach wieder **Herunterfahren**.

### 11.22 Test G9: Wartezeit nach abgelehntem Key „status“ beenden

Ab Version 0.4.4. Der Test stellt nach, was beim Einrichten passieren kann: Die Zeile für den Key „status“ fehlt auf Groß, TwinRaid wird abgewiesen und wartet danach. Groß ist an; die Gaming-VM darf laufen oder aus sein. Geändert wird nur die Zeile für „status“; Aktionen löst der Test nicht aus.

1. In der Unraid-Oberfläche von **Groß**: *Users* → **root** → Feld **SSH authorized keys**. Die Zeile, die auf `twinraid-status` endet, ganz markieren, ausschneiden (`Strg+X`) und in einem Texteditor aufheben → **Save**. Die Zeile für „power“ bleibt stehen.
2. Die laufende Statusverbindung bleibt davon bestehen; deshalb beendest du sie. Im **Unraid-Terminal von Groß**:

   ```bash
   pkill -f 'guard.sh status'
   ```

   | Erwartet | |
   |---|---|
   | VM-Kachel, sofort | kurz „Statusverbindung unterbrochen – neuer Versuch folgt“ mit „Nächster Versuch um hh:mm:ss.“ wenige Sekunden später |
   | VM-Kachel, nach etwa 5 Sekunden | **Unbekannt** mit „Der Server lehnt den SSH-Key „status“ ab …“ und darunter „Nächster Versuch um hh:mm:ss. Ist die Ursache behoben, verbindet sich TwinRaid sofort nach einem erfolgreichen „twinraid-admin trust-host-key target“ in der Container-Konsole.“ Die Uhrzeit liegt etwa 30 Minuten in der Zukunft |
   | Sprache auf **EN** | derselbe Hinweis auf Englisch |

3. Die Zeile aus Schritt 1 wieder in eine **neue Zeile** einfügen → **Save**. Eine Minute warten.

   | Erwartet | |
   |---|---|
   | VM-Kachel | unverändert mit derselben Uhrzeit: Von selbst versucht TwinRaid es erst zur angezeigten Zeit, damit Groß nicht mit Anmeldungen überhäuft wird |

4. In der **Container-Konsole**:

   ```bash
   twinraid-admin trust-host-key target
   ```

   Der Hostkey ist schon bestätigt; TwinRaid zeigt ihn trotzdem zum Vergleich an. Mit `yes` bestätigen.

   | Erwartet | |
   |---|---|
   | Ausgabe | `This host key was already confirmed.`, `OK: guard.sh answers (twinraid-guard 2 status ok).`, falls der Key „power“ eingerichtet ist `OK: guard.sh answers (twinraid-guard 2 power ok).` und zum Schluss `TwinRaid connects with the key 'status' right away.` |
   | VM-Kachel, nach wenigen Sekunden | der Zustand der VM; die Zeile „Nächster Versuch …“ ist weg |
   | Container-Log | `Retry of the status connection requested (twinraid-admin trust-host-key).` |
   | Syslog von Groß (*Tools → System Log*) | eine neue Zeile `twinraid-guard: vm-watch started` |

Ohne Schritt 4 verbindet sich TwinRaid spätestens zur angezeigten Uhrzeit von selbst, ebenso nach einem Neustart von Groß.

### 11.23 Simulation (optional)

Im Simulationsmodus lässt sich das Verhalten ohne Server ausprobieren: `twinraid-admin mock-fault other-vm on` lässt auf dem simulierten Groß eine weitere VM namens „Test-VM“ laufen. Seit 0.4.3 gibt es zusätzlich `vm-not-configured` (`power.conf` fehlt), `libvirt-down` (VM-Dienst nicht bereit), `guard-outdated` (guard.sh zu alt) und `connect-plugin` (Unraid Connect mit Flash Backup), seit 0.4.4 `status-key-rejected` (Groß lehnt den Key „status“ ab; die VM-Kachel zeigt die Pause mit dem nächsten Versuch). `off` schaltet jeweils wieder aus; bei `status-key-rejected` wirkt das wie ein erfolgreiches `trust-host-key`.

### 11.24 Abschluss

1. In `power.conf` wieder die Gaming-VM eintragen (9.3), in `twinraid.yaml` ihren Namen bei `unraid_vm_name` und `shutdown_timeout_minutes` auf den gewünschten Wert setzen (Standard 3), Container neu starten.
2. Mit der Gaming-VM einmal **Starten** und **Herunterfahren**.
3. `twinraid-admin show-action-log` ansehen: Jede Aktion steht dort mit Beginn und Ergebnis.
4. Die Ergebnisse bitte im Pull Request festhalten (für 0.4.0 besonders Test 1, für 0.4.1 die Tests 9 und 10, für 0.4.2 Test 11, für 0.4.3 die Tests G1 bis G8, für 0.4.4 Test 4 Schritt 3, Test 11 und G9). Dort nur „Klein“ und „Groß“ schreiben, keine IP-Adressen, Hostnamen, Fingerabdrücke oder UUIDs.

---

## 12. Von 0.4.2 auf 0.4.3 umstellen

Seit 0.4.3 liest TwinRaid die VM per SSH über `guard.sh` und braucht keinen API-Key mehr. Die Umstellung dauert etwa zehn Minuten. Groß sollte dabei an sein.

1. **TwinRaid aktualisieren:** Reiter **Docker** → bei *TwinRaid* **update ready** → **Update**. Das Template bleibt unverändert.
2. **Container-Log ansehen:** Einmal steht dort `Removed the Unraid API key of Unraid Groß …`; TwinRaid hat den alten Key und den Fingerabdruck aus seinem Datenordner gelöscht. Die VM-Kachel zeigt jetzt „Kein SSH-Key „status““.
3. **`guard.sh` Version 2** auf Groß holen (9.2). Bis dahin meldet die Kachel nach Schritt 5 „guard.sh lehnt ab“.
4. **`power.conf`** auf Groß anlegen (9.3). Für die Tests in Abschnitt 11 erst mit der Test-VM (11.2), sonst gleich mit der Gaming-VM.
5. **Key „status“:** in der Container-Konsole `twinraid-admin init-ssh-keys --key status`, die Zeile auf Groß unter *Users → root → SSH authorized keys* in eine **neue Zeile** einfügen. Die vorhandene Zeile für „power“ bleibt unverändert.
6. In der Container-Konsole `twinraid-admin trust-host-key target`. Der Hostkey ist schon bestätigt; mit `yes` bestätigen. Erwartet: `OK: guard.sh answers (twinraid-guard 2 status ok).` und, falls der Key „power“ eingerichtet ist, `OK: guard.sh answers (twinraid-guard 2 power ok).` Ab 0.4.4 folgt `TwinRaid connects with the key 'status' right away.`
7. **Dashboard:** Die VM-Kachel zeigt nach wenigen Sekunden den Zustand der VM aus `power.conf`.
8. **Den alten API-Key in Unraid löschen.** Das ist Teil von Test G2 (11.5); wer die Tests nicht macht, löscht ihn jetzt (*Settings → Management Access → API Keys*).

**HTTPS:** Hast du Groß früher nur für TwinRaid auf *Use SSL/TLS: Yes* umgestellt, kannst du es so lassen oder zurückstellen. Für TwinRaid zählt nur, dass `servers.target.webgui` zum offenen Port passt (`scheme: https` steht für 443). Der früher bestätigte Fingerabdruck wird nicht mehr gebraucht.

---

## 13. Fehlerbilder und Abhilfe

| Anzeige im Dashboard | Bedeutung | Abhilfe |
|---|---|---|
| Klein: **TwinRaid läuft auf diesem Server** | Normal im Echtbetrieb: TwinRaid läuft auf Klein und prüft Klein deshalb nicht | Nichts zu tun. „Host access to custom networks“ bleibt aus (Schritt 2) |
| Browser zeigt nur **forbidden** | Das Gerät steht nicht in `web.allowed_networks`; im Container-Log steht `Rejected request from … not in web.allowed_networks` | IP des Geräts eintragen (im Server-Netz einzeln als `/32`, Schritt 3.3) und den Container neu starten |
| Log: `… has host bits set` | Ein Eintrag in `allowed_networks` wie `192.168.10.50/24` | Einzelnes Gerät als `192.168.10.50/32`, ganzes Netz als `192.168.10.0/24` schreiben; die Meldung nennt beide Varianten |
| TwinRaid aus dem Unraid-Terminal von Klein nicht erreichbar (`ping`, `curl`) | Normal ohne „Host access to custom networks“ | Nichts zu tun; von anderen Geräten aus testen |
| MAC nach Update oder Neustart anders, UniFi zeigt ein neues Gerät | Feld **Fixed MAC address** im Template leer oder Unraid älter als 7.3 | MAC aus Schritt 1 im Template eintragen (Schritt 4.2), danach Schritt 5.4 |
| Groß: **Nicht erreichbar**, VM: **Server offline** | Unraid Groß ist aus oder nicht erreichbar | Normal, wenn Groß aus ist. Sonst IP und `webgui` (Port der Unraid-Oberfläche) in `twinraid.yaml` prüfen |
| VM: **Statusverbindung wird aufgebaut …** | Groß ist gerade angegangen, SSH antwortet noch nicht | Normal nach dem Start von Groß; verschwindet nach wenigen Sekunden |
| VM: **Statusverbindung unterbrochen – neuer Versuch folgt** | Die SSH-Verbindung für den Status ist abgebrochen | TwinRaid versucht es nach 5 s, 15 s, 60 s und dann alle 5 Minuten erneut; die Uhrzeit des nächsten Versuchs steht darunter (ab 0.4.4). Bleibt die Meldung, SSH auf Groß und das Netz prüfen |
| VM: **Nächster Versuch um …** (ab 0.4.4) | Nach einem Fehler der Statusverbindung wartet TwinRaid, damit Groß nicht mit Anmeldungen überhäuft wird: nach Abbrüchen 5 Sekunden bis 5 Minuten, nach Fehlern bei Key, Hostkey oder `guard.sh` 30 Minuten | Die Ursache aus der Meldung darüber beheben, dann `twinraid-admin trust-host-key target`. Besteht der Test mit dem Key „status“, endet die Wartezeit sofort (Test G9, 11.22) |
| VM: **Kein SSH-Key „status“** | Schritt 10.2 bzw. 12.5 fehlt | `twinraid-admin init-ssh-keys --key status` und die Zeile auf Groß eintragen |
| VM: **SSH-Hostkey des Servers nicht bestätigt** | Hostkey von Groß noch nicht bestätigt | `twinraid-admin trust-host-key target` (10.2) |
| VM: **Der Server lehnt den SSH-Key „status“ ab** | Zeile für „status“ fehlt oder ist abgeschnitten, oder TwinRaid hat eine andere IP als in `from=` | `twinraid-admin init-ssh-keys --key status` (gibt die Zeile erneut aus) und auf Groß eintragen oder ersetzen; danach `twinraid-admin trust-host-key target`, dann verbindet sich TwinRaid sofort statt erst zur angezeigten Uhrzeit |
| VM: **guard.sh lehnt ab** | Auf Groß liegt noch `guard.sh` Version 1 (kennt den Key „status“ nicht), oder die Zeile ruft `guard.sh` in einem anderen Modus auf | `guard.sh` wie in 9.2 neu holen; die Zeile für „status“ muss auf `guard.sh status` enden. Danach `twinraid-admin trust-host-key target` (verbindet sofort neu) |
| VM: **guard.sh auf dem Server ist zu alt** | `guard.sh` ist älter als die installierte TwinRaid-Version verlangt | `guard.sh` wie in 9.2 neu holen, danach `twinraid-admin trust-host-key target` (verbindet sofort neu) |
| VM: **VM auf dem Server nicht eingetragen** | `power.conf` fehlt oder ist ungültig | Schritt 9.3; die Datei enthält genau eine Zeile `vm <UUID>` |
| VM: **VM nicht gefunden** | Die UUID in `power.conf` gehört zu keiner VM (VM neu angelegt?) | UUID wie in 9.3 neu eintragen |
| VM: **VM-Dienst nicht bereit** | Auf Groß läuft der VM-Dienst nicht oder startet gerade (`virsh` antwortet nicht) | Kurz nach dem Start von Groß normal. Sonst *Settings → VM Manager* → **Enable VMs** prüfen |
| Hinweis: **Die VM aus power.conf heißt anders als gaming_vm.unraid_vm_name** | `power.conf` zeigt auf eine andere VM als gedacht, oder der Name in `twinraid.yaml` ist veraltet | UUID in `power.conf` prüfen (9.3); stimmt sie, den Namen in `twinraid.yaml` anpassen |
| Hinweis: **Auf Unraid Groß ist das Plugin Unraid Connect installiert** | Groß ist mit der Cloud von Unraid verbunden; mit Flash Backup läge eine Kopie des Sticks dort | Bewusst so gewollt? Sonst Connect in *Plugins* entfernen. Flash Backup auf keinen Fall einschalten |
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
| „Ein SSH-Key fehlt“ | Der Key „power“ fehlt (Test 3, 11.6) | `twinraid-admin init-ssh-keys --key power` und die Zeile auf Groß eintragen |
| „Der SSH-Hostkey des Servers ist nicht bestätigt“ | Hostkey von Groß noch nicht bestätigt | `twinraid-admin trust-host-key target` (10.2) |
| „Der SSH-Hostkey des Servers hat sich geändert“ | Groß zeigt einen anderen SSH-Hostkey als bestätigt; TwinRaid sendet nichts mehr | Hast du Groß neu eingerichtet oder den USB-Stick ersetzt, den Fingerabdruck wie in 11.6 Schritt 3 prüfen und neu bestätigen. Sonst nichts bestätigen und herausfinden, warum er anders ist |
| „Der Server lehnt den SSH-Key ab“ (bei einer Aktion) | Zeile für „power“ fehlt unter *Users → root → SSH authorized keys*, ist abgeschnitten, oder TwinRaid hat eine andere IP als in `from=` | `twinraid-admin init-ssh-keys --key power` erneut ausführen (gibt die Zeile wieder aus) und die Zeile auf Groß ersetzen |
| „SSH auf dem Server ist nicht erreichbar“ | SSH auf Groß aus oder anderer Port | *Settings → Management Access → Use SSH* einschalten; abweichenden Port als `servers.target.ssh_port` eintragen |
| „guard.sh auf dem Server hat den Befehl abgelehnt“ | `guard.sh` auf Groß ist veraltet, oder die Zeile für „power“ ruft einen anderen Modus auf | `guard.sh` wie in 9.2 neu holen (Version im Pfad = installierte Version) |
| „Der Befehl ist auf dem Server fehlgeschlagen“ | `guard.sh` fehlt unter `/boot/config/twinraid/guard.sh`, oder `virsh` meldet einen Fehler | Ablage von `guard.sh` prüfen (9.2); Syslog von Groß ansehen (*Tools → System Log*, `twinraid-guard`) |
| „Die VM ist auf dem Server nicht eingetragen“ (bei einer Aktion) | `power.conf` fehlt oder ist ungültig | Schritt 9.3 |
| „Der Server wurde nicht ausgeschaltet, weil die VM noch lief“ | `guard.sh` hat unmittelbar vor dem Ausschalten eine laufende VM gesehen | VM herunterfahren (lassen) und Groß erneut herunterfahren |
| „Der Server war nicht rechtzeitig bereit“ | Groß ist nach dem Wecken nicht (rechtzeitig) gestartet, oder das Array startet nicht automatisch | Wake-on-LAN im BIOS und mit `ethtool` prüfen, Auto-Start des Arrays einschalten (11.1); bei langem Start `boot_timeout_minutes` erhöhen |
| „Die VM hat sich nicht rechtzeitig heruntergefahren“ | Die VM reagiert nicht auf das Signal (Netzschalter-Einstellung in Windows, laufende Updates) | Netzschalter auf „Herunterfahren“ stellen (11.1), `shutdown_timeout_minutes` erhöhen oder **Erzwingen**; nach einem gescheiterten Herunterfahren von Groß 10 Minuten lang auch **Erzwingen und danach Server herunterfahren** (11.12) |
| Meldung grau, ohne Hinweis darunter | Das Ergebnis ist nur noch Verlauf: Der Hinweis (z. B. auf „Erzwingen“) trifft nicht mehr zu, etwa weil die VM inzwischen aus ist | Nichts zu tun |
| Groß: **Weitere laufende VMs: …** (ab 0.4.4) | Auf Groß laufen außer der Gaming-VM weitere VMs, auch pausierte; ausgeschaltete erscheinen nicht | Nur Anzeige, TwinRaid steuert diese VMs nicht. Solange sie laufen, schaltet TwinRaid Groß nicht aus (nächste Zeile) |
| Herunterfahren gesperrt: „Auf … laufen noch weitere VMs: …“ | Außer der Gaming-VM laufen auf Groß weitere VMs (auch pausierte oder abgestürzte zählen) | Die genannten VMs in der Unraid-Oberfläche von Groß herunterfahren; nach etwa 10 Sekunden ist der Button wieder aktiv |
| „Der Server wurde nicht ausgeschaltet, weil noch weitere VMs liefen“ | Eine weitere VM lief beim Herunterfahren (geprüft unmittelbar vor dem Ausschalten); Groß ist an geblieben | Die VM herunterfahren und Groß erneut herunterfahren |
| Herunterfahren gesperrt: „der Zustand der VM ist unbekannt“ | TwinRaid kann die VM über `guard.sh` gerade nicht lesen | Ursache in der VM-Kachel beheben (z. B. Key „status“, `power.conf`, VM-Dienst) oder Groß ausnahmsweise über die Unraid-Oberfläche herunterfahren |
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

> **Waren die Dateien für alle lesbar?** Die Werkzeuge geben allen Benutzern Lesezugriff. Ist `appdata` als Netzwerkfreigabe für andere Personen erreichbar, sicherheitshalber neu erzeugen: eine neue CA mit `twinraid-admin renew-cert --new-ca` (danach auf den Geräten neu installieren), neue SSH-Keys mit `twinraid-admin init-ssh-keys --key status --new` und `--key power --new` (die Zeilen auf Groß ersetzen) und ein neues Passwort mit `twinraid-admin set-password`.

**Abgemeldet nach 10 Minuten:** Ohne Klick endet die Sitzung nach `session_idle_minutes` (Standard 10). Ein offenes Dashboard zählt nicht als Aktivität; nur während ein Kopierjob läuft, bleibt die Sitzung mit offenem Dashboard bestehen (ab Phase 5). Den Wert kannst du in `twinraid.yaml` erhöhen (höchstens 720).

**Gesperrt nach falschen Passwörtern:** Nach 5 Fehlversuchen in 15 Minuten ist die Anmeldung von diesem Gerät 15 Minuten gesperrt. Sofort aufheben: `twinraid-admin unlock` in der Container-Konsole.

---

## 14. Updates einspielen und Herkunft prüfen

- Unraid prüft regelmäßig, ob es eine neue Version gibt. Im Reiter **Docker** steht dann bei TwinRaid **update ready**. Klick auf das Symbol → **Update**. Daten und Einstellungen bleiben erhalten, die MAC dank des Felds **Fixed MAC address** ebenso. Nach dem ersten Update einmal wie in Schritt 5.4 vergleichen.
- Von 0.4.3 auf 0.4.4 genügt das Update: `guard.sh` (Version 2), `power.conf`, die Keys und `twinraid.yaml` bleiben unverändert.
- Das Template verwendet `ghcr.io/mantua1/twinraid:latest`. Dieser Tag zeigt immer auf das neueste Release. Wer eine Version festhalten möchte, trägt unter *Repository* zum Beispiel `ghcr.io/mantua1/twinraid:0.3` ein.
- Jedes Image wird ausschließlich von GitHub Actions aus diesem Repository gebaut und hat einen signierten Herkunftsnachweis. Prüfen kannst du ihn auf einem PC mit der GitHub-CLI (`gh`):

```bash
gh attestation verify oci://ghcr.io/mantua1/twinraid:0.4.4 --owner mantua1
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
| `twinraid-admin init-ssh-keys` | SSH-Keys erzeugen (falls noch keine da sind) und die Zeilen für *Users → root → SSH authorized keys* auf Groß ausgeben: „status“ immer, „power“, sobald `actions.vm` oder `actions.shutdown` freigeschaltet ist. `--key status` bzw. `--key power` nur für diesen Key; `--key … --new` ersetzt ihn |
| `twinraid-admin trust-host-key target` | SSH-Hostkey von Unraid Groß bestätigen und die Verbindung zu `guard.sh` mit jedem vorhandenen Key testen (ändert nichts auf Groß). Besteht der Test mit dem Key „status“, beendet er eine Wartezeit der Statusverbindung sofort (ab 0.4.4) |
| `twinraid-admin show-action-log` | Aktionslog des Workers anzeigen (`--lines 100` für mehr Einträge) |
| `twinraid-admin mock-job on` / `off` | Nur im Simulationsmodus: einen laufenden Kopierjob vortäuschen |
| `twinraid-admin mock-tour on` / `off` | Nur im Simulationsmodus: alle Zustände und Ursachen nacheinander zeigen (Aktionen sind dann gesperrt) |
| `twinraid-admin mock-fault <Name> on` / `off` | Nur im Simulationsmodus: Fehler vortäuschen, `<Name>` ist `vm-hang`, `ssh-denied`, `no-boot`, `other-vm` (eine weitere VM läuft), `vm-not-configured`, `libvirt-down`, `guard-outdated`, `connect-plugin` oder `status-key-rejected` (Groß lehnt den Key „status“ ab) |

`set-api-key` und `trust-api-cert` gibt es seit 0.4.3 nicht mehr.
