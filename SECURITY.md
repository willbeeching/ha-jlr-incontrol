# Security policy

## Reporting a vulnerability

Please report anything security-relevant privately, using GitHub's
[report a vulnerability](https://github.com/willbeeching/ha-jlr-incontrol/security/advisories/new)
form, rather than opening a public issue.

This is a spare-time project rather than a product, so please do not expect a
same-day reply — but reports will be read and acted on.

## What is worth reporting

The integration holds live credentials for someone's car account: an OAuth
refresh token, and the browser session cookies that open the Jaguar Land Rover
owner portal. Anything that could expose those, or the vehicle data behind
them, is worth reporting. So is anything that leaks an identifier: a VIN
identifies a car and, through it, a person, and the telematics IMEI is a
permanent hardware identifier.

Particularly welcome:

- Credentials, cookies or identifiers appearing in logs, diagnostics downloads
  or issue templates. Diagnostics and debug logs are routinely pasted into
  public issues, so anything reaching them is effectively published.
- A way to make the integration send credentials somewhere other than Jaguar
  Land Rover.
- Anything that would let one Home Assistant user's config entry read another's
  data.

## What is out of scope

- That the integration talks to Jaguar Land Rover's servers using a session
  established by the owner signing in. That is what it is for, and it is
  documented in the README.
- Vulnerabilities in Home Assistant itself — please report those to
  [Home Assistant](https://www.home-assistant.io/security/).
- Anything requiring an attacker who already has access to the Home Assistant
  configuration directory. At that point they have the credentials regardless.

## Handling of secrets

Credentials live only in the Home Assistant config entry. They are never
written to logs, and the diagnostics download is scrubbed as a whole — keys
matched by normalised suffix and anything VIN-shaped removed from free text —
rather than by a list of field names, because the list approach shipped a
telematics IMEI in clear for months by naming `imei` when the payload said
`TU_STATUS_IMEI`.
