# Vehicle data model → Home Assistant entities

Reverse-engineered from the vehicle status DTOs and mappers. The IF9
`healthstatus` response is a `coreStatus` list of `{key, value}` pairs (plus a separate `evStatus`
list and a `vehicleAlerts` feed). Key groups below; HA mapping at the end.

## Core status keys (confirmed from mappers)
- **EV / charging**: `EV_STATE_OF_CHARGE`, `EV_CHARGING_STATUS`
  (CHARGING/FULLYCHARGED/PAUSED/NOTCONNECTED/INITIALIZATION/FAULT/BULKCHARGED/WAITINGTOCHARGE),
  `EV_CHARGING_METHOD`, `EV_MINUTES_TO_FULLY_CHARGED`, `EV_CHARGING_RATE_SOC/MILES/KM_PER_HOUR`,
  `EV_RANGE_ON_BATTERY_KM/MILES`, `EV_PHEV_RANGE_COMBINED_KM/MILES`, `EV_PERMANENT_MAX_SOC_VALUE`,
  `EV_PRECONDITION_OPERATING_STATUS`, `EV_PRECONDITION_REMAINING_RUNTIME_MINUTES`.
- **Fuel / range**: `FUEL_LEVEL_PERC`, `DISTANCE_TO_EMPTY_FUEL`.
- **Odometer**: `ODOMETER`, `ODOMETER_METER`, `ODOMETER_MILES`.
- **Climate (ICE)**: `CLIMATE_STATUS_OPERATING_STATUS`, `CLIMATE_STATUS_REMAINING_RUNTIME`,
  `VEHICLE_STATE_TYPE` (ENGINE_ON_REMOTE_START).
- **Doors / windows / roof**: `DOOR_IS_ALL_DOORS_LOCKED`, `DOOR_{FRONT,REAR}_{LEFT,RIGHT}_POSITION`,
  `DOOR_ENGINE_HOOD_POSITION`, `DOOR_BOOT_POSITION`, `WINDOW_{FRONT,REAR}_{LEFT,RIGHT}_STATUS`,
  `IS_SUNROOF_OPEN`, `IS_CAB_OPEN`.
- **Security**: `THEFT_ALARM_STATUS` (ALARM_TRIGGER/ALARM_ARMED/ALARM_OFF).
- **Tyres / fluids (alert feed)**: `TYRE_PRESSURE_{FL,FR,RL,RR}`, `BRAKE_FLUID_STATUS`,
  `BRAKE_PAD_WEAR`, `COOLANT_LEVEL`, `WASHER_FLUID_LEVEL`, `OIL_LEVEL`, `ENGINE_MALFUNCTION`.
- **Service / DEF**: `EXT_KILOMETERS_TO_SERVICE`, `EXT_EXHAUST_FLUID_DISTANCE_TO_SERVICE_KM`,
  `EXT_PARTICULATE_FILTER_WARN`.
- **Vehicle state** (`VehicleState$State`): KEY_REMOVED, KEY_ON_ENGINE_OFF, KEY_ON_ENGINE_ON,
  ENGINE_ON_PARK, ENGINE_ON_MOVING, ENGINE_ON_REMOTE_START.

## Attributes / capabilities (`VehicleAttributes`)
Identity: `vin`, `registrationNumber`, `market`, `modelYear`, `brand`, `model`, `nickname`,
`numberOfDoors`, `telematicsDevice.serialNumber`. Powertrain: `FuelType` (PETROL/DIESEL/EV/PHEV),
`PowerTrainType` (BEV/PHEV/INTERNAL_COMBUSTION). Feature-gating combines three sources:
`services` (availableServices, matched to `ServiceName` codes), `vehicleCapability` (fitted-hardware
option codes), and `computedValues` (pre-computed booleans, e.g. `remoteDoorPermitted`,
`selectableClimateCapable`, `iceDepartureAvailable`, `isWebsocketV2`).

## Position
`VehiclePositionResponse.position` = `{ longitude, latitude, timestamp }` only — **no heading or
speed**. Reverse-geocoded into street/city/postcode/country for display.

## HA entity mapping
| Platform | Entities |
|---|---|
| `sensor` | battery charge %, fuel level %, fuel range, electric range, odometer, charging status, time-to-full, charge rate, distance-to-service, AdBlue range, tyre pressures, climate remaining runtime, last-updated; diagnostic: VIN, reg, model year, fuel type |
| `binary_sensor` | all-doors-locked, per-door/bonnet/boot open, per-window open, roof/sunroof, alarm triggered, charge cable connected, is-being-driven, low tyre/oil/brake/washer/coolant, service due, DEF low |
| `device_tracker` | lat/lon + reverse-geocoded address attributes |
| `lock` | doors (RDL/RDU) — model a transitional state (commands are async) |
| `climate` | preconditioning: ECC (BEV/PHEV) or REON/REOFF (ICE), target temperature |
| `switch`/`button` | charging on/off (CP), cabin air (CAC), honk & flash (HBLF, button), silence alarm (ALOFF, button), Guardian Mode (GM) |
| `number` | charge target SoC, climate target temperature |

Feature-gate every entity on the vehicle's `services` + `vehicleCapability` + `computedValues`
rather than assuming all exist for every VIN.
