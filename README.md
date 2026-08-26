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
> **Upgrading from v1.3.x or earlier: remove and re-add the integration.** v1.4.0 changed how it
> reads your car, and the new route needs a sign-in that older versions never captured. Without it
> you'll see vehicles named from their VIN and no location.
>
> This release is **read-only**. Remote control (lock, climate, charging, honk) is no longer
> available.

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

- **Location is where the last journey ended**, not a live position. It updates when a trip
  completes, so it answers "is the car home" well and "where is it mid-drive" not at all. The
  tracker carries the time of the fix, and reports nothing rather than a wrong coordinate for a
  car with no journeys logged.
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
Rover. It relies on an undocumented API that JLR could change or block tomorrow, which would break
things without warning. It was developed with AI assistance, it comes with no warranty, and you
should use it with your own account and vehicles at your own risk. There are technical notes and a
data model write-up in `docs/` if you're curious.

## Support

This was reverse-engineered and vibe-coded over many late nights, and the AI tokens don't pay for
themselves. If this integration ever saved you a trip outside to check whether you locked the car,
consider [buying me some AI tokens](https://buymeacoffee.com/willbeeching) ☕🤖. Entirely optional —
bug reports and stars are appreciated just as much.
