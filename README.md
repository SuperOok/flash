# flash

Interaktives Terminal-Tool, um SD-Karten und USB-Sticks zu prüfen, zu testen und
Daten zu retten.

Gerät einstecken, in der Liste auswählen und einen Ablauf wählen:

| Ablauf | Schreibt? | Was passiert |
|---|---|---|
| Schnell-Check | nein | 100 Stichproben übers ganze Gerät, Lesegeschwindigkeit, `fsck -n`, Kernel-Log |
| Kompletter Lesetest | nein | `badblocks` liest jeden Block |
| Daten retten | nein | `ddrescue` erstellt ein Image (2 Durchläufe), danach Dateien daraus wiederherstellen |
| Dateien direkt wiederherstellen | nein | PhotoRec direkt auf dem Gerät |
| Aushängen & sicher entfernen | nein | Partitionen aushängen, `sync`, Gerät abschalten (auch ohne Root, Taste `a`) |
| Intensiv-Check | **ja, alles** | `f3probe` (Fake-Kapazität) + `badblocks -w` mit Zufallsmuster |
| Intensiv-Check gründlich | **ja, alles** | wie oben, aber 4 Testmuster |

Systemplatten werden nie angezeigt. Destruktive Tests verlangen die Eingabe des
Gerätenamens.

### Sicherung öffnen (Taste `b`)

Holt Dateien aus einem gesicherten Abbild (`*.img`, z. B. aus „Daten retten“) in
drei Stufen – die nächste nur, wenn die vorige scheitert:

| Stufe | Original-Abbild | Was passiert |
|---|---|---|
| 1. Dateien wiederherstellen | unverändert | schreibgeschützt verbinden (`losetup -r`), nur lesend einhängen, alles kopieren; scheitert das Einhängen, prüft `fsck -n` |
| 2. Reparieren und wiederherstellen | unverändert | Kopie des Abbilds anlegen, **die Kopie** mit `fsck` reparieren, dann kopieren – nur nach ausdrücklicher Warnung, weil die Reparatur Ordner und Dateien zerstören kann |
| 3. Dateien suchen (PhotoRec) | unverändert | PhotoRec liest das Abbild und findet Dateien am Inhalt, ohne Namen und Ordner |

Gefunden werden Abbilder unter `~/flash-rescue`; andere Orte (z. B.
`/mnt/data/flash-rescue`) einfach als Pfad eingeben.

## Installation

```sh
sudo apt install python3-venv gddrescue f3 testdisk e2fsprogs dosfstools exfatprogs
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
ln -s "$PWD/bin/flash" ~/.local/bin/flash
```

## Benutzung

```sh
flash
```

`bin/flash` startet sich selbst über `sudo`, weil Blockgeräte nur mit Root-Rechten
lesbar sind.

Bedienung: Pfeiltasten und Enter, `Esc` zurück bzw. Abbrechen, `a` aushängen,
`b` Sicherung öffnen, `r` aktualisieren, `q` beenden. In Pfadfelder lässt sich mit
`Strg+V` (System-Zwischenablage, braucht `xclip`, `xsel` oder `wl-paste`) oder
`Strg+Umschalt+V` (Terminal) einfügen; ein eingefügter Pfad ersetzt die Vorgabe. Die Geräteliste aktualisiert sich beim Ein- und Ausstecken selbst.

## Entwicklung

```sh
.venv/bin/python -m pytest -q
```
