import os
import sys

from flash import jobs
from flash.devices import Device, Partition
from flash.jobs import CommandStep, StepContext
from flash.runner import handle_line, run_command


def make_device(path="/dev/sdb", size=4 * jobs.MIB, partitions=None):
    return Device(
        name=os.path.basename(path),
        path=path,
        size=size,
        model="Ultra",
        vendor="SanDisk",
        serial="1234",
        tran="usb",
        removable=True,
        readonly=False,
        partitions=partitions or [],
    )


def collect():
    logs, progress, status = [], [], []
    ctx = StepContext(logs.append, progress.append)
    return ctx, logs, progress, status


def test_badblocks_fortschritt_geht_in_status_nicht_ins_log():
    step = jobs.full_read(make_device()).steps[1]
    ctx, logs, progress, status = collect()
    handle_line(step, ctx, "  12.34% done, 0:05 elapsed. (0/0/0 errors)", status.append)
    assert progress == [12.34]
    assert status and not logs


def test_badblocks_fehler_wird_erkannt():
    step = jobs.full_read(make_device()).steps[1]
    ctx, *_ , status = collect()
    assert handle_line(step, ctx, "Pass completed, 0 bad blocks found. (0/0/0 errors)", status.append) is None
    assert handle_line(step, ctx, "Pass completed, 7 bad blocks found. (7/0/0 errors)", status.append)


def test_ddrescue_status():
    step = jobs.rescue(make_device(), jobs.Path("/tmp/x")).steps[2]
    ctx, logs, progress, status = collect()
    handle_line(step, ctx, "\x1b[A pct rescued:   45.50%, read errors:        0", status.append)
    assert progress == [45.5]
    assert not logs


def test_f3probe_fake_wird_erkannt():
    step = jobs.intensive(make_device()).steps[2]
    ctx, *_ , status = collect()
    assert handle_line(step, ctx, "Bad news: The device `/dev/sdb' is a counterfeit", status.append)


def test_quick_check_ueberspringt_eingehaengte_partitionen():
    device = make_device(
        partitions=[
            Partition("sdb1", "/dev/sdb1", 1, "vfat", None, ["/media/x"]),
            Partition("sdb2", "/dev/sdb2", 1, "ext4", None, []),
            Partition("sdb3", "/dev/sdb3", 1, "unbekannt", None, []),
        ]
    )
    titles = [s.title for s in jobs.quick_check(device).steps]
    assert any("sdb1: übersprungen" in t for t in titles)
    assert any("sdb2 prüfen" in t for t in titles)
    assert not any("sdb3" in t for t in titles)


def test_nur_intensiv_check_ist_destruktiv():
    device = make_device()
    assert jobs.intensive(device).destructive
    assert jobs.intensive(device, thorough=True).destructive
    for build in (jobs.quick_check, jobs.full_read):
        job = build(device)
        assert not job.destructive
        argv = " ".join(" ".join(s.argv) for s in job.steps if isinstance(s, CommandStep))
        assert "-w" not in argv.split()


def test_stichproben_auf_imagedatei(tmp_path):
    image = tmp_path / "disk.img"
    image.write_bytes(os.urandom(4 * jobs.MIB))
    device = make_device(path=str(image), size=4 * jobs.MIB)
    ctx, logs, progress, _ = collect()
    assert jobs.sample_read(device, samples=10)(ctx) is None
    assert progress[-1] == 100
    assert any("Lesefehler: 0" in line for line in logs)


async def test_run_command_liefert_exit_code():
    ctx, logs, *_ = collect()
    ok = CommandStep("ok", [sys.executable, "-c", "print('hallo')"])
    assert await run_command(ok, ctx, lambda s: None) is None
    assert "hallo" in logs
    bad = CommandStep("bad", [sys.executable, "-c", "raise SystemExit(3)"])
    assert await run_command(bad, ctx, lambda s: None) == "Exit-Code 3"
