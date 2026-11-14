# TwinRaid auf Unraid einrichten

> **Stand: Version 0.5.2.** Diese Version zeigt den Status beider Server und der Gaming-VM an und kann Unraid Groß wecken und herunterfahren sowie die Gaming-VM starten, herunterfahren und erzwingen. Status und VM laufen über SSH und `guard.sh` auf Groß; einen API-Key von Unraid braucht TwinRaid seit 0.4.3 nicht mehr. Die Aktionen werden einzeln freigeschaltet (Abschnitt 11). Seit 0.5.0 gibt es den Wächter auf Klein (Abschnitt 12), die Vorbereitung von Groß für den Spiegel (Abschnitt 13) und das eigene Dataset für TwinRaid (Abschnitt 14). Seit 0.5.1 gibt es „Nur Probelauf“ mit `guard.sh` Version 3 und `source-guard.sh` Version 2 (Abschnitt 15). **Neu in 0.5.2** ist der echte Sync der Test-Share mit `source-guard.sh` Version 3 (Abschnitt 16): Container kurz anhalten, Löschungen freigeben, Fortschritt mit Restzeit, Abbrechen und eine Sync-Historie. **Von 0.5.0 auf 0.5.1 genügt das Update nicht:** Vorher die Schlüssel `exclude` und `protect_on_target` aus `twinraid.yaml` entfernen, sonst startet der Container nicht, und die Reihenfolge aus Abschnitt 15 einhalten. Von 0.5.1 auf 0.5.2 genügt das Update für den bisherigen Betrieb; für den Sync folgen die Skripte in der Reihenfolge aus Abschnitt 16. Wer von 0.4.2 kommt, beginnt mit Abschnitt 17. Die Anleitung wird mit jeder Version erweitert und ist in Phase 7 fertig.

Diese Anleitung führt Schritt für Schritt durch die Installation auf **Unraid Klein** (dem stromsparenden Server, der immer läuft). Am Ende:

- läuft TwinRaid als Container mit eigener IP- und MAC-Adresse im Server-Netz,
- ist die Weboberfläche per HTTPS aus dem Server-Netz und dem Client-Netz erreichbar,
- vertrauen deine Geräte (Windows, Linux, Android) dem Zertifikat ohne Warnung,
- zeigt das Dashboard den echten Status von Unraid Klein, Unraid Groß und der Gaming-VM,
- kannst du Groß und die Gaming-VM aus dem Dashboard heraus starten und herunterfahren (Abschnitt 11),
- läuft auf Klein der Wächter für den späteren Sync, und Groß ist für die Spiegel vorbereitet (Abschnitte 12 bis 14),
- zeigt „Nur Probelauf“, was ein Sync der Test-Share senden würde (Abschnitt 15),
- spiegelt „Jetzt synchronisieren“ die Test-Share nach Groß (Abschnitt 16).

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
12. [Wächter auf Klein einrichten (ab 0.5.0)](#12-wächter-auf-klein-einrichten-ab-050)
13. [Unraid Groß für den Spiegel vorbereiten (ab 0.5.0)](#13-unraid-groß-für-den-spiegel-vorbereiten-ab-050)
14. [Eigenes Dataset für TwinRaid (Test Z4)](#14-eigenes-dataset-für-twinraid-test-z4)
15. [Probelauf einrichten (ab 0.5.1)](#15-probelauf-einrichten-ab-051)
16. [Echter Sync der Test-Share (ab 0.5.2)](#16-echter-sync-der-test-share-ab-052)
17. [Von 0.4.2 auf 0.4.3 umstellen](#17-von-042-auf-043-umstellen)
18. [Fehlerbilder und Abhilfe](#18-fehlerbilder-und-abhilfe)
19. [Updates einspielen und Herkunft prüfen](#19-updates-einspielen-und-herkunft-prüfen)
20. [Befehle der Admin-CLI](#20-befehle-der-admin-cli)

### So sind die Schritte zu lesen

- **Unraid-Terminal:** In der Unraid-Oberfläche oben rechts auf das Symbol `>_` klicken. Es öffnet sich ein Fenster mit einer Kommandozeile auf dem Server.
- **Container-Konsole:** Reiter **Docker**, auf das Symbol des Containers *TwinRaid* klicken, dann **Console**. Dort laufen die Befehle `twinraid-admin …` innerhalb des Containers.
- Befehle in grauen Kästen kopierst du vollständig und fügst sie ein (im Terminal mit Rechtsklick oder `Strg+Umschalt+V`), dann `Enter`.
- Die Bezeichnungen in der Unraid-Oberfläche stehen hier auf Englisch, wie in der Standardsprache. In der deutschen Oberfläche heißen sie sinngemäß gleich.
- **Ein Befehl pro Kasten, wo es darauf ankommt:** Befehle, die bei doppelter Ausführung schaden (etwa eine Sicherung überschreiben oder Testdaten zweimal ändern), stehen allein in einem Kasten mit einem Hinweis davor. Diese Kästen nur einmal einfügen.
- **Welche Fassung der Skripte:** Die Abschnitte 3 bis 15 laden die Dateien von `v0.5.1`, Abschnitt 16 die von `v0.5.2`. So passt jede erwartete Ausgabe zu der Fassung, für die der Test geschrieben ist. TwinRaid 0.5.2 arbeitet mit beiden Fassungen; wer neu einrichtet, geht die Abschnitte der Reihe nach durch und bringt die Skripte in Abschnitt 16 auf den Stand von 0.5.2.

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
- Für den Sync müssen Container auf Klein kurz angehalten werden. Das übernimmt ab 0.5.0 der Wächter auf dem Host von Klein (Abschnitt 12); TwinRaid braucht dafür keine Netzverbindung zu Klein.

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

> **Vorsicht mit „New Permissions“ und „Docker Safe New Perms“.** Das Unraid-Werkzeug *Tools → New Permissions* und *Docker Safe New Perms* aus dem Plugin *Fix Common Problems* setzen Besitzer und Rechte auf `nobody:users` und Schreibrechte für alle zurück. *New Permissions* tut das für alle gewählten Shares und Datenträger, auch für `appdata`. *Docker Safe New Perms* lässt `appdata` normalerweise aus, aber nur, solange es in seinen Einstellungen ausgenommen ist. Erwischt eines der Werkzeuge den Datenordner von TwinRaid, startet der Container danach nicht mehr. Wie du das reparierst, steht in Abschnitt 18 unter „Rechte des Datenordners zurückgesetzt“.

> **Hinweis zu `appdata`:** TwinRaid speichert eine kleine SQLite-Datenbank. Am zuverlässigsten liegt `appdata` auf einem Pool (z. B. Cache) mit der Share-Einstellung *Exclusive access*. Bei den Unraid-Standardeinstellungen ist das meist schon so.

**3.2 Beispielkonfiguration herunterladen.** Die Datei ist in `v0.5.1` und `v0.5.2` gleich (zu den Fassungen siehe „So sind die Schritte zu lesen“).

```bash
wget -O /mnt/user/appdata/twinraid/config/twinraid.yaml \
  https://raw.githubusercontent.com/mantua1/twinraid/v0.5.1/config.example.yaml
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
- `sync.shares`: die Datasets, die gespiegelt werden sollen. Am Anfang nur die Test-Share `twinraidtest` (Abschnitt 15); dieselben Namen stehen in `source-guard.conf` auf Klein und `mirror.conf` auf Groß. Der echte Sync kommt mit 0.5.2.
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
  https://raw.githubusercontent.com/mantua1/twinraid/v0.5.1/docker/unraid-template.xml
```

**4.2 Container anlegen:**

1. Reiter **Docker** → unten **Add Container**.
2. Bei **Template** unter *User templates* den Eintrag **TwinRaid** wählen.
3. **Network Type:** `Custom: br0` (ist vorausgewählt).
4. **Fixed IP address:** die feste IP für TwinRaid aus Schritt 1. Das ist die einzige Stelle, an der die IP steht.
5. **Fixed MAC address** (direkt darunter, ab Unraid 7.3): die MAC aus Schritt 1. Auch sie steht nur hier.
6. **Betriebsmodus:** `mock` lassen.
7. **Konfiguration** und **Daten:** die vorgegebenen Pfade lassen (`/mnt/user/appdata/twinraid/config` und `…/data`).
8. **Wächter: Auftragsdatei** und **Wächter: Status:** leer lassen. Sie werden erst eingetragen, wenn der Wächter auf Klein läuft (Abschnitt 12); bis dahin zeigt die Kachel Klein „Wächter nicht eingerichtet“.
9. **Apply** klicken.

Unraid übergibt Netzwerk, IP und MAC zusammen an Docker. In der Ausgabe nach **Apply** steht deshalb eine Zeile mit `--network='name=br0,ip=…,mac-address=…'`. Die MAC bitte **nicht** zusätzlich als `--mac-address` in die *Extra Parameters* schreiben; das Feld ist der vorgesehene Weg.

Die zusätzlichen Docker-Parameter (*Extra Parameters*, nur in der *Advanced View* sichtbar) härten den Container: schreibgeschütztes Dateisystem, nur fünf nötige Rechte, kein Rechteausbau. Bitte nicht ändern.

---

## 5. Erster Start: Log prüfen und Passwort setzen

**5.1 Log ansehen:** Reiter **Docker** → Symbol *TwinRaid* → **Logs**. Du solltest unter anderem sehen:

- einen deutlichen Hinweis **SIMULATION MODE ACTIVE**,
- `TLS address: 192.168.10.20 (detected on eth0)` mit deiner festen IP. Steht hier eine andere Adresse oder eine Warnung **PROBABLY THE WRONG NETWORK TYPE**, ist im Template nicht `Custom: br0` gewählt (siehe Abschnitt 18).
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

Hat sich die MAC geändert, ist das Feld **Fixed MAC address** leer oder die Unraid-Version älter als 7.3. Ohne feste MAC vergibt Docker bei jedem Start eine neue. Wiederhole den Vergleich nach dem ersten echten Update (Abschnitt 19).

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

**9.2 `guard.sh` holen.** Im **Unraid-Terminal von Groß** (Fassung von `v0.5.1`; Abschnitt 16 bringt sie auf den Stand von 0.5.2):

```bash
mkdir -p /boot/config/twinraid
```

```bash
wget -O /tmp/guard.sh \
  https://raw.githubusercontent.com/mantua1/twinraid/v0.5.1/host-scripts/guard.sh
sha256sum /tmp/guard.sh
```

Erwartet genau `c5f69807b76eb74af373361c47ecad3e39e718c278df10edba0e2bad40cae47f  /tmp/guard.sh`. Weicht der Wert ab, hier abbrechen und nichts kopieren. Stimmt er:

```bash
cp /tmp/guard.sh /boot/config/twinraid/guard.sh.neu
mv /boot/config/twinraid/guard.sh.neu /boot/config/twinraid/guard.sh
head -2 /boot/config/twinraid/guard.sh
```

Die beiden Zeilen lauten `#!/bin/bash` und `# TwinRaid: SSH-Wächter auf Unraid Groß, Version 3 (Konzept 3.5, 3.7 und 5.6).` Ausführbar machen ist nicht nötig: Unraid ruft das Skript über `/bin/bash` auf.

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

   TwinRaid zeigt `ED25519 SHA256:…`. Mit Schritt 3 vergleichen; nur wenn beide gleich sind, `yes` eingeben. Erwartet: `Host key confirmed. From now on TwinRaid only accepts exactly this key.`, `OK: guard.sh answers (twinraid-guard 3 status ok).` und `TwinRaid connects with the key 'status' right away.`

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

TwinRaid 0.4.3 oder neuer läuft im Echtbetrieb, der Key „status“ ist eingerichtet (Abschnitt 10 bzw. 15), `actions` fehlt in `twinraid.yaml` oder steht überall auf `false`. Steht dort `wol_broadcast`, kannst du die Zeile löschen; TwinRaid leitet die Adresse dann selbst ab.

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
| `check` im Modus status | `twinraid-guard 3 status ok`, Exit `0` |
| `poweroff` und `vm-start` im Modus status | `twinraid-guard: denied`, Exit `2`; Groß bleibt an, die VM aus |
| `vm-shutdown <UUID>` im Modus power (mit Parameter) | `twinraid-guard: denied`, Exit `2` |
| `check; reboot` | `twinraid-guard: denied`, Exit `2` |
| `vm-watch` (5 Sekunden lang) | eine Zeile wie `watch 3 vm=SHUTOFF name=… others=0 names=- connect=absent flash_backup=inactive arc_max=… c_max=…`, dann Exit `124` (beendet von `timeout`) |
| *Tools → System Log* | Einträge `twinraid-guard: denied: …` ohne den Text der Anfrage |

**`power.conf` fehlt:** Die Datei kurz umbenennen und die Statusabfrage wiederholen, danach zurückbenennen:

```bash
mv /boot/config/twinraid/power.conf /boot/config/twinraid/power.conf.bak
SSH_ORIGINAL_COMMAND=vm-watch timeout 5 bash $G status
mv /boot/config/twinraid/power.conf.bak /boot/config/twinraid/power.conf
```

Erwartet: `watch 3 vm=NOT_CONFIGURED name=- others=? names=- …`. Weil `guard.sh` die Datei alle 15 Sekunden neu liest, kann das Dashboard in dieser Zeit kurz „VM auf dem Server nicht eingetragen“ zeigen; danach zeigt es wieder den Zustand der VM.

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
| Ausgabe | `This host key was already confirmed.`, `OK: guard.sh answers (twinraid-guard 3 status ok).`, `OK: guard.sh answers (twinraid-guard 3 power ok).` und ab 0.4.4 `TwinRaid connects with the key 'status' right away.` |
| Unraid Groß | nichts passiert; der Test verändert nichts |

Meldet der Befehl einen Fehler, steht die Abhilfe dabei, zum Beispiel bei abgelehntem Key (`Users → root`), ausgeschaltetem SSH oder fehlendem `guard.sh`. Siehe auch Abschnitt 18.

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
   | VM-Kachel | kurz **Startet …**, nach wenigen Sekunden wieder **Offline**; rot „Zuletzt: VM starten – Ein durchgereichtes Gerät der VM, etwa die Grafikkarte, wird gerade von einer anderen VM benutzt.“, darunter „Weitere laufende VMs: …“ mit dem Namen der zweiten VM |
   | Kachel Groß | bleibt **Online**; **Herunterfahren** ist grau mit dem Hinweis auf weitere VMs und dem Namen der zweiten VM (wie in Test 11) |
   | zweite VM | läuft ohne Unterbrechung weiter: Bild, Ton, USB-Geräte und Programme in der VM arbeiten wie vorher; im Reiter **VMs** weiterhin **Started** |
   | `twinraid-admin show-action-log` | `vm_start  dashboard: vm_device_busy` |
   | Syslog von Groß (*Tools → System Log*) | `twinraid-guard: vm-start of VM … requested by TwinRaid`, danach `twinraid-guard: refused: device-busy` |
   | Grund, im **Unraid-Terminal von Groß** | `grep -i 'in use' /var/log/libvirt/libvirtd.log \| tail -n 3` zeigt eine Zeile mit `Requested operation is not valid: PCI device … is in use by driver QEMU, domain …` |

   `guard.sh` erkennt das belegte Gerät seit Version 3 an einem festen Muster in der Meldung von `virsh`; die Meldung selbst gibt es nie an TwinRaid weiter (Konzept 3.5). Die vollständige Meldung steht nur im Log von libvirt. Andere Fehler beim Start zeigt die Kachel als „Unraid konnte die VM nicht starten; den Grund nennt das libvirt-Log.“
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
   | Ausgabe | `This host key was already confirmed.`, `OK: guard.sh answers (twinraid-guard 3 status ok).`, falls der Key „power“ eingerichtet ist `OK: guard.sh answers (twinraid-guard 3 power ok).` und zum Schluss `TwinRaid connects with the key 'status' right away.` |
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

## 12. Wächter auf Klein einrichten (ab 0.5.0)

Ab 0.5.0 läuft auf dem Host von **Unraid Klein** der **Wächter** `source-guard.sh`. Ihn braucht der Sync (ab 0.5.1): Er hält die freigegebenen Container kurz an, legt ZFS-Snapshots an und sendet sie nach Groß. TwinRaid bekommt dafür weder Docker noch die Daten. Es schreibt dem Wächter nur Aufträge in eine einzelne Datei und liest seinen Status; was erlaubt ist, steht in `source-guard.conf` auf Klein. In 0.5.0 zeigte TwinRaid den Wächter nur an; ab 0.5.1 schreibt es Aufträge für den Probelauf (Abschnitt 15).

| Pfad auf Klein | Inhalt |
|---|---|
| `/boot/config/twinraid/source-guard.sh` | das Skript, eine Kopie aus dem Repository |
| `/boot/config/twinraid/source-guard.conf` | Erlaubnisliste und Zeiten, keine Secrets |
| `/run/twinraid-source-guard/in/request` | Auftragsdatei auf einem eigenen, 64 KiB kleinen tmpfs; in TwinRaid eingebunden (Read/Write) |
| `/run/twinraid-source-guard/state/status.json` | Status des Wächters; in TwinRaid nur lesend eingebunden (Read Only) |

> **Die Reihenfolge ist wichtig:** erst den Wächter starten, dann die zwei Pfade in TwinRaid eintragen. Startet TwinRaid mit den Pfaden, bevor der Wächter lief, legt Docker an der Stelle der Auftragsdatei ein Verzeichnis an (Test W8). Der Wächter startet deshalb aus der go-Datei, noch vor Docker.

Die Tests W1 bis W8 führen durch die Einrichtung. Sie arbeiten nur mit einem eigenen Testcontainer; deine anderen Container hält der Wächter nie an, solange sie nicht in seiner Erlaubnisliste stehen.

### 12.1 Vorbereitung: Test-Share und Testcontainer

1. **Share `twinraidtest` anlegen** (Unraid-Oberfläche von Klein): Reiter **Shares** → **Add Share** → Name `twinraidtest`, **Primary storage** `mainpool`, **Secondary storage** `None`, bei *SMB Security Settings* **Export** `No` → **Add Share**. Kontrolle im **Unraid-Terminal von Klein**:

   ```bash
   zfs list -o name,mountpoint mainpool/twinraidtest
   ```

   Erwartet: `mainpool/twinraidtest  /mnt/mainpool/twinraidtest`. Fehlt die Zeile, hat Unraid für die Share kein eigenes Dataset angelegt; dann liegt `mainpool` nicht als ZFS vor oder die Share auf einem anderen Pool.
2. **Testcontainer anlegen.** Er schreibt alle 0,1 Sekunden einen Zähler in die Test-Share und beim Anhalten die Zeile „sauber beendet“; er hat kein Netzwerk und keine Rechte.

   ```bash
   mkdir -p /mnt/user/twinraidtest/testcontainer
   wget -O /boot/config/plugins/dockerMan/templates-user/my-twinraid-testcontainer.xml \
     https://raw.githubusercontent.com/mantua1/twinraid/v0.5.1/host-scripts/twinraid-testcontainer.xml
   ```

   Dann Reiter **Docker** → **Add Container** → Template **twinraid-testcontainer** → **Apply**. Danach im Reiter **Docker** beim Testcontainer **Autostart** einschalten (Schalter in der Spalte *Autostart*), damit er nach einem Neustart von Klein wieder läuft (Test W6).
3. **Kontrolle:** Den folgenden Befehl zweimal im Abstand von ein paar Sekunden ausführen; die Zahl muss wachsen.

   ```bash
   cat /mnt/user/twinraidtest/testcontainer/counter
   ```

### 12.2 Test W1: Update auf 0.5.0, die Pfade bleiben leer

Dieser Test zeigt, dass Unraid einen leeren Pfad im Template auslässt und Docker dann nichts unter `/run/twinraid-source-guard` anlegt. Der Wächter ist dafür noch **nicht** installiert.

1. Im **Unraid-Terminal von Klein**:

   ```bash
   ls -ld /run/twinraid-source-guard
   ```

   Erwartet: `No such file or directory`.
2. TwinRaid aktualisieren: Reiter **Docker** → bei *TwinRaid* **update ready** → **Update** (Abschnitt 19).
3. **Die zwei Einträge leer anlegen.** Wer TwinRaid neu installiert (Abschnitt 4), hat sie schon im Template; dann bei Punkt 4 weiter. Wer von 0.4.x kommt, ergänzt sie einmal von Hand, weil Unraid ein vorhandenes Template beim Update nicht erweitert: Reiter **Docker** → Symbol *TwinRaid* → **Edit** → unten **Add another Path, Port, Variable, Label or Device**:

   | Feld | Auftragsdatei | Status |
   |---|---|---|
   | **Config Type** | `Path` | `Path` |
   | **Name** | `Wächter: Auftragsdatei` | `Wächter: Status` |
   | **Container Path** | `/srv/worker/source-guard/request` | `/srv/worker/source-guard/state` |
   | **Host Path** | leer lassen | leer lassen |
   | **Default Value** | leer lassen | leer lassen |
   | **Access Mode** | `Read/Write` | `Read Only` |

   Jeweils **Add**. Lässt Unraid das Speichern mit leerem **Host Path** nicht zu, die Einträge jetzt weglassen und in W2 Punkt 6 mit Wert anlegen; das Ergebnis im PR festhalten.
4. **Apply.** Unraid zeigt den Docker-Befehl an. Er darf **keine** Zeile mit `source-guard` enthalten.
5. Im **Unraid-Terminal von Klein**:

   ```bash
   docker inspect --format '{{range .Mounts}}{{.Destination}} {{end}}' TwinRaid
   ls -ld /run/twinraid-source-guard
   ```

| Prüfen | Erwartet |
|---|---|
| Docker-Befehl nach **Apply** | keine Zeile mit `source-guard` |
| `docker inspect` | nur `/config` und `/data` |
| `ls -ld /run/twinraid-source-guard` | weiterhin `No such file or directory` |
| Kachel **Unraid Klein** | grau: „Wächter nicht eingerichtet. Die Schritte stehen in docs/EINRICHTUNG.md.“; kein roter Text |
| **Einstellungen → Wächter auf Unraid Klein** | „Wächter nicht eingerichtet.“ |

**Lässt Unraid den leeren Pfad nicht weg** (der Docker-Befehl enthält eine Zeile mit `source-guard`, das Anlegen scheitert, oder `/run/twinraid-source-guard` gibt es danach): beide Einträge wieder entfernen (**Edit** → beim Eintrag **Remove** → **Apply**), ein von Docker angelegtes `/run/twinraid-source-guard` mit `rm -r /run/twinraid-source-guard` löschen (es liegt nur im Arbeitsspeicher, und der Wächter ist noch nicht installiert) und die zwei Einträge erst in W2 Punkt 6 mit Wert anlegen. Das Ergebnis bitte im PR festhalten.

### 12.3 Test W2: Wächter installieren und starten

1. **Skript und Konfiguration holen** (Unraid-Terminal von Klein; Fassung von `v0.5.1`, Abschnitt 16 bringt das Skript auf Version 3):

   ```bash
   mkdir -p /boot/config/twinraid
   ```

   ```bash
   wget -O /tmp/source-guard.sh \
     https://raw.githubusercontent.com/mantua1/twinraid/v0.5.1/host-scripts/source-guard.sh
   sha256sum /tmp/source-guard.sh
   ```

   Erwartet genau `d4f05ada883386b365520296312f916fc57d0a283725c0b3c8d5d3ce0f85d647  /tmp/source-guard.sh`. Weicht der Wert ab, hier abbrechen und nichts kopieren. Stimmt er:

   ```bash
   cp /tmp/source-guard.sh /boot/config/twinraid/source-guard.sh.neu
   mv /boot/config/twinraid/source-guard.sh.neu /boot/config/twinraid/source-guard.sh
   ```

   Danach die Beispielkonfiguration holen und die Kopfzeile ansehen:

   ```bash
   wget -O /boot/config/twinraid/source-guard.conf \
     https://raw.githubusercontent.com/mantua1/twinraid/v0.5.1/host-scripts/source-guard.conf.example
   head -2 /boot/config/twinraid/source-guard.sh
   ```

   Die zweite Zeile lautet `# TwinRaid: Wächter auf dem Host von Unraid Klein, Version 2 (Konzept 3.6).` Ausführbar machen ist nicht nötig; der Aufruf läuft immer über `/bin/bash`.
2. **Konfiguration ansehen:** `nano /boot/config/twinraid/source-guard.conf`. Für die Tests bleibt `allow twinraid-testcontainer` die einzige Freigabe und `dataset twinraidtest` das einzige Dataset. Die Zeile `target_host` mit dem Beispielwert mit `#` auskommentieren; das Ziel trägst du in Test K1 ein (15.3). Speichern mit `Strg+O`, `Enter`, beenden mit `Strg+X`.
3. **Prüfen:**

   ```bash
   bash /boot/config/twinraid/source-guard.sh check-config
   ```

   Erwartet: `Konfiguration in Ordnung: 1 Container freigegeben, 1 Datasets.` Sonst nennt die Meldung Grund und Zeile.
4. **Start beim Hochfahren eintragen.** `nano /boot/config/go` und **vor** der Zeile mit `emhttp` diese Zeile einfügen:

   ```bash
   /bin/bash /boot/config/twinraid/source-guard.sh start
   ```

   Kontrolle mit `cat /boot/config/go`: Die neue Zeile steht über der Zeile mit `emhttp`. So startet der Wächter vor Docker und damit vor TwinRaid.
5. **Jetzt einmal von Hand starten:**

   ```bash
   bash /boot/config/twinraid/source-guard.sh start
   bash /boot/config/twinraid/source-guard.sh status
   ls -l /run/twinraid-source-guard/in/request
   findmnt -n -o FSTYPE,OPTIONS /run/twinraid-source-guard/in
   ```

   `start` wartet höchstens 5 Sekunden, bis der Wächter läuft. Meldet es stattdessen `Der Wächter wurde gestartet, meldet sich aber noch nicht; in einigen Sekunden status aufrufen.`, einfach kurz warten und `status` wiederholen.

6. **Pfade in TwinRaid eintragen:** Reiter **Docker** → Symbol *TwinRaid* → **Edit**. Bei **Wächter: Auftragsdatei** als Host Path `/run/twinraid-source-guard/in/request` (Access Mode `Read/Write`), bei **Wächter: Status** `/run/twinraid-source-guard/state` (Access Mode `Read Only`) eintragen; wer die Einträge in W1 nicht anlegen konnte, legt sie jetzt wie in der Tabelle dort an, mit diesen Werten. Immer `/run/…` schreiben, nicht `/var/run/…`. Der Auswahldialog von Unraid (Klick ins Feld) zeigt `/run` nicht an; den Pfad deshalb von Hand ins Feld schreiben. → **Apply**.

| Prüfen | Erwartet |
|---|---|
| `start` | `Der Wächter wurde gestartet.` |
| `status` | `Der Wächter läuft (PID …).`, danach eine Zeile JSON mit `"error":null`; ein gerade gestarteter Wächter kann noch den Status von vorher zeigen, dann nach einigen Sekunden wiederholen |
| `ls -l …/request` | eine leere Datei, Zeile beginnt mit `-rw-------` und enthält `root root` |
| `findmnt` | `tmpfs` mit `size=64k`, `mode=700`, `nosuid`, `nodev`, `noexec` |
| Docker-Befehl nach **Apply** | `-v '/run/twinraid-source-guard/in/request':'/srv/worker/source-guard/request':'rw'` und `…state':'/srv/worker/source-guard/state':'ro'` |
| Kachel **Unraid Klein** | „Wächter: aktiv · 1 Container freigegeben“, darunter klein „Status wird alle 5 s geprüft.“, und eine Zeile „ZFS: 1 Dataset, … frei auf mainpool.“ |
| **Einstellungen → Wächter auf Unraid Klein** | Version 2, die Grenzen aus `source-guard.conf`, „twinraid-testcontainer: läuft“, „mainpool/twinraidtest: … · Kompression on“ (oder `lz4`), Ziel „nicht eingetragen“ |
| Container-Log von TwinRaid | `source guard: active` |
| Syslog von Klein (*Tools → System Log*, Kennung `twinraid-source-guard`) | `started` |

### 12.4 Test W3: Selbsttest mit dem Testcontainer

Der Selbsttest schreibt dieselben Aufträge, die TwinRaid ab 0.5.2 schreibt: Lauf öffnen, den Testcontainer anhalten, Lauf beenden. Er läuft im **Unraid-Terminal von Klein**:

```bash
bash /boot/config/twinraid/source-guard.sh selftest twinraid-testcontainer
grep -c "sauber beendet" /mnt/user/twinraidtest/testcontainer/log
```

| Prüfen | Erwartet |
|---|---|
| Ausgabe | `Container twinraid-testcontainer vorher: true`, `stop: stopped (Container jetzt: false)`, `Container twinraid-testcontainer nachher: true`, `OK: angehalten und wieder gestartet.` |
| `grep -c` | mindestens `1`: Der Container wurde sauber angehalten, nicht abgewürgt |
| Kachel **Unraid Klein**, während des Tests | kurz „1 Container angehalten (twinraid-testcontainer), spätestens wieder an um hh:mm.“ |
| Kachel **Sync**, danach | „Nächster Sync frühestens um hh:mm.“, 30 Minuten nach dem Ende des Laufs |
| Syslog von Klein | `order 1: begin: opened`, `order 2: stop twinraid-testcontainer: stopped`, `order 3: finish: finished` (die Nummern zählen weiter, wenn schon Aufträge liefen) |

Nach jedem Lauf gilt eine **Pause von 30 Minuten** (`min_run_interval_minutes`): So kann auch ein übernommener TwinRaid die freigegebenen Container nicht dauernd anhalten. Für die nächsten Tests hebst du sie als root auf Klein auf; TwinRaid kann das nicht:

```bash
bash /boot/config/twinraid/source-guard.sh reset-run-interval
```

Erwartet: `Die Pause zwischen zwei Läufen ist aufgehoben.` Die Zeile in der Kachel **Sync** verschwindet nach wenigen Sekunden. **Vor jedem weiteren Selbsttest diesen Befehl ausführen.**

### 12.5 Test W4: Totmannschalter

Meldet sich TwinRaid während eines Laufs nicht mehr, startet der Wächter die angehaltenen Container nach der Herzschlag-Frist selbst wieder. Für den Test wird die Frist kurz auf 30 Sekunden gesetzt.

1. In `/boot/config/twinraid/source-guard.conf` `heartbeat_timeout_seconds 30` eintragen (statt `120`), dann:

   ```bash
   bash /boot/config/twinraid/source-guard.sh restart
   bash /boot/config/twinraid/source-guard.sh reset-run-interval
   bash /boot/config/twinraid/source-guard.sh selftest-deadman twinraid-testcontainer
   ```

2. Danach den Wert wieder auf `120` setzen und erneut `restart` und `reset-run-interval`.

| Prüfen | Erwartet |
|---|---|
| Ausgabe | `Angehalten. Ohne ping greift der Totmannschalter nach 30 s.`, nach etwa 30 bis 35 Sekunden `OK: Totmannschalter ausgelöst, Container läuft wieder.` |
| Kachel **Unraid Klein** | „Der letzte Lauf endete um hh:mm über den Totmannschalter: TwinRaid hat sich nicht mehr gemeldet.“ |
| Syslog von Klein | eine Warnung zum Totmannschalter (`deadman_heartbeat`) |
| **Einstellungen → Wächter** nach Punkt 2 | Herzschlag wieder `120 s` |

### 12.6 Test W5: Was der Wächter ablehnt

1. **Nicht freigegebener Container:** TwinRaid selbst steht nicht in `allow`.

   ```bash
   bash /boot/config/twinraid/source-guard.sh reset-run-interval
   bash /boot/config/twinraid/source-guard.sh selftest TwinRaid
   ```

   Den Namen so schreiben, wie er im Reiter **Docker** steht. Erwartet: `stop: not_allowed (Container jetzt: true)` und `OK: abgelehnt (not_allowed), Container unverändert.` Das Dashboard bleibt dabei erreichbar. Dasselbe mit einem weiteren deiner Container wiederholen (vorher wieder `reset-run-interval`); auch er bleibt unberührt.
2. **TwinRaid in der Erlaubnisliste:** In `source-guard.conf` vorübergehend eine Zeile `allow TwinRaid` ergänzen, dann:

   ```bash
   bash /boot/config/twinraid/source-guard.sh check-config
   bash /boot/config/twinraid/source-guard.sh restart
   ```

   Erwartet: `check-config` meldet `Konfiguration ungültig: allow enthält TwinRaid (TwinRaid).`. Die Kachel **Unraid Klein** zeigt rot „Wächter: source-guard.conf ist ungültig; er nimmt keine Aufträge an.“ mit „In allow steht TwinRaid selbst; der Wächter hält TwinRaid nie an.“ und darunter den gelben Hinweis, dass die Erlaubnisliste den Container von TwinRaid enthält. Die Zeile wieder löschen, `restart`; die Kachel zeigt wieder „aktiv“.

### 12.7 Test W6: Neustart von Klein

Wenn es passt, Unraid Klein neu starten. Ist beim Testcontainer kein Autostart eingeschaltet (12.1), ihn danach zuerst starten (Reiter **Docker** → *twinraid-testcontainer* → **Start**); sonst meldet der Selbsttest richtig `not_running`. Dann im **Unraid-Terminal von Klein**:

```bash
bash /boot/config/twinraid/source-guard.sh status | grep -o '"last_seq":[0-9]*'
ls -l /run/twinraid-source-guard/in/request
bash /boot/config/twinraid/source-guard.sh selftest twinraid-testcontainer
```

| Prüfen | Erwartet |
|---|---|
| `last_seq` | `"last_seq":0`: Nach einem Neustart beginnt die Zählung neu |
| `ls -l …/request` | eine Datei (`-rw-------`), kein Verzeichnis: Der Wächter lief vor Docker |
| Kachel **Unraid Klein** | „Wächter: aktiv …“, sobald TwinRaid läuft. Kurz nach dem Start kann dort „Wächter: Docker ist … nicht erreichbar“ stehen, bis Docker läuft; das verschwindet von selbst |
| Selbsttest | `OK: angehalten und wieder gestartet.`; im Syslog `order 1: begin: opened` |

Danach `reset-run-interval`.

### 12.8 Test W7: Hinweis zu Unraid Connect

Reiter **Plugins** auf Klein ansehen: Ist **Unraid Connect** installiert, zeigt die Kachel **Unraid Klein** den gelben Hinweis „Auf Unraid Klein ist das Plugin Unraid Connect installiert …“, bei aktivem Flash Backup zusätzlich „Flash Backup von Unraid Connect ist auf Unraid Klein aktiv.“ Ist es nicht installiert, erscheint kein Hinweis. Erwartet: Die Anzeige passt zur Plugins-Seite. Connect bitte nicht nur für den Test installieren; ab 0.5.1 liegt der Key „sync“ auf dem Boot-Medium von Klein.

### 12.9 Test W8: Verzeichnis statt Auftragsdatei

Startet TwinRaid, bevor der Wächter lief (etwa weil die Zeile in der go-Datei fehlt), legt Docker an der Stelle der Auftragsdatei ein Verzeichnis an. Was dann passiert, hängt vom Container ab:

- **Der Container lief schon einmal mit der Datei** (der Normalfall, etwa nach einem Neustart von Klein): Er startet gar nicht. Unraid meldet „Bad parameter“, Docker `not a directory: Are you trying to mount a directory onto a file (or vice-versa)?`.
- **Der Container wurde gerade neu angelegt** (Erstinstallation oder **Edit → Apply**): Er startet, und die Kachel **Unraid Klein** zeigt rot „An der Stelle der Auftragsdatei liegt ein Verzeichnis, weil TwinRaid vor dem Wächter gestartet ist. Wächter starten, dann TwinRaid neu starten …“.

Der Test stellt den Teil des Wächters nach, ohne TwinRaid dafür vor dem Wächter zu starten: Du legst das Verzeichnis selbst an, so wie Docker es tun würde. Alles spielt sich unter `/run` ab, also nur im Arbeitsspeicher.

1. Reiter **Docker** → *TwinRaid* → **Stop**.
2. Im **Unraid-Terminal von Klein** den Wächter stoppen, seinen Ordner entfernen und das Verzeichnis anlegen, wie Docker es täte:

   ```bash
   bash /boot/config/twinraid/source-guard.sh stop
   umount /run/twinraid-source-guard/in
   rm -r /run/twinraid-source-guard
   mkdir -p /run/twinraid-source-guard/in/request /run/twinraid-source-guard/state
   ```

3. Den Wächter starten und ansehen, was er meldet:

   ```bash
   bash /boot/config/twinraid/source-guard.sh start
   sleep 5
   bash /boot/config/twinraid/source-guard.sh status | grep -o '"error":"[a-z_]*","error_detail":"[a-z_]*"'
   findmnt /run/twinraid-source-guard/in || echo "kein tmpfs"
   ```

   Erwartet: `"error":"request_not_a_file","error_detail":"request_is_directory"` und `kein tmpfs`; im Syslog von Klein `request is a directory (TwinRaid started before the guard): stop TwinRaid, remove the directory, restart the guard, then start TwinRaid`. Der Wächter legt nichts über das Verzeichnis und nimmt keine Aufträge an.
4. **Beheben** wie im Ernstfall (unten), hier ab Punkt 2, weil TwinRaid schon gestoppt ist.

**So behebst du es im Ernstfall**, in dieser Reihenfolge:

1. TwinRaid stoppen, falls es läuft (Reiter **Docker** → *TwinRaid* → **Stop**).
2. Das Verzeichnis entfernen und den Wächter starten bzw. neu starten:

   ```bash
   rmdir /run/twinraid-source-guard/in/request
   bash /boot/config/twinraid/source-guard.sh restart
   sleep 5
   ls -l /run/twinraid-source-guard/in/request
   findmnt -n -o FSTYPE /run/twinraid-source-guard/in
   ```

   Erwartet: `ls` zeigt eine Datei (`-rw-------`, `root root`), `findmnt` zeigt `tmpfs`. Meldet `rmdir`, das Verzeichnis sei nicht leer, liegt darin etwas Unerwartetes: nachsehen, nicht löschen.
3. TwinRaid starten: Reiter **Docker** → *TwinRaid* → **Start** oder im Terminal `docker start TwinRaid`. Startet es trotzdem mit „Bad parameter“ nicht, über **Edit** → ohne Änderung **Apply** neu anlegen.
4. Die Kachel **Unraid Klein** zeigt wieder „Wächter: aktiv …“. Danach die Zeile in `/boot/config/go` prüfen (12.3), damit der Wächter beim nächsten Neustart vor Docker startet.

### 12.10 Simulation (optional)

Im Simulationsmodus ist der Wächter simuliert: aktiv, mit dem freigegebenen Container `twinraid-testcontainer` und dem Dataset `twinraidtest`. In der Container-Konsole: `twinraid-admin mock-fault source-guard-inactive on` (Wächter läuft nicht; „nicht aktiv seit“ zeigt die Uhrzeit des Einschaltens), `source-guard-lost` (Zustand verloren) und `source-guard-start-failed` (ein Container startet nach dem Lauf nicht wieder; danach zeigt die Kachel **Sync** die Pause). `twinraid-admin mock-tour on` zeigt nacheinander jeden Zustand des Wächters. `off` schaltet jeweils wieder aus.

### 12.11 Abschluss

Die Ergebnisse von W1 bis W8 bitte im Pull Request festhalten. Dort nur „Klein“ und „Groß“ schreiben, nur die Testnamen `twinraidtest` und `twinraid-testcontainer`, keine Namen deiner anderen Container, keine IP-Adressen und keine Größen.

---

## 13. Unraid Groß für den Spiegel vorbereiten (ab 0.5.0)

Ab 0.5.1 nimmt Groß die Snapshots von Klein an, in schreibgeschützte Spiegel unter `mainpool/twinraid-mirror/<Share>`. Dafür braucht Groß einen ZFS-Pool namens `mainpool`, der **nur** die Spiegel enthält; VMs und `system` liegen auf einem anderen Pool, den `guard.sh` nie erreicht. In 0.5.0 bereitest du Groß nur vor und prüfst mit zwei Machbarkeitstests (Z1 und Z3), dass ZFS und Unraid sich so verhalten wie geplant. TwinRaid spricht Groß dabei nicht an.

### 13.1 Wenn Groß noch keinen Pool `mainpool` hat

Hat Groß schon einen ZFS-Pool `mainpool`, der nur für die Spiegel da ist, weiter mit 13.2.

1. **Vorher sichern.** Unraid formatiert die Datenträger eines neuen Pools; alles darauf geht verloren.
2. **VMs und `system` gehören auf einen anderen Pool** als die Spiegel. Liegen `domains` und `system` auf den Datenträgern, die `mainpool` werden sollen, ziehen sie vorher auf einen anderen Pool um, bei ausgeschaltetem VM-Manager und Docker-Dienst. Wie das mit den Share-Einstellungen und dem Mover geht, beschreibt die Unraid-Dokumentation; TwinRaid ist daran nicht beteiligt.
3. **Pool anlegen:** Reiter **Main** → bei gestopptem Array **Add Pool** → Name `mainpool` → Datenträger zuordnen → beim ersten Datenträger **File system type** `zfs` und das passende Layout (z. B. `raidz1` bei drei Datenträgern, `mirror` bei zweien) → **Compression** `on` → Array starten und den Pool formatieren.
4. **Docker** braucht Groß im Normalbetrieb nicht: *Settings → Docker* → **Enable Docker** `No`.
5. **Grenze für den ZFS-Cache (ARC):** *Settings → Disk Settings* → **Tunable (zfs_arc_max)** auf einen festen Wert setzen. Wie viel du einträgst, hängt von den VMs ab, die gleichzeitig laufen sollen (13.6).

### 13.2 Groß prüfen

Diese Schritte prüfen den vorhandenen Stand und räumen auf, was Unraid sonst an der falschen Stelle anlegt (Konzept, Entscheidung E26). Alles im **Unraid-Terminal von Groß** bzw. in seiner Oberfläche.

1. **Bestand notieren** (nur zur Information):

   ```bash
   zpool status mainpool
   zfs list -r -o name,used,mountpoint mainpool
   zfs get -o name,property,value,source compression,atime,xattr,acltype,recordsize mainpool
   ls /boot/config/shares/
   ```

   Die Werte der obersten Ebene setzt Unraid bei jedem Start auf die Pool-Einstellungen zurück. Die Spiegel bekommen ab 0.5.1 bei jedem Empfang ihre eigenen, festen Werte.
2. **Share `twinraid-mirror` anlegen:** Reiter **Shares** → **Add Share** → Name `twinraid-mirror`, **Primary storage** `mainpool`, **Secondary storage** `None`, **Export** `No` (SMB und NFS) → **Add Share**. Kontrolle:

   ```bash
   zfs list -o name,mountpoint mainpool/twinraid-mirror
   ```

   Erwartet: `mainpool/twinraid-mirror  /mnt/mainpool/twinraid-mirror`.
3. **ISO-Ordner des VM-Managers:** *Settings → VM Manager* → **Default ISO storage path**. Zeigt er auf eine Share, die gespiegelt werden soll (Standard ist `/mnt/user/isos`), auf einen Ordner mit anderem Namen umstellen, z. B. eine eigene Share `vm-isos` auf dem Pool für VMs. Je nach Unraid-Version geht das nur bei ausgeschaltetem VM-Dienst; dann vorher alle VMs herunterfahren. Nach dem ersten Sync von isos kann der Pfad auch auf `/mnt/mainpool/twinraid-mirror/isos/` zeigen; dort sind aber keine Downloads möglich (etwa des VirtIO-ISOs), weil der Spiegel schreibgeschützt ist. Ebenso das Feld **Default Windows VirtIO driver ISO** auf derselben Seite: Es zeigt meist auf eine Datei unter `/mnt/user/isos/`; die Datei in den neuen Ordner kopieren und das Feld dorthin umstellen, oder das Feld leeren.
4. **Share-Konfigurationen mit den Namen der gespiegelten Shares entfernen.** Unraid legt für jede Share-Konfiguration ein Dataset auf oberster Ebene eines Pools an, auch auf einem anderen Pool als dem eingetragenen, wenn dieser fehlt. Im Normalbetrieb darf es auf Groß deshalb keine Konfiguration mit dem Namen einer gespiegelten Share geben (z. B. `appdata` oder `isos`). Für jeden Namen aus `sync.shares` (Beispiel `isos`):

   ```bash
   cat /boot/config/shares/isos.cfg
   ls -A /mnt/user/isos
   zfs list -t all -r -o name mainpool/isos
   ```

   Gibt es die Datei und ist die Share leer (`ls -A` gibt nichts aus, `zfs list` höchstens die eine Zeile `mainpool/isos`), die Share in Unraid löschen: Reiter **Shares** → Share anklicken → unten **Delete** ankreuzen → **Delete**. Ist die Share nicht leer, erst die Daten an einen anderen Ort bringen. Bleibt danach das Dataset `mainpool/isos` stehen, löschst du es von Hand, **ohne** `-r`:

   ```bash
   zfs destroy mainpool/isos
   ```

   Ohne `-r` löscht ZFS ein Dataset nur, wenn es keine Snapshots und keine Kinder hat; sonst meldet es einen Fehler, und du siehst nach, was darin liegt.

   **Lässt sich die Share in der Oberfläche nicht löschen** („Selected Pool is not available“), gibt es ihren eingetragenen Pool nicht mehr. Dann von Hand:

   1. Prüfen, dass sie leer ist, wie oben (`ls -A /mnt/user/isos` gibt nichts aus; `zfs list -t all -r -o name` auf jedem Pool zeigt höchstens ein leeres Dataset dieses Namens).
   2. Ein solches Dataset ohne `-r` löschen, z. B. `zfs destroy mainpool/isos`.
   3. Die Konfiguration sichern:

      ```bash
      mkdir -p /boot/config/twinraid/shares-sicherung
      cp /boot/config/shares/isos.cfg /boot/config/twinraid/shares-sicherung/
      ```

   4. Alle VMs herunterfahren, dann Reiter **Main** → **Stop** (Array und Pools stoppen).
   5. Die Datei löschen: `rm /boot/config/shares/isos.cfg`
   6. Reiter **Main** → **Start**. Die Share erscheint nicht mehr unter **Shares**.
5. **Kontrolle:** `ls /boot/config/shares/` zeigt keine Datei mit einem gespiegelten Namen mehr. `zfs list -r -o name mainpool` und derselbe Befehl mit dem Namen des Pools für VMs zeigen kein Dataset mit einem gespiegelten Namen auf oberster Ebene.
6. **ARC und Docker:**

   ```bash
   cat /sys/module/zfs/parameters/zfs_arc_max
   ```

   Erwartet: eine Zahl größer als `0` (bei `0` ist keine Grenze eingetragen, 13.1 Punkt 5). *Settings → Docker* zeigt **Enable Docker** `No`.

Datasets, die von einem Umzug übrig sind, dürfen vorerst in `mainpool` bleiben; TwinRaid und `guard.sh` fassen sie nie an. Gelöscht werden sie vor der ersten echten Share (Merkliste im Konzept, 10.2).

### 13.3 Test Z1: ZFS auf beiden Servern

`host-scripts/zfs-check.sh` prüft auf den echten Servern genau die ZFS-Befehle, die Wächter und `guard.sh` ab 0.5.1 verwenden: Snapshots, Senden, die feste Befehlszeile für den Empfang, was ohne `-F` scheitert, einen absichtlich bösen Strom mit Sperre und Zurücksetzen, Lesezeichen, Hold, Klone für den Notbetrieb und ob `receive:append` als zweite Sperre trägt. Auf Groß arbeitet es nur in einem eigenen Test-Dataset `mainpool/twinraid-mirror/zfs-check`, auf Klein in einem eigenen Pool in einer Datei unter `/tmp`. Alles, was es anlegt, trägt die Eigenschaft `twinraid:zfs-check`, und am Ende löscht es nur diese Objekte, einzeln und nie mit `-r`.

**Auf Groß** (nach 13.2; alle VMs aus, weil das Skript zusätzlich misst, ob `MemAvailable` den ZFS-Cache schon enthält):

```bash
wget -O /tmp/zfs-check.sh \
  https://raw.githubusercontent.com/mantua1/twinraid/v0.5.1/host-scripts/zfs-check.sh
zfs list -r -o name mainpool/twinraid-mirror > /tmp/zfs-check-vorher.txt
bash /tmp/zfs-check.sh target 2>&1 | tee /tmp/zfs-check-gross.txt; echo "Exit: ${PIPESTATUS[0]}"
zfs list -r -o name mainpool/twinraid-mirror | diff /tmp/zfs-check-vorher.txt - && echo "nichts übrig"
zfs allow mainpool/twinraid-mirror
```

**Auf Klein** (braucht 1,5 GiB freien Arbeitsspeicher und 1,25 GiB frei in `/tmp`):

```bash
wget -O /tmp/zfs-check.sh \
  https://raw.githubusercontent.com/mantua1/twinraid/v0.5.1/host-scripts/zfs-check.sh
bash /tmp/zfs-check.sh source 2>&1 | tee /tmp/zfs-check-klein.txt; echo "Exit: ${PIPESTATUS[0]}"
zpool list -H -o name
ls /tmp | grep twinraid-check || echo "nichts übrig"
```

| Prüfen | Erwartet |
|---|---|
| letzte Zeile, beide Server | `Alle Prüfungen in Ordnung.` und `Exit: 0` |
| Groß: `diff` | `nichts übrig` |
| Groß: `zfs allow` | keine Ausgabe (keine Rechte auf `twinraid-mirror`) |
| Groß: Zeilen mit `sharesmb` | `sharesmb auf Groß nicht im bösen Strom …`; ZFS legt auf Groß keine Samba-Freigabe an Unraid vorbei an |
| Klein: `zpool list` | nur deine Pools, kein `twinraid-check-…` |
| Klein: `ls /tmp` | `nichts übrig` |
| zum Notieren | die Zeilen `Befund -x …`, `receive:append: …` und auf Groß `Messung: MemAvailable sank um … % des ARC-Wachstums: …` |

- **`Exit: 1`** mit Zeilen `FEHLER …`: Das Verhalten weicht vom Plan ab. Nichts weiter einrichten und die Ausgabe in den PR stellen.
- **`Exit: 2`:** Eine Voraussetzung fehlt; die Meldung nennt sie (z. B. `zuerst den Schritt Groß prüfen ausführen`).
- **Abgebrochen** (Strg+C, Verbindung weg): Das Skript räumt selbst auf. Nach einem harten Abbruch entfernt `bash /tmp/zfs-check.sh cleanup target` bzw. `… cleanup source` die Reste, wieder nur eigene Objekte.

> **Nach Test K1 (15.3):** Besteht die Delegation auf `mainpool/twinraid-mirror`, bricht `zfs-check.sh target` ab. Vorher `zfs unallow -u 64000 mainpool/twinraid-mirror` und danach den `zfs allow`-Befehl aus 15.3.

Für den PR: Die Ausgaben enthalten nur Codes und Zahlen. Die Zeilen mit `MiB` zeigen Speichergrößen deines Servers; sie bleiben privat, übernimm nur die Prozentzeile.

### 13.4 Test Z3: Unraid legt nichts an

Dieser Test prüft, dass Unraid nach einem Neustart, nach Stopp und Start der Pools und beim Start des VM-Managers keine Datasets mit gespiegelten Namen und nichts unterhalb von `twinraid-mirror` anlegt und die Einstellungen hält. Den Namen deines Pools für VMs trägst du in den Befehlen bei `VMPOOL=` statt des Beispiels `vmpool` ein.

1. **Vergleichsstand speichern** (Unraid-Terminal von Groß; die Datei liegt auf `/boot`, damit sie den Neustart übersteht):

   ```bash
   VMPOOL=vmpool   # Beispiel: hier den Namen deines Pools für VMs eintragen
   { zfs list -r -o name mainpool; zfs list -r -o name "$VMPOOL"; ls /boot/config/shares/
     zfs get -H -o name,property,value,source compression,atime,xattr,acltype mainpool
     cat /sys/module/zfs/parameters/zfs_arc_max; } > /boot/config/twinraid/z3-vorher.txt
   ```

2. **Groß neu starten.** Danach den VM-Manager laufen lassen, eine VM kurz starten und wieder herunterfahren, dann vergleichen:

   ```bash
   VMPOOL=vmpool   # Beispiel: hier den Namen deines Pools für VMs eintragen
   { zfs list -r -o name mainpool; zfs list -r -o name "$VMPOOL"; ls /boot/config/shares/
     zfs get -H -o name,property,value,source compression,atime,xattr,acltype mainpool
     cat /sys/module/zfs/parameters/zfs_arc_max; } | diff /boot/config/twinraid/z3-vorher.txt - && echo gleich
   ```

3. **Array stoppen und wieder starten** (Reiter **Main**, alle VMs vorher aus), wieder den VM-Manager laufen lassen und eine VM kurz starten, und den Vergleich aus Punkt 2 wiederholen.
4. Aufräumen: `rm /boot/config/twinraid/z3-vorher.txt`.

| Prüfen | Erwartet |
|---|---|
| Vergleich nach Neustart und nach Stopp und Start | `gleich` |
| insbesondere | `mainpool` und `mainpool/twinraid-mirror` da, darunter nichts Neues; kein neues Dataset mit gespiegeltem Namen (etwa `mainpool/isos`) auf keinem Pool; keine neuen `.cfg`-Dateien mit gespiegeltem Namen; `compression` auf oberster Ebene wieder `on`; `zfs_arc_max` unverändert; der Pool für VMs und die VMs wie vorher |

Zeigt `diff` Unterschiede, die Zeilen mit `<` (vorher) und `>` (nachher) in den PR stellen, ohne Namen eigener VMs.

### 13.5 Was die Spiegel später schützt

Zur Einordnung: `guard.sh` empfängt ab 0.5.1 nur in `mainpool/twinraid-mirror/<Name>`, nur Namen aus `/boot/config/twinraid/mirror.conf`, nie mit `-F` und als eigener Benutzer ohne root, der nur anhängen darf (15.3). Jeder Empfang ist ein eigener Snapshot, den `guard.sh` frühestens nach 30 Tagen löscht. Einen gesperrten Spiegel setzt du nach [WIEDERHERSTELLUNG.md](WIEDERHERSTELLUNG.md) zurück. Die Spiegel sind schreibgeschützt, und ein fremdes Dataset an ihrer Stelle ersetzt `guard.sh` nie.

### 13.6 Arbeitsspeicher auf Groß

Gleichzeitig laufende VMs, der ZFS-Cache (ARC) bis zu seiner Grenze und Unraid selbst müssen zusammen in den Arbeitsspeicher von Groß passen. VMs mit durchgereichten Geräten (z. B. der Grafikkarte) belegen ihren ganzen Speicher schon beim Start. Ab 0.5.1 lehnt `guard.sh` den Start der Gaming-VM aus TwinRaid ab, wenn dafür nicht genug frei ist (`memory-low`, Test M1 in 15.12); ein Start über die Unraid-Oberfläche wird nicht geprüft. Feste Zahlen gibt TwinRaid nicht vor.

---

## 14. Eigenes Dataset für TwinRaid (Test Z4)

Ab 0.5.1 kann der Wächter `appdata` spiegeln. Darin liegen auch die Konfiguration und die Secrets von TwinRaid (CA-Schlüssel, SSH-Keys, Passwort-Hash). Sie dürfen nie nach Groß gelangen. Deshalb bekommt TwinRaid ein eigenes Dataset `mainpool/appdata/twinraid`: Ein Snapshot von `mainpool/appdata` enthält es nicht, und der Wächter sendet `appdata` nur, wenn `/config` und `/data` von TwinRaid darin liegen. Die Pfade im Template bleiben gleich.

**Voraussetzung:** `appdata` ist auf Klein ein eigenes Dataset. Im **Unraid-Terminal von Klein**:

```bash
zfs list -o name,mountpoint mainpool/appdata
ls -ld /mnt/mainpool/appdata/twinraid
```

Erwartet: `mainpool/appdata  /mnt/mainpool/appdata` und der Ordner von TwinRaid darin. Fehlt das Dataset, liegt `appdata` nicht als eigenes Dataset auf `mainpool`; dann diesen Abschnitt noch nicht ausführen.

1. **Vorher:** `bash /boot/config/twinraid/source-guard.sh status | grep -o '"self_dataset":"[a-z_]*"'` zeigt `"self_dataset":"missing"`.
2. Reiter **Docker** → *TwinRaid* → **Stop**.
3. Den bisherigen Ordner umbenennen, das Dataset anlegen und die Daten mit allen Rechten hineinkopieren:

   ```bash
   cd /mnt/mainpool/appdata
   mv twinraid twinraid.alt
   zfs create mainpool/appdata/twinraid
   cp -a /mnt/mainpool/appdata/twinraid.alt/. /mnt/mainpool/appdata/twinraid/
   zfs list -o name,mountpoint mainpool/appdata/twinraid
   ls -ld /mnt/user/appdata/twinraid/data
   ls -l /mnt/user/appdata/twinraid/data/tls/ca.pem
   ```

   Erwartet: das Dataset mit dem Mountpoint `/mnt/mainpool/appdata/twinraid`; der Datenordner unter dem **bisherigen** Pfad `/mnt/user/appdata/twinraid/data` mit `drwxr-xr-x` und `root root`; die Datei `ca.pem` vorhanden. **Fehlt `ca.pem` unter `/mnt/user/…`, TwinRaid nicht starten**: Es würde sonst eine neue CA erzeugen. Dann zurück wie unten unter „Rückweg“.
4. Reiter **Docker** → *TwinRaid* → **Start**. Nach etwa einer Minute:

   ```bash
   bash /boot/config/twinraid/source-guard.sh status | grep -o '"self_dataset":"[a-z_]*"'
   ```

| Prüfen | Erwartet |
|---|---|
| Dashboard | wie vorher erreichbar ohne Warnung zum Zertifikat; Anmeldung mit dem bisherigen Passwort; keine Meldung zu einer neuen CA |
| Container-Log | keine Meldung `Generated the TwinRaid CA` |
| Wächter | `"self_dataset":"found"` |

5. **Die alte Kopie löschen**, sobald TwinRaid wieder läuft. Sie enthält die Secrets von TwinRaid und liegt noch im Dataset `mainpool/appdata`; sie muss weg sein, bevor `appdata` zum ersten Mal gespiegelt wird. Zuerst prüfen, dass es von `mainpool/appdata` keine Snapshots gibt:

   ```bash
   zfs list -t snapshot -r mainpool/appdata
   ```

   Erwartet: `no datasets available`. **Zeigt der Befehl Snapshots, nicht weitermachen:** Die Secrets aus `twinraid.alt` stecken dann auch in diesen Snapshots und blieben nach dem Löschen der Kopie dort erhalten. Die Ausgabe im PR melden (ohne die Namen der Snapshots, falls sie private Angaben enthalten) und erst nach Rücksprache weiter. Ist die Liste leer:

   ```bash
   rm -r /mnt/mainpool/appdata/twinraid.alt
   ```

**Rückweg**, falls Punkt 3 oder 4 nicht wie erwartet ausgeht: TwinRaid stoppen, dann

```bash
zfs list -t snapshot -r mainpool/appdata/twinraid
zfs destroy mainpool/appdata/twinraid
rmdir /mnt/mainpool/appdata/twinraid 2>/dev/null || true
mv /mnt/mainpool/appdata/twinraid.alt /mnt/mainpool/appdata/twinraid
```

`zfs list` darf keinen Snapshot zeigen; `zfs destroy` ohne `-r` löscht nur das leere neue Dataset. Danach TwinRaid starten; es läuft wie vorher.

---

## 15. Probelauf einrichten (ab 0.5.1)

Mit 0.5.1 kommt **„Nur Probelauf“**: Der Wächter auf Klein legt einen Snapshot der Test-Share `twinraidtest` an, zählt die Änderungen, schätzt die Größe und fragt über den neuen Key „sync“ bei `guard.sh` auf Groß nach, ob ein Empfang möglich wäre. TwinRaid zeigt das Ergebnis je Share. **Nach Groß wird noch nichts geschrieben;** der echte Sync folgt mit 0.5.2. Der Probelauf hält auch keine Container an.

Neu auf den Servern sind `guard.sh` Version 3 und `source-guard.sh` Version 2. Die Abschnitte bauen aufeinander auf, die Reihenfolge ist wichtig:

| Abschnitt | Was | Wo |
|---|---|---|
| 15.1 | Voraussetzungen vor dem Update | Klein, Groß |
| 15.2 | Test G10: Update in der richtigen Reihenfolge | Klein, Groß |
| 15.3 | Test K1: Key „sync“, `mirror.conf`, Delegation, Hostkey | Klein, Groß |
| 15.4 | Test W9: `start` wartet auf die Überwachungsschleife | Klein |
| 15.5 | Test 1: Testdaten anlegen | Klein |
| 15.6 | Test 2: der erste Probelauf | Dashboard |
| 15.7 | Test 2b: was `guard.sh` ablehnt | Groß |
| 15.8 | Test 2c: fremdes Dataset an der Stelle des Spiegels | Groß, Dashboard |
| 15.9 | Test 2d: Delegation stimmt nicht | Groß |
| 15.10 | Test 2e: Neustart von Groß | Groß |
| 15.11 | Test G8 wiederholen: belegtes Gerät | Dashboard |
| 15.12 | Test M1: Speicherprüfung vor dem Start der VM | Groß, Dashboard |

Der Key „status“ und der Key „power“ bleiben, wie sie sind. In den Protokollen im PR nur „Klein“ und „Groß“ schreiben, nur die Testnamen, keine IP-Adressen, Fingerabdrücke, Namen eigener VMs und keine Größen.

### 15.1 Voraussetzungen vor dem Update

**Freigabe des Boot-Mediums ausschalten, auf Klein und auf Groß.** Dort liegen `guard.sh`, `authorized_keys`, `source-guard.sh`, die go-Datei und ab K1 der private Key „sync“. In der Unraid-Oberfläche jedes Servers: Reiter **Main** → Boot-Medium anklicken (**Flash**) → *SMB Security Settings* → **Export** `No` → **Apply**; ebenso bei *NFS Security Settings*, falls NFS eingeschaltet ist. Ohne das kann jeder im Netz den Key lesen oder `guard.sh` ändern. TwinRaid prüft das noch nicht selbst.

**`twinraid.yaml` anpassen, bevor du aktualisierst.** Seit 0.5.1 gibt es die Schlüssel `exclude` und `protect_on_target` unter `sync` nicht mehr: Ausschlüsse sind jetzt eigene Datasets, ältere Stände liegen in den Snapshots. **Stehen dort noch Einträge, startet der Container nicht.** Das Log nennt dann `exclude is no longer supported since v0.5.1` bzw. `protect_on_target is no longer supported since v0.5.1`. Eine leere Liste wäre nur eine Warnung im Log, ebenso `appdata_share` und `max_deletions`; die Löschgrenze steht jetzt in `source-guard.conf`.

Im **Unraid-Terminal von Klein** zuerst die Sicherung für den Rückweg in 15.2 anlegen; 0.5.0 verlangt die Schlüssel, die du gleich löschst.

> **Diesen Kasten allein einfügen.** Er legt die Sicherung nur an, wenn es sie noch nicht gibt. So überschreibt ein zweiter Aufruf, etwa nach dem Bearbeiten, die Sicherung nicht mit der schon geänderten Datei.

```bash
cd /mnt/user/appdata/twinraid/config
[ -e twinraid.yaml.vor-0.5.1 ] || cp -p twinraid.yaml twinraid.yaml.vor-0.5.1
ls -l twinraid.yaml*
```

`ls -l` zeigt `twinraid.yaml` und `twinraid.yaml.vor-0.5.1` mit gleicher Größe und gleicher Uhrzeit (`cp -p` übernimmt sie). Stammt die Sicherung von einem früheren Versuch und ist älter als deine letzte gewollte Änderung, sie mit `rm twinraid.yaml.vor-0.5.1` löschen und den Kasten noch einmal einfügen. Dann:

```bash
cd /mnt/user/appdata/twinraid/config
grep -n -E '^ *(exclude|protect_on_target|appdata_share|max_deletions):' twinraid.yaml
```

Zeigt `grep` Zeilen, die Datei mit `nano twinraid.yaml` öffnen und diese Schlüssel löschen, **samt der Listeneinträge darunter** (Zeilen, die mit `-` beginnen). Speichern mit `Strg+O`, `Enter`, beenden mit `Strg+X`. Danach zeigt derselbe `grep`-Befehl nichts mehr.

**Pflichtschlüssel unter `sync` prüfen.** Unter `sync` müssen `stop_containers`, `never_stop` und `copy_templates` stehen, in der Datei und in der Kopie für den Rückweg. Fehlt einer, startet der Container nicht. Das Log von `twinraid.preflight` nennt dann den Schlüssel, zum Beispiel `sync.stop_containers: required key is missing`.

```bash
grep -c -E '^ +(stop_containers|never_stop|copy_templates):' twinraid.yaml twinraid.yaml.vor-0.5.1
```

Beide Zeilen der Ausgabe müssen auf `:3` enden. Zeigt eine Zeile weniger, die betreffende Datei mit `nano` öffnen und den fehlenden Schlüssel unter `sync:` ergänzen, genauso eingerückt wie `shares:`. Die Werte aus `config.example.yaml` sind `stop_containers: true`, `never_stop: []` und `copy_templates: false`. Danach den `grep`-Befehl wiederholen.

**Das Update direkt danach einspielen (15.2).** TwinRaid liest die Datei nur beim Start: Es läuft weiter, aber 0.5.0 startet mit der geänderten Datei nicht mehr, etwa nach einem Neustart von Klein.

### 15.2 Test G10: Update in der richtigen Reihenfolge

Der Worker in TwinRaid läuft ab 0.5.1 ohne jede Capability, also ohne `DAC_READ_SEARCH`. Das klappt, solange seine Dateien root gehören oder für alle lesbar sind. Der Starttest prüft das; dieser Test prüft es auf Unraid, wo sich Rechte durch Werkzeuge wie *New Permissions* geändert haben können.

**Vor dem Update** im **Unraid-Terminal von Klein** die Rechte festhalten:

```bash
stat -c '%U:%G %a %n' \
  /mnt/user/appdata/twinraid/config \
  /mnt/user/appdata/twinraid/config/twinraid.yaml \
  /mnt/user/appdata/twinraid/data \
  /mnt/user/appdata/twinraid/data/secrets \
  /run/twinraid-source-guard/in/request \
  /run/twinraid-source-guard/state
```

| Pfad | Erwartet | Warum |
|---|---|---|
| `config` | für alle lesbar und betretbar: Rechte enden auf `5` oder `7`, richtig ist `755` | Worker und Web-Prozess lesen hier als „alle anderen“, wenn der Ordner nicht root gehört |
| `twinraid.yaml` | für alle lesbar: Rechte enden auf `4`, `5`, `6` oder `7` | ebenso |
| `data` | `root:root 755` | sonst startet TwinRaid nicht (5.9 im Konzept) |
| `data/secrets` | `root:<beliebige Gruppe> 700` | Key und Passwort-Hash; nur root liest sie |
| `…/in/request` | `root:root 600` | Auftragsdatei des Wächters |
| `…/state` | `root:root 755` | Status des Wächters |

Weicht etwas ab, erst beheben (bei `data` und `secrets`: Abschnitt 18, „Rechte des Datenordners zurückgesetzt“).

- **Gruppe von `data/secrets`:** Sie bleibt vom Anlegen im Container und erscheint auf dem Host oft als `UNKNOWN` (10001). Das ist unbedenklich; es zählen nur der Besitzer root und die Rechte `700`.
- **`config` mit `777`:** Das funktioniert, richtig ist aber `755`. Unraid setzt `appdata` oft auf `777`, zum Beispiel über *New Permissions*. Korrigieren mit `chmod 755 /mnt/user/appdata/twinraid/config`.

> **Skripte nie direkt nach `/boot/config/twinraid/` laden.** Erst nach `/tmp`, dann die SHA-256 vergleichen, dann kopieren und mit `mv` ersetzen. Die laufende Sitzung `vm-watch` (`guard.sh`) und der laufende Wächter lesen ihr Skript während der Ausführung nach; `mv` lässt ihnen die alte Datei. Sie bekommen die neue erst beim nächsten Start.

Danach in **dieser Reihenfolge**:

1. **TwinRaid aktualisieren:** Reiter **Docker** → bei *TwinRaid* **update ready** → **Update**. Die VM-Kachel zeigt jetzt „guard.sh auf dem Server ist zu alt“ und die Kachel Klein „Status des Wächters ungültig: Der Wächter spricht ein anderes Protokoll …“. Das ist für die nächsten Schritte normal.
2. **`guard.sh` Version 3 auf Groß holen** (Unraid-Terminal von **Groß**):

   ```bash
   wget -O /tmp/guard.sh \
     https://raw.githubusercontent.com/mantua1/twinraid/v0.5.1/host-scripts/guard.sh
   sha256sum /tmp/guard.sh
   ```

   Erwartet genau `c5f69807b76eb74af373361c47ecad3e39e718c278df10edba0e2bad40cae47f  /tmp/guard.sh`. Weicht der Wert ab, hier abbrechen und nichts kopieren. Stimmt er:

   ```bash
   cp /tmp/guard.sh /boot/config/twinraid/guard.sh.neu
   mv /boot/config/twinraid/guard.sh.neu /boot/config/twinraid/guard.sh
   head -2 /boot/config/twinraid/guard.sh
   ```

   Die zweite Zeile lautet `# TwinRaid: SSH-Wächter auf Unraid Groß, Version 3 (Konzept 3.5, 3.7 und 5.6).`
3. **Verbindung testen** (Container-Konsole von TwinRaid). Der Hostkey ist schon bestätigt; mit `yes` bestätigen:

   ```bash
   twinraid-admin trust-host-key target
   ```

   Erwartet: `OK: guard.sh answers (twinraid-guard 3 status ok).`, `OK: guard.sh answers (twinraid-guard 3 power ok).` und `TwinRaid connects with the key 'status' right away.` Damit endet die Wartezeit nach „zu alt“ sofort.
4. **`source-guard.sh` Version 2 auf Klein holen und neu starten** (Unraid-Terminal von **Klein**):

   ```bash
   wget -O /tmp/source-guard.sh \
     https://raw.githubusercontent.com/mantua1/twinraid/v0.5.1/host-scripts/source-guard.sh
   sha256sum /tmp/source-guard.sh
   ```

   Erwartet genau `d4f05ada883386b365520296312f916fc57d0a283725c0b3c8d5d3ce0f85d647  /tmp/source-guard.sh`. Weicht der Wert ab, hier abbrechen und nichts kopieren. Stimmt er:

   ```bash
   cp /tmp/source-guard.sh /boot/config/twinraid/source-guard.sh.neu
   mv /boot/config/twinraid/source-guard.sh.neu /boot/config/twinraid/source-guard.sh
   head -2 /boot/config/twinraid/source-guard.sh
   bash /boot/config/twinraid/source-guard.sh restart
   bash /boot/config/twinraid/source-guard.sh status
   ```

   Die zweite Zeile lautet `# TwinRaid: Wächter auf dem Host von Unraid Klein, Version 2 (Konzept 3.6).` `status` zeigt `Der Wächter läuft (PID …).` und eine Zeile JSON mit `"protocol":2`.

> **Warum diese Reihenfolge?** TwinRaid 0.5.0 versteht die neue Statuszeile `watch 3` von `guard.sh` 3 nicht. Andersherum zeigt TwinRaid 0.5.1 bei `guard.sh` 2 nur „zu alt“, und der nächste Schritt behebt das. Also erst TwinRaid, dann `guard.sh`, dann `trust-host-key`.

**Nach dem Update** im **Unraid-Terminal von Klein**:

```bash
for p in $(pgrep -f '/bin/twinraid-worker'); do echo "PID $p"; grep -E '^Cap(Eff|Bnd)' "/proc/$p/status"; done
stat -c '%U:%G %a %n' /run/twinraid-source-guard/in/request /run/twinraid-source-guard/state
```

In der **Container-Konsole**:

```bash
twinraid-admin show-action-log --lines 5
```

| Prüfen | Erwartet |
|---|---|
| Worker | genau ein Prozess, `CapEff: 0000000000000000` und `CapBnd: 0000000000000000` |
| Kachel **Unraid Klein** | „Wächter: aktiv · 1 Container freigegeben“, darunter „Status wird alle 5 s geprüft.“ und die ZFS-Zeile; das genaue Alter des Herzschlags steht nur noch unter **Einstellungen** |
| Status von Groß über den Key „status“ | VM-Kachel zeigt den Zustand der VM ohne Fehler; schaltest du die VM um, ändert sich die Kachel nach wenigen Sekunden |
| `show-action-log` | die letzten Einträge erscheinen ohne Fehlermeldung (eine leere Liste ist bei einer frischen Installation in Ordnung) |
| Kachel **Unraid Groß** | kein gelber Hinweis zum ZFS-Cache |
| **Einstellungen** | `zfs_arc_max` und `c_max` von Groß; **Wächter auf Unraid Klein**: Version 2, Ziel „nicht eingetragen“ |
| Container-Log von TwinRaid | keine Meldung `Permission denied` |

Rechte `stat` wie vorher.

**Scheitert G10, zurück auf 0.5.0.** *Rückweg beschrieben, bis 1.0 nicht auf den Servern geprüft.* Das betrifft besonders eine Meldung `Permission denied` im Log, einen Worker, der nicht startet, oder eine Kachel, die nach den Schritten oben noch einen Fehler zeigt.

1. **Klein:** Die alte Konfiguration zurückholen; 0.5.0 braucht die Schlüssel aus 15.1 und kennt `sync_dry_run` nicht:

   ```bash
   cp -p /mnt/user/appdata/twinraid/config/twinraid.yaml.vor-0.5.1 /mnt/user/appdata/twinraid/config/twinraid.yaml
   grep -c -E '^ +(stop_containers|never_stop|copy_templates):' /mnt/user/appdata/twinraid/config/twinraid.yaml
   ```

   Erwartet: `3`. Bei einer kleineren Zahl fehlt ein Pflichtschlüssel, und 0.5.0 startet nicht. Dann den Schlüssel wie in 15.1 ergänzen.

2. **Klein:** Reiter **Docker** → Symbol *TwinRaid* → **Edit** → **Repository** `ghcr.io/mantua1/twinraid:0.5.0` → **Apply**.
3. **Groß:** `guard.sh` Version 2 zurückholen:

   ```bash
   wget -O /tmp/guard.sh \
     https://raw.githubusercontent.com/mantua1/twinraid/v0.5.0/host-scripts/guard.sh
   sha256sum /tmp/guard.sh
   ```

   Erwartet genau `af4afe98fc11b20565194d7c22b293c165ee279efd79932878c31b5c80b42940  /tmp/guard.sh`. Weicht der Wert ab, hier abbrechen und nichts kopieren. Stimmt er:

   ```bash
   cp /tmp/guard.sh /boot/config/twinraid/guard.sh.neu
   mv /boot/config/twinraid/guard.sh.neu /boot/config/twinraid/guard.sh
   head -2 /boot/config/twinraid/guard.sh
   ```

   Die zweite Zeile lautet `# TwinRaid: SSH-Wächter auf Unraid Groß, Version 2 (Konzept 3.5 und 5.6).`
4. **Klein:** `source-guard.sh` Version 1 zurückholen und neu starten:

   ```bash
   wget -O /tmp/source-guard.sh \
     https://raw.githubusercontent.com/mantua1/twinraid/v0.5.0/host-scripts/source-guard.sh
   sha256sum /tmp/source-guard.sh
   ```

   Erwartet genau `ca2f12f98dcae89c524b2f361dc6de5e5c3adfdd240fcbc336dc89a6b7939d3a  /tmp/source-guard.sh`. Weicht der Wert ab, hier abbrechen und nichts kopieren. Stimmt er:

   ```bash
   cp /tmp/source-guard.sh /boot/config/twinraid/source-guard.sh.neu
   mv /boot/config/twinraid/source-guard.sh.neu /boot/config/twinraid/source-guard.sh
   head -2 /boot/config/twinraid/source-guard.sh
   bash /boot/config/twinraid/source-guard.sh restart
   ```

   Die zweite Zeile lautet `# TwinRaid: Wächter auf dem Host von Unraid Klein, Version 1 (Konzept 3.6).`
5. **Container-Konsole:** `twinraid-admin trust-host-key target`. Erwartet: `twinraid-guard 2 status ok`.
6. Ausgabe und Log im PR melden (ohne private Angaben). Das nächste Update nimmst du erst nach einer Korrektur; dann **Repository** wieder auf `ghcr.io/mantua1/twinraid:latest`.

### 15.3 Test K1: Key „sync“, `mirror.conf` und Delegation

**Auf Klein: Ziel eintragen.** `nano /boot/config/twinraid/source-guard.conf`, die Zeile `target_host` mit dem Beispielwert aktiv machen und die IP von Groß eintragen. `dataset twinraidtest` bleibt das einzige Dataset. Dann:

```bash
bash /boot/config/twinraid/source-guard.sh check-config
bash /boot/config/twinraid/source-guard.sh restart
bash /boot/config/twinraid/source-guard.sh init-key
```

`init-key` legt den Key an (`/boot/config/twinraid/ssh-sync`) und gibt eine Zeile aus, die so beginnt: `restrict,from="<IP von Klein>",command="/bin/bash /boot/config/twinraid/guard.sh sync" ssh-ed25519 …`. Der Key liegt nur auf dem Host von Klein, nie im Container von TwinRaid. Noch einmal aufgerufen, gibt `init-key` dieselbe Zeile aus; `--new` ersetzt den Key.

**Auf Groß: Zeile eintragen.** Unraid-Oberfläche von Groß: *Users → root → SSH authorized keys*, die Zeile vollständig in eine **neue Zeile** einfügen. Die Zeilen für „status“ und „power“ bleiben unverändert. **Save**.

**Auf Groß: `mirror.conf` anlegen.** Sie nennt die Spiegel, die `guard.sh` annimmt. Vorerst nur die Test-Share. Unraid-Terminal von **Groß**:

```bash
printf 'mirror twinraidtest\n' > /boot/config/twinraid/mirror.conf
cat /boot/config/twinraid/mirror.conf
```

**Auf Groß: Delegation einrichten.** `zfs receive` läuft ab 0.5.1 nicht als root, sondern als eigener Benutzer mit der festen Nummer 64000, der nur anhängen darf. Der Kernel verhindert dann unabhängig von `guard.sh` jedes `-F`, Rollback und Löschen. Diesen Benutzer gibt es nur als Zahl: **UID und GID 64000 dürfen weder in `/etc/passwd` noch in `/etc/group` stehen,** sonst lehnt `guard.sh` ab (`receiver-id-in-use`). Prüfen, beide Befehle geben nichts aus:

```bash
awk -F: '$3 == 64000 || $4 == 64000' /etc/passwd
awk -F: '$3 == 64000' /etc/group
```

Dann genau diese 28 Rechte vergeben, nur für UID 64000 und nur auf `mainpool/twinraid-mirror`. Ohne weitere Angabe gelten sie als „Local+Descendent“, also für dieses Dataset und alles darunter. Das ist die einzige erlaubte Form:

```bash
zfs allow -u 64000 acltype,atime,canmount,checksum,compression,copies,create,dedup,devices,exec,mount,mountpoint,primarycache,quota,readonly,receive:append,recordsize,refquota,refreservation,reservation,secondarycache,setuid,sharenfs,sharesmb,snapdir,sync,userprop,xattr mainpool/twinraid-mirror
```

**Prüfen:**

```bash
zfs allow mainpool/twinraid-mirror
```

Erwartet ist **genau** diese Ausgabe aus drei Zeilen; die dritte beginnt mit einem Tabulator, und die Rechte stehen alphabetisch:

```text
---- Permissions on mainpool/twinraid-mirror -------------------------
Local+Descendent permissions:
	user (unknown: 64000) acltype,atime,canmount,checksum,compression,copies,create,dedup,devices,exec,mount,mountpoint,primarycache,quota,readonly,receive:append,recordsize,refquota,refreservation,reservation,secondarycache,setuid,sharenfs,sharesmb,snapdir,sync,userprop,xattr
```

`(unknown: 64000)` ist richtig: Genau das zeigt, dass der Benutzer keinen Eintrag hat. Weitere Benutzer, Gruppen, `everyone`, Rechte auf `mainpool` selbst, die Überschriften „Local permissions“, „Descendent permissions“ oder „Create time permissions“ oder ein fehlendes oder zusätzliches Recht sind Fehler: Dann empfängt `guard.sh` nicht (`delegation-mismatch`), und einen Rückfall auf root gibt es nicht. Die Delegation steht im Pool, übersteht Neustarts, und `guard.sh` ändert sie nie.

> **`zfs-check.sh` nach der Delegation:** Der Machbarkeitstest (13.3) bricht auf Groß ab, solange die Delegation besteht, weil sein Test-Dataset sonst die Rechte erben würde. Soll er noch einmal laufen, vorher `zfs unallow -u 64000 mainpool/twinraid-mirror` und danach den `zfs allow`-Befehl von oben.

**Auf Klein: Hostkey von Groß bestätigen.**

```bash
bash /boot/config/twinraid/source-guard.sh trust-target
```

Es zeigt `Hostkey von Groß: ED25519 SHA256:…`. Im Unraid-Terminal von **Groß** vergleichen:

```bash
ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub
```

Nur wenn beide gleich sind, auf Klein `yes` eingeben.

**Prüfen, im Unraid-Terminal von Groß:**

```bash
SSH_ORIGINAL_COMMAND=check bash /boot/config/twinraid/guard.sh sync
```

| Prüfen | Erwartet |
|---|---|
| `trust-target` | `Hostkey gespeichert: …` und `OK: guard.sh antwortet (twinraid-guard 3 sync ok).` |
| `check`, erste Zeile | `twinraid-guard 3 sync ok` |
| `check`, Spiegel | `mirror twinraidtest state=absent props=- count=0 …` |
| `check`, letzte Zeile | `summary root=ok conf=ok deleg=ok receive=idle clones=0 available=… size=… reserve_percent=15 keep_days=30 min_interval_minutes=10` |
| **Einstellungen → Wächter auf Unraid Klein** | Ziel mit Port, Key „sync“: vorhanden, Hostkey: bestätigt |

Scheitert `trust-target` mit `Test mit check gescheitert: …`, nennt der Code den Grund: `auth_failed` (Zeile auf Groß fehlt oder ist abgeschnitten, oder `from=` passt nicht), `denied` (noch `guard.sh` Version 2), `unreachable` (SSH auf Groß aus, oder Groß aus).

### 15.4 Test W9: `start` wartet auf die Überwachungsschleife

Im **Unraid-Terminal von Klein**, beide Zeilen auf einmal einfügen:

```bash
bash /boot/config/twinraid/source-guard.sh stop
bash /boot/config/twinraid/source-guard.sh start; bash /boot/config/twinraid/source-guard.sh status
```

Erwartet: `Der Wächter wurde gestoppt.`, `Der Wächter wurde gestartet.` und direkt danach `Der Wächter läuft (PID …).`, ohne `sleep`.

### 15.5 Test 1: Testdaten anlegen

`testdata.sh` legt in der Test-Share Dateien mit allen Sonderfällen an: ungewöhnliche Namen, tiefe Ordner, viele kleine und einige große Dateien, Dateien mit Loch, Hardlinks, Symlinks, xattrs, ACLs, Rechte, Besitzer, Zeitstempel und eine FIFO. Dazu schreibt es eine Prüfliste mit SHA-256. Es belegt etwa 200 MiB. Im **Unraid-Terminal von Klein**:

```bash
wget -O /tmp/testdata.sh \
  https://raw.githubusercontent.com/mantua1/twinraid/v0.5.1/host-scripts/testdata.sh
sha256sum /tmp/testdata.sh
```

Erwartet genau `ca4b8833cfde011242f08cf67cd72d5fd7a08e14f3f3db3811076888f864a019  /tmp/testdata.sh`. Weicht der Wert ab, hier abbrechen und nichts kopieren. Stimmt er:

```bash
cp /tmp/testdata.sh /boot/config/twinraid/testdata.sh.neu
mv /boot/config/twinraid/testdata.sh.neu /boot/config/twinraid/testdata.sh
bash /boot/config/twinraid/testdata.sh create
bash /boot/config/twinraid/testdata.sh verify
```

| Prüfen | Erwartet |
|---|---|
| `create` | `Testdaten angelegt: /mnt/mainpool/twinraidtest/testdata`, die Zahl der Einträge, `Sonderfälle: xattr yes, acl yes, Besitzer yes` und `Prüfliste: PRUEFLISTE.txt, SHA-256 …` |
| `verify` | `Keine Abweichung (Lauf 0).` und dieselbe SHA-256 |

Die SHA-256 der Prüfliste notieren; ab 0.5.2 vergleichst du damit den Spiegel auf Groß mit `testdata.sh verify-latest`, das immer im neuesten Snapshot des Spiegels prüft (16.3). Im eingehängten Spiegel selbst kann der Kernel nach einem inkrementellen Empfang noch eine alte ACL zeigen, obwohl sie richtig angekommen ist; `verify` aus 0.5.2 lehnt ihn deshalb ab. Steht dort `acl no` oder `xattr no`, kann das Dataset das nicht, oder das Werkzeug fehlt; `verify` lässt es dann aus. Das bitte im PR festhalten. `create` legt nur neu an; für einen neuen Anfang den Ordner `/mnt/mainpool/twinraidtest/testdata` von Hand löschen.

Später erzeugt `bash /boot/config/twinraid/testdata.sh change` je Lauf eine bekannte Änderung: 50 Dateien gelöscht, 20 geändert, 30 neu, 10 umbenannt, dazu ein Block in einer großen Datei und einzelne Eigenschaften. Das braucht der Test mit `zfs diff` in 0.5.2; `zfs diff` zählt die betroffenen Ordner zusätzlich als geändert.

### 15.6 Test 2: Der erste Probelauf

**Freischalten.** `nano /mnt/user/appdata/twinraid/config/twinraid.yaml` auf Klein. Unter `actions` die Zeile `sync_dry_run: true` ergänzen. Unter `sync` bei `shares` vorerst **nur** `twinraidtest` eintragen:

```yaml
actions:
  # … deine bisherigen Schalter bleiben …
  sync_dry_run: true

sync:
  shares:
    - twinraidtest
```

`sync.shares`, die Zeilen `dataset` in `source-guard.conf` und die Zeilen `mirror` in `mirror.conf` sind die **drei Ebenen**; sie müssen dieselben Namen enthalten. Container von TwinRaid neu starten: Reiter **Docker** → *TwinRaid* → **Restart**.

**Vorher** in der Container-Konsole:

```bash
twinraid-admin sync-check
```

Erwartet: `Source guard: active, version 2`, die Zeile `twinraidtest` mit `yes` in den Spalten für `twinraid.yaml` und `source-guard.conf` (die Spalte für `mirror.conf` zeigt `?`, bis ein Probelauf gefragt hat), `Target: no check yet; start a dry run.` und `ZFS cache on the target: zfs_arc_max …`.

**Probelauf:** Groß muss an sein; ist es aus, weckt TwinRaid es nur, wenn `actions.wake` freigeschaltet ist, und lässt es danach an. In der Kachel **Sync** auf **Nur Probelauf** klicken.

| Prüfen | Erwartet |
|---|---|
| Kachel **Sync** während des Laufs | „Snapshot auf … wird angelegt …“, dann „Änderungen werden gezählt und … geprüft …“, Button **Abbrechen** |
| Kachel **Sync** danach | „Letzter Probelauf (…): Probelauf abgeschlossen; Senden wäre möglich.“ und die Tabelle unten |
| Zeile `twinraidtest` | Modus „vollständig (erster Lauf)“; gelöscht, geändert, neu, umbenannt leer, weil es noch keine Basis zum Vergleichen gibt; Größe etwa die 200 MiB aus 15.5; Spiegel „noch keiner“ |
| unter der Tabelle | „Senden wäre möglich.“ und freier Platz auf Groß mit Reserve 15 % |
| Button **Jetzt synchronisieren** | grau, „kommt mit v0.5.2“; mit TwinRaid 0.5.2 „in twinraid.yaml unter actions nicht freigeschaltet“, bis Abschnitt 16 den Sync freischaltet |
| Klein, Snapshot | `zfs list -t snapshot mainpool/twinraidtest` zeigt `mainpool/twinraidtest@twinraid-…`. Er bleibt 7 Tage und läuft dann selbst ab |
| Groß, kein Spiegel | `zfs list -r -o name mainpool/twinraid-mirror` zeigt nur `mainpool/twinraid-mirror` |
| Groß, Syslog | keine Zeile `receive into mirror` |
| `twinraid-admin show-action-log` | `sync_dry_run` mit dem Ergebnis `dry_run_success` |
| `twinraid-admin sync-check` | `mirror.conf`-Spalte `yes`, `Target: last check …: ok`, `delegation ok`, `twinraidtest: absent` |

Zwischen zwei Probeläufen liegen mindestens **30 Minuten**, weil der Wächter höchstens alle 30 Minuten einen Snapshot anlegt. Früher meldet die Kachel „Zu kurz nach dem letzten Snapshot …“. Das gilt auch für die Tests unten.

### 15.7 Test 2b: Was `guard.sh` ablehnt

Im **Unraid-Terminal von Groß**. Die Befehle verändern nichts; jeder endet mit einer Ablehnung.

```bash
G=/boot/config/twinraid/guard.sh
SSH_ORIGINAL_COMMAND="receive system" bash $G sync; echo "Exit: $?"
SSH_ORIGINAL_COMMAND="receive gibtesnicht" bash $G sync; echo "Exit: $?"
SSH_ORIGINAL_COMMAND="receive ../fremd" bash $G sync; echo "Exit: $?"
SSH_ORIGINAL_COMMAND="zfs destroy mainpool/twinraid-mirror" bash $G sync; echo "Exit: $?"
SSH_ORIGINAL_COMMAND=poweroff bash $G sync; echo "Exit: $?"
SSH_ORIGINAL_COMMAND=vm-start bash $G sync; echo "Exit: $?"
```

| Anfrage | Erwartet |
|---|---|
| `receive system`, `receive gibtesnicht` | `twinraid-guard: refused name-denied`, Exit `3` |
| `receive ../fremd`, `zfs destroy …`, `poweroff` und `vm-start` im Modus sync | `twinraid-guard: denied`, Exit `2` |
| *Tools → System Log* | Einträge `twinraid-guard: refused: name-denied` und `twinraid-guard: denied: …` ohne den Text der Anfrage |

**`poweroff` während eines Empfangs.** Dafür hältst du die Sperre des Empfangs selbst, höchstens 10 Minuten, und gibst sie danach selbst frei. Alle Befehle im selben Terminal ausführen, sonst fehlen `$G` und `$LOCKPID`.

```bash
flock -o /var/run/twinraid-guard-receive.lock sleep 600 &
LOCKPID=$!
sleep 1
SSH_ORIGINAL_COMMAND=check bash $G sync | grep -o 'receive=[a-z]*'
```

**Ohne `receive=busy` nicht weitermachen:** Ohne Sperre würde Groß wirklich ausgehen, wenn keine VM läuft. Dann `kill $LOCKPID` und nachsehen, warum die Sperre fehlt. Bei `receive=busy`:

```bash
SSH_ORIGINAL_COMMAND=poweroff bash $G power; echo "Exit: $?"
```

Erwartet: `twinraid-guard: refused sync-running`, Exit `3`, Groß bleibt an. Dann die Sperre freigeben:

```bash
kill $LOCKPID
sleep 1
SSH_ORIGINAL_COMMAND=check bash $G sync | grep -o 'receive=[a-z]*'
```

Erwartet: `receive=idle`. Das `-o` bei `flock` ist nötig: Ohne es erbt `sleep` die Sperre und hielte sie nach `kill` die ganzen 10 Minuten weiter. Mit `-o` läuft nur `sleep` noch bis zum Ablauf weiter, ohne Sperre und ohne Folgen.

### 15.8 Test 2c: Fremdes Dataset an der Stelle des Spiegels

`guard.sh` ersetzt und löscht nie ein Dataset, das es nicht selbst empfangen hat (Konzept, E26). Im **Unraid-Terminal von Groß**:

```bash
zfs create mainpool/twinraid-mirror/twinraidtest
SSH_ORIGINAL_COMMAND=check bash /boot/config/twinraid/guard.sh sync | grep '^mirror'
```

Erwartet: `mirror twinraidtest state=foreign …`. Nach Ablauf der 30 Minuten aus Test 2 in der Kachel **Sync** auf **Nur Probelauf** klicken.

| Prüfen | Erwartet |
|---|---|
| Kachel **Sync** | „Der Zielserver würde den Empfang ablehnen.“ mit dem Grund, Spiegel „fremdes Dataset“, und „Auf … liegt an der Stelle eines Spiegels ein fremdes Dataset. Ansehen und von Hand entfernen (docs/EINRICHTUNG.md).“ |
| `twinraid-admin show-action-log` | `sync_dry_run` mit `target_refused` und Grund `target_exists` |
| Groß | `zfs list mainpool/twinraid-mirror/twinraidtest` zeigt das Dataset unverändert |

Dann das Dataset von Hand entfernen, ohne `-r`; ohne `-r` löscht ZFS nur ein Dataset ohne Snapshots und Kinder:

```bash
zfs destroy mainpool/twinraid-mirror/twinraidtest
```

Nach weiteren 30 Minuten noch ein Probelauf: wieder Spiegel „noch keiner“ und „Senden wäre möglich.“

### 15.9 Test 2d: Delegation stimmt nicht

Im **Unraid-Terminal von Groß**. `check` zeigt jeweils, was ein Empfang täte; es verändert nichts.

```bash
G=/boot/config/twinraid/guard.sh
zfs unallow -u 64000 mainpool/twinraid-mirror
SSH_ORIGINAL_COMMAND=check bash $G sync | grep -o 'deleg=[a-z-]*'
```

Erwartet: `deleg=mismatch`. Dann die Delegation mit dem `zfs allow`-Befehl aus 15.3 wiederherstellen und zusätzlich ein Recht vergeben, das nicht dazugehört:

```bash
zfs allow -u 64000 destroy mainpool/twinraid-mirror
SSH_ORIGINAL_COMMAND=check bash $G sync | grep -o 'deleg=[a-z-]*'
zfs unallow -u 64000 destroy mainpool/twinraid-mirror
SSH_ORIGINAL_COMMAND=check bash $G sync | grep -o 'deleg=[a-z-]*'
zfs allow mainpool/twinraid-mirror
```

Erwartet: erst `deleg=mismatch`, dann `deleg=ok`, und `zfs allow` zeigt wieder genau die drei Zeilen aus 15.3.

### 15.10 Test 2e: Neustart von Groß

Die Delegation steht im Pool und muss einen Neustart überstehen. Groß neu starten (alle VMs aus; das Array startet automatisch), dann im **Unraid-Terminal von Groß**:

```bash
SSH_ORIGINAL_COMMAND=check bash /boot/config/twinraid/guard.sh sync
zfs allow mainpool/twinraid-mirror
```

| Prüfen | Erwartet |
|---|---|
| `check` | `deleg=ok`, `root=ok`, `conf=ok` |
| `zfs allow` | genau die drei Zeilen aus 15.3 |
| Dashboard | Groß und die VM-Kachel melden sich ohne Eingriff zurück |

Unraid setzt beim Start die Werte der obersten Ebene eines Pools zurück; die festen Werte der Spiegel stehen aber direkt am Spiegel. In 0.5.1 gibt es noch keinen Spiegel. `props=ok` je Spiegel prüft dieser Test deshalb mit 0.5.2 nach dem ersten echten Sync noch einmal.

### 15.11 Test G8 wiederholen: Belegtes Gerät

Wie in 11.21, nur mit der neuen Meldung. Seit `guard.sh` 3 erkennt TwinRaid den Grund. Reicht der Speicher schon nicht für die Gaming-VM, kommt stattdessen die Meldung aus 15.12.

### 15.12 Test M1: Speicherprüfung vor dem Start der VM

Vor `vm-start` prüft `guard.sh`, ob genug Arbeitsspeicher frei ist. Der Test zeigt, ob das auf Groß so greift wie gedacht, wenn neben der Gaming-VM schon eine zweite VM läuft. **Seit 0.5.2 beträgt der Sicherheitsabstand 2 GiB** (Ergebnis dieses Tests). `guard.sh` aus 0.5.1, das dieser Abschnitt lädt, rechnet noch mit 1 GiB; wer den Test mit dieser Fassung macht, setzt in der Formel unten 1 GiB statt 2 GiB ein. Ab 16.2 gilt 2 GiB.

> **Nur starten, wenn in beiden VMs nichts verloren gehen kann.** Reicht der Speicher trotz der Prüfung nicht, beendet der Kernel eine VM wie bei einem Stromausfall. In beiden VMs vorher alles speichern und schließen.

1. Die **zweite VM** in der Unraid-Oberfläche von Groß starten und warten, bis sie läuft. Die Gaming-VM ist aus.
2. Im **Unraid-Terminal von Groß** die Werte **vorher** notieren. Den Namen der VM genau wie im Reiter **VMs** eintragen:

   ```bash
   grep MemAvailable /proc/meminfo
   awk '$1 == "size" || $1 == "c_min" {print $1, $3}' /proc/spl/kstat/zfs/arcstats
   virsh dominfo "Windows 11 Gaming" | grep 'Max memory'
   ```

3. **Von Hand rechnen.** `MemAvailable` und `Max memory` sind in KiB, `size` und `c_min` in Byte:

   ```text
   frei  = MemAvailable in GiB + (size − c_min) in GiB, der zweite Teil mindestens 0
   nötig = Max memory in GiB + 2 GiB
   ```

   TwinRaid startet die VM nur, wenn `frei` mindestens `nötig` ist. KiB teilst du zweimal durch 1024, Byte dreimal. Ein Beispiel mit erfundenen Zahlen: `MemAvailable` 7 340 032 kB sind 7 GiB, `size` 6 442 450 944 und `c_min` 1 073 741 824 ergeben 5 GiB, also `frei` = 12 GiB. `Max memory` 10 485 760 KiB sind 10 GiB, also `nötig` = 12 GiB: Die VM startet gerade noch. Mit 11 534 336 KiB (11 GiB) wäre `nötig` = 13 GiB, und sie startet nicht.
4. In TwinRaid in der VM-Kachel **Starten** klicken.

| Ergebnis | Erwartet |
|---|---|
| `frei` kleiner als `nötig` | VM bleibt aus; „Zuletzt: VM starten – Auf dem Server ist nicht genug Arbeitsspeicher frei, um die VM zu starten.“; `show-action-log` zeigt `vm_memory_low`; Syslog von Groß `twinraid-guard: refused: memory-low`; die zweite VM läuft ungestört weiter |
| `frei` mindestens `nötig` | VM startet. Beide VMs laufen ein paar Minuten unter normaler Last; `grep -i 'out of memory' /var/log/syslog` zeigt nichts. Danach beide herunterfahren |

**Im PR festhalten:** welcher der beiden Fälle eintrat, ob er zur Rechnung passt und wie groß der Überschuss oder Fehlbetrag in Prozent von `nötig` war, ohne die Größen selbst.

### 15.13 Abschluss

Die Ergebnisse von G10, K1, W9, 1, 2, 2b bis 2e, G8 und M1 bitte im Pull Request festhalten. `actions.sync_dry_run` kann eingeschaltet bleiben; der Probelauf schreibt nie nach Groß. Die Simulation ohne echte Server zeigt dasselbe: **Nur Probelauf** in der Kachel **Sync** und `twinraid-admin mock-fault` mit den Namen aus Abschnitt 20.

---

## 16. Echter Sync der Test-Share (ab 0.5.2)

Mit 0.5.2 kommt **„Jetzt synchronisieren“**. Der Wächter auf Klein hält die freigegebenen Container kurz an, legt einen Snapshot der Test-Share `twinraidtest` an, zählt die Änderungen und sendet sie mit `zfs send` an `guard.sh` auf Groß. Groß übernimmt sie in den schreibgeschützten Spiegel `mainpool/twinraid-mirror/twinraidtest`; jeder Lauf ist dort ein eigener Snapshot. Der erste Lauf sendet alles, jeder weitere nur die Änderungen seit dem letzten. Bis alle Tests dieses Abschnitts bestanden sind, bleibt `twinraidtest` die einzige Share (Konzept 10.2).

Neu auf den Servern sind `source-guard.sh` Version 3 (Klein), `guard.sh` mit 2 GiB Sicherheitsabstand beim Start der VM (Groß, weiter Version 3) und `testdata.sh` mit `verify-latest` und `verify-container` (Klein **und** Groß). TwinRaid 0.5.2 arbeitet auch mit den Skripten aus 0.5.1; die Reihenfolge in 16.2 hält nur den Rückweg kurz.

| Abschnitt | Was | Wo |
|---|---|---|
| 16.1 | Vorbereitung | Klein, Groß |
| 16.2 | Test G11 und W10: Update in dieser Reihenfolge, Rückweg auf 0.5.1 | Klein, Groß |
| 16.3 | Test 3: der erste Sync | Dashboard, Groß, Klein |
| 16.4 | Test 3b: Neustart von Groß | Groß |
| 16.5 | Test 4: nur die Änderungen senden | Klein, Dashboard, Groß |
| 16.6 | Test A1 und A2: Aufbewahrung auf Klein | Klein, Groß |
| 16.7 | Test 5a: Löschgrenze | Klein, Dashboard |
| 16.8 | Test 5c: Die drei Ebenen passen nicht | Klein, Dashboard |
| 16.9 | Test 5e: Spiegel auf Groß verändert | Groß, Dashboard |
| 16.10 | Test 6a bis 6d: Container | Klein, Dashboard |
| 16.11 | Test 7, 5d und 8a: Menge, gesperrtes Herunterfahren, Abbrechen | Klein, Dashboard, Groß |
| 16.12 | Test 8b: Neustart von TwinRaid während des Sendens | Dashboard |
| 16.13 | Test 8c (optional): Neustart von Klein während des Sendens | Klein, Groß |
| 16.14 | Aufräumen | Klein, Dashboard |
| 16.15 | Test 9 entfällt (Rückweg bis 1.0 nicht geprüft) | – |
| 16.16 | Test M1 (optional) mit 2 GiB | Groß, Dashboard |
| 16.17 | Test 5b (optional): Test-Share neu angelegt | Klein, Groß, Dashboard |
| 16.18 | Test A3: Aufbewahrung auf Groß nach einem Monat | Groß |
| 16.19 | Abschluss | |

> **Zeitbedarf:** Der Wächter legt höchstens alle 30 Minuten einen Snapshot an, und jeder Probelauf und jeder Sync braucht einen. `reset-run-interval` hebt nur die Pause nach einem Lauf mit angehaltenen Containern auf, nicht diesen Abstand. Früher meldet die Kachel „Zu kurz nach dem letzten Snapshot …“. Die Tests brauchen etwa 15 Läufe, also mindestens acht Stunden; verteile sie auf zwei bis drei Tage. A2 läuft über zwei Tage, A3 über einen Monat.

In den Protokollen im PR nur „Klein“ und „Groß“ schreiben, nur die Testnamen `twinraidtest` und `twinraid-testcontainer`, keine IP-Adressen, Fingerabdrücke, Namen eigener VMs, Container oder Shares und keine Größen. Durchsatz und Genauigkeit der Restzeit bitte nur in Prozent angeben (16.12).

### 16.1 Vorbereitung

**Sicherung der Konfiguration,** im **Unraid-Terminal von Klein**. Du brauchst sie nur, wenn 0.5.1 auf dem Rückweg mit der geänderten Datei nicht startet (16.2).

> **Diesen Kasten allein einfügen.** Er legt die Sicherung nur an, wenn es sie noch nicht gibt. So überschreibt ein zweiter Aufruf, etwa nach den Änderungen in 16.2, die Sicherung nicht mit der schon geänderten Datei.

```bash
cd /mnt/user/appdata/twinraid/config
[ -e twinraid.yaml.vor-0.5.2 ] || cp -p twinraid.yaml twinraid.yaml.vor-0.5.2
ls -l twinraid.yaml*
```

`ls -l` zeigt `twinraid.yaml.vor-0.5.2` mit derselben Größe und Uhrzeit wie `twinraid.yaml` (`cp -p` übernimmt sie); `twinraid.yaml.vor-0.5.1` aus 15.1 darf daneben liegen bleiben. Stammt die Sicherung von einem früheren Versuch und ist älter als deine letzte gewollte Änderung, sie mit `rm twinraid.yaml.vor-0.5.2` löschen und den Kasten noch einmal einfügen.

**Kein Lauf und kein Job offen**, im **Unraid-Terminal von Klein**:

```bash
bash /boot/config/twinraid/source-guard.sh status | grep -o -E '"run":(null|\{)|"job":(null|\{"id":[0-9]+,"state":"[a-z_]+")'
```

Erwartet: `"run":null` und entweder `"job":null` oder ein Job mit `"state":"closed"`, `"done"` oder `"failed"`. Zeigt die Ausgabe `"run":{` oder einen anderen Zustand des Jobs, läuft noch ein Probelauf; warten und den Befehl wiederholen. Ein Neustart des Wächters in 16.2 bräche ihn sonst ab.

**Groß ist an, und der Spiegel gibt es noch nicht.** Im **Unraid-Terminal von Groß**:

```bash
SSH_ORIGINAL_COMMAND=check bash /boot/config/twinraid/guard.sh sync
```

Erwartet: `mirror twinraidtest state=absent props=- count=0 …` und in der Zeile `summary` `root=ok conf=ok deleg=ok receive=idle clones=0`. Steht dort `state=foreign`, liegt noch das Dataset aus Test 2c (15.8); es wie dort beschrieben entfernen. Groß bleibt für die Tests an; ist es aus, weckt TwinRaid es beim Sync nur, wenn `actions.wake` freigeschaltet ist, und lässt es danach an.

### 16.2 Test G11 und W10: Update in dieser Reihenfolge

Wie in 15.2 gilt: **Skripte nie direkt nach `/boot/config/twinraid/` laden,** sondern nach `/tmp`, dann die SHA-256 vergleichen, dann kopieren und mit `mv` ersetzen.

1. **TwinRaid aktualisieren:** Reiter **Docker** → bei *TwinRaid* **update ready** → **Update**. Danach zeigen alle Kacheln ihren gewohnten Zustand ohne Fehler; die Kachel **Unraid Klein** „Wächter: aktiv · 1 Container freigegeben“. Der Button **Jetzt synchronisieren** ist grau mit „in twinraid.yaml unter actions nicht freigeschaltet“.
2. **`source-guard.sh` Version 3 auf Klein** (Unraid-Terminal von **Klein**):

   ```bash
   wget -O /tmp/source-guard.sh \
     https://raw.githubusercontent.com/mantua1/twinraid/v0.5.2/host-scripts/source-guard.sh
   sha256sum /tmp/source-guard.sh
   ```

   Erwartet genau `2cbccdfba70f8372799512f1b75e568abd71140c03f4d582af90b99294db0bd7  /tmp/source-guard.sh`. Weicht der Wert ab, hier abbrechen und nichts kopieren. Stimmt er:

   ```bash
   cp /tmp/source-guard.sh /boot/config/twinraid/source-guard.sh.neu
   mv /boot/config/twinraid/source-guard.sh.neu /boot/config/twinraid/source-guard.sh
   head -2 /boot/config/twinraid/source-guard.sh
   ```

   Die zweite Zeile lautet `# TwinRaid: Wächter auf dem Host von Unraid Klein, Version 3 (Konzept 3.6).` Dann den Wächter neu starten. `restart` bricht eine laufende Übertragung ab; deshalb vorher die Prüfung aus 16.1.

   ```bash
   bash /boot/config/twinraid/source-guard.sh restart
   bash /boot/config/twinraid/source-guard.sh status | grep -o -E '"protocol":[0-9]+|"version":[0-9]+'
   ```

   Erwartet: `Der Wächter wurde gestartet.`, dann `"protocol":2` und `"version":3`.
3. **`guard.sh` auf Groß** (Unraid-Terminal von **Groß**). Es bleibt Version 3; neu ist nur der Abstand von 2 GiB beim Start der VM.

   ```bash
   wget -O /tmp/guard.sh \
     https://raw.githubusercontent.com/mantua1/twinraid/v0.5.2/host-scripts/guard.sh
   sha256sum /tmp/guard.sh
   ```

   Erwartet genau `30713f387695c48611bcd3762bfca59e1928c5436db0ea8cf7eb1b68143b2e0c  /tmp/guard.sh`. Weicht der Wert ab, hier abbrechen und nichts kopieren. Stimmt er:

   ```bash
   cp /tmp/guard.sh /boot/config/twinraid/guard.sh.neu
   mv /boot/config/twinraid/guard.sh.neu /boot/config/twinraid/guard.sh
   head -2 /boot/config/twinraid/guard.sh
   grep -n '^readonly MEMORY_MARGIN=' /boot/config/twinraid/guard.sh
   ```

   Die zweite Zeile lautet `# TwinRaid: SSH-Wächter auf Unraid Groß, Version 3 (Konzept 3.5, 3.7 und 5.6).`, und `grep` zeigt `52:readonly MEMORY_MARGIN=2147483648`. Die laufende Statusverbindung (`vm-watch`) behält bis zum nächsten Start die alte Datei; `vm-start` und jeder Empfang lesen bei jedem Aufruf die neue. `twinraid-admin trust-host-key` ist nicht nötig.
4. **`testdata.sh` auf Klein und auf Groß.** Denselben Kasten erst im Unraid-Terminal von **Klein**, dann im Unraid-Terminal von **Groß** ausführen:

   ```bash
   wget -O /tmp/testdata.sh \
     https://raw.githubusercontent.com/mantua1/twinraid/v0.5.2/host-scripts/testdata.sh
   sha256sum /tmp/testdata.sh
   ```

   Erwartet genau `c151d3421085cf2857640377d98ed0b7520c340adc950b6bac6ba725449d39de  /tmp/testdata.sh`. Weicht der Wert ab, hier abbrechen und nichts kopieren. Stimmt er:

   ```bash
   cp /tmp/testdata.sh /boot/config/twinraid/testdata.sh.neu
   mv /boot/config/twinraid/testdata.sh.neu /boot/config/twinraid/testdata.sh
   ```

   Zur Kontrolle auf **Klein** `bash /boot/config/twinraid/testdata.sh verify` und auf **Groß** `bash /boot/config/twinraid/testdata.sh verify-latest`. Klein meldet `Keine Abweichung (Lauf 0).` (oder einen höheren Lauf, falls du `change` schon ausgeführt hast), Groß `mainpool/twinraid-mirror/twinraidtest hat keinen Snapshot.`, weil es den Spiegel noch nicht gibt.
5. **Sync freischalten:** `nano /mnt/user/appdata/twinraid/config/twinraid.yaml` auf Klein. Unter `actions` die Zeile `sync: true` ergänzen (`sync_dry_run: true` kann bleiben). Unter `sync` für die Tests 3 bis 5 `stop_containers: false` setzen; 16.10 stellt es auf `true` zurück. Speichern mit `Strg+O`, `Enter`, beenden mit `Strg+X`. Dann Reiter **Docker** → *TwinRaid* → **Restart**.

   ```yaml
   actions:
     # … deine bisherigen Schalter bleiben …
     sync_dry_run: true
     sync: true

   sync:
     shares:
       - twinraidtest
     stop_containers: false
     # … never_stop, copy_templates und schedule bleiben, wie sie sind …
   ```

6. **Test W10: Die Pause ist sofort aufgehoben.** Im **Unraid-Terminal von Klein** zuerst einen Lauf mit dem Testcontainer, damit es eine Pause gibt:

   ```bash
   bash /boot/config/twinraid/source-guard.sh reset-run-interval
   bash /boot/config/twinraid/source-guard.sh selftest twinraid-testcontainer
   grep -o '"run_allowed_at":[^,]*' /run/twinraid-source-guard/state/status.json
   ```

   Erwartet: `OK: angehalten und wieder gestartet.` und `"run_allowed_at":` mit einer Zahl. Dann beide Befehle in **einer** Zeile, damit `grep` direkt nach dem Befehl liest:

   ```bash
   bash /boot/config/twinraid/source-guard.sh reset-run-interval; grep -o '"run_allowed_at":[^,]*' /run/twinraid-source-guard/state/status.json
   ```

| Prüfen | Erwartet |
|---|---|
| Kacheln nach Punkt 1 | ohne Fehler; **Jetzt synchronisieren** grau, „nicht freigeschaltet“ |
| **Einstellungen → Wächter auf Unraid Klein** nach Punkt 2 | Version 3 |
| VM-Kachel nach Punkt 3 | unverändert, ohne Fehler |
| nach Punkt 5 | **Jetzt synchronisieren** und **Nur Probelauf** sind aktiv; die Kachel **Sync** zeigt „Zeitplan aus (ab Phase 6).“ |
| W10 | `Die Pause zwischen zwei Läufen ist aufgehoben.` und sofort `"run_allowed_at":null`; mit Version 2 stand dort kurz noch die Zahl |
| Container-Log von TwinRaid | keine Meldung `Permission denied` |

> **Warum diese Reihenfolge?** Anders als bei 0.5.1 zwingt kein Protokoll eine Reihenfolge: TwinRaid 0.5.2 versteht `source-guard.sh` 2 und 3 und `guard.sh` 3 in beiden Fassungen. Erst TwinRaid, dann die Skripte, und erst ganz am Ende `actions.sync`: So lässt sich jeder Schritt einzeln zurücknehmen, und der Sync ist erst freigeschaltet, wenn alle Teile da sind.

**Scheitert G11, zurück auf 0.5.1.** *Rückweg beschrieben, bis 1.0 nicht auf den Servern geprüft.*

1. **Kein Lauf und keine Übertragung offen:** Prüfung aus 16.1. Ein Neustart von TwinRaid lässt eine Übertragung zwar weiterlaufen, TwinRaid 0.5.1 kann sie aber nicht anzeigen.
2. **Klein:** Reiter **Docker** → Symbol *TwinRaid* → **Edit** → **Repository** `ghcr.io/mantua1/twinraid:0.5.1` → **Apply**. `twinraid.yaml` bleibt; 0.5.1 kennt alle Schlüssel, auch `actions.sync`, und zeigt bei **Jetzt synchronisieren** „kommt mit v0.5.2“.
3. **Nur wenn 0.5.1 mit der Datei nicht startet** (das Container-Log nennt den Schlüssel), die Sicherung aus 16.1 zurückholen.

   > **Diesen Kasten allein einfügen.** Er ersetzt `twinraid.yaml` durch den Stand vor 16.2; alle späteren Änderungen an der Datei sind danach weg.

   ```bash
   cp -p /mnt/user/appdata/twinraid/config/twinraid.yaml.vor-0.5.2 /mnt/user/appdata/twinraid/config/twinraid.yaml
   ```

   Dann Reiter **Docker** → *TwinRaid* → **Restart**.
4. **Die Skripte bleiben**, denn sie passen auch zu 0.5.1. Nur wenn ein Skript selbst der Grund für den Rückweg ist, die Fassung aus 0.5.1 holen: `guard.sh` wie in 15.2, Punkt 2, `source-guard.sh` wie in 15.2, Punkt 4 (mit `restart`), `testdata.sh` wie in 15.5 ohne `create`. Dort stehen die SHA-256 der Fassung 0.5.1.
5. Ausgabe und Log im PR melden (ohne private Angaben). Zurück auf 0.5.2 geht es mit **Repository** `ghcr.io/mantua1/twinraid:latest` → **Apply**.

### 16.3 Test 3: Der erste Sync

Voraussetzungen: 16.2 ist durch, Groß ist an, `stop_containers: false`, der Testcontainer läuft (er schreibt in `twinraidtest/testcontainer`). In der Kachel **Sync** auf **Jetzt synchronisieren** klicken.

| Prüfen | Erwartet |
|---|---|
| Kachel **Sync** während des Laufs | „Sync läuft: Snapshot auf … wird angelegt …“, dann „Änderungen werden gezählt und … geprüft …“, dann „Sendet nach … …“ mit Fortschrittsbalken, „… % · … von … · Share twinraidtest“ und darunter Rate, Restzeit und „fertig gegen hh:mm“ (in den ersten 15 Sekunden „Restzeit wird berechnet …“) |
| Kachel **Unraid Groß** während des Laufs | **Herunterfahren** grau, „gesperrt, solange ein Sync läuft“ |
| Kachel **Sync** danach | „Letzter Sync (hh:mm): erfolgreich“, „Ergebnis des Syncs vom …“ mit `twinraidtest` „vollständig (erster Lauf)“, Gesendet etwa die Größe, „angekommen“; darunter „Letzter Lauf“ mit „Sync · Dauer … · … gesendet“ |

Danach im **Unraid-Terminal von Groß**:

```bash
bash /boot/config/twinraid/testdata.sh verify-latest
bash /boot/config/twinraid/testdata.sh verify-container
zfs list -t snapshot -o name,creation -d 1 mainpool/twinraid-mirror/twinraidtest
zfs get -o property,value,source readonly,exec,setuid,devices,canmount,mountpoint,primarycache,compression,atime,xattr,acltype,twinraid:mirror mainpool/twinraid-mirror/twinraidtest
findmnt -n -o TARGET,SOURCE,FSTYPE /mnt/mainpool/twinraid-mirror/twinraidtest
zfs get -H -d 1 -t snapshot -o name,value,source twinraid:received mainpool/twinraid-mirror/twinraidtest
SSH_ORIGINAL_COMMAND=check bash /boot/config/twinraid/guard.sh sync
grep twinraid-guard /var/log/syslog | tail -n 5
```

Im **Unraid-Terminal von Klein**:

```bash
zfs list -t snapshot -o name,userrefs -d 1 mainpool/twinraidtest
zfs list -t bookmark -o name -d 1 mainpool/twinraidtest
grep twinraid-source-guard /var/log/syslog | tail -n 8
```

In der **Container-Konsole**:

```bash
twinraid-admin show-sync-log --lines 1
twinraid-admin show-action-log --lines 3
```

| Prüfen | Erwartet |
|---|---|
| `verify-latest` | `Snapshot: twinraid-…`, dann `Keine Abweichung (Lauf 0).` mit **derselben SHA-256 der Prüfliste** wie in 15.5. Damit sind auch xattrs, ACLs, Hardlinks, Symlinks, Besitzer, Rechte und Zeitstempel angekommen |
| `verify-container` | `Testcontainer lief beim Snapshot (zaehler …).`: Mit `stop_containers: false` wird er nicht angehalten. Selten steht dort, das Log sei leer gewesen; der Container leert es alle 10 Minuten |
| Snapshots auf Groß | genau einer, mit demselben Namen wie der neueste Snapshot auf Klein (`twinraid-<Datum>T<Zeit>Z`) |
| `zfs get` | jede Zeile mit Quelle `local`: `readonly on`, `exec off`, `setuid off`, `devices off`, `canmount on`, `mountpoint /mnt/mainpool/twinraid-mirror/twinraidtest`, `primarycache metadata`, `compression lz4`, `atime off`, `xattr sa`, `acltype posix`, `twinraid:mirror 1` |
| `findmnt` | `/mnt/mainpool/twinraid-mirror/twinraidtest mainpool/twinraid-mirror/twinraidtest zfs` |
| `twinraid:received` | eine Zahl (Sekunden seit 1970) mit Quelle `local` |
| `check` | `mirror twinraidtest state=mirror props=ok count=1 …`; `summary` mit `deleg=ok receive=idle clones=0` |
| Syslog von Groß | `receive into mirror twinraidtest requested`, `… started` und `… finished` |
| Snapshots auf Klein | der gesendete mit `userrefs` `1` (Hold `twinraid-base`), alle anderen mit `0` |
| Lesezeichen auf Klein | genau eines, `mainpool/twinraidtest#` mit dem Namen des gesendeten Snapshots |
| Syslog von Klein | `snapshot twinraid-… created for 1 datasets (job …)` und `sending twinraidtest (full)`; gibt es noch Snapshots aus den Probeläufen, die älter als 7 Tage sind, zusätzlich `retention: … own snapshots destroyed, 0 failed` |
| `show-sync-log` | eine Zeile `sync: success`, darunter `twinraidtest  full` mit Größe und gesendeter Menge und `ok` |
| `show-action-log` | `sync_start  dashboard: success` |

### 16.4 Test 3b: Neustart von Groß

Wie Test 2e (15.10), jetzt mit einem echten Spiegel. **Vor dem Neustart** im **Unraid-Terminal von Groß** festhalten, welche Datasets und Share-Konfigurationen es gibt. Die Ausgabe enthält deine eigenen Namen; kopiere sie nur in eine Textdatei auf deinem PC, nicht in den PR.

```bash
zfs list -H -o name
ls /boot/config/shares/
```

Dann Groß neu starten (alle VMs aus; das Array startet automatisch) und danach:

```bash
SSH_ORIGINAL_COMMAND=check bash /boot/config/twinraid/guard.sh sync
zfs get -o property,value,source readonly,exec,setuid,devices,canmount,mountpoint,primarycache,compression,atime,xattr,acltype,twinraid:mirror mainpool/twinraid-mirror/twinraidtest
findmnt -n -o TARGET,SOURCE,FSTYPE /mnt/mainpool/twinraid-mirror/twinraidtest
zfs allow mainpool/twinraid-mirror
zfs list -H -o name
ls /boot/config/shares/
```

| Prüfen | Erwartet |
|---|---|
| `check` | `mirror twinraidtest state=mirror props=ok …`, `summary` mit `root=ok conf=ok deleg=ok` |
| `zfs get` | wie in 16.3, alle Werte mit Quelle `local` |
| `findmnt` | Unraid hat den Spiegel beim Start am richtigen Ort eingehängt, wie in 16.3 |
| `zfs allow` | genau die drei Zeilen aus 15.3 |
| `zfs list`, `ls` | dieselben Einträge wie vor dem Neustart: Unraid hat keine Datasets und keine Share-Konfigurationen angelegt |
| Dashboard | Groß und die VM-Kachel melden sich ohne Eingriff zurück |

Danach, mindestens 30 Minuten nach dem Snapshot aus 16.3, noch einmal **Jetzt synchronisieren**. Erwartet: „erfolgreich“, Modus „inkrementell“, eine kleine Größe. Gezählt werden nur wenige geänderte Einträge: die Dateien des Testcontainers in `twinraidtest/testcontainer`, denn sonst hat sich nichts geändert.

### 16.5 Test 4: Nur die Änderungen senden

1. **Testdaten ändern** im **Unraid-Terminal von Klein**:

   > **Diesen Kasten allein und nur einmal einfügen.** Jeder Aufruf ändert die Testdaten erneut; ein zweiter verdoppelt die Zählungen, und die Erwartungen unten stimmen nicht mehr.

   ```bash
   bash /boot/config/twinraid/testdata.sh change
   ```

   Erwartet: `Testdaten geändert: /mnt/mainpool/twinraidtest/testdata (Lauf N)` und `Dateien: 50 gelöscht, 20 geändert, 30 neu, 10 umbenannt`. Die Zahl `N` notieren; beim ersten `change` ist sie `1`.
2. **Nur Probelauf** (mindestens 30 Minuten nach dem letzten Snapshot). Erwartet: „Probelauf abgeschlossen; Senden wäre möglich.“, `twinraidtest` „inkrementell“, Spiegel „vorhanden · 2 Snapshots“ und die Zählungen aus der Tabelle unten.
3. **ACL im eingehängten Spiegel vorher lesen** (nur wenn `create` in 15.5 `acl yes` meldete), im **Unraid-Terminal von Groß**:

   ```bash
   getfacl -p /mnt/mainpool/twinraid-mirror/twinraidtest/testdata/acl/datei.txt
   ```

   Die Ausgabe notieren. So liest der Kernel die Datei vor dem Empfang, wie im Befund aus der CI (Konzept 3.7).
4. **Jetzt synchronisieren**, wieder mindestens 30 Minuten nach dem Probelauf.
5. **Prüfen** im **Unraid-Terminal von Groß**. Die ersten zwei Zeilen merken sich den neuesten und den vorigen Snapshot:

   ```bash
   NEU=$(zfs list -H -t snapshot -o name -s createtxg -d 1 mainpool/twinraid-mirror/twinraidtest | tail -n 1 | cut -d@ -f2)
   VOR=$(zfs list -H -t snapshot -o name -s createtxg -d 1 mainpool/twinraid-mirror/twinraidtest | tail -n 2 | head -n 1 | cut -d@ -f2)
   echo "neuester: $NEU, voriger: $VOR"
   bash /boot/config/twinraid/testdata.sh verify-latest
   bash /boot/config/twinraid/testdata.sh verify "/mnt/mainpool/twinraid-mirror/twinraidtest/.zfs/snapshot/$VOR/testdata"
   getfacl -p /mnt/mainpool/twinraid-mirror/twinraidtest/testdata/acl/datei.txt
   getfacl -p "/mnt/mainpool/twinraid-mirror/twinraidtest/.zfs/snapshot/$NEU/testdata/acl/datei.txt"
   ```

| Prüfen | Erwartet |
|---|---|
| Zählungen im Probelauf und im Sync | **gelöscht 52**: die 50 Dateien, dazu die neu geschriebene Prüfliste und der neu gesetzte Symlink `links/relativ`; `zfs diff` zählt ersetzte Einträge als gelöscht und neu (so in der CI gemessen). **neu 33**: 30 Dateien, der Ordner `neu`, Prüfliste und Symlink (32, wenn `neu` schon von einem früheren `change` stammt). **umbenannt 10**. **geändert mindestens 23**: die 20 Dateien, die große Datei `gross/zufall-1.bin`, `rechte/datei-0664` und `zeiten/2000-nanosekunden.txt`, dazu `xattr/datei.txt` und `acl/datei.txt`, wenn `create` `xattr yes` bzw. `acl yes` meldete, die betroffenen Ordner und die Dateien des Testcontainers |
| Größe | klein: das geänderte MiB der großen Datei und die kleinen Dateien, nicht die 200 MiB des ersten Laufs |
| Kachel **Sync** | „erfolgreich“, `twinraidtest` „inkrementell“ |
| `verify-latest` | `Keine Abweichung (Lauf N)` mit dem `N` aus Punkt 1 |
| `verify` im vorigen Snapshot | `Keine Abweichung (Lauf N-1)`; der vorige Stand bleibt unverändert erhalten |
| `getfacl` im neuen Snapshot | enthält `user:1000:r--`, die Änderung aus `change` |
| `getfacl` im eingehängten Spiegel | entweder dasselbe wie im Snapshot oder noch der Stand aus Punkt 3. **Bitte im PR festhalten, welcher Fall eintrat**; er kommt ins Konzept (3.7). Die Daten sind in beiden Fällen richtig angekommen, wie der Snapshot zeigt |

### 16.6 Test A1 und A2: Aufbewahrung auf Klein

**A1, gleich nach Test 4,** im **Unraid-Terminal von Klein**:

```bash
zfs list -t snapshot -o name,userrefs,creation -d 1 mainpool/twinraidtest
zfs list -t bookmark -o name,creation -d 1 mainpool/twinraidtest
```

und im **Unraid-Terminal von Groß**:

```bash
zfs get -H -d 1 -t snapshot -o name,value,source twinraid:received mainpool/twinraid-mirror/twinraidtest
```

| Prüfen | Erwartet |
|---|---|
| Snapshots auf Klein | genau einer mit `userrefs` `1`: der zuletzt gesendete, derselbe Name wie der neueste auf Groß. Keiner ist älter als 7 Tage |
| Lesezeichen auf Klein | je erfolgreichem Sync eines (bisher drei), keines gelöscht |
| Groß | so viele Snapshots wie erfolgreiche Syncs, jeder mit `twinraid:received` und Quelle `local`; keiner gelöscht |

**A2 (optional), über zwei Tage.** In `/boot/config/twinraid/source-guard.conf` `snapshot_keep_days 1` eintragen (statt `7`), dann im **Unraid-Terminal von Klein** (nur ohne offenen Job, Prüfung aus 16.1):

```bash
bash /boot/config/twinraid/source-guard.sh check-config
bash /boot/config/twinraid/source-guard.sh restart
```

Am nächsten Tag, wenn die Snapshots vom Vortag älter als 24 Stunden sind, ein Sync. Danach die Befehle aus A1 und `grep 'retention:' /var/log/syslog | tail -n 3` auf Klein. Zum Schluss den Wert wieder auf `7` setzen, `check-config` und `restart`.

| Prüfen | Erwartet |
|---|---|
| Syslog von Klein | `retention: … own snapshots destroyed, 0 failed` |
| Snapshots auf Klein | die Basis mit `userrefs` `1` und die Snapshots der letzten 24 Stunden; ältere sind weg |
| Lesezeichen auf Klein | alle noch da |
| Groß | nur der neue Snapshot dazu, sonst unverändert |

### 16.7 Test 5a: Löschgrenze

1. In `/boot/config/twinraid/source-guard.conf` `max_deletions 5` eintragen (statt `1000`), dann im **Unraid-Terminal von Klein** `bash /boot/config/twinraid/source-guard.sh check-config` und `… restart` (ohne offenen Job).
2. **Testdaten ändern:**

   > **Diesen Kasten allein und nur einmal einfügen.** Jeder Aufruf ändert die Testdaten erneut.

   ```bash
   bash /boot/config/twinraid/testdata.sh change
   ```

   Die Zahl `N` aus `(Lauf N)` notieren.
3. **Jetzt synchronisieren.** Die Kachel zeigt „Wartet auf die Freigabe der Löschungen …“ und den Abschnitt „Löschungen freigeben?“. Auf **Ablehnen** klicken.
4. Nach 30 Minuten erneut **Jetzt synchronisieren**, diesmal **Freigeben und senden** und im Dialog „Löschungen freigeben und senden?“ wieder **Freigeben und senden**.
5. Im **Unraid-Terminal von Groß** wie in Test 4 prüfen (die Zeilen mit `NEU`, `VOR`, `verify-latest` und `verify` im vorigen Snapshot).
6. `max_deletions 1000` wieder eintragen, `check-config`, `restart`.

| Prüfen | Erwartet |
|---|---|
| Abschnitt „Löschungen freigeben?“ | „52 Löschungen, mehr als max_deletions (5) in source-guard.conf.“, der Hinweis, dass freigegebene Löschungen in den älteren Snapshots auf Groß erhalten bleiben, und „Ohne Antwort schließt der Wächter den Job um hh:mm …“, 30 Minuten nach dem Ende der Prüfung |
| nach **Ablehnen** | „Letzter Sync (…): Die Löschungen wurden abgelehnt; nichts wurde gesendet.“; auf Groß kein neuer Snapshot (`zfs list -t snapshot -d 1 mainpool/twinraid-mirror/twinraidtest`) |
| nach **Freigeben und senden** | „erfolgreich“; `verify-latest` meldet `Lauf N`, `verify` im vorigen Snapshot `Lauf N-1`: Die gelöschten Dateien liegen dort noch vor |
| `show-action-log` | `sync_start … deletions_declined` und danach `sync_start … success` |

### 16.8 Test 5c: Die drei Ebenen passen nicht

In `twinraid.yaml` unter `sync` → `shares` vorübergehend eine zweite Zeile `- gibtesnicht` ergänzen und TwinRaid neu starten (Reiter **Docker** → *TwinRaid* → **Restart**).

| Prüfen | Erwartet |
|---|---|
| **Jetzt synchronisieren** und **Nur Probelauf** | grau, „sync.shares in twinraid.yaml passt nicht zu den Zeilen dataset in source-guard.conf auf …“ |
| `twinraid-admin sync-check` | die Zeile `gibtesnicht` mit `yes` nur in der Spalte für `twinraid.yaml` |

Danach die Zeile wieder löschen und TwinRaid neu starten; beide Buttons sind wieder aktiv.

### 16.9 Test 5e: Spiegel auf Groß verändert

Drei Fälle, jeweils mit 30 Minuten Abstand zum vorigen Snapshot. Alle Befehle im **Unraid-Terminal von Groß**.

**(i) Eine Datei im Spiegel.** Der Kasten öffnet den Spiegel kurz zum Schreiben, legt eine Datei an und schließt ihn wieder:

> **Diesen Kasten allein einfügen.** Er verändert den Spiegel absichtlich; das macht Punkt „Zurücksetzen“ unten rückgängig.

```bash
zfs set readonly=off mainpool/twinraid-mirror/twinraidtest
echo fremd > /mnt/mainpool/twinraid-mirror/twinraidtest/fremd.txt
zfs set readonly=on mainpool/twinraid-mirror/twinraidtest
```

Dann **Jetzt synchronisieren**. Erwartet: „Der Spiegel auf dem Zielserver wurde seit dem letzten Snapshot verändert; der Empfang wurde abgelehnt.“, beim Share „Ziel verändert“. Auf Groß bleiben Zahl der Snapshots und `props=ok` gleich, `fremd.txt` liegt noch da, und das Syslog von Groß enthält eine Zeile mit `has been modified`.

**Zurücksetzen** auf den neuesten Snapshot:

> **Diesen Kasten allein einfügen.** `zfs rollback` verwirft alles, was im Spiegel nach dem neuesten Snapshot geschrieben wurde, hier nur `fremd.txt`. Es nimmt immer den neuesten Snapshot und braucht deshalb kein `-r`.

```bash
S=$(zfs list -H -t snapshot -o name -s createtxg -d 1 mainpool/twinraid-mirror/twinraidtest | tail -n 1)
echo "$S"
zfs rollback "$S"
ls /mnt/mainpool/twinraid-mirror/twinraidtest/fremd.txt
```

Erwartet: `ls` meldet `No such file or directory`. Meldet `zfs rollback` einen Fehler, die Meldung im PR festhalten und nicht weitermachen. Nach 30 Minuten ist ein Sync wieder „erfolgreich“.

**(ii) Schreibschutz bleibt aus.**

> **Diesen Kasten allein einfügen.** Er lässt den Spiegel beschreibbar; der Kasten danach schaltet den Schutz wieder ein.

```bash
zfs set readonly=off mainpool/twinraid-mirror/twinraidtest
```

Dann **Jetzt synchronisieren**. Erwartet: Schon die Prüfung meldet den Spiegel als abweichend: „Der Zielserver lehnt den Empfang ab.“ mit dem Grund „Ein Spiegel auf … ist gesperrt (props-mismatch) …“. Gesendet wird nichts; `canmount` bleibt `on`, der Spiegel bleibt eingehängt. Der Text spricht von „gesperrt“; hier weicht nur `readonly` ab, und das behebt der nächste Kasten:

```bash
zfs set readonly=on mainpool/twinraid-mirror/twinraidtest
SSH_ORIGINAL_COMMAND=check bash /boot/config/twinraid/guard.sh sync | grep '^mirror'
```

Erwartet: `props=ok`.

**(iii) Schreibschutz geht während der Freigabe aus.** `max_deletions 5` wie in 16.7, Punkt 1, dann auf Klein:

> **Diesen Kasten allein und nur einmal einfügen.** Jeder Aufruf ändert die Testdaten erneut.

```bash
bash /boot/config/twinraid/testdata.sh change
```

**Jetzt synchronisieren.** Während der Abschnitt „Löschungen freigeben?“ offen ist, auf Groß:

> **Diesen Kasten allein einfügen.** Er lässt den Spiegel beschreibbar; `guard.sh` sperrt ihn daraufhin beim Empfang.

```bash
zfs set readonly=off mainpool/twinraid-mirror/twinraidtest
```

Dann **Freigeben und senden**. Danach auf Groß:

```bash
zfs get -o property,value canmount,mounted mainpool/twinraid-mirror/twinraidtest
grep twinraid-guard /var/log/syslog | tail -n 5
bash /boot/config/twinraid/testdata.sh verify-latest
```

| Prüfen | Erwartet |
|---|---|
| Kachel **Sync** | „Der Zielserver lehnt den Empfang ab.“ mit „feste Eigenschaften fehlen oder wurden geändert, der Spiegel ist gesperrt (props-not-applied) …“, beim Share „abgelehnt“ |
| `zfs get` | `canmount noauto`, `mounted no` |
| Syslog von Groß | `mirror twinraidtest locked` und `refused: props-not-applied` |
| `verify-latest` | `… ist nicht eingehängt (gesperrt?); siehe docs/WIEDERHERSTELLUNG.md.` |

Dann den Spiegel nach [WIEDERHERSTELLUNG.md](WIEDERHERSTELLUNG.md), Abschnitt „Spiegel gesperrt“, Weg „Nach `props-not-applied`“ zurücksetzen (Vorbereitung mit `N=twinraidtest`, dann die Schritte 1 bis 4). Erwartet: `check` meldet `deleg=ok` und `props=ok`. Nach 30 Minuten **Jetzt synchronisieren**, freigeben: „erfolgreich“, und `verify-latest` meldet `Keine Abweichung` mit dem Lauf aus dem letzten `change`; `guard.sh` hat den Spiegel selbst wieder eingehängt. Zum Schluss `max_deletions 1000`, `check-config`, `restart`.

### 16.10 Test 6a bis 6d: Container

**6a: Container werden für den Snapshot angehalten.** In `twinraid.yaml` `stop_containers: true` setzen und TwinRaid neu starten. Der Testcontainer muss laufen: `cat /mnt/user/twinraidtest/testcontainer/counter` zweimal, die Zahl wächst. Dann **Jetzt synchronisieren**. Danach auf Groß `bash /boot/config/twinraid/testdata.sh verify-container`, auf Klein `docker inspect -f '{{.State.Running}} {{.State.StartedAt}}' twinraid-testcontainer` und in der Container-Konsole `twinraid-admin show-action-log --lines 6`.

| Prüfen | Erwartet |
|---|---|
| Kachel **Sync** während des Laufs | „Sync läuft: Container auf … werden angehalten …“ und „1 Container auf … angehalten.“, dann „Container auf … werden wieder gestartet …“ |
| `verify-container` | `Testcontainer war beim Snapshot sauber angehalten.` |
| `docker inspect` | `true` und ein Startzeitpunkt von eben: Der Container war nur kurz aus |
| `show-action-log` | eine Zeile `source_guard_stop … (part of sync_start)` mit `1 containers`, eine Zeile `source_guard_finish … (part of sync_start)` und `sync_start  dashboard: success` |
| Historie | „Sync · … · 1 Container angehalten“ |
| Syslog von Klein | `begin: opened`, `stop twinraid-testcontainer: stopped`, `ping: extended`, `snapshot: created`, `finish: finished` |
| Kachel **Sync** danach | „Nächster Sync frühestens um hh:mm.“; **Jetzt synchronisieren** grau mit „der Wächter auf … hält die Pause zwischen zwei Läufen ein; frühestens um hh:mm“ |

**6b: Wächter aus.** Im **Unraid-Terminal von Klein**, damit nur dieser Grund sperrt:

```bash
bash /boot/config/twinraid/source-guard.sh reset-run-interval
bash /boot/config/twinraid/source-guard.sh stop
```

Nach etwa 35 Sekunden zeigt die Kachel **Unraid Klein** rot „Wächter: nicht aktiv seit hh:mm.“, und **Jetzt synchronisieren** ist grau mit „der Wächter auf … ist nicht aktiv oder ZFS ist dort nicht verfügbar“. Dann `bash /boot/config/twinraid/source-guard.sh start`; nach wenigen Sekunden ist alles wieder aktiv.

**6c: TwinRaid startet neu, während Container angehalten sind.** Das Anhalten des Testcontainers dauert nur einen Augenblick; deshalb löst das Terminal den Neustart aus, sobald der Lauf offen ist. Mindestens 30 Minuten nach dem Snapshot aus 6a im **Unraid-Terminal von Klein**:

> **Diesen Kasten allein einfügen.** Die zweite Zeile wartet höchstens 5 Minuten, bis ein Lauf offen ist, und startet dann TwinRaid neu. Öffnet sich in dieser Zeit kein Lauf, endet sie von selbst mit `kein Lauf geöffnet, nichts neu gestartet`; dann den Kasten noch einmal einfügen und den Sync früher starten. Mit `Strg+C` lässt sie sich vorher beenden.

```bash
bash /boot/config/twinraid/source-guard.sh reset-run-interval
timeout 300 sh -c 'until grep -q "\"run\":{" /run/twinraid-source-guard/state/status.json; do sleep 0.2; done' && docker restart TwinRaid || echo "kein Lauf geöffnet, nichts neu gestartet"
```

Gleich danach im Dashboard **Jetzt synchronisieren** klicken. Das Dashboard verliert kurz die Verbindung. Nach etwa einer Minute:

```bash
docker inspect -f '{{.State.Running}}' twinraid-testcontainer
bash /boot/config/twinraid/source-guard.sh status | grep -o -E '"run":(null|\{)|"job":(null|\{"id":[0-9]+,"state":"[a-z_]+")'
grep twinraid-source-guard /var/log/syslog | tail -n 8
```

| Prüfen | Erwartet |
|---|---|
| Testcontainer | `true`: TwinRaid hat beim Beenden `finish` geschrieben, oder spätestens nach 120 Sekunden hat der Totmannschalter ihn gestartet |
| Wächter | `"run":null`; ein Job, falls der Snapshot schon lag, ist `closed` |
| Syslog von Klein | `finish: finished` (oder `deadman: no heartbeat from TwinRaid`), bei einem Job `close: closed` |
| `show-action-log` und Historie | der Sync mit `interrupted` |

**6d: Andere Container bleiben unberührt.** Wähle einen deiner Container, der läuft und **nicht** in `allow` steht. Vor und nach einem Sync (30 Minuten nach dem Snapshot aus 6c, vorher `reset-run-interval`) im **Unraid-Terminal von Klein** den Startzeitpunkt ansehen; `<Name>` durch den Namen aus dem Reiter **Docker** ersetzen und den Namen nicht in den PR schreiben:

```bash
docker inspect -f '{{.State.StartedAt}}' <Name>
```

Erwartet: Vorher und nachher derselbe Zeitpunkt; im Syslog von Klein nur `stop twinraid-testcontainer`.

### 16.11 Test 7, 5d und 8a: Menge, gesperrtes Herunterfahren, Abbrechen

Für diesen und die nächsten Tests muss das Senden einige Minuten dauern, etwa 5, sonst ist es vorbei, bevor du abbrechen kannst. Die nötige Menge ist ungefähr **Rate des Netzes × Minuten**. Beispiel: Ein Netz mit 2,5 GbE überträgt höchstens etwa 298 MiB/s, also rund 17 GiB je Minute; für 5 Minuten wären das rund 87 GiB. Langsamer als das Netz ist es oft, dann dauert das Senden nur länger. Dateien, die sich kaum packen lassen (etwa ISO-Dateien), passen gut: Der Wächter sendet die Blöcke so, wie sie im Pool liegen, gut packbare Dateien wären also schneller durch. Reicht der Platz nicht, geht es mit weniger: Dann früher abbrechen und die Restzeit nur grob vergleichen.

> **Nur unkritische Daten kopieren,** zum Beispiel ISO-Dateien: keine persönlichen Dokumente, keine Schlüssel, Passwörter oder Konfigurationen. Die Kopie bleibt nach dem Löschen (16.14) noch **7 Tage auf Klein** und **30 Tage auf Groß** in den Snapshots, und erst danach wird der Platz frei.

**Platz prüfen.** Auf Klein `zfs list -o name,used,avail mainpool`, auf Groß `zfs list -o name,used,avail mainpool/twinraid-mirror`. Die Kopie muss auf beiden Seiten zweimal hineinpassen (für 16.13), und es muss die Reserve von 15 % frei bleiben; sonst lehnen der Wächter (`snapshot_low_space`) bzw. `guard.sh` (`space_low`) ab.

**Dateien auswählen** im **Unraid-Terminal von Klein**: einzelne unkritische Dateien, nicht eine ganze Share. Statt `/mnt/user/isos/<Datei1> /mnt/user/isos/<Datei2>` die Pfade deiner Dateien eintragen, so viele wie nötig; Pfade mit Leerzeichen in Anführungszeichen setzen, und keine zwei Dateien mit demselben Namen wählen (sonst überschreibt die zweite die erste in der Kopie). Zuerst nur die Summe ansehen:

```bash
du -ch /mnt/user/isos/<Datei1> /mnt/user/isos/<Datei2>
```

Die letzte Zeile (`total`) ist die Summe. Liegt sie deutlich unter der Zielgröße, Dateien hinzunehmen, liegt sie über dem freien Platz, weglassen, und `du -ch` wiederholen. Die Liste der Pfade für das Kopieren und für 16.13 aufheben.

**Kopieren** mit genau derselben Liste:

> **Diesen Kasten allein und nur einmal einfügen.** Das Kopieren kann lange dauern. Gibt es `kopie` schon, bricht der Kasten ab, statt ein zweites Mal zu kopieren.

```bash
mkdir /mnt/mainpool/twinraidtest/kopie && cp -p /mnt/user/isos/<Datei1> /mnt/user/isos/<Datei2> /mnt/mainpool/twinraidtest/kopie/
```

Danach zeigt `du -sh /mnt/mainpool/twinraidtest/kopie` ungefähr die Summe von eben.

Dann **Jetzt synchronisieren** (30 Minuten nach dem letzten Snapshot) und während des Sendens:

1. Bei etwa **10 %** Restzeit und „fertig gegen“ notieren.
2. **Test 5d:** In der Kachel **Unraid Groß** ist **Herunterfahren** grau, „gesperrt, solange ein Sync läuft“. Im **Unraid-Terminal von Groß** schickt die folgende Zeile `poweroff` nur, wenn `check` unmittelbar vorher `receive=busy` meldet:

   > **Diesen Kasten allein einfügen, und nur, während gesendet wird (bei etwa 10 %).** Ohne laufenden Empfang schaltet `poweroff` Groß wirklich aus, wenn keine VM läuft, und der Sync bricht ab. Die Zeile prüft das unmittelbar vorher; endet der Empfang aber genau zwischen `check` und `poweroff`, wirkt es trotzdem. Deshalb nicht kurz vor dem Ende des Sendens verwenden.

   ```bash
   SSH_ORIGINAL_COMMAND=check bash /boot/config/twinraid/guard.sh sync | grep -q 'receive=busy' && { SSH_ORIGINAL_COMMAND=poweroff bash /boot/config/twinraid/guard.sh power; echo "Exit: $?"; } || echo "kein Empfang, nichts geschickt"
   ```

   Erwartet: `twinraid-guard: refused sync-running`, `Exit: 3`, Groß bleibt an. Steht dort `kein Empfang, nichts geschickt`, lief gerade kein Empfang; dann wurde nichts geschickt, und der Test wird beim nächsten Sync wiederholt.
3. **Test 8a:** Bei etwa **50 %** auf **Abbrechen** klicken und im Dialog „Sync abbrechen?“ **Sync abbrechen** wählen.

Danach im **Unraid-Terminal von Groß**:

```bash
zfs list -H -t snapshot -d 1 mainpool/twinraid-mirror/twinraidtest | wc -l
SSH_ORIGINAL_COMMAND=check bash /boot/config/twinraid/guard.sh sync
zfs list -H -t all -r -o name mainpool/twinraid-mirror | grep '%' || echo "kein Rest eines Empfangs"
```

und im **Unraid-Terminal von Klein**:

```bash
zfs list -t snapshot -o name,userrefs -d 1 mainpool/twinraidtest
pgrep -f 'zfs send' || echo "kein zfs send"
```

| Prüfen | Erwartet |
|---|---|
| Kachel **Sync** nach dem Klick | kurz „Wird abgebrochen …“, dann „Letzter Sync (…): abgebrochen“ |
| Snapshots auf Groß | dieselbe Zahl wie vor dem Sync |
| `check` | `props=ok`, `receive=idle` |
| Rest eines Empfangs | `kein Rest eines Empfangs` |
| Klein | `userrefs` `1` weiter am vorigen gesendeten Snapshot (derselbe Name wie der neueste auf Groß), der neue Snapshot mit `0`; `kein zfs send` |
| Syslog von Klein | `close: closed` |
| `show-sync-log` | `sync: cancelled` |

### 16.12 Test 8b: Neustart von TwinRaid während des Sendens

Mindestens 30 Minuten nach dem abgebrochenen Lauf noch einmal **Jetzt synchronisieren**. Er sendet dieselbe Kopie, weil der Abbruch nichts nach Groß übernommen hat.

1. Bei etwa **10 %** Restzeit und „fertig gegen“ notieren (`E1`).
2. Bei etwa **30 %**: Reiter **Docker** → *TwinRaid* → **Restart**. Nach dem Neuladen der Seite wieder anmelden.
3. Bei etwa **50 %** Restzeit und „fertig gegen“ notieren (`E2`).
4. Das tatsächliche Ende notieren.

| Prüfen | Erwartet |
|---|---|
| Kachel **Sync** nach dem Neustart | „Übertragung läuft weiter (vor dem Neustart begonnen).“ mit Fortschritt; für 15 Sekunden „Restzeit wird berechnet …“; **Abbrechen** ist möglich |
| Kachel **Sync** am Ende | „erfolgreich“; die Historie zeigt den übernommenen Lauf mit „nach einem Neustart übernommen“ und davor den unterbrochenen mit „abgebrochen, weil TwinRaid neu gestartet wurde“ |
| `show-action-log` | `sync_start … interrupted` und `sync_start … success, adopted` |
| Groß | ein Snapshot mehr; `verify-latest` meldet `Keine Abweichung` mit dem Lauf aus dem letzten `change` |

**Im PR festhalten, nur in Prozent:** den Durchsatz als Anteil an der Rate des Netzes (die Rate aus der Kachel geteilt durch die Rate des Netzes, bei 2,5 GbE zum Beispiel etwa 298 MiB/s) und die Abweichung von `E1` und `E2` vom tatsächlichen Ende, jeweils bezogen auf die Dauer des Sendens.

### 16.13 Test 8c (optional): Neustart von Klein während des Sendens

Klein ist der Server, der immer läuft; nur machen, wenn ein Neustart gerade passt. Es braucht neue Daten, sonst gibt es nichts zu senden.

> **Diesen Kasten allein und nur einmal einfügen.** Er kopiert dieselben unkritischen Dateien wie in 16.11 ein zweites Mal; auch diese Kopie bleibt 7 bzw. 30 Tage in den Snapshots. Gibt es `kopie2` schon, bricht er ab.

Statt `/mnt/user/isos/<Datei1> /mnt/user/isos/<Datei2>` dieselbe Liste wie in 16.11 eintragen:

```bash
mkdir /mnt/mainpool/twinraidtest/kopie2 && cp -p /mnt/user/isos/<Datei1> /mnt/user/isos/<Datei2> /mnt/mainpool/twinraidtest/kopie2/
```

Dann **Jetzt synchronisieren** und bei etwa **30 %** Klein neu starten (Unraid-Oberfläche von Klein → *Reboot*). Wenn Klein wieder läuft, im **Unraid-Terminal von Groß** die drei Befehle aus 16.11, und auf Klein `bash /boot/config/twinraid/source-guard.sh status | grep -o '"last_seq":[0-9]*'`.

| Prüfen | Erwartet |
|---|---|
| Groß | Zahl der Snapshots unverändert, `props=ok`, `receive=idle`, `kein Rest eines Empfangs` |
| Klein | `"last_seq":0` oder wenig mehr: neue Instanz des Wächters; Kachel **Unraid Klein** „aktiv“; der Testcontainer läuft (Autostart) |
| nächster Sync | sendet das ganze Delta mit `kopie2` und endet „erfolgreich“. Der Abstand von 30 Minuten zählt nach einem Neustart neu, der Sync geht also sofort |

### 16.14 Aufräumen

Die Kopien aus der Test-Share löschen, im **Unraid-Terminal von Klein**:

> **Diesen Kasten allein einfügen und den Pfad genau so übernehmen.** Er löscht die beiden Kopien in der Test-Share; `kopie2` gibt es nur nach 16.13, sonst meldet `rm` das nur.

```bash
rm -r /mnt/mainpool/twinraidtest/kopie /mnt/mainpool/twinraidtest/kopie2
```

Dann **Jetzt synchronisieren**. Meist fragt der Sync nach einer Freigabe, weil die Share auf weniger als die Hälfte schrumpft („Auf weniger als die Hälfte geschrumpft (neu / Basis): twinraidtest …“), bei vielen Dateien auch wegen der Löschungen. **Freigeben und senden**. Die Daten bleiben noch 7 Tage auf Klein und 30 Tage auf Groß in den Snapshots; erst dann wird der Platz frei. Löschen kann sie vorher nur root von Hand, und das ist nicht nötig.

### 16.15 Test 9 entfällt

Festlegung vom 08.11.2026: Bis zur Version 1.0 gibt es keine Servertests für den Rückweg auf die Vorversion. Der Rückweg auf 0.5.1 steht weiter in 16.2 („Scheitert G11, zurück auf 0.5.1“). *Rückweg beschrieben, bis 1.0 nicht auf den Servern geprüft.* Ab 1.0 bekommt jede Version einen Rückweg-Test.

### 16.16 Test M1 (optional) mit 2 GiB

Wie 15.12, jetzt mit `guard.sh` aus 16.2 und der Formel `nötig = Max memory in GiB + 2 GiB`. Erwartet: Das Ergebnis passt zur Rechnung.

### 16.17 Test 5b (optional): Test-Share neu angelegt

Ein Dataset, das auf Klein gelöscht und neu angelegt wurde, darf den Spiegel nicht ersetzen. Der Test löscht dafür auf Klein die Test-Share samt ihrer Snapshots und legt sie neu an; **alle älteren Stände der Test-Share auf Klein sind danach weg.** Nur mit `twinraidtest`, nie mit einer anderen Share.

1. Reiter **Docker** → *twinraid-testcontainer* → **Stop**; er schreibt sonst in die Share.
2. **Ansehen**, im **Unraid-Terminal von Klein**:

   ```bash
   zfs list -H -t snapshot -o name,userrefs -d 1 mainpool/twinraidtest
   zfs list -H -t bookmark -o name -d 1 mainpool/twinraidtest
   ```

   Es dürfen nur Namen mit `@twinraid-` bzw. `#twinraid-` erscheinen. Steht dort ein anderer Snapshot, hier aufhören und im PR nachfragen.
3. **Hold freigeben:**

   > **Diesen Kasten allein einfügen.** Er gibt den Hold `twinraid-base` an jedem Snapshot der Test-Share frei; danach schützt nichts mehr die Basis.

   ```bash
   for s in $(zfs list -H -t snapshot -o name -d 1 mainpool/twinraidtest); do zfs release twinraid-base "$s" 2>/dev/null && echo "freigegeben: $s"; done
   ```

4. **Snapshots und Lesezeichen einzeln löschen,** ohne `-r`:

   > **Diesen Kasten allein einfügen.** Er löscht die eigenen Snapshots und Lesezeichen der Test-Share auf Klein, einen nach dem anderen.

   ```bash
   for s in $(zfs list -H -t snapshot -o name -d 1 mainpool/twinraidtest | grep '@twinraid-'); do zfs destroy "$s"; done
   for b in $(zfs list -H -t bookmark -o name -d 1 mainpool/twinraidtest | grep '#twinraid-'); do zfs destroy "$b"; done
   zfs list -H -t all -r -o name mainpool/twinraidtest
   ```

   Erwartet in der letzten Zeile nur `mainpool/twinraidtest`.
5. **Dataset löschen und neu anlegen:**

   > **Diesen Kasten allein und nur einmal einfügen.** Ein zweiter Aufruf löschte das gerade neu angelegte Dataset wieder. `zfs destroy` ohne `-r` scheitert, solange noch Snapshots oder Kinder da sind.

   ```bash
   zfs destroy mainpool/twinraidtest
   zfs create mainpool/twinraidtest
   mkdir -p /mnt/mainpool/twinraidtest/testcontainer
   zfs list -o name,mountpoint,compression mainpool/twinraidtest
   ```

   Erwartet: `mainpool/twinraidtest  /mnt/mainpool/twinraidtest` mit `on` oder `lz4`.
6. Testcontainer starten (Reiter **Docker** → **Start**) und `bash /boot/config/twinraid/testdata.sh create`; die neue SHA-256 der Prüfliste notieren.
7. **Nur Probelauf** (30 Minuten nach dem letzten Snapshot), danach nach weiteren 30 Minuten **Jetzt synchronisieren**.

| Prüfen | Erwartet |
|---|---|
| Kachel **Unraid Klein**, spätestens 90 Sekunden nach Punkt 5 | der gelbe Hinweis „Die neue Basis von twinraidtest auf … ist nicht gehalten; der nächste erfolgreiche Sync behebt das.“ Er bleibt stehen |
| Probelauf | „Für einen inkrementellen Strom fehlt die Basis auf dem Quellserver.“; `twinraidtest` mit „Basis fehlt“, Spiegel „vorhanden“ |
| Sync | endet **ebenfalls** mit „Für einen inkrementellen Strom fehlt die Basis …“, vor dem Senden. Einen erfolgreichen Sync gibt es hier erst nach dem Rückweg unten; der Hinweis bleibt deshalb stehen |
| Groß | unverändert: gleiche Zahl der Snapshots, `props=ok`, Syslog ohne neuen Empfang |

**Rückweg, nur nach einer Entscheidung im PR.** Er löscht auf Groß alle Stände der Test-Share; danach sendet der nächste Sync vollständig. Im **Unraid-Terminal von Groß**:

> **Diesen Kasten allein einfügen.** Er löscht die Snapshots des Spiegels `twinraidtest` einzeln und dann den Spiegel selbst, jeweils ohne `-r`. Andere Spiegel berührt er nicht.

```bash
for s in $(zfs list -H -t snapshot -o name -d 1 mainpool/twinraid-mirror/twinraidtest | grep '@twinraid-'); do zfs destroy "$s"; done
zfs destroy mainpool/twinraid-mirror/twinraidtest
SSH_ORIGINAL_COMMAND=check bash /boot/config/twinraid/guard.sh sync | grep '^mirror'
```

Erwartet: `mirror twinraidtest state=absent …`. Nach 30 Minuten **Jetzt synchronisieren**: „erfolgreich“, Modus „vollständig (erster Lauf)“, `verify-latest` auf Groß meldet `Keine Abweichung (Lauf 0)` mit der SHA-256 aus Punkt 6. Der gelbe Hinweis verschwindet danach.

### 16.18 Test A3: Aufbewahrung auf Groß nach einem Monat

`guard.sh` löscht Snapshots eines Spiegels frühestens nach 30 Tagen und nur nach einem erfolgreichen Empfang. Frühestens 31 Tage nach dem ersten Sync (bzw. nach dem Rückweg in 16.17), mit mindestens vier Syncs in dieser Zeit, noch einen Sync, dann im **Unraid-Terminal von Groß**:

```bash
zfs list -t snapshot -o name,creation -s creation -d 1 mainpool/twinraid-mirror/twinraidtest
grep 'retention of mirror' /var/log/syslog | tail -n 3
```

Erwartet: kein Snapshot älter als 30 Tage, die drei neuesten immer da; im Syslog `retention of mirror twinraidtest: … snapshots destroyed, 0 failed`. Auf Klein ist kein eigener Snapshot älter als 7 Tage, außer der Basis mit `userrefs` `1`.

### 16.19 Abschluss

Die Ergebnisse von G11, W10, 3, 3b, 4, A1, 5a, 5c, 5e, 6a bis 6d, 7, 5d, 8a und 8b, falls gemacht auch A2, 8c, M1 und 5b, bitte im Pull Request festhalten, A3 später. `actions.sync` kann eingeschaltet bleiben, ebenso `stop_containers: true`. Bevor eine echte Share dazukommt, gilt die Merkliste aus dem Konzept (10.2). Die Simulation ohne echte Server zeigt dasselbe: **Jetzt synchronisieren** in der Kachel **Sync** und `twinraid-admin mock-fault` mit den Namen aus Abschnitt 20.

---

## 17. Von 0.4.2 auf 0.4.3 umstellen

Seit 0.4.3 liest TwinRaid die VM per SSH über `guard.sh` und braucht keinen API-Key mehr. Die Umstellung dauert etwa zehn Minuten. Groß sollte dabei an sein. Der Abschnitt beschreibt den Stand von 0.4.3; wer gleich auf 0.5.1 geht, bekommt in Schritt 3 `guard.sh` Version 3 und liest in Schritt 6 entsprechend `twinraid-guard 3`.

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

## 18. Fehlerbilder und Abhilfe

| Anzeige im Dashboard | Bedeutung | Abhilfe |
|---|---|---|
| Klein: **TwinRaid läuft auf diesem Server** | Normal im Echtbetrieb: TwinRaid läuft auf Klein und prüft Klein deshalb nicht | Nichts zu tun. „Host access to custom networks“ bleibt aus (Schritt 2) |
| Klein: **Wächter nicht eingerichtet** (grau) | Die zwei Pfade des Wächters sind im Template leer | Normal, bis der Wächter läuft. Einrichten nach Abschnitt 12: erst den Wächter starten, dann die Pfade eintragen |
| Klein: **Wächter nicht aktiv seit …** | Der Wächter schreibt keinen Status mehr: Er läuft nicht (Zeile in `/boot/config/go` fehlt, beendet, nach zu vielen Abbrüchen aufgegeben) | Im Unraid-Terminal von Klein `bash /boot/config/twinraid/source-guard.sh status`; läuft er nicht, `… start`. Die Zeile in der go-Datei prüfen (12.3), Syslog von Klein ansehen (Kennung `twinraid-source-guard`) |
| Klein: **An der Stelle der Auftragsdatei liegt ein Verzeichnis …** | TwinRaid wurde neu angelegt und vor dem Wächter gestartet; Docker hat ein Verzeichnis angelegt | Wie in 12.9 unter „So behebst du es im Ernstfall“: TwinRaid stoppen, `rmdir /run/twinraid-source-guard/in/request`, Wächter neu starten, Datei prüfen, TwinRaid starten; danach die Zeile in der go-Datei prüfen |
| TwinRaid startet nicht: Unraid „Bad parameter“, Docker `not a directory: Are you trying to mount a directory onto a file` | Der Wächter lief beim Start von TwinRaid nicht; Docker hat an der Stelle der Auftragsdatei ein Verzeichnis angelegt und kann es nicht auf die Datei im Container einhängen | Wie in 12.9 unter „So behebst du es im Ernstfall“; startet TwinRaid danach mit `docker start TwinRaid` noch nicht, über **Edit → Apply** neu anlegen |
| Klein: **Wächter falsch eingebunden: …** | Im Template fehlt einer der zwei Pfade, oder die Zugriffsart ist vertauscht | Im Template beide Pfade eintragen: Auftragsdatei `Read/Write`, Status `Read Only` (12.3 Punkt 6) |
| Klein: **Wächter: source-guard.conf ist ungültig** | Fehler in `source-guard.conf`; die Kachel nennt Grund und Zeile | `bash /boot/config/twinraid/source-guard.sh check-config` zeigt dasselbe; korrigieren, dann `… restart` |
| Klein: **Wächter: Docker ist … nicht erreichbar** | Kurz nach dem Start von Klein normal: Der Wächter startet vor Docker | Verschwindet von selbst; bleibt es, den Docker-Dienst von Klein prüfen |
| Klein: **Wächter hat seinen Zustand verloren** | Sein Ordner `private/` fehlt, meist nach einem Eingriff von Hand | Die genannten Container in Unraid prüfen und von Hand starten, dann `… restart` |
| Klein: **Status des Wächters ungültig: … anderes Protokoll** | `source-guard.sh` passt nicht zur installierten Version von TwinRaid; TwinRaid ab 0.5.1 verlangt Version 2 oder 3 (Protokoll 2) | Skript wie in 16.2 neu holen (Version 3; Version 2 aus 15.2, Punkt 4, passt ebenfalls), dann `… restart` |
| Klein: **… ist nach dem Lauf nicht wieder gestartet** | Ein angehaltener Container startete nicht wieder | In Unraid den Container ansehen und von Hand starten |
| Hinweis: **Die Erlaubnisliste des Wächters … enthält den Container von TwinRaid** | In `source-guard.conf` steht TwinRaid unter `allow` | Die Zeile löschen, `… restart`; TwinRaid wird nie angehalten |
| Klein: **Das Dataset mainpool/… fehlt** oder **Kompression ist … aus** | Ein Dataset aus `source-guard.conf` gibt es nicht, oder bei ihm ist `compression=off` | Share anlegen bzw. den Namen prüfen; in Unraid in den Einstellungen des Pools „Compression on“ wählen. `on` und `lz4` sind beide in Ordnung |
| Sync: **Nächster Sync frühestens um …** | Pause von 30 Minuten nach jedem Lauf des Wächters | Normal. Für Tests hebt root auf Klein sie mit `bash /boot/config/twinraid/source-guard.sh reset-run-interval` auf |
| Container startet nicht, Log: `exclude is no longer supported since v0.5.1` oder `protect_on_target is no longer supported since v0.5.1` | In `twinraid.yaml` stehen noch Einträge unter diesen Schlüsseln | Den Schlüssel samt Einträgen löschen (15.1), Container neu starten |
| Log: `sync.appdata_share is ignored since v0.5.1` oder dasselbe für `max_deletions`, `exclude`, `protect_on_target` | Entfallene Schlüssel ohne Wirkung | Nur ein Hinweis; die Zeilen bei Gelegenheit löschen |
| Button **Nur Probelauf** oder **Jetzt synchronisieren** grau: „sync.shares in twinraid.yaml passt nicht zu den Zeilen dataset …“ | Die drei Ebenen enthalten verschiedene Namen | `sync.shares` und die Zeilen `dataset` in `source-guard.conf` angleichen, dann TwinRaid bzw. `source-guard.sh restart` (15.6) |
| Button **Nur Probelauf** oder **Jetzt synchronisieren** grau: „der Wächter … ist nicht aktiv oder ZFS ist dort nicht verfügbar“ | Wächter nicht aktiv, oder `mainpool` ist noch nicht da | Kachel Klein ansehen; kurz nach einem Start von Klein normal |
| Probelauf: „Der Zielserver hat die Prüfung (check) nicht beantwortet.“ | Der Grund steht dabei: Key „sync“ fehlt, Hostkey nicht bestätigt, Key abgelehnt, SSH nicht erreichbar oder `guard.sh` zu alt | Test K1 (15.3) nachholen. Key abgelehnt: Zeile aus `init-key` auf Groß prüfen (`from=` muss die IP des Hosts Klein sein, nicht die von TwinRaid) |
| Probelauf: „Die Rechte für den Empfang (zfs allow) … stimmen nicht“ | `zfs allow mainpool/twinraid-mirror` weicht von 15.3 ab | Delegation wie in 15.3 herstellen; mit `check` prüfen (`deleg=ok`). `id-in-use` heißt: UID oder GID 64000 steht in `/etc/passwd` oder `/etc/group` |
| Probelauf: „… liegt an der Stelle eines Spiegels ein fremdes Dataset …“ | Unter `mainpool/twinraid-mirror/<Name>` liegt ein Dataset, das `guard.sh` nicht empfangen hat | Ansehen und, wenn es nichts Wichtiges enthält, ohne `-r` löschen: `zfs destroy mainpool/twinraid-mirror/<Name>` (15.8) |
| Probelauf: „Ein Spiegel … ist gesperrt (props-mismatch) …“ | `guard.sh` hat einen Spiegel gesperrt | [WIEDERHERSTELLUNG.md](WIEDERHERSTELLUNG.md), Abschnitt „Spiegel gesperrt“ |
| Probelauf: „Zu kurz nach dem letzten Snapshot …“ | Der Wächter legt höchstens alle 30 Minuten einen Snapshot an | 30 Minuten warten |
| Button **Jetzt synchronisieren** grau: „der Wächter … hält die Pause zwischen zwei Läufen ein; frühestens um …“ | Nach jedem Lauf mit angehaltenen Containern gilt eine Pause von 30 Minuten | Bis zur genannten Uhrzeit warten; für Tests hebt root auf Klein sie mit `… reset-run-interval` auf |
| Button **Jetzt synchronisieren** grau: „auf … besteht laut letzter Prüfung ein Klon eines Spiegels“ | Von einem Spiegel auf Groß gibt es einen Klon, etwa aus einem Notbetrieb; solange er besteht, nimmt `guard.sh` nichts an | Auf Groß ansehen: `zfs list -H -o name,origin -r mainpool \| grep twinraid-mirror`. Einen Klon nur löschen, wenn er nicht mehr gebraucht wird; danach ein Probelauf, damit TwinRaid den neuen Stand kennt |
| Sync: „Die Löschungen wurden abgelehnt; nichts wurde gesendet.“ | Im Dialog wurde **Ablehnen** gewählt | Gewollt? Sonst nach 30 Minuten erneut synchronisieren und **Freigeben und senden** wählen (16.7) |
| Sync: „Die Löschungen wurden nicht rechtzeitig freigegeben …“ | Der Dialog stand 30 Minuten ohne Antwort; der Wächter hat den Job geschlossen | Erneut synchronisieren und den Dialog beantworten |
| Sync: „Gesendet, aber ein angehaltener Container läuft danach nicht wieder.“ | Die Daten sind auf Groß angekommen, aber ein Container auf Klein startete nach dem Snapshot nicht wieder; die Kachel **Unraid Klein** nennt ihn | Den Container in Unraid ansehen und von Hand starten |
| Sync: „Das Senden ist gescheitert. …“ | Eine Share kam nicht an; der Grund steht darunter. Shares, die vorher angekommen sind, sind auf Groß aktuell | Grund beheben (etwa SSH auf Groß); der nächste Sync sendet die fehlenden Änderungen |
| Sync: „Der Spiegel auf dem Zielserver wurde seit dem letzten Snapshot verändert …“ | Jemand hat im Spiegel auf Groß geschrieben (nur mit `readonly=off` möglich); ZFS nimmt dann nichts mehr an | Auf Groß ansehen, was sich geändert hat, dann auf den neuesten Snapshot zurücksetzen wie in 16.9, Fall (i) |
| Sync: „Für einen inkrementellen Strom fehlt die Basis auf dem Quellserver.“ | Auf Klein fehlen der Snapshot und das Lesezeichen, auf denen der Spiegel aufbaut, etwa weil das Dataset neu angelegt wurde | Wie in 16.17: nach einer Entscheidung den Spiegel auf Groß von Hand löschen; der nächste Sync sendet vollständig |
| Sync: „Der Wächter hat abgelehnt.“ mit „Grund: der Totmannschalter …“ oder „docker stop ist … fehlgeschlagen“ | Die Container-Phase ist gescheitert; TwinRaid hat vor dem Snapshot abgebrochen und die angehaltenen Container wieder starten lassen | Container in Unraid prüfen; beim Totmannschalter das Syslog von Klein ansehen (`twinraid-source-guard`) |
| Sync: „Wird abgebrochen … (wartet auf den Container)“ | Ein Abbruch wartet, bis `docker stop` oder der Neustart eines Containers fertig ist | Normal; höchstens `stop_timeout_seconds` plus 15 Sekunden |
| Sync: „Übertragung läuft weiter (vor dem Neustart begonnen).“ | TwinRaid wurde während des Sendens neu gestartet; der Wächter sendet weiter | Normal; am Ende steht der Lauf in der Historie mit „nach einem Neustart übernommen“ |
| Hinweis Kachel Klein: „Die neue Basis von … ist nicht gehalten …“ | Nach dem Senden hat der Wächter den Snapshot nicht mit einem Hold festgehalten, oder auf Klein fehlt die Basis ganz | Der nächste erfolgreiche Sync behebt das. Endet er mit „fehlt die Basis“, wie in 16.17 |
| Hinweis: **Auf … ist keine Grenze für den ZFS-Cache eingetragen** | Bei `zfs_arc_max` steht in Unraid `0` | Auf Groß *Settings → Disk Settings* → **Tunable (zfs_arc_max)** eintragen (13.6) |
| VM: **Auf dem Server ist nicht genug Arbeitsspeicher frei** | `guard.sh` hat den Start abgelehnt (`memory-low`, 15.12) | Eine andere VM herunterfahren, oder die Grenze des ZFS-Caches senken |
| VM: **Ein durchgereichtes Gerät der VM … wird gerade von einer anderen VM benutzt** | Eine andere VM nutzt dieselbe Grafikkarte (`device-busy`, 11.21) | Die andere VM in Unraid herunterfahren |
| VM: **Unraid konnte die VM nicht starten** | `virsh start` scheiterte aus einem anderen Grund | `/var/log/libvirt/libvirtd.log` auf Groß ansehen |
| Browser zeigt nur **forbidden** | Das Gerät steht nicht in `web.allowed_networks`; im Container-Log steht `Rejected request from … not in web.allowed_networks` | IP des Geräts eintragen (im Server-Netz einzeln als `/32`, Schritt 3.3) und den Container neu starten |
| Log: `… has host bits set` | Ein Eintrag in `allowed_networks` wie `192.168.10.50/24` | Einzelnes Gerät als `192.168.10.50/32`, ganzes Netz als `192.168.10.0/24` schreiben; die Meldung nennt beide Varianten |
| TwinRaid aus dem Unraid-Terminal von Klein nicht erreichbar (`ping`, `curl`) | Normal ohne „Host access to custom networks“ | Nichts zu tun; von anderen Geräten aus testen |
| MAC nach Update oder Neustart anders, UniFi zeigt ein neues Gerät | Feld **Fixed MAC address** im Template leer oder Unraid älter als 7.3 | MAC aus Schritt 1 im Template eintragen (Schritt 4.2), danach Schritt 5.4 |
| Groß: **Nicht erreichbar**, VM: **Server offline** | Unraid Groß ist aus oder nicht erreichbar | Normal, wenn Groß aus ist. Sonst IP und `webgui` (Port der Unraid-Oberfläche) in `twinraid.yaml` prüfen |
| VM: **Statusverbindung wird aufgebaut …** | Groß ist gerade angegangen, SSH antwortet noch nicht | Normal nach dem Start von Groß; verschwindet nach wenigen Sekunden |
| VM: **Statusverbindung unterbrochen – neuer Versuch folgt** | Die SSH-Verbindung für den Status ist abgebrochen | TwinRaid versucht es nach 5 s, 15 s, 60 s und dann alle 5 Minuten erneut; die Uhrzeit des nächsten Versuchs steht darunter (ab 0.4.4). Bleibt die Meldung, SSH auf Groß und das Netz prüfen |
| VM: **Nächster Versuch um …** (ab 0.4.4) | Nach einem Fehler der Statusverbindung wartet TwinRaid, damit Groß nicht mit Anmeldungen überhäuft wird: nach Abbrüchen 5 Sekunden bis 5 Minuten, nach Fehlern bei Key, Hostkey oder `guard.sh` 30 Minuten | Die Ursache aus der Meldung darüber beheben, dann `twinraid-admin trust-host-key target`. Besteht der Test mit dem Key „status“, endet die Wartezeit sofort (Test G9, 11.22) |
| VM: **Kein SSH-Key „status“** | Schritt 10.2 bzw. Abschnitt 17, Punkt 5 fehlt | `twinraid-admin init-ssh-keys --key status` und die Zeile auf Groß eintragen |
| VM: **SSH-Hostkey des Servers nicht bestätigt** | Hostkey von Groß noch nicht bestätigt | `twinraid-admin trust-host-key target` (10.2) |
| VM: **Der Server lehnt den SSH-Key „status“ ab** | Zeile für „status“ fehlt oder ist abgeschnitten, oder TwinRaid hat eine andere IP als in `from=` | `twinraid-admin init-ssh-keys --key status` (gibt die Zeile erneut aus) und auf Groß eintragen oder ersetzen; danach `twinraid-admin trust-host-key target`, dann verbindet sich TwinRaid sofort statt erst zur angezeigten Uhrzeit |
| VM: **guard.sh lehnt ab** | Auf Groß liegt noch `guard.sh` Version 1 (kennt den Key „status“ nicht), oder die Zeile ruft `guard.sh` in einem anderen Modus auf | `guard.sh` wie in 9.2 neu holen; die Zeile für „status“ muss auf `guard.sh status` enden. Danach `twinraid-admin trust-host-key target` (verbindet sofort neu) |
| VM: **guard.sh auf dem Server ist zu alt** | `guard.sh` ist älter als die installierte TwinRaid-Version verlangt; ab 0.5.1 ist das Version 3 | `guard.sh` wie in 16.2 neu holen, danach `twinraid-admin trust-host-key target` (verbindet sofort neu) |
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
| „guard.sh auf dem Server hat den Befehl abgelehnt“ | `guard.sh` auf Groß ist veraltet, oder die Zeile für „power“ ruft einen anderen Modus auf | `guard.sh` wie in 16.2 neu holen |
| „Der Befehl ist auf dem Server fehlgeschlagen“ | `guard.sh` fehlt unter `/boot/config/twinraid/guard.sh`, oder `virsh` meldet einen Fehler | Ablage von `guard.sh` prüfen (9.2); Syslog von Groß ansehen (*Tools → System Log*, `twinraid-guard`) |
| „Die VM ist auf dem Server nicht eingetragen“ (bei einer Aktion) | `power.conf` fehlt oder ist ungültig | Schritt 9.3 |
| „Der Server wurde nicht ausgeschaltet, weil die VM noch lief“ | `guard.sh` hat unmittelbar vor dem Ausschalten eine laufende VM gesehen | VM herunterfahren (lassen) und Groß erneut herunterfahren |
| „Der Server wurde nicht ausgeschaltet, weil gerade ein Sync empfangen wird“ | Ein Empfang lief auf Groß (`sync-running`) | Warten, bis der Empfang endet, und erneut herunterfahren |
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

## 19. Updates einspielen und Herkunft prüfen

- Unraid prüft regelmäßig, ob es eine neue Version gibt. Im Reiter **Docker** steht dann bei TwinRaid **update ready**. Klick auf das Symbol → **Update**. Daten und Einstellungen bleiben erhalten, die MAC dank des Felds **Fixed MAC address** ebenso. Nach dem ersten Update einmal wie in Schritt 5.4 vergleichen.
- Von 0.4.3 auf 0.4.4 genügt das Update: `guard.sh` (Version 2), `power.conf`, die Keys und `twinraid.yaml` bleiben unverändert.
- Von 0.4.4 auf 0.5.0 genügt für den bisherigen Betrieb ebenfalls das Update; `guard.sh` bleibt Version 2. Neu ist der Wächter auf Klein mit zwei weiteren Pfaden im Template; bis er eingerichtet ist (Abschnitt 12, ab Test W1), zeigt die Kachel Klein grau „Wächter nicht eingerichtet“.
- Von 0.5.0 auf 0.5.1 genügt das Update **nicht**: Vorher `exclude` und `protect_on_target` aus `twinraid.yaml` entfernen, sonst startet der Container nicht. TwinRaid 0.5.1 verlangt `guard.sh` Version 3 und `source-guard.sh` Version 2. Reihenfolge und Rückweg stehen in 15.1 und 15.2. Erst TwinRaid aktualisieren, dann `guard.sh` kopieren, dann `twinraid-admin trust-host-key target`, dann `source-guard.sh` kopieren.
- Von 0.5.1 auf 0.5.2 genügt das Update für den bisherigen Betrieb: `twinraid.yaml` bleibt, und TwinRaid 0.5.2 arbeitet mit `guard.sh` 3 und `source-guard.sh` 2 aus 0.5.1. Für den echten Sync kommen `source-guard.sh` Version 3, `guard.sh` mit 2 GiB Abstand und `testdata.sh` auf beiden Servern dazu, in der Reihenfolge aus 16.2; erst danach `actions.sync` freischalten. Der Rückweg steht in 16.2. *Rückweg beschrieben, bis 1.0 nicht auf den Servern geprüft.*
- Das Template verwendet `ghcr.io/mantua1/twinraid:latest`. Dieser Tag zeigt immer auf das neueste Release. Wer eine Version festhalten möchte, trägt unter *Repository* zum Beispiel `ghcr.io/mantua1/twinraid:0.3` ein.
- Jedes Image wird ausschließlich von GitHub Actions aus diesem Repository gebaut und hat einen signierten Herkunftsnachweis. Prüfen kannst du ihn auf einem PC mit der GitHub-CLI (`gh`):

```bash
gh attestation verify oci://ghcr.io/mantua1/twinraid:0.5.2 --owner mantua1
```

Steht am Ende `Verification succeeded!`, stammt das Image nachweislich aus diesem Repository.

---

## 20. Befehle der Admin-CLI

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
| `twinraid-admin show-sync-log` | Ab 0.5.2: Sync-Historie anzeigen, je Lauf eine Zeile mit Art, Ergebnis und Grund, darunter je Share Modus, Zählungen, Größe und gesendete Menge (`--lines 100` für mehr Läufe) |
| `twinraid-admin sync-check` | Ab 0.5.1, nur im Modus `real`, ändert nichts: die drei Ebenen (`twinraid.yaml`, `source-guard.conf`, `mirror.conf`), den Wächter, das eigene Dataset von TwinRaid, die Spiegel auf Groß aus dem letzten Probelauf und die Grenze des ZFS-Caches auf Groß |
| `twinraid-admin mock-tour on` / `off` | Nur im Simulationsmodus: alle Zustände und Ursachen nacheinander zeigen (Aktionen sind dann gesperrt) |
| `twinraid-admin mock-fault <Name> on` / `off` | Nur im Simulationsmodus: Fehler vortäuschen, `<Name>` ist `vm-hang`, `ssh-denied`, `no-boot`, `other-vm` (eine weitere VM läuft), `vm-not-configured`, `libvirt-down`, `guard-outdated`, `connect-plugin`, `status-key-rejected` (Groß lehnt den Key „status“ ab), ab 0.5.0 `source-guard-inactive` (Wächter läuft nicht), `source-guard-lost` (Wächter hat seinen Zustand verloren) oder `source-guard-start-failed` (ein Container startet nach dem Lauf nicht wieder), ab 0.5.1 `sync-deletions` (1500 Löschungen; ab 0.5.2 fragt der Sync nach der Freigabe), `sync-empty-source` (Quelle fast leer; ab 0.5.2 ebenso mit Freigabe), `sync-target-refused`, `target-exists`, `target-clone` (wirken auf Probelauf und Sync), `sync-send-error` (ab 0.5.2: die letzte Share scheitert mit „Das Senden ist gescheitert“, Grund „SSH auf … nicht erreichbar“; die übrigen kommen an und behalten ihre neue Basis), `sync-slow` (ab 0.5.2: Senden mit 1 MiB/s, knapp 6 Minuten je Share, zum Ausprobieren von Abbrechen und Restzeit), `vm-device-busy`, `vm-memory-low` (Start der VM scheitert) und `arc-unlimited` (keine Grenze für den ZFS-Cache auf Groß) |

`set-api-key` und `trust-api-cert` gibt es seit 0.4.3 nicht mehr.

**Wächter auf Klein** (ab 0.5.0), im **Unraid-Terminal von Klein**, jeweils mit `bash /boot/config/twinraid/source-guard.sh …`:

| Befehl | Wirkung |
|---|---|
| `start` / `stop` / `restart` | Wächter starten, stoppen (ein offener Lauf startet vorher seine Container) oder beides; `start` steht auch in `/boot/config/go` |
| `status` | ob er läuft, dazu `status.json` |
| `check-config` | `source-guard.conf` prüfen, ohne etwas zu ändern |
| `selftest <Container>` | Lauf öffnen, den Container anhalten, Lauf beenden; nur für Tests (12.4) |
| `selftest-deadman <Container>` | wie `selftest`, aber ohne Herzschlag: Der Totmannschalter muss den Container wieder starten (12.5) |
| `reset-run-interval` | die Pause von 30 Minuten nach einem Lauf aufheben; nur root auf Klein kann das, TwinRaid nicht. Der Abstand von 30 Minuten zwischen zwei Snapshots bleibt. Ab Version 3 meldet der Befehl Erfolg erst, wenn der neue Status geschrieben ist |
| `init-key` | ab 0.5.1: Key „sync“ erzeugen, falls er fehlt, und die Zeile für *Users → root → SSH authorized keys* auf Groß ausgeben (15.3); `--new` ersetzt ihn |
| `trust-target` | ab 0.5.1: Hostkey von Groß anzeigen, nach `yes` speichern und `guard.sh sync check` testen (15.3); `--fingerprint SHA256:…` statt der Frage |

**Testdaten** (ab 0.5.1, 15.5), im Unraid-Terminal von Klein: `bash /boot/config/twinraid/testdata.sh create` legt sie in `/mnt/mainpool/twinraidtest/testdata` an, `… change` ändert sie auf bekannte Weise, `… verify [Ordner]` vergleicht einen Ordner mit der Prüfliste und ändert nichts. Auf Groß prüft `verify` nur in einem Snapshot (`…/.zfs/snapshot/<Name>/testdata`); den eingehängten Spiegel lehnt es ab 0.5.2 ab, weil der Kernel dort eine alte ACL zeigen kann. Ab 0.5.2, im Unraid-Terminal von Groß (16.2):

| Befehl | Wirkung |
|---|---|
| `… verify-latest [Dataset]` | `verify` im neuesten Snapshot des Datasets, ohne Angabe `mainpool/twinraid-mirror/twinraidtest`; nennt den Snapshot. Liest nur |
| `… verify-container [Dataset\|Ordner]` | sagt, ob der Testcontainer beim neuesten Snapshot (oder im angegebenen Snapshot-Ordner) sauber angehalten war oder lief. Liest nur |

**Machbarkeitstest Z1** (ab 0.5.0, 13.3): `bash /tmp/zfs-check.sh target` auf Groß, `… source` auf Klein, `… cleanup target|source` nach einem harten Abbruch.
