# Changelog

## 2.1.0

- Fixed a likely cause of periodic "no communication" glitches on the
  physical panel and the heater appearing to restart on its own every
  30-40 minutes with Auto thermostat (or Prevent freezing) enabled: every
  command this add-on injects toward the heater (Start preheat/thermostat,
  Stop, Start pump -- both the manual buttons and the automatic
  stop/start-thermostat calls Auto thermostat and Prevent freezing make on
  their own) was written straight onto the bus with no check for whether
  the panel's own query/reply exchange was already mid-flight. That's the
  same collision mechanism the debug add-on's 1.5.0 fix addressed for its
  diagnostic handshake, but it was never applied here -- and Auto
  thermostat/Prevent freezing routinely fire on exactly this kind of
  interval, with no debug mode involved. Every injected command now waits
  for a 250ms quiet gap on the bus first (same as the debug handshake),
  up to a 2s cap. As with that fix, this narrows the collision window
  rather than formally proving it eliminated -- if you were seeing this,
  it's worth confirming the frequency actually drops.

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
