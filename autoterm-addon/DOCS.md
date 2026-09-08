# Autoterm 5D add-on

Bridges an Autoterm 5D diesel heater's UART link to Home Assistant over
MQTT. Full protocol derivation lives in `docs/PROTOCOL.md` in the
[main repo](https://github.com/sinise/autoterm-5d-control) -- read it if you
want to understand *why* the commands below are safe, or if you're adapting
this to different hardware.

## Before you install

- This add-on **directly owns both UART ports** (the panel leg and the
  heater leg of the inline proxy). A serial port can only be opened by one
  process at a time -- **do not** run this at the same time as
  `autoterm_web.py`/`autoterm_proxy.py` on the same ports. Stop and disable
  that systemd service first if you're migrating from it
  (`sudo systemctl disable --now autoterm-web`).
- This assumes you already have the Pi wired **inline** between the panel
  and heater (both legs of the original wire cut, Pi in between) -- not just
  passively tapped. See the main repo's README for passive-tap-vs-inline
  wiring details.
- **Confirm your port assignment by content, not by devnode name.** USB
  serial adapters can re-enumerate after a replug, silently swapping which
  physical connector `/dev/ttyUSB1` vs `/dev/ttyUSB3` refers to -- this has
  bitten this project before (see `CONTEXT.md`). If commands have no effect,
  re-check which port is actually reporting the rich heater status frame vs.
  the bare cabin-temperature reading before assuming a protocol problem.
- **The physical panel keeps working the entire time** -- this add-on only
  relays and additionally injects; it never disables passthrough. It's
  always available as a manual fallback if MQTT, Home Assistant, or the
  add-on itself is down.
- This controls a real combustion appliance. Watch the entities after your
  first Start/Stop before trusting it unattended, same as the standalone
  dashboard.

## Configuration

| Option | Meaning |
|---|---|
| `panel_port` | Serial device wired to the panel leg (default `/dev/ttyUSB1`) |
| `heater_port` | Serial device wired to the heater leg, commands are injected out this port (default `/dev/ttyUSB3`) |
| `baud` | UART baud rate (default `2400`, confirmed on the reference hardware) |
| `autodiscover_ports` | If `true`, probe for the correct ports on every startup instead of trusting `panel_port`/`heater_port` -- see below |
| `preheat_default_minutes` | Initial value of the Preheat duration entity |
| `auto_target_default` | Initial target for the auto-thermostat climate entity |
| `mqtt_host`/`mqtt_port`/`mqtt_username`/`mqtt_password` | Only used as a fallback if no MQTT service (e.g. the Mosquitto broker add-on) is auto-discovered |

If you have the official **Mosquitto broker** add-on (or any add-on
providing the `mqtt` service) installed, this add-on finds it automatically
and the `mqtt_*` options can be left blank.

## Port autodiscovery

USB-serial adapters can re-enumerate on replug, silently swapping which
physical connector `/dev/ttyUSB1` vs `/dev/ttyUSB3` refers to -- this has
bitten this exact project before (see `CONTEXT.md` in the main repo).
Turning on `autodiscover_ports` runs a probe at every startup instead of
trusting the configured device paths:

1. **Find the panel** -- listen (read-only) on every `/dev/ttyUSB*` and
   `/dev/ttyACM*` device at once, up to 8 seconds, for a valid frame from
   dev03. The panel appears to report its cabin temperature on its own,
   without needing anything from the heater side, so this works from pure
   listening.
2. **Find the heater** -- on each remaining candidate, send the empty
   type0f status query (the one documented, non-actuating "poll" the panel
   itself sends -- see `docs/PROTOCOL.md`) and listen for the heater's
   18-byte dev04 reply. **Only this empty query is ever sent during
   discovery -- never a start (`type01`/`type02`) or stop (`type03`)
   command**, since those actually move the heater's state machine and must
   never be used just to probe a port.

If both are found, they're saved back into this add-on's own configuration
(so the Configuration tab reflects reality, and you can turn
`autodiscover_ports` back off afterward) and used for that run. If either
step fails (nothing found within the timeout), the add-on logs why and
falls back to whatever `panel_port`/`heater_port` are currently configured
-- it never refuses to start over a failed discovery.

This is a heuristic based on how the wiring has behaved on the reference
hardware (see `docs/PROTOCOL.md`'s notes on device roles), not a certainty
for every unit/firmware revision. Watch the add-on log on first use, and
cross-check with the physical panel that the labeled entities actually
track what you expect.

## What you get

A single "Autoterm 5D Heater" device in Home Assistant with:

- **Sensors**: State (idle/running/late-run/cooldown/final-shutdown), Fault
  code, Cabin temperature, Coolant temperature, Elapsed run time
- **Binary sensors**: Burner active, Telemetry stale (diagnostic -- turns on
  if no fresh frames have arrived in 5s, e.g. a wiring or port problem)
- **Climate entity** ("Autoterm thermostat"): mode `off`/`heat` toggles the
  add-on's own software hysteresis loop (stops the heater at target+1°C,
  starts it at target-1°C in thermostat mode); shows current cabin
  temperature and burner state as HVAC action
- **Number**: Preheat duration (minutes), used by the Start preheat button
- **Buttons**: Start preheat, Start thermostat (manual, one-shot -- distinct
  from the climate entity's automatic loop), Stop, Start pump (ventilation
  only, no combustion -- runs the circulation fan/pump without heat; stop it
  with the same Stop button)

All confirmed protocol commands (start preheat/thermostat, stop) and the
device-role/frame-format knowledge this relies on are reused byte-for-byte
from `autoterm_web.py`/`docs/PROTOCOL.md` in the main repo -- not
re-derived. The auto-thermostat hysteresis logic is also unchanged from
there.

## Persistence

Preheat duration and the auto-thermostat's enabled/target settings are
saved to the add-on's `/data` volume, so they survive an add-on restart
without falling back to the config defaults above.

## Troubleshooting

- **No entities appear in Home Assistant**: check MQTT is actually
  discovered (add-on log should say "Using MQTT service auto-discovery"
  rather than the fallback-options warning) and that the MQTT integration is
  set up in Home Assistant (Settings -> Devices & Services).
- **Entities show unavailable**: the add-on publishes an MQTT "offline" LWT
  on crash/stop -- check the add-on log for a serial error (wrong port,
  permission, or an unplugged adapter).
- **Commands have no visible effect**: this exact failure mode has happened
  before in this project from a wrong port/device-byte assumption -- see
  `docs/PROTOCOL.md`'s "Important history" note. Re-verify port assignment
  by content before assuming the command itself is wrong.
