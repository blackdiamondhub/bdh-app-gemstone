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


# Fixture ids the fake cloud "already has"; anything the app sends must be a UUID.
FIXTURE_IDS = {"p-warm", "p-blue", "p-ours", "p-lib"}


def cloud_pattern_id(pid):
    """The real cloud 400s with "Pattern Id is not a valid UUID" otherwise."""
    if pid in FIXTURE_IDS:
        return
    try:
        uuid.UUID(str(pid))
    except ValueError:
        from pygemstone import GemstoneValueError
        raise GemstoneValueError("Invalid Pattern property: Value for Pattern Id is not a valid UUID.")


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
        cloud_pattern_id(pattern.id)
        await self._write("play", cid, pattern.name)
        self.states[cid].update(onState=True, pattern=dict(pattern.to_api()))

    async def save_folder(self, folder_id, body):
        await self._write("save_folder", folder_id, body["name"])
        folder = Folder.from_api(body)
        self.folders_.append(folder)
        return folder

    async def save_pattern(self, folder_id, pattern):
        cloud_pattern_id(pattern.id)
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
        data = layout.get("house")
        self.assertFalse(data["photo"])
        self.assertEqual(list(data["runs"][0]), ["name", "points"])

    def test_save_and_validate_runs(self):
        runs = [{"name": "Front", "points": [[0, 0.5], [0.5, 0.2], [1, 0.5]]},
                {"name": "", "points": [[0.1, 0.9], [0.9, 0.9]], "leds": 50, "reverse": True}]
        data = layout.save("house", runs)
        self.assertEqual(data["runs"][1], {"name": "Run 2", "points": [[0.1, 0.9], [0.9, 0.9]]})
        self.assertEqual(layout.get("house")["runs"][0]["points"][1], [0.5, 0.2])
        bad = [[], [{"points": [[0, 0]]}], [{"points": [[0, 0], [2, 0]]}], [{"points": "x"}],
               [{"points": [[0, 0], [1, 0]]}] * 9, [{"points": [[0, 0]] * 201}]]
        for runs in bad:
            with self.assertRaises(layout.Invalid):
                layout.clean_runs(runs)
        with self.assertRaises(layout.Invalid):
            layout.check_cid("../etc")

    def test_photo_is_kept_as_uploaded(self):
        data = layout.upload_photo("house", jpeg(2400, 1200))
        self.assertTrue(data["photo"])
        self.assertEqual(data["aspect"], 2.0)
        img = Image.open(io.BytesIO(layout.photo_bytes("house"))).convert("RGB")
        self.assertEqual(img.size, (stylize.MAX_WIDTH, 800))
        r, g, b = img.resize((1, 1)).getpixel((0, 0))
        self.assertLess(abs(r - 200) + abs(g - 150) + abs(b - 100), 12)  # same colours, no restyle
        v = data["v"]
        self.assertEqual(layout.upload_photo("house", jpeg())["v"], v + 1)
        data = layout.remove_photo("house")
        self.assertFalse(data["photo"])
        self.assertNotIn("aspect", data)
        self.assertIsNone(layout.photo_bytes("house"))

    def test_old_restyled_layout_goes_back_to_the_original_photo(self):
        from bdh_sdk.storage import app_media, app_state
        from django.core.files.base import ContentFile
        media = app_media("gemstone")
        media.save("photos/house.jpg", ContentFile(jpeg(color=(10, 10, 60))))  # the old "night" render
        media.save("photos/house.orig.jpg", ContentFile(jpeg(color=(200, 150, 100))))
        app_state("gemstone").set("layout:house", {
            "photo": True, "v": 3, "look": "night", "aspect": 2.0,
            "runs": [{"name": "Eave", "points": [[0, 0], [1, 0]], "leds": 150, "reverse": True}]})
        data = layout.get("house")
        self.assertEqual(data, {"photo": True, "v": 4, "aspect": 2.0,
                                "runs": [{"name": "Eave", "points": [[0, 0], [1, 0]]}]})
        r, g, b = Image.open(io.BytesIO(layout.photo_bytes("house"))).convert("RGB").resize((1, 1)).getpixel((0, 0))
        self.assertGreater(r, b)
        self.assertFalse(media.exists("photos/house.orig.jpg"))
        self.assertEqual(layout.get("house")["v"], 4)  # migrated once, not on every read

    def test_old_original_look_just_drops_the_old_keys(self):
        from bdh_sdk.storage import app_state
        app_state("gemstone").set("layout:tub", {"photo": False, "v": 1, "look": "original",
                                                 "runs": [{"name": "A", "points": [[0, 0], [1, 1]], "leds": 9}]})
        self.assertEqual(layout.get("tub"), {"photo": False, "v": 1, "runs": [{"name": "A", "points": [[0, 0], [1, 1]]}]})

    def test_bad_photo_is_rejected(self):
        with self.assertRaises(layout.Invalid):
            layout.upload_photo("house", b"not an image")


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
        self.assertNotIn("leds", house["layout"]["runs"][0])

    def test_play_and_errors(self):
        r = self.post("gemstone_play", {"ids": ["house", "tub"], "pattern": {"kind": "solid", "color": "#00ff00"}})
        self.assertEqual(r.json()["on"], "House + Hot Tub")
        self.assertEqual(self.post("gemstone_play", {"ids": ["x"], "pattern": {}}).status_code, 400)
        self.assertEqual(self.client.get(reverse("gemstone_play")).status_code, 405)

    def test_every_write_sends_ids_the_cloud_accepts(self):
        """Editing a design failed on every change: live/solid pattern ids weren't UUIDs."""
        names = [a for _, a in service.ANIMATIONS] + list(service.MORE_ANIMATIONS)
        for anim in names:
            spec = {"colors": ["#ff0000", "#00ff00"], "animation": anim}
            r = self.post("gemstone_live", {"ids": ["house", "tub"], "pattern": spec})
            self.assertEqual(r.status_code, 200, (anim, r.content))
        for ref in ({"kind": "solid", "color": "#00ff00", "name": "Green"},
                    {"kind": "solid", "color": service.WARM_HEX},
                    {"kind": "spec", "spec": {"colors": ["#0000ff"], "animation": "chase"}}):
            r = self.post("gemstone_play", {"ids": ["house"], "pattern": ref})
            self.assertEqual(r.status_code, 200, (ref, r.content))
        r = self.post("gemstone_design", {"pattern": {"colors": ["#ff0000"], "animation": "starry"}})
        self.assertEqual(r.status_code, 200, r.content)
        again = self.post("gemstone_design", {"id": r.json()["id"], "pattern": {"colors": ["#00ff00"]}})
        self.assertEqual(again.status_code, 200, again.content)

    def test_solid_ids_match_the_ui(self):
        """gemstone.js marks the playing swatch by comparing against solidId(hex)."""
        sid = service.solid_id("#A0B1C2")
        self.assertEqual(sid, str(uuid.UUID(sid)))
        js = (ROOT / "static/gemstone/gemstone.js").read_text(encoding="utf-8")
        prefix = re.search(r"const solidId = \(hex\) => '([0-9a-f-]+)'", js).group(1)
        self.assertEqual(prefix + "a0b1c2", sid)

    def test_guests_cannot_use_admin_endpoints(self):
        self.assertEqual(self.post("gemstone_folders", {"name": "X"}).status_code, 403)
        self.assertEqual(self.post("gemstone_layout", {"runs": []}, "house").status_code, 403)
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
        runs = [{"points": [[0, 0], [1, 0]]}, {"points": [[0, 1], [1, 1]], "leds": 80}]
        self.assertEqual(self.post("gemstone_layout", {"runs": runs}, "house").json()["runs"][1],
                         {"name": "Run 2", "points": [[0, 1], [1, 1]]})
        self.assertEqual(self.post("gemstone_layout", {"runs": [{"points": [[0, 0]]}]}, "house").status_code, 400)
        r = self.client.post(reverse("gemstone_photo_upload", args=["house"]), {"photo": io.BytesIO(jpeg())})
        self.assertTrue(r.json()["photo"])
        self.assertNotIn("look", r.json())
        photo = self.client.get(reverse("gemstone_photo", args=["house"]))
        self.assertEqual(photo["Content-Type"], "image/jpeg")
        self.assertEqual(self.client.get(reverse("gemstone_photo", args=["tub"])).status_code, 404)
        r = self.client.post(reverse("gemstone_photo_upload", args=["house"]), {"remove": "1"})
        self.assertFalse(r.json()["photo"])
        self.assertEqual(self.client.post("/gemstone/api/blink/house/").status_code, 404)

    def test_settings_page_is_admin_only(self):
        self.assertEqual(self.client.get(reverse("gemstone_settings")).status_code, 404)
        with patch("gemstone.views.is_admin", return_value=True):
            page = self.client.get(reverse("gemstone_settings"))
        self.assertContains(page, 'data-mode="setup"')
        self.assertContains(page, 'id="s-ed"')
        self.assertContains(page, 'id="gs-initial"')
        # A way back to the app's other hub settings (the hub draws no nav round this page).
        self.assertContains(page, 'data-bdh-nav="hub_settings_app" data-bdh-args=\'["gemstone"]\'')
        for gone in ('s-look', 's-blink', 'Flip start'):
            self.assertNotContains(page, gone)

    def test_setup_is_not_in_the_app(self):
        with patch("gemstone.views.is_admin", return_value=True):
            page = self.client.get(reverse("gemstone_index"))
        for gone in ('data-view="setup"', 'id="v-setup"', 'id="s-ed"'):
            self.assertNotContains(page, gone)


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
        # Photos & light paths live under the hub's Settings → Gemstone.
        self.assertEqual([p.url_name for p in spec.settings_pages], ["gemstone_settings"])
        self.assertEqual(reverse("gemstone_settings"), "/gemstone/settings/")

    def test_tile_is_named_after_the_product(self):
        # "Lights" was ambiguous next to the hub's other lighting apps.
        from gemstone.plugin import spec
        self.assertEqual([t.title for t in spec.tiles], ["Gemstone"])
        html = (ROOT / "templates/gemstone/index.html").read_text(encoding="utf-8")
        self.assertIn("{% block title %}Gemstone{% endblock %}", html)


ROOT = pathlib.Path(__file__).resolve().parent.parent


class HubFrameLayoutTests(SimpleTestCase):
    """The hub shows apps in a full-height frame with no navbar inside it."""

    def test_no_navbar_offsets(self):
        for path in list(ROOT.glob("templates/**/*.html")) + list(ROOT.glob("static/**/*.css")):
            text = path.read_text(encoding="utf-8").replace(" ", "")
            for bad in ("top:62px", "top:64px", "118px", "100vh-6", ".navbar"):
                self.assertNotIn(bad, text, f"{path.name}: {bad}")


class FrameworkClassTests(SimpleTestCase):
    """The runtime base.html loads Bootstrap, so any class we share with it
    silently picks up its styles (.card went dark, .row chips went full-width)."""

    def test_no_bootstrap_class_names(self):
        fixture = pathlib.Path(__file__).with_name("bootstrap_classes.txt").read_text(encoding="utf-8")
        bootstrap = {ln.strip() for ln in fixture.splitlines() if ln.strip() and not ln.startswith("#")}
        ours = {}
        for path in ROOT.glob("static/**/*.css"):
            text = re.sub(r"url\([^)]*\)", "", path.read_text(encoding="utf-8"))
            for name in re.findall(r"\.(-?[_a-zA-Z][\w-]*)", text):
                ours.setdefault(name, path.name)
        for path in ROOT.glob("templates/**/*.html"):
            for attr in re.findall(r'class="([^"{]*)"', path.read_text(encoding="utf-8")):
                for name in attr.split():
                    ours.setdefault(name, path.name)
        for path in ROOT.glob("static/**/*.js"):
            text = path.read_text(encoding="utf-8")
            pats = (r"\bh\(\s*'[^']*'\s*,\s*'([^']*)'", r"classList\.(?:add|toggle|remove)\(\s*'([^']*)'")
            for pat in pats:
                for attr in re.findall(pat, text):
                    for name in attr.split():
                        ours.setdefault(name, path.name)
        self.assertGreater(len(ours), 50)
        clashes = sorted(f"{name} ({where})" for name, where in ours.items() if name in bootstrap)
        self.assertEqual(clashes, [])


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


class LightsSwitchTests(SimpleTestCase):
    """One Lights switch: off = preview on the picture only, on = live on the real lights."""

    def setUp(self):
        self.html = (ROOT / "templates/gemstone/index.html").read_text(encoding="utf-8")
        self.js = (ROOT / "static/gemstone/gemstone.js").read_text(encoding="utf-8")

    def test_every_preview_picture_has_the_switch(self):
        for hero in ("t-hero", "p-hero", "c-hero"):
            block = self.html.split(f'id="{hero}"', 1)[1].split("</div>", 1)[0]
            self.assertIn("data-lights", block, hero)

    def test_old_live_button_and_undo_are_gone(self):
        for gone in ("c-live", "gs-toast-undo"):
            self.assertNotIn(gone, self.html)
        self.assertNotIn("url('undo')", self.js)
        self.assertIsNone(re.search(r"\bC\.live\b", self.js))

    def test_taps_go_through_the_switch(self):
        # Pattern cards and solid colours must not play directly; choose() previews while the lights are off.
        self.assertNotRegex(self.js, r"onclick\s*=\s*\(\)\s*=>\s*play\(")
        self.assertIn("const playPattern = (p) => choose(", self.js)

    def test_every_element_the_script_looks_up_exists(self):
        # The script runs on two pages: the app (index.html) and Settings → Photos & light paths
        # (settings.html, setup ids s-*). Each id must exist on the page that uses it.
        def ids(html):
            return set(re.findall(r'id="([^"{]+)"', html)) | set(re.findall(r'json_script:"([\w-]+)"', html))
        app = ids(self.html)
        setup = ids((ROOT / "templates/gemstone/settings.html").read_text(encoding="utf-8"))
        used = set(re.findall(r"\$\('([\w-]+)'\)", self.js))
        setup_only = {i for i in used if i.startswith("s-") or i == "v-setup"}
        self.assertEqual(sorted(setup_only - setup), [])
        self.assertEqual(sorted(used - setup_only - app), [])
        for shared in ("gs", "gs-msg", "gs-toast", "gs-toast-text", "gs-pop"):
            self.assertIn(shared, setup)

class LayoutRunNameTests(Fresh, TestCase):
    """Runs aren't named any more; the UI sends whatever and the server numbers them."""

    def test_runs_without_a_name_save(self):
        data = layout.save("house", [{"points": [[0, 0], [1, 0]]}, {"points": [[0, 1], [1, 1]]}])
        self.assertEqual([r["name"] for r in data["runs"]], ["Run 1", "Run 2"])

    def test_setup_has_no_run_name_field(self):
        js = (ROOT / "static/gemstone/gemstone.js").read_text(encoding="utf-8")
        self.assertNotIn("input.nm", js)
        self.assertIsNone(re.search(r"h\(\s*'input'\s*,\s*'nm\b", js))
        self.assertNotIn("SU.open", js)


class SelectedCardRingTests(SimpleTestCase):
    """Card rows clip overflow, so a highlight drawn outside a card gets cut off
    (the Tonight favourite's border was). Rings must be drawn inside the card."""

    def test_card_state_rings_stay_inside(self):
        css = (ROOT / "static/gemstone/gemstone.css").read_text(encoding="utf-8")
        rules = re.findall(r"([^{}]*\.g-card\.[\w-]+[^{}]*)\{([^}]*)\}", css)
        self.assertTrue(rules)
        for sel, body in rules:
            body = body.replace(" ", "")
            for shadow in re.findall(r"box-shadow:([^;]*)", body):
                for part in shadow.split(","):
                    if part and part != "none":
                        self.assertIn("inset", part, sel.strip())
            # The preview canvas paints over an outline/inset shadow, so rings were
            # half hidden. They must be drawn by the ::after overlay above the content.
            self.assertNotRegex(body, r"outline:(?!none)", sel.strip())

    def test_ring_overlay_sits_above_the_preview(self):
        css = (ROOT / "static/gemstone/gemstone.css").read_text(encoding="utf-8").replace(" ", "")
        m = re.search(r"\.gs\.g-card::after\{([^}]*)\}", css)
        self.assertTrue(m, "card ring overlay missing")
        body = m.group(1)
        for need in ("position:absolute", "inset:0", "pointer-events:none", "border:4px"):
            self.assertIn(need, body)
        self.assertGreater(int(re.search(r"z-index:(\d+)", body).group(1)), 1)
        for state in ("on", "g-pvw"):
            self.assertRegex(css, r"\.g-card\." + state + r"::after\{border-color:")


class PreviewDrawingTests(SimpleTestCase):
    def setUp(self):
        self.js = (ROOT / "static/gemstone/gemstone.js").read_text(encoding="utf-8")

    def test_card_strips_are_straight(self):
        body = self.js[self.js.index("function strip("):self.js.index("// ---", self.js.index("function strip("))]
        self.assertNotIn("arc ?", body)
        self.assertNotIn("Math.sin", body)

    def test_setup_lights_are_static_and_uniform(self):
        """Setup showed the live pattern: some lights blinked, others were dim or under the line."""
        frame = self.js[self.js.index("function frame()"):self.js.index("setInterval(frame")]
        setup = frame[frame.index("view === 'setup'"):]
        self.assertNotIn(".draw(", setup)
        ov = self.js[self.js.index("function renderOverlay()"):self.js.index("function runLabel(")]
        self.assertIn("fill: SETUP_LIGHT", ov)
        self.assertNotIn("colAt", ov)
        # lights after run lines, handles after lights
        self.assertLess(ov.index("'polyline'"), ov.index("SETUP_LIGHT"))
        self.assertLess(ov.index("SETUP_LIGHT"), ov.index("r: act ? 11 : 8"))

    def test_live_pushes_never_overlap(self):
        """Every animation tap fired its own live request; they piled up behind the cloud's pacing."""
        body = self.js[self.js.index("async function pushLive()"):self.js.index("function renderCreateHeader")]
        self.assertIn("if (C.pushing) { C.pushAgain = true; return; }", body)
        self.assertIn("finally", body)


class OptimisticUiTests(SimpleTestCase):
    """Cloud writes are spaced ~2 s apart server-side; taps must update the screen first,
    not wait for the request (the app felt frozen)."""

    def setUp(self):
        self.js = (ROOT / "static/gemstone/gemstone.js").read_text(encoding="utf-8")

    def body(self, name):
        m = re.search(r"async function " + name + r"\([^)]*\)\s*\{", self.js)
        self.assertIsNotNone(m, name)
        depth, i = 1, m.end()
        while depth:
            depth += {"{": 1, "}": -1}.get(self.js[i], 0)
            i += 1
        return self.js[m.end():i]

    def test_writes_update_the_screen_before_sending(self):
        opt = self.body("optimistic")
        self.assertLess(opt.index("apply()"), opt.index("await "))
        for name in ("play", "setLights"):
            self.assertIn("optimistic(", self.body(name), name)
        until = self.body("setUntil")
        self.assertLess(until.index("pending++"), until.index("await "))

    def test_poll_does_not_overwrite_a_pending_write(self):
        self.assertRegex(self.js, r"if \(pending\) return;")
