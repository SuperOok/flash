"""Dateien aus einem Sicherungs-Abbild (z. B. von ddrescue) wiederherstellen.

Das Original-Abbild wird nur schreibgeschützt verbunden (`losetup --read-only`).
Reparaturen laufen ausschließlich auf einer Kopie.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from rich.markup import escape

from .devices import Device, human_size
from .jobs import (
    FSCK_COMMANDS,
    MIB,
    Job,
    PythonStep,
    Step,
    StepContext,
    chown_to_user,
    ensure_free_space,
    photorec,
    rescue_base,
)

# Englische Ausgaben wie in runner.COMMAND_ENV (runner importiert jobs, daher hier lokal).
COMMAND_ENV = {**os.environ, "LC_ALL": "C.UTF-8", "LANG": "C.UTF-8"}

# Reparatur-Aufrufe und Exit-Codes, die "sauber" oder "behoben" bedeuten.
REPAIR_COMMANDS: dict[str, tuple[list[str], tuple[int, ...]]] = {
    "vfat": (["fsck.vfat", "-a", "-v"], (0, 1)),
    "exfat": (["fsck.exfat", "-y", "-v"], (0, 1)),
    "ext2": (["e2fsck", "-f", "-y", "-v"], (0, 1, 2)),
    "ext3": (["e2fsck", "-f", "-y", "-v"], (0, 1, 2)),
    "ext4": (["e2fsck", "-f", "-y", "-v"], (0, 1, 2)),
    "ntfs": (["ntfsfix"], (0,)),
}

# ext3/4 würden beim Einhängen das Journal einspielen – auf schreibgeschütztem
# Loop-Gerät scheitert das, also Journal ignorieren.
MOUNT_OPTIONS = {"ext3": "ro,noload", "ext4": "ro,noload"}


@dataclass
class Filesystem:
    path: str
    fstype: str | None
    label: str | None


@dataclass
class ImageSession:
    """Zustand, den die Schritte eines Ablaufs miteinander teilen."""

    image: Path
    readonly: bool = True
    loop: str | None = None
    # (Dateisystem, Mountpunkt)
    mounts: list[tuple[Filesystem, Path]] = field(default_factory=list)
    unreadable: list[str] = field(default_factory=list)


# --- Hilfsfunktionen -------------------------------------------------------------


def image_device(image: Path) -> Device:
    """Stellt ein Abbild für Anzeige und Jobs wie ein Gerät dar."""
    return Device(
        name=image.name,
        path=str(image),
        size=image.stat().st_size if image.exists() else 0,
        model=image.name,
        vendor="",
        serial="",
        tran="Abbild",
        removable=False,
        readonly=True,
    )


def find_images(*bases: Path) -> list[Path]:
    """Sucht Abbilder (*.img) direkt in und eine Ebene unter den Verzeichnissen."""
    found: set[Path] = set()
    for base in bases:
        if not base.is_dir():
            continue
        for pattern in ("*.img", "*/*.img"):
            found.update(p for p in base.glob(pattern) if p.is_file())
    return sorted(found, key=lambda p: p.stat().st_mtime, reverse=True)


def default_image_bases() -> list[Path]:
    return [rescue_base()]


def target_dir(image: Path, purpose: str) -> Path:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    return image.parent / f"{image.stem}-{purpose}-{stamp}"


def parse_blkid(output: str) -> dict[str, str]:
    """Liest die Ausgabe von `blkid -o export`."""
    result = {}
    for line in output.splitlines():
        key, sep, value = line.partition("=")
        if sep:
            result[key.strip()] = value.strip()
    return result


def parse_partitions(data: dict) -> list[str]:
    """Pfade der Partitionen eines Loop-Geräts aus `lsblk -J`."""
    devices = data.get("blockdevices") or []
    if not devices:
        return []
    return [c["path"] for c in devices[0].get("children") or [] if c.get("type") == "part"]


def safe_name(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", text).strip("._") or "partition"


def _run(argv: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(argv, capture_output=True, text=True, env=COMMAND_ENV)


def run_logged(ctx: StepContext, argv: list[str]) -> int:
    """Führt ein Programm aus und schreibt seine Ausgabe ins Log."""
    ctx.log(f"[dim]$ {escape(' '.join(argv))}[/]")
    try:
        process = subprocess.Popen(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            text=True,
            errors="replace",
            env=COMMAND_ENV,
        )
    except FileNotFoundError:
        ctx.log(f"Programm nicht gefunden: {escape(argv[0])}")
        return 127
    ctx.process = process
    assert process.stdout is not None
    for line in process.stdout:
        if line.strip():
            ctx.log(escape(line.rstrip()))
    code = process.wait()
    ctx.process = None
    return code


def filesystems(loop: str) -> list[Filesystem]:
    """Dateisysteme auf dem Loop-Gerät – Partitionen oder das ganze Abbild."""
    result = _run(["lsblk", "-J", "-o", "NAME,PATH,TYPE", loop])
    paths = parse_partitions(json.loads(result.stdout)) if result.returncode == 0 else []
    found = []
    for path in paths or [loop]:
        # -p liest direkt vom Gerät statt aus dem (evtl. noch leeren) udev-Cache.
        info = parse_blkid(_run(["blkid", "-p", "-o", "export", path]).stdout)
        if info.get("USAGE", "filesystem") == "filesystem":
            found.append(Filesystem(path, info.get("TYPE"), info.get("LABEL")))
    return found


def copy_tree(src: Path, dst: Path, ctx: StepContext, done: list[int], total: int) -> list[str]:
    """Kopiert einen Verzeichnisbaum; liefert die Dateien, die nicht lesbar waren.

    `done` ist ein Zähler (Liste mit einem Wert), der über mehrere Aufrufe weiterläuft.
    """
    failed = []
    last_update = 0.0
    dirs = []
    for root, dirnames, filenames in os.walk(src, onerror=lambda e: failed.append(str(e.filename))):
        rel = Path(root).relative_to(src)
        target = dst / rel
        target.mkdir(parents=True, exist_ok=True)
        dirs.append((Path(root), target))
        for name in filenames + [d for d in dirnames if (Path(root) / d).is_symlink()]:
            if ctx.cancel.is_set():
                return failed
            source, dest = Path(root) / name, target / name
            try:
                if source.is_symlink():
                    os.symlink(os.readlink(source), dest)
                    continue
                if not source.is_file():
                    continue
                with open(source, "rb") as fin, open(dest, "wb") as fout:
                    while chunk := fin.read(4 * MIB):
                        fout.write(chunk)
                        done[0] += len(chunk)
                        if total and time.monotonic() - last_update > 0.2:
                            last_update = time.monotonic()
                            ctx.progress(min(100.0, 100 * done[0] / total))
                        if ctx.cancel.is_set():
                            break
                shutil.copystat(source, dest)
            except OSError as exc:
                failed.append(str(rel / name))
                ctx.log(f"[#E06C75]Nicht kopiert: {escape(str(rel / name))} ({escape(exc.strerror or str(exc))})[/]")
                dest.unlink(missing_ok=True)
    # Zeitstempel der Ordner erst am Ende setzen, sonst ändert das Kopieren sie wieder.
    for source, target in reversed(dirs):
        try:
            shutil.copystat(source, target)
        except OSError:
            pass
    return failed


def tree_size(path: Path) -> int:
    total = 0
    for root, _, filenames in os.walk(path):
        for name in filenames:
            item = Path(root) / name
            if item.is_file() and not item.is_symlink():
                total += item.stat().st_size
    return total


# --- Schritte ----------------------------------------------------------------------


def attach(session: ImageSession) -> Callable:
    def run(ctx: StepContext) -> str | None:
        argv = ["losetup", "--find", "--show", "--partscan"]
        if session.readonly:
            argv.append("--read-only")
        result = _run([*argv, str(session.image)])
        if result.returncode != 0:
            return f"losetup fehlgeschlagen: {result.stderr.strip()}"
        session.loop = result.stdout.strip()
        # Partitionsgeräte erscheinen über udev erst kurz danach.
        _run(["udevadm", "settle"])
        mode = "schreibgeschützt" if session.readonly else "beschreibbar"
        ctx.log(f"{escape(str(session.image))} → {session.loop} ({mode})")
        return None

    return run


def mount_all(session: ImageSession) -> Callable:
    """Hängt alle erkennbaren Dateisysteme nur lesend ein."""

    def run(ctx: StepContext) -> str | None:
        assert session.loop
        found = filesystems(session.loop)
        if not found:
            return "Kein Dateisystem im Abbild erkannt"
        for fs in found:
            name = f"{fs.path} ({fs.fstype or '?'}{', ' + fs.label if fs.label else ''})"
            if not fs.fstype:
                ctx.log(f"[#E5B567]{escape(name)}: kein Dateisystem erkannt[/]")
                session.unreadable.append(fs.path)
                continue
            mountpoint = Path(tempfile.mkdtemp(prefix="flash-"))
            options = MOUNT_OPTIONS.get(fs.fstype, "ro")
            result = _run(["mount", "-o", options, fs.path, str(mountpoint)])
            if result.returncode == 0:
                session.mounts.append((fs, mountpoint))
                ctx.log(f"[#98C379]{escape(name)} eingehängt[/] unter {mountpoint}")
                continue
            mountpoint.rmdir()
            session.unreadable.append(fs.path)
            ctx.log(f"[#E06C75]{escape(name)} lässt sich nicht einhängen: {escape(result.stderr.strip())}[/]")
            if command := FSCK_COMMANDS.get(fs.fstype):
                ctx.log("Prüfe das Dateisystem nur lesend (ändert nichts):")
                run_logged(ctx, [*command, fs.path])
        if not session.mounts:
            return "Kein Dateisystem einhängbar – vermutlich beschädigt"
        return None

    return run


def check_space(session: ImageSession, target: Path) -> Callable:
    def run(ctx: StepContext) -> str | None:
        needed = 0
        for _, mountpoint in session.mounts:
            stat = os.statvfs(mountpoint)
            needed += (stat.f_blocks - stat.f_bfree) * stat.f_frsize
        return ensure_free_space(target, needed)(ctx)

    return run


def copy_files(session: ImageSession, target: Path) -> Callable:
    def run(ctx: StepContext) -> str | None:
        total = sum(tree_size(m) for _, m in session.mounts)
        ctx.log(f"Zu kopieren: {human_size(total)}")
        done = [0]
        failed: list[str] = []
        used: set[str] = set()
        for fs, mountpoint in session.mounts:
            name = safe_name(fs.label or Path(fs.path).name)
            while name in used:
                name += "_"
            used.add(name)
            dest = target / name
            ctx.log(f"{escape(fs.path)} → {escape(str(dest))}")
            failed += copy_tree(mountpoint, dest, ctx, done, total)
            if ctx.cancel.is_set():
                return "abgebrochen"
        ctx.progress(100)
        ctx.log(f"[#98C379]{human_size(done[0])} wiederhergestellt nach {escape(str(target))}[/]")
        problems = []
        if failed:
            problems.append(f"{len(failed)} Dateien nicht lesbar")
        if session.unreadable:
            problems.append("nicht einhängbar: " + ", ".join(session.unreadable))
        return "; ".join(problems) or None

    return run


def detach(session: ImageSession) -> Callable:
    """Räumt auf: aushängen, Mountpunkte löschen, Loop-Gerät lösen."""

    def run(ctx: StepContext) -> str | None:
        errors = []
        for fs, mountpoint in reversed(session.mounts):
            result = _run(["umount", str(mountpoint)])
            if result.returncode != 0:
                errors.append(f"{fs.path}: {result.stderr.strip()}")
                continue
            mountpoint.rmdir()
        session.mounts.clear()
        if session.loop:
            result = _run(["losetup", "--detach", session.loop])
            if result.returncode != 0:
                errors.append(f"{session.loop}: {result.stderr.strip()}")
            else:
                ctx.log(f"{session.loop} gelöst.")
                session.loop = None
        return "; ".join(errors) or None

    return run


def copy_image(src: Path, dst: Path) -> Callable:
    """Kopiert das Abbild; leere Bereiche bleiben dünn belegt (sparse)."""

    def run(ctx: StepContext) -> str | None:
        if dst.exists():
            return f"{dst} existiert bereits – anderes Ziel wählen"
        if src.resolve() == dst.resolve():
            return "Kopie und Original wären dieselbe Datei"
        size = src.stat().st_size
        chunk = 4 * MIB
        done, last_update = 0, 0.0
        with open(src, "rb") as fin, open(dst, "wb") as fout:
            while data := fin.read(chunk):
                if ctx.cancel.is_set():
                    fout.close()
                    dst.unlink()
                    return "abgebrochen"
                if data.count(0) == len(data):
                    fout.seek(len(data), os.SEEK_CUR)
                else:
                    fout.write(data)
                done += len(data)
                if time.monotonic() - last_update > 0.2:
                    last_update = time.monotonic()
                    ctx.progress(100 * done / size)
            fout.truncate(size)
        ctx.progress(100)
        ctx.log(f"Kopie: {escape(str(dst))} ({human_size(size)}); Original unverändert.")
        return None

    return run


def repair(session: ImageSession) -> Callable:
    def run(ctx: StepContext) -> str | None:
        assert session.loop and not session.readonly
        problems = []
        for fs in filesystems(session.loop):
            entry = REPAIR_COMMANDS.get(fs.fstype or "")
            if entry is None:
                ctx.log(f"[#E5B567]{escape(fs.path)}: keine Reparatur für {escape(fs.fstype or '?')} bekannt[/]")
                continue
            command, ok_codes = entry
            code = run_logged(ctx, [*command, fs.path])
            if ctx.cancel.is_set():
                return "abgebrochen"
            if code not in ok_codes:
                problems.append(f"{fs.path}: Exit-Code {code}")
        # Kein Abbruch: auch ein teilweise repariertes Dateisystem lässt sich oft einhängen.
        for problem in problems:
            ctx.log(f"[#E5B567]Nicht vollständig repariert – {escape(problem)}[/]")
        return None

    return run


# --- Jobs ----------------------------------------------------------------------------

NEXT_STAGE_HINT = (
    "Nächste Stufe: Sicherung erneut öffnen (Taste b) → „Reparieren und wiederherstellen“ "
    "(arbeitet an einer Kopie) oder PhotoRec."
)


def restore_files(image: Path, target: Path) -> Job:
    """Stufe 1: Abbild schreibgeschützt einhängen und alle Dateien kopieren."""
    session = ImageSession(image, readonly=True)
    steps: list[Step] = [
        PythonStep("Abbild schreibgeschützt verbinden", attach(session)),
        PythonStep("Dateisysteme nur lesend einhängen", mount_all(session)),
        PythonStep("Speicherplatz am Ziel prüfen", check_space(session, target)),
        PythonStep("Dateien kopieren", copy_files(session, target)),
        PythonStep("Aushängen und Abbild lösen", detach(session), always=True),
        PythonStep("Dateirechte anpassen", chown_to_user(target), always=True),
    ]
    job = Job("Dateien aus Sicherung wiederherstellen (nur lesend)", image_device(image), steps)
    job.failure_hint = NEXT_STAGE_HINT
    return job


def repair_and_restore(image: Path, target: Path) -> Job:
    """Stufe 2: Kopie des Abbilds reparieren, dann Dateien daraus kopieren."""
    copy = target / f"{image.stem}-kopie.img"
    files = target / "dateien"
    session = ImageSession(copy, readonly=False)
    steps: list[Step] = [
        PythonStep(
            "Speicherplatz für die Kopie prüfen",
            ensure_free_space(target, image.stat().st_size if image.exists() else 0),
        ),
        PythonStep("Abbild kopieren (Original bleibt unverändert)", copy_image(image, copy)),
        PythonStep("Kopie verbinden", attach(session)),
        PythonStep("Dateisysteme der Kopie reparieren (fsck)", repair(session)),
        PythonStep("Dateisysteme nur lesend einhängen", mount_all(session)),
        PythonStep("Speicherplatz am Ziel prüfen", check_space(session, files)),
        PythonStep("Dateien kopieren", copy_files(session, files)),
        PythonStep("Aushängen und Kopie lösen", detach(session), always=True),
        PythonStep("Dateirechte anpassen", chown_to_user(target), always=True),
    ]
    job = Job("Kopie reparieren und Dateien wiederherstellen", image_device(image), steps)
    job.failure_hint = "Letzte Stufe: PhotoRec auf das Original-Abbild (Taste b → PhotoRec)."
    return job


def photorec_image(image: Path, target: Path) -> Job:
    """Stufe 3: PhotoRec liest das Abbild nur und sucht Dateien am Inhalt."""
    return photorec(image_device(image), target, source=image)
