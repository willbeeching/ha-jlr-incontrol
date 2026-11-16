# Jaguar Land Rover InControl for Home Assistant

[![CI](https://github.com/willbeeching/ha-jlr-incontrol/actions/workflows/ci.yaml/badge.svg)](https://github.com/willbeeching/ha-jlr-incontrol/actions/workflows/ci.yaml)
[![GitHub Release](https://img.shields.io/github/v/release/willbeeching/ha-jlr-incontrol?include_prereleases)](https://github.com/willbeeching/ha-jlr-incontrol/releases)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://github.com/willbeeching/ha-jlr-incontrol/blob/master/LICENSE)
[![hacs_badge](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://github.com/hacs/integration)
[![vibe-coded](https://img.shields.io/badge/vibe-coded-ff69b4?logo=musicbrainz&logoColor=white)](https://en.wikipedia.org/wiki/Vibe_coding)
[![Buy Me A Coffee](https://img.shields.io/badge/Buy%20me%20AI%20tokens-ffdd00?logo=buymeacoffee&logoColor=black)](https://buymeacoffee.com/willbeeching)

Get your Jaguar or Land Rover into Home Assistant: fuel, doors, locks, windows, tyre pressures,
warnings, timers and where you parked it.

It reads them from the **JLR Data Hub**, JLR's official API for owners: no InControl password,
nothing reverse-engineered, and not limited to the UK and EU. If [data.jlr.com](https://data.jlr.com)
lists your cars, wherever you are, it should work for you. There's no third-party cloud in the
middle and nothing to plug into the car: the integration talks to JLR's own service from your
Home Assistant, so it runs anywhere Home Assistant does. It is read-only: it reads the car and
never sends it a command.

> [!IMPORTANT]
> **Upgrading from 2.x on the InControl sign-in?** Version 3.0.0 removed it. Your entry keeps its
> cars, entities and history, but reads nothing until you link the Data Hub, and a repair says so.
> To link it:
>
> 1. At [data.jlr.com](https://data.jlr.com), check your cars are listed, and declare each one
>    (sole or shared user) so JLR allow its location.
> 2. In Home Assistant: **Settings → Devices & Services → Jaguar Land Rover InControl →
>    Configure → Link the JLR Data Hub**, and follow [docs/DATA_HUB.md](docs/DATA_HUB.md).
>
> Every entity comes back as it was. The InControl sign-in's stored details, its password among
> them, are deleted from the entry on upgrade. If data.jlr.com does not list your cars, please
> [open an issue](https://github.com/willbeeching/ha-jlr-incontrol/issues) saying where you are.

## Where this is heading

Over August 2026 Jaguar Land Rover closed off, one after another, the routes this integration had
been using: first the login endpoint, then app attestation over the endpoints that serve vehicle
data, then the same over everything that sends a command to the car. Each time a way round was
found, and each time it looked like the last one. Remote control did not survive it, which is why
this integration has been read-only since 1.4.0 and will stay that way.

Then, in November, JLR's **Data Hub** turned out to offer owners exactly what this integration
needs: a documented API, a sign-in that is meant for third parties, and a stream
meant to deliver the car's readings as it reports them. Laid side by side with the old connection
on real cars, every value matched. Since 3.0.0 it is the only way this integration reads a car.
It is the sanctioned route, and nothing about it depends on JLR leaving a door open by accident.

It was launched under the EU Data Act, but it is not limited to the UK and EU: owners in Canada
and Australia have it working. If data.jlr.com lists your cars, wherever you are, it should work
for you.

The reverse-engineered InControl connection ran alongside it through 2.x and is gone from 3.0.0,
the release for 1 December 2026, as announced in the 2.1.2 release notes.

### Owners' right to their own data

Under the **EU Data Act** (Regulation (EU) 2023/2854), which covers connected products and names
vehicles explicitly, owners in the EU have a right to the data their vehicle generates, and a right
to have the manufacturer share it with a third party of their choosing — machine-readable and,
where technically feasible, continuously. The Data Hub looks very much like JLR's answer to it, and
it is a good one.

Outside the EU and the UK there is no such route yet. If you're one of those owners, it is worth
asking Jaguar Land Rover how you are meant to get access to your own vehicle's data; manufacturers
respond to being asked. None of this is legal advice — the detail matters. **If you ask and get a
useful answer, please post it in an issue.**

## What you get

- Vehicle status: fuel level, litres and range, odometer, service and AdBlue distance, tyre
  pressures and warnings, the 12V battery, coolant temperature, and a good deal more.
- Every door's open/closed and lock state, the windows, the sunroof, the bonnet and boot, the theft
  alarm, and warnings for fluids, the particulate filter and service, as binary sensors.
- Climate and the cabin: whether climate and air conditioning are running, the climate and
  departure timers, the fuel-fired heater, headlights, airbag and front-impact flags, and seat fold
  positions.
- **Electric and plug-in hybrid cars:** battery charge, electric and combined range, charging status
  and rate, time to full, charge limits and preconditioning.
- Only what your car has: entities are made for the readings your car reports, and the ones that
  mean nothing for it are left off (no fuel sensors on an electric car, no AdBlue on a petrol one,
  no electric sensors on a car without a charge port). Niche ones are off by default; every entity
  is listed in [docs/ENTITIES.md](docs/ENTITIES.md).
- A `device_tracker` with where the car last reported its position, and the time of that fix.
- The last journey: distance, duration, average speed and when it ended, where JLR give journeys.
- Diagnostics download for troubleshooting, with VINs, plates, positions, keys and tokens redacted.
- Distance and pressure unit overrides.
- More than one car on the account? They all show up automatically.

## Requirements

- Home Assistant 2025.8 or newer.
- A JLR account with your cars listed at [data.jlr.com](https://data.jlr.com), and your phone to
  approve the link.

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

Go to **Settings → Devices & Services → Add Integration** and search for **Jaguar Land Rover
InControl**. You create a client at [data.jlr.com](https://data.jlr.com), paste the certificate
Home Assistant shows, and approve the link on your phone. For location, declare each car at
data.jlr.com as having a **sole user** or **shared user**; JLR refuse a car's location until it is
declared. The steps are in [docs/DATA_HUB.md](docs/DATA_HUB.md). Each car appears as a device with
its own entities.

### Options

**Settings → Devices & Services → Jaguar Land Rover InControl → Configure** opens a menu:

- **Units and readings**: the settings below.
- **Link the JLR Data Hub**: on an entry from the InControl sign-in, waiting for the Data Hub. See
  [docs/DATA_HUB.md](docs/DATA_HUB.md).
- **Link the JLR Data Hub again**: shown only when the link has stopped working or its certificate
  has under 30 days left.

The two unit settings are purely about display; neither changes what is fetched or how often.

| Option | Values | Default | What it does |
| --- | --- | --- | --- |
| Distance unit | Use Home Assistant default / Miles / Kilometres | Use Home Assistant default | Forces distance sensors (odometer, ranges, distance to service) to one unit instead of following your Home Assistant unit system. |
| Pressure unit | Use Home Assistant default / kPa / bar / psi | Use Home Assistant default | The same for the four tyre pressure sensors. JLR report kPa; the other two are converted. |

Choosing a unit applies it to every sensor of that kind at the moment you save. After that, a
unit you set on one sensor in its own settings is kept through restarts and updates, and going
back to *Use Home Assistant default* only removes the unit the option set.

On the default, distance sensors created from 1.8.0 on follow your unit system: kilometres on a
metric install, miles on US customary. Sensors that already existed keep the unit they were
created with, which before 1.8.0 was always miles — change those with the option above or in the
sensor's own settings.

Saving reloads the integration, which takes a few seconds and creates nothing new — entity ids
and history are kept.

### Removing the integration

**Settings → Devices & Services → Jaguar Land Rover InControl → ⋮ → Delete.**

Everything goes with it. The Data Hub's private key, client id and refresh token live in that
config entry and are deleted with it, along with the cached vehicle details, every device and
entity it created, and any repair it raised. Nothing is stored anywhere else: there is no account
to close and no server of ours holding anything.

To revoke access at JLR's end as well, delete the client at [data.jlr.com](https://data.jlr.com).
The key Home Assistant holds is then worth nothing.

To remove **one car** but keep the rest, take it off the Data Hub client at data.jlr.com, and it
disappears here once JLR stop listing it, cached details included.
You can also delete its device directly, which Home Assistant allows once the account genuinely no
longer lists it.

## How it works

Adding the integration makes a private key inside Home Assistant and shows you the certificate
that goes with it. You paste the certificate into a client at data.jlr.com, and approve the link
on your phone with a code Home Assistant shows you. From then on every request to JLR is signed
with that key, which never leaves Home Assistant, so a token on its own is no use to anyone. JLR's
refresh token for the link does not expire, so it keeps working without you.

Status is read from JLR's REST API every 15 minutes, and the integration also holds one connection
to JLR's stream for the whole account, which brings changes within seconds of the car reporting
them, and the car's position when a journey ends. JLR send no position while the car is being
driven, as their own app does not show it. Once it has delivered a reading newer than REST had,
the REST read drops to hourly. Location is read with each status update for every car declared
at data.jlr.com, and a car JLR refuse is asked only hourly. Each reading carries
the time the car actually reported it. Where JLR give journeys, the last one's distance, duration
and average speed are read a few minutes after it ends.

It is built to be a quiet client of a service that is not ours: waiting to be told where it can,
asking on a timer only where it must, and backing off when JLR ask it to.

- **Location is where the car last reported it**, normally where it was parked, not a live
  position. It answers "is the car home" well and "where is it mid-drive" not at all. The tracker
  carries the time of the fix as a `timestamp` attribute, and a `stale` attribute once that fix is
  over a day old.
- **If the location can't be refreshed, the tracker says nothing rather than guessing.** An old fix
  still resolves to a zone, so serving one anyway doesn't produce a cautious answer — it produces a
  confident wrong one. When it can't be refreshed the state goes unknown and `trusted` is false;
  worth checking in automations that act on the car being home.
- The status you see is whatever the car last reported. Cars report on their own schedule, and
  nothing here can wake one up to ask.
- Locked and alarm-armed are independent states: a car can be locked with the alarm off.
- **Preconditioning time remaining** is a point-in-time value, not a live countdown — the car
  reports it once when preconditioning starts and it then sits still rather than ticking down.
  For a countdown that actually counts, see [docs/RECIPES.md](docs/RECIPES.md).
- If the link stops working — the client deleted at data.jlr.com, its certificate replaced or
  consent withdrawn — Home Assistant says so under **Settings → Repairs**, with what to do.

## What people use it for

- **Is the car home?** The `device_tracker` resolves to your zones, so "arrived home" and "left"
  automations work — bearing in mind it updates when the car reports its position, not
  continuously.
- **Charge on cheap rate or on solar.** **Plugged in**, **Charging** and the **Battery** percentage
  are what a tariff automation or a surplus-charging controller needs. The **EVCC status** sensor
  (off by default) reports IEC 61851 connector state for wallbox controllers that speak it.
- **Notice you left something open.** The per-door, per-window and sunroof binary sensors, usually
  combined with the car being at home and the weather turning.
- **Servicing and consumables.** Distance to service, AdBlue range, tyre pressures and the fluid
  warnings, as a monthly nudge rather than a dashboard nobody reads.
- **Preconditioning countdown.** The car reports the remaining minutes once and then sits still, so
  a live countdown needs a template sensor — there is one in the recipes.

Template sensors, automations and dashboard snippets are in
[docs/RECIPES.md](docs/RECIPES.md). Contributions very welcome.

## Documentation

| | |
| --- | --- |
| [Data Hub](docs/DATA_HUB.md) | Linking the Data Hub, how it talks to JLR, and what JLR's docs get wrong |
| [Entities](docs/ENTITIES.md) | Every entity, which cars get it, where its data comes from and how it degrades |
| [Troubleshooting](docs/TROUBLESHOOTING.md) | By symptom, not by log line |
| [Recipes](docs/RECIPES.md) | Template sensors, automations, dashboard cards |
| [Data model](docs/DATA_MODEL.md) | The status keys the entities read, for anyone extending this |

## Disclaimer

This is an unofficial community project with no affiliation to, or endorsement from, Jaguar Land
Rover. "Jaguar", "Land Rover" and "InControl" are their trademarks, used here only to describe what
this connects to. The Data Hub is JLR's documented API for owners. It was developed with
AI assistance, it comes with no warranty, and you should use it with your own account and vehicles
at your own risk. There are technical notes and a data model write-up in `docs/` if you're curious.

It reads one thing: your own vehicle's data, from your own account, into your own Home Assistant.
Nothing is routed through me or anyone else, and it sends no commands to any car.

### A note to Jaguar Land Rover

Thank you for the Data Hub. It is what this note used to ask you for: a supported, read-only way
for owners to see their own car in their own home automation, and since 3.0.0 it is the only thing
this integration uses. I would be glad to help test it anywhere it is not offered yet.

If anyone at JLR would like anything here changed or taken down, please
[open an issue](https://github.com/willbeeching/ha-jlr-incontrol/issues) or get in touch through my
GitHub profile and I'll do it. No argument and no lawyers required. This is a personal side
project, not a product and not a commercial venture.

## Support

This was reverse-engineered, then rebuilt on the Data Hub, and vibe-coded over many late nights, and the AI tokens don't pay for
themselves. If this integration ever saved you a trip outside to check whether you locked the car,
consider [buying me some AI tokens](https://buymeacoffee.com/willbeeching) ☕🤖. Entirely optional —
bug reports and stars are appreciated just as much.
