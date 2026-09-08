# Changelog

## 2.0.0

- **Renamed**: add-on name `Autoterm 5D` -> `Autoterm Heater`, slug
  `autoterm5d` -> `autoterm_heater`, repo moved to
  `autoterm-heater-control` -- reflects that the underlying project (and
  the debug add-on's per-model profiles) now covers Autoterm-family
  heaters generally, not just the 5D/Flow 5 this add-on itself is
  confirmed against. **Breaking**: every Home Assistant entity gets a new
  entity_id (derived from the device name, which changed from "Autoterm
  5D Heater" to "Autoterm Heater") -- existing automations/dashboards
  referencing the old `sensor.autoterm_5d_heater_*` entity IDs need
  updating, and you'll likely need to remove and reinstall this add-on
  from the renamed repository. See the main repo's README for the
  updated install URL.

## 1.2.0

- Added a "Prevent freezing" switch and target (0-10°C): an independent
  frost-protection safety net that starts the heater whenever cabin
  temperature reaches the floor, regardless of the auto-thermostat's own
  state or a prior manual Stop. Never stops a run it didn't start, so it
  doesn't fight the auto-thermostat or a manual preheat session.

## 1.1.0

- Added a "Start pump (ventilation only)" button, using a newly-confirmed
  command (`type 0x21`, payload `00 28`) recovered from a real capture
  against the vendor's diagnostic tool. See `docs/PROTOCOL.md`.

## 1.0.0

- Initial release. Owns both UART legs of the inline proxy directly (runs
  instead of `autoterm_web.py`, not alongside it), publishes decoded status
  and injects start/stop/thermostat commands via MQTT with Home Assistant
  MQTT discovery.
