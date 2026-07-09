# Jaguar Land Rover InControl for Home Assistant

[![CI](https://github.com/willbeeching/ha-jlr-incontrol/actions/workflows/ci.yaml/badge.svg)](https://github.com/willbeeching/ha-jlr-incontrol/actions/workflows/ci.yaml)
[![GitHub Release](https://img.shields.io/github/v/release/willbeeching/ha-jlr-incontrol)](https://github.com/willbeeching/ha-jlr-incontrol/releases)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://github.com/willbeeching/ha-jlr-incontrol/blob/master/LICENSE)
[![hacs_badge](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://github.com/hacs/integration)
[![vibe-coded](https://img.shields.io/badge/vibe-coded-ff69b4?logo=musicbrainz&logoColor=white)](https://en.wikipedia.org/wiki/Vibe_coding)
[![Buy Me A Coffee](https://img.shields.io/badge/Buy%20me%20AI%20tokens-ffdd00?logo=buymeacoffee&logoColor=black)](https://buymeacoffee.com/willbeeching)

Get your Jaguar or Land Rover into Home Assistant. Fuel level, doors, windows, tyre pressures,
where you parked it, and (if you want) remote lock and climate control. All you need is the email
and password you use for the InControl app.

There's no third-party cloud in the middle and nothing to plug into the car. The integration talks
to JLR's own backend, so it runs anywhere Home Assistant does. Petrol, diesel, and electric models
all work.

> [!WARNING]
> **Early release, and AI-assisted.** I built this by reverse-engineering an undocumented API,
> with a lot of help from AI coding tools. Expect rough edges. The read-only stuff (sensors,
> location) is the best-tested part. The PIN-gated remote commands (lock, climate, honk & flash)
> have had limited real-world testing and might not work on every vehicle. Use at your own risk,
> and if something misbehaves, please
> [open an issue](https://github.com/willbeeching/ha-jlr-incontrol/issues).

## What you get

- Live vehicle status: fuel level and range, odometer, service/AdBlue distance, tyre pressures,
  12V battery, coolant temperature, and a fair bit more.
- Every door's open/closed and lock state, all four windows, the sunroof, theft alarm status,
  and warnings for fluids and service as binary sensors.
- A GPS `device_tracker` so you can see where the car is, along with heading, speed, and when it
  last phoned home.
- If you provide your vehicle PIN: remote lock, climate pre-conditioning, alarm off, and
  honk & flash, plus a button to request a fresh update from the car.
- Got more than one car on the account? They all show up automatically.

## Requirements

- Home Assistant 2024.4 or newer
- An InControl account with your vehicle(s) added to it
- Your account email and password
- Your vehicle security PIN, but only if you want the remote commands

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
3. Optionally enter your vehicle PIN. If you leave it blank you get monitoring only, and the
   lock/climate/honk entities simply aren't created. You can add the PIN later by reconfiguring
   the entry.

Each vehicle shows up as a device with its sensors, binary sensors, and (if you gave a PIN) the
control entities.

## How it works

Login is a standard password grant, and the integration then reads status and sends commands
through the same webview API that JLR's own apps use, with a registered device id. Your
credentials, device id, user id, and vehicle info are fetched at setup and stored in your own
Home Assistant config entry. None of it goes anywhere else.

Status is polled every 5 minutes by default. Tokens are long-lived and refresh automatically, so
you shouldn't need to log in again.

A couple of things worth knowing:

- The status you see is whatever the car last reported. Hit the refresh button if you want it to
  wake up and send fresh data.
- Remote commands wake the car, so they take a few seconds. The integration waits for the vehicle
  to confirm before reporting success or failure.

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
