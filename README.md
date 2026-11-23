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
  saved. Tap ♡ on any card to make it a favourite (a library pattern is saved into your folder as
  it does). Tapping a card plays it without moving the page; once the big picture has scrolled
  away a slim now-playing bar with the same Lights switch and brightness stays at the top, its
  picture zoomed so every light is in view. `#patterns` links open Tonight at All patterns.
- **Create** — pick colours, an animation, speed, brightness and direction. Designs save
  automatically; "Live on lights" pushes the design to the real lights while you edit.
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
