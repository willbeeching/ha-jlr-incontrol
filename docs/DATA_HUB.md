# JLR Data Hub (beta)

JLR now publish an official API for owners, the **JLR Data Hub**, under the EU Data Act. It is
documented at [developer.jlr.com](https://developer.jlr.com/docs/getting-started), and it is
available in the UK as well as the EU.

From 1.9.0 the integration can be set up on it from **Add integration**, with no InControl email or
password, and an existing InControl entry can be linked to it from the options. Either way,
**vehicle status comes from the Data Hub** instead of the InControl telemetry socket: doors, locks, windows, tyres, fuel, mileage,
warnings, the 12 V battery and the rest. The entities are the same ones, with the same ids and
history; only where their values come from changes. Linking is optional, and an install that does
not link carries on exactly as before.

- **The cars and their details** (brand, model, year, fuel type) come from the Data Hub too: the
  cars are the ones selected on the client at data.jlr.com. The older vehicle list is not used.
- **Location** comes from the Data Hub too, once each car is **declared** at data.jlr.com: say
  whether you are its sole user or a shared user (and, if shared, that the other drivers agree).
  JLR refuse a car's location until that is done; confirmed on two cars in November 2026, which
  were refused before the declaration and permitted straight after it. A car that is not declared
  can take its location from JLR's owner portal instead, which needs an InControl sign-in: on an
  entry set up on the Data Hub, choose **InControl sign-in for location** in the options. Only the
  portal's session is kept, not your password, and it is used for location and car names only.
- **Moving off InControl.** An entry that started on the InControl sign-in and has the Data Hub
  linked can drop the sign-in: **Configure → Remove the InControl sign-in**. Every entity and its
  history stays. What goes is the fallback below, and portal location for a car not declared at
  data.jlr.com. It is refused while the link is not working, since the entry would then read
  nothing.
- **If the link stops working** (the client deleted, its certificate replaced, consent
  withdrawn), a repair says so. An entry that also has an InControl sign-in falls back to the
  telemetry socket by itself, so the sensors keep working while you link it again; one set up on
  the Data Hub alone keeps its last readings until you link it again from the options.
- **Select every car** on the client at data.jlr.com. A car left off it is not on the integration.

Linking also adds two entities to each car, and a `datahub` section in diagnostics:

- **Last report (Data Hub)**: the time the car itself sent its latest reading. Nothing else in
  JLR's data has ever carried that.
- **Data Hub stream**: whether the live stream is connected.

How the Data Hub's signals map onto the socket's keys is in
[`vss.py`](../custom_components/jlr_incontrol/vss.py). Side by side on two cars, every value
matched exactly, so the mapping is a renaming. The electric signals are the exception: they come
from JLR's catalog and have not yet been checked against an electric car.

## Linking it

You need a JLR account whose cars appear at [data.jlr.com](https://data.jlr.com), and a few
minutes with your phone to hand.

1. In Home Assistant, open **Settings → Devices & Services → Jaguar Land Rover InControl →
   Configure → Link the JLR Data Hub**. It shows a certificate. Leave the page open.
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
   reloads with the link in place.

The certificate is public. The private key that matches it was made inside Home Assistant, is
stored only in the integration's config entry, and is never shown or sent anywhere. It signs
every request to JLR, which is why the link cannot be used by anyone who only has a token.

To remove the link, choose **Unlink the JLR Data Hub** in the same options, then delete the
client at data.jlr.com.

## How it talks to JLR

- **Sign-in**: once, with the device code you approve on your phone. After that a refresh token
  keeps the link alive without you. JLR's refresh token for this has no expiry of its own, but the
  certificate does: it is made valid for a year. From 30 days before it runs out, the options offer
  **Link the JLR Data Hub again**, which makes a new one; the same entry is offered it if the link
  stops working. A link that is working is not offered it, since linking again only replaces a
  good key.
- **Status**: one REST read per car when the link starts, then every 15 minutes until the stream
  has delivered a message, and hourly after that. Fifteen minutes is the cadence the integration's
  housekeeping already keeps; it is there because the REST read is the only source of change
  until the stream is shown to work. The **Refresh** button reads status on demand, at most once a
  minute.
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
- **Stream**: one MQTT connection for the account, held open. JLR end each session at an hour;
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
- REST status has one `timestamp` for the whole snapshot, not one per signal. Stream messages
  carry one each.
- The status values are the same as the existing integration's (the same frozen coolant
  temperature, the same `KEY_ON_ENGINE_OFF`), so everything in [DATA_MODEL.md](DATA_MODEL.md)
  still applies.
- **Position** is refused per car ("Access to category is not permitted for this vehicle") until
  the car is **declared** at data.jlr.com: region, whether you are the sole user or a shared user,
  and whether other drivers have agreed. Location is personal data about whoever is driving, and
  the docs do not say this. Confirmed on two cars: both refused, both declared as sole user, both
  permitted at the next read. Look for each car's status and class in your vehicle list at
  data.jlr.com.
- **Electric cars, not yet checked against one.** The EV endpoint gives the true charge, a
  plugged-in flag, a charging flag and a mode, but no charging status, displayed charge or charging
  method, which the entities and the charge-port check read. Those are filled in from what it does
  give, word for word where the meaning is the same; plugged in and not charging reads **Plugged
  in, not charging**, because the endpoint does not say whether that is paused, waiting or full.
  JLR's own documents disagree on two units: the signal catalog gives time to full charge in
  minutes and charging rate in km of range per hour, the API spec gives seconds and kW. The
  catalog's are used. If either reads wrong on your car, please open an issue.
- **Journeys** are refused for owner accounts altogether, even with the category selected and the
  car declared.
- The stream's sign-in is refused (`Client not enabled to retrieve service account`) until the
  stream is switched on for the client in the portal.
- With it switched on, the broker the portal names does not resolve, and the broker in the docs
  refuses every login except one: the client ID as issued, used as both the MQTT username and the
  MQTT client ID. With that login it then hung up on the docs' own subscription,
  `account/{accountId}/vehicles/{vin}/#`. From 1.9.0-beta.5 the link keeps the accepted login and
  tries the other topic layouts the docs show, once each. That name is not a portal typo:
  the portal reads it from JLR's own `/account/credential/stream` API. The portal also shows every
  stream detail in capitals through CSS, so the capitals are not the username's real case. As of
  1.9.0-beta.3 the stream has not been seen to connect for an owner account; the link reads status
  hourly meanwhile.
- The docs' Vehicle Data and Diagnostics pages list endpoints the published API spec does not:
  `/v1/vehicles/{vin}/alerts`, `/ev/hv-battery` and `/diagnostics`, and a `Capabilities` list on
  vehicle details that includes `live-data-streaming`. Neither car tested returned `Capabilities`,
  and the extra endpoints are untested.

## If it is not working

- **Last report (Data Hub) stays unknown**: the car has not been allowed status, or the link has
  not read it yet. Download diagnostics and look at `datahub`.
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
- **A repair says the link stopped working**: the client was deleted, its certificate replaced,
  or consent withdrawn. Link it again from the options; a new certificate is made each time.
- Turn on debug logging (see [TROUBLESHOOTING.md](TROUBLESHOOTING.md)) to see every signal the
  stream delivers, with the time the car sent it. Car identities are replaced with a stable
  label in those lines.
