# TwinRaid: Wiederherstellung

> **Stand: Version 0.5.1.** Dieses Dokument beschreibt bisher nur den Abschnitt „Spiegel gesperrt“. Die Anleitung für den Notbetrieb (letzten Stand finden, einzelne Dateien zurückholen, Notbetrieb starten, Rückweg nach Klein) folgt mit Version 0.5.3.

Die Grundlagen stehen in [KONZEPT.md](KONZEPT.md), Abschnitte 3.7 und 4.6. TwinRaid hat für die Schritte hier keine Funktion; sie laufen von Hand als root im **Unraid-Terminal von Groß**.

## Inhalt

- [Spiegel gesperrt](#spiegel-gesperrt)
  - [Woran du es erkennst](#woran-du-es-erkennst)
  - [Vorbereitung](#vorbereitung)
  - [Nach `received-properties` oder `props-mismatch`](#nach-received-properties-oder-props-mismatch)
  - [Nach `props-not-applied`](#nach-props-not-applied)

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
