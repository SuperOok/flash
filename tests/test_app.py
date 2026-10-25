from textual.widgets import DataTable

from flash import app as flash_app
from flash import jobs
from test_jobs import make_device


async def test_geraeteliste_und_aktionsmenue(monkeypatch):
    monkeypatch.setattr(flash_app, "list_devices", lambda: [make_device()])
    async with flash_app.FlashApp().run_test() as pilot:
        await pilot.pause()
        table = pilot.app.screen.query_one(DataTable)
        assert table.row_count == 1
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(pilot.app.screen, flash_app.ActionScreen)
        await pilot.press("escape")
        await pilot.pause()
        assert isinstance(pilot.app.screen, flash_app.DeviceScreen)


async def test_schnell_check_auf_image_laeuft_durch(tmp_path, monkeypatch):
    monkeypatch.setattr(flash_app, "list_devices", lambda: [])
    image = tmp_path / "disk.img"
    image.write_bytes(b"\x01" * 8 * jobs.MIB)
    job = jobs.quick_check(make_device(path=str(image), size=8 * jobs.MIB))
    async with flash_app.FlashApp().run_test() as pilot:
        screen = flash_app.RunScreen(job)
        pilot.app.push_screen(screen)
        await pilot.pause()
        await pilot.app.workers.wait_for_complete()
        await pilot.pause()
        assert not screen.running
        # dmesg kann ohne Root fehlen, wird dann aber übersprungen statt zu scheitern.
        assert screen.states[:3] == ["ok", "ok", "ok"]
        assert not [f for f in screen.failures if "Kernel" not in f[0]]


async def test_taste_a_startet_aushaengen_auch_ohne_root(monkeypatch):
    device = make_device()
    monkeypatch.setattr(flash_app, "list_devices", lambda: [device])
    monkeypatch.setattr(flash_app, "find_device", lambda identity: device)
    monkeypatch.setattr(flash_app.os, "geteuid", lambda: 1000)
    started = []
    monkeypatch.setattr(flash_app.DeviceScreen, "run_job", lambda self, job: started.append(job))
    async with flash_app.FlashApp().run_test() as pilot:
        await pilot.pause()
        await pilot.press("a")
        await pilot.pause()
        assert [job.title for job in started] == ["Aushängen und sicher entfernen"]


async def test_sicherung_oeffnen_zeigt_stufen(tmp_path, monkeypatch):
    image = tmp_path / "sdc.img"
    image.write_bytes(b"x" * 1024)
    monkeypatch.setattr(flash_app, "list_devices", lambda: [])
    monkeypatch.setattr(flash_app.images, "default_image_bases", lambda: [tmp_path])
    async with flash_app.FlashApp().run_test() as pilot:
        await pilot.pause()
        await pilot.press("b")
        await pilot.pause()
        assert isinstance(pilot.app.screen, flash_app.ImageScreen)
        assert pilot.app.screen.images == [image]
        await pilot.press("enter")
        await pilot.pause()
        screen = pilot.app.screen
        assert isinstance(screen, flash_app.ActionScreen)
        assert [a[0] for a in screen.actions] == ["files", "repair", "imgphotorec"]


async def test_reparatur_verlangt_warnung(tmp_path, monkeypatch):
    image = tmp_path / "sdc.img"
    image.write_bytes(b"x" * 1024)
    monkeypatch.setattr(flash_app, "list_devices", lambda: [])
    monkeypatch.setattr(flash_app.os, "geteuid", lambda: 0)
    started = []
    monkeypatch.setattr(flash_app.DeviceScreen, "run_job", lambda self, job: started.append(job))
    async with flash_app.FlashApp().run_test() as pilot:
        await pilot.pause()
        pilot.app.screen.start_image(image, "repair")
        await pilot.pause()
        await pilot.press("enter")  # Zielverzeichnis übernehmen
        await pilot.pause()
        assert isinstance(pilot.app.screen, flash_app.WarnScreen)
        await pilot.press("escape")
        await pilot.pause()
        assert started == []
        pilot.app.screen.start_image(image, "repair")
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        assert [job.title for job in started] == ["Kopie reparieren und Dateien wiederherstellen"]


async def test_aufraeumen_laeuft_auch_nach_fehler(monkeypatch):
    monkeypatch.setattr(flash_app, "list_devices", lambda: [])
    cleaned = []
    job = jobs.Job(
        "Test",
        make_device(),
        [
            jobs.PythonStep("scheitert", lambda ctx: "kaputt"),
            jobs.PythonStep("übersprungen", lambda ctx: None),
            jobs.PythonStep("aufräumen", lambda ctx: cleaned.append(1), always=True),
        ],
        failure_hint="Nächste Stufe probieren",
    )
    async with flash_app.FlashApp().run_test() as pilot:
        screen = flash_app.RunScreen(job)
        pilot.app.push_screen(screen)
        await pilot.pause()
        await pilot.app.workers.wait_for_complete()
        await pilot.pause()
        assert screen.states == ["failed", "skipped", "ok"]
        assert cleaned == [1]
        assert "Nächste Stufe" in str(screen.query_one("#result").render())


async def test_bestaetigung_verlangt_geraetenamen(monkeypatch):
    device = make_device()
    async with flash_app.FlashApp().run_test() as pilot:
        results = []
        pilot.app.push_screen(flash_app.ConfirmScreen(device), results.append)
        await pilot.pause()
        await pilot.press(*"sdc", "enter")
        await pilot.pause()
        assert results == []
        await pilot.press(*"sdb", "enter")
        await pilot.pause()
        assert results == [True]
