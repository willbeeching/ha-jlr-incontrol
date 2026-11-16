# JLR Data Hub

JLR publish an official API for owners, the **JLR Data Hub**, under the EU Data Act. It is
documented at [developer.jlr.com](https://developer.jlr.com/docs/getting-started). Despite the EU
origin it is not limited to the UK and EU: an owner in Canada has it working with location, the
car declared as outside the UK and the EU, and an owner in Australia has it working too. If
data.jlr.com lists your cars, it should work wherever you are.

Since 3.0.0 it is the only way this integration reads a car: doors, locks, windows, tyres, fuel,
mileage, warnings, the 12 V battery, battery and charging, location and journeys. The entities are
the ones the InControl connection gave, with the same ids and history.

- **The cars and their details** (brand, model, year, fuel type) come from the Data Hub: the cars
  are the ones selected on the client at data.jlr.com.
- **Location** comes from the Data Hub once each car is **declared** at data.jlr.com: say whether
  you are its sole user or a shared user (and, if shared, that the other drivers agree). JLR refuse
  a car's location until that is done; confirmed on two cars in November 2026, which were refused
  before the declaration and permitted straight after it.
- **If the link stops working** (the client deleted, its certificate replaced, consent
  withdrawn), the integration asks to be reconfigured under **Settings → Devices & Services**.
  That runs the linking steps again on the same entry; its entities and history are kept.
- **Select every car** on the client at data.jlr.com. A car left off it is not on the integration.

**Coming from the InControl sign-in?** 3.0.0 removed it. On upgrade, everything that sign-in kept
in the entry, the password among it, is deleted. An entry that also had the Data Hub linked carries
on as before. One that had only the InControl sign-in keeps its cars, entities and history, reads
nothing, and shows a repair until you link it: **Configure → Link the JLR Data Hub**, then the
steps below. Every entity comes back as it was. A car that had its location from the owner portal
needs declaring at data.jlr.com to keep it.

Each car also has two entities about the link, and diagnostics a `datahub` section:

- **Last report (Data Hub)**: the time the car itself sent its latest reading. Nothing else in
  JLR's data has ever carried that.
- **Data Hub stream**: whether the live stream is connected.

How the Data Hub's signals map onto the status keys the entities read, the names the InControl
connection used, is in [`vss.py`](../custom_components/jlr_incontrol/vss.py). Side by side on
two cars, every value matched exactly, so the mapping is a renaming.

## Linking it

You need a JLR account whose cars appear at [data.jlr.com](https://data.jlr.com), and a few
minutes with your phone to hand.

1. In Home Assistant, open **Settings → Devices & Services → Add Integration → Jaguar Land Rover
   InControl** (or, on an entry from the InControl sign-in, **Configure → Link the JLR Data
   Hub**). It shows a certificate. Leave the page open.
2. At [data.jlr.com](https://data.jlr.com), sign in and create a client:
   - credential type **device-based**
   - select your vehicles
   - select **all** data categories
   - for location, complete each car's **declaration** in your vehicle list: sole user or shared
     user. Without it JLR refuse that car's location.
   - when it asks for a public certificate, paste the whole block Home Assistant showed, from
     `-----BEGIN CERTIFICATE-----` to `-----END CERTIFICATE-----`.
3. On the client's page, turn **Toggle your stream** on.
4. Copy the client ID it shows (it looks like `dsas-0123456789ab`) into Home Assistant and
   submit.
5. Home Assistant shows a link and a code. Open the link on your phone, sign in with your JLR
   account and approve it. The page in Home Assistant moves on by itself, and the integration
   starts with the link in place.

The certificate is public. The private key that matches it was made inside Home Assistant, is
stored only in the integration's config entry, and is never shown or sent anywhere. It signs
every request to JLR, which is why the link cannot be used by anyone who only has a token.

To remove the link, delete the integration in Home Assistant, then delete the client at
data.jlr.com.

## How it talks to JLR

- **Sign-in**: once, with the device code you approve on your phone. After that a refresh token
  keeps the link alive without you. JLR's refresh token for this has no expiry of its own, but the
  certificate does: it is made valid for a year. From 30 days before it runs out, the options offer
  **Link the JLR Data Hub again**, which makes a new one; the same entry is offered it if the link
  stops working. A link that is working is not offered it, since linking again only replaces a
  good key.
- **Status**: one REST read per car when the link starts, then every 15 minutes until the stream
  has delivered a reading newer than the car's last report, and hourly after that. A message that
  only repeats what REST already brought does not count. Fifteen minutes is the cadence the
  integration's housekeeping already keeps; it is there because the REST read is the only source
  of change until the stream is shown to work. The **Refresh** button reads status and location on
  demand, at most once a minute.
- **Location**: read with each status read for every car JLR allow it for, so every 15 minutes
  until the stream proves itself and hourly after. A car JLR refuse (not declared at data.jlr.com)
  is asked once an hour instead, so a declaration made later is picked up within the hour without
  asking every 15 minutes in the meantime. The answer per car is in diagnostics under `location`.
  The stream also subscribes to each car's `position` topic alongside `status`; if the broker will
  not allow it, the link falls back to status alone.
- **Battery and charging**: from their own endpoint (`/ev`), read with each status read for a car
  that has a charge port. The status snapshot carries none of it, so without this a plug-in car had
  no battery readings until the stream delivered. A car without a charge port is refused there,
  and is asked again once a day rather than every read. The answer per car is in diagnostics under
  `ev`.
- **Journeys**: for the **Last journey** sensors. Read when the link starts, five minutes after the
  stream sends the position that ends a journey, and every six hours in case the stream missed
  one. The stream resends its last position each time it reconnects, with the same time on it, so
  that is recognised and not read again; the first one heard after a restart costs one read. If
  the read after a journey finds nothing newer, JLR have most likely not filed it yet, and it is
  asked for once more half an hour later rather than left to the six-hour read. JLR
  list journeys oldest first, twenty to a page, so this is one request, or two once a car
  has more than twenty. A car JLR refuse journeys for gains no journey sensors and is asked again
  once a day. Each journey JLR send includes where it started and ended; the integration does not
  keep either. The answer per car is in diagnostics under `journeys`.
- **Stream**: one MQTT connection for the account, held open, which brings status as the car
  reports it, typically within about 16 seconds, and the car's position when a journey ends.
  JLR send no position while the car is being driven, matching their own app, which hides it. JLR end each session at an hour;
  the integration reconnects at 55 minutes on its own schedule. Between attempts it waits at
  least 30 seconds, and backs off to 15 minutes when connections keep failing. If the broker
  turns every way of logging in down, it waits six hours before asking again.

## What JLR's docs get wrong

Measured on a UK account in November 2026. The integration follows what was measured.

| | The docs say | What happens |
| --- | --- | --- |
| Access token lifetime | 1 hour | 5 minutes |
| Refresh token | 14 days | An "Offline" token with no expiry |
| `Vehicle.TraveledDistance` | km | metres |
| Rate limits | Headers on every response | No rate-limit headers on any response |
| Stream broker | `stream-data.jlr-vcdp.com` | The portal shows `stream-data.b2b-prod.jlr-vcdp.com`, which does not resolve. The docs' host is used, and the portal's tried if it fails. |
| Stream username | from the portal | The portal shows it in capitals, but shows that whole panel in capitals. The client ID as issued is tried first. |

Also measured:

- Tyre pressures are in hPa (2278 is about 33 psi).
- REST status has one `timestamp` for the whole snapshot, not one per signal. JLR's streaming
  guide shows stream messages carrying one signal each, and flags that schema itself as
  unconfirmed. The first drive with the stream connected brought 13 messages and no reading newer
  than REST already had, so it reads both the documented shape and the REST snapshot's, and keeps the
  key names (never the values) of anything else in diagnostics as `unread_shape`. That is how the
  real shape was found: on a drive in Australia the broker sent lists of signals keyed
  `dataType`, `description`, `signalName`, `signalTimestamp`, `signalValue`, `unit` and
  `vssIdentifier`, which the link now reads, taking the VSS name from `vssIdentifier` (or
  `signalName`) and reading numbers sent as text as numbers. `signalTimestamp` is epoch
  milliseconds as text (`"1794167909000"`, measured on an I-Pace), not the ISO 8601 the REST
  answers use; until 2.0.4 read it, every streamed reading was set aside for want of a time.
- The status values are the same as the existing integration's (the same frozen coolant
  temperature, the same `KEY_ON_ENGINE_OFF`), so everything in [DATA_MODEL.md](DATA_MODEL.md)
  still applies.
- **Position** is refused per car ("Access to category is not permitted for this vehicle") until
  the car is **declared** at data.jlr.com: region, whether you are the sole user or a shared user,
  and whether other drivers have agreed. Location is personal data about whoever is driving, and
  the docs do not say this. Confirmed on two cars: both refused, both declared as sole user, both
  permitted at the next read. Look for each car's status and class in your vehicle list at
  data.jlr.com.
- **Electric cars** have been checked on a plug-in hybrid Range Rover Sport and an I-Pace. The
  EV endpoint gives the true charge, a
  plugged-in flag, a charging flag and a mode, but no charging status, displayed charge or charging
  method, which the entities and the charge-port check read. Those are filled in from what it does
  give, word for word where the meaning is the same; plugged in and not charging reads **Plugged
  in, not charging**, because the endpoint does not say whether that is paused, waiting or full.
  JLR's own documents disagree on two units: the signal catalog gives time to full charge in
  minutes and charging rate in km of range per hour, the API spec gives seconds and kW. The
  catalog is right: on an I-Pace charging at home, 114 minutes and 22 km/h matched the JLR
  app's 1 hr 54 min and 14 mi/hr.
- **Journeys** were refused for owner accounts altogether until November 2026, even with the
  category selected and the car declared. Since then two owners' accounts answer, without
  changing anything at data.jlr.com, with journeys from 30 October 2026 on, nothing earlier. Each
  has its start and end time, duration in seconds, distance and average speed with their units, and
  its start and end coordinates. JLR's spec lists one more, `energyUsed` in kWh or litres, which
  the first answers did not include; nothing about fuel economy, EV distance or regeneration is
  in the spec at all. Diagnostics list the newest journey's field names, never their values,
  under each car's `journey_shape`, so a car that gets more fields shows it. On a Range Rover
  the journey was in the list at the first read,
  five minutes after the stream's end-of-journey fix, so 5 minutes 20 seconds after the car
  stopped. One owner's two most recent drives were missing from the list at first, so it may not
  be that quick on every account.
- The stream's sign-in is refused (`Client not enabled to retrieve service account`) until the
  stream is switched on for the client in the portal.
- With it switched on, the broker the portal names does not resolve, and the broker in the docs
  refuses every login except one: the client ID as issued, used as both the MQTT username and the
  MQTT client ID. With that login it then hung up on the docs' own subscription,
  `account/{accountId}/vehicles/{vin}/#`. The link keeps the accepted login and tries the other
  topic layouts the docs show, once each, starting with the exact `status` and `position` topics,
  which the broker accepts. That name is not a portal typo:
  the portal reads it from JLR's own `/account/credential/stream` API. The portal also shows every
  stream detail in capitals through CSS, so the capitals are not the username's real case. The
  stream connects and subscribes on owner accounts, and delivers while the car is driven. On an
  I-Pace with 2.0.4, six streamed readings moved the car's report forward during a five-minute
  drive, and its parked position arrived 34 seconds after the journey ended. That was the only
  fix of the drive: the stream sends position at the end of a journey, not during it. On two cars the
  stream's message came about 16 seconds after the car's own report time. Status is read every 15
  minutes until a streamed reading is newer than what REST has, and hourly after.
- The docs' Vehicle Data and Diagnostics pages list endpoints the published API spec does not:
  `/v1/vehicles/{vin}/alerts`, `/ev/hv-battery` and `/diagnostics`, and a `Capabilities` list on
  vehicle details that includes `live-data-streaming`. Neither car tested returned `Capabilities`,
  and the extra endpoints are untested.

## If it is not working

- **Last report (Data Hub) stays unknown**: the car has not been allowed status, or the link has
  not read it yet. Download diagnostics and look at `datahub`.
- **Settings → Repairs** says, in plain words and with what to do at data.jlr.com, each of the
  things only the owner can put right: the stream is switched off,
  a car's location is refused because it is not declared, a car's readings are refused because it or its data
  categories are not selected on the client, and the certificate has under 30 days left. Each one
  goes away by itself once it is put right.
- **Data Hub stream stays off**: Home Assistant says so under **Settings → Repairs**. Check
  **Toggle your stream** is on at data.jlr.com. With it off the link reads status every 15 minutes
  and asks about the stream again each hour; `stream_enabled: false` in diagnostics
  means exactly this. If it is on, diagnostics show `last_rejection`: which way of logging in was
  tried last and where the broker hung up (before the login was accepted, or before the
  subscription was confirmed). Until one works the link tries five, once each: the username as
  issued and in capitals, each with its own client id and as the client id, then port 443. Once a
  login is accepted it keeps it, and tries the topic layouts once each: status and position per car,
  status alone per car, every category per car, each car with no category, the whole account, the
  account id in capitals, and the first car alone. `accepted_login` and `topics` in diagnostics show where it got to. If a
  round ends with nothing working it leaves the stream for six hours before trying again.
- **The integration asks to be reconfigured**: JLR stopped accepting the link, because the
  client was deleted, its certificate replaced, or consent withdrawn. Choose **Reconfigure** on
  it under **Settings → Devices & Services** and follow the same steps as the first link; a new
  certificate is made each time. **Configure → Link the JLR Data Hub again** does the same.
  Linking a client from a different JLR account is refused, since its cars are not this entry's.
- **A repair says to link the Data Hub**: the entry was set up on the InControl sign-in, which
  3.0.0 removed. It reads nothing until it is linked; **Configure → Link the JLR Data Hub**.
- **The stream is connected but nothing arrives from it**: diagnostics count, under `stats`,
  every `messages` the broker sent, the `fresh` ones that moved a car's last report forward, and
  the `unread` ones with no reading the link could place, with `last_message` (when one last
  arrived) and `unread_shape` (the key names of the last unread one). A count of `unread` with
  its shape in an issue is what is needed to read them. `untimed` counts readings whose time could
  not be read, which never count as news, and `untimed_format` shows the last such time with every
  digit turned to 9, so its format can be shared without the time itself. Each car's `fix_time` is
  when its latest streamed location was taken; it sits on its own key because the signal names it
  would otherwise appear under are redacted with the coordinates.
- Turn on debug logging (see [TROUBLESHOOTING.md](TROUBLESHOOTING.md)) to see every signal the
  stream delivers, with the time the car sent it. Car identities are replaced with a stable
  label in those lines.
