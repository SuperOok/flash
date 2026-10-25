"""Prüf- und Rettungsabläufe, zusammengesetzt aus einzelnen Schritten."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from rich.markup import escape

from .devices import Device, human_size

MIB = 1024 * 1024


class StepContext:
    """Schnittstelle eines Schritts zur Oberfläche: Log, Fortschritt, Abbruch."""

    def __init__(
        self,
        log: Callable[[str], None],
        progress: Callable[[float | None], None],
    ) -> None:
        self.log = log
        self.progress = progress
        self.cancel = threading.Event()
        # Laufender externer Prozess, damit ein Abbruch ihn beenden kann.
        self.process = None


@dataclass
class CommandStep:
    title: str
    argv: list[str]
    # Regex mit einer Gruppe für den Fortschritt in Prozent.
    progress_re: re.Pattern | None = None
    # Regex, deren Treffer den Schritt als fehlgeschlagen markiert.
    fail_re: re.Pattern | None = None
    ok_codes: tuple[int, ...] = (0,)
    # Zeilen, die nur den laufenden Status zeigen und nicht ins Log gehören.
    status_re: re.Pattern | None = None
    # Interaktive Programme (z. B. photorec) laufen im echten Terminal.
    interactive: bool = False
    cwd: str | None = None


@dataclass
class PythonStep:
    title: str
    # Liefert None bei Erfolg, sonst eine Fehlermeldung.
    func: Callable[[StepContext], str | None]
    # Läuft auch nach Fehler oder Abbruch (Aufräumen, Dateirechte).
    always: bool = False


Step = CommandStep | PythonStep


@dataclass
class Job:
    title: str
    device: Device
    steps: list[Step]
    destructive: bool = False
    # Bei False laufen nach einem Fehler die übrigen Schritte weiter.
    stop_on_error: bool = True
    # Wird nach erfolgreichem Lauf als weiterer Job angeboten.
    followup: Callable[[], Job] | None = field(default=None, repr=False)
    followup_label: str = ""
    # Wird nach einem Lauf mit Fehlern als nächster Schritt empfohlen.
    failure_hint: str = ""


BADBLOCKS_PROGRESS = re.compile(r"([\d.]+)% done")
BADBLOCKS_FAIL = re.compile(r"Pass completed, ([1-9]\d*) bad blocks found")
DDRESCUE_PROGRESS = re.compile(r"pct rescued:\s*([\d.]+)%")
DDRESCUE_STATUS = re.compile(
    r"^\s*(ipos|opos|non-tried|non-trimmed|non-scraped|rescued|bad-sector|bad areas|"
    r"run time|remaining time|pct rescued|current rate|average rate|read errors|"
    r"error rate|time since last|Copying|Trimming|Scraping|Retrying|Finished)"
)
F3PROBE_FAIL = re.compile(r"Bad news|counterfeit", re.IGNORECASE)

FSCK_COMMANDS = {
    "vfat": ["fsck.vfat", "-n", "-v"],
    "exfat": ["fsck.exfat", "-n", "-v"],
    "ext2": ["e2fsck", "-n", "-f", "-v"],
    "ext3": ["e2fsck", "-n", "-f", "-v"],
    "ext4": ["e2fsck", "-n", "-f", "-v"],
    "ntfs": ["ntfsfix", "-n"],
}


# --- Python-Schritte ---------------------------------------------------------


def device_info(device: Device) -> Callable[[StepContext], str | None]:
    def run(ctx: StepContext) -> str | None:
        ctx.log(f"Gerät:      {device.path} ({device.kind})")
        ctx.log(f"Modell:     {device.title}")
        ctx.log(f"Seriennr.:  {device.serial or '–'}")
        ctx.log(f"Größe:      {human_size(device.size)} ({device.size} Bytes)")
        for part in device.partitions:
            mount = ", ".join(part.mountpoints) or "nicht eingehängt"
            ctx.log(
                f"Partition:  {part.path} {human_size(part.size)} "
                f"{part.fstype or '?'} {part.label or ''} ({mount})"
            )
        if not device.partitions:
            ctx.log("Partition:  keine")
        if device.readonly:
            # Verschlissener Flash schaltet sich oft dauerhaft auf nur-lesen.
            ctx.log("[#E5B567]Gerät ist schreibgeschützt (Schalter oder verschlissener Flash).[/]")
        return None

    return run


def sample_read(device: Device, samples: int = 100, chunk: int = MIB) -> Callable:
    """Liest verteilte Stichproben über das ganze Gerät und misst die Lesegeschwindigkeit."""

    def run(ctx: StepContext) -> str | None:
        last = max(device.size - chunk, 0)
        offsets = sorted({(last * i // (samples - 1)) // 4096 * 4096 for i in range(samples)})
        errors, blank, total_time = 0, 0, 0.0
        fd = os.open(device.path, os.O_RDONLY)
        try:
            # Seitencache verwerfen, sonst misst man den RAM statt der Karte.
            os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)
            for i, offset in enumerate(offsets):
                if ctx.cancel.is_set():
                    return "abgebrochen"
                start = time.monotonic()
                try:
                    data = os.pread(fd, chunk, offset)
                except OSError as exc:
                    errors += 1
                    ctx.log(f"[#E06C75]Lesefehler bei {human_size(offset)}: {exc.strerror}[/]")
                    continue
                total_time += time.monotonic() - start
                if data.count(0) == len(data) or data.count(0xFF) == len(data):
                    blank += 1
                ctx.progress(100 * (i + 1) / len(offsets))
        finally:
            os.close(fd)
        read_ok = len(offsets) - errors
        speed = read_ok * chunk / total_time / MIB if total_time else 0
        ctx.log(f"Stichproben: {len(offsets)}, Lesefehler: {errors}, leer: {blank}")
        ctx.log(f"Lesegeschwindigkeit (zufällig verteilt): {speed:.1f} MiB/s")
        if errors:
            return f"{errors} Stichproben nicht lesbar"
        return None

    return run


def sequential_read(device: Device, amount: int = 128 * MIB) -> Callable:
    def run(ctx: StepContext) -> str | None:
        amount_ = min(amount, device.size)
        fd = os.open(device.path, os.O_RDONLY)
        try:
            os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)
            done = 0
            start = time.monotonic()
            while done < amount_:
                if ctx.cancel.is_set():
                    return "abgebrochen"
                data = os.pread(fd, 4 * MIB, done)
                if not data:
                    break
                done += len(data)
                ctx.progress(100 * done / amount_)
            elapsed = time.monotonic() - start
        except OSError as exc:
            return f"Lesefehler: {exc.strerror}"
        finally:
            os.close(fd)
        ctx.log(f"Sequentiell gelesen: {human_size(done)} mit {done / elapsed / MIB:.1f} MiB/s")
        return None

    return run


def kernel_messages(device: Device) -> Callable:
    """Sucht im Kernel-Log nach I/O-Fehlern des Geräts."""

    def run(ctx: StepContext) -> str | None:
        result = subprocess.run(["dmesg"], capture_output=True, text=True)
        if result.returncode != 0:
            ctx.log("dmesg nicht lesbar (Root-Rechte?) – übersprungen.")
            return None
        names = [device.name] + [p.name for p in device.partitions]
        pattern = re.compile(r"\b(" + "|".join(map(re.escape, names)) + r")\b")
        problems = [
            line
            for line in result.stdout.splitlines()
            if pattern.search(line) and re.search(r"error|fail|reset|corrupt", line, re.I)
        ]
        for line in problems[-20:]:
            ctx.log(f"[#E5B567]{line}[/]")
        if problems:
            return f"{len(problems)} Fehlermeldungen im Kernel-Log"
        ctx.log("Keine Fehlermeldungen im Kernel-Log.")
        return None

    return run


def _unmount_one(path: str) -> str | None:
    """Hängt ein Blockgerät aus; liefert bei Fehler die Meldung."""
    # udisksctl klappt auch ohne Root (polkit) und räumt den Mountpunkt unter /media auf.
    if shutil.which("udisksctl"):
        result = subprocess.run(
            ["udisksctl", "unmount", "--no-user-interaction", "-b", path],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            return None
    result = subprocess.run(["umount", path], capture_output=True, text=True)
    return None if result.returncode == 0 else result.stderr.strip()


def unmount(device: Device) -> Callable:
    def run(ctx: StepContext) -> str | None:
        for part in [device, *device.partitions]:
            for mount in part.mountpoints:
                ctx.log(f"Hänge {part.path} aus ({mount}) …")
                error = _unmount_one(part.path)
                if error is not None:
                    return f"Aushängen fehlgeschlagen: {error}"
        if not device.is_mounted:
            ctx.log("Nichts eingehängt.")
        return None

    return run


def flush_buffers(ctx: StepContext) -> str | None:
    os.sync()
    ctx.log("Alle Schreibpuffer geschrieben.")
    return None


def power_off(device: Device) -> Callable:
    """Schaltet das Gerät ab, damit es gefahrlos abgezogen werden kann."""

    def run(ctx: StepContext) -> str | None:
        if not shutil.which("udisksctl"):
            ctx.log("[#E5B567]udisksctl fehlt – Gerät nicht abgeschaltet, kann aber abgezogen werden.[/]")
            return None
        result = subprocess.run(
            ["udisksctl", "power-off", "--no-user-interaction", "-b", device.path],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            # Fest eingebaute Kartenleser lassen sich oft nicht abschalten – Aushängen genügt.
            ctx.log(f"[#E5B567]Abschalten nicht möglich: {escape(result.stderr.strip())}[/]")
            ctx.log("Das Gerät ist ausgehängt und kann trotzdem abgezogen werden.")
            return None
        ctx.log(f"[#98C379]{device.path} ist abgeschaltet und kann abgezogen werden.[/]")
        return None

    return run


def chown_to_user(path: Path) -> Callable:
    """Gibt unter sudo erzeugte Dateien dem aufrufenden Benutzer zurück."""

    def run(ctx: StepContext) -> str | None:
        uid, gid = os.environ.get("SUDO_UID"), os.environ.get("SUDO_GID")
        if not uid or not gid or not path.exists():
            return None
        for item in [path, *path.rglob("*")]:
            os.chown(item, int(uid), int(gid))
        ctx.log(f"Besitzer von {path} auf UID {uid} gesetzt.")
        return None

    return run


def ensure_free_space(target: Path, needed: int) -> Callable:
    def run(ctx: StepContext) -> str | None:
        target.mkdir(parents=True, exist_ok=True)
        stat = os.statvfs(target)
        free = stat.f_bavail * stat.f_frsize
        ctx.log(f"Ziel: {target} – frei: {human_size(free)}, benötigt: {human_size(needed)}")
        if free < needed:
            return "Nicht genug freier Speicher am Ziel"
        return None

    return run


# --- Jobs ----------------------------------------------------------------------


def quick_check(device: Device) -> Job:
    steps: list[Step] = [
        PythonStep("Geräteinformationen", device_info(device)),
        PythonStep("Stichproben über das ganze Gerät lesen", sample_read(device)),
        PythonStep("Sequentielle Lesegeschwindigkeit", sequential_read(device)),
    ]
    for part in device.partitions:
        command = FSCK_COMMANDS.get(part.fstype or "")
        if command is None:
            continue
        if part.mountpoints:
            steps.append(
                PythonStep(
                    f"Dateisystem {part.name}: übersprungen (eingehängt)",
                    lambda ctx: None,
                )
            )
            continue
        # fsck: 0 = sauber, 1 = Fehler gefunden (bei -n nicht behoben).
        steps.append(CommandStep(f"Dateisystem {part.name} prüfen ({part.fstype})", [*command, part.path]))
    steps.append(PythonStep("Kernel-Log auf Fehler prüfen", kernel_messages(device)))
    return Job("Schnell-Check (nur lesend)", device, steps, stop_on_error=False)


def full_read(device: Device) -> Job:
    steps: list[Step] = [
        PythonStep("Geräteinformationen", device_info(device)),
        CommandStep(
            "Kompletter Lesetest (badblocks, nur lesend)",
            ["badblocks", "-sv", "-b", "4096", "-c", "1024", device.path],
            progress_re=BADBLOCKS_PROGRESS,
            status_re=BADBLOCKS_PROGRESS,
            fail_re=BADBLOCKS_FAIL,
        ),
        PythonStep("Kernel-Log auf Fehler prüfen", kernel_messages(device)),
    ]
    return Job("Kompletter Lesetest (nur lesend)", device, steps, stop_on_error=False)


def rescue(device: Device, target: Path) -> Job:
    image = target / f"{device.name}.img"
    mapfile = target / f"{device.name}.map"
    steps: list[Step] = [
        PythonStep("Geräteinformationen", device_info(device)),
        PythonStep("Speicherplatz am Ziel prüfen", ensure_free_space(target, device.size)),
        CommandStep(
            "Image erstellen – schneller Durchlauf (ddrescue -n)",
            ["ddrescue", "-n", device.path, str(image), str(mapfile)],
            progress_re=DDRESCUE_PROGRESS,
            status_re=DDRESCUE_STATUS,
        ),
        CommandStep(
            "Fehlerhafte Bereiche erneut lesen (ddrescue -r3)",
            ["ddrescue", "-d", "-r3", device.path, str(image), str(mapfile)],
            progress_re=DDRESCUE_PROGRESS,
            status_re=DDRESCUE_STATUS,
        ),
        PythonStep("Dateirechte anpassen", chown_to_user(target)),
    ]
    job = Job("Datenrettung (Image mit ddrescue)", device, steps)

    def followup() -> Job:
        from .images import restore_files  # images baut auf jobs auf

        return restore_files(image, target / "dateien")

    job.followup = followup
    job.followup_label = "Dateien aus dem Image wiederherstellen (nur lesend)"
    return job


def photorec(device: Device, target: Path, source: Path | None = None) -> Job:
    outdir = target / "photorec"
    steps: list[Step] = [
        PythonStep("Zielverzeichnis anlegen", lambda ctx: outdir.mkdir(parents=True, exist_ok=True)),
        CommandStep(
            "PhotoRec (interaktiv)",
            ["photorec", "/log", "/d", str(outdir / "recup"), str(source or device.path)],
            interactive=True,
            cwd=str(outdir),
        ),
        PythonStep("Dateirechte anpassen", chown_to_user(target)),
    ]
    return Job("Dateien wiederherstellen (PhotoRec)", device, steps)


def intensive(device: Device, thorough: bool = False) -> Job:
    pattern = [] if thorough else ["-t", "random"]
    steps: list[Step] = [
        PythonStep("Geräteinformationen", device_info(device)),
        PythonStep("Partitionen aushängen", unmount(device)),
        CommandStep(
            "Echte Kapazität prüfen (f3probe, Fake-Erkennung)",
            ["f3probe", "--destructive", "--time-ops", device.path],
            fail_re=F3PROBE_FAIL,
        ),
        CommandStep(
            "Schreib-/Lesetest " + ("mit 4 Mustern" if thorough else "mit Zufallsmuster") + " (badblocks -w)",
            ["badblocks", "-wsv", "-b", "4096", "-c", "1024", *pattern, device.path],
            progress_re=BADBLOCKS_PROGRESS,
            status_re=BADBLOCKS_PROGRESS,
            fail_re=BADBLOCKS_FAIL,
        ),
        PythonStep("Kernel-Log auf Fehler prüfen", kernel_messages(device)),
    ]
    title = "Intensiv-Check gründlich" if thorough else "Intensiv-Check"
    return Job(title + " (überschreibt alles!)", device, steps, destructive=True)


def eject(device: Device) -> Job:
    steps: list[Step] = [
        PythonStep("Partitionen aushängen", unmount(device)),
        PythonStep("Schreibpuffer leeren (sync)", flush_buffers),
        PythonStep("Gerät abschalten", power_off(device)),
    ]
    return Job("Aushängen und sicher entfernen", device, steps)


def rescue_base() -> Path:
    """Standardort für Sicherungen im Home des aufrufenden Benutzers, auch unter sudo."""
    user = os.environ.get("SUDO_USER", "")
    return Path(os.path.expanduser(f"~{user}")) / "flash-rescue"


def default_rescue_dir(device: Device) -> Path:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    ident = re.sub(r"[^A-Za-z0-9_-]+", "_", device.serial or device.title)
    return rescue_base() / f"{ident}-{stamp}"

