# Jaguar Land Rover InControl for Home Assistant

[![CI](https://github.com/willbeeching/ha-jlr-incontrol/actions/workflows/ci.yaml/badge.svg)](https://github.com/willbeeching/ha-jlr-incontrol/actions/workflows/ci.yaml)
[![GitHub Release](https://img.shields.io/github/v/release/willbeeching/ha-jlr-incontrol?include_prereleases)](https://github.com/willbeeching/ha-jlr-incontrol/releases)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://github.com/willbeeching/ha-jlr-incontrol/blob/master/LICENSE)
[![hacs_badge](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://github.com/hacs/integration)
[![vibe-coded](https://img.shields.io/badge/vibe-coded-ff69b4?logo=musicbrainz&logoColor=white)](https://en.wikipedia.org/wiki/Vibe_coding)
[![Buy Me A Coffee](https://img.shields.io/badge/Buy%20me%20AI%20tokens-ffdd00?logo=buymeacoffee&logoColor=black)](https://buymeacoffee.com/willbeeching)

Get your Jaguar or Land Rover into Home Assistant. Fuel level, doors, windows, tyre pressures,
where you parked it, and (if you want) remote lock and climate control. All you need is the email
and password you use for the InControl app.

There's no third-party cloud in the middle and nothing to plug into the car. The integration talks
to JLR's own backend, so it runs anywhere Home Assistant does.

> [!WARNING]
> **Early release, and AI-assisted.** I built this by reverse-engineering an undocumented API,
> with a lot of help from AI coding tools. Expect rough edges. The read-only stuff (sensors,
> location) is the most reliable part; remote commands may behave differently between models.
> Use at your own risk, and if something misbehaves, please
> [open an issue](https://github.com/willbeeching/ha-jlr-incontrol/issues).
>
> **Community-tested so far** (see [issue #1](https://github.com/willbeeching/ha-jlr-incontrol/issues/1)):
>
> | Vehicle | Powertrain | Year |
> |---|---|---|
> | Jaguar I-PACE | BEV | 2020 |
> | Range Rover Sport | PHEV | — |
> | Discovery Sport | PHEV | 2025 |
> | Defender | ICE | 2022 |
> | Range Rover | ICE | 2022 |
>
> ICE 2022 models remain the best-tested for remote commands. BEV and PHEV support is newer but
> early community reports are positive. Some EV-specific commands (ECC preconditioning, VHS
> refresh, charge control) are implemented from native-app API docs and may still need tweaks on
> the webview backend — please report errors.

## What you get

- Live vehicle status: fuel level and range, odometer, service/AdBlue distance, tyre pressures,
  12V battery, coolant temperature, and a fair bit more.
- **BEV support:** battery SoC, electric range, charging status, time to full, and charge-now
  control. ICE-only sensors (fuel level, coolant temp, combined range) are automatically hidden
  on pure electric vehicles.
- Every door's open/closed and lock state, all four windows, the sunroof, theft alarm status,
  and warnings for fluids and service as binary sensors.
- A GPS `device_tracker` so you can see where the car is, along with heading, speed, and when it
  last phoned home.
- If you provide your vehicle PIN: remote lock, honk & flash, and alarm off.
- **Climate:** ICE/PHEV uses remote engine start (REON/REOFF) with heat and cool modes
  plus a target temperature. BEVs use electric preconditioning (ECC) with a target
  temperature — no PIN required for ECC.
- **Update from vehicle** button (VHS) to force the car to report fresh status, plus a cheap
  **Refresh** button that re-polls the server cache.
- **Charge now** switch for BEVs (force charge on/off).
- **Last trip** sensor with distance and trip metadata (when the trips API is available).
- **All info** sensor (disabled by default) exposing the full flattened status dict as attributes.
- Diagnostics download for troubleshooting (VIN/position redacted).
- Configurable distance and pressure unit overrides in integration options.
- Got more than one car on the account? They all show up automatically.

## Requirements

- Home Assistant 2024.4 or newer
- An InControl account with your vehicle(s) added to it
- Your account email and password
- Your vehicle security PIN for lock, honk & flash, alarm off, and charge control. BEV climate
  (ECC) works without a PIN.

## Installation

### HACS (recommended)

1. In HACS, open **⋮** → **Custom repositories** and add this repo with category **Integration**.
2. Install **Jaguar Land Rover InControl**.
3. Restart Home Assistant.

### Manual

Copy `custom_components/jlr_incontrol/` into your Home Assistant `config/custom_components/`
folder and restart.

## Setup

1. Go to **Settings → Devices & Services → Add Integration** and search for
   **Jaguar Land Rover InControl**.
2. Enter your InControl email and password.
3. Optionally enter your vehicle PIN. If you leave it blank you get monitoring only (plus BEV
   climate if you have an electric vehicle). You can add the PIN later by reconfiguring the entry.

Each vehicle shows up as a device with its sensors, binary sensors, and control entities.

### Options

Under **Configure** on the integration entry you can override distance units (miles / km) and
pressure units (kPa / bar / psi). Leave as "Use Home Assistant default" to let HA convert
automatically.

> **HACS beta releases:** enable **Show beta versions** on the custom repository in HACS
> (⋮ menu → Show beta versions) to pick a tagged release (e.g. `v1.0.0-beta.4`) instead of
> tracking the default branch.

## How it works

Login is a standard password grant, and the integration then reads status and sends commands
through the same webview API that JLR's own apps use, with a registered device id. Your
credentials, device id, user id, and vehicle info are fetched at setup and stored in your own
Home Assistant config entry. None of it goes anywhere else.

Status is polled every 5 minutes by default. Tokens are long-lived and refresh automatically, so
you shouldn't need to log in again.

A couple of things worth knowing:

- The status you see is whatever the car last reported to JLR's servers. Use **Update from
  vehicle** (VHS) to wake the car and push fresh data, or **Refresh** to re-fetch the cached
  copy from the backend.
- The `last_updated` timestamp reflects when the car last reported position/status to JLR — it
  may lag behind individual values like SoC during charging.
- Remote commands wake the car, so they take a few seconds. The integration waits for the vehicle
  to confirm before reporting success or failure.
- ECC preconditioning, VHS refresh, charge control, and trip data use endpoints documented from
  the native-app API. They may need media-type tweaks on the webview edge — please report errors.

## Disclaimer

This is an unofficial community project with no affiliation to, or endorsement from, Jaguar Land
Rover. It relies on an undocumented API that JLR could change or block tomorrow, which would break
things without warning. It was developed with AI assistance, it's an early release, it comes with
no warranty, and remote commands may behave differently between models. Use it with your own
account and vehicles, at your own risk. There are more technical notes and a data model write-up
in `docs/` if you're curious.

## Support

This was reverse-engineered and vibe-coded over many late nights, and the AI tokens don't pay for
themselves. If this integration ever saved you a trip outside to check whether you locked the car,
consider [buying me some AI tokens](https://buymeacoffee.com/willbeeching) ☕🤖. Entirely optional —
bug reports and stars are appreciated just as much.
