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

Everything comes from the [JLR Data Hub](DATA_HUB.md), on a few different clocks. Knowing which
one an entity uses tells you how fresh it can be and what happens when it fails.

| What | What it feeds | How often | If it fails |
| --- | --- | --- | --- |
| Stream (MQTT) | every sensor and binary sensor below | as the car reports, about 16 seconds behind it | readings fall back to the status reads below; nothing goes unavailable |
| Status (REST) | every sensor and binary sensor below | every 15 minutes, hourly once the stream has delivered | entities go unavailable after three hours without a good read or a connected stream, because a value nobody can refresh is not a reading |
| Location (REST, and the stream's position topic) | `device_tracker` | with each status read, and about half a minute after a journey ends | the tracker holds its last fix and reports `trusted: false` once it is over three hours old |
| Journeys (REST) | the last-journey sensors and **Recent journeys** | five minutes after a journey ends, and every six hours | the sensors keep the last journeys they read |
| Vehicle details (REST) | the device's name, model and registration | once a day | the names already held are kept |

The vehicle list is the cars selected on the client you created at data.jlr.com; it is checked
every 15 minutes, so adding or removing a car there shows up here without a restart. The stream
reconnects about once an hour by design, when its session runs out; that is not an outage and
does not flap entities.

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
  hiding a diesel's AdBlue warning on a guess would be the worse mistake. So does a car counting
  down to an AdBlue service, whatever its fuel type says: the Data Hub has called a diesel Range
  Rover petrol.
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
| Charging rate (range per hour, in mph when the distance option is miles; and % per hour) | `EV_CHARGING_RATE_*` | EV/PHEV, Data Hub | — | on |
| Battery (actual) | `EV_STATE_OF_CHARGE_ACTUAL` | EV/PHEV, Data Hub | diagnostic | **off** |
| Consumption by speed band ×7, climbing, descending, standing | `EV_CONSUMPTION_*` | EV/PHEV, Data Hub | diagnostic | **off** |
| Usable energy (initial, from regeneration, revised), in kWh: JLR send usable and revised in hundredths of a kWh and regeneration in Wh | `EV_USABLE_ENERGY_*` | EV/PHEV, Data Hub | diagnostic | **off** |
| Last updated | — | any | diagnostic | on |
| Last report (Data Hub) | Data Hub `timestamp` | linked cars | diagnostic | on |
| Last journey distance, duration, average speed, and when it ended (with `started` as an attribute); distance and speed follow the distance option | Data Hub journeys | cars JLR give journeys for | — | on |
| Last journey energy used (kWh) or fuel used (litres) | Data Hub journeys, `energyUsed` | only once a journey carries it | — | on |
| Recent journeys: how many are held, with the last 20 in the `journeys` attribute, newest first | Data Hub journeys | cars JLR give journeys for | — | on |
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

The **Last journey** sensors appear only once JLR have answered with a journey for the car, so an
account still refused journeys has none. They change when a journey ends, not while one is under
way: the read follows the position the stream sends at the end of a journey, five minutes later,
with a read every six hours as a fallback. The last-journey sensors are single readings and carry
no places; where each journey started and ended is in **Recent journeys**.
**Energy used** and **fuel used** come from one optional field in JLR's spec, `energyUsed`, which is
in kWh or litres depending on the car; each sensor appears only once a journey has carried it, and a
journey without it reads unknown rather than zero. JLR's journeys carry no MPG, EV distance,
regeneration or efficiency figures, so there are no sensors for those.

**Recent journeys** is a trip history for dashboards: its state is how many journeys it holds, up
to 20, and its `journeys` attribute lists them newest first. Each has JLR's `journey_id`, which
stays the same as the rest is filled in and is the thing to match on when saving them, `started`
and `ended`,
`duration_min`, `distance_km`, `average_speed_kmh`, `energy_used_kwh` or `fuel_used_l` when JLR
send it, and `start` and `end` as `{latitude, longitude}`. The units are in the names because an
attribute is not converted with the distance option the way a sensor's state is; JLR's list
starts on 30 October 2026, so there is nothing earlier. The list is read with the journeys
above, not more often. It is kept out of the recorder: the entity always has the current list,
but its past versions are not written to the database, which keeps it small and keeps a
location history out of it. After a restart the list is back with the first journeys read,
within minutes. [RECIPES.md](RECIPES.md#journey-history) has a card for it.

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
| Brake fluid / coolant / washer fluid warnings | `*_WARN` | any |
| Oil level warning | `EXT_OIL_LEVEL_WARN` | not electric |
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

**Data Hub** in the table means the reading is new with the Data Hub: the InControl connection
before 3.0 had no key for it. The electric rows marked Data Hub are from JLR's published catalog, with the
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
charging, since the endpoint does not say whether that is paused, waiting or full. A car that
sends its charging status but nothing about the plug, as one I-Pace does, gets **Plugged in** and
**EVCC status** from that status instead: not connected is unplugged, and charging, waiting,
paused, full or initialising is plugged in. A fault says neither, and they read unknown. See
[DATA_HUB.md](DATA_HUB.md).

Unfitted hardware commonly reports `UNKNOWN`, which would otherwise read as "window open" or
"warning active". Those map to *unknown* rather than to a state.

The per-door locks are for what central locking cannot say: single-point entry unlocks the
driver's door alone, leaving one door unlocked and the rest locked. They come from the same
snapshot as central locking, so they are exactly as stale as it is — switch them on for the
detail, not for fresher news. Only `LOCKED` and `UNLOCKED` are understood; anything else, such as
whatever a double-locked door reports, reads *unknown* rather than being guessed as either.

**An armed alarm means locked.** While the car reports the alarm armed, central locking and the
per-door locks read locked, even where their own readings say unlocked. The car sends its alarm
before its locks catch up: an I-Pace locked and went to sleep on a report saying armed with every
door unlocked, and nothing followed to correct it. JLR cars arm only once locked, and unlocking
disarms first. The alarm reading has to be at least as new as the lock's: the Data Hub keeps each
reading as it last arrived, and an unlock that arrives before its disarm shows unlocked. With the
alarm off they read what the car sent, and a lock the car did not report, or reported as unknown,
stays unknown. The boot lock always reads what the car sent, because Land Rover document the
exterior alarm staying armed with the tailgate open, and so does the bonnet latch, which is outside
central locking and the alarm.

The entities kept the status keys the InControl connection used, so an install upgraded from 2.x
kept its entity ids and history: the Data Hub's signal names are mapped onto those keys.

The two Data Hub entities are diagnostics about the link itself. **Last report
(Data Hub)** is the time the car sent its latest reading, as JLR stamp it: the newest of the
hourly status snapshot and every signal the stream delivers. **Data Hub stream** is on while the
stream is connected; it is one connection for the account, so every linked car shows the same.

## Other platforms

| Entity | Notes |
| --- | --- |
| `device_tracker` — Location | Where the last completed journey ended, not a live position. Carries `timestamp` for the fix, `trusted` for whether it is recent enough to act on, and `stale` once over a day old. |
| `button` — Refresh | Reads the status and location JLR already hold, now rather than at the next scheduled read. It does not wake the car. Rate-limited to one read a minute, and says so rather than quietly skipping it. It reports success only if this car's status was actually read: JLR answering for another car on the account, or refusing this one, is reported as a failure. |

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

## Out-of-order readings

A status reading older than the one already held, by the car's own report time, is discarded.
The stream and the status reads overlap, and either can deliver a reading the other has already
overtaken. Equal times are still adopted, which is the common case: a parked car's hourly read
returns what the stream already said.

## Degraded behaviour

| What is wrong | What you see |
| --- | --- |
| Stream down | nothing; status is read every 15 minutes instead, and the stream reconnects with a growing backoff |
| Stream and status reads both failing for over three hours | status entities unavailable; location and the refresh button keep working, because they do not depend on the latest read |
| JLR refuse a car's status or location | a repair names the car and what to change at data.jlr.com; that car's other readings are unaffected |
| The link is revoked at data.jlr.com, or its certificate has run out | the integration asks to be reconfigured, which links the Data Hub again; entities go unavailable but are kept, with their history |
| JLR outage or rate limit | retried with a growing backoff, and logged once at info when it starts and once when it ends; no prompt, because an outage says nothing about your link |
| A vehicle leaves the account | its entities disappear, its cached details are dropped, and its device can be deleted |
