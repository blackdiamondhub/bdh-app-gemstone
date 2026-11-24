# bdh-app-gemstone

A [Black Diamond Hub](https://github.com/blackdiamondlodge/BlackDiamondHub) app for
[Gemstone Lights](https://gemstonelights.com/) permanent outdoor lighting. It talks to the
Gemstone cloud through [pygemstone](https://pypi.org/project/pygemstone/) and works with any
number of controllers.

## Views

- **Tonight** — what's playing on each selected controller, brightness, tonight's schedule and
  the Autopilot event, then every pattern on one scrolling page: a ♥ Favourites strip, **All
  patterns** (chips for each of your folders and for solid colours; the chips stay at the top
  while you scroll) and, folded away at the bottom, the Gemstone library patterns you haven't
  saved. Each card's ⋯ menu (or a long press) offers Edit / Edit a copy, add to or remove from
  favourites (a library pattern is saved into your folder as it does) and, for your own designs,
  Delete — it happens at once with an Undo toast for a few seconds; library and Gemstone-managed
  patterns can't be deleted, and guests can only delete Guest designs. Every card tags
  🏠 Custom and 👤 Guest designs and shows ♥ on favourites. Copies are named "Sunset (copy)",
  "Sunset (copy 2)", ….
  Tapping a card plays it without moving the page; once the big picture has scrolled
  away a slim now-playing bar with the same Lights switch and brightness stays at the top, its
  picture zoomed so every light is in view. `#patterns` links open Tonight at All patterns.
- **Create** — pick colours, an animation, speed, brightness and direction. Designs save
  automatically; "↺ Discard changes" (shown once you edit a saved design) puts back the design
  as it was when Create opened. "Show on house" tries the design on the real lights while you edit.
- **Plan** (admins only) — 📅 in the rail. **Coming up** shows what plays on each group of lights
  over the next 2 weeks (or the season), merging Autopilot and schedules; whatever is beaten is
  shown struck through under the winner. Below it the **Autopilot** settings and the
  **Schedules**: a saved design on some lights, every day / some days / a date range, from
  sunset, sunrise or a time (sunset/sunrise can be shifted) until a time or sunrise. A schedule
  is one Gemstone timer per light; matching timers made in the Gemstone app show as one schedule.
  Saves go to the lights one at a time (each waits for the controller to confirm), so they run in
  the background. Two schedules of the same kind starting at exactly the same time on the same
  lights are refused. What wins: date ranges, then Autopilot, then every day / some days; among
  equals the latest start, and when it ends the lights go back to whatever else is still on.
  Tonight shows the playing schedule (📅 … · until …) to everyone, or when it resumes after
  someone played something else.
- **Setup** (admins, hub Settings → Gemstone → Photos & light paths) — per controller, upload a
  photo of the house and draw the light runs on it. The preview spaces dots evenly along each run
  and patterns flow through the runs in the order they were drawn. Tap a point to delete it, or a
  line to cut it in two. Without a photo the app draws a simple house with one straight run.

Guests (anyone not signed in as a hub admin) get Tonight and Create. Their designs go
into a Gemstone folder called **Guest designs** (named "Untitled", "Untitled (1)", … until they
rename them by tapping the title). Create previews on screen only; **Show on house** puts the design
on the lights until they tap **Keep it** or **Back** (Back, leaving Create, or 10 idle minutes put the
previous pattern back). Guests can only keep the
lights on until 10 PM, and a daily job deletes guest designs older than 30 days. On Tonight's
schedule a guest's off time must be strictly after tonight's on time (sunset + offset) and no
later than 10 PM; without a location (no sunset) it must be after 5 PM instead. Admins aren't
limited.

## Settings

| Setting | |
|---|---|
| `gemstone_email` | Gemstone account email (falls back to `GEMSTONE_EMAIL`) |
| `gemstone_password` | Gemstone account password (secret; falls back to `GEMSTONE_PASSWORD`) |

Writes to the Gemstone cloud are sent one at a time, about two seconds apart, because the cloud
silently drops overlapping commands. Reads are cached briefly.

The brightness slider only changes lights that are on; it never turns one on.

Gemstone's Autopilot can skip its off time for a controller that was sent a command during the
evening, so the app double-checks: 3 minutes after Autopilot's off time it turns off any
Autopilot controller that is still on, once a night, unless "keep on until" is holding it on.

Every command the app sends (play, live, power, brightness, autopilot changes and its own
off-time enforcement) is kept in a log of the last 300, with who sent it (admin, guest or the
app itself). Admins can read it at `api/activity/`.
