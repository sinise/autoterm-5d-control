# Grafana dashboard

`autoterm-5d-dashboard.json` -- a dashboard for the Autoterm 5D Heater
metrics, built against the specific metric names Home Assistant's built-in
Prometheus integration produces for these entities (confirmed against a
real instance via Grafana Explore, not guessed).

## Requires

- Home Assistant's [Prometheus integration](https://www.home-assistant.io/integrations/prometheus/)
  enabled, scraped into VictoriaMetrics (e.g. via vmagent or Prometheus
  remote_write).
- The [Autoterm 5D](../autoterm-addon/) or
  [Autoterm 5D Debug](../autoterm-debug-addon/) add-on installed and its
  entities present in Home Assistant. Several panels (Defined/Measured
  revolutions, Fuel pump frequency, Flame/Liquid/Overheat/Board
  temperature, Fan current) only populate while the **Debug** add-on's
  Debug mode is on and Extended telemetry active.

## Datasource is hardcoded, not a prompted import input

Every panel's `datasource` is hardcoded to
`{"type": "victoriametrics-metrics-datasource", "uid": "cfxh0obaj30g0f"}` --
the dedicated VictoriaMetrics Grafana plugin (not the generic "Prometheus"
datasource type), at the UID it has on the instance this was built for.
An earlier version of this file used Grafana's `__inputs`/`${DS_...}`
mechanism to prompt for a datasource on import instead, but that assumed
the generic Prometheus plugin -- this instance uses the VictoriaMetrics
plugin specifically, and there was no matching datasource type to select,
so import failed outright.

**If you're importing this into a different Grafana instance** (or this
one after re-adding the datasource with a new UID), find/replace
`cfxh0obaj30g0f` throughout the file with your own datasource UID first
(Grafana -> Connections -> Data sources -> click your VictoriaMetrics
source -> the UID is in the URL). If your instance uses the generic
Prometheus datasource plugin instead of the VictoriaMetrics one, also
change `"type": "victoriametrics-metrics-datasource"` to `"type":
"prometheus"` throughout.

## Importing

Grafana -> Dashboards -> New -> Import -> upload
`autoterm-5d-dashboard.json`. No prompts -- it should just work against the
datasource baked in above.

## Panels

- At-a-glance stats: cabin/coolant temperature, supply voltage, fault
  code, elapsed run time, burner active.
- **Temperature, revolutions and fuel pump frequency** -- the combined
  panel. Temperature on the left axis; revolutions and fuel pump frequency
  share the right axis (frequency is scaled x50 in the query purely so a
  ~0-5Hz line is visible next to ~0-250 revolutions -- see the panel's own
  description for the real-units caveat, or read it off the dedicated Fuel
  pump frequency panel below instead).
- All temperatures (flame, liquid, coolant, cabin, board, overheat).
- Blower speed: defined vs. measured revolutions.
- Fuel pump frequency (unscaled).
- Electrical: supply voltage + fan current.
- Running time: base (always available) vs. extended (debug mode only).
- Engine / relay state: raw diagnostic codes, not yet individually decoded
  -- useful for spotting *when* they change, not yet for reading a specific
  meaning off the value.
- Status flags: burner active / glow plug / telemetry stale / extended
  telemetry active, as a timeline.
- **State**, **Mode of operation (named)**, **Fault (extended, named)** --
  state-timeline panels, one row per possible value (e.g. Mode of
  operation's row set is Low/Middle/High/each ignition stage/etc). As of
  this writing these three are showing "No data" -- see the section below.
- **Cabin temperature vs. heater output (%)** -- cabin temperature against
  two independent derived "power output" estimates: measured revolutions
  and fuel pump frequency, each normalized against the highest value seen
  within the panel's own displayed time range (`$__range` -- self-adjusting
  as you change the dashboard's time window, since there's no known
  manufacturer spec to normalize against instead). Kept as two series, not
  collapsed to one -- checked against real capture data and they diverge
  sharply outside steady combustion: revolutions-based reads ~26% during
  the pre-ignition glow-plug phase and ~69% during the post-shutdown
  cooldown purge (fuel is 0 in both, no heat is actually being produced),
  while fuel-based correctly reads 0% in both cases. They only agree within
  ~5-10 points during steady mid-to-high combustion. Only extended-frame
  fields, so only populated while Extended telemetry active is on.

## `State`/`Mode of operation`/`Fault (extended, named)` -- currently showing "No data"

These three are text-valued (e.g. "idle", "High", "glow plug warming up"),
and Prometheus/VictoriaMetrics can only store numbers -- Home Assistant's
exporter used to silently drop them entirely. Fixed in `autoterm-debug-addon`
1.2.0: these are now declared as MQTT `enum` sensors (`device_class: enum`
+ an explicit `options` list), which HA's Prometheus integration exports
the same way it already exports the climate entity's `mode`/`action` -- a
separate boolean series per possible value (e.g.
`homeassistant_climate_mode{mode="heat"}` / `{mode="off"}`).

**What's not yet confirmed**: the exact metric name and label key a plain
`sensor`-domain enum uses -- there's no existing example of one on this
instance to check against (climate's `mode`/`action` are entity-specific
attribute names, not necessarily what a generic sensor's own enum state
uses). The three panels above are written assuming metric name
`homeassistant_sensor_state` (the same bucket the other unitless sensors
already use) with a `state="<value>"` label, e.g.:

```
homeassistant_sensor_state{entity="sensor.autoterm_5d_heater_state", state="idle"} 1
```

**To verify**: after updating the add-on to 1.2.0+ and letting it run for a
minute, repeat the same Explore metrics-browser search used to build the
rest of this dashboard, this time for `sensor.autoterm_5d_heater_state` (or
`_mode_of_operation` / `_fault_extended_named`). If the real metric/label
names differ from the guess above, only the `expr` in these three panels
needs updating -- everything else in the dashboard is already confirmed
against real metric names.
