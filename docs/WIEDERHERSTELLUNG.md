# TwinRaid: Wiederherstellung

> **Stand: Version 0.5.3.** Diese Anleitung ist für den Ernstfall: den letzten Stand finden, einzelne Dateien zurückholen, einen gesperrten Spiegel freigeben, den Notbetrieb auf Groß starten und danach zurück nach Klein. Die Vorlagen der Container (Templates) kommen erst mit Version 0.5.4; bis dahin legst du die Container im Notbetrieb von Hand an.

Die Grundlagen stehen in [KONZEPT.md](KONZEPT.md), Abschnitte 3.7 und 4.6. TwinRaid hat für die Schritte hier keine Funktion; sie laufen von Hand als root, fast alle im **Unraid-Terminal von Groß**. Wo ein Befehl auf Klein läuft, steht das dabei.

## Inhalt

- [Überblick](#überblick)
  - [Variablen und Verbindung zu Klein](#variablen-und-verbindung-zu-klein)
- [Letzten Stand finden](#letzten-stand-finden)
- [Einzelne Dateien zurückholen](#einzelne-dateien-zurückholen)
- [Spiegel gesperrt](#spiegel-gesperrt)
  - [Woran du es erkennst](#woran-du-es-erkennst)
  - [Vorbereitung](#vorbereitung)
  - [Nach `received-properties` oder `props-mismatch`](#nach-received-properties-oder-props-mismatch)
  - [Nach `props-not-applied`](#nach-props-not-applied)
- [Notbetrieb starten](#notbetrieb-starten)
- [Rückweg nach Klein](#rückweg-nach-klein)
- [Aufräumen auf Groß](#aufräumen-auf-groß)
- [Vor dem ersten echten Spiegel von appdata](#vor-dem-ersten-echten-spiegel-von-appdata)

---

## Überblick

| Lage | Abschnitt |
|---|---|
| Du willst wissen, welcher Stand auf Groß liegt und wann er ankam | [Letzten Stand finden](#letzten-stand-finden) |
| Klein läuft, aber einzelne Dateien sind gelöscht oder beschädigt | [Einzelne Dateien zurückholen](#einzelne-dateien-zurückholen) |
| TwinRaid meldet einen gesperrten Spiegel (`props-mismatch`, `props-not-applied`, `received-properties`) | [Spiegel gesperrt](#spiegel-gesperrt) |
| Klein ist ausgefallen, die Container sollen auf Groß laufen | [Notbetrieb starten](#notbetrieb-starten) |
| Klein läuft wieder und soll den Stand aus dem Notbetrieb übernehmen | [Rückweg nach Klein](#rückweg-nach-klein), danach [Aufräumen auf Groß](#aufräumen-auf-groß) |
| Bevor `appdata` zum ersten Mal gespiegelt wird | [Vor dem ersten echten Spiegel von appdata](#vor-dem-ersten-echten-spiegel-von-appdata) |

> **Grundsatz: Container laufen nie gleichzeitig auf Klein und Groß.** Sie haben auf beiden Servern dieselben festen IP- und MAC-Adressen, und doppelt laufende Container wirken doppelt nach außen, etwa ein Sync-Client, der alte Dateien hochlädt. Deshalb bleibt Klein im Notbetrieb aus, und der Rückweg beginnt mit Klein ohne Netzwerk.

Die Spiegel liegen auf Groß unter `mainpool/twinraid-mirror/<Share>` und sind schreibgeschützt. Jeder Sync ist dort ein eigener Snapshot; `guard.sh` bewahrt sie 30 Tage auf und löscht nie die drei neuesten. Auf Klein bleiben die eigenen Snapshots 7 Tage und die Basis des nächsten Syncs.

Die Befehle der Abschnitte „Letzten Stand finden“, „Notbetrieb starten“, „Rückweg nach Klein“ und „Aufräumen auf Groß“ führt `docker/recovery-test.sh` in der CI genau so aus, mit Pools in Dateien. Sie stehen deshalb ohne Platzhalter da; die einzige Stelle, die du anpasst, ist der Kasten unten.

### Variablen und Verbindung zu Klein

Die Befehle verwenden die Variable `N` für den Namen der Share (hier das Beispiel `twinraidtest`) und für den Rückweg die Kurzform `klein` für Befehle auf Klein. Statt `<IP von Klein>` trägst du die IP-Adresse von Klein ein, ohne die spitzen Klammern:

```bash
N=twinraidtest
klein() { ssh root@<IP von Klein> "$@"; }
```

Ein Terminal merkt sich die beiden Zeilen nur, solange es offen ist; nach einem neuen Fenster sie zuerst erneut ausführen. Für eine weitere Share den Kasten mit ihrem Namen wiederholen und den Abschnitt von vorn beginnen.

- **`klein` braucht nur der Rückweg.** Es meldet sich mit deinem eigenen Admin-Zugang als root auf Klein an (Passwort von root oder dein eigener Key), nie mit den Keys von TwinRaid oder dem Key „sync“ des Wächters. Auf Klein muss SSH an sein (*Settings → Management Access → Use SSH*).
- **Erste Verbindung:** `ssh` zeigt den Fingerabdruck des Hostkeys von Klein und fragt nach. Vergleiche ihn mit `ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub` im Unraid-Terminal von Klein (zeigt `ssh` einen anderen Typ, die passende Datei in `/etc/ssh/` nehmen), dann `yes`.

---

## Letzten Stand finden

Im **Unraid-Terminal von Groß**, nach dem Kasten aus [Variablen und Verbindung zu Klein](#variablen-und-verbindung-zu-klein). Die Befehle ändern nichts.

```bash
zfs list -t snapshot -d 1 -s createtxg -o name,creation,twinraid:received "mainpool/twinraid-mirror/$N"
zfs get -H -d 1 -t snapshot -o name,value,source twinraid:received "mainpool/twinraid-mirror/$N" | while read -r s t q; do
  if [ "$q" = local ] && [ "$t" -ge 0 ] 2>/dev/null; then echo "$s  empfangen $(date -d "@$t" '+%d.%m.%Y %H:%M')"; else echo "$s  ohne gültiges twinraid:received"; fi
done
zfs get -H -o property,value canmount,mounted "mainpool/twinraid-mirror/$N"
```

| Ausgabe | Bedeutung |
|---|---|
| erster Befehl | alle Snapshots des Spiegels, ältester zuerst. Der **unterste ist der letzte Stand**. `creation` ist die Zeit des Snapshots auf Klein, `twinraid:received` die Zeit des Empfangs auf Groß in Sekunden seit 1970 |
| zweiter Befehl | dieselben Snapshots mit der Zeit des Empfangs als Datum. „ohne gültiges twinraid:received“ heißt: Der Wert fehlt oder kam nicht von `guard.sh` (Quelle nicht `local`); so ein Snapshot stammt nicht aus einem normalen Sync |
| dritter Befehl | `canmount on` und `mounted yes`. Zeigt `canmount` `noauto`, ist der Spiegel gesperrt: zuerst [Spiegel gesperrt](#spiegel-gesperrt) |

Meldet `zfs` `dataset does not exist`, gibt es für diese Share keinen Spiegel; dann steht in `N` ein anderer Name als in `mirror.conf`, oder sie wurde nie gespiegelt.

---

## Einzelne Dateien zurückholen

Für Dateien, die auf Klein gelöscht oder beschädigt sind, während Klein läuft. Geholt wird **immer aus einem Snapshot**, nie aus dem eingehängten Spiegel: Dort kann der Kernel nach einem Empfang noch eine alte ACL zeigen (Konzept 3.7). In den Snapshots bleibt alles unverändert; der Ordner `.zfs` ist versteckt, aber über den Pfad erreichbar.

**Auf Klein aus einem eigenen Snapshot** (die letzten 7 Tage), im **Unraid-Terminal von Klein**, nach `N=<Share>`:

```bash
ls "/mnt/mainpool/$N/.zfs/snapshot/"
```

Die Namen lauten `twinraid-<Datum>T<Zeit>Z` in UTC. Dann eine Datei mit Besitzer, Rechten, Zeitstempeln, xattrs und ACLs zurückkopieren. Statt `<Snapshot>` den Namen aus der Liste eintragen, statt `<Pfad>` den Pfad innerhalb der Share, etwa `ordner/datei.txt`:

```bash
cp -a -i "/mnt/mainpool/$N/.zfs/snapshot/<Snapshot>/<Pfad>" "/mnt/mainpool/$N/<Pfad>"
```

`-i` fragt, bevor eine vorhandene Datei überschrieben wird. Ein ganzer Ordner geht ebenso; dann als Ziel den übergeordneten Ordner mit `/` am Ende angeben.

**Von Groß aus einem Snapshot des Spiegels** (30 Tage), im **Unraid-Terminal von Groß**, nach dem Kasten aus [Variablen und Verbindung zu Klein](#variablen-und-verbindung-zu-klein):

```bash
ls "/mnt/mainpool/twinraid-mirror/$N/.zfs/snapshot/"
```

Dann mit `rsync` über deinen Admin-Zugang nach Klein, nie mit den Keys von TwinRaid. `-aAXH` übernimmt Besitzer, Rechte, Zeitstempel, ACLs, xattrs und Hardlinks; `-s` erlaubt Leerzeichen im Pfad auf Klein. Statt `<IP von Klein>`, `<Snapshot>` und `<Pfad>` deine Werte eintragen:

```bash
rsync -aAXHs "/mnt/mainpool/twinraid-mirror/$N/.zfs/snapshot/<Snapshot>/<Pfad>" "root@<IP von Klein>:/mnt/mainpool/$N/<Pfad>"
```

`rsync` überschreibt eine vorhandene Datei ohne Frage. Willst du erst vergleichen, als Ziel einen anderen Namen angeben, etwa `<Pfad>.zurueck`.

---

## Spiegel gesperrt

`guard.sh` sperrt einen Spiegel auf Groß, wenn nach einem Empfang etwas nicht stimmt. Es hängt ihn aus, setzt `canmount=noauto` und nimmt keine weiteren Empfänge an, damit nichts Unerwartetes eingehängt wird. Die Daten und alle Snapshots bleiben dabei unverändert. Aufheben kann die Sperre nur root auf Groß; TwinRaid und Klein haben dafür keinen Befehl.

| Grund | Bedeutung | Weiter mit |
|---|---|---|
| `received-properties` | Der Strom brachte Eigenschaften oder Kinder mit. Ein Strom des Wächters tut das nie; praktisch heißt das: **Klein ist übernommen** | [Nach `received-properties` oder `props-mismatch`](#nach-received-properties-oder-props-mismatch) |
| `props-mismatch` | Weitere Empfänge werden abgelehnt, weil der Spiegel schon gesperrt ist | wie oben, oder [Nach `props-not-applied`](#nach-props-not-applied), wenn das Syslog diesen Grund nennt |
| `props-not-applied` | Ein fester Wert fehlt am Spiegel. Die Ursache liegt meist in der Delegation, nicht auf Klein | [Nach `props-not-applied`](#nach-props-not-applied) |

### Woran du es erkennst

- **TwinRaid:** Der Probelauf nennt „Ein Spiegel … ist gesperrt (props-mismatch)“; `twinraid-admin sync-check` zeigt beim Spiegel `props mismatch`.
- **Groß:** *Tools → System Log* enthält Zeilen mit `twinraid-guard: refused: …` und `mirror … locked`.

### Vorbereitung

Die Befehle unten verwenden zwei Variablen. Trage bei `N` den Namen des gesperrten Spiegels ein (hier das Beispiel `twinraidtest`):

```bash
N=twinraidtest
D=mainpool/twinraid-mirror/$N
```

Ein Terminal merkt sich die beiden Zeilen nur, solange es offen ist; nach einem neuen Fenster sie zuerst erneut ausführen.

### Nach `received-properties` oder `props-mismatch`

**1. Ursache auf Klein zuerst klären.** Ein Strom mit Eigenschaften kommt nie vom Wächter. Solange unklar ist, wie er zustande kam, gilt Klein als übernommen: TwinRaid auf Klein stoppen und die Zeile für den Key „sync“ auf Groß unter *Users → root → SSH authorized keys* entfernen. Erst wenn Klein wieder vertrauenswürdig ist, geht es auf Groß weiter.

**2. Den Fund ansehen.** Diese Befehle ändern nichts:

```bash
zfs get -H -r -t filesystem,volume,snapshot -s received -o name,property,value all "$D"
zfs list -r -t all -o name,type,canmount,mounted "$D"
zfs list -H -t snapshot -d 1 -s creation -o name,guid,creation "$D"
zfs get -H -d 1 -t snapshot -o name,value,source twinraid:received "$D"
grep twinraid-guard /var/log/syslog | tail -n 30
```

| Befehl | zeigt |
|---|---|
| erster | jede Eigenschaft, die aus dem Strom kam, mit Dataset oder Snapshot; das sind genau die Werte für Schritt 4 |
| zweiter | ob der Spiegel Kinder hat, und `canmount` (`noauto` heißt gesperrt) |
| dritter | die Snapshots, ältester zuerst, mit GUID und Zeit |
| vierter | `twinraid:received` je Snapshot. Der **letzte gute** Snapshot ist der neueste mit Quelle `local`, an dem der erste Befehl nichts meldet und dessen GUID auf Klein zu einem eigenen Snapshot passt. Der gesperrte Empfang bekommt diese Eigenschaft nicht |
| fünfter | Zeitpunkt und Grund der Sperre im Syslog von Groß |

**3. Verdächtige Snapshots einzeln löschen,** alle nach dem letzten guten, neuester zuerst, immer mit `@` im Namen und nie mit `-r`. Danach zurück auf den letzten guten; er ist dann der neueste, also geht `zfs rollback` ohne `-r`:

```bash
zfs destroy "$D@twinraid-20261101T120000Z"     # Beispiel: je verdächtigem Snapshot eine Zeile
zfs rollback "$D@twinraid-20261031T120000Z"    # Beispiel: der letzte gute Snapshot
```

Hat der zweite Befehl aus Schritt 2 **Kinder** gezeigt, löschst du sie ebenso einzeln, zuerst ihre Snapshots, dann das Kind selbst, ohne `-r`:

```bash
zfs destroy "$D/kind@snapshot"     # Beispiel
zfs destroy "$D/kind"              # Beispiel
```

Gibt es **keinen guten Snapshot**, weil schon der erste Empfang der verdächtige war, löschst du alle Snapshots wie oben und dann den Spiegel selbst, ohne `-r`. Der nächste Sync sendet dann vollständig, und Schritt 4 und 5 entfallen:

```bash
zfs destroy "$D"
```

**4. Werte prüfen.** Für jede Eigenschaft, die der erste Befehl aus Schritt 2 **jetzt noch** meldet, genau eine Zeile `zfs inherit`. Nur diese Eigenschaften, nie eine der festen aus der Tabelle unten, sonst fiele etwa `readonly=on` weg:

```bash
zfs get -H -r -t filesystem,volume,snapshot -s received -o name,property,value all "$D"
zfs inherit quota "$D"                                   # Beispiel: nur gemeldete Eigenschaft
zfs inherit twinraid:beispiel "$D@twinraid-20261031T120000Z"   # Beispiel: an einem Snapshot
```

Danach muss der erste Befehl nichts mehr ausgeben. Dann alle festen Werte ansehen:

```bash
zfs get -H -o property,value,source readonly,exec,setuid,devices,primarycache,secondarycache,compression,atime,xattr,acltype,recordsize,snapdir,sync,dedup,copies,checksum,quota,refquota,reservation,refreservation,sharesmb,sharenfs,twinraid:mirror,mountpoint,canmount "$D"
```

Jede Zeile muss in der dritten Spalte `local` zeigen, und die Werte müssen zur Tabelle passen. `compression` darf auch `on` sein. `canmount` zeigt noch `noauto`; das ändert Schritt 5. Weicht ein Wert ab, weiter wie in [Nach `props-not-applied`](#nach-props-not-applied), Schritt 2.

| Eigenschaft | Wert | Eigenschaft | Wert |
|---|---|---|---|
| `readonly` | `on` | `snapdir` | `hidden` |
| `exec` | `off` | `sync` | `standard` |
| `setuid` | `off` | `dedup` | `off` |
| `devices` | `off` | `copies` | `1` |
| `primarycache` | `metadata` | `checksum` | `on` |
| `secondarycache` | `all` | `quota`, `refquota` | `none` |
| `compression` | `lz4` | `reservation`, `refreservation` | `none` |
| `atime` | `off` | `sharesmb`, `sharenfs` | `off` |
| `xattr` | `sa` | `twinraid:mirror` | `1` |
| `acltype` | `posix` | `mountpoint` | `/mnt/mainpool/twinraid-mirror/<Name>` |
| `recordsize` | `128K` | `canmount` | `on` (erst nach Schritt 5) |

**5. Sperre aufheben.** Das geht nur hier von Hand:

```bash
zfs set canmount=on "$D"
SSH_ORIGINAL_COMMAND=check bash /boot/config/twinraid/guard.sh sync
```

Erwartet: In der Zeile des Spiegels steht `props=ok`. `guard.sh` hängt den Spiegel beim nächsten Empfang nach der Prüfung selbst ein; bis dahin bleibt er ausgehängt.

**6. Danach** läuft der Sync inkrementell von der Basis weiter, also vom letzten guten Snapshot. Eine Zeile für den Key „sync“, die du in Schritt 1 entfernt hast, trägst du erst wieder ein, wenn Klein vertrauenswürdig ist; ist der Key möglicherweise bekannt geworden, erzeugst du auf Klein mit `source-guard.sh init-key --new` einen neuen.

### Nach `props-not-applied`

Hier fehlt meist ein Recht in der Delegation, oder jemand hat einen festen Wert von Hand geändert. Solange keine Eigenschaft die Quelle `received` hat, bleiben alle Snapshots erhalten.

**1. Delegation prüfen und korrigieren:**

```bash
SSH_ORIGINAL_COMMAND=check bash /boot/config/twinraid/guard.sh sync
```

Zeigt die Zeile `summary` `deleg=mismatch`, die Delegation wie in [EINRICHTUNG.md](EINRICHTUNG.md), Test K1 (15.3), korrigieren, bis dort `deleg=ok` steht.

**2. Feste Werte setzen.** Der Befehl setzt alle Werte außer `canmount` auf einmal. `N` und `D` aus der [Vorbereitung](#vorbereitung) müssen gesetzt sein:

```bash
zfs set readonly=on exec=off setuid=off devices=off primarycache=metadata secondarycache=all compression=lz4 atime=off xattr=sa acltype=posix recordsize=128K snapdir=hidden sync=standard dedup=off copies=1 checksum=on quota=none refquota=none reservation=none refreservation=none sharesmb=off sharenfs=off twinraid:mirror=1 mountpoint=/mnt/mainpool/twinraid-mirror/$N "$D"
```

**3. Prüfen:** der Befehl `zfs get -H -o property,value,source …` aus [Schritt 4 oben](#nach-received-properties-oder-props-mismatch). Jede Zeile zeigt `local` und den Wert aus der Tabelle, `canmount` noch `noauto`. Meldet `zfs get -H -r -t filesystem,volume,snapshot -s received -o name,property,value all "$D"` etwas, kam eine Eigenschaft aus einem Strom; dann gilt der Weg oben, beginnend bei Schritt 1.

**4. Sperre aufheben** wie in [Schritt 5 oben](#nach-received-properties-oder-props-mismatch).

---

## Notbetrieb starten

Klein ist ausgefallen, und die Container sollen mit dem Stand des letzten Syncs auf Groß laufen. Dafür entsteht je Share ein beschreibbarer **Klon** des neuesten Spiegel-Snapshots als `mainpool/<Share>`, also unter demselben Pfad `/mnt/mainpool/<Share>` wie auf Klein. Der Spiegel selbst bleibt unverändert und schreibgeschützt; solange ein Klon besteht, lehnt `guard.sh` jeden Empfang ab (`clone-active`).

**1. Klein bleibt aus.** Kann Klein von selbst wieder starten (etwa nach einem Stromausfall), Strom- oder Netzwerkkabel ziehen. Sonst liefen dieselben Container gleichzeitig auf beiden Servern ([Grundsatz](#überblick)).

**2. Im Unraid-Terminal von Groß** den Kasten aus [Variablen und Verbindung zu Klein](#variablen-und-verbindung-zu-klein) einfügen (die Zeile `klein` brauchst du hier nicht). Die Schritte 3 bis 6 gelten je Share.

**3. Liegt schon ein Dataset an der Stelle des Klons?** Unraid legt für eine Share-Konfiguration manchmal selbst ein leeres Dataset an (Konzept 3.7, E26). Der Kasten entfernt es nur, wenn es eingehängt und leer ist, keine Snapshots und Kinder hat und kein Klon ist; sonst ändert er nichts:

```bash
if ! zfs list -H -o name "mainpool/$N" >/dev/null 2>&1; then
  echo "mainpool/$N gibt es nicht; weiter mit Schritt 4"
elif [ "$(zfs list -H -t all -r -o name "mainpool/$N" | wc -l)" = 1 ] && [ "$(zfs get -H -o value origin,mounted "mainpool/$N" | tr '\n' ' ')" = "- yes " ] && [ -z "$(ls -A "/mnt/mainpool/$N")" ]; then
  zfs destroy "mainpool/$N" && echo "leeres Dataset mainpool/$N entfernt; weiter mit Schritt 4"
else
  echo "mainpool/$N ist nicht leer, hat Snapshots oder Kinder oder ist ein Klon; nichts gelöscht, hier aufhören"
fi
```

Endet die Ausgabe mit „hier aufhören“, das Dataset erst ansehen (`zfs list -t all -r -o name,used,origin "mainpool/$N"`, `ls -la "/mnt/mainpool/$N"`). Ist es ein Klon aus einem früheren Notbetrieb, gilt [Aufräumen auf Groß](#aufräumen-auf-groß); fremde Daten entfernst du nur von Hand, nachdem du sie gesichert hast.

**4. Klon anlegen.**

> **Diesen Kasten allein einfügen.** Er legt den Klon des neuesten Snapshots an. Ein zweiter Aufruf scheitert nur, weil es `mainpool/<Share>` dann schon gibt.

```bash
B=$(zfs list -H -t snapshot -o name -s createtxg -d 1 "mainpool/twinraid-mirror/$N" | tail -n 1 | cut -d@ -f2)
echo "Neuester Stand: $B"
zfs clone -o readonly=off -o exec=on -o setuid=off -o devices=off -o primarycache=all -o compression=lz4 -o atime=off -o xattr=sa -o acltype=posix "mainpool/twinraid-mirror/$N@$B" "mainpool/$N"
```

Die Eigenschaften stehen ausdrücklich dabei, weil ein Klon sonst von der obersten Ebene des Pools erbt, die Unraid verwaltet.

**5. Klon prüfen.** Die Befehle ändern nichts außer einer Probedatei, die sofort wieder gelöscht wird:

```bash
zfs get -H -o property,value,source readonly,exec,setuid,devices,primarycache,compression,atime,xattr,acltype,origin "mainpool/$N"
findmnt -n -o TARGET,SOURCE,FSTYPE "/mnt/mainpool/$N"
touch "/mnt/mainpool/$N/.twinraid-schreibprobe" && rm "/mnt/mainpool/$N/.twinraid-schreibprobe" && echo "beschreibbar"
zfs get -H -o value readonly "mainpool/twinraid-mirror/$N"
```

| Prüfen | Erwartet |
|---|---|
| `zfs get` am Klon | `readonly off`, `exec on`, `setuid off`, `devices off`, `primarycache all`, `compression lz4`, `atime off`, `xattr sa`, `acltype posix`, jeweils mit Quelle `local`; `origin` ist `mainpool/twinraid-mirror/<Share>@twinraid-…` |
| `findmnt` | `/mnt/mainpool/<Share> mainpool/<Share> zfs` |
| Probedatei | `beschreibbar` |
| letzte Zeile | `on`: Der Spiegel bleibt schreibgeschützt |

**6. Share in Unraid anlegen** (Unraid-Oberfläche von Groß): Reiter **Shares** → **Add Share** → Name genau wie auf Klein, **Primary storage** `mainpool`, **Secondary storage** `None`, bei *SMB Security Settings* dieselbe Freigabe wie auf Klein → **Add Share**. Unraid übernimmt das vorhandene Dataset. Kontrolle im Unraid-Terminal von Groß:

```bash
zfs get -H -o value origin "mainpool/$N"
[ -e "/boot/config/shares/$N.cfg" ] && echo "Share-Konfiguration vorhanden" || echo "Share-Konfiguration fehlt"
```

Erwartet: wieder `mainpool/twinraid-mirror/<Share>@twinraid-…` (es ist noch der Klon) und `Share-Konfiguration vorhanden`.

**7. setuid und Gerätedateien** bleiben im Klon aus: Images liegen nicht in den Shares, und durchgereichte Geräte kommen aus `/dev` des Hosts. Hat die Suche aus [Vor dem ersten echten Spiegel von appdata](#vor-dem-ersten-echten-spiegel-von-appdata) Dateien gezeigt, die ein Container braucht, für diese Share `zfs set setuid=on "mainpool/$N"`.

**8. Docker auf Groß einschalten:** *Settings → Docker → Enable Docker: Yes* → **Apply**. Im Normalbetrieb ist Docker auf Groß aus. Fehlt das Docker-Abbild (`docker.img`), legt Unraid ein neues an; die Images lädt Docker dann beim Anlegen der Container neu, dafür braucht Groß Internet.

**9. Container anlegen:** bis Version 0.5.4 von Hand (Reiter **Docker** → **Add Container**), mit demselben Image, denselben Pfaden unter `/mnt/user/<Share>/…` und denselben Netzwerkeinstellungen wie auf Klein. Feste IP und MAC dürfen gleich sein, weil Klein aus ist. TwinRaid selbst gehört nicht dazu.

Zur Kontrolle meldet `SSH_ORIGINAL_COMMAND=check bash /boot/config/twinraid/guard.sh sync` in der Zeile `summary` jetzt `clones=1` (je Klon eins mehr); ein Sync wäre gesperrt. Der Notbetrieb läuft, bis Klein wieder da ist.

---

## Rückweg nach Klein

Klein läuft wieder, und der Stand aus dem Notbetrieb soll zurück nach Klein. Der Weg sendet die Änderungen des Klons seit der gemeinsamen Basis mit `zfs receive -F` nach Klein. **Das ist die einzige Stelle mit `-F`.** Sie läuft nur von Hand, aus dem Unraid-Terminal von Groß über deinen Admin-Zugang; TwinRaid, der Wächter und `guard.sh` haben dafür keinen Befehl.

> **Was `-F` auf Klein tut:** Es setzt `mainpool/<Share>` auf Klein auf die gemeinsame Basis zurück und löscht dabei alle Snapshots auf Klein, die neuer sind als die Basis. Was auf Klein nach der Basis geschrieben wurde (etwa kurz vor dem Ausfall) und diese neueren Snapshots **gehen verloren**. Basis und Lesezeichen bleiben. Hat einer der neueren Snapshots einen Hold oder einen Klon, scheitert der Empfang, und nichts ändert sich. Schritt 5 prüft das vorher.

**1. Auf Groß den Stand festhalten.** Reiter **Docker** → alle Container stoppen (**Stop All**). Dann im Unraid-Terminal von Groß, nach dem Kasten aus [Variablen und Verbindung zu Klein](#variablen-und-verbindung-zu-klein):

> **Diesen Kasten allein einfügen.** Er legt einen Snapshot des Klons an; ab hier ändert sich auf Groß nichts mehr.

```bash
zfs snapshot "mainpool/$N@notbetrieb-$(date -u +%Y%m%dT%H%M%SZ)"
zfs list -H -t snapshot -o name -s createtxg -d 1 "mainpool/$N"
```

Erwartet: genau ein Snapshot `mainpool/<Share>@notbetrieb-<Datum>T<Zeit>Z` (oder mehrere, wenn du den Kasten öfter eingefügt hast; es zählt der unterste).

**2. Klein ohne Netzwerk starten.** Das Netzwerkkabel von Klein ziehen, **bevor** es startet. Unraid startet beim Hochfahren alle Container mit Autostart, auch TwinRaid. Mit dem alten Stand würden sie sonst Daten nach außen übertragen, etwa ein Sync-Client alte Dateien hochladen; das macht kein Rückweg rückgängig. Für die nächsten Schritte brauchst du Bildschirm und Tastatur an Klein: im Startmenü **Unraid OS GUI Mode** wählen, dann ist die Unraid-Oberfläche direkt an Klein erreichbar.

**3. Docker auf Klein ausschalten:** *Settings → Docker → Enable Docker: No* → **Apply**. Das hält alle Container an, auch TwinRaid; während des Rückwegs gibt es also keinen Sync. Ohne GUI Mode geht es an der Konsole von Klein mit `/etc/rc.d/rc.docker stop`; `docker ps` meldet danach, dass es den Docker-Daemon nicht erreicht. **Erst danach das Netzwerkkabel wieder einstecken.** Bis Schritt 7 nichts in die Share auf Klein schreiben, auch nicht über eine Freigabe im Netz; es ginge in Schritt 6 verloren.

**4. Werte für den Rückweg**, im Unraid-Terminal von Groß, nach dem Kasten aus [Variablen und Verbindung zu Klein](#variablen-und-verbindung-zu-klein). Ändert nichts; nach einem neuen Terminal diesen Kasten wiederholen:

```bash
B=$(zfs get -H -o value origin "mainpool/$N" | cut -d@ -f2)
R=$(zfs list -H -t snapshot -o name -s createtxg -d 1 "mainpool/$N" | tail -n 1 | cut -d@ -f2)
echo "Basis: $B, Stand aus dem Notbetrieb: $R"
klein zfs list -H -o name "mainpool/$N"
```

Erwartet: `Basis: twinraid-…, Stand aus dem Notbetrieb: notbetrieb-…` und in der letzten Zeile `mainpool/<Share>` von Klein. Bei der ersten Verbindung fragt `ssh` nach dem Hostkey ([Variablen und Verbindung zu Klein](#variablen-und-verbindung-zu-klein)).

**5. Prüfungen vor dem Senden.** Die Befehle ändern nichts.

*a) Gemeinsame Basis:* Der Snapshot, aus dem der Klon stammt, muss auf Klein mit derselben GUID liegen:

```bash
zfs get -H -o value guid "mainpool/twinraid-mirror/$N@$B"
klein zfs get -H -o value guid "mainpool/$N@$B"
```

Erwartet: zweimal dieselbe Zahl. Meldet Klein `dataset does not exist` oder eine andere Zahl, gibt es keine gemeinsame Basis: **hier aufhören.** Ein inkrementeller Rückweg geht dann nicht; die Daten bleiben im Klon auf Groß sicher, und der Weg von dort ist nicht Teil dieser Anleitung.

*b) Neuere Snapshots auf Klein* ohne Hold und ohne Klon:

```bash
klein zfs list -H -t snapshot -o name,userrefs,clones -s createtxg -d 1 "mainpool/$N" | awk -v b="mainpool/$N@$B" 'f { print "wird gelöscht: " $0; if ($2 != 0 || $3 != "-") bad = 1 } $1 == b { f = 1 } END { if (!f) print "Basis fehlt auf Klein: nicht weitermachen"; else if (bad) print "Hold oder Klon an einem neueren Snapshot: nicht weitermachen"; else print "Prüfung in Ordnung" }'
```

Erwartet: je neuerem Snapshot eine Zeile `wird gelöscht: …` mit `0` und `-` (oder keine), am Ende `Prüfung in Ordnung`. Sonst **hier aufhören**: einen Hold zeigt `klein zfs holds "<Snapshot>"`; einen Klon auf Klein erst ansehen.

*c) Was auf Klein nach der Basis entstand:*

```bash
klein zfs diff -H "mainpool/$N@$B" "mainpool/$N" | wc -l
klein zfs diff -H "mainpool/$N@$B" "mainpool/$N" | head -n 50
```

Die erste Zahl ist die Anzahl geänderter Einträge seit der Basis, darunter die ersten 50 mit `-` (gelöscht), `+` (neu), `M` (geändert) und `R` (umbenannt). Alles davon geht im nächsten Schritt verloren, ebenso der Inhalt der neueren Snapshots aus b). **Was du brauchst, jetzt sichern**, im Unraid-Terminal von Klein mit `cp -a` an einen Ort außerhalb von `/mnt/mainpool/<Share>`, aus neueren Snapshots unter `/mnt/mainpool/<Share>/.zfs/snapshot/<Name>/`.

**6. Stand nach Klein senden.**

Gesendet wird von einem **Lesezeichen der Basis im Klon** (`mainpool/<Share>#<Basis>`). Ein Strom direkt vom Snapshot des Spiegels wäre für ZFS ein Strom eines Klons, weil die Basis in einem anderen Dataset liegt; den nimmt Klein nur als neues Dataset an (`destination has snapshots … must destroy them`). Das Lesezeichen gehört zum Klon und hat dieselbe GUID wie die Basis; der Spiegel ändert sich dabei nicht, und das Lesezeichen verschwindet beim Aufräumen mit dem Klon.

> **Diesen Kasten allein einfügen, und nur, wenn Schritt 5 in Ordnung war.** Er legt im Klon das Lesezeichen der Basis an, falls es fehlt, und setzt `mainpool/<Share>` auf Klein mit `zfs receive -F` auf den Stand aus dem Notbetrieb. Er prüft die Basis unmittelbar vorher noch einmal und sendet sonst nichts.

```bash
zfs list -H -t bookmark -o name "mainpool/$N#$B" >/dev/null 2>&1 || zfs bookmark "mainpool/twinraid-mirror/$N@$B" "mainpool/$N#$B" || true
GB=$(zfs get -H -o value guid "mainpool/$N#$B" 2>/dev/null || true)
GK=$(klein zfs get -H -o value guid "mainpool/$N@$B" 2>/dev/null || true)
if [ -n "$R" ] && [ -n "$GB" ] && [ "$GB" = "$GK" ]; then
  zfs send -L -c -e -i "mainpool/$N#$B" "mainpool/$N@$R" | klein zfs receive -F "mainpool/$N" && echo "Rückweg gesendet"
else
  echo "Basis fehlt auf Klein oder hat eine andere GUID; nichts gesendet"
fi
```

Erwartet: `Rückweg gesendet`. Meldet `zfs receive` `dataset is busy`, greift auf Klein noch etwas auf die Share zu (etwa ein Rechner über eine Freigabe); den Zugriff beenden und den Kasten wiederholen. Danach kontrollieren:

```bash
zfs get -H -o value guid "mainpool/$N@$R"
klein zfs get -H -o value guid "mainpool/$N@$R"
klein zfs list -H -t snapshot -o name,userrefs -s createtxg -d 1 "mainpool/$N"
klein zfs list -H -t bookmark -o name -d 1 "mainpool/$N"
```

| Prüfen | Erwartet |
|---|---|
| die zwei GUIDs | dieselbe Zahl: Klein hat genau den Stand aus dem Notbetrieb |
| Snapshots auf Klein | die Basis `@twinraid-…` mit `userrefs` `1` (Hold des Wächters), darunter nur noch `@notbetrieb-…`; die neueren aus Schritt 5 b) sind weg |
| Lesezeichen auf Klein | dieselben wie vorher, darunter `#` mit dem Namen der Basis |

**7. Docker auf Klein wieder einschalten:** *Settings → Docker → Enable Docker: Yes* → **Apply** (nach der Konsole aus Schritt 3: `/etc/rc.d/rc.docker start`). Die Container starten mit den Daten aus dem Notbetrieb, und TwinRaid läuft wieder. Bis zum Ende von [Aufräumen auf Groß](#aufräumen-auf-groß) keinen Sync starten; Groß lehnt ihn ohnehin ab (`clone-active`).

---

## Aufräumen auf Groß

Nach dem Rückweg kommen die Klone weg; die Spiegel bleiben unverändert. Danach läuft der Sync mit derselben Basis weiter.

**1. Docker auf Groß ausschalten:** *Settings → Docker → Enable Docker: No* → **Apply**. Im Normalbetrieb ist Docker auf Groß aus (Konzept 2).

**2. Klon löschen**, im Unraid-Terminal von Groß, nach dem Kasten aus [Variablen und Verbindung zu Klein](#variablen-und-verbindung-zu-klein):

> **Diesen Kasten allein einfügen.** Er löscht `mainpool/<Share>` mit seinen Snapshots und Lesezeichen einzeln und ohne `-r`, aber nur, wenn es ein Klon eines Spiegels ist. Den Spiegel `mainpool/twinraid-mirror/<Share>` berührt er nie.

```bash
O=$(zfs get -H -o value origin "mainpool/$N" 2>/dev/null || true)
case "$O" in
  "mainpool/twinraid-mirror/$N@"*)
    for s in $(zfs list -H -t snapshot,bookmark -o name -d 1 "mainpool/$N"); do zfs destroy "$s"; done
    zfs destroy "mainpool/$N" && echo "Klon mainpool/$N gelöscht"
    ;;
  *) echo "mainpool/$N ist kein Klon eines Spiegels; nichts gelöscht" ;;
esac
```

Erwartet: `Klon mainpool/<Share> gelöscht`. Meldet `zfs` `dataset is busy`, greift noch etwas darauf zu, meist eine Freigabe: in Unraid bei der Share *Export* auf *No* stellen und den Kasten wiederholen.

**3. Share in Unraid löschen** (Unraid-Oberfläche von Groß): Reiter **Shares** → die Share anklicken → **Delete** ankreuzen → **Delete**. Das geht, weil sie jetzt leer ist. Im Normalbetrieb gibt es auf Groß keine Share-Konfiguration mit dem Namen einer gespiegelten Share (Konzept 2, E26).

**4. Kontrolle:**

```bash
zfs list -H -o name "mainpool/$N" 2>/dev/null || echo "kein Dataset mainpool/$N"
[ -e "/boot/config/shares/$N.cfg" ] && echo "Share-Konfiguration noch da" || echo "keine Share-Konfiguration"
zfs list -H -t snapshot -o name,clones -d 1 "mainpool/twinraid-mirror/$N"
```

| Prüfen | Erwartet |
|---|---|
| erste Zeile | `kein Dataset mainpool/<Share>`. Hat Unraid inzwischen ein leeres Dataset angelegt, mit dem Kasten aus [Notbetrieb starten](#notbetrieb-starten), Schritt 3, entfernen |
| zweite Zeile | `keine Share-Konfiguration` |
| Snapshots des Spiegels | in der zweiten Spalte überall `-`: kein Klon mehr; Anzahl und Namen wie vor dem Notbetrieb |

Zusätzlich meldet `SSH_ORIGINAL_COMMAND=check bash /boot/config/twinraid/guard.sh sync` in der Zeile `summary` `clones=0`.

**5. Danach:** In TwinRaid **Nur Probelauf**, dann **Jetzt synchronisieren**. Der Sync sendet inkrementell von derselben Basis, was sich im Notbetrieb geändert hat. Der Snapshot `@notbetrieb-…` bleibt auf Klein liegen; der Wächter löscht ihn nie, weil er ihn nicht selbst angelegt hat. Nach dem nächsten erfolgreichen Sync kannst du ihn im Unraid-Terminal von Klein mit `zfs destroy "mainpool/<Share>@notbetrieb-…"` löschen (ohne `-r`).

**6. Docker-Abbild auf Groß:** Das `docker.img` aus Schritt 8 des Notbetriebs bleibt auf dem Pool für `system`. Es stört nicht; wer es nicht behalten will, löscht es bei ausgeschaltetem Docker unter *Settings → Docker*.

---

## Vor dem ersten echten Spiegel von appdata

Im Klon bleiben `setuid` und Gerätedateien ausgeschaltet ([Notbetrieb starten](#notbetrieb-starten), Schritt 7). Bevor `appdata` zum ersten Mal gespiegelt wird, im **Unraid-Terminal von Klein** nachsehen, ob es dort solche Dateien gibt:

```bash
find /mnt/mainpool/appdata -xdev \( -perm -4000 -o -perm -2000 -o -type b -o -type c \) -print | wc -l
find /mnt/mainpool/appdata -xdev \( -perm -4000 -o -perm -2000 -o -type b -o -type c \) -print | head -n 50
```

Erwartet: `0` und keine Pfade. Zeigt die Liste Dateien, gehören sie meist zu einem Container (etwa ein Programm mit setuid in seinen Daten). Die Pfade enthalten Namen deiner Container; sie gehören nicht in einen PR. Notiere, welcher Container sie braucht; im Notbetrieb schaltest du für seine Share `setuid` am Klon ein (Schritt 7). Gerätedateien in `appdata` braucht ein Container praktisch nie, denn durchgereichte Geräte kommen aus `/dev` des Hosts.
