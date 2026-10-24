# AGENTS.md

`flash` ist eine interaktive Terminal-Oberfläche (Python + [Textual](https://textual.textualize.io/))
zum Prüfen, Testen und Retten von SD-Karten und USB-Sticks unter Linux (Ubuntu).

## Aufbau

- `src/flash/devices.py` – Geräteerkennung per `lsblk -J`; nur Wechseldatenträger,
  Systemplatten werden nie angeboten
- `src/flash/jobs.py` – Abläufe (`Job`) aus Schritten: `CommandStep` (externes Programm,
  Fortschritt per Regex) oder `PythonStep` (Funktion, läuft im Thread)
- `src/flash/runner.py` – führt Schritte aus, verteilt Ausgaben auf Log/Status/Fortschritt;
  unabhängig von der Oberfläche testbar
- `src/flash/app.py` – Textual-App: Geräteliste → Aktionsmenü → (Bestätigung) → Laufansicht
- `bin/flash` – Startskript, ruft sich bei Bedarf über `sudo` mit der venv auf
- `tests/` – pytest; `test_app.py` steuert die Oberfläche mit Textuals Pilot

## Entwicklung

- venv: `python3 -m venv .venv` und `.venv/bin/pip install -e '.[dev]'`
  (braucht das apt-Paket `python3-venv`).
- Tests: `.venv/bin/python -m pytest -q` – vor jedem Commit.
- Oberfläche ohne Root ansehen: `.venv/bin/python -m flash` (Liste geht, Prüfungen nicht).
- Ein Agent kann `bin/flash` nicht sinnvoll starten (sudo-Passwort, echtes Gerät nötig).
  Abläufe lieber in Tests gegen Image-Dateien prüfen (`tmp_path`) und den Benutzer
  bitten, mit echter Hardware zu testen.
- Screenshots für Layout-Kontrolle: `pilot.app.save_screenshot()` in `run_test()` (SVG).

## Sicherheitsregeln (nicht aufweichen)

- Destruktive Abläufe (`Job.destructive`) nur nach Eingabe des Gerätenamens im
  `ConfirmScreen`; danach Gerät per `find_device(identity)` neu einlesen, damit kein
  zwischenzeitlich getauschter Stick überschrieben wird.
- Geräte mit Systemmountpunkten (`SYSTEM_MOUNTS`) filtert `parse_lsblk` heraus –
  auch wenn sie per USB hängen.
- Lesende Abläufe dürfen nichts schreiben: kein `-w` bei badblocks, fsck nur mit `-n`.
  `test_nur_intensiv_check_ist_destruktiv` sichert das ab.
- Rettungsziel nie auf dem Quellgerät; Images landen standardmäßig in
  `~<SUDO_USER>/flash-rescue/` und werden per `chown_to_user` zurückgegeben.

## Fallstricke

- System-Locale ist Deutsch. Externe Programme laufen mit `LC_ALL=C.UTF-8`
  (`runner.COMMAND_ENV`), damit die Regexe auf englische Ausgaben passen.
- badblocks überschreibt Fortschrittszeilen mit `\b`, ddrescue mit `\r` und ANSI-Escapes –
  `runner.LINE_SPLIT`/`ANSI` behandeln das; neue Tools ggf. dort ergänzen.
- Textual: UI-Änderungen aus Threads nur über `call_from_thread` (`RunScreen._ui`).
  `check_action` → `False` blendet eine Taste aus, `None` zeigt sie ausgegraut.
- Interaktive Programme (PhotoRec) laufen in `app.suspend()` im echten Terminal.

## Konventionen

- Kommentare, Oberflächentexte und Doku auf Deutsch; Testnamen ebenso.
- Farben über die Konstanten in `app.py` (`ACCENT`, `OK`, `WARN`, `ERROR`), keine
  ANSI-Farbnamen im Markup.
- Fremdtexte (Gerätenamen, Programmausgaben) vor dem Einsetzen in Rich-Markup mit
  `rich.markup.escape` maskieren.
- Keine Geheimnisse, Seriennummern echter Geräte oder Hostdetails einchecken.
