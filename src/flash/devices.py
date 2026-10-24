"""Erkennung von Wechseldatenträgern (USB-Sticks, SD-Karten) über lsblk."""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field

LSBLK_COLUMNS = (
    "NAME,PATH,TYPE,SIZE,MODEL,VENDOR,SERIAL,TRAN,RM,HOTPLUG,RO,FSTYPE,LABEL,MOUNTPOINTS"
)

# Mountpunkte, die auf eine Systemplatte hindeuten. Solche Geräte werden nie angeboten.
SYSTEM_MOUNTS = ("/", "/boot", "/boot/efi", "/home", "/usr", "/var", "[SWAP]")


@dataclass
class Partition:
    name: str
    path: str
    size: int
    fstype: str | None
    label: str | None
    mountpoints: list[str] = field(default_factory=list)


@dataclass
class Device:
    name: str
    path: str
    size: int
    model: str
    vendor: str
    serial: str
    tran: str
    removable: bool
    readonly: bool
    partitions: list[Partition] = field(default_factory=list)
    mountpoints: list[str] = field(default_factory=list)

    @property
    def all_mountpoints(self) -> list[str]:
        mounts = list(self.mountpoints)
        for part in self.partitions:
            mounts.extend(part.mountpoints)
        return mounts

    @property
    def is_mounted(self) -> bool:
        return bool(self.all_mountpoints)

    @property
    def is_system(self) -> bool:
        return any(m in SYSTEM_MOUNTS for m in self.all_mountpoints)

    @property
    def kind(self) -> str:
        if self.name.startswith("mmcblk") or self.tran == "mmc":
            return "SD-Karte"
        if self.tran == "usb":
            return "USB"
        return self.tran or "?"

    @property
    def title(self) -> str:
        text = " ".join(p for p in (self.vendor, self.model) if p)
        return text or self.name

    @property
    def identity(self) -> tuple[str, int, str]:
        """Merkmale, an denen dasselbe Gerät nach erneutem Einlesen erkannt wird."""
        return (self.path, self.size, self.serial)


def _clean(value: object) -> str:
    return str(value).strip() if value else ""


def _mounts(entry: dict) -> list[str]:
    return [m for m in entry.get("mountpoints") or [] if m]


def parse_lsblk(data: dict) -> list[Device]:
    """Wandelt die JSON-Ausgabe von lsblk in Wechseldatenträger um."""
    devices = []
    for entry in data.get("blockdevices", []):
        if entry.get("type") != "disk":
            continue
        name = entry["name"]
        tran = _clean(entry.get("tran"))
        removable = bool(entry.get("rm")) or bool(entry.get("hotplug"))
        if not (removable or tran in ("usb", "mmc") or name.startswith("mmcblk")):
            continue
        partitions = [
            Partition(
                name=child["name"],
                path=child["path"],
                size=int(child.get("size") or 0),
                fstype=child.get("fstype"),
                label=child.get("label"),
                mountpoints=_mounts(child),
            )
            for child in entry.get("children", [])
            if child.get("type") == "part"
        ]
        device = Device(
            name=name,
            path=entry["path"],
            size=int(entry.get("size") or 0),
            model=_clean(entry.get("model")),
            vendor=_clean(entry.get("vendor")),
            serial=_clean(entry.get("serial")),
            tran=tran,
            removable=removable,
            readonly=bool(entry.get("ro")),
            partitions=partitions,
            mountpoints=_mounts(entry),
        )
        # Leere Kartenleser melden Größe 0 – nichts zu prüfen.
        if device.size == 0 or device.is_system:
            continue
        devices.append(device)
    return devices


def list_devices() -> list[Device]:
    result = subprocess.run(
        ["lsblk", "-J", "-b", "-o", LSBLK_COLUMNS],
        capture_output=True,
        text=True,
        check=True,
    )
    return parse_lsblk(json.loads(result.stdout))


def find_device(identity: tuple[str, int, str]) -> Device | None:
    """Liest die Geräte neu ein und sucht dasselbe Gerät wieder."""
    for device in list_devices():
        if device.identity == identity:
            return device
    return None


def human_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1000 or unit == "TB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1000
    return f"{size} B"
