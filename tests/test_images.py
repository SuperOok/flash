import json
import os
import subprocess
from pathlib import Path

import pytest

from flash import images, jobs
from flash.jobs import StepContext


def collect():
    logs, progress = [], []
    return StepContext(logs.append, progress.append), logs, progress


class FakeSystem:
    """Simuliert losetup, lsblk, blkid und mount, damit Abläufe ohne Root testbar sind.

    `mount` legt die Dateien aus `content` im Mountpunkt an, `umount` räumt sie weg.
    """

    def __init__(self, content=None, mount_ok=True, fstype="exfat"):
        self.calls = []
        self.content = content or {"DCIM/bild.jpg": b"jpeg" * 1000, "notiz.txt": b"hallo"}
        self.mount_ok = mount_ok
        self.fstype = fstype

    def __call__(self, argv):
        self.calls.append(argv)
        out, code, err = "", 0, ""
        if argv[0] == "losetup" and "--find" in argv:
            out = "/dev/loop9\n"
        elif argv[0] == "lsblk":
            out = json.dumps(
                {"blockdevices": [{"path": "/dev/loop9", "children": [{"path": "/dev/loop9p1", "type": "part"}]}]}
            )
        elif argv[0] == "blkid":
            out = f"TYPE={self.fstype}\nLABEL=Stick\nUSAGE=filesystem\n"
        elif argv[0] == "mount":
            if self.mount_ok:
                for name, data in self.content.items():
                    path = Path(argv[-1]) / name
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(data)
            else:
                code, err = 32, "wrong fs type, bad superblock"
        elif argv[0] == "umount":
            for item in sorted(Path(argv[-1]).rglob("*"), reverse=True):
                item.rmdir() if item.is_dir() else item.unlink()
        return subprocess.CompletedProcess(argv, code, out, err)

    def losetup_calls(self):
        return [c for c in self.calls if c[0] == "losetup" and "--find" in c]


@pytest.fixture
def system(monkeypatch):
    fake = FakeSystem()
    monkeypatch.setattr(images, "_run", fake)
    monkeypatch.setattr(images, "run_logged", lambda ctx, argv: fake.calls.append(argv) or 0)
    return fake


@pytest.fixture
def image(tmp_path):
    path = tmp_path / "sicherung" / "sdc.img"
    path.parent.mkdir()
    path.write_bytes(b"\x00" * 8 * jobs.MIB + b"daten" + b"\x00" * 4 * jobs.MIB)
    return path


def run_steps(job):
    """Führt die Schritte wie RunScreen aus: nach Fehler nur noch `always`-Schritte."""
    ctx, logs, _ = collect()
    results, failed = [], False
    for step in job.steps:
        if failed and not step.always:
            results.append("skipped")
            continue
        error = step.func(ctx)
        failed = failed or bool(error)
        results.append(error)
    return results, logs


def test_blkid_ausgabe():
    info = images.parse_blkid('LABEL=Stick\nUUID=1234-ABCD\nTYPE=exfat\n')
    assert info == {"LABEL": "Stick", "UUID": "1234-ABCD", "TYPE": "exfat"}


def test_partitionen_des_loop_geraets():
    data = {"blockdevices": [{"path": "/dev/loop0", "children": [
        {"path": "/dev/loop0p1", "type": "part"},
        {"path": "/dev/loop0p2", "type": "part"},
    ]}]}
    assert images.parse_partitions(data) == ["/dev/loop0p1", "/dev/loop0p2"]
    assert images.parse_partitions({"blockdevices": [{"path": "/dev/loop0"}]}) == []


def test_abbilder_werden_gefunden(tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "sdc.img").write_bytes(b"x")
    (tmp_path / "direkt.img").write_bytes(b"x")
    (tmp_path / "a" / "sdc.map").write_text("")
    names = {p.name for p in images.find_images(tmp_path, tmp_path / "fehlt")}
    assert names == {"sdc.img", "direkt.img"}


def test_wiederherstellen_kopiert_dateien_und_laesst_original_unveraendert(system, image, tmp_path):
    before = image.read_bytes()
    target = tmp_path / "ziel"
    results, _ = run_steps(images.restore_files(image, target))
    assert results == [None] * 6
    assert (target / "Stick" / "notiz.txt").read_bytes() == b"hallo"
    assert (target / "Stick" / "DCIM" / "bild.jpg").read_bytes() == b"jpeg" * 1000
    assert image.read_bytes() == before
    # Original nur schreibgeschützt verbunden, danach wieder gelöst.
    assert system.losetup_calls() == [
        ["losetup", "--find", "--show", "--partscan", "--read-only", str(image)]
    ]
    assert ["losetup", "--detach", "/dev/loop9"] in system.calls
    assert ["mount", "-o", "ro"] == [c for c in system.calls if c[0] == "mount"][0][:3]


def test_nicht_einhaengbar_prueft_nur_lesend_und_raeumt_auf(system, image, tmp_path):
    system.mount_ok = False
    results, logs = run_steps(images.restore_files(image, tmp_path / "ziel"))
    assert results[1] == "Kein Dateisystem einhängbar – vermutlich beschädigt"
    assert results[2:4] == ["skipped", "skipped"]
    # Aufräumen und Dateirechte laufen trotzdem.
    assert results[4:] == [None, None]
    assert ["losetup", "--detach", "/dev/loop9"] in system.calls
    assert ["fsck.exfat", "-n", "-v", "/dev/loop9p1"] in system.calls
    mountpoint = Path([c for c in system.calls if c[0] == "mount"][0][-1])
    assert not mountpoint.exists()


def test_ext4_wird_ohne_journal_eingehaengt(system, image, tmp_path):
    system.fstype = "ext4"
    run_steps(images.restore_files(image, tmp_path / "ziel"))
    assert [c for c in system.calls if c[0] == "mount"][0][:3] == ["mount", "-o", "ro,noload"]


def test_reparatur_arbeitet_nur_an_kopie(system, image, tmp_path):
    before = image.read_bytes()
    target = tmp_path / "reparatur"
    results, _ = run_steps(images.repair_and_restore(image, target))
    assert results == [None] * 9
    copy = target / "sdc-kopie.img"
    assert copy.read_bytes() == before
    assert image.read_bytes() == before
    # Beschreibbar verbunden wird ausschließlich die Kopie.
    assert system.losetup_calls() == [["losetup", "--find", "--show", "--partscan", str(copy)]]
    assert ["fsck.exfat", "-y", "-v", "/dev/loop9p1"] in system.calls
    assert (target / "dateien" / "Stick" / "notiz.txt").read_bytes() == b"hallo"


def test_kopie_ueberschreibt_nichts(image, tmp_path):
    ctx, *_ = collect()
    existing = tmp_path / "vorhanden.img"
    existing.write_bytes(b"wichtig")
    assert "existiert bereits" in images.copy_image(image, existing)(ctx)
    assert existing.read_bytes() == b"wichtig"


def test_kopie_ist_sparse_und_identisch(image, tmp_path):
    ctx, _, progress = collect()
    copy = tmp_path / "kopie.img"
    assert images.copy_image(image, copy)(ctx) is None
    assert copy.read_bytes() == image.read_bytes()
    assert progress[-1] == 100
    assert os.stat(copy).st_blocks * 512 < copy.stat().st_size


def test_kopieren_ueberspringt_unlesbare_dateien(tmp_path):
    if os.geteuid() == 0:
        pytest.skip("Root kann jede Datei lesen")
    src = tmp_path / "quelle"
    (src / "ordner").mkdir(parents=True)
    (src / "ordner" / "gut.txt").write_text("gut")
    (src / "kaputt.txt").write_text("x")
    (src / "kaputt.txt").chmod(0)
    (src / "link").symlink_to("ordner/gut.txt")
    ctx, logs, _ = collect()
    failed = images.copy_tree(src, tmp_path / "ziel", ctx, [0], 10)
    assert failed == ["kaputt.txt"]
    assert (tmp_path / "ziel" / "ordner" / "gut.txt").read_text() == "gut"
    assert os.readlink(tmp_path / "ziel" / "link") == "ordner/gut.txt"
    assert not (tmp_path / "ziel" / "kaputt.txt").exists()


def test_pfad_aus_zwischenablage_bereinigen():
    from flash.clipboard import clean_path

    assert clean_path("  /mnt/data/flash-rescue \n") == "/mnt/data/flash-rescue"
    assert clean_path("'/mnt/data/a b.img'") == "/mnt/data/a b.img"
    assert clean_path("file:///home/x/sdc%20alt.img\nfile:///zweite") == "/home/x/sdc alt.img"
    assert clean_path("\n\n") == ""
