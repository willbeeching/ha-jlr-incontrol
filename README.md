# Jaguar Land Rover InControl for Home Assistant

[![CI](https://github.com/willbeeching/ha-jlr-incontrol/actions/workflows/ci.yaml/badge.svg)](https://github.com/willbeeching/ha-jlr-incontrol/actions/workflows/ci.yaml)
[![GitHub Release](https://img.shields.io/github/v/release/willbeeching/ha-jlr-incontrol?include_prereleases)](https://github.com/willbeeching/ha-jlr-incontrol/releases)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://github.com/willbeeching/ha-jlr-incontrol/blob/master/LICENSE)
[![hacs_badge](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://github.com/hacs/integration)
[![vibe-coded](https://img.shields.io/badge/vibe-coded-ff69b4?logo=musicbrainz&logoColor=white)](https://en.wikipedia.org/wiki/Vibe_coding)
[![Buy Me A Coffee](https://img.shields.io/badge/Buy%20me%20AI%20tokens-ffdd00?logo=buymeacoffee&logoColor=black)](https://buymeacoffee.com/willbeeching)

> [!IMPORTANT]
> ### Update to v1.4.0 — vehicle data now arrives in real time, remote commands are blocked
>
> Around **25 August 2026** JLR extended their app-attestation wall (Approov) over the endpoints
> this integration read vehicle data from. They answer `498` to anything that is not the signed
> JLR app, and no combination of headers gets past it. Every version up to v1.3.4 shows the car
> as **unavailable** as a result — often with nothing useful in the log.
>
> **[v1.4.0](https://github.com/willbeeching/ha-jlr-incontrol/releases/tag/v1.4.0) fixes the
> readings** by moving them to the real-time telemetry websocket JLR's app uses, which is not
> attested. This is genuinely better than what it replaces: status is **pushed the moment the car
> reports it** instead of being polled every few minutes.
>
> **Remote commands (lock, climate, charging, honk) do not work in v1.4.0.** They run over the
> same walled endpoints and there is currently no way through. The buttons are still there and
> will tell you plainly why they failed rather than silently doing nothing. Whether commands can
> be moved to the websocket too is [being looked
> at](https://github.com/willbeeching/ha-jlr-incontrol/issues/12).
>
> If your vehicles show a brand and four digits ("Land Rover 8558") after updating, that is the
> same wall: the endpoint serving make, model and nickname is blocked, so the name falls back to
> the marque from the VIN plus its last four characters — which at least tells two cars apart.
> Names already known are kept, and the real ones return by themselves if JLR lift the wall.

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
> | Jaguar I-PACE | BEV | 2019 |
> | Jaguar I-PACE | BEV | 2021 |
> | Range Rover Sport | PHEV | — |
> | Discovery Sport | PHEV | 2025 |
> | Defender | ICE | 2022 |
> | Range Rover | ICE | 2022 |
> | Range Rover (L405) | ICE | 2019 |
>
> ICE 2022 models remain the best-tested for remote commands. BEV and PHEV support is newer but
> early community reports are positive — ECC preconditioning is now confirmed working on the
> I-PACE (see [issue #3](https://github.com/willbeeching/ha-jlr-incontrol/issues/3)). VHS
> refresh and charge control are implemented from native-app API docs and may still need tweaks
> on the webview backend — please report errors.

## What you get

- Live vehicle status: fuel level and range, odometer, service/AdBlue distance, tyre pressures,
  12V battery, coolant temperature, and a fair bit more.
- **BEV support:** battery SoC, electric range, charging status, time to full, preconditioning
  time remaining, and charge control. ICE-only sensors (fuel level, coolant temp, combined range)
  are automatically hidden on pure electric vehicles.
- Every door's open/closed and lock state, all four windows, the sunroof, theft alarm status,
  and warnings for fluids and service as binary sensors.
- A GPS `device_tracker` so you can see where the car is, along with heading, speed, and when it
  last phoned home.
- If you provide your vehicle PIN: remote lock, honk & flash, and alarm off.
- **Climate:** ICE/PHEV uses remote engine start (REON/REOFF) with a single heat/cool mode —
  set the target temperature and the car decides whether to heat or cool (15.5&nbsp;°C is LO on
  the car's dial, 28.5&nbsp;°C is HI). BEVs use electric preconditioning (ECC) with a target
  temperature — no PIN required for ECC.
- **Update from vehicle** button (VHS) to force the car to report fresh status, plus a cheap
  **Refresh** button that re-polls the server cache.
- **Force charge on / off** buttons for BEVs, with a **Charge now setting** sensor showing the
  live override state (`DEFAULT` / `FORCE_ON` / `FORCE_OFF`).
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
3. Enter the verification code JLR emails you.
4. Optionally enter your vehicle PIN. If you leave it blank you get monitoring only (plus BEV
   climate if you have an electric vehicle) — you can add it later, see below.

Each vehicle shows up as a device with its sensors, binary sensors, and control entities.

### Adding or changing the vehicle PIN

Plenty of people set this up read-only first and want the controls later. You don't have to
remove and re-add the integration (and you won't need another verification code):

**Settings → Devices & Services → Jaguar Land Rover InControl → ⋮ on the entry → Reconfigure**

Enter the PIN and submit. The entry reloads and the PIN-gated entities appear: door lock,
honk & flash, alarm off, charge control, and ICE/PHEV climate. Clearing the box removes the PIN
again and drops you back to monitoring only. Your sign-in is untouched either way.

The PIN is your **vehicle security PIN** — the one the InControl app asks for before a remote
command, not your account password.

### Options

Under **Configure** on the integration entry you can override distance units (miles / km) and
pressure units (kPa / bar / psi). Leave as "Use Home Assistant default" to let HA convert
automatically.

> **HACS beta releases:** enable **Show beta versions** on the custom repository in HACS
> (⋮ menu → Show beta versions) to pick a tagged release (e.g. `v1.0.0-beta.4`) instead of
> tracking the default branch.

## How it works

Sign-in uses the same OpenID Connect flow as JLR's own app, which finishes with a verification
code emailed to you. Only a renewable token, your device id, and your user id are stored in your
own Home Assistant config entry — your password is not kept. None of it goes anywhere else.

Once set up it runs unattended: the token renews itself. You'll only be asked to sign in again
(and for a fresh emailed code) if that renewal stops working.

**Vehicle data is pushed, not polled.** The integration holds open the same real-time telemetry
connection JLR's app uses and subscribes to each vehicle on your account. The car's full status
arrives the instant it is subscribed, and updates stream in as the vehicle reports them — so
values change in Home Assistant when they change in the car, rather than up to twenty minutes
later. One connection covers every vehicle.

This is also the polite way round, which matters for an unofficial integration: instead of asking
JLR's servers the same question on a timer forever, it waits to be told. The only thing still on a
schedule is housekeeping every fifteen minutes — renew the token, keep the device registration
alive, and notice a vehicle being added or removed.

A couple of things worth knowing:

- **Remote commands are currently blocked by JLR** (lock, unlock, climate, charging, honk and
  flash, and the *Update from vehicle* / VHS button). They use endpoints that now demand app
  attestation, and pressing one gives an error saying so. Reading works; controlling does not.
- The status you see is whatever the car last reported to JLR's servers, which is not the same
  as what the car is doing right now — the vehicle reports on its own schedule, and this
  integration relays those reports as they arrive. While commands are blocked there is no way
  to prompt the car for an update.
- The `last_updated` timestamp reflects when the car last reported position/status to JLR — it
  may lag behind individual values like SoC during charging.
- **Location may be missing.** The position endpoint is behind the same wall, and it is not yet
  confirmed whether the telemetry connection carries position for every vehicle. Where it does
  not, the device tracker simply has no coordinates rather than showing a wrong one.
- Locked and alarm-armed are independent states: a car can be locked with the alarm off. A
  remote lock command both locks and arms. Alarm state changes reach the backend in ~30 seconds.
- Remote commands wake the car, so they take a few seconds. The integration waits for the vehicle
  to confirm before reporting success or failure.
- **Preconditioning time remaining** is a point-in-time value, not a live countdown. The car
  reports it once when preconditioning starts (~29 minutes on an I-PACE) and only refreshes it
  on a VHS, so it will sit still rather than tick down — and occasionally reports 0 at the start.
  Preconditioning started from the JLR app can take ~5 minutes to appear in Home Assistant at
  all. If you want a ticking countdown, see [docs/RECIPES.md](docs/RECIPES.md) — it's better done
  locally in HA than by repeatedly waking the car.
- ECC preconditioning, VHS refresh, and charge control use endpoints documented from
  the native-app API. They may need media-type tweaks on the webview edge — please report errors.

### Why there's no trip / journey data

Short version: JLR turned it off. The `/trips` endpoint still exists on the webview edge (it
negotiates media types — a wrong `Accept` returns a proper 406), but with the correct
`triplist-v2` media type the legacy backend behind it never answers and eventually returns a
504 Gateway Timeout (verified with waits of 70+ seconds). Two other signals confirm it's gone
for good: the modern app/webview JS bundle contains no trip endpoints at all, and the old
direct `/if9/jlr/` path that community integrations used for trips is now behind JLR's Approov
attestation wall. A last-trip sensor would just sit there timing out and slow every refresh
down, so this integration deliberately doesn't have one. If JLR ever resurfaces journeys in
their API, it can come back.

The legacy owner web portal (`incontrol.jaguar.com`) was also checked as an alternative
source: it still has a Journeys feature, but it lives behind its own web SSO cookie login
(a separate auth world from the token flow this integration uses), and its trips backend is
the same service that times out. Not a viable path either.

### Why there's no Guardian Mode

Half-supported by the backend, not by our path to it. The Guardian endpoints (`gm/status`,
`gm/alarms`) exist and respond quickly, and PIN authentication for the `GM` service works —
but the webview edge this integration relies on doesn't expose usable Guardian reads: the
endpoints are POST-only there and reject every content type, the settings paths 404, and the
webview app's own code contains no Guardian feature at all. The native app does Guardian over
its Approov-attested path, which we can't use. Toggling Guardian Mode in the JLR app also has
no effect on any status key this integration can see, so there's nothing to sense either.

## Recipes

Template sensors, automations, and dashboard snippets that build on these entities live in
[docs/RECIPES.md](docs/RECIPES.md) — including a local preconditioning countdown and how to get
fresh lock/alarm state without waking the car on a timer. Contributions very welcome.

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
