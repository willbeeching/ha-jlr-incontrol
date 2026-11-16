# Troubleshooting, by symptom

Start here rather than with the logs. Most of these look alike from the outside and have quite
different causes.

Before filing anything: **Settings → Devices & Services → Jaguar Land Rover InControl → ⋮ →
Download diagnostics**. VIN, registration, IMEI, telematics serial and coordinates are all
redacted, so it is safe to attach to a public issue.

---

## "Home Assistant asks me to link the Data Hub"

- **The repair says the entry was set up on the InControl sign-in.** Version 3.0.0 removed that
  sign-in, and an entry that had only it reads nothing until the Data Hub is linked. Its devices,
  entities and their history are all kept. Follow [DATA_HUB.md](DATA_HUB.md) from **Configure →
  Link the JLR Data Hub**; the entities pick up again as soon as it is linked. Deleting and
  re-adding the integration instead loses the entity ids your automations and dashboards use.
- **The integration asks to be reconfigured.** JLR no longer accept the link: the client was
  deleted at data.jlr.com, its certificate was replaced, or consent was withdrawn. Choose
  **Reconfigure** on it under **Settings → Devices & Services**; it runs the same steps as the
  first link, on the same entry, and the entities are kept.
- **The repair says the link needs renewing.** The certificate lasts a year. **Link the JLR Data
  Hub again** makes a new one to paste at data.jlr.com, the same way as the first time.

An outage, a rate limit or a refusal for one car will **not** ask you to link again. If you were
asked, JLR genuinely refused the link.

## "Location is missing, or in the wrong place"

- **It has never appeared.** JLR refuse a car's location until it is declared at data.jlr.com;
  see [below](#there-is-no-location).
- **It is out of date.** It is where the last *completed* journey ended, not a live position:
  JLR send no position while the car is driven, as their own app hides it then. The tracker
  carries a `timestamp` attribute for the fix.
- **It went to unknown.** Deliberate. When the fix cannot be refreshed the tracker reports nothing
  rather than serving an old one — an old fix still resolves to a zone, so serving it anyway gives
  a confident wrong answer instead of a cautious one. Check the `trusted` attribute in automations
  that act on the car being home.

## "The doors show as open but they are not"

Check whether the entity is actually a door sensor. **Central locking** reports locked/unlocked,
and a group or template that mixes it in with the door sensors will read "open" whenever the car
is unlocked. This has caught people out more than once, including the maintainer.

If a genuine door sensor is wrong, it is JLR's cached value: the car reports on its own schedule,
and nothing here can wake it. **Last updated** tells you how old the reading is.

## "Lock or alarm state is hours out of date"

Real, and not fixable from here. Those two keys only refresh in JLR's cache when the car next
wakes on its own. Waking it took a remote command, which JLR now gate behind their app's device
attestation. The official app can still do it. Gate anything that must not act on a stale lock
state on the **Last updated** sensor.

## "A door, window or lock shows Unknown for a while"

Unknown is a deliberate answer, for one of two reasons:

- **The car said nothing useful.** It reported `UNKNOWN`, sent a value the integration does not
  understand, or left the key out of the snapshot altogether.
- **The reading was held back.** A snapshot taken while somebody was still in the car, saying a
  door is open or unlocked, stops being shown once it has gone quiet. The next snapshot clears it.
  See **Readings from a car caught mid-use** in [ENTITIES.md](ENTITIES.md).

With [debug logging](#turning-on-debug-logging) on, each sensor logs one line saying which, the
moment it turns Unknown — for example `vehicle_1a2b3c door_front_left_lock reads unknown: 'X' is
not a value it understands`. That line is what to paste into an issue.

## "Distances are in miles, or a unit I changed goes back"

Before 1.8.0 every distance sensor was created in miles whatever your unit system, and a unit
changed in a sensor's own settings was put back on every restart while the **Distance unit**
option was on its default. Both are fixed: a unit you pick by hand now stays, and new sensors
follow your unit system. Sensors created by an earlier release keep miles until you change them —
for all of them at once, set **Distance unit** to Kilometres under Configure. See
[Options](../README.md#options).

## "Everything is unavailable"

- **After upgrading to 3.0.** The entry was on the InControl sign-in alone; see
  [above](#home-assistant-asks-me-to-link-the-data-hub).
- **With the integration asking to be reconfigured, or a repair about the link.** Do what it
  says; see
  [above](#home-assistant-asks-me-to-link-the-data-hub).
- **Otherwise.** Readings are kept for three hours after the last good read before anything is
  marked unavailable, so this means JLR have not answered for that long. The integration keeps
  retrying with a growing backoff and recovers on its own once JLR do. It says so once when reads
  or the stream start failing (`Data Hub reads failing: …`, `Data Hub stream failing: …`, with the
  reason) and once when they work again. Those lines are at info, which Home Assistant does not
  show by default: [turn on debug logging](#turning-on-debug-logging) to see them.
- **Location and the refresh button still work.** That is correct — they do not depend on the
  latest read.

## "A car I sold is still there"

It should disappear on the next 15-minute poll once the account no longer lists it, along with
its cached details. If the device remains, delete it: **the device page → ⋮ → Delete**. That is
refused while the account still lists the car, and also refused if the last account read failed —
a failed listing is not evidence anyone's car is gone.

## "A car I just added is missing"

Only the cars selected on the client you created at data.jlr.com are shared. Edit the client
there and select the new car, with all data categories; it then appears within 15 minutes without
a restart. If it does not, attach diagnostics.

## "Some sensors exist on one car and not the other"

Intended. Entities are created only for the status keys a given car actually reports, because one
backed by a key that never arrives would sit unavailable forever. See
[ENTITIES.md](ENTITIES.md) for what applies to what.

## "An entity disappeared after an update"

Entities are matched to the status keys a car actually reports, and that matching has tightened
over several versions — a diesel-only AdBlue sensor on a petrol car, rear doors on a two-door.
Anything an older version created that your car does not report is now removed rather than left
behind permanently greyed out.

Two things are never removed. A car that is silent — flat 12V, no signal — keeps every entity it
has, because "not reporting anything" and "does not have it" are not the same thing; removal only
happens for a car that has just sent a snapshot. And the EV entities are kept whenever the car has
not told us its fuel type. Deciding a car is not electric from the absence of a battery reading is
a good enough reason not to create a sensor and nowhere near good enough to delete one, so an
unwanted EV entity on a petrol car stays until the fuel type is known.

Removals are logged at INFO, which most installs do not show. To see them, turn on debug logging
for the integration *before* the restart that will do the removing.

## "An entity I want is not there at all"

Three are off by default — 12V battery charge, EVCC status and the charge-now override. Switch
them on in the entity's settings. See [ENTITIES.md](ENTITIES.md) for why each one is off.

## "There is no location"

Home Assistant says which car under **Settings → Repairs**. JLR refuse each car's location until
the car is **declared** at data.jlr.com. Open your vehicle
list there and complete the declaration for each car, saying whether you are its **sole user** or a
**shared user**. The next read picks it up, within the hour; diagnostics show the answer per car
under `datahub` → `location` (`refused: …` before, `permitted` after).

## "There are no Last journey sensors, or they show an old journey"

- **None at all.** They are created only once JLR answer with a journey for the car, and JLR
  refused journeys to every owner account until November 2026. Look in the diagnostics under
  `datahub` → `journeys`: `refused` means JLR still refuse them for that car, and the integration
  asks again once a day.
- **An old journey.** JLR's list starts on 30 October 2026, so nothing before that appears. The
  integration reads five minutes after the stream reports the journey's end, and again every six
  hours. On a Range Rover the journey was there at that first read, but one owner's recent drives
  were missing from JLR's list for a while. If a drive is still missing a day later, please say so
  on an issue. If the stream is off, a drive waits for the six-hourly read.

## "The Data Hub link will not finish, or stopped working"

See [DATA_HUB.md](DATA_HUB.md#if-it-is-not-working). The two usual causes are the stream not
switched on for the client at data.jlr.com, and a certificate at data.jlr.com that is not the one
Home Assistant showed — a new one is made each time you start linking, so paste the latest.

## "The setup or options screens have no text, or no certificate"

Blank menu buttons, or a Data Hub step that is just a `client_id` box with no certificate above
it, mean the Home Assistant app is showing the text it saved before the integration updated. The
labels and the step that shows the certificate are both text it loads. Fully close and reopen the
app, or clear its cache, or do the step in a browser instead; an owner on the Android Companion
app had exactly this, and the browser showed everything.

## "data.jlr.com shows no data categories to choose"

When creating access credentials at data.jlr.com, the data categories come from JLR. An empty
list, with **Next** greyed out, is a fault on JLR's side, not something you did: an "API ERROR
MESSAGE" flashes up for a few seconds. One owner captured the response behind it in the browser's
developer tools: a `502 Bad Gateway` from `/v1/categories` saying **"Powertrain data is missing from
the vehicle record"**. JLR's own record for the car is incomplete, and only they can fix it.
Contact JLR's Data Hub support, linked from developer.jlr.com, with that message and the
`correlationId` from the same response. An entry upgraded from 2.x keeps its entities while it
waits, and picks them up again once the Data Hub is linked.

## Turning on debug logging

**Settings → Devices & Services → Jaguar Land Rover InControl → Enable debug logging**, reproduce,
then disable it to download the log. VINs and coordinates are scrubbed from these lines, as are
the account's own identifiers and tokens if a server quotes them back.

## Filing a good issue

[Open one here](https://github.com/willbeeching/ha-jlr-incontrol/issues) with the diagnostics
download, the vehicle model and year, and what you expected instead. Model year matters more than
it sounds: JLR's data varies noticeably between generations.
