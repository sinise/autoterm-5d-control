# Changelog

## 1.1.0

- Added a "Start pump (ventilation only)" button, using a newly-confirmed
  command (`type 0x21`, payload `00 28`) recovered from a real capture
  against the vendor's diagnostic tool. See `docs/PROTOCOL.md`.

## 1.0.0

- Initial release. Owns both UART legs of the inline proxy directly (runs
  instead of `autoterm_web.py`, not alongside it), publishes decoded status
  and injects start/stop/thermostat commands via MQTT with Home Assistant
  MQTT discovery.
