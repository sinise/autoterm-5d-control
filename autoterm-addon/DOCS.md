# Autoterm Heater add-on

Bridges an Autoterm-family diesel heater's UART link to Home Assistant
over MQTT. Confirmed against a real Autoterm 5D / Flow 5 (internally
BINAR-5S) unit; the base protocol this add-on relies on (frame format,
commands) is not yet independently confirmed on other models -- see
`docs/PROTOCOL.md`. If you're on a different model, the
[Autoterm Heater Debug](../autoterm-debug-addon/) add-on's "Heater
profile" option covers extended telemetry for 19 vendor profiles, useful
for helping validate this on other hardware. Full protocol derivation
lives in `docs/PROTOCOL.md` in the
[main repo](https://github.com/sinise/autoterm-heater-control) -- read it
if you want to understand *why* the commands below are safe, or if you're
adapting this to different hardware.

## Before you install

- This add-on **directly owns both UART ports** (the panel leg and the
  heater leg of the inline proxy). A serial port can only be opened by one
  process at a time -- **do not** run this at the same time as
  `autoterm_web.py`/`autoterm_proxy.py` on the same ports. Stop and disable
  that systemd service first if you're migrating from it
  (`sudo systemctl disable --now autoterm-web`).
- This assumes you already have the Pi wired **inline** between the panel
  and heater (both legs of the original wire cut, Pi in between) -- not just
  passively tapped. See "Wiring" below for exactly how, and the main repo's
  README for passive-tap-vs-inline wiring details in general.
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

## Wiring: connecting the Pi to the heater

**Hardware needed:** a USB-to-serial adapter exposing **two independent
5V TTL UART interfaces** (not RS-232, and not a 3.3V-only adapter unless
it's confirmed 5V-tolerant on its inputs -- the panel/heater bus runs 5V
TTL logic). A single 4-port adapter (e.g. an FTDI/CP2108-based quad
adapter) works well since it gives you two spare ports beyond the two this
add-on needs.

**The panel-heater harness has (at least) four wires you care about:**

| Wire | Carries |
|---|---|
| Yellow | The panel/display's **RX** -- i.e. this is the wire the **heater transmits on** |
| White | The panel/display's **TX** -- i.e. this is the wire the **heater receives on** |
| Red | **+12V power**, not a data signal |
| Black (or similar) | Ground, common to the whole harness |

**Cut both the yellow and the white wire** (only those two -- leave red and
black intact) at a convenient point between the panel and the heater. Each
cut leaves a "panel-side" stub and a "heater-side" stub. Wire each stub to
the UART port on that same side -- one wire, one direction of travel, per
diagram:

```
YELLOW wire -- carries data FROM the heater TO the panel:

   HEATER >---[cut]---> HEATER_PORT's RX pin

        (add-on relays it here, in software)

   PANEL_PORT's TX pin >---[cut]---> PANEL / DISPLAY


WHITE wire -- carries data FROM the panel TO the heater:

   PANEL / DISPLAY >---[cut]---> PANEL_PORT's RX pin

        (add-on relays it here, in software)

   HEATER_PORT's TX pin >---[cut]---> HEATER
```

So: `heater_port` RX = yellow's heater-side stub, `heater_port` TX =
white's heater-side stub; `panel_port` RX = white's panel-side stub,
`panel_port` TX = yellow's panel-side stub. The add-on relays bytes
between `panel_port` and `heater_port` in software (see
`docs/PROTOCOL.md`), so the panel and heater talk exactly as before, just
through the Pi in the middle.

**Do not connect the red wire to anything on the Pi or the USB-serial
adapter.** It's +12V, not a logic-level signal -- feeding 12V into a UART
RX pin built for 3.3V/5V logic can permanently damage the adapter (and
possibly the Pi's USB port behind it) if that input isn't rated for it.
Leave it connected exactly as it already is between the panel and heater;
this add-on has no reason to touch the power wire at all.

**Ground is not optional.** Tie the Pi's GND (shared between both UART
ports is fine) to the harness's black/ground wire. A missing shared ground
produces pure garbage on the line, not silence -- if a capture looks like
noise, check this first.

Confirm which physical port ended up as `panel_port` vs `heater_port` by
**content, not by assumption** -- USB-serial adapters can re-enumerate
after a replug (see the port-autodiscovery section below and `CONTEXT.md`
in the main repo for why this matters and how to check).

## Configuration

| Option | Meaning |
|---|---|
| `panel_port` | Serial device wired to the panel leg (default `/dev/ttyUSB1`) |
| `heater_port` | Serial device wired to the heater leg, commands are injected out this port (default `/dev/ttyUSB3`) |
| `baud` | UART baud rate (default `2400`, confirmed on the reference hardware) |
| `autodiscover_ports` | If `true`, probe for the correct ports on every startup instead of trusting `panel_port`/`heater_port` -- see below |
| `preheat_default_minutes` | Initial value of the Preheat duration entity |
| `auto_target_default` | Initial target for the auto-thermostat climate entity |
| `prevent_freezing_target_default` | Initial value of the Prevent freezing target entity (0-10°C) -- see below |
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

A single "Autoterm Heater" device in Home Assistant with:

- **Sensors**: State (idle/running/late-run/cooldown/final-shutdown), Fault
  code, Cabin temperature, Coolant temperature, Elapsed run time
- **Binary sensors**: Burner active, Telemetry stale (diagnostic -- turns on
  if no fresh frames have arrived in 5s, e.g. a wiring or port problem)
- **Climate entity** ("Autoterm thermostat"): mode `off`/`heat` toggles the
  add-on's own software hysteresis loop (stops the heater at target+1°C,
  starts it at target-1°C in thermostat mode); shows current cabin
  temperature and burner state as HVAC action
- **Number**: Preheat duration (minutes), used by the Start preheat button;
  Prevent freezing target (°C, 0-10)
- **Switch**: Prevent freezing -- see below
- **Buttons**: Start preheat, Start thermostat (manual, one-shot -- distinct
  from the climate entity's automatic loop), Stop, Start pump (ventilation
  only, no combustion -- runs the circulation fan/pump without heat; stop it
  with the same Stop button)

All confirmed protocol commands (start preheat/thermostat, stop) and the
device-role/frame-format knowledge this relies on are reused byte-for-byte
from `autoterm_web.py`/`docs/PROTOCOL.md` in the main repo -- not
re-derived. The auto-thermostat hysteresis logic is also unchanged from
there.

## Prevent freezing

An independent frost-protection safety net, separate from the auto-
thermostat climate entity above. When the **Prevent freezing** switch is
on, the heater is started (thermostat mode) whenever cabin temperature
reaches the **Prevent freezing target** (0-10°C) -- **regardless of
whether the auto-thermostat climate entity is on or off, and regardless of
a prior manual Stop.** That's the point of the feature: it can't be
silently defeated by turning normal heating off or pressing Stop once --
only turning the Prevent freezing switch itself off disables it.

It won't fight anything else, though: it never stops a heater run it
didn't start (so it doesn't interrupt the auto-thermostat's own comfort
run, or a manual preheat session, or another admin's separate Start), and
if you disable Prevent freezing while it's mid-run, that run is left
running rather than cut off abruptly -- something else (manual Stop, the
auto-thermostat) needs to end it.

**Practical implication:** if it's cold and Prevent freezing is on, a
plain Stop button press won't keep the heater off -- it'll restart within
seconds once cabin temperature is still at/below the floor. To actually
stop the heater in that situation, turn off Prevent freezing first (or
raise its target below the current cabin temperature).

## Persistence

Preheat duration, the auto-thermostat's enabled/target settings, and
Prevent freezing's enabled/target settings are saved to the add-on's
`/data` volume, so they survive an add-on restart without falling back to
the config defaults above.

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
- **Panel briefly shows "no communication", or the heater seems to restart
  on its own every 30-40 minutes**: fixed in 2.1.0. Any command this
  add-on injects toward the heater -- including the automatic
  stop/start-thermostat calls Auto thermostat and Prevent freezing make on
  their own -- could land while the panel's own query/reply exchange was
  mid-flight and corrupt it; this is most visible with Auto thermostat
  enabled, since its own hysteresis logic fires on roughly this kind of
  interval. 2.1.0 makes every injected command wait for a quiet moment on
  the bus first. See CHANGELOG.md and `docs/PROTOCOL.md`.
