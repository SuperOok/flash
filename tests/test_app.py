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
