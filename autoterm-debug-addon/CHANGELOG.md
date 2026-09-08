# Changelog

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
