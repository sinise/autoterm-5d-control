# Changelog

## 2.0.0

- **Renamed**, matching the regular add-on's 2.0.0: `Autoterm 5D Debug` ->
  `Autoterm Heater Debug`, slug `autoterm5d_debug` -> `autoterm_heater_debug`,
  repo moved to `autoterm-heater-control`. **Breaking**: every Home
  Assistant entity gets a new entity_id (device name changed from
  "Autoterm 5D Heater" to "Autoterm Heater") -- existing
  automations/dashboards/Grafana panels referencing the old
  `sensor.autoterm_5d_heater_*` entity IDs need updating, and you'll
  likely need to remove and reinstall this add-on from the renamed
  repository. `binar_5s`/`binar_5s_next` are now also marked `tested`
  (same internal codename as `autoterm_flow_5`, byte-identical data).

## 1.6.0

- Added a "Heater profile" config option: the extended-telemetry decoder
  (byte offsets, state/mode names, fault names) is now selectable across
  19 heater models, extracted from the vendor diagnostic tool's own
  per-model Profiles/*.pfl + language.res data files (plaintext, not a
  decompile) -- the same way `autoterm_flow_5`'s fields were originally
  derived. **Only `autoterm_flow_5` is confirmed against real hardware --
  every other profile is untested**: unverified byte offsets, and not
  even confirmed the extended-telemetry mechanism (PUBR0 handshake,
  dev02/type01 frame) works the same way on that model at all. A new
  "Heater profile" sensor shows the active selection and flags
  "(NOT TESTED)" for anything but Flow 5; the add-on log does the same at
  startup. Selecting a different profile only changes which formulas
  decode the extended frame -- it doesn't affect the base 18-byte
  protocol, commands, or Prevent freezing, all of which stay as
  originally confirmed regardless of this setting.

## 1.5.0

- Confirmed on real hardware (reconstructing this add-on's own "Telemetry
  stale" logic against a live capture and matching it second-for-second
  to Home Assistant's own history) that sending the debug handshake while
  the panel's own query/reply exchange is mid-flight can corrupt that
  exchange -- happened on roughly half of handshake sends, consistent
  with a timing collision. Fixed: the handshake is now held until the bus
  has been quiet for 250ms (no frame seen from either device) instead of
  fired blindly on a fixed timer, applied to both the periodic send and
  the "Send debug handshake now" button. Narrows the collision window;
  still treat debug mode as experimental.

## 1.4.0

- Added numeric mirrors for the three `enum` sensors added in 1.2.0:
  "State code", "Mode code" (`state*10+substate`), "Fault code
  (extended)". Confirmed via Grafana Explore against a real instance that
  Home Assistant's Prometheus integration tracks enum sensors'
  availability/last-updated/change-count, but never exports their actual
  text value as a metric -- the 1.2.0 fix didn't actually solve the
  Prometheus/Grafana graphing gap it was meant to. These numeric sensors
  do export normally (same as Fault code/Engine state/Relay state
  already did), and are meant to be name-mapped in Grafana itself (value
  mappings) rather than relying on HA's exporter for that. See `grafana/`
  in the main repo for updated panels using these.

## 1.3.0

- Added a "Prevent freezing" switch and target (0-10°C), mirrored from the
  Autoterm 5D add-on 1.2.0: an independent frost-protection safety net
  that starts the heater whenever cabin temperature reaches the floor,
  regardless of the auto-thermostat's own state or a prior manual Stop.

## 1.2.0

- `State`, `Mode of operation`, and `Fault (extended, named)` are now
  declared with `device_class: enum` and an explicit `options` list
  (required for MQTT enum sensors). Previously these were plain text
  sensors that Home Assistant's Prometheus exporter silently drops (it
  can't export non-numeric values) -- they're now exported the same way
  the climate entity's mode/action already were, one boolean series per
  possible value, so they show up in VictoriaMetrics/Grafana too.

## 1.1.0

- The extended telemetry frame (dev02/type01) is no longer forwarded to
  the physical panel -- confirmed on real hardware that receiving it
  visibly confuses the panel's own display. It's still decoded for HA
  sensors and still logged (marked "NOT forwarded (filtered)") if capture
  logging is on, it just never reaches the panel's wire anymore.
- The add-on's own log messages (info/warning/error) are now also written
  into the capture log, interleaved chronologically with the traffic --
  one file has everything needed to debug an incident.

## 1.0.1

- Moved the capture log from `/share/autoterm_debug/` to
  `/config/autoterm_debug/` -- it now shows up directly in the File editor
  add-on's default file tree instead of requiring extra navigation/config
  to reach `/share`.

## 1.0.0

- Initial release. Based on the Autoterm 5D add-on (1.1.0): same relay,
  commands, and MQTT discovery, plus a toggleable debug-mode extended
  telemetry probe (vendor "PUBR0" handshake), a toggleable raw traffic
  capture log under `/share`, and sensors for every known extended-frame
  field. See `docs/PROTOCOL.md` in the main repo for the field formulas.
