# Changelog

## 1.0.0

- Initial release. Owns both UART legs of the inline proxy directly (runs
  instead of `autoterm_web.py`, not alongside it), publishes decoded status
  and injects start/stop/thermostat commands via MQTT with Home Assistant
  MQTT discovery.
