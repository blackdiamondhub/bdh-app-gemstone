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
from django.urls import NoReverseMatch, reverse
from django.utils import timezone
from PIL import Image
from pygemstone.models import (Device, DeviceState, DownloadablePattern, EventCategory, EventsSettings, Folder,
                               FolderPattern, HomeGroup, Pattern, SubscribedEvent, Timer, TimerData)

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
                                                      "category": "christmas", "downloads": 42})]
        self.events_raw = {"homegroupId": "hg-1", "categoryIds": ["usa", "nhl-van"], "deviceIds": ["house"],
                           "setupYet": True, "allowStaticPatterns": True, "allowAnimatedPatterns": True,
                           "brightness": 255, "futureField": {"keep": 1},
                           "schedule": {"onTime": "sunset", "onOffsetInMinutes": -15,
                                        "offTime": "21:00", "offOffsetInMinutes": 0}}
        self.events = [
            {"eventId": "e-game", "name": "Canucks vs Flames", "categoryId": "nhl-van",
             "categoryName": "Vancouver Canucks", "icon": "", "startDate": "2026-11-24", "endDate": "2026-11-24",
             "isHomeTeam": True, "staticPatterns": [pat("p-van", "Canucks")], "animatedPatterns": []},
            {"eventId": "e-thanks", "name": "Thanksgiving Day", "categoryId": "usa", "categoryName": "USA",
             "icon": "usa", "backgroundColor": 0xFFB5651D, "startDate": "2026-11-26", "endDate": "2026-11-26",
             "staticPatterns": [pat("p-t1", "Harvest"), pat("p-t2", "Pumpkin")],
             "animatedPatterns": [pat("p-t3", "Leaves", animation="chase"), pat("p-t4", "Feast", animation="chase")]},
        ]
        self.categories = [
            {"id": "canada", "name": "Canada", "group": "general", "icon": "maple_leaf"},
            {"id": "usa", "name": "USA", "group": "general", "icon": "usa"},
            {"id": "fun", "name": "Fun", "group": "general", "icon": "assets/icons/airplane.svg"},
            {"id": "nhl-van", "name": "Vancouver Canucks", "group": "nhl"},
            {"id": "nfl-sea", "name": "Seattle Seahawks", "group": "nfl"},
        ]
        self.writes = []  # (op, args, monotonic start, monotonic end)
        self.inflight = 0
        self.max_inflight = 0
        self.delay = 0.0
        self.timers = {}
        self.pending = {}  # cid -> change the controller hasn't applied yet

    @staticmethod
    def _fp(folder, data, created, fav=False):
        return FolderPattern.from_api({"id": str(uuid.uuid4()), "folderId": folder, "patternData": data,
                                       "createdAt": int(created.timestamp()), "isFavorite": fav})

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
    async def devices(self, homegroup_id=None):
        return list(self.devices_)

    async def homegroups(self):
        return [HomeGroup.from_api({"id": "hg-1", "name": "Home"})]

    async def events_settings(self, homegroup_id):
        return EventsSettings.from_api(json.loads(json.dumps(self.events_raw)))

    async def subscribed_events(self, homegroup_id, page=0):
        return [SubscribedEvent.from_api(e) for e in self.events] if page == 0 else []

    async def events_categories(self):
        return [EventCategory.from_api(c) for c in self.categories]

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

    async def save_pattern(self, folder_id, pattern, *, is_favorite=False):
        cloud_pattern_id(pattern.id)
        await self._write("save_pattern", folder_id, pattern.id, pattern.name)
        self.fps = [fp for fp in self.fps if not (fp.folder_id == folder_id and fp.pattern.id == pattern.id)]
        self.fps.append(self._fp(folder_id, pattern.to_api(), dt.datetime.now(UTC), is_favorite))

    async def delete_pattern(self, folder_id, pattern_id):
        await self._write("delete_pattern", folder_id, pattern_id)
        self.fps = [fp for fp in self.fps if not (fp.folder_id == folder_id and fp.pattern.id == pattern_id)]

    async def update_events_settings(self, settings):
        body = json.loads(json.dumps(settings.to_api()))
        await self._write("events_settings", body)
        self.events_raw = body

    async def impulse(self, cid, lights):
        await self._write("impulse", cid, list(lights))

    # timers: like the real controller, a write lands later and a second
    # write to a controller with one still pending is silently dropped.
    async def timers_by_homegroup(self, homegroup_id):
        return list(self.timers.values())

    async def _timer_write(self, op, cid, tid, change):
        if cid in self.pending:
            await self._write("dropped", op, cid, tid)
            return
        await self._write(op, cid, tid)
        self.pending[cid] = change

    async def create_timer(self, cid, hg, *, name, on_time, off_time, pattern, enabled=True, timer_type="daily",
                           days_of_week=None, start_date=None, end_date=None, on_offset_minutes=None,
                           timer_id=None):
        td = TimerData(timer_type, on_time, off_time, days_of_week, start_date, end_date, on_offset_minutes)
        td.validate()
        t = Timer(id=timer_id, name=name, homegroup_id=hg, assignee_id=cid, enabled=enabled, timer_data=td,
                  pattern=pattern)
        await self._timer_write("create_timer", cid, timer_id, lambda: self.timers.__setitem__(t.id, t))
        return t

    async def update_timer(self, timer):
        t = Timer(id=timer.id, name=timer.name, homegroup_id=timer.homegroup_id, assignee_id=timer.assignee_id,
                  enabled=True, timer_data=timer.timer_data, pattern=timer.pattern)
        await self._timer_write("update_timer", timer.assignee_id, timer.id,
                                lambda: self.timers.__setitem__(t.id, t))
        return t

    async def set_timer_enabled(self, cid, tid, enabled):
        def apply():
            self.timers[tid].enabled = enabled
        await self._timer_write("set_timer_enabled", cid, tid, apply)

    async def delete_timer(self, cid, tid):
        await self._timer_write("delete_timer", cid, tid, lambda: self.timers.pop(tid, None))

    async def wait_for_timer(self, hg, tid, predicate=None, *, present=True, timeout=None):
        await self._write("wait", tid)
        for change in self.pending.values():
            change()
        self.pending.clear()
        t = self.timers.get(tid)
        if present:
            assert t is not None and (predicate is None or predicate(t)), f"timer {tid} never landed"
        else:
            assert t is None, f"timer {tid} never went away"
        return t

    def add_timer(self, tid, cid, name="1 on, 3 off Orange", enabled=True, **td):
        data = dict(timer_type="daily", on_time="sunset", off_time="22:00", on_offset_minutes=-30)
        data.update(td)
        self.timers[tid] = Timer(id=tid, name=name, homegroup_id="hg-1", assignee_id=cid, enabled=enabled,
                                 timer_data=TimerData(**data), pattern=Pattern.from_api(
                                     pat("p-orange", "1 on, 3 off Orange", (0xFFFF8800, 0))))

    def seed_wife(self):
        """The two per-controller timers made in the Gemstone app: one schedule."""
        self.add_timer("t-house", "house")
        self.add_timer("t-tub", "tub")


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

    def test_show_on_house_try_reverts_to_what_was_playing(self):
        """Create's "Show on house" must be undoable back to the old pattern, and off stays off."""
        spec = {"colors": ["#ff0000"], "animation": "chase", "name": "Mine"}
        out = service.live(["house", "tub"], spec, start=True)
        self.assertIn("undo", out)
        self.assertEqual(self.fake.states["house"]["pattern"]["name"], "Mine")
        self.assertTrue(self.fake.states["tub"]["onState"])
        # Later edits during the try don't take a new snapshot (that would undo to the design).
        self.assertNotIn("undo", service.live(["house", "tub"], dict(spec, colors=["#00ff00"])))
        service.undo(out["undo"])
        self.assertEqual(self.fake.states["house"]["pattern"]["name"], "Warm white")
        self.assertEqual(self.fake.states["tub"]["pattern"]["name"], "Blue")
        self.assertFalse(self.fake.states["tub"]["onState"])

    def test_play_saved_and_library_patterns_by_id(self):
        service.play(["tub"], {"id": "p-lib"})
        service.play(["house"], {"id": "p-ours"})
        self.assertEqual(self.fake.ops("play"), [("tub", "Candy cane"), ("house", "Sunset")])
        with self.assertRaises(service.Invalid):
            service.play(["house"], {"id": "p-unknown"})

    def test_library_and_ours(self):
        lib = service.library()
        self.assertEqual((lib[0]["name"], lib[0]["category"]), ("Candy cane", "Christmas"))
        self.assertEqual(lib[0]["downloads"], 42)
        ours = service.ours()
        self.assertEqual(ours["patterns"][0]["colors"], ["#ff0000", "#00ff00"])
        self.assertEqual({f["id"]: f["managed"] for f in ours["folders"]}, {"f-ours": False, "f-gem": True})

    def test_plays_are_counted_per_pattern(self):
        """Groups sort by what this hub has played; "Recently played" lists the latest."""
        self.assertEqual(service.plays(), {})
        service.play(["tub"], {"id": "p-lib"})
        service.play(["house"], {"id": "p-lib"})
        service.play(["house"], {"id": "p-ours"})
        got = service.plays()
        self.assertEqual((got["p-lib"]["n"], got["p-ours"]["n"]), (2, 1))
        self.assertGreaterEqual(got["p-ours"]["at"], got["p-lib"]["at"])
        service.record_play(None)
        self.assertEqual(set(service.plays()), {"p-lib", "p-ours"})

    def test_play_history_keeps_only_the_most_recent(self):
        base = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)
        for i in range(service.PLAYS_KEEP + 5):
            service.record_play(f"p{i}", now=base + dt.timedelta(minutes=i))
        got = service.plays()
        self.assertEqual(len(got), service.PLAYS_KEEP)
        self.assertNotIn("p0", got)
        self.assertIn(f"p{service.PLAYS_KEEP + 4}", got)

    def test_brightness_replays_current_pattern(self):
        out = service.brightness(["house", "tub"], 50)
        self.assertEqual(out["brightness"], 128)
        self.assertEqual(self.fake.states["house"]["pattern"]["brightness"], 128)
        self.assertEqual(out["changed"], ["house"])

    def test_brightness_never_turns_lights_on(self):
        """The slider replayed the pattern on every selected light, turning on ones that were off."""
        service.brightness(["house", "tub"], 30)
        self.assertFalse(self.fake.states["tub"]["onState"])
        self.assertEqual([a[0] for a in self.fake.ops("play")], ["house"])
        self.fake.states["house"]["onState"] = False
        cloud.forget("state:")
        self.assertEqual(service.brightness(["house", "tub"], 60)["changed"], [])
        self.assertEqual(len(self.fake.ops("play")), 1)

    def test_colour_round_trip(self):
        for color in ("#ff0000", "#00ff00", "#0000ff", "#ffffff", service.WARM_HEX):
            self.assertEqual(service.int_to_hex(service.hex_to_int(color)), color)
        # Device colours survive display -> device to within rounding (what a saved design sends back).
        for device in (0xFF, 0x1EFF, 0x7A00FF, 0xFF5700, 0x123456, 0x80FF80):
            back = service.hex_to_int(service.int_to_hex(device))
            for shift in (0, 8, 16):
                self.assertLessEqual(abs((back >> shift & 0xFF) - (device >> shift & 0xFF)), 1, hex(device))
        with self.assertRaises(service.Invalid):
            service.hex_to_int("red")

    def test_device_colours_display_as_they_look(self):
        """"1 on, 3 off Orange" is (255, 30, 0) on the LEDs, which showed as red on screen."""
        hexc = service.int_to_hex(0x1EFF)  # r=255 g=30 b=0
        r, g, b = int(hexc[1:3], 16), int(hexc[3:5], 16), int(hexc[5:7], 16)
        self.assertEqual((r, b), (255, 0))
        self.assertGreater(g, 100, f"{hexc} should read as orange, not red")
        self.assertEqual(service.int_to_hex(0xFF0000), "#0000ff")  # pure colours are unchanged

    def test_pattern_params_and_background(self):
        raw = dict(pat("p-acc", "1 on, 3 off Orange", (0x1EFF,), "accent"), backgroundColor=0xFF0000,
                   extraParameters={"0": {"value": 1}, "1": {"value": 3}, "version": 0})
        out = service.pattern_json(Pattern.from_api(raw))
        self.assertEqual(out["params"], {"on": 1, "off": 3})
        self.assertEqual(out["background"], "#0000ff")
        # Parameters this animation doesn't draw are dropped; silly values clamped.
        self.assertEqual(service.params_json("chase", raw["extraParameters"]), {})
        self.assertEqual(service.params_json("smooth", {"0": {"value": 9999}, "1": {"value": -4}}),
                         {"length": service.PARAM_MAX, "spacing": 0})
        p = service.pattern_from_spec({"colors": ["#ff7700"], "animation": "accent", "background": "#0000ff",
                                       "params": {"on": 2, "off": 5, "length": 9}})
        self.assertEqual(p.extra_parameters, {"0": {"name": "On Length", "value": 2},
                                              "1": {"name": "Off Length", "value": 5}, "version": 0})
        self.assertEqual(p.background_color, 0xFF0000)
        self.assertIsNone(service.pattern_from_spec({"colors": ["#ff0000"], "animation": "chase",
                                                     "params": {"on": 2}}).extra_parameters)
        self.assertEqual(service.pattern_json(p)["params"], {"on": 2, "off": 5})

    def test_solid_ids_match_the_ui(self):
        """gemstone.js builds the same id from the swatch hex, so the playing swatch lights up."""
        self.assertEqual(service.solid_id("#FF7A1A"), service.SOLID_ID_PREFIX + "ff7a1a")
        with self.assertRaises(service.Invalid):
            service.solid_id("orange")

    def test_outside_plays_count_once(self):
        """A design started in the Gemstone app shows in Recently played."""
        self.assertEqual(service.note_outside_plays(NOW), ["p-warm"])  # first look: what's on counts
        self.assertEqual(service.note_outside_plays(NOW), [])
        self.fake.states["house"]["pattern"] = pat("p-ios", "1 on, 3 off Orange", (0x1EFF,), "accent")
        self.fake.states["tub"] = {"id": "tub", "onState": True, "pattern": pat("p-ios", "1 on, 3 off Orange")}
        cloud.forget("state:")
        self.assertEqual(service.note_outside_plays(NOW), ["p-ios"])  # two controllers, one play
        self.assertEqual(service.plays()["p-ios"]["n"], 1)
        self.assertEqual(service.note_outside_plays(NOW), [])
        self.assertEqual(service.plays()["p-ios"]["n"], 1)

    def test_outside_plays_ignore_hub_plays_and_lights_off(self):
        service.note_outside_plays(NOW)
        service.play(["house"], {"id": "p-ours"})
        cloud.forget("state:")
        self.assertEqual(service.note_outside_plays(NOW), [])
        self.assertEqual(service.plays()["p-ours"]["n"], 1)
        self.fake.states["tub"]["pattern"] = pat("p-off", "Off one")  # tub is off
        cloud.forget("state:")
        self.assertEqual(service.note_outside_plays(NOW), [])

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
        self.assertEqual(out["name"], "Mine")
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

    def test_tick_keeps_the_lights_off_tonight(self):
        service.set_dark(True, now=self.local(19))
        self.fake.states["house"]["onState"] = True
        cloud.forget("state:")
        worker.tick(self.local(20))
        self.assertFalse(self.fake.states["house"]["onState"])

    def test_tick_turns_autopilot_lights_off_after_its_off_time(self):
        self.fake.states["house"]["onState"] = True
        cloud.forget("state:")
        worker.tick(self.local(21, 5))
        self.assertFalse(self.fake.states["house"]["onState"])

    def test_tick_counts_designs_started_elsewhere(self):
        worker.tick(self.local(12))
        self.fake.states["house"]["pattern"] = pat("p-ios", "From the phone")
        cloud.forget("state:")
        worker.tick(self.local(12, 1))
        self.assertIn("p-ios", service.plays())

    def test_should_start(self):
        self.assertTrue(worker.should_start(["/x/bdh_sdk/runtime/__main__.py"]))
        self.assertFalse(worker.should_start(["manage.py", "test"]))
        with patch.dict("os.environ", {"BDH_GEMSTONE_WORKER": "0"}):
            self.assertFalse(worker.should_start(["/x/bdh_sdk/runtime/__main__.py"]))


class AutopilotBackstopTests(Fresh, TestCase):
    """The hot tub stayed on all night: Gemstone skipped a controller sent a command
    during the evening. Autopilot (fake: off at 21:00, drives "house") must still go off."""

    def turn_on(self, *ids):
        for cid in ids:
            self.fake.states[cid]["onState"] = True
        cloud.forget("state:")

    def offs(self):
        return [a for a in self.fake.ops("on") if a[1] is False]

    def test_whatever_was_sent_in_the_evening_goes_off(self):
        sends = {
            "play": lambda: service.play(["house"], {"kind": "solid", "color": "#00ff00"}),
            "live": lambda: service.live(["house"], {"colors": ["#ff0000"], "animation": "starry"}),
            "brightness": lambda: service.brightness(["house"], 40),
            "power": lambda: service.power(["house"], True, admin=True),
            "guest power": lambda: service.power(["house"], True, admin=False, now=self.local(19)),
        }
        for day, (name, send) in enumerate(sends.items()):
            with self.subTest(name):
                self.turn_on("house")
                service._drop_until(["house"])
                send()
                cloud.forget("state:")
                night = self.local(21, 10) + dt.timedelta(days=day)
                self.assertEqual(service.enforce_autopilot_off(night), ["house"])
                self.assertFalse(self.fake.states["house"]["onState"])

    def test_only_autopilot_lights_and_only_after_the_off_time(self):
        self.turn_on("house", "tub")
        self.assertEqual(service.enforce_autopilot_off(self.local(20, 59)), [])
        self.assertEqual(service.enforce_autopilot_off(self.local(21, 1)), [])  # Gemstone goes first
        self.assertEqual(service.enforce_autopilot_off(self.local(21, 3)), ["house"])
        self.assertTrue(self.fake.states["tub"]["onState"])

    def test_once_a_night(self):
        self.turn_on("house")
        self.assertEqual(service.enforce_autopilot_off(self.local(21, 5)), ["house"])
        self.turn_on("house")  # someone wants them back on: that's fine
        self.assertEqual(service.enforce_autopilot_off(self.local(22)), [])
        self.assertEqual(service.enforce_autopilot_off(self.local(5, 30) + dt.timedelta(days=1)), [])
        self.assertTrue(self.fake.states["house"]["onState"])
        self.assertEqual(service.enforce_autopilot_off(self.local(21, 5) + dt.timedelta(days=1)), ["house"])

    def test_already_off_writes_nothing(self):
        self.fake.states["house"]["onState"] = False
        self.assertEqual(service.enforce_autopilot_off(self.local(21, 5)), [])
        self.assertEqual(self.offs(), [])

    def test_keep_on_until_wins(self):
        service.power(["house"], True, admin=False, now=self.local(20, 30))
        self.assertGreater(dt.datetime.fromisoformat(service.until()["at"]), self.local(21, 5))
        cloud.forget("state:")
        self.assertEqual(service.enforce_autopilot_off(self.local(21, 5)), [])
        self.assertTrue(self.fake.states["house"]["onState"])

    def test_autopilot_off(self):
        self.fake.events_raw["deviceIds"] = []
        self.turn_on("house")
        self.assertEqual(service.enforce_autopilot_off(self.local(21, 30)), [])

    def test_off_time_past_midnight(self):
        self.fake.events_raw["schedule"]["offTime"] = "00:30"
        self.turn_on("house")
        self.assertEqual(service.enforce_autopilot_off(self.local(23, 50)), [])
        next_day = dt.timedelta(days=1)
        self.assertEqual(service.enforce_autopilot_off(self.local(0, 20) + next_day), [])
        self.assertEqual(service.enforce_autopilot_off(self.local(0, 40) + next_day), ["house"])

    def test_not_after_morning(self):
        self.turn_on("house")
        self.assertEqual(service.enforce_autopilot_off(self.local(7) + dt.timedelta(days=1)), [])

    def test_retries_after_a_cloud_error(self):
        self.turn_on("house")
        with patch.object(cloud, "write", side_effect=cloud.CloudError("down")):
            self.assertEqual(service.enforce_autopilot_off(self.local(21, 5)), [])
        self.assertEqual(service.enforce_autopilot_off(self.local(21, 6)), ["house"])

    def test_logged(self):
        self.turn_on("house")
        service.enforce_autopilot_off(self.local(21, 5))
        entry = service.activity()[0]
        self.assertEqual((entry["action"], entry["ids"], entry["source"], entry["names"]),
                         ("off", ["house"], "autopilot backstop", ["House"]))
        self.assertNotIn("by", entry)


class ActivityTests(Fresh, TestCase):
    def post(self, name, body):
        return self.client.post(reverse(name), body, content_type="application/json")

    def test_every_command_is_logged_with_who_sent_it(self):
        self.post("gemstone_play", {"ids": ["house", "tub"], "pattern": {"kind": "solid", "color": "#00ff00"}})
        self.post("gemstone_power", {"ids": ["tub"], "on": False})
        with patch("gemstone.views.is_admin", return_value=True):
            self.post("gemstone_brightness", {"ids": ["house"], "brightness": 40})
        got = [(e["action"], e["ids"], e.get("by")) for e in service.activity()]
        self.assertEqual(got, [("brightness", ["house"], "admin"), ("off", ["tub"], "guest"),
                               ("play", ["house", "tub"], "guest")])
        self.assertIsNone(service.actor.get())

    def test_repeats_merge(self):
        for pct in (20, 30, 40):
            service.brightness(["house"], pct)
        log = service.activity()
        self.assertEqual(len(log), 1)
        self.assertEqual((log[0]["n"], log[0]["level"]), (3, 40))
        service.log_command("brightness", ["house"], now=dt.datetime.now(UTC) + dt.timedelta(minutes=5))
        self.assertEqual(len(service.activity()), 2)

    def test_capped(self):
        for i in range(service.ACTIVITY_KEEP + 5):
            service.log_command("on", [f"c{i}"])
        log = service.activity()
        self.assertEqual(len(log), service.ACTIVITY_KEEP)
        self.assertEqual(log[0]["ids"], [f"c{service.ACTIVITY_KEEP + 4}"])

    def test_never_breaks_the_command(self):
        with patch.object(service, "app_state", side_effect=RuntimeError("db")):
            service.log_command("on", ["house"])

    def test_endpoint_is_admin_only(self):
        service.log_command("on", ["house"])
        self.assertEqual(self.client.get(reverse("gemstone_activity")).status_code, 403)
        with patch("gemstone.views.is_admin", return_value=True):
            r = self.client.get(reverse("gemstone_activity"))
        self.assertEqual(r.json()["activity"][0]["names"], ["House"])


class AutopilotTests(Fresh, TestCase):
    def last_write(self):
        return self.fake.ops("events_settings")[-1][0]

    def test_guest_off_time_is_capped_at_ten(self):
        with self.assertRaises(service.Invalid):
            service.set_schedule(off_time="22:05", admin=False)
        with self.assertRaises(service.Invalid):
            service.set_schedule(off_time="00:30", admin=False)
        self.assertEqual(self.fake.ops("events_settings"), [])
        service.set_schedule(off_time="22:00", admin=False)
        self.assertEqual(self.last_write()["schedule"]["offTime"], "22:00")
        service.set_schedule(off_time="23:30", admin=True)
        self.assertEqual(self.last_write()["schedule"]["offTime"], "23:30")
        # A guest nudging the start time leaves an admin's later off time alone.
        out = service.set_schedule(on_offset=-30, off_time="23:30", admin=False)
        self.assertEqual((out["on_offset"], out["off_time"]), (-30, "23:30"))
        service.set_schedule(off_time="00:30", admin=True)
        self.assertEqual(self.last_write()["schedule"]["offTime"], "00:30")
        with self.assertRaises(service.Invalid):
            service.set_schedule(off_time="25:00", admin=True)

    def test_guest_can_move_the_sunset_offset_within_bounds(self):
        out = service.set_schedule(on_offset=-30, admin=False)
        self.assertEqual(out["on_offset"], -30)
        self.assertEqual(self.last_write()["schedule"]["onOffsetInMinutes"], -30)
        for bad in (125, -125, 7, "soon"):
            with self.assertRaises(service.Invalid, msg=bad):
                service.set_schedule(on_offset=bad, admin=False)
        with self.assertRaises(service.Invalid):
            service.set_schedule(admin=True)

    def test_writes_keep_fields_the_app_does_not_know(self):
        service.set_autopilot({"brightness": 50}, admin=True)
        body = self.last_write()
        self.assertEqual(body["futureField"], {"keep": 1})
        self.assertEqual(body["brightness"], 128)
        self.assertEqual(body["homegroupId"], "hg-1")
        self.assertEqual(body["schedule"]["onTime"], "sunset")

    def test_only_admins_change_autopilot(self):
        with self.assertRaises(service.Forbidden):
            service.set_autopilot({"brightness": 50}, admin=False)
        with self.assertRaises(service.Invalid):
            service.set_autopilot({"colour": "red"}, admin=True)
        with self.assertRaises(service.Invalid):
            service.set_autopilot({"allow_static": False, "allow_animated": False}, admin=True)
        with self.assertRaises(service.Invalid):
            service.set_autopilot({"add_categories": ["nope"]}, admin=True)
        self.assertEqual(self.fake.ops("events_settings"), [])

    def test_categories_split_into_holidays_and_teams(self):
        cats = service.categories()
        self.assertEqual([(c["id"], c["emoji"]) for c in cats["general"]],
                         [("canada", "🍁"), ("usa", "🇺🇸"), ("fun", "✈️")])
        self.assertEqual([(t["id"], t["league"]) for t in cats["teams"]], [("nhl-van", "NHL"), ("nfl-sea", "NFL")])
        self.assertEqual(cats["leagues"], ["NHL", "NFL"])
        out = service.set_autopilot({"add_categories": ["nfl-sea", "canada"], "remove_categories": ["usa"]},
                                    admin=True)
        self.assertEqual(out["category_ids"], ["nhl-van", "nfl-sea", "canada"])

    def test_tonight_event_by_date(self):
        self.assertIsNone(service.tonight_event(self.local(19)))
        ev = service.tonight_event(timezone.make_aware(dt.datetime(2026, 11, 26, 19, 0)))
        self.assertEqual((ev["name"], ev["emoji"], ev["color"], ev["count"]), ("Thanksgiving Day", "🦃", "#b5651d", 4))
        self.assertEqual([(p["name"], p["kind"]) for p in ev["patterns"]],
                         [("Harvest", "static"), ("Pumpkin", "static"), ("Leaves", "animated"), ("Feast", "animated")])
        game = service.tonight_event(timezone.make_aware(dt.datetime(2026, 11, 24, 23, 0)))
        self.assertTrue(game["home"])
        ap = service.autopilot(timezone.make_aware(dt.datetime(2026, 11, 25, 12, 0)))
        self.assertEqual([(e["name"], e["tonight"]) for e in ap["upcoming"]], [("Thanksgiving Day", False)])

    def test_master_switch_remembers_the_lights(self):
        self.assertTrue(service.autopilot()["settings"]["enabled"])
        off = service.set_autopilot({"enabled": False}, admin=True)
        self.assertEqual((off["enabled"], off["device_ids"]), (False, []))
        on = service.set_autopilot({"enabled": True}, admin=True)
        self.assertEqual(on["device_ids"], ["house"])
        with self.assertRaises(service.Invalid):
            service.set_autopilot({"device_ids": []}, admin=True)
        service.set_autopilot({"enabled": False}, admin=True)
        from bdh_sdk.storage import app_state
        app_state(service.APP).delete("autopilot_devices")
        self.assertEqual(service.set_autopilot({"enabled": True}, admin=True)["device_ids"], ["house", "tub"])

    def test_lights_off_tonight(self):
        service.set_schedule(dark_tonight=True, admin=False, now=self.local(19))
        self.assertFalse(self.fake.states["house"]["onState"])
        self.assertEqual(dt.datetime.fromisoformat(service.dark()["until"]),
                         timezone.make_aware(dt.datetime(2026, 11, 22, 6, 0)))
        self.fake.states["tub"]["onState"] = True
        cloud.forget("state:")
        self.assertTrue(service.enforce_dark(self.local(21)))
        self.assertFalse(self.fake.states["tub"]["onState"])
        self.assertFalse(service.enforce_dark(self.local(22)))
        # Playing something by hand wins over "lights off tonight".
        service.play(["house"], {"id": "p-lib"})
        self.assertIsNone(service.dark())
        service.set_dark(True, now=self.local(19))
        self.assertFalse(service.enforce_dark(timezone.make_aware(dt.datetime(2026, 11, 22, 6, 1))))
        self.assertIsNone(service.dark())

    def test_sunset(self):
        from zoneinfo import ZoneInfo
        at = service.sunset(dt.date(2026, 6, 21), 47.61, -122.33, ZoneInfo("America/Los_Angeles"))
        self.assertEqual((at.hour, at.minute // 10), (21, 1))  # 9:11 PM in Seattle
        self.assertIsNone(service.sunset(dt.date(2026, 6, 21), 89.9, 0, ZoneInfo("UTC")))


class GuestOffTimeTests(Fresh, TestCase):
    """Guests pick an off time strictly after tonight's on time (sunset + offset) and by 22:00."""

    def setUp(self):
        super().setUp()
        self.sun = timezone.make_aware(dt.datetime(2026, 11, 21, 16, 30))
        patch.object(service, "sunset_today", lambda now=None: self.sun).start()
        # The fake cloud's schedule is sunset −15 → on at 16:15, off at 21:00.

    def off(self):
        return self.fake.ops("events_settings")[-1][0]["schedule"]["offTime"]

    def test_before_and_at_the_on_time_are_rejected(self):
        self.assertEqual(service.schedule()["on_at"], "16:15")
        for bad in ("16:00", "16:15"):
            with self.assertRaisesRegex(service.Invalid, "after the lights come on at 16:15", msg=bad):
                service.set_schedule(off_time=bad, admin=False)
        self.assertEqual(self.fake.ops("events_settings"), [])

    def test_after_the_on_time_up_to_ten_is_accepted(self):
        service.set_schedule(off_time="16:30", admin=False)
        self.assertEqual(self.off(), "16:30")
        service.set_schedule(off_time="22:00", admin=False)
        self.assertEqual(self.off(), "22:00")
        with self.assertRaisesRegex(service.Invalid, "by 22:00 at the latest"):
            service.set_schedule(off_time="22:05", admin=False)
        self.assertEqual(self.off(), "22:00")

    def test_admins_are_not_capped(self):
        service.set_schedule(off_time="23:30", admin=True)
        self.assertEqual(self.off(), "23:30")
        service.set_schedule(off_time="16:00", admin=True)
        self.assertEqual(self.off(), "16:00")

    def test_without_a_location_guests_fall_back_to_after_five(self):
        patch.object(service, "sunset_today", lambda now=None: None).start()
        self.assertIsNone(service.schedule()["on_at"])
        with self.assertRaisesRegex(service.Invalid, "after the lights come on at 17:00"):
            service.set_schedule(off_time="17:00", admin=False)
        service.set_schedule(off_time="17:15", admin=False)
        self.assertEqual(self.off(), "17:15")

    def test_changing_the_offset_revalidates_the_off_time(self):
        service.set_schedule(off_time="17:00", admin=False)
        writes = len(self.fake.ops("events_settings"))
        # Sunset +30 → on at 17:00, which isn't before the 17:00 off time any more.
        with self.assertRaisesRegex(service.Invalid, "before they come on at 17:00"):
            service.set_schedule(on_offset=30, admin=False)
        self.assertEqual(len(self.fake.ops("events_settings")), writes)
        # Snapping the off time along with the offset (what the dropdown does) is fine.
        out = service.set_schedule(on_offset=30, off_time="17:15", admin=False)
        self.assertEqual((out["on_at"], out["off_time"]), ("17:00", "17:15"))
        # Admins can move the offset regardless.
        service.set_schedule(on_offset=60, admin=True)
        self.assertEqual(self.fake.ops("events_settings")[-1][0]["schedule"]["onOffsetInMinutes"], 60)

    def test_schedule_api_explains_the_rule(self):
        r = self.client.post(reverse("gemstone_schedule"), {"off_time": "16:00"}, content_type="application/json")
        self.assertEqual(r.status_code, 400)
        self.assertIn("after the lights come on at 16:15", r.json()["error"])
        state = self.client.get(reverse("gemstone_state")).json()
        self.assertEqual((state["guest_last"], state["guest_fallback"]), ("22:00", "17:00"))

    def test_event_bar_state_is_remembered(self):
        js = (ROOT / "static/gemstone/gemstone.js").read_text(encoding="utf-8")
        self.assertIn("'gemstone.eventBar.expanded'", js)
        self.assertRegex(js, r"localStorage\.setItem\(EV_KEY, evOpen \? '1' : '0'\)")


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
        self.assertContains(page, 'id="t-sched"')
        for gone in ('id="t-pwr"', 'id="t-until"', "Just tonight"):
            self.assertNotContains(page, gone)
        data = json.loads(page.content.decode().split('id="gs-initial" type="application/json">', 1)[1]
                          .split("</script>", 1)[0])
        self.assertEqual(data["schedule"]["off_time"], "21:00")
        self.assertIn("tonight_event", data)

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

    def test_ours_includes_play_history(self):
        self.post("gemstone_play", {"ids": ["house"], "pattern": {"id": "p-ours"}})
        plays = self.client.get(reverse("gemstone_ours")).json()["plays"]
        self.assertEqual(plays["p-ours"]["n"], 1)

    def test_every_write_sends_ids_the_cloud_accepts(self):
        """Editing a design failed on every change: live/solid pattern ids weren't UUIDs."""
        names = [a for _, a in service.ANIMATIONS] + list(service.MORE_ANIMATIONS)
        for anim in names:
            spec = {"colors": ["#ff0000", "#00ff00"], "animation": anim}
            r = self.post("gemstone_live", {"ids": ["house", "tub"], "pattern": spec})
            self.assertEqual(r.status_code, 200, (anim, r.content))
        r = self.post("gemstone_live", {"ids": ["house"], "pattern": {"colors": ["#ff0000"]}, "start": True})
        self.assertEqual(self.post("gemstone_undo", {"token": r.json()["undo"]}).status_code, 200)
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

    def test_guest_design_ignores_folder_keeps_name(self):
        r = self.post("gemstone_design", {"pattern": {"colors": ["#ff0000"]}, "folder": "f-ours", "name": "  Mine \n "})
        self.assertEqual(r.json()["folder"]["name"], "Guest designs")
        self.assertEqual(r.json()["name"], "Mine")
        r = self.post("gemstone_design", {"pattern": {"colors": ["#ff0000"]}})
        self.assertEqual(r.json()["name"], "Untitled")
        folders = self.client.get(reverse("gemstone_ours")).json()["folders"]
        self.assertEqual({f["name"]: f["locked"] for f in folders},
                         {"Our designs": True, "Holidays": True, "Guest designs": False})

    def test_guest_schedule_api(self):
        self.assertEqual(self.post("gemstone_schedule", {"off_time": "22:30"}).status_code, 400)
        r = self.post("gemstone_schedule", {"on_offset": -20, "off_time": "22:00"})
        self.assertEqual((r.json()["schedule"]["on_offset"], r.json()["schedule"]["off_time"]), (-20, "22:00"))
        self.assertEqual(self.post("gemstone_schedule", {"on_offset": 3}).status_code, 400)
        r = self.post("gemstone_schedule", {"dark": True})
        self.assertTrue(r.json()["schedule"]["dark"])

    def test_autopilot_is_admin_only(self):
        self.assertEqual(self.client.get(reverse("gemstone_autopilot_api")).status_code, 403)
        self.assertEqual(self.post("gemstone_autopilot_api", {"brightness": 40}).status_code, 403)
        with patch("gemstone.views.is_admin", return_value=True):
            page = self.client.get(reverse("gemstone_index"))
            self.assertContains(page, 'id="ap"')
            self.assertContains(page, 'data-autopilot-url="/gemstone/api/autopilot/"')
            self.assertEqual(self.client.get(reverse("gemstone_autopilot_api")).status_code, 200)
            r = self.post("gemstone_autopilot_api", {"schedule": {"off_time": "23:45"}})
            self.assertEqual(r.json()["settings"]["schedule"]["off_time"], "23:45")

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

    def test_tonight_holds_every_pattern(self):
        page = self.client.get(reverse("gemstone_index"))
        for have in ('id="t-favs"', 'id="t-all"', 'id="t-stk"', 'id="t-chips"', 'id="t-grid"', 'id="t-libh"',
                     'id="t-lib"', 'id="g-mini"', 'id="m-ph"', 'id="m-bslider"', "gemstone/gemstone-core.js"):
            self.assertContains(page, have)
        # The Patterns page and its search are gone.
        for gone in ('id="v-patterns"', 'data-view="patterns"', 'id="p-search"', 'id="p-hero"'):
            self.assertNotContains(page, gone)
        html = page.content.decode()
        self.assertLess(html.index("gemstone-core.js"), html.index("gemstone/gemstone.js"))
        # The mini bar has the same Lights switch as the hero.
        mini = html.split('id="g-mini"', 1)[1].split('id="v-tonight"', 1)[0]
        self.assertIn("data-lights", mini)

    def fav(self, pid, fav=True):
        return self.post("gemstone_fav", {"id": pid, "fav": fav})

    def ours(self):
        return {p["id"]: p for p in self.client.get(reverse("gemstone_ours")).json()["patterns"]}

    def test_heart_a_saved_pattern(self):
        self.assertFalse(self.ours()["p-ours"]["fav"])
        r = self.fav("p-ours")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual((r.json()["fav"], r.json()["saved"], r.json()["folder"]), (True, False, "f-ours"))
        self.assertTrue(self.ours()["p-ours"]["fav"])
        self.fav("p-ours", False)
        self.assertFalse(self.ours()["p-ours"]["fav"])
        self.assertEqual([a[0] for a in self.fake.ops("save_pattern")], ["f-ours", "f-ours"])

    def test_guest_heart_on_a_library_pattern_saves_it_to_guest_designs(self):
        r = self.fav("p-lib")
        self.assertTrue(r.json()["saved"])
        mine = self.ours()["p-lib"]
        self.assertTrue(mine["fav"])
        folders = {f["id"]: f["name"] for f in self.client.get(reverse("gemstone_ours")).json()["folders"]}
        self.assertEqual(folders[mine["folder"]], "Guest designs")

    @patch("gemstone.views.is_admin", return_value=True)
    def test_admin_heart_on_a_library_pattern_saves_it_to_the_house_folder(self, _admin):
        self.assertEqual(self.fav("p-lib").json()["folder"], "f-ours")
        self.assertEqual(self.ours()["p-lib"]["folder"], "f-ours")

    def test_unheart_unsaved_library_pattern_writes_nothing(self):
        r = self.fav("p-lib", False)
        self.assertEqual((r.status_code, r.json()["fav"]), (200, False))
        self.assertEqual(self.fake.ops("save_pattern"), [])

    def test_heart_unknown_pattern(self):
        self.assertEqual(self.fav("nope").status_code, 400)
        self.assertEqual(self.post("gemstone_fav", {"fav": True}).status_code, 400)
        self.assertEqual(self.client.get(reverse("gemstone_fav")).status_code, 405)

    def delete(self, pid):
        return self.post("gemstone_design_delete", {"id": pid})

    def guest_design(self):
        return self.post("gemstone_design", {"pattern": {"colors": ["#ff0000"]}, "name": "Mine"}).json()["id"]

    @patch("gemstone.views.is_admin", return_value=True)
    def test_admin_deletes_a_custom_design(self, _admin):
        r = self.delete("p-ours")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual((r.json()["ok"], r.json()["name"]), (True, "Sunset"))
        self.assertTrue(r.json()["token"])
        self.assertEqual(self.fake.ops("delete_pattern"), [("f-ours", "p-ours")])
        self.assertNotIn("p-ours", self.ours())
        entry = service.activity()[0]
        self.assertEqual((entry["action"], entry["design"], entry["name"]), ("delete", "p-ours", "Sunset"))

    def test_admin_deletes_a_guest_design(self):
        pid = self.guest_design()
        with patch("gemstone.views.is_admin", return_value=True):
            self.assertEqual(self.delete(pid).status_code, 200)
        self.assertNotIn(pid, self.ours())

    def test_guest_deletes_guest_designs_only(self):
        pid = self.guest_design()
        self.assertTrue(self.ours()[pid]["guest"])
        self.assertFalse(self.ours()["p-ours"]["guest"])
        self.assertEqual(self.delete(pid).status_code, 200)
        self.assertNotIn(pid, self.ours())
        self.assertEqual(self.delete("p-ours").status_code, 403)
        self.assertIn("p-ours", self.ours())

    def test_gemstone_patterns_cannot_be_deleted(self):
        managed = pat(str(uuid.uuid4()), "Holly")
        self.fake.fps.append(FakeGemstone._fp("f-gem", managed, NOW))
        self.client.get(reverse("gemstone_library"))
        with patch("gemstone.views.is_admin", return_value=True):
            self.fav("p-lib")
            mine = self.ours()
            self.assertTrue(mine[managed["id"]]["stock"])
            self.assertTrue(mine["p-lib"]["stock"])
            self.assertFalse(mine["p-ours"]["stock"])
            self.assertEqual(self.delete(managed["id"]).status_code, 403)
            self.assertEqual(self.delete("p-lib").status_code, 403)
        self.assertEqual(self.fake.ops("delete_pattern"), [])

    def test_delete_missing_design(self):
        with patch("gemstone.views.is_admin", return_value=True):
            self.assertEqual(self.delete(str(uuid.uuid4())).status_code, 404)
            self.assertEqual(self.post("gemstone_design_delete", {}).status_code, 400)
        self.assertEqual(self.client.get(reverse("gemstone_design_delete")).status_code, 405)

    @patch("gemstone.views.is_admin", return_value=True)
    def test_deleting_the_live_design_leaves_the_lights_alone(self, _admin):
        self.post("gemstone_play", {"ids": ["house"], "pattern": {"id": "p-ours"}})
        self.fake.writes.clear()
        self.assertEqual(self.delete("p-ours").status_code, 200)
        self.assertEqual([w[0] for w in self.fake.writes], ["delete_pattern"])
        self.assertTrue(self.fake.states["house"]["onState"])

    @patch("gemstone.views.is_admin", return_value=True)
    def test_restore_puts_it_back_with_its_heart(self, _admin):
        self.fav("p-ours")
        token = self.delete("p-ours").json()["token"]
        self.assertNotIn("p-ours", self.ours())
        r = self.post("gemstone_design_restore", {"token": token})
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual((r.json()["id"], r.json()["name"]), ("p-ours", "Sunset"))
        back = self.ours()["p-ours"]
        self.assertEqual((back["folder"], back["fav"], back["name"]), ("f-ours", True, "Sunset"))
        self.assertEqual(self.fake.ops("save_pattern")[-1], ("f-ours", "p-ours", "Sunset"))
        self.assertEqual(service.activity()[0]["action"], "restore")
        self.assertEqual(self.post("gemstone_design_restore", {"token": token}).status_code, 404)
        self.assertEqual(self.post("gemstone_design_restore", {"token": "nope"}).status_code, 404)

    def test_restore_expires(self):
        pid = self.guest_design()
        token = self.delete(pid).json()["token"]
        with patch.object(service.time, "monotonic", return_value=time.monotonic() + service.DELETE_UNDO_SECONDS + 1):
            self.assertEqual(self.post("gemstone_design_restore", {"token": token}).status_code, 404)


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
        # Autopilot moved into the app's Plan tab.
        self.assertEqual([p.url_name for p in spec.settings_pages], ["gemstone_settings"])
        with self.assertRaises(NoReverseMatch):
            reverse("gemstone_autopilot")
        self.assertFalse((ROOT / "templates/gemstone/autopilot.html").exists())
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

    def test_tonight_has_the_switch_create_tries_it_on(self):
        # Tonight controls the house; Create only touches it through "Show on house" (a try).
        block = self.html.split('id="t-hero"', 1)[1].split("</div>", 1)[0]
        self.assertIn("data-lights", block)
        create = self.html.split('id="c-hero"', 1)[1].split('class="c-show"', 1)[0]
        self.assertNotIn("data-lights", create)
        self.assertIn('id="c-show"', self.html)

    def test_undo_only_comes_from_a_try(self):
        for gone in ("c-live", "gs-toast-undo"):
            self.assertNotIn(gone, self.html)
        self.assertEqual(self.js.count("url('undo')"), 1)
        self.assertIn("token: t.undo", self.js)
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

    def test_plan_tab_has_every_element_its_scripts_use(self):
        html = (ROOT / "templates/gemstone/index.html").read_text(encoding="utf-8")
        have = set(re.findall(r'id="([^"{]+)"', html))
        for name in ("autopilot.js", "plan.js"):
            js = (ROOT / "static/gemstone" / name).read_text(encoding="utf-8")
            used = set(re.findall(r"(?:\$|getElementById)\('([\w-]+)'\)", js))
            self.assertGreater(len(used), 10, name)
            self.assertEqual(sorted(used - have), [], name)

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


class RunDirectionTests(Fresh, TestCase):
    """Setup had no way to see or change which way a pattern flows along a run."""

    def setUp(self):
        super().setUp()
        self.js = (ROOT / "static/gemstone/gemstone.js").read_text(encoding="utf-8")

    def test_reversed_points_are_saved_in_order(self):
        # Direction is the point order; the server must keep it exactly.
        pts = [[0.9, 0.5], [0.5, 0.2], [0.1, 0.5]]
        self.assertEqual(layout.save("house", [{"points": pts}])["runs"][0]["points"], pts)
        self.assertEqual(layout.get("house")["runs"][0]["points"], pts)

    def test_each_run_has_an_undoable_reverse_button(self):
        body = self.js[self.js.index("function renderRuns()"):self.js.index("function renderOverlay()")]
        self.assertIn("'Reverse direction'", body)
        rev = body[body.index("rev.onclick"):]
        rev = rev[:rev.index("\n")]
        for need in ("pushHist()", "r.points.reverse()", "runsChanged()"):
            self.assertIn(need, rev)

    def test_arrows_show_flow_above_the_lights(self):
        ov = self.js[self.js.index("function renderOverlay()"):self.js.index("function flowArrows(")]
        self.assertIn("flowArrows(pts)", ov)
        # visible over the lights, under the draggable handles
        self.assertLess(ov.index("SETUP_LIGHT"), ov.index("flowArrows(pts)"))
        self.assertLess(ov.index("flowArrows(pts)"), ov.index("r: act ? 11 : 8"))
        fa = self.js[self.js.index("function flowArrows("):self.js.index("function runLabel(")]
        self.assertIn("Math.atan2(b[1] - a[1], b[0] - a[0])", fa)  # points first -> last

    def test_preview_flows_in_point_order(self):
        dots = self.js[self.js.index("    layoutDots() {"):self.js.index("    draw(pattern")]
        self.assertIn("run.points.map((p) => this.toPx(p))", dots)
        self.assertIn("offset + j", dots)
        self.assertNotIn("reverse", dots)


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

    def test_photos_are_shown_whole(self):
        """Tonight zoomed into the light runs, so a hot tub photo showed only a sliver."""
        fit = self.js[self.js.index("    fit() {"):self.js.index("    toPx(p)")]
        # A photo is either letterboxed (contain) or filled only when the shapes nearly match.
        self.assertIn("if (this.opts.contain || (this.photo && !fill)) s = contain;", fit)
        self.assertIn("r > 0.9 && r < 1.11", fit)
        self.assertLess(fit.index("s = contain;"), fit.index("0.9 * w / (bw * A)"))
        # The bars beside a letterboxed photo are a blurred copy, like the screensaver.
        self.assertIn("this.blur.hidden = !this.photo || this.opts.contain || fill;", fit)
        css = (ROOT / "static/gemstone/gemstone.css").read_text(encoding="utf-8")
        self.assertRegex(css, r"\.scn\.blur\{[^}]*filter:blur\(")

    def test_photo_swaps_only_once_decoded(self):
        """Switching hot tub <-> house briefly showed the new photo stretched into the old shape."""
        body = self.js[self.js.index("const swap = () => {"):self.js.index("    fit() {")]
        self.assertIn("img.decode", body)
        self.assertIn("if (this.want !== key) return;", body)
        # picture, aspect and lights change in the same step
        swap = body[:body.index("};")]
        self.assertLess(swap.index("appendChild(img)"), swap.index("commit()"))
        self.assertLess(swap.index("commit()"), swap.index("this.fit()"))

    def test_resize_redraws_still_previews(self):
        """A motionless preview went blank ~20 s in: fit() cleared the canvas but the
        still-frame cache said it was already drawn."""
        fit = self.js[self.js.index("    fit() {"):self.js.index("    toPx(p)")]
        self.assertIn("this.drawn = '';", fit)

    def test_every_group_is_capped(self):
        """Grouped lists show at most GROUP_PAGE cards, with Show more for the rest."""
        groups = self.js[self.js.index("function fillGroups("):self.js.index("let cat = ''")]
        self.assertIn("g.pats.slice(0, lim)", groups)
        self.assertIn("grpMore[g.key] || GROUP_PAGE", groups)
        lib = self.js[self.js.index("async function renderLib()"):self.js.index("$('t-libh').onclick")]
        self.assertIn("fillGroups(box,", lib)
        for fn in ("async function renderRecent()", "async function renderFavs()"):
            body = self.js[self.js.index(fn):self.js.index("\n  }\n", self.js.index(fn))]
            self.assertIn("GROUP_PAGE", body, fn)
        html = (ROOT / "templates/gemstone/index.html").read_text(encoding="utf-8")
        self.assertIn('id="t-recent-w"', html)
        self.assertIn('id="t-cx"', html)

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
        sched = self.body("setSchedule")
        self.assertLess(sched.index("pending++"), sched.index("await "))

    def test_poll_does_not_overwrite_a_pending_write(self):
        self.assertRegex(self.js, r"if \(pending\) return;")


def entry(key, kind, start, end, lights=("house",)):
    return {"key": key, "kind": kind, "name": key, "lights": set(lights), "start": start, "end": end,
            "schedule": key, "colors": None, "design": None}


class PrecedenceTests(SimpleTestCase):
    """What the controller plays when schedules overlap (measured on a real one)."""

    def at(self, hour, minute=0):
        return dt.datetime(2026, 11, 21, hour, minute, tzinfo=UTC)

    def test_latest_start_wins_then_falls_back_like_a_stack(self):
        orange = entry("orange", "daily", self.at(16, 30), self.at(22))
        a = entry("a", "daily", self.at(21, 3), self.at(21, 13))
        b = entry("b", "daily", self.at(21, 6), self.at(21, 10))
        es = [orange, a, b]
        self.assertEqual([service.winner(es, self.at(21, m))["key"] for m in (2, 4, 7, 11, 14)],
                         ["orange", "a", "b", "a", "orange"])
        self.assertIsNone(service.winner(es, self.at(22)))
        segs = service.segments(es, self.at(16), self.at(23))
        self.assertEqual([(x["entry"]["key"], x["start"].strftime("%H:%M")) for x in segs],
                         [("orange", "16:30"), ("a", "21:03"), ("b", "21:06"), ("a", "21:10"), ("orange", "21:13")])

    def test_tiers_beat_a_later_start(self):
        daily = entry("daily", "daily", self.at(20), self.at(23))
        auto = entry("auto", "autopilot", self.at(18), self.at(23))
        dates = entry("dates", "yearly", self.at(17), self.at(23))
        self.assertEqual(service.winner([daily, auto], self.at(21))["key"], "auto")
        self.assertEqual(service.winner([daily, auto, dates], self.at(21))["key"], "dates")

    def test_same_start_tie_goes_to_weekly(self):
        daily = entry("daily", "daily", self.at(20), self.at(23))
        weekly = entry("weekly", "weekly", self.at(20), self.at(22))
        self.assertEqual(service.winner([daily, weekly], self.at(21))["key"], "weekly")
        self.assertEqual(service.winner([daily, weekly], self.at(22, 30))["key"], "daily")


class ScheduleTests(Fresh, TestCase):
    def setUp(self):
        super().setUp()
        patch.object(service, "_site", return_value=None).start()  # sunset falls back to 17:00
        patch.object(service, "_spawn", lambda fn: fn()).start()
        service._job.clear()
        self.addCleanup(service._job.clear)
        self.fake.seed_wife()
        self.admin = patch("gemstone.views.is_admin", return_value=True)

    def post(self, name, body):
        return self.client.post(reverse(name), body, content_type="application/json")

    def as_admin(self):
        self.admin.start()
        self.addCleanup(self.admin.stop)

    def body(self, **kw):
        b = {"lights": ["house", "tub"], "design": "p-ours", "repeat": "weekly", "days": [5, 6],
             "on": "20:00", "off": "23:00", "name": "Weekend"}
        b.update(kw)
        return b

    def timer_ops(self):
        return [(w[0], w[1][-1] if w[0] != "wait" else w[1][0]) for w in self.fake.writes]

    def test_matching_timers_on_two_lights_are_one_schedule(self):
        self.fake.add_timer("t-other", "house", name="Late", on_time="23:00", off_time="23:30",
                            on_offset_minutes=None)
        scheds = service.schedules()
        self.assertEqual(len(scheds), 2)
        wife = next(x for x in scheds if x["name"] == "1 on, 3 off Orange")
        self.assertEqual((wife["lights"], wife["light_names"]), (["house", "tub"], ["House", "Hot Tub"]))
        self.assertEqual((wife["on"], wife["on_offset"], wife["off"], wife["repeat"]), ("sunset", -30, "22:00", "daily"))
        self.assertEqual(wife["timers"], {"house": "t-house", "tub": "t-tub"})
        self.assertEqual(wife["design"]["name"], "1 on, 3 off Orange")

    def test_create_on_two_lights_writes_one_at_a_time(self):
        self.as_admin()
        r = self.post("gemstone_schedule_save", self.body())
        self.assertEqual(r.json()["job"]["state"], "done", r.json())
        ops = self.timer_ops()
        self.assertEqual([o for o, _ in ops], ["create_timer", "wait", "create_timer", "wait"])
        self.assertEqual(ops[0][1], ops[1][1])
        self.assertNotIn("dropped", [o for o, _ in ops])
        weekend = next(x for x in self.client.get(reverse("gemstone_plan")).json()["schedules"]
                       if x["name"] == "Weekend")
        self.assertEqual((weekend["lights"], weekend["days"], weekend["design"]["id"]),
                         (["house", "tub"], [5, 6], "p-ours"))

    def test_edit_toggle_and_delete_reach_every_light(self):
        self.as_admin()
        sid = "t-house"
        r = self.post("gemstone_schedule_save", self.body(id=sid, repeat="daily", days=[], on="sunset",
                                                          on_offset=-30, off="23:00", name="Orange"))
        self.assertEqual(r.json()["job"]["state"], "done", r.json())
        self.assertEqual([o for o, _ in self.timer_ops()], ["update_timer", "wait"] * 2)
        self.assertEqual({t.timer_data.off_time for t in self.fake.timers.values()}, {"23:00"})
        self.fake.writes.clear()
        self.post("gemstone_schedule_toggle", {"id": sid, "enabled": False})
        self.assertEqual([o for o, _ in self.timer_ops()], ["set_timer_enabled", "wait"] * 2)
        self.assertFalse(any(t.enabled for t in self.fake.timers.values()))
        self.post("gemstone_schedule_delete", {"id": sid})
        self.assertEqual(self.fake.timers, {})

    def test_dropping_a_light_deletes_only_its_timer(self):
        self.as_admin()
        self.post("gemstone_schedule_save", self.body(id="t-house", lights=["house"], repeat="daily", days=[],
                                                      on="sunset", on_offset=-30, off="22:00",
                                                      name="1 on, 3 off Orange", design="p-ours"))
        self.assertEqual(sorted(self.fake.timers), ["t-house"])

    def test_same_start_on_shared_lights_is_refused(self):
        self.as_admin()
        r = self.post("gemstone_schedule_save", self.body(lights=["tub"], repeat="weekly", days=[6],
                                                          on="sunset", on_offset=-30))
        self.assertEqual(r.status_code, 400)
        self.assertIn("already starts at that time", r.json()["error"])
        self.assertEqual(self.fake.writes, [])
        # A different tier, time or light is fine: the timeline shows who wins.
        for ok in ({"repeat": "yearly", "start": "12-24", "end": "12-26", "on": "sunset", "on_offset": -30},
                   {"on": "sunset", "on_offset": -15}):
            service._job.clear()
            self.assertEqual(self.post("gemstone_schedule_save", self.body(**ok)).status_code, 200, ok)

    def test_reenabling_into_a_clash_is_refused(self):
        self.as_admin()
        self.fake.add_timer("t-x", "house", name="Copy", enabled=False)
        r = self.post("gemstone_schedule_toggle", {"id": "t-x", "enabled": True})
        self.assertEqual(r.status_code, 400)

    def test_coming_up_shows_the_winner_and_whats_beaten(self):
        self.fake.add_timer("t-xmas", "house", name="Lodge night", timer_type="yearly", days_of_week=list(range(7)),
                            start_date="11-21", end_date="11-21", on_time="16:00", off_time="23:00",
                            on_offset_minutes=None)
        rows = service.plan(14, now=self.local(12))
        tonight = rows[0]
        self.assertEqual((tonight["first"], tonight["last"]), ("2026-11-21", "2026-11-21"))
        house = next(g for g in tonight["groups"] if "house" in g["lights"])
        tub = next(g for g in tonight["groups"] if "tub" in g["lights"])
        self.assertEqual([x["name"] for x in house["segments"]], ["Lodge night"])
        self.assertEqual([x["name"] for x in house["losers"]], ["1 on, 3 off Orange"])
        self.assertEqual(([x["name"] for x in tub["segments"]], tub["losers"]), (["1 on, 3 off Orange"], []))
        # Later nights have only the orange schedule on both lights, merged into one row.
        self.assertEqual(rows[1]["first"], "2026-11-22")
        self.assertEqual([g["lights"] for g in rows[1]["groups"]], [["house", "tub"]])

    def test_tonight_line_while_playing_and_after_an_override(self):
        self.fake.states["house"].update(onState=True, pattern=pat("p-orange", "1 on, 3 off Orange"))
        now = self.local(19)
        playing = service.schedule_now(now)
        self.assertEqual(playing["name"], "1 on, 3 off Orange")
        self.assertEqual(dt.datetime.fromisoformat(playing["until"]), self.local(22))
        self.fake.states["house"]["pattern"] = pat("p-warm", "Warm white")
        cloud.reset()
        later = service.schedule_now(now)
        self.assertEqual(set(later), {"name", "resumes"})
        self.assertEqual(dt.datetime.fromisoformat(later["resumes"]), self.local(16, 30) + dt.timedelta(days=1))

    def test_tonight_line_before_a_schedule_starts(self):
        # Midday, lights off: tonight belongs to the orange schedule, so Tonight says so.
        soon = service.schedule_now(self.local(12))
        self.assertEqual(soon["name"], "1 on, 3 off Orange")
        self.assertEqual((dt.datetime.fromisoformat(soon["starts"]), dt.datetime.fromisoformat(soon["until"])),
                         (self.local(16, 30), self.local(22)))
        # After it ends, tomorrow's run isn't "tonight".
        self.assertIsNone(service.schedule_now(self.local(23)))

    def test_guests_get_nothing_from_the_plan(self):
        for name, method in (("gemstone_plan", "get"), ("gemstone_schedule_save", "post"),
                             ("gemstone_schedule_toggle", "post"), ("gemstone_schedule_delete", "post")):
            r = (self.client.get(reverse(name)) if method == "get"
                 else self.post(name, {"id": "t-house", "enabled": False, **self.body()}))
            self.assertEqual(r.status_code, 403, name)
        self.assertEqual(len(self.fake.timers), 2)
        self.assertEqual(self.fake.writes, [])
        page = self.client.get(reverse("gemstone_index")).content.decode()
        for admin_only in ('data-view="plan"', 'id="v-plan"', "plan.js", "autopilot.js", "data-plan-url"):
            self.assertNotIn(admin_only, page)
        self.as_admin()
        page = self.client.get(reverse("gemstone_index")).content.decode()
        for admin_only in ('data-view="plan"', 'id="v-plan"', "plan.js", "data-plan-url"):
            self.assertIn(admin_only, page)

    def test_guest_state_has_only_the_tonight_line(self):
        self.fake.states["house"].update(onState=True, pattern=pat("p-orange", "1 on, 3 off Orange"))
        with patch.object(service, "_local_now", lambda now=None: timezone.localtime(now or self.local(19))):
            data = self.client.get(reverse("gemstone_state")).json()
        self.assertEqual(set(data["schedule_now"]), {"name", "until"})
        text = json.dumps(data)
        for leak in ("t-house", "t-tub", "timeline", "light_names", "\"repeat\"", "\"days\""):
            self.assertNotIn(leak, text)
        self.assertNotIn("timers", data)

    def test_state_survives_a_broken_timer_list(self):
        async def boom(hg):
            raise cloud.CloudError("down")
        self.fake.timers_by_homegroup = boom
        data = self.client.get(reverse("gemstone_state")).json()
        self.assertIsNone(data.get("schedule_now"))
        self.assertTrue(data["controllers"])


# ---------------------------------------------------------------- assistant tools

from bdh_sdk import ALL_MODES  # noqa: E402
from bdh_sdk.plugins import GUEST, OWNER  # noqa: E402
from bdh_sdk.tools import ToolError  # noqa: E402

from gemstone import assistant, matching  # noqa: E402


class AssistantMatchingTests(SimpleTestCase):
    def setUp(self):
        self.zones = matching.Zones([{"id": "g", "name": "BDL-Gemstone", "label": "House"},
                                     {"id": "t", "name": "Hot Tub", "label": "Hot Tub"}], primary="g")

    def ids(self, text):
        return [z["id"] for z in self.zones.resolve(text)]

    def test_zones(self):
        for text in ("", None, "outside", "the outdoor lights", "gemstone lights", "all of them", "both"):
            self.assertEqual(self.ids(text), ["g", "t"], text)
        self.assertEqual(self.ids("house lights"), ["g"])
        self.assertEqual(self.ids("the roof"), ["g"])
        self.assertEqual(self.ids("BDL"), ["g"])
        self.assertEqual(self.ids("hot tube"), ["t"])
        self.assertEqual(self.ids("house and hot tub"), ["g", "t"])
        with self.assertRaises(matching.NoMatch) as cm:
            self.zones.resolve("garage")
        self.assertIn("House and Hot Tub", str(cm.exception))

    def test_ambiguous_zone_asks_and_plural_means_all(self):
        zones = matching.Zones([{"id": "f", "name": "Front Deck", "label": "Front Deck"},
                                {"id": "b", "name": "Back Deck", "label": "Back Deck"}])
        with self.assertRaises(matching.Ambiguous) as cm:
            zones.resolve("deck")
        self.assertIn("Front Deck and Back Deck", str(cm.exception))
        self.assertEqual([z["id"] for z in zones.resolve("the decks")], ["f", "b"])

    PATS = [{"id": "1", "name": "Christmas 3R 3G"}, {"id": "2", "name": "Traditional Christmas"},
            {"id": "3", "name": "Christmas Glam"},
            {"id": "4", "name": "Holly & Berry Jingle", "folder_name": "Christmas"},
            {"id": "5", "name": "Halloween Boooo"}, {"id": "6", "name": "Warm White", "fav": True},
            {"id": "7", "name": "Glitter - Canucks"}, {"id": "8", "name": "Chase - Canucks", "fav": True}]

    def test_pattern_words_favourites_and_plays(self):
        best, others = matching.find_pattern("christmas", self.PATS)
        self.assertEqual(best["name"], "Christmas Glam")  # every word, most of the name, shortest
        self.assertEqual({o["name"] for o in others},
                         {"Traditional Christmas", "Christmas 3R 3G", "Holly & Berry Jingle"})
        best, _ = matching.find_pattern("christmas", self.PATS, plays={"2": {"n": 4}})
        self.assertEqual(best["name"], "Traditional Christmas")
        self.assertEqual(matching.find_pattern("canucks pattern", self.PATS)[0]["name"], "Chase - Canucks")
        self.assertEqual(matching.find_pattern("traditional christmas", self.PATS)[0]["id"], "2")
        self.assertEqual(matching.find_pattern("holly and berry", self.PATS)[0]["id"], "4")

    def test_pattern_asr_slips_and_unknown(self):
        self.assertEqual(matching.find_pattern("haloween", self.PATS)[0]["name"], "Halloween Boooo")
        self.assertIsNone(matching.find_pattern("easter", self.PATS))
        self.assertIn("Christmas Glam", matching.suggest("christmass glum", self.PATS))
        self.assertEqual(matching.popular(self.PATS)[:2], ["Warm White", "Chase - Canucks"])

    def test_colours(self):
        self.assertEqual(matching.find_colour("blue"), ("Blue", "#2f6bff"))
        self.assertEqual(matching.find_colour("Warm White"), ("Warm white", "#fff1e0"))
        self.assertEqual(matching.find_colour("turquoise")[0], "Teal")
        self.assertEqual(matching.find_colour("purpel")[0], "Purple")
        self.assertEqual(matching.find_colour("#00FF00")[1], "#00ff00")
        self.assertIsNone(matching.find_colour("plaid"))


class AssistantTests(Fresh, TestCase):
    def setUp(self):
        super().setUp()
        self.fake.devices_[0] = Device.from_api({"id": "house", "name": "BDL-Gemstone",
                                                 "hub": {"pixelCount": [150, 50]}, "online": True})
        self.xmas = str(uuid.uuid4())
        self.glam = str(uuid.uuid4())
        FIXTURE_IDS.update({self.xmas, self.glam})
        self.addCleanup(FIXTURE_IDS.difference_update, {self.xmas, self.glam})
        self.fake.fps += [self.fake._fp("f-ours", pat(self.xmas, "Traditional Christmas", (0xFF0000, 0xFF00)), NOW),
                          self.fake._fp("f-ours", pat(self.glam, "Christmas Glam", (0xFFD700,)), NOW)]
        service.record_play(self.xmas)
        # The warm-up loads the library on another thread; tests load it themselves.
        patch.object(assistant, "_warm").start()

    def run_tool(self, tool, mode=GUEST, **args):
        return assistant.run_tool(tool, args, mode)

    def test_declarations(self):
        from gemstone.plugin import spec
        self.assertEqual((spec.tools, spec.run_tool), ("gemstone.assistant.tools", "gemstone.assistant.run_tool"))
        service.controllers()
        tools = {t.id: t for t in assistant.tools(GUEST)}
        self.assertEqual(set(tools), {"lights_status", "set_lights"})
        self.assertTrue(all(t.modes == ALL_MODES and not t.confirm for t in tools.values()))
        self.assertTrue(tools["lights_status"].readonly)
        self.assertFalse(tools["set_lights"].readonly)
        self.assertIn("House, Hot Tub", tools["set_lights"].params["properties"]["zone"]["description"])
        with self.assertRaises(ToolError):
            assistant.run_tool("nope", {}, GUEST)

    def test_status_is_compact(self):
        patch.object(service, "tonight_event", return_value=None).start()
        text = self.run_tool("lights_status")
        self.assertEqual(text, "House: on, Warm white, 100%. Hot Tub: off. Autopilot is on until 9 PM.")
        self.assertTrue(self.run_tool("lights_status", zone="hot tub").startswith("Hot Tub: off."))
        self.assertEqual(self.fake.writes, [])

    def test_on_off(self):
        self.assertEqual(self.run_tool("set_lights", on=True, zone="hot tube"),
                         "The Hot Tub lights are on, playing Blue." + assistant._until_note())
        self.assertTrue(self.fake.states["tub"]["onState"])
        self.assertTrue(service.until()["guest"])  # guests' lights go off by 10 PM, as on the page
        self.assertIn("already on", self.run_tool("set_lights", on=True))
        self.assertEqual(self.run_tool("set_lights", on=False), "The lights are off now.")
        self.assertFalse(self.fake.states["house"]["onState"] or self.fake.states["tub"]["onState"])
        self.assertIsNone(service.until())
        self.assertEqual(self.run_tool("set_lights", on=False, zone="house"), "The House lights are already off.")
        self.run_tool("set_lights", on=True, mode=OWNER)
        self.assertIsNone(service.until())  # owners aren't limited
        self.assertEqual(service.activity()[0]["by"], "admin")

    def test_pattern_by_theme(self):
        text = self.run_tool("set_lights", pattern="christmas")
        self.assertTrue(text.startswith("The lights are on Traditional Christmas."), text)  # the most played
        self.assertIn("Others: Christmas Glam", text)
        self.assertEqual([a for a in self.fake.ops("play")], [("house", "Traditional Christmas"),
                                                              ("tub", "Traditional Christmas")])
        self.assertEqual(service.activity()[0]["by"], "guest")

    def test_unknown_pattern_suggests(self):
        with self.assertRaises(ToolError) as cm:
            self.run_tool("set_lights", pattern="chrismas tree")
        self.assertIn("Close ones", str(cm.exception))
        self.assertIn("Christmas Glam", str(cm.exception))
        with self.assertRaises(ToolError) as cm:
            self.run_tool("set_lights", pattern="zzzz")
        self.assertIn("Some favourites", str(cm.exception))
        self.assertEqual(self.fake.writes, [])

    def test_library_pattern_once_loaded(self):
        with self.assertRaises(ToolError):
            self.run_tool("set_lights", pattern="candy cane")
        service.library()
        self.assertIn("Candy cane", self.run_tool("set_lights", pattern="candy cane", zone="house"))

    def test_colour(self):
        text = self.run_tool("set_lights", color="blue", zone="house")
        self.assertEqual(text, "The House lights are on blue.")
        self.assertEqual(self.fake.states["house"]["pattern"]["id"], service.solid_id("#2f6bff"))
        with self.assertRaises(ToolError) as cm:
            self.run_tool("set_lights", color="plaid")
        self.assertIn("warm white", str(cm.exception))

    def test_pattern_at_a_brightness(self):
        self.run_tool("set_lights", pattern="christmas glam", brightness=40, zone="house")
        self.assertEqual(self.fake.states["house"]["pattern"]["brightness"], 102)
        self.assertEqual(len(self.fake.ops("play")), 1)

    def test_brightness(self):
        self.assertEqual(self.run_tool("set_lights", brightness=50),
                         "The lights are at 50%. The Hot Tub is off.")
        self.assertEqual(self.fake.states["house"]["pattern"]["brightness"], 128)
        self.assertFalse(self.fake.states["tub"]["onState"])
        self.assertEqual(self.run_tool("set_lights", brightness_change="down", zone="house"),
                         "The House lights are at 30%.")
        self.assertEqual(self.run_tool("set_lights", brightness_change="up", by=50, zone="house"),
                         "The House lights are at 80%.")
        self.assertEqual(self.run_tool("set_lights", brightness_change="down", zone="hot tub"),
                         "The Hot Tub lights are off. Turn them on first.")
        self.assertEqual(self.run_tool("set_lights", brightness=0, zone="house"), "The House lights are off now.")

    def test_bad_requests(self):
        for args in ({}, {"on": False, "pattern": "christmas"}, {"pattern": "christmas", "color": "red"}):
            with self.assertRaises(ToolError):
                self.run_tool("set_lights", **args)
        self.assertEqual(self.fake.writes, [])
