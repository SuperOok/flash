"""Interaktive Oberfläche auf Basis von Textual."""

from __future__ import annotations

import os
import subprocess
import threading
import time
from pathlib import Path

from rich.markup import escape
from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen, Screen
from textual.theme import Theme
from textual.widgets import DataTable, Footer, Input, OptionList, ProgressBar, RichLog, Static
from textual.widgets.option_list import Option

from . import images, jobs
from .devices import Device, find_device, human_size, list_devices
from .jobs import CommandStep, Job, PythonStep, StepContext
from .runner import run_command, run_python

ACCENT = "#D97757"
OK = "#98C379"
WARN = "#E5B567"
ERROR = "#E06C75"
SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
BANNER = (
    f"[b {ACCENT}]⚡ flash[/]  "
    "[dim]SD-Karten und USB-Sticks prüfen, testen und Daten retten[/]"
)

# (id, Titel, Beschreibung, Art: "read" nur lesend, "eject" aushängen, "write" überschreibt)
ACTIONS = [
    ("quick", "Schnell-Check", "Stichproben, Geschwindigkeit, Dateisystem – ca. 1 Minute", "read"),
    ("full", "Kompletter Lesetest", "liest jeden Block (badblocks) – je nach Größe lange", "read"),
    ("rescue", "Daten retten", "Image mit ddrescue sichern, danach PhotoRec anbieten", "read"),
    ("photorec", "Dateien direkt wiederherstellen", "PhotoRec direkt auf dem Gerät", "read"),
    ("eject", "Aushängen & sicher entfernen", "Partitionen aushängen, Gerät abschalten – danach abziehen", "eject"),
    ("intensive", "Intensiv-Check", "Fake-Erkennung + Schreib-/Lesetest mit Zufallsmuster", "write"),
    ("thorough", "Intensiv-Check gründlich", "4 Testmuster – sehr langsam, aber gründlich", "write"),
]

# Stufen der Wiederherstellung aus einem Sicherungs-Abbild; "copy" arbeitet an einer Kopie.
IMAGE_ACTIONS = [
    ("files", "1. Dateien wiederherstellen", "Abbild schreibgeschützt einhängen und alle Dateien kopieren", "read"),
    ("repair", "2. Reparieren und wiederherstellen", "falls 1. scheitert: Kopie des Abbilds mit fsck reparieren, dann kopieren", "copy"),
    ("imgphotorec", "3. Dateien suchen (PhotoRec)", "letzter Ausweg: findet Dateien am Inhalt – ohne Namen und Ordner", "read"),
]

# Aktionen, die auch ohne Root-Rechte funktionieren (udisksctl fragt per polkit).
NO_ROOT_ACTIONS = ("eject",)

FLASH_THEME = Theme(
    name="flash",
    primary=ACCENT,
    secondary="#8FA8C8",
    accent=ACCENT,
    warning=WARN,
    error=ERROR,
    success=OK,
    background="#161616",
    surface="#1F1F1F",
    panel="#2A2A2A",
    dark=True,
)


def device_status(device: Device) -> Text:
    if device.readonly:
        return Text("schreibgeschützt", style=WARN)
    if device.is_mounted:
        return Text("eingehängt", style=WARN)
    return Text("bereit", style=OK)


def device_summary(device: Device) -> str:
    return (
        f"[b]{escape(device.title)}[/]  [dim]{device.path} · {device.kind} · "
        f"{human_size(device.size)}[/]"
    )


class ActionScreen(ModalScreen[str | None]):
    BINDINGS = [Binding("escape", "close", "Zurück")]

    def __init__(self, device: Device, actions: list = ACTIONS) -> None:
        super().__init__()
        self.device = device
        self.actions = actions

    def compose(self) -> ComposeResult:
        options = []
        for key, title, description, kind in self.actions:
            if kind == "write":
                prompt = f"[b {ERROR}]{title}[/]  [{ERROR}]überschreibt alles[/]\n  [dim]{description}[/]"
            elif kind == "eject":
                tag = f"[{WARN}]eingehängt[/]" if self.device.is_mounted else "[dim]nicht eingehängt[/]"
                prompt = f"[b]{title}[/]  {tag}\n  [dim]{description}[/]"
            elif kind == "copy":
                prompt = f"[b]{title}[/]  [{WARN}]Original bleibt unverändert[/]\n  [dim]{description}[/]"
            else:
                prompt = f"[b]{title}[/]  [{OK}]nur lesend[/]\n  [dim]{description}[/]"
            options.append(Option(prompt, id=key))
        with Vertical(classes="dialog"):
            yield Static(device_summary(self.device), classes="dialog-title")
            yield OptionList(*options)
            if os.geteuid() != 0:
                yield Static(
                    f"[{WARN}]Ohne Root-Rechte sind keine Prüfungen möglich (Aushängen geht) – "
                    "mit [b]bin/flash[/] starten.[/]",
                    classes="dialog-hint",
                )

    @on(OptionList.OptionSelected)
    def chosen(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(event.option_id)

    def action_close(self) -> None:
        self.dismiss(None)


class PathScreen(ModalScreen[Path | None]):
    BINDINGS = [Binding("escape", "close", "Zurück")]

    def __init__(
        self,
        default: Path,
        title: str = "Zielverzeichnis für gerettete Daten",
        hint: str = "Das Image braucht so viel Platz wie das ganze Gerät. "
        "Nicht auf dem zu rettenden Gerät speichern!",
    ) -> None:
        super().__init__()
        self.default = default
        self.title_text = title
        self.hint = hint

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Static(f"[b]{escape(self.title_text)}[/]", classes="dialog-title")
            yield Static(f"[dim]{escape(self.hint)}[/]")
            yield Input(value=str(self.default))

    @on(Input.Submitted)
    def submitted(self, event: Input.Submitted) -> None:
        if event.value.strip():
            self.dismiss(Path(event.value.strip()).expanduser())

    def action_close(self) -> None:
        self.dismiss(None)


class ImageScreen(ModalScreen[Path | None]):
    """Auswahl eines Sicherungs-Abbilds: gefundene Abbilder oder eigener Pfad."""

    BINDINGS = [Binding("escape", "close", "Zurück")]

    def __init__(self, bases: list[Path]) -> None:
        super().__init__()
        self.bases = bases
        self.images: list[Path] = []

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Static("[b]Sicherung öffnen[/]", classes="dialog-title")
            yield OptionList()
            yield Static(
                "[dim]Anderes Abbild oder Ordner mit Abbildern (z. B. /mnt/data/flash-rescue):[/]",
                classes="dialog-hint",
            )
            yield Input(value=str(self.bases[0]) if self.bases else "")

    def on_mount(self) -> None:
        self.show_images(images.find_images(*self.bases))

    def show_images(self, found: list[Path]) -> None:
        self.images = found
        options = self.query_one(OptionList)
        options.clear_options()
        for i, image in enumerate(found):
            stamp = time.strftime("%d.%m.%Y %H:%M", time.localtime(image.stat().st_mtime))
            options.add_option(
                Option(
                    f"[b]{escape(image.name)}[/]  [dim]{human_size(image.stat().st_size)} · {stamp}[/]\n"
                    f"  [dim]{escape(str(image.parent))}[/]",
                    id=str(i),
                )
            )
        options.display = bool(found)
        if found:
            options.highlighted = 0
            options.focus()

    @on(OptionList.OptionSelected)
    def chosen(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(self.images[int(event.option_id)])

    @on(Input.Submitted)
    def submitted(self, event: Input.Submitted) -> None:
        path = Path(event.value.strip()).expanduser()
        if path.is_file():
            self.dismiss(path)
        elif path.is_dir():
            found = images.find_images(path)
            if not found:
                self.notify("Keine Abbilder (*.img) in diesem Ordner gefunden.", severity="warning")
            self.show_images(found)
        else:
            self.notify("Pfad nicht gefunden.", severity="error")

    def action_close(self) -> None:
        self.dismiss(None)


class WarnScreen(ModalScreen[bool]):
    """Ausdrückliche Warnung vor einem riskanten, aber nicht zerstörenden Schritt."""

    BINDINGS = [
        Binding("enter", "ok", "Fortfahren"),
        Binding("escape", "close", "Abbrechen"),
    ]

    def __init__(self, title: str, lines: list[str]) -> None:
        super().__init__()
        self.title_text = title
        self.lines = lines

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog danger"):
            yield Static(f"[b {WARN}]⚠ {escape(self.title_text)}[/]", classes="dialog-title")
            for line in self.lines:
                yield Static(line)
            yield Static(f"\n[b {ACCENT}]Enter[/] fortfahren · [b {ACCENT}]Esc[/] abbrechen")

    def action_ok(self) -> None:
        self.dismiss(True)

    def action_close(self) -> None:
        self.dismiss(False)


class ConfirmScreen(ModalScreen[bool]):
    BINDINGS = [Binding("escape", "close", "Abbrechen")]

    def __init__(self, device: Device) -> None:
        super().__init__()
        self.device = device

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog danger"):
            yield Static(f"[b {ERROR}]⚠ Alle Daten werden unwiderruflich überschrieben[/]", classes="dialog-title")
            yield Static(device_summary(self.device))
            if self.device.partitions:
                parts = ", ".join(
                    f"{p.name} ({p.label or p.fstype or '?'})" for p in self.device.partitions
                )
                yield Static(f"[dim]Partitionen: {escape(parts)}[/]")
            yield Static(f"\nZum Bestätigen den Gerätenamen [b]{self.device.name}[/] eingeben:")
            yield Input(placeholder=self.device.name)

    @on(Input.Submitted)
    def submitted(self, event: Input.Submitted) -> None:
        if event.value.strip() == self.device.name:
            self.dismiss(True)
        else:
            event.input.clear()
            self.notify("Eingabe stimmt nicht mit dem Gerätenamen überein.", severity="error")

    def action_close(self) -> None:
        self.dismiss(False)


class RunScreen(Screen):
    BINDINGS = [
        Binding("escape", "close", "Abbrechen / Zurück"),
        Binding("p", "followup", "Weiter"),
    ]

    def __init__(self, job: Job) -> None:
        super().__init__()
        self.job = job
        self.states = ["pending"] * len(job.steps)
        self.ctx: StepContext | None = None
        self.running = True
        self.cancel_requested = False
        self.failures: list[tuple[str, str]] = []
        self.spin = 0
        self.main_thread = threading.get_ident()

    def compose(self) -> ComposeResult:
        style = "red" if self.job.destructive else ACCENT
        yield Static(
            f"[b {style}]{escape(self.job.title)}[/]\n{device_summary(self.job.device)}",
            id="job-title",
        )
        with Horizontal(id="run"):
            with Vertical(id="steps"):
                for i, step in enumerate(self.job.steps):
                    yield Static(id=f"step-{i}", classes="step")
            with Vertical(id="output"):
                yield ProgressBar(total=None, show_eta=True, id="progress")
                yield Static("", id="status")
                yield RichLog(markup=True, wrap=True, id="log")
        yield Static("", id="result")
        yield Footer()

    def on_mount(self) -> None:
        for i in range(len(self.job.steps)):
            self.render_step(i)
        self.set_interval(0.1, self.tick)
        self.execute()

    # --- Anzeige -----------------------------------------------------------

    def render_step(self, index: int) -> None:
        state = self.states[index]
        icon = {
            "pending": "[dim]○[/]",
            "running": f"[{ACCENT}]{SPINNER[self.spin % len(SPINNER)]}[/]",
            "ok": f"[{OK}]✔[/]",
            "failed": f"[{ERROR}]✘[/]",
            "skipped": "[dim]–[/]",
        }[state]
        title = escape(self.job.steps[index].title)
        if state in ("pending", "skipped"):
            title = f"[dim]{title}[/]"
        elif state == "running":
            title = f"[b]{title}[/]"
        self.query_one(f"#step-{index}", Static).update(f"{icon} {title}")

    def tick(self) -> None:
        self.spin += 1
        for i, state in enumerate(self.states):
            if state == "running":
                self.render_step(i)

    def set_state(self, index: int, state: str) -> None:
        self.states[index] = state
        self.render_step(index)

    def _ui(self, func, *args) -> None:
        """Führt UI-Änderungen auch aus Worker-Threads sicher aus."""
        if threading.get_ident() == self.main_thread:
            func(*args)
        else:
            self.app.call_from_thread(func, *args)

    def log_line(self, text: str) -> None:
        self._ui(self.query_one(RichLog).write, text)

    def set_progress(self, percent: float | None) -> None:
        bar = self.query_one(ProgressBar)
        if percent is None:
            self._ui(lambda: bar.update(total=None))
        else:
            self._ui(lambda: bar.update(total=100, progress=percent))

    def set_status(self, text: str) -> None:
        self._ui(self.query_one("#status", Static).update, Text(text, style="dim"))

    # --- Ablauf ------------------------------------------------------------

    @work(exclusive=True)
    async def execute(self) -> None:
        log = self.query_one(RichLog)
        for i, step in enumerate(self.job.steps):
            always = isinstance(step, PythonStep) and step.always
            if self.cancel_requested and not always:
                self.set_state(i, "skipped")
                continue
            self.set_state(i, "running")
            self.set_progress(None)
            self.set_status("")
            log.write(f"\n[b {ACCENT}]▶ {escape(step.title)}[/]")
            self.ctx = StepContext(self.log_line, self.set_progress)
            if isinstance(step, PythonStep):
                error = await run_python(step, self.ctx)
            elif step.interactive:
                error = self.run_interactive(step)
            else:
                error = await run_command(step, self.ctx, self.set_status)
            if self.cancel_requested and not always:
                error = "abgebrochen"
            if error:
                self.set_state(i, "failed")
                self.failures.append((step.title, error))
                log.write(f"[{ERROR}]✘ {escape(error)}[/]")
                if self.job.stop_on_error:
                    self.cancel_requested = True
            else:
                self.set_state(i, "ok")
        self.finish()

    def run_interactive(self, step: CommandStep) -> str | None:
        with self.app.suspend():
            try:
                code = subprocess.run(step.argv, cwd=step.cwd).returncode
            except FileNotFoundError:
                return f"Programm nicht gefunden: {step.argv[0]}"
        return None if code in step.ok_codes else f"Exit-Code {code}"

    def finish(self) -> None:
        self.running = False
        bar = self.query_one(ProgressBar)
        bar.update(total=100, progress=100 if not self.failures else bar.progress)
        if self.failures:
            lines = [f"[b {ERROR}]✘ {len(self.failures)} Problem(e):[/]"]
            lines += [f"  [{ERROR}]•[/] {escape(t)}: {escape(e)}" for t, e in self.failures]
            if self.job.destructive or self.job.title.startswith("Kompletter"):
                lines.append(f"[{WARN}]Gerät nicht mehr für wichtige Daten verwenden.[/]")
            if self.job.failure_hint:
                lines.append(f"[{WARN}]{escape(self.job.failure_hint)}[/]")
        else:
            lines = [f"[b {OK}]✔ Alle Schritte ohne Befund abgeschlossen.[/]"]
            if self.job.destructive:
                lines.append("[dim]Das Gerät enthält jetzt nur Testdaten – vor Benutzung neu formatieren.[/]")
            if self.job.followup:
                lines.append(f"[b {ACCENT}]p[/] {escape(self.job.followup_label)}")
        lines.append("[dim]Esc: zurück zur Geräteliste[/]")
        self.query_one("#result", Static).update("\n".join(lines))
        self.refresh_bindings()
        self.app.bell()

    def check_action(self, action: str, parameters: tuple) -> bool | None:
        if action == "followup":
            # False blendet die Taste in der Fußzeile aus.
            return not self.running and not self.failures and self.job.followup is not None
        return True

    def action_close(self) -> None:
        if not self.running:
            self.app.pop_screen()
            return
        if self.cancel_requested and self.ctx and not self.ctx.process:
            return
        self.cancel_requested = True
        self.notify("Breche ab …")
        if self.ctx:
            self.ctx.cancel.set()
            if self.ctx.process and self.ctx.process.returncode is None:
                self.ctx.process.terminate()

    def action_followup(self) -> None:
        if self.job.followup and not self.running:
            self.app.switch_screen(RunScreen(self.job.followup()))


class DeviceScreen(Screen):
    BINDINGS = [
        Binding("enter", "choose", "Auswählen", show=True, priority=False),
        Binding("a", "eject", "Aushängen"),
        Binding("b", "open_backup", "Sicherung öffnen"),
        Binding("r", "refresh", "Aktualisieren"),
        Binding("q", "app.quit", "Beenden"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.devices: dict[str, Device] = {}
        self.snapshot: list | None = None

    def compose(self) -> ComposeResult:
        yield Static(BANNER, id="banner")
        yield DataTable(id="devices", cursor_type="row", zebra_stripes=True)
        yield Static(
            "Keine SD-Karte und kein USB-Stick gefunden – einfach einstecken, "
            "die Liste aktualisiert sich selbst.",
            id="empty",
        )
        if os.geteuid() != 0:
            yield Static(
                f"[{WARN}]Läuft ohne Root-Rechte: Geräte werden angezeigt, "
                "Prüfungen brauchen aber [b]bin/flash[/] (startet mit sudo).[/]",
                id="hint",
            )
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one(DataTable)
        table.add_columns("Gerät", "Typ", "Modell", "Größe", "Partitionen", "Status")
        self.refresh_devices()
        self.set_interval(2, self.refresh_devices)

    def refresh_devices(self) -> None:
        try:
            devices = list_devices()
        except (OSError, subprocess.CalledProcessError) as exc:
            self.notify(f"lsblk fehlgeschlagen: {exc}", severity="error")
            return
        snapshot = [(d.identity, d.all_mountpoints, d.readonly) for d in devices]
        if snapshot == self.snapshot:
            return
        self.snapshot = snapshot
        table = self.query_one(DataTable)
        selected = None
        if table.row_count:
            selected = table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value
        table.clear()
        self.devices = {d.path: d for d in devices}
        for device in devices:
            parts = ", ".join(
                f"{p.label or p.name} ({p.fstype or '?'})" for p in device.partitions
            ) or "–"
            table.add_row(
                device.name,
                device.kind,
                device.title,
                human_size(device.size),
                parts,
                device_status(device),
                key=device.path,
            )
        if selected in self.devices:
            table.move_cursor(row=table.get_row_index(selected))
        self.query_one("#empty").display = not devices
        table.display = bool(devices)

    def action_refresh(self) -> None:
        self.snapshot = None
        self.refresh_devices()

    def selected_device(self) -> Device | None:
        table = self.query_one(DataTable)
        if not table.row_count:
            return None
        key = table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value
        return self.devices[key]

    def action_choose(self) -> None:
        if device := self.selected_device():
            self.open_actions(device)

    def action_eject(self) -> None:
        if device := self.selected_device():
            self.start(device, "eject")

    def action_open_backup(self) -> None:
        def image_chosen(image: Path | None) -> None:
            if image is None:
                return
            self.app.push_screen(
                ActionScreen(images.image_device(image), IMAGE_ACTIONS),
                lambda action: self.start_image(image, action),
            )

        self.app.push_screen(ImageScreen(images.default_image_bases()), image_chosen)

    def start_image(self, image: Path, action: str | None) -> None:
        if action is None:
            return
        if os.geteuid() != 0:
            self.notify("Abbilder einhängen braucht Root-Rechte – bitte mit bin/flash starten.", severity="error")
            return
        if action == "files":
            self.ask_target(
                images.target_dir(image, "dateien"),
                "Ziel für die wiederhergestellten Dateien",
                "Das Abbild wird nur schreibgeschützt eingehängt und nicht verändert.",
                lambda target: self.run_job(images.restore_files(image, target)),
            )
        elif action == "repair":
            size = human_size(image.stat().st_size)

            def warned(target: Path, ok: bool | None) -> None:
                if ok:
                    self.run_job(images.repair_and_restore(image, target))

            def with_target(target: Path) -> None:
                lines = [
                    f"Das Original [b]{escape(image.name)}[/] bleibt unverändert – repariert wird "
                    f"eine Kopie in [b]{escape(str(target))}[/].",
                    f"Platzbedarf: bis zu {size} für die Kopie plus die wiederhergestellten Dateien.",
                    f"\n[{WARN}]fsck kann in der Kopie beschädigte Ordner und Dateien kürzen, löschen "
                    "oder nach FOUND.000 bzw. lost+found verschieben. Dabei können Strukturen "
                    "zerstört werden.[/]",
                    "[dim]Was hier verloren geht, findet PhotoRec im Original eventuell noch.[/]",
                ]
                self.app.push_screen(
                    WarnScreen("Reparatur kann Strukturen zerstören", lines),
                    lambda ok: warned(target, ok),
                )

            self.ask_target(
                images.target_dir(image, "reparatur"),
                "Ziel für Kopie und wiederhergestellte Dateien",
                f"Hier entsteht eine Kopie des Abbilds ({size}) und darin der Ordner dateien/.",
                with_target,
            )
        elif action == "imgphotorec":
            self.ask_target(
                images.target_dir(image, "photorec"),
                "Ziel für die von PhotoRec gefundenen Dateien",
                "PhotoRec liest das Abbild nur. Dateinamen und Ordner gehen dabei verloren.",
                lambda target: self.run_job(images.photorec_image(image, target)),
            )

    def ask_target(self, default: Path, title: str, hint: str, then) -> None:
        def chosen(path: Path | None) -> None:
            if path is not None:
                then(path)

        self.app.push_screen(PathScreen(default, title, hint), chosen)

    @on(DataTable.RowSelected)
    def row_selected(self, event: DataTable.RowSelected) -> None:
        self.open_actions(self.devices[event.row_key.value])

    def open_actions(self, device: Device) -> None:
        self.app.push_screen(ActionScreen(device), lambda action: self.start(device, action))

    def start(self, device: Device, action: str | None) -> None:
        if action is None:
            return
        if os.geteuid() != 0 and action not in NO_ROOT_ACTIONS:
            self.notify("Prüfungen brauchen Root-Rechte – bitte mit bin/flash starten.", severity="error")
            return
        if action == "eject":
            # Neu einlesen, damit auch zwischenzeitlich eingehängte Partitionen erfasst werden.
            fresh = find_device(device.identity)
            if fresh is None:
                self.notify("Gerät nicht mehr gefunden – schon abgezogen?", severity="warning")
                return
            self.run_job(jobs.eject(fresh))
        elif action == "quick":
            self.run_job(jobs.quick_check(device))
        elif action == "full":
            self.run_job(jobs.full_read(device))
        elif action in ("rescue", "photorec"):
            build = jobs.rescue if action == "rescue" else jobs.photorec

            def with_path(path: Path | None) -> None:
                if path is not None:
                    self.run_job(build(device, path))

            self.app.push_screen(PathScreen(jobs.default_rescue_dir(device)), with_path)
        elif action in ("intensive", "thorough"):

            def confirmed(ok: bool | None) -> None:
                if not ok:
                    return
                # Neu einlesen: zwischen Auswahl und Bestätigung kann der Stick gewechselt sein.
                fresh = find_device(device.identity)
                if fresh is None:
                    self.notify("Gerät nicht mehr gefunden – abgebrochen.", severity="error")
                    return
                self.run_job(jobs.intensive(fresh, thorough=action == "thorough"))

            self.app.push_screen(ConfirmScreen(device), confirmed)

    def run_job(self, job: Job) -> None:
        self.app.push_screen(RunScreen(job))


class FlashApp(App):
    TITLE = "flash"
    CSS = f"""
    Screen {{ background: $background; }}
    #banner {{ padding: 1 2; }}
    #devices {{ margin: 0 2; height: auto; max-height: 70%; }}
    #empty, #hint {{ padding: 1 2; color: $text-muted; }}
    ActionScreen, PathScreen, ConfirmScreen, ImageScreen, WarnScreen {{ align: center middle; }}
    .dialog {{
        width: 76; height: auto; padding: 1 2;
        border: round {ACCENT}; background: $surface;
    }}
    .dialog.danger {{ border: round $error; }}
    .dialog-title {{ margin-bottom: 1; }}
    .dialog-hint {{ margin-top: 1; }}
    .dialog OptionList {{ height: auto; max-height: 20; border: none; background: $surface; }}
    #job-title {{ padding: 1 2 0 2; }}
    #run {{ height: 1fr; margin: 1 2 0 2; }}
    #steps {{ width: 46; border: round $panel; padding: 0 1; }}
    .step {{ margin-bottom: 1; }}
    #output {{ border: round $panel; padding: 0 1; }}
    #progress {{ margin-top: 1; }}
    #progress Bar {{ width: 1fr; }}
    #status {{ height: auto; max-height: 12; }}
    #log {{ height: 1fr; background: $background; overflow-x: hidden; }}
    #result {{ padding: 1 2; height: auto; }}
    """

    def on_mount(self) -> None:
        self.register_theme(FLASH_THEME)
        self.theme = "flash"
        self.push_screen(DeviceScreen())


def main() -> None:
    FlashApp().run()


if __name__ == "__main__":
    main()
