# Troubleshooting, by symptom

Start here rather than with the logs. Most of these look alike from the outside and have quite
different causes.

Before filing anything: **Settings → Devices & Services → Jaguar Land Rover InControl → ⋮ →
Download diagnostics**. VIN, registration, IMEI, telematics serial and coordinates are all
redacted, so it is safe to attach to a public issue.

---

## "It keeps asking me to sign in again"

Signing in costs an emailed code, so this is the most expensive failure here and the one worth
getting right.

- **Every few days, or after a restart.** Expected up to a point: JLR rotate the refresh token on
  every renewal and retire the old one immediately. The integration persists each new token, so a
  restart should not cost a sign-in. If it does, the entry is not being written — attach
  diagnostics.
- **Immediately after signing in.** The account may be signed in on another device that has
  invalidated this session. Sign in once more and leave it.
- **Only location stopped, and there is a repair about the portal.** That is the *other* session —
  the owner web portal's — not your credentials. Use **⋮ → Reconfigure** on the entry: it signs in
  again and keeps every entity id, so your automations and dashboards survive. Deleting and
  re-adding the integration does not.

An outage, a rate limit or a 403 from JLR will **not** ask you to sign in. If you were asked, the
credentials genuinely were refused.

## "Location is missing, or in the wrong place"

- **It has never appeared.** Location comes from the owner web portal, not the telemetry socket.
  An entry created before the portal was used has no session for it — **Reconfigure** adopts one.
- **It is out of date.** It is where the last *completed* journey ended, not a live position, and
  JLR can take hours to process one. The tracker carries a `timestamp` attribute for the fix.
- **It went to unknown.** Deliberate. When the fix cannot be refreshed the tracker reports nothing
  rather than serving an old one — an old fix still resolves to a zone, so serving it anyway gives
  a confident wrong answer instead of a cautious one. Check the `trusted` attribute in automations
  that act on the car being home.
- **Jaguar owners specifically.** Both brands share one identity, so signing in succeeds either
  way; only the right brand's portal has your car. This was broken for Jaguar-only accounts before
  v1.5.0 — if you are on something older, update.

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

## "Everything is unavailable"

- **For under half an hour.** Give it time. The telemetry socket reconnects every few minutes by
  design and there is a 30-minute grace before anything is marked unavailable.
- **For longer.** The socket is genuinely down. Check **Settings → System → Logs** for
  `telemetry socket dropped`; the reason is on that line. The integration keeps retrying with a
  growing backoff and recovers on its own once JLR do.
- **Location and the refresh button still work.** That is correct — they do not come from the
  socket.

## "A car I sold is still there"

It should disappear on the next 15-minute poll once the account no longer lists it, along with
its cached details. If the device remains, delete it: **the device page → ⋮ → Delete**. That is
refused while the account still lists the car, and also refused if the last account read failed —
a failed listing is not evidence anyone's car is gone.

## "A car I just added is missing"

It appears within 15 minutes without a restart. If it does not, check the logs for
`owner portal listed N vehicle(s)` and attach diagnostics.

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
not told us its fuel type, which is the usual case while JLR keep the attributes endpoint behind
app attestation. Deciding a car is not electric from the absence of a battery reading is a good
enough reason not to create a sensor and nowhere near good enough to delete one, so on those
accounts an unwanted EV entity on a petrol car stays until the fuel type is known.

Removals are logged at INFO, which most installs do not show. To see them, turn on debug logging
for the integration *before* the restart that will do the removing.

## "An entity I want is not there at all"

Three are off by default — 12V battery charge, EVCC status and the charge-now override. Switch
them on in the entity's settings. See [ENTITIES.md](ENTITIES.md) for why each one is off.

## Turning on debug logging

**Settings → Devices & Services → Jaguar Land Rover InControl → Enable debug logging**, reproduce,
then disable it to download the log. VINs and coordinates are scrubbed from these lines, as are
the account's own identifiers and tokens if a server quotes them back.

## Filing a good issue

[Open one here](https://github.com/willbeeching/ha-jlr-incontrol/issues) with the diagnostics
download, the vehicle model and year, and what you expected instead. Model year matters more than
it sounds: JLR's data varies noticeably between generations.
