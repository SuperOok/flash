from flash.devices import human_size, parse_lsblk


def disk(name, tran="usb", rm=True, size=32_000_000_000, children=None, mounts=None):
    return {
        "name": name,
        "path": f"/dev/{name}",
        "type": "disk",
        "size": size,
        "model": "Ultra ",
        "vendor": "SanDisk ",
        "serial": "1234",
        "tran": tran,
        "rm": rm,
        "hotplug": rm,
        "ro": False,
        "fstype": None,
        "label": None,
        "mountpoints": mounts or [None],
        "children": children or [],
    }


def part(name, fstype="vfat", mounts=None):
    return {
        "name": name,
        "path": f"/dev/{name}",
        "type": "part",
        "size": 1000,
        "fstype": fstype,
        "label": "STICK",
        "mountpoints": mounts or [None],
    }


def test_usb_stick_wird_erkannt():
    devices = parse_lsblk({"blockdevices": [disk("sdb", children=[part("sdb1")])]})
    assert len(devices) == 1
    stick = devices[0]
    assert stick.title == "SanDisk Ultra"
    assert stick.kind == "USB"
    assert stick.partitions[0].fstype == "vfat"
    assert not stick.is_mounted


def test_sd_karte_ohne_tran_wird_erkannt():
    devices = parse_lsblk({"blockdevices": [disk("mmcblk0", tran=None, rm=False)]})
    assert [d.kind for d in devices] == ["SD-Karte"]


def test_interne_platten_und_loops_werden_ignoriert():
    data = {
        "blockdevices": [
            disk("nvme0n1", tran="nvme", rm=False),
            {**disk("loop0"), "type": "loop"},
        ]
    }
    assert parse_lsblk(data) == []


def test_systemplatte_wird_nie_angeboten():
    # Auch ein per USB angeschlossenes System darf nicht auswählbar sein.
    system = disk("sda", children=[part("sda1", "ext4", mounts=["/"])])
    assert parse_lsblk({"blockdevices": [system]}) == []


def test_leerer_kartenleser_wird_ignoriert():
    assert parse_lsblk({"blockdevices": [disk("sdc", size=0)]}) == []


def test_eingehaengte_partition():
    stick = disk("sdb", children=[part("sdb1", mounts=["/media/x/STICK"])])
    device = parse_lsblk({"blockdevices": [stick]})[0]
    assert device.is_mounted
    assert device.all_mountpoints == ["/media/x/STICK"]


def test_human_size():
    assert human_size(512) == "512 B"
    assert human_size(32_000_000_000) == "32.0 GB"
