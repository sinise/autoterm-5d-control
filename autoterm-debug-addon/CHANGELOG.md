# Changelog

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
