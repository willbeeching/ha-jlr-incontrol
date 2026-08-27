# Jaguar Land Rover InControl for Home Assistant

[![CI](https://github.com/willbeeching/ha-jlr-incontrol/actions/workflows/ci.yaml/badge.svg)](https://github.com/willbeeching/ha-jlr-incontrol/actions/workflows/ci.yaml)
[![GitHub Release](https://img.shields.io/github/v/release/willbeeching/ha-jlr-incontrol?include_prereleases)](https://github.com/willbeeching/ha-jlr-incontrol/releases)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://github.com/willbeeching/ha-jlr-incontrol/blob/master/LICENSE)
[![hacs_badge](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://github.com/hacs/integration)
[![vibe-coded](https://img.shields.io/badge/vibe-coded-ff69b4?logo=musicbrainz&logoColor=white)](https://en.wikipedia.org/wiki/Vibe_coding)
[![Buy Me A Coffee](https://img.shields.io/badge/Buy%20me%20AI%20tokens-ffdd00?logo=buymeacoffee&logoColor=black)](https://buymeacoffee.com/willbeeching)

Get your Jaguar or Land Rover into Home Assistant. Fuel level, doors, windows, tyre pressures and
where you parked it. All you need is the email and password you use for the InControl app.

There's no third-party cloud in the middle and nothing to plug into the car. The integration talks
to JLR's own backend, so it runs anywhere Home Assistant does.

> [!IMPORTANT]
> **Upgrading from v1.3.x or earlier: sign in again once.** v1.4.0 changed how it reads your car,
> and reading location and vehicle names needs a sign-in that older versions never captured.
> Without it you'll see vehicles named from their VIN and no location.
>
> **Settings → Devices & Services → Jaguar Land Rover InControl → ⋮ on the entry → Reconfigure**,
> then your password and the emailed code. Your entities, history and automations are kept —
> there's no need to remove and re-add anything.
>
> This release is **read-only**. Remote control (lock, climate, charging, honk) is no longer
> available.

## Where this is heading

Read this before you build anything important on top of it.

Over August 2026 Jaguar Land Rover closed off the routes this integration used, one after another.
First the login endpoint, then app attestation over the endpoints that serve vehicle data, then
the same over everything that sends a command to the car. Each time, a way round has been found —
sign-in moved to the flow the app itself uses, status moved to the real-time telemetry connection,
and location and vehicle names now come from the owner web portal.

But that is three workarounds in a fortnight, and the pattern is not ambiguous: JLR are steadily
shutting the door on anything that is not their own app. The two routes still open are open
because nobody has closed them yet, not because they are meant to be used. **Assume this is the
last working version.** If the telemetry connection or the portal goes the way of the rest, there
may be nothing left to move to, and that will be the end of it rather than another update.

Remote control has already reached that point. Every surface has been checked and there is no way
to actuate the car without the app's attestation, so this integration is read-only now and will
stay that way.

I'm sorry. I know a fair few people have built automations and dashboards on this, and it is
genuinely useful right up until the moment it isn't. I'll keep it working for as long as there is
something to work with, and if it does stop for good I'll say so plainly here rather than leave
anyone guessing. And if JLR ask me to stop, I will — see the
[note to them](#a-note-to-jaguar-land-rover) at the end.

### Owners' right to their own data

Worth knowing, if you'd rather none of this were necessary.

Under the **EU Data Act** (Regulation (EU) 2023/2854), which covers connected products and names
vehicles explicitly, owners in the EU have a right to the data their vehicle generates, and a right
to have the manufacturer share it with a third party of their choosing — machine-readable and,
where technically feasible, continuously. It also limits using technical measures to frustrate
those rights.

That is the route this integration ought to be unnecessary alongside. If you're in the EU, you can
ask Jaguar Land Rover directly how an owner is meant to get continuous access to their own vehicle
data, and what the compliant route is. In the UK the equivalent is weaker: UK GDPR gives you access
and portability, which means a copy of your data on request rather than a live feed.

None of that makes this integration official or sanctioned, and none of it is legal advice — the
Data Act's applicability dates are staggered and the detail matters. But manufacturers respond to
being asked, and the more owners who ask for proper access, the less anyone needs a project like
this one. **If you do ask and get a useful answer, please post it in an issue** — a documented,
supported route would be a better outcome than another workaround.

## What you get

- Live vehicle status, pushed as the car reports it: fuel level and range, odometer,
  service/AdBlue distance, tyre pressures, 12V battery, coolant temperature, and a fair bit more.
- **BEV support:** battery charge, electric range, charging status, time to full and
  preconditioning time remaining. ICE-only sensors are hidden automatically on electric cars.
- Every door's open/closed and lock state, all four windows, the sunroof, theft alarm status, and
  warnings for fluids and service, as binary sensors.
- A GPS `device_tracker` showing where the car finished its last journey, with the time of the fix.
- **All info** sensor (disabled by default) exposing the full status dict as attributes.
- Diagnostics download for troubleshooting, with VIN, plate and position redacted.
- Configurable distance and pressure unit overrides.
- More than one car on the account? They all show up automatically.

## Requirements

- Home Assistant 2024.4 or newer
- An InControl account with your vehicle(s) added to it
- Your account email and password

## Installation

### HACS (recommended)

1. In HACS, open **⋮** → **Custom repositories** and add this repo with category **Integration**.
2. Install **Jaguar Land Rover InControl**.
3. Restart Home Assistant.

To pick up beta releases, enable **Show beta versions** on the custom repository in HACS.

### Manual

Copy `custom_components/jlr_incontrol/` into your Home Assistant `config/custom_components/`
folder and restart.

## Setup

1. Go to **Settings → Devices & Services → Add Integration** and search for
   **Jaguar Land Rover InControl**.
2. Enter your InControl email and password.
3. Enter the verification code JLR emails you. It expires quickly, so have your inbox open.

Each vehicle appears as a device with its own sensors.

### Options

Under **Configure** on the integration entry you can override distance units (miles / km) and
pressure units (kPa / bar / psi). Leave both as "Use Home Assistant default" to let Home Assistant
convert automatically.

## How it works

Sign-in uses the same OpenID Connect flow as JLR's own app, which finishes with a verification code
emailed to you. Only a renewable token, a session, your device id and your user id are stored in
your own Home Assistant config entry — your password is not kept, and none of it goes anywhere
else.

**Vehicle status is pushed, not polled.** The integration holds open the same real-time telemetry
connection JLR's app uses and subscribes to each vehicle on the account. A car's full status
arrives the moment it subscribes, and updates stream in as the vehicle reports them, so values
change in Home Assistant when they change in the car.

That's also the polite way round, which matters for an unofficial integration: rather than asking
JLR's servers the same question on a timer forever, it waits to be told. What remains on a schedule
is light — renewing the token and, every half hour, re-reading the parked location.

Worth knowing:

- **Location is where the last journey ended**, not a live position. It appears once a completed
  journey has been processed, which can be hours after you park, so it answers "is the car home"
  well and "where is it mid-drive" not at all. The tracker carries the time of the fix as a
  `timestamp` attribute, and a `stale` attribute once that fix is over a day old.
- **If the location can't be refreshed, the tracker says nothing rather than guessing.** An old fix
  still resolves to a zone, so serving one anyway doesn't produce a cautious answer — it produces a
  confident wrong one. When it can't be refreshed the state goes unknown and `trusted` is false;
  worth checking in automations that act on the car being home.
- The status you see is whatever the car last reported. Vehicles report on their own schedule,
  and this integration relays those reports as they arrive.
- Locked and alarm-armed are independent states: a car can be locked with the alarm off.
- **Preconditioning time remaining** is a point-in-time value, not a live countdown — the car
  reports it once when preconditioning starts and it then sits still rather than ticking down.
  For a countdown that actually counts, see [docs/RECIPES.md](docs/RECIPES.md).
- Sign-in sessions eventually expire. When that happens Home Assistant will ask you to sign in
  again, with a fresh emailed code.

## Recipes

Template sensors, automations and dashboard snippets that build on these entities live in
[docs/RECIPES.md](docs/RECIPES.md). Contributions very welcome.

## Disclaimer

This is an unofficial community project with no affiliation to, or endorsement from, Jaguar Land
Rover. "Jaguar", "Land Rover" and "InControl" are their trademarks, used here only to describe what
this connects to. It relies on undocumented APIs that JLR are actively closing off — see
[Where this is heading](#where-this-is-heading) — and it can stop working without warning. It was developed with AI assistance, it comes with no warranty, and you
should use it with your own account and vehicles at your own risk. There are technical notes and a
data model write-up in `docs/` if you're curious.

It reads one thing: your own vehicle's data, from your own account, into your own Home Assistant.
Nothing is routed through me or anyone else, and it sends no commands to any car.

### A note to Jaguar Land Rover

If anyone at JLR would like this changed or taken down, please
[open an issue](https://github.com/willbeeching/ha-jlr-incontrol/issues) or get in touch through my
GitHub profile and I'll do it. No argument and no lawyers required. This is a personal side
project, not a product and not a commercial venture, built so owners can see their own car in their
own home automation. If that isn't something you want, say so and it stops.

Better still, I would rather work with you than around you. If there is any appetite for a
sanctioned route — a supported API, a developer programme, or even a narrow read-only one covering
an owner's own vehicle — I would be glad to talk, to help build or test it, and to retire this
project in favour of it. The demand is plainly there: people want to see their own car in their own
home automation, and nothing more than that. Serving it properly would suit everyone better than
blocking it, and I would much rather be on the right side of that.

## Support

This was reverse-engineered and vibe-coded over many late nights, and the AI tokens don't pay for
themselves. If this integration ever saved you a trip outside to check whether you locked the car,
consider [buying me some AI tokens](https://buymeacoffee.com/willbeeching) ☕🤖. Entirely optional —
bug reports and stars are appreciated just as much.
