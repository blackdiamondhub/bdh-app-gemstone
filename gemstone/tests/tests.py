import asyncio
import datetime as dt
import io
import json
import pathlib
import re
import shutil
import tempfile
import threading
import time
import uuid
from unittest.mock import patch

from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from PIL import Image
from pygemstone.models import Device, DeviceState, DownloadablePattern, Folder, FolderPattern, Pattern

from bdh_sdk import INFO, ON, WARN, secrets
from bdh_sdk.testing import FakeBroker, assert_plugin_importable
from gemstone import cloud, homepage, layout, service, stylize, worker

UTC = dt.timezone.utc
NOW = dt.datetime(2026, 11, 21, 18, 0, tzinfo=UTC)


def pat(pid, name, colors=(0xFF,), animation="motionless"):
    return {"id": pid, "name": name, "colors": list(colors), "animation": animation,
            "brightness": 255, "speed": 128, "direction": 0, "backgroundColor": 0}


class FakeGemstone:
    """An in-memory Gemstone cloud with two controllers."""

    def __init__(self):
        self.devices_ = [
            Device.from_api({"id": "house", "name": "House", "hub": {"pixelCount": [150, 50]}, "online": True}),
            Device.from_api({"id": "tub", "name": "Hot Tub", "hub": {"pixelCount": 60}, "online": True}),
        ]
        self.states = {"house": {"id": "house", "onState": True, "pattern": pat("p-warm", "Warm white")},
                       "tub": {"id": "tub", "onState": False, "pattern": pat("p-blue", "Blue")}}
        self.folders_ = [Folder.from_api({"folderId": "f-ours", "name": "Our designs"}),
                         Folder.from_api({"folderId": "f-gem", "name": "Holidays", "gemstoneManaged": True})]
        self.fps = [self._fp("f-ours", pat("p-ours", "Sunset", (0xFF, 0xFF00)), NOW)]
        self.library = [DownloadablePattern.from_api({"id": "d1", "patternData": pat("p-lib", "Candy cane"),
                                                      "category": "christmas"})]
        self.writes = []  # (op, args, monotonic start, monotonic end)
        self.inflight = 0
        self.max_inflight = 0
        self.delay = 0.0

    @staticmethod
    def _fp(folder, data, created):
        return FolderPattern.from_api({"id": str(uuid.uuid4()), "folderId": folder, "patternData": data,
                                       "createdAt": int(created.timestamp())})

    async def _write(self, op, *args):
        self.inflight += 1
        self.max_inflight = max(self.max_inflight, self.inflight)
        start = time.monotonic()
        if self.delay:
            await asyncio.sleep(self.delay)
        self.inflight -= 1
        self.writes.append((op, args, start, time.monotonic()))

    def ops(self, op):
        return [w[1] for w in self.writes if w[0] == op]

    # reads
    async def devices(self):
        return list(self.devices_)

    async def device_state(self, cid):
        return DeviceState.from_api(json.loads(json.dumps(self.states[cid])))

    async def folders(self):
        return list(self.folders_)

    async def folder_patterns(self, page=1):
        return list(self.fps) if page == 1 else []

    async def downloadable_patterns(self, page=1):
        return list(self.library) if page == 1 else []

    async def account_profile(self):
        class P:
            id = "owner-1"
        return P()

    # writes
    async def set_on_state(self, cid, on):
        await self._write("on", cid, on)
        self.states[cid]["onState"] = on

    async def play_pattern(self, cid, pattern):
        await self._write("play", cid, pattern.name)
        self.states[cid].update(onState=True, pattern=dict(pattern.to_api()))

    async def save_folder(self, folder_id, body):
        await self._write("save_folder", folder_id, body["name"])
        folder = Folder.from_api(body)
        self.folders_.append(folder)
        return folder

    async def save_pattern(self, folder_id, pattern):
        await self._write("save_pattern", folder_id, pattern.id, pattern.name)
        self.fps = [fp for fp in self.fps if not (fp.folder_id == folder_id and fp.pattern.id == pattern.id)]
        self.fps.append(self._fp(folder_id, pattern.to_api(), dt.datetime.now(UTC)))

    async def delete_pattern(self, folder_id, pattern_id):
        await self._write("delete_pattern", folder_id, pattern_id)
        self.fps = [fp for fp in self.fps if not (fp.folder_id == folder_id and fp.pattern.id == pattern_id)]

    async def impulse(self, cid, lights):
        await self._write("impulse", cid, list(lights))


class Fresh:
    def setUp(self):
        self.broker = FakeBroker(install=True, app_id="gemstone").start()
        self.addCleanup(self.broker.stop)
        self.broker.settings[("gemstone", "gemstone_email")] = "me@example.com"
        self.broker.secrets[("gemstone", "gemstone_password")] = "pw"
        self.fake = FakeGemstone()
        self.logins = []

        async def factory(email, password):
            self.logins.append(email)
            return self.fake

        patch.object(cloud, "client_factory", factory).start()
        patch.object(cloud, "WRITE_GAP", 0.0).start()
        patch.object(service, "BLINK_SECONDS", 0.0).start()
        self.addCleanup(patch.stopall)
        cloud.reset()
        self.addCleanup(cloud.reset)
        self.media = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.media, True)
        media = override_settings(MEDIA_ROOT=self.media)
        media.enable()
        self.addCleanup(media.disable)

    def local(self, hour, minute=0):
        return timezone.make_aware(dt.datetime(2026, 11, 21, hour, minute))


class CloudTests(Fresh, SimpleTestCase):
    def test_credentials_come_from_app_settings(self):
        self.assertEqual(cloud.credentials(), ("me@example.com", "pw"))
        self.assertTrue(cloud.configured())
        secrets.delete("gemstone_password", app="gemstone")
        self.assertFalse(cloud.configured())
        with self.assertRaises(cloud.NotConfigured):
            service.controllers()

    def test_reads_are_cached(self):
        service.controllers()
        service.controllers()
        self.assertEqual(self.logins, ["me@example.com"])
        self.fake.devices_.pop()
        self.assertEqual(len(service.controllers()), 2)
        cloud.forget("controllers")
        self.assertEqual(len(service.controllers()), 1)

    def test_writes_are_serialized_and_spaced(self):
        cloud.WRITE_GAP = 0.15
        self.fake.delay = 0.05
        threads = [threading.Thread(target=service.power, args=(["house", "tub"], True), kwargs={"admin": True})
                   for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(self.fake.writes), 4)
        self.assertEqual(self.fake.max_inflight, 1)
        for prev, nxt in zip(self.fake.writes, self.fake.writes[1:]):
            self.assertGreaterEqual(nxt[2] - prev[3], 0.14)


class ServiceTests(Fresh, TestCase):
    def test_controllers_and_led_counts(self):
        self.assertEqual([(c["id"], c["name"], c["leds"]) for c in service.controllers()],
                         [("house", "House", 200), ("tub", "Hot Tub", 60)])
        self.assertEqual(service.names(["house", "tub"]), "House + Hot Tub")
        with self.assertRaises(service.Invalid):
            service.check_ids(["nope"])
        with self.assertRaises(service.Invalid):
            service.check_ids([])

    def test_play_applies_to_every_selected_controller_and_undoes(self):
        out = service.play(["house", "tub"], {"kind": "solid", "color": "#ff0000", "name": "Red"})
        self.assertEqual(out["on"], "House + Hot Tub")
        self.assertEqual([a[0] for a in self.fake.ops("play")], ["house", "tub"])
        self.assertEqual(self.fake.states["tub"]["pattern"]["colors"], [0xFF])
        service.undo(out["undo"])
        self.assertEqual(self.fake.states["house"]["pattern"]["name"], "Warm white")
        self.assertEqual(self.fake.states["tub"]["pattern"]["name"], "Blue")
        self.assertFalse(self.fake.states["tub"]["onState"])
        with self.assertRaises(service.Invalid):
            service.undo(out["undo"])

    def test_play_saved_and_library_patterns_by_id(self):
        service.play(["tub"], {"id": "p-lib"})
        service.play(["house"], {"id": "p-ours"})
        self.assertEqual(self.fake.ops("play"), [("tub", "Candy cane"), ("house", "Sunset")])
        with self.assertRaises(service.Invalid):
            service.play(["house"], {"id": "p-unknown"})

    def test_library_and_ours(self):
        lib = service.library()
        self.assertEqual((lib[0]["name"], lib[0]["category"]), ("Candy cane", "Christmas"))
        ours = service.ours()
        self.assertEqual(ours["patterns"][0]["colors"], ["#ff0000", "#00ff00"])
        self.assertEqual({f["id"]: f["managed"] for f in ours["folders"]}, {"f-ours": False, "f-gem": True})

    def test_brightness_replays_current_pattern(self):
        out = service.brightness(["house", "tub"], 50)
        self.assertEqual(out["brightness"], 128)
        self.assertEqual(self.fake.states["house"]["pattern"]["brightness"], 128)
        self.assertEqual(out["changed"], ["house", "tub"])

    def test_colour_round_trip(self):
        for color in ("#ff0000", "#00ff00", "#0000ff", "#123456", service.WARM_HEX):
            self.assertEqual(service.int_to_hex(service.hex_to_int(color)), color)
        with self.assertRaises(service.Invalid):
            service.hex_to_int("red")

    def test_pattern_spec_validation(self):
        p = service.pattern_from_spec({"colors": ["#ff0000"], "animation": "chase", "brightness": 50})
        self.assertEqual((p.animation, p.brightness), ("chase", 128))
        for bad in ({"colors": []}, {"colors": ["#ff0000"] * 21}, {"colors": ["#ff0000"], "animation": "x"}, None):
            with self.assertRaises(service.Invalid):
                service.pattern_from_spec(bad)

    def test_guest_design_goes_to_locked_guest_folder(self):
        spec = {"colors": ["#ff0000"], "animation": "chase"}
        out = service.save_design(spec, admin=False, folder_id="f-ours", name="Mine", now=self.local(20, 5))
        self.assertTrue(out["locked"])
        self.assertEqual(out["folder"]["name"], "Guest designs")
        self.assertEqual(out["name"], "Guest design · 2026-11-21 20:05")
        self.assertEqual([a[1] for a in self.fake.ops("save_folder")], ["Guest designs"])
        guest_id = out["folder"]["id"]
        self.assertEqual(self.fake.ops("save_pattern")[-1][0], guest_id)
        # Saving again updates the same design instead of creating another folder or pattern.
        again = service.save_design(dict(spec, colors=["#00ff00"]), admin=False, design_id=out["id"])
        self.assertEqual((again["id"], again["name"]), (out["id"], out["name"]))
        self.assertEqual(len(self.fake.ops("save_folder")), 1)
        self.assertEqual(len([fp for fp in self.fake.fps if fp.folder_id == guest_id]), 1)

    def test_guest_cannot_overwrite_house_designs(self):
        out = service.save_design({"colors": ["#ff0000"]}, admin=False, design_id="p-ours")
        self.assertNotEqual(out["id"], "p-ours")
        self.assertEqual(self.fake.states["house"]["pattern"]["name"], "Warm white")
        sunset = [fp for fp in self.fake.fps if fp.pattern.id == "p-ours"]
        self.assertEqual((sunset[0].folder_id, sunset[0].pattern.name), ("f-ours", "Sunset"))

    def test_admin_picks_folder_and_name_and_can_move(self):
        spec = {"colors": ["#ff0000"]}
        out = service.save_design(spec, admin=True, folder_id="f-ours", name="  Big   red ")
        self.assertEqual((out["name"], out["folder"]["id"], out["locked"]), ("Big red", "f-ours", False))
        new = service.new_folder("Christmas")
        self.assertEqual(service.new_folder("christmas")["id"], new["id"])
        moved = service.save_design(spec, admin=True, design_id=out["id"], folder_id=new["id"])
        self.assertEqual(moved["folder"]["name"], "Christmas")
        self.assertEqual(self.fake.ops("delete_pattern"), [("f-ours", out["id"])])
        self.assertEqual([fp.folder_id for fp in self.fake.fps if fp.pattern.name == "Big red"], [new["id"]])
        with self.assertRaises(service.Invalid):
            service.save_design(spec, admin=True, folder_id="f-gem")
        with self.assertRaises(service.Invalid):
            service.new_folder("Guest designs")
        with self.assertRaises(service.Invalid):
            service.new_folder("   ")

    def test_cleanup_deletes_only_old_guest_designs(self):
        guest = Folder.from_api({"folderId": "f-guest", "name": "Guest designs"})
        self.fake.folders_.append(guest)
        self.fake.fps += [
            self.fake._fp("f-guest", pat("g-old", "Guest old"), NOW - dt.timedelta(days=31)),
            self.fake._fp("f-guest", pat("g-new", "Guest new"), NOW - dt.timedelta(days=29)),
            self.fake._fp("f-ours", pat("o-old", "House old"), NOW - dt.timedelta(days=400)),
        ]
        self.assertEqual(service.cleanup(NOW), 1)
        self.assertEqual(self.fake.ops("delete_pattern"), [("f-guest", "g-old")])
        self.assertEqual(sorted(fp.pattern.id for fp in self.fake.fps), ["g-new", "o-old", "p-ours"])

    def test_cleanup_without_guest_folder_does_nothing(self):
        self.assertEqual(service.cleanup(NOW), 0)
        self.assertEqual(self.fake.ops("save_folder"), [])

    def test_guest_keep_on_is_capped_at_ten(self):
        out = service.set_until(["house"], "23:30", admin=False, now=self.local(20))
        self.assertEqual(dt.datetime.fromisoformat(out["at"]), self.local(22))
        self.assertTrue(out["guest"])
        with self.assertRaises(service.Forbidden):
            service.set_until(["house"], "", admin=False, now=self.local(20))
        admin = service.set_until(["house"], "23:30", admin=True, now=self.local(20))
        self.assertEqual(dt.datetime.fromisoformat(admin["at"]), self.local(23, 30))
        self.assertIsNone(service.set_until(["house"], None, admin=True))
        with self.assertRaises(service.Invalid):
            service.set_until(["house"], "25:00", admin=True)

    def test_guest_turning_on_late_gets_a_short_grace(self):
        service.power(["tub"], True, admin=False, now=self.local(22, 30))
        self.assertEqual(dt.datetime.fromisoformat(service.until()["at"]), self.local(22, 45))
        self.assertFalse(service.enforce_until(self.local(22, 40)))
        self.assertTrue(service.enforce_until(self.local(22, 46)))
        self.assertFalse(self.fake.states["tub"]["onState"])
        self.assertIsNone(service.until())

    def test_admin_power_sets_no_limit_and_off_clears_it(self):
        service.power(["house"], True, admin=True)
        self.assertIsNone(service.until())
        service.power(["house", "tub"], True, admin=False, now=self.local(19))
        service.power(["house"], False, admin=True)
        self.assertEqual(service.until()["ids"], ["tub"])

    def test_blink_lights_the_start_then_restores(self):
        service.blink("house", 195, wait=True)
        (cid, lights), = self.fake.ops("impulse")
        self.assertEqual(cid, "house")
        from pygemstone import encode_lights
        self.assertEqual(lights, encode_lights(range(195, 200)))
        self.assertEqual(self.fake.ops("play"), [("house", "Warm white")])
        self.assertTrue(self.fake.states["house"]["onState"])

    def test_blink_restores_off_controller_to_off(self):
        service.blink("tub", 0, wait=True)
        self.assertEqual(self.fake.ops("impulse")[0][0], "tub")
        self.assertEqual([w[0] for w in self.fake.writes], ["impulse", "play", "on"])
        self.assertFalse(self.fake.states["tub"]["onState"])


class WorkerTests(Fresh, TestCase):
    def test_cleanup_runs_daily(self):
        self.fake.folders_.append(Folder.from_api({"folderId": "f-guest", "name": "Guest designs"}))
        self.fake.fps.append(self.fake._fp("f-guest", pat("g-old", "Old"), NOW - dt.timedelta(days=40)))
        self.assertEqual(worker.run_cleanup(NOW), 1)
        self.assertIsNone(worker.run_cleanup(NOW + dt.timedelta(hours=23)))
        self.fake.fps.append(self.fake._fp("f-guest", pat("g-old2", "Old"), NOW - dt.timedelta(days=40)))
        self.assertEqual(worker.run_cleanup(NOW + dt.timedelta(days=1, minutes=1)), 1)

    def test_cleanup_skipped_when_not_configured(self):
        secrets.delete("gemstone_password", app="gemstone")
        self.assertIsNone(worker.run_cleanup(NOW))

    def test_should_start(self):
        self.assertTrue(worker.should_start(["/x/bdh_sdk/runtime/__main__.py"]))
        self.assertFalse(worker.should_start(["manage.py", "test"]))
        with patch.dict("os.environ", {"BDH_GEMSTONE_WORKER": "0"}):
            self.assertFalse(worker.should_start(["/x/bdh_sdk/runtime/__main__.py"]))


def jpeg(w=800, h=400, color=(200, 150, 100)):
    buf = io.BytesIO()
    Image.new("RGB", (w, h), color).save(buf, "JPEG")
    return buf.getvalue()


class LayoutTests(Fresh, TestCase):
    def test_default_is_one_straight_run(self):
        data = layout.get("house", 200)
        self.assertFalse(data["photo"])
        self.assertEqual(len(data["runs"]), 1)
        self.assertEqual(data["runs"][0]["leds"], 200)

    def test_save_and_validate_runs(self):
        runs = [{"name": "Front", "points": [[0, 0.5], [0.5, 0.2], [1, 0.5]], "leds": 150},
                {"name": "", "points": [[0.1, 0.9], [0.9, 0.9]], "leds": 50, "reverse": True}]
        data = layout.save("house", runs=runs, leds=200)
        self.assertEqual(data["runs"][1]["name"], "Run 2")
        self.assertTrue(data["runs"][1]["reverse"])
        self.assertEqual(layout.get("house")["runs"][0]["points"][1], [0.5, 0.2])
        self.assertEqual(layout.start_offset("house", 1), 150)
        bad = [[], [{"points": [[0, 0]], "leds": 10}], [{"points": [[0, 0], [2, 0]], "leds": 10}],
               [{"points": [[0, 0], [1, 0]], "leds": 0}], [{"points": [[0, 0], [1, 0]]}] * 9]
        for runs in bad:
            with self.assertRaises(layout.Invalid):
                layout.clean_runs(runs)
        with self.assertRaises(layout.Invalid):
            layout.check_cid("../etc")
        with self.assertRaises(layout.Invalid):
            layout.save("house", look="sepia")

    def test_photo_upload_stylizes_and_restyles(self):
        data = layout.upload_photo("house", jpeg(2400, 1200), "night", 200)
        self.assertTrue(data["photo"])
        self.assertEqual(data["aspect"], 2.0)
        night = Image.open(io.BytesIO(layout.photo_bytes("house")))
        self.assertEqual(night.size, (stylize.MAX_WIDTH, 800))
        r, g, b = night.convert("RGB").resize((1, 1)).getpixel((0, 0))
        self.assertLess(r + g + b, 200 + 150 + 100)  # darker than the photo
        grey = stylize.night(Image.new("RGB", (200, 100), (120, 120, 120)))
        r, g, b = grey.getpixel((100, 50))
        self.assertGreater(b, r)  # blue night tint
        v = data["v"]
        data = layout.save("house", look="original", leds=200)
        self.assertEqual(data["v"], v + 1)
        r, g, b = Image.open(io.BytesIO(layout.photo_bytes("house"))).convert("RGB").resize((1, 1)).getpixel((0, 0))
        self.assertGreater(r, b)
        data = layout.remove_photo("house", 200)
        self.assertFalse(data["photo"])
        self.assertIsNone(layout.photo_bytes("house"))

    def test_bad_photo_is_rejected(self):
        with self.assertRaises(layout.Invalid):
            layout.upload_photo("house", b"not an image")
        for look in stylize.LOOKS:
            self.assertEqual(stylize.render(stylize.load(jpeg(100, 50)), look).size, (100, 50))


class ViewTests(Fresh, TestCase):
    def post(self, name, body=None, *args):
        return self.client.post(reverse(name, args=args), body or {}, content_type="application/json")

    def test_index_renders_state(self):
        page = self.client.get(reverse("gemstone_index"))
        self.assertContains(page, 'id="gs-initial"')
        self.assertContains(page, "Hot Tub")
        self.assertNotContains(page, "Schedule")

    def test_state_when_not_configured(self):
        secrets.delete("gemstone_password", app="gemstone")
        data = self.client.get(reverse("gemstone_state")).json()
        self.assertFalse(data["configured"])
        self.assertEqual(data["controllers"], [])
        self.assertEqual(self.post("gemstone_play", {"ids": ["house"]}).status_code, 409)

    def test_state_lists_controllers_with_layout(self):
        data = self.client.get(reverse("gemstone_state")).json()
        self.assertFalse(data["admin"])
        house = data["controllers"][0]
        self.assertEqual((house["leds"], house["state"]["on"]), (200, True))
        self.assertEqual(house["layout"]["runs"][0]["leds"], 200)

    def test_play_and_errors(self):
        r = self.post("gemstone_play", {"ids": ["house", "tub"], "pattern": {"kind": "solid", "color": "#00ff00"}})
        self.assertEqual(r.json()["on"], "House + Hot Tub")
        self.assertEqual(self.post("gemstone_play", {"ids": ["x"], "pattern": {}}).status_code, 400)
        self.assertEqual(self.client.get(reverse("gemstone_play")).status_code, 405)

    def test_guests_cannot_use_admin_endpoints(self):
        self.assertEqual(self.post("gemstone_folders", {"name": "X"}).status_code, 403)
        self.assertEqual(self.post("gemstone_layout", {"runs": []}, "house").status_code, 403)
        self.assertEqual(self.post("gemstone_blink", {}, "house").status_code, 403)
        r = self.client.post(reverse("gemstone_photo_upload", args=["house"]), {"photo": io.BytesIO(jpeg())})
        self.assertEqual(r.status_code, 403)
        self.assertEqual(self.client.get(reverse("gemstone_layout", args=["house"])).status_code, 200)
        self.assertEqual(self.client.get(reverse("gemstone_layout", args=["nope"])).status_code, 404)

    def test_guest_design_ignores_folder_and_name(self):
        r = self.post("gemstone_design", {"pattern": {"colors": ["#ff0000"]}, "folder": "f-ours", "name": "Mine"})
        self.assertEqual(r.json()["folder"]["name"], "Guest designs")
        self.assertTrue(r.json()["name"].startswith("Guest design · "))
        folders = self.client.get(reverse("gemstone_ours")).json()["folders"]
        self.assertEqual({f["name"]: f["locked"] for f in folders},
                         {"Our designs": True, "Holidays": True, "Guest designs": False})

    def test_guest_cannot_clear_limit(self):
        self.assertEqual(self.post("gemstone_until", {"ids": ["house"], "at": ""}).status_code, 403)

    @patch("gemstone.views.is_admin", return_value=True)
    def test_admin_setup(self, _admin):
        self.assertTrue(self.client.get(reverse("gemstone_state")).json()["admin"])
        r = self.post("gemstone_design", {"pattern": {"colors": ["#ff0000"]}, "folder": "f-ours", "name": "Mine"})
        self.assertEqual((r.json()["folder"]["name"], r.json()["name"]), ("Our designs", "Mine"))
        self.assertEqual(self.post("gemstone_folders", {"name": "Party"}).json()["name"], "Party")
        runs = [{"points": [[0, 0], [1, 0]], "leds": 120}, {"points": [[0, 1], [1, 1]], "leds": 80}]
        self.assertEqual(self.post("gemstone_layout", {"runs": runs}, "house").json()["runs"][1]["leds"], 80)
        self.assertEqual(self.post("gemstone_layout", {"runs": [{"points": [[0, 0]]}]}, "house").status_code, 400)
        r = self.client.post(reverse("gemstone_photo_upload", args=["house"]),
                             {"photo": io.BytesIO(jpeg()), "look": "drawing"})
        self.assertEqual(r.json()["look"], "drawing")
        photo = self.client.get(reverse("gemstone_photo", args=["house"]))
        self.assertEqual(photo["Content-Type"], "image/jpeg")
        self.assertEqual(self.client.get(reverse("gemstone_photo", args=["tub"])).status_code, 404)
        with patch.object(service, "blink") as blink:
            self.assertEqual(self.post("gemstone_blink", {"run": 1}, "house").status_code, 200)
        blink.assert_called_once_with("house", 120)
        self.assertEqual(self.post("gemstone_blink", {"run": 5}, "house").status_code, 400)


class HomepageTests(Fresh, TestCase):
    def test_status(self):
        s = homepage.status()
        self.assertEqual((s.text, s.level), ("Warm white on House", ON))
        self.fake.states["house"]["onState"] = False
        cloud.forget()
        self.assertEqual(homepage.status().level, INFO)
        secrets.delete("gemstone_password", app="gemstone")
        self.assertEqual(homepage.status().level, WARN)


class PluginTests(SimpleTestCase):
    def test_plugin_importable(self):
        assert_plugin_importable("gemstone.plugin")

    def test_manifest(self):
        from gemstone.plugin import spec
        self.assertEqual(spec.network, "outbound")
        self.assertEqual({s.key for s in spec.settings}, {"gemstone_email", "gemstone_password"})


ROOT = pathlib.Path(__file__).resolve().parent.parent


class HubFrameLayoutTests(SimpleTestCase):
    """The hub shows apps in a full-height frame with no navbar inside it."""

    def test_no_navbar_offsets(self):
        for path in list(ROOT.glob("templates/**/*.html")) + list(ROOT.glob("static/**/*.css")):
            text = path.read_text(encoding="utf-8").replace(" ", "")
            for bad in ("top:62px", "top:64px", "118px", "100vh-6", ".navbar"):
                self.assertNotIn(bad, text, f"{path.name}: {bad}")


class PhoneLayoutTests(SimpleTestCase):
    def test_grid_columns_can_shrink(self):
        css = list(ROOT.glob("static/**/*.css"))
        self.assertTrue(css)
        for path in css:
            for cols in re.findall(r"grid-template-columns\s*:([^;}]*)", path.read_text(encoding="utf-8")):
                bare = re.sub(r"minmax\(\s*0\s*,\s*[\d.]+fr\s*\)", "", cols)
                self.assertNotRegex(bare, r"\d+fr", f"{path.name}: {cols.strip()}")

    def test_has_phone_layout(self):
        css = (ROOT / "static/gemstone/gemstone.css").read_text(encoding="utf-8")
        self.assertIn("@media(max-width:700px)", css.replace(" ", ""))
