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

### Local preconditioning countdown

The JLR integration reports the remaining preconditioning time from the vehicle, but this value does not provide a
continuously updating countdown. This solution stores the expected end time and calculates the remaining time locally in
Home Assistant.

- **Step 1: Store the expected end time**: Creates a date-and-time helper that stores when preconditioning is expected
  to finish.
- **Step 2: Keep the end time synchronized**: Creates an automation that runs when preconditioning starts or stops. When
  it starts, the automation refreshes the vehicle data, retrieves the reported remaining time and calculates the
- expected end time. It uses 29 minutes as a fallback if no valid value is received. When preconditioning stops,
- the stored end time is reset to the current time.
- **Step 3: Calculate the remaining time locally**: Creates a template sensor that calculates the remaining whole
- minutes from the stored end time. It updates every minute and whenever a referenced entity changes, returns `0`
- when preconditioning is off, and never reports a negative value.

The result is a reliable, continuously updating countdown without repeatedly requesting new data from the vehicle. This
provides smoother dashboard updates while reducing unnecessary calls to the JLR service.

### Step 1: Store the expected end time

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

### Step 2: Keep the end time synchronized

This automation updates the local preconditioning end time whenever preconditioning starts or stops. When
preconditioning starts, the automation refreshes the vehicle data and waits for an updated remaining-time value. It
calculates and stores the expected end time, using 29 minutes as a fallback. When preconditioning stops, the stored end
time is reset to the current time.

The `input_datetime.jaguar_preconditioning_end_time` entity comes from the helper created in Step 1. The other three
entities are provided by the [`ha-jlr-incontrol`](https://github.com/willbeeching/ha-jlr-incontrol) integration:

- `binary_sensor.jaguar_preconditioning`
- `sensor.jaguar_preconditioning_time_remaining`
- `button.jaguar_update_from_vehicle`

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
          - action: button.press
            target:
              entity_id: button.jaguar_update_from_vehicle
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

### Step 3: Calculate the remaining time locally

Create a template sensor that calculates the remaining preconditioning time from the end-time helper created in step 2.
It returns `0` when preconditioning is off and counts down toward the end time while it is running.

Step 3 creates a local countdown sensor. While preconditioning is active, it calculates the remaining whole minutes from
the end time stored in step 1, rounding up and never returning a negative value. The sensor updates every minute and
whenever a referenced entity changes. It returns `0` when preconditioning is off and becomes unavailable if the required
entities do not contain valid states.

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
a valid date and time. You can now use this sensor to display an count down while precondition is activated.

## Fresh lock & alarm state on arrival

**Problem:** `DOOR_IS_ALL_DOORS_LOCKED` and `THEFT_ALARM_STATUS` only refresh in JLR's cache when
the car next wakes, so after locking with the key fob they can read stale for hours. **Refresh**
re-reads the same stale cache; only **Update from vehicle** (VHS) wakes the car.

**Approach:** trigger the **Update from vehicle** button from an event rather than a timer — for
example when you arrive home (a `zone` trigger), or when you open the dashboard you check the car
on. You get an accurate state exactly when you'd look at it, without waking the car all day.

_Recipe wanted._ See [issue #5](https://github.com/willbeeching/ha-jlr-incontrol/issues/5) for
the background.

## EVCC / surplus charging

The **EVCC status** sensor reports IEC 61851 connector state (`A` disconnected, `B` connected but
not charging, `C` charging) so wallbox controllers can consume it directly. The **Plugged in** and
**Preconditioning** binary sensors are also designed to be automated against — the latter is
useful for holding the wallbox open while the car is preconditioning on shore power.

_Recipe wanted._ If you have a working EVCC configuration, it'd be a great addition here.
