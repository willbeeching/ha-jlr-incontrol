Thank you for the incredibly thorough test report — this is exactly the kind of feedback that helps move BEV support forward. Seriously appreciated.

These changes are available in **[v1.0.0-beta.2](https://github.com/willbeeching/ha-jlr-incontrol/releases/tag/v1.0.0-beta.2)**.

## What's been addressed

**ICE sensors on BEVs** — fuel level, fuel range, engine coolant temperature, and combined range are now suppressed when `fuelType` is Electric. Sentinel values (-60 °C coolant, -1 combined range) are also filtered to `unknown` as a fallback.

**Climate 403** — BEVs now use ECC electric preconditioning (`preconditioning` endpoint) instead of REON/REOFF. The climate entity supports a target temperature (16–28 °C) and does not require your account PIN for ECC (per native-app behaviour).

**Stale lock state / frozen `last_updated`** — this is a JLR server-side caching limitation: individual values like SoC can update while the aggregate timestamp and lock state only refresh on a full vehicle wake. A new **Update from vehicle** button (VHS) has been added to force the car to push fresh status. The existing **Refresh** button still just re-polls the server cache.

**Distance units** — HA already converts distance sensors automatically via `device_class=DISTANCE` and your system unit settings. An integration options flow has also been added to override distance (miles/km) and pressure (kPa/bar/psi) per-entry if you prefer.

**New features**
- **Charge now** switch (CP) for BEVs
- **Last trip** sensor (when the trips API is available)
- **All info** sensor (disabled by default) with the full status dict as attributes
- Diagnostics download (VIN/position redacted)

## Needs your testing

ECC, VHS, CP, and the trips endpoint are implemented from the native-app API documentation but have **not yet been verified** against the webview backend this integration uses. The webview edge is picky about Accept/Content-Type headers, so these may need adjustment.

Could you try on your I-PACE:
1. Climate on/off with a target temperature
2. **Update from vehicle** button
3. **Charge now** switch
4. Whether the **Last trip** sensor appears

If any return errors (especially 403/406), please paste the HA log line and we'll iterate.

Thanks again — this report was a huge help.
