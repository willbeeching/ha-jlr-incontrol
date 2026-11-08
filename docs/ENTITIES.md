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

There are three sources, on three different clocks, plus the Data Hub when it is linked. Knowing which one an entity uses tells you
how fresh it can be and what happens when that source is down.

| Source | What it feeds | How often | If it fails |
| --- | --- | --- | --- |
| Telemetry socket (STOMP over websocket) | every sensor and binary sensor below | pushed by the car; a full snapshot on each connection, then updates as the car reports | entities go unavailable after a 30-minute grace, because a value nobody can refresh is not a reading |
| JLR Data Hub, when [linked](DATA_HUB.md) | replaces the telemetry socket for every sensor and binary sensor below, and the owner portal for `device_tracker` on cars declared at data.jlr.com | REST every 15 minutes (hourly once the stream delivers), location with it, plus the stream as the car reports | entities go unavailable after three hours without a good read; a revoked link falls back to the socket |
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

Names put the thing first (**Door front left**, **Window rear right**, **Tyre pressure front
left**, **Charge limit one-off**), because Home Assistant lists a device's entities alphabetically
and related readings then sit together. Readings worth checking now and then rather than at a
glance are in the device page's **Diagnostic** section. Renaming changed the names only: entity
ids made before it are kept, and so is their history.

## Applicability

- **any** — created if the car reports the key.
- **non-EV** — suppressed on battery-electric cars, where it is meaningless.
- **EV/PHEV** — only on cars with a charge port. ICE cars report several `EV_*` keys with
  `UNKNOWN` sentinels, so key presence alone is not enough to go on; `EV_STATE_OF_CHARGE` is the
  discriminator.
- **PHEV** — plug-in hybrids only.
- **diesel** — hidden on a car whose fuel type is known and is not diesel. Petrol cars send the
  AdBlue keys too, so presence alone is not enough. A car whose fuel type is not known keeps them:
  hiding a diesel's AdBlue warning on a guess would be the worse mistake.
- **soft-top** — only when the body type from the Data Hub says convertible or cabriolet. Every car
  sends the convertible roof key, hard-tops included.

## Sensors

| Entity | Status key | Applies to | Category | Default |
| --- | --- | --- | --- | --- |
| Alarm state | `THEFT_ALARM_STATUS` | any | diagnostic | on |
| Fuel level | `FUEL_LEVEL_PERC` | non-EV | — | on |
| Fuel range | `DISTANCE_TO_EMPTY_FUEL` | non-EV | — | on |
| Odometer | `ODOMETER_MILES` | any | — | on |
| AdBlue range | `EXT_EXHAUST_FLUID_DISTANCE_TO_SERVICE_KM` | diesel | — | on |
| Distance to service | `EXT_KILOMETERS_TO_SERVICE` | any | — | on |
| 12V battery voltage | `BATTERY_VOLTAGE` | any | diagnostic | on |
| 12V battery charge | `BATTERY_STATUS_12V_SOC` | any | diagnostic | **off** |
| Engine coolant temperature | `ENGINE_COOLANT_TEMP` | non-EV | — | on |
| Vehicle state | `VEHICLE_STATE_TYPE` | any | diagnostic | on |
| Tyre pressure ×4 | `TYRE_PRESSURE_*` | any | — | on |
| Battery | `EV_STATE_OF_CHARGE` | EV/PHEV | — | on |
| Battery range | `EV_RANGE_ON_BATTERY_MILES` | EV/PHEV | — | on |
| Combined range | `EV_PHEV_RANGE_COMBINED_MILES` | PHEV | — | on |
| Charging time to full | `EV_MINUTES_TO_FULLY_CHARGED` | EV/PHEV | — | on |
| Charging status | `EV_CHARGING_STATUS` | EV/PHEV | — | on |
| Preconditioning time remaining | `EV_PRECONDITION_REMAINING_RUNTIME_MINUTES` | EV/PHEV | — | on |
| EVCC status | derived | EV/PHEV | — | **off** |
| Charge-now override | `EV_CHARGE_NOW_SETTING` | EV/PHEV | — | **off** |
| Fuel in tank | `FUEL_LEVEL_LITRES` | non-EV, Data Hub | — | on |
| Total range | `DISTANCE_TO_EMPTY_TOTAL` | PHEV, Data Hub | — | on |
| AdBlue to refill | `EXT_EXHAUST_FLUID_VOLUME_REFILL_LITRESX10` | diesel | — | on |
| Engine immobiliser | `ENGINE_BLOCK` | any | diagnostic | on |
| Lifecycle mode | `VEHICLE_LIFECYCLE_MODE` | any, Data Hub | diagnostic | on |
| 12V battery status, health, charge estimate | `BATTERY_STATUS`, `BATTERY_STATUS_12V_HEALTH`, `BATTERY_STATUS_12V_ESTIMATE` | any | diagnostic | **off** |
| Climate mode | `CLIMATE_MODE` | any, Data Hub | — | on |
| Climate priority | `CLIMATE_PRIORITY` | any, Data Hub | diagnostic | on |
| Climate run time | `CLIMATE_STATUS_REMAINING_RUNTIME` | any | diagnostic | on |
| Climate venting time | `CLIMATE_STATUS_VENTING_TIME` | any | diagnostic | **off** |
| Fuel-fired heater time remaining | `CLIMATE_STATUS_FFH_REMAINING_RUNTIME` | not electric | diagnostic | on |
| Climate timer 1 and 2 | `CLIMATE_STATUS_TIMER{1,2}_*` | any | diagnostic | on |
| Departure timer | `DEPARTURE_TIMER_*` | any, Data Hub | diagnostic | on |
| Air purification cycle time, last cycle, started, last completed, last error | `CAC_*` | any | diagnostic | **off** |
| Seat ×7 (fold position) | `SEAT_*_FOLDED` | any, Data Hub | diagnostic | **off** |
| Colour | vehicle details | Data Hub | diagnostic | on |
| Charging time to bulk | `EV_MINUTES_TO_BULK_CHARGED` | EV/PHEV | — | on |
| Charging energy last session | `EV_ENERGY_CONSUMED_LAST_CHARGE_KWH` | EV/PHEV | — | on |
| Charge limit | `EV_CHARGE_LIMIT` | EV/PHEV, Data Hub | — | on |
| Charge limit one-off, its mode, and permanent mode | `EV_CHARGE_LIMIT_*` | EV/PHEV, Data Hub | diagnostic | **off** |
| Charging mode | `EV_CHARGING_MODE_CHOICE` | EV/PHEV, Data Hub | — | on |
| Charging rate (range per hour, and % per hour) | `EV_CHARGING_RATE_*` | EV/PHEV, Data Hub | — | on |
| Battery (actual) | `EV_STATE_OF_CHARGE_ACTUAL` | EV/PHEV, Data Hub | diagnostic | **off** |
| Consumption by speed band ×7, climbing, descending, standing | `EV_CONSUMPTION_*` | EV/PHEV, Data Hub | diagnostic | **off** |
| Usable energy (initial, from regeneration, revised) | `EV_USABLE_ENERGY_*` | EV/PHEV, Data Hub | diagnostic | **off** |
| Last updated | — | any | diagnostic | on |
| Last report (Data Hub) | Data Hub `timestamp` | linked cars | diagnostic | on |
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
| Door front/rear left/right, boot, bonnet | `DOOR_*_POSITION` | any |
| Window front/rear left/right | `WINDOW_*_STATUS` | any |
| Sunroof | `IS_SUNROOF_OPEN` | any |
| Central locking | `DOOR_IS_ALL_DOORS_LOCKED` | any |
| Door lock front/rear left/right, boot lock (off by default) | `DOOR_*_LOCK_STATUS` | cars that report them |
| Alarm armed | `THEFT_ALARM_STATUS` | any |
| Alarm triggered | `THEFT_ALARM_STATUS` | any |
| Brake fluid / coolant / oil / washer fluid warnings | `*_WARN` | any |
| AdBlue warning | `EXT_EXHAUST_FLUID_WARN` | diesel |
| Charging | `EV_CHARGING_STATUS` | EV/PHEV |
| Plugged in | `EV_CHARGING_METHOD` | EV/PHEV |
| Preconditioning | `EV_PRECONDITION_OPERATING_STATUS` | EV/PHEV |
| Bonnet lock (diagnostic, off by default) | `DOOR_ENGINE_HOOD_LOCK_STATUS` | any |
| Convertible roof | `IS_CAB_OPEN` | soft-top |
| Headlights | `IS_HEAD_LIGHTS_ON` | any |
| Panic alarm, airbag deployed, front impact | `IS_PANIC_ALARM_TRIGGERED`, `SRS_STATUS`, `FRONT_IMPACT_DETECTED` | any |
| Privacy mode (diagnostic) | `PRIVACY_SWITCH` | any |
| Particulate filter warning | `EXT_PARTICULATE_FILTER_WARN` | any: not hidden on petrol cars, many of which have one too |
| Tyre warning front/rear left/right | `TYRE_STATUS_*` | any |
| Climate, air conditioning | `CLIMATE_STATUS_OPERATING_STATUS`, `AIR_CONDITIONING_ACTIVE` | any |
| Climate timer, departure timer set (diagnostic) | `CLIMATE_STATUS_TIMER_ACTIVATION_STATUS`, `DEPARTURE_TIMER_IS_SET` | any |
| Fuel-fired heater enabled (diagnostic) | `CLIMATE_FFH_SETTING` | not electric |
| Air purification (diagnostic, off by default) | `CAC_OPERATING_STATE` | any |
| Seat held ×7 (diagnostic, off by default) | `SEAT_*_INHIBITED` | any, Data Hub |
| Charging (flag, off by default), charging cable connected, battery conditioning | `EV_IS_CHARGING`, `EV_IS_PLUGGED_IN`, `EV_IS_PRECONDITIONING` | EV/PHEV |
| Data Hub stream (diagnostic) | — | cars on a linked Data Hub |

**Data Hub** in the table means the reading comes only from the Data Hub: the telemetry socket had
no key for it. The electric rows marked Data Hub are from JLR's published catalog, with the
catalog's units, and have not yet been checked against an electric car's own display; if one reads
wrong on yours, please open an issue. The **Total range** is made only for plug-in hybrids: on a car
with one source of energy it is that source's range again. **Climate run time** reads 30 with the climate
off: it is how long a run lasts, and counts down only while one is going. The climate timers carry
no year, so each is shown in the year that puts it nearest to now. Departure and climate timers are
read in Home Assistant's time zone, as the car keeps its owner's clock. A departure timer dated
before 2015 reads unknown: JLR send a date in 2000 for no timer, which showed as "26 years ago".
The fuel-fired heater is left off electric cars, which send its keys without having one. A car
whose fuel type JLR do not give counts as electric when it reports a battery charge and no fuel
level; an existing heater entity on such a car is kept rather than deleted on that judgement.

The bonnet lock is off by default and not part of the security readings: it is a latch outside
central locking, and reads locked with every door open. `DOOR_IS_BOOT_LOCKED` and the kilometre electric
range have no entity of their own: they say what the boot lock and electric range already say.

On the Data Hub, a plug-in car's battery and charging come from JLR's EV endpoint, which has no
charging status of its own. **Charging status** is then made from its charging and plugged-in flags:
charging, not connected, a fault, or **Plugged in, not charging** when it is plugged in and not
charging, since the endpoint does not say whether that is paused, waiting or full. See
[DATA_HUB.md](DATA_HUB.md).

Unfitted hardware commonly reports `UNKNOWN`, which would otherwise read as "window open" or
"warning active". Those map to *unknown* rather than to a state.

The per-door locks are for what central locking cannot say: single-point entry unlocks the
driver's door alone, leaving one door unlocked and the rest locked. They come from the same
snapshot as central locking, so they are exactly as stale as it is — switch them on for the
detail, not for fresher news. Only `LOCKED` and `UNLOCKED` are understood; anything else, such as
whatever a double-locked door reports, reads *unknown* rather than being guessed as either.

With the [Data Hub linked](DATA_HUB.md), every entity above that reads a status key gets its value
from the Data Hub instead of the telemetry socket: same entity, same key, different source.

The two Data Hub entities exist only once the Data Hub is linked. **Last report
(Data Hub)** is the time the car sent its latest reading, as JLR stamp it: the newest of the
hourly status snapshot and every signal the stream delivers. **Data Hub stream** is on while the
stream is connected; it is one connection for the account, so every linked car shows the same.

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
