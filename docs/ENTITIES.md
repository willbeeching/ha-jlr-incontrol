# Entities, and where each one comes from

Every entity below is created only if the vehicle actually reports the status key behind it.
Two cars on the same account will not necessarily have the same entities, and that is
deliberate: an entity backed by a key a car never sends would sit unavailable forever, which
reads like a fault rather than an absence.

## Where the data comes from

There are three sources, on three different clocks. Knowing which one an entity uses tells you
how fresh it can be and what happens when that source is down.

| Source | What it feeds | How often | If it fails |
| --- | --- | --- | --- |
| Telemetry socket (STOMP over websocket) | every sensor and binary sensor below | pushed by the car; a full snapshot on each connection, then updates as the car reports | entities go unavailable after a 30-minute grace, because a value nobody can refresh is not a reading |
| Owner web portal | `device_tracker`, and the vehicle's real name and registration | location every 30 minutes; names once a day | location holds its last fix and reports `trusted: false`; a repair appears if the session has gone |
| IF9 REST API | the vehicle list, and the account housekeeping | every 15 minutes | setup retries; nothing already on screen is lost |

The socket reconnects roughly every five minutes by design — the session is bound to an access
token with about that lifetime — and each reconnect re-delivers a full snapshot. A reconnect is
not an outage and does not flap entities.

**Nothing here wakes the car.** Waking it took a remote command, which JLR now gate behind their
app's device attestation. `DOOR_IS_ALL_DOORS_LOCKED` and `THEFT_ALARM_STATUS` in particular only
refresh in JLR's cache when the car next wakes on its own, so after locking with the key fob they
can read stale for hours. Treat them as "last known", and use **Last updated** to say how old
that is.

## Applicability

- **any** — created if the car reports the key.
- **non-EV** — suppressed on battery-electric cars, where it is meaningless.
- **EV/PHEV** — only on cars with a charge port. ICE cars report several `EV_*` keys with
  `UNKNOWN` sentinels, so key presence alone is not enough to go on; `EV_STATE_OF_CHARGE` is the
  discriminator.
- **PHEV** — plug-in hybrids only.

## Sensors

| Entity | Status key | Applies to | Category | Default |
| --- | --- | --- | --- | --- |
| Alarm state | `THEFT_ALARM_STATUS` | any | diagnostic | on |
| Fuel level | `FUEL_LEVEL_PERC` | non-EV | — | on |
| Fuel range | `DISTANCE_TO_EMPTY_FUEL` | non-EV | — | on |
| Odometer | `ODOMETER_MILES` | any | — | on |
| AdBlue range | `EXT_EXHAUST_FLUID_DISTANCE_TO_SERVICE_KM` | any | — | on |
| Distance to service | `EXT_KILOMETERS_TO_SERVICE` | any | — | on |
| 12V battery voltage | `BATTERY_VOLTAGE` | any | diagnostic | on |
| 12V battery charge | `BATTERY_STATUS_12V_SOC` | any | diagnostic | **off** |
| Engine coolant temperature | `ENGINE_COOLANT_TEMP` | non-EV | — | on |
| Tyre pressure ×4 | `TYRE_PRESSURE_*` | any | — | on |
| Battery | `EV_STATE_OF_CHARGE` | EV/PHEV | — | on |
| Electric range | `EV_RANGE_ON_BATTERY_MILES` | EV/PHEV | — | on |
| Combined range | `EV_PHEV_RANGE_COMBINED_MILES` | PHEV | — | on |
| Time to full charge | `EV_MINUTES_TO_FULLY_CHARGED` | EV/PHEV | — | on |
| Charging status | `EV_CHARGING_STATUS` | EV/PHEV | — | on |
| Preconditioning time remaining | `EV_PRECONDITION_REMAINING_RUNTIME_MINUTES` | EV/PHEV | — | on |
| EVCC status | derived | EV/PHEV | — | **off** |
| Charge-now override | `EV_CHARGE_NOW_SETTING` | EV/PHEV | — | **off** |
| Last updated | — | any | diagnostic | on |
| All info | — | any | diagnostic | **off** |

Three are off by default on purpose. **12V battery charge** reads 0 whenever the car is asleep, so
left on it writes a meaningless sawtooth into the recorder — voltage is the real signal. **EVCC
status** exists for wallbox controllers to read (IEC 61851 connector state: `A` disconnected, `B`
connected not charging, `C` charging), not for a dashboard. **Charge-now override** answers "why is
this plugged-in car not charging", which matters when it matters and is noise otherwise. Switch
any of them on in the entity settings.

## Binary sensors

| Entity | Status key | Applies to |
| --- | --- | --- |
| Front/rear left and right doors, boot, bonnet | `DOOR_*_POSITION` | any |
| Front/rear left and right windows | `WINDOW_*_STATUS` | any |
| Sunroof | `IS_SUNROOF_OPEN` | any |
| Central locking | `DOOR_IS_ALL_DOORS_LOCKED` | any |
| Alarm armed | `THEFT_ALARM_STATUS` | any |
| Alarm triggered | `THEFT_ALARM_STATUS` | any |
| Brake fluid / coolant / oil / washer fluid / AdBlue warnings | `*_WARN` | any |
| Charging | `EV_CHARGING_STATUS` | EV/PHEV |
| Plugged in | `EV_CHARGING_METHOD` | EV/PHEV |
| Preconditioning | `EV_PRECONDITION_OPERATING_STATUS` | EV/PHEV |

Unfitted hardware commonly reports `UNKNOWN`, which would otherwise read as "window open" or
"warning active". Those map to *unknown* rather than to a state.

## Other platforms

| Entity | Notes |
| --- | --- |
| `device_tracker` — Location | Where the last completed journey ended, not a live position. Carries `timestamp` for the fix, `trusted` for whether it is recent enough to act on, and `stale` once over a day old. |
| `button` — Refresh | Re-reads what JLR already hold, including location. It does not wake the car. |

## Degraded behaviour

| What is wrong | What you see |
| --- | --- |
| Socket down under 30 minutes | nothing; reconnects are routine and flapping every entity would be noise |
| Socket down over 30 minutes | status entities unavailable; location and the refresh button keep working, because they do not come from the socket |
| Portal session expired | a repair in **Settings → Repairs**; location stops updating and reports `trusted: false`; everything else is unaffected |
| Portal slow or erroring | retried; nothing is reported to you unless it keeps failing |
| Refresh token spent | Home Assistant asks you to sign in again, with a fresh emailed code |
| JLR outage or rate limit | retried with a growing backoff; no reauthentication prompt, because an outage says nothing about your credentials |
| A vehicle leaves the account | its entities disappear, its cached details are dropped, and its device can be deleted |
