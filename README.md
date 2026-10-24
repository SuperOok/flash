# flash

Interaktives Terminal-Tool, um SD-Karten und USB-Sticks zu prüfen, zu testen und
Daten zu retten.

Gerät einstecken, in der Liste auswählen und einen Ablauf wählen:

| Ablauf | Schreibt? | Was passiert |
|---|---|---|
| Schnell-Check | nein | 100 Stichproben übers ganze Gerät, Lesegeschwindigkeit, `fsck -n`, Kernel-Log |
| Kompletter Lesetest | nein | `badblocks` liest jeden Block |
| Daten retten | nein | `ddrescue` erstellt ein Image (2 Durchläufe), danach optional PhotoRec |
| Dateien direkt wiederherstellen | nein | PhotoRec direkt auf dem Gerät |
| Intensiv-Check | **ja, alles** | `f3probe` (Fake-Kapazität) + `badblocks -w` mit Zufallsmuster |
| Intensiv-Check gründlich | **ja, alles** | wie oben, aber 4 Testmuster |

Systemplatten werden nie angezeigt. Destruktive Tests verlangen die Eingabe des
Gerätenamens.

## Installation

```sh
sudo apt install gddrescue f3 testdisk e2fsprogs dosfstools exfatprogs
python3 -m venv --without-pip .venv
python3 -m pip --python .venv/bin/python install -e '.[dev]'
ln -s "$PWD/bin/flash" ~/.local/bin/flash
```

## Benutzung

```sh
flash
```

`bin/flash` startet sich selbst über `sudo`, weil Blockgeräte nur mit Root-Rechten
lesbar sind.

Bedienung: Pfeiltasten und Enter, `Esc` zurück bzw. Abbrechen, `r` aktualisieren,
`q` beenden. Die Geräteliste aktualisiert sich beim Ein- und Ausstecken selbst.

## Entwicklung

```sh
.venv/bin/python -m pytest -q
```
