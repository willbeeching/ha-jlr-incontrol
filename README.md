# Jaguar Land Rover InControl for Home Assistant

[![CI](https://github.com/willbeeching/ha-jlr-incontrol/actions/workflows/ci.yaml/badge.svg)](https://github.com/willbeeching/ha-jlr-incontrol/actions/workflows/ci.yaml)
[![GitHub Release](https://img.shields.io/github/v/release/willbeeching/ha-jlr-incontrol)](https://github.com/willbeeching/ha-jlr-incontrol/releases)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://github.com/willbeeching/ha-jlr-incontrol/blob/master/LICENSE)
[![hacs_badge](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://github.com/hacs/integration)
[![vibe-coded](https://img.shields.io/badge/vibe-coded-ff69b4?logo=musicbrainz&logoColor=white)](https://en.wikipedia.org/wiki/Vibe_coding)

Bring your Jaguar or Land Rover (InControl) vehicle into Home Assistant — live status,
location, and remote controls — using just your **InControl email and password**.

Works on petrol, diesel, and electric models. No third‑party cloud service and no dongle — the
integration talks to JLR's backend directly and runs anywhere Home Assistant does (Raspberry Pi,
x86, VM, HAOS, container).

> [!WARNING]
> **Early release, and AI‑assisted.** This integration was built by reverse‑engineering an
> undocumented, unofficial API with heavy help from AI coding tools. It is an early release: expect
> rough edges, and be aware that some features — particularly the PIN‑gated remote commands (lock,
> climate, honk & flash) — have had only limited real‑world testing and may not work on every
> vehicle. Read‑only monitoring is the best‑tested path. Use it at your own risk, and please
> [open an issue](https://github.com/willbeeching/ha-jlr-incontrol/issues) if something misbehaves.

## Features

- **Live vehicle status** — fuel level & range, odometer, service/AdBlue distance, tyre pressures,
  12V battery, coolant temperature, and more.
- **Doors, windows & security** — per‑door open/closed and lock state, all four windows, sunroof,
  theft‑alarm status, and fluid/service warnings as binary sensors.
- **Location** — a GPS `device_tracker` with heading, speed, and last‑seen timestamp.
- **Remote controls** *(optional, needs your vehicle PIN)* — lock, climate pre‑conditioning, alarm
  off, and honk‑&‑flash, plus a refresh button.
- **Multiple vehicles** — every car on your account is added automatically.

## Requirements

- Home Assistant 2024.4 or newer.
- A Jaguar/Land Rover **InControl** account with your vehicle(s) added.
- Your account **email and password**.
- *(Optional)* your **vehicle security PIN**, to enable remote commands.

## Installation

### HACS (recommended)

1. In HACS → **⋮** → **Custom repositories**, add this repository with category **Integration**.
2. Install **Jaguar Land Rover InControl**.
3. Restart Home Assistant.

### Manual

Copy `custom_components/jlr_incontrol/` into your Home Assistant `config/custom_components/` folder
and restart.

## Setup

1. Go to **Settings → Devices & Services → Add Integration** and search for
   **Jaguar Land Rover InControl**.
2. Enter your InControl **email** and **password**.
3. *(Optional)* enter your **vehicle PIN** to enable remote commands. Leave it blank for
   monitoring‑only — the lock, climate, and honk entities are simply not created.

Your vehicles appear as devices with their sensors, binary sensors, and (if a PIN was provided)
controls.

## How it works

The integration authenticates with a standard password grant and reads/commands vehicles through
JLR's browser (webview) API surface, using a registered device id. Everything personal — your
credentials, device id, user id, and vehicles — is obtained at setup and stored only in your own
Home Assistant config entry. Nothing is shared with any third party.

Data is polled every 5 minutes by default. Access tokens are long‑lived and refreshed
automatically.

## Options & notes

- **Monitoring‑only vs. control:** without a PIN you get the full read‑only picture; adding a PIN
  unlocks the command entities. You can reconfigure the entry later to add or change the PIN.
- **Command timing:** remote commands wake the car and can take a few seconds; the integration
  waits for the vehicle to confirm and reports the outcome.
- **Polling:** status reflects what the vehicle last reported. Use the refresh button to request a
  fresh update from the car.

## Disclaimer

This is an unofficial, community integration and is not affiliated with, authorised by, or endorsed
by Jaguar Land Rover. It talks to an undocumented API that JLR can change or block at any time,
which may break the integration without warning. It was developed with AI assistance and is an early
release — provided "as is", with no warranty, and remote commands may not behave identically across
models. Use it only with your own account and vehicles, and at your own risk. See `docs/` for the
data model and technical notes.
