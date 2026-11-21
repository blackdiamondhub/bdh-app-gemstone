# bdh-app-gemstone

A [Black Diamond Hub](https://github.com/blackdiamondlodge/BlackDiamondHub) app for
[Gemstone Lights](https://gemstonelights.com/) permanent outdoor lighting. It talks to the
Gemstone cloud through [pygemstone](https://pypi.org/project/pygemstone/) and works with any
number of controllers.

## Views

- **Tonight** — what's playing on each selected controller, power, brightness, "keep on until"
  and favourites.
- **Patterns** — your favourites, your folders ("Ours"), the Gemstone library and solid colours.
  Tapping a pattern plays it right away on every selected controller, with an Undo toast.
- **Create** — pick colours, an animation, speed, brightness and direction. Designs save
  automatically; "Live on lights" pushes the design to the real lights while you edit.
- **Setup** (admins) — per controller, upload a house photo (stylized to a night look on the
  hub), draw the light runs along the roofline, mark where each run starts and how many LEDs it
  has, and blink the first 10 lights to find the real start. Without a photo the app draws a
  simple house with one straight run.

Guests (anyone not signed in as a hub admin) get Tonight, Patterns and Create. Their designs go
into a Gemstone folder called **Guest designs** with a date-coded name, they can only keep the
lights on until 10 PM, and a daily job deletes guest designs older than 30 days.

## Settings

| Setting | |
|---|---|
| `gemstone_email` | Gemstone account email (falls back to `GEMSTONE_EMAIL`) |
| `gemstone_password` | Gemstone account password (secret; falls back to `GEMSTONE_PASSWORD`) |

Writes to the Gemstone cloud are sent one at a time, about two seconds apart, because the cloud
silently drops overlapping commands. Reads are cached briefly.
