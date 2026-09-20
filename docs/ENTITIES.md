# Entities, and where each one comes from

Every entity below is created only if the vehicle actually reports the status key behind it.
Two cars on the same account will not necessarily have the same entities, and that is
deliberate: an entity backed by a key a car never sends would sit unavailable forever, which
reads like a fault rather than an absence. The same applies in reverse — an entity an older
version created for a key your car does not report is deleted the first time the new version
sees a snapshot from that car, rather than left behind greyed out. The EV entities are the one
exception: they are kept unless the car has actually reported its fuel type, because that
judgement otherwise rests on a heuristic and deleting is not undoable.

## Where the data comes from

There are three sources, on three different clocks. Knowing which one an entity uses tells you
how fresh it can be and what happens when that source is down.

| Source | What it feeds | How often | If it fails |
| --- | --- | --- | --- |
| Telemetry socket (STOMP over websocket) | every sensor and binary sensor below | pushed by the car; a full snapshot on each connection, then updates as the car reports | entities go unavailable after a 30-minute grace, because a value nobody can refresh is not a reading |
| Owner web portal | `device_tracker`, and the vehicle's real name and registration | location every 30 minutes; names once a day | location holds its last fix and reports `trusted: false`; Home Assistant asks you to sign in again if the session has gone |
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
| Vehicle state | `VEHICLE_STATE_TYPE` | any | diagnostic | on |
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

**Engine coolant temperature** reads unknown once the figure has stood unchanged for longer
than the withholding threshold. The car latches the gauge when the engine stops and keeps
pushing that number for as long as it sits there, so 89 °C is both a perfectly ordinary warm
engine and a perfectly ordinary car that went cold overnight — nothing in the value gives it
away. Showing it briefly after a drive and then admitting we no longer know is about how the
real thing behaves.

It was first gated on **Vehicle state** reporting a running engine. No car does: across four
days and several drives on two vehicles, that key was only ever `KEY_REMOVED` or
`KEY_ON_ENGINE_OFF`, because the telematics unit does not push while the engine is turning.
The sensor therefore read unknown permanently, including twenty-five minutes after a drive
with the figure sitting in the snapshot. Age judges it now, measured on the reading itself
rather than on when the car last said anything — a parked car's 12V voltage drifts down on its
own, and counting that as the car reporting in would keep a coolant figure looking current all
night.

Three are off by default on purpose. **12V battery charge** reads 0 whenever the car is asleep, so
left on it writes a meaningless sawtooth into the recorder — voltage is the real signal. **EVCC
status** exists for wallbox controllers to read, not for a dashboard: its state is the raw IEC
61851 connector letter — `A` disconnected, `B` connected but not charging, `C` charging — left
upper case because that is what consumers of it expect, which also means Home Assistant cannot
translate it. **Charge-now override** answers "why is
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
| `button` — Refresh | Re-reads what JLR already hold: the vehicle list and location, and a fresh telemetry snapshot by resubscribing to the socket. It does not wake the car, and it does nothing more than the reconnect that happens on its own every few minutes — it just does it now. Rate-limited to one resubscription a minute, and says so rather than quietly skipping it. It reports success only once a snapshot has actually arrived, not when the connection comes up — those are a fraction of a second apart, and a broker that accepts the subscription and then sends nothing would otherwise look like one that answered. |

## Readings from a car caught mid-use

Doors, windows, the bonnet and boot, central locking, the sunroof and the alarm report
**unknown** — rather than the last thing the car said — when all three of these hold:

- **The snapshot was caught mid-use.** `VEHICLE_STATE_TYPE` says somebody still had the key
  in the car, which makes the snapshot a photograph of something in progress rather than of
  how the car was left. Only states actually observed count; anything unrecognised is treated
  as settled.
- **It has gone quiet.** The readings above have not moved for longer than the threshold —
  **30 minutes** by default, configurable in the integration's options, including off. The
  clock runs on those readings alone, so a parked car's 12V voltage drifting down does not
  count as the car reporting in. It survives a restart.
- **The reading claims the car is not secure.** A door or window open, the car unlocked, the
  alarm not armed.

That last condition is the important one, and it is asymmetric on purpose. A car somebody is
walking away from moves towards shut, locked and armed — so a stale mid-use snapshot claiming
a door is *open* is the one likely to have been overtaken, and it is also the reading that
sends somebody back out to the drive at midnight. One saying the door is shut is where the car
was heading anyway, and is harmless if it is a few minutes behind.

Withholding both directions was the first attempt and it was wrong. It assumed every car
passes briefly through the mid-use state on its way to a settled one. Of the two cars this was
built on, one does — it reports `KEY_REMOVED` within minutes — and the other sits in
`KEY_ON_ENGINE_OFF` for fifteen hours at a stretch. On that car every reading went unknown
overnight, all of them shut and all of them correct.

Readings that do not decay — odometer, fuel, tyre pressures, service intervals — are never
withheld: the last figure is still the best answer available.

None of this makes the data fresher. The delay is in Jaguar Land Rover's copy and nothing here
reaches past it. It stops a guess being displayed as a fact.

## Out-of-order snapshots

A snapshot whose odometer reads lower than the one already held is discarded.
JLR deliver late and out of order, and a week-old snapshot has overwritten a
current one before now. The odometer is the only ordering key these cars give
us: they send no timestamp of their own, and the message envelope's is when the
broker sent it rather than when the car recorded it. Equal readings are still
adopted, which is the common case — a parked car redelivers the same snapshot
every four minutes.

## Degraded behaviour

| What is wrong | What you see |
| --- | --- |
| Socket down under 30 minutes | nothing; reconnects are routine and flapping every entity would be noise |
| Socket down over 30 minutes | status entities unavailable; location and the refresh button keep working, because they do not come from the socket |
| Portal session expired | Home Assistant prompts you to sign in again, on the integration and in **Settings → Repairs**; location stops updating and reports `trusted: false`; everything else is unaffected |
| Portal slow or erroring | retried; nothing is reported to you unless it keeps failing |
| Refresh token spent | Home Assistant asks you to sign in again, with a fresh emailed code |
| JLR outage or rate limit | retried with a growing backoff; no reauthentication prompt, because an outage says nothing about your credentials |
| A vehicle leaves the account | its entities disappear, its cached details are dropped, and its device can be deleted |
