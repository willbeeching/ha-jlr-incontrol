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

This integration is read-only and mostly push-driven: vehicle data arrives over a telemetry
socket rather than being asked for. **Refresh** is the one button, and it re-reads what JLR
already hold rather than waking the car — there is no longer any way to wake a vehicle from
Home Assistant, because that took a remote command and JLR now gate those behind the app's
device attestation.

That does not make polling free. Every refresh is a request to JLR's servers, reached through
an unofficial API that they can withdraw. Prefer event-driven refreshes (a state change,
arriving home, opening a dashboard) over a timer, and count down locally rather than re-asking.

## Preconditioning countdown

**Problem:** `sensor.<car>_preconditioning_time_remaining` is a snapshot, not a countdown. The
car sets it once when preconditioning starts and only refreshes it on a VHS, so it sits still
instead of ticking down.

**Approach:** seed a local template sensor from the integration's value when preconditioning
starts, then decrement it every minute in Home Assistant. Only fall back to a single VHS call if
the initial value comes back as 0.

### Local preconditioning countdown

_Contributed by [@ismarslomic](https://github.com/ismarslomic). Tested on a Jaguar I-PACE 2021._

The JLR integration reports the remaining preconditioning time from the vehicle, but this value does not provide a
continuously updating countdown. This solution stores the expected end time and calculates the remaining time locally in
Home Assistant.

- **Step 1: Store the expected end time**: Creates a date-and-time helper that stores when preconditioning is expected
  to finish.
- **Step 2: Keep the end time synchronized**: Creates an automation that runs when preconditioning starts or stops. When
  it starts, the automation refreshes the vehicle data, retrieves the reported remaining time and calculates the
  expected end time. It uses 29 minutes as a fallback if no valid value is received. When preconditioning stops,
  the stored end time is reset to the current time.
- **Step 3: Calculate the remaining time locally**: Creates a template sensor that calculates the remaining whole
  minutes from the stored end time. It updates every minute and whenever a referenced entity changes, returns `0`
  when preconditioning is off, and never reports a negative value.

The result is a reliable, continuously updating countdown without repeatedly requesting new data from the vehicle. This
provides smoother dashboard updates while reducing unnecessary calls to the JLR service.

#### Step 1: Store the expected end time

1. In Home Assistant, go to **Settings → Devices & services → Helpers**.
2. Select **Create helper → Date and/or time**.
3. Configure the helper with:
    - **Name:** `Jaguar Preconditioning End Time`
    - **Icon:** `mdi:timer-sand`
    - **Input:** `Date and time`
    - **Area:** Optional
4. Select **Create**.
5. You have created a new entity with the name `input_datetime.jaguar_preconditioning_end_time`

The date and time selected in this helper represent when the vehicle should finish preconditioning.

#### Step 2: Keep the end time synchronized

This automation updates the local preconditioning end time whenever preconditioning starts or stops. When
preconditioning starts, the automation waits for the car to report a remaining-time value, then calculates and stores
the expected end time, using 29 minutes as a fallback. When preconditioning stops, the stored end time is reset to the
current time.

The `input_datetime.jaguar_preconditioning_end_time` entity comes from the helper created in Step 1. The other two
entities are provided by the [`ha-jlr-incontrol`](https://github.com/willbeeching/ha-jlr-incontrol) integration:

- `binary_sensor.jaguar_preconditioning`
- `sensor.jaguar_preconditioning_time_remaining`

Replace these entity IDs if they differ in your installation.

```yaml
alias: Jaguar - update local end time for preconditioning
description: Updates the estimated end time when preconditioning starts or stops
triggers:
  - trigger: state
    entity_id: binary_sensor.jaguar_preconditioning
    to: "on"
    id: started
  - trigger: state
    entity_id: binary_sensor.jaguar_preconditioning
    from: "on"
    to: "off"
    id: stopped
actions:
  - choose:
      - conditions:
          - condition: trigger
            id: started
        sequence:
          - delay:
              seconds: 10
          - variables:
              previous_update: |
                {% set entity_id =
                  'sensor.jaguar_preconditioning_time_remaining'
                %}
                {% if has_value(entity_id) %}
                  {{ as_timestamp(
                    states[entity_id].last_updated,
                    default=0
                  ) }}
                {% else %}
                  0
                {% endif %}
          - wait_template: |
              {% set entity_id =
                'sensor.jaguar_preconditioning_time_remaining'
              %}
              {% if has_value(entity_id) %}
                {% set sensor = states[entity_id] %}
                {{
                  as_timestamp(sensor.last_updated, default=0)
                    > (previous_update | float(0))
                  and sensor.state | int(0) > 0
                }}
              {% else %}
                false
              {% endif %}
            timeout:
              minutes: 2
            continue_on_timeout: true
          - variables:
              reported_minutes: >
                {% set value = states(
                  'sensor.jaguar_preconditioning_time_remaining'
                ) | int(0) %}
                {{ value if wait.completed and value > 0 else 29 }}
          - action: input_datetime.set_datetime
            target:
              entity_id: input_datetime.jaguar_preconditioning_end_time
            data:
              timestamp: |
                {{ now().timestamp() + reported_minutes * 60 }}
      - conditions:
          - condition: trigger
            id: stopped
        sequence:
          - action: input_datetime.set_datetime
            target:
              entity_id: input_datetime.jaguar_preconditioning_end_time
            data:
              timestamp: "{{ now().timestamp() }}"
mode: restart
```

#### Step 3: Calculate the remaining time locally

Create a template sensor that calculates the remaining preconditioning time from the end-time helper created in step 1
and kept up to date by the automation in step 2. While preconditioning is active it calculates the remaining whole
minutes, rounding up and never returning a negative value. The sensor updates every minute and whenever a referenced
entity changes. It returns `0` when preconditioning is off, and becomes unavailable if the required entities do not
contain valid states.

1. Go to **Settings → Devices & services → Helpers**.
2. Select **Create helper → Template → Sensor**.
3. Configure the sensor with:

    - **Name:** `Jaguar preconditioning time remaining (local)`
    - **Unit of measurement:** `min`
    - **Device class:** `Duration`
    - **State class:** None
    - **Device:** Your Jaguar vehicle (optional)

4. Enter the following **State template**:

```jinja
{% if is_state('binary_sensor.jaguar_preconditioning', 'off') %}
  0
{% else %}
  {% set end_time = as_timestamp(
      states('input_datetime.jaguar_preconditioning_end_time')
  ) %}
  {% set remaining = ((end_time - now().timestamp()) / 60)
      | round(0, 'ceil') | int %}
  {{ [remaining, 0] | max }}
{% endif %}
```

5. Under **Additional options**, enter the following **Availability template**:

```jinja
{{ states('binary_sensor.jaguar_preconditioning') in ['on', 'off']
  and
  as_timestamp(
    states('input_datetime.jaguar_preconditioning_end_time'),
    none
  ) is not none
}}
```

6. Select **Create**.

The sensor is available only when the integration reports a valid preconditioning state and the end-time helper contains
a valid date and time. You can now use this sensor to display a countdown while preconditioning is active.

## Stale lock & alarm state

**Problem:** `DOOR_IS_ALL_DOORS_LOCKED` and `THEFT_ALARM_STATUS` only refresh in JLR's cache when
the car next wakes on its own, so after locking with the key fob they can read stale for hours.
**Refresh** re-reads that same cache and will not change the answer.

**There is no fix from here.** Waking the car took the VHS remote command, and JLR now require
the app's device attestation on that path, so it is not available to this integration at any
price. The official app can still do it.

**What you can do:** treat both entities as "last known", not "now". If an automation must not
act on a stale lock state, gate it on the vehicle's **Last updated** sensor — a value older than
an hour or two is telling you the car has not reported since, not that nothing has changed.

_Recipe wanted._ See [issue #5](https://github.com/willbeeching/ha-jlr-incontrol/issues/5) for
the background.

## EVCC / surplus charging

The **EVCC status** sensor reports IEC 61851 connector state (`A` disconnected, `B` connected but
not charging, `C` charging) so wallbox controllers can consume it directly. The **Plugged in** and
**Preconditioning** binary sensors are also designed to be automated against — the latter is
useful for holding the wallbox open while the car is preconditioning on shore power.

_Recipe wanted._ If you have a working EVCC configuration, it'd be a great addition here.
