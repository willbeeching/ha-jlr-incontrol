# Recipes

Community snippets — template sensors, automations, and dashboard cards — that build on the
entities this integration provides.

The integration deliberately exposes what the car actually reports, without inventing values it
can't verify. That leaves some gaps best filled locally in Home Assistant, which is what this
page is for. (The idea is borrowed from the `msp1974/homeassistant-jlrincontrol` integration,
which had a Recipes page of its own.)

**Contributions welcome** — open a PR adding a section here, or paste your YAML in an issue and
it'll be added with credit. Please say which vehicle and model year you tested on, since JLR's
data varies between generations.

## A note on politeness

Several recipes below avoid calling **Update from vehicle** (VHS) on a timer. VHS wakes the car,
and repeatedly waking a parked JLR drains its 12V battery — the official app warns about this
too. It also puts avoidable load on JLR's servers, which this integration reaches through an
unofficial API. Prefer event-driven refreshes (a state change, arriving home, opening a
dashboard) over polling, and count down locally rather than re-asking the car.

## Preconditioning countdown

**Problem:** `sensor.<car>_preconditioning_time_remaining` is a snapshot, not a countdown. The
car sets it once when preconditioning starts and only refreshes it on a VHS, so it sits still
instead of ticking down.

**Approach:** seed a local template sensor from the integration's value when preconditioning
starts, then decrement it every minute in Home Assistant. Only fall back to a single VHS call if
the initial value comes back as 0.

_Recipe wanted._ @ismarslomic has a working version for a Jaguar I-PACE 2021 — see
[issue #8](https://github.com/willbeeching/ha-jlr-incontrol/issues/8). PR it here (or paste it in
the issue) and it'll be added with credit.

## Fresh lock & alarm state on arrival

**Problem:** `DOOR_IS_ALL_DOORS_LOCKED` and `THEFT_ALARM_STATUS` only refresh in JLR's cache when
the car next wakes, so after locking with the key fob they can read stale for hours. **Refresh**
re-reads the same stale cache; only **Update from vehicle** (VHS) wakes the car.

**Approach:** trigger the **Update from vehicle** button from an event rather than a timer — for
example when you arrive home (a `zone` trigger), or when you open the dashboard you check the car
on. You get accurate state exactly when you'd look at it, without waking the car all day.

_Recipe wanted._ See [issue #5](https://github.com/willbeeching/ha-jlr-incontrol/issues/5) for
the background.

## EVCC / surplus charging

The **EVCC status** sensor reports IEC 61851 connector state (`A` disconnected, `B` connected but
not charging, `C` charging) so wallbox controllers can consume it directly. The **Plugged in** and
**Preconditioning** binary sensors are also designed to be automated against — the latter is
useful for holding the wallbox open while the car is preconditioning on shore power.

_Recipe wanted._ If you have a working EVCC configuration, it'd be a great addition here.
