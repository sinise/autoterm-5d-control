# Autoterm Heater Control

A Home Assistant Supervisor add-on and a Grafana dashboard for an
**Autoterm-family diesel heater** and its comfort panel, controlled from a
Raspberry Pi wired inline between the two -- built for a boat installation,
but the protocol and add-on aren't boat-specific. Confirmed against a real
**Autoterm 5D / Flow 5** (internally BINAR-5S) unit; the add-on's optional
Heater profile setting extends extended-telemetry decoding to 19 vendor
models, only the Flow 5/BINAR-5S family of which is independently
confirmed -- see its DOCS.md.

Status: passive decoding and live command injection (start/stop, preheat,
thermostat) are **working and verified against real hardware**.

## What's in here

| Path | Purpose |
|---|---|
| `autoterm-debug-addon/` | The Home Assistant Supervisor add-on -- owns both UART ports directly, publishes status and exposes controls via MQTT discovery, plus optional extended-telemetry probing and a raw traffic capture log. See its `DOCS.md`. |
| `grafana/` | A Grafana dashboard for the add-on's entities (via Home Assistant's Prometheus integration + VictoriaMetrics). See its `README.md`. |
| `docs/PROTOCOL.md` | Full protocol writeup: frame format, CRC, device roles, message catalog, state machine, confirmed commands, open questions. |

## Hardware

- Raspberry Pi (any model with enough USB ports / a multi-port USB-serial
  adapter) running Home Assistant OS/Supervised.
- A USB-to-UART adapter exposing (at least) two independent **5V TTL**
  serial ports -- not RS-232, and not 3.3V-only unless it's confirmed
  5V-tolerant on its inputs.
- Two data wires spliced into the panel<->heater harness, **cut** so the
  Pi sits inline between panel and heater -- plus a shared ground with the
  heater/panel circuit. **A missing ground produces pure garbage, not
  silence** -- check this first if a capture looks like noise.
- On the reference harness, the two data wires are **yellow** (carries
  data from the heater to the panel) and **white** (carries data from the
  panel to the heater). There's also usually a **red +12V power** wire in
  the same harness -- **leave it alone**. It's not a data signal; feeding
  12V into a UART pin built for 3.3V/5V logic can permanently damage the
  adapter (and possibly the Pi behind it) if that input isn't rated for
  it. See [the add-on's DOCS.md, "Wiring"](autoterm-debug-addon/DOCS.md#wiring-connecting-the-pi-to-the-heater)
  for the full step-by-step and a diagram.

**Confirm which physical port reaches which device before trusting a
default port assignment.** USB-serial adapters can re-enumerate on replug
(port names shifting which physical connector they refer to), and getting
the device roles backwards produces silent no-op, not an error -- the most
misleading failure mode here. The add-on's `autodiscover_ports` option
handles this automatically (see its DOCS.md); otherwise verify by content
(read a few seconds of traffic on each port and check which one reports a
heater-shaped rich status frame vs. a simple cabin-temperature reading)
rather than trusting a port number.

## Installing

This is a standard Home Assistant Supervisor add-on -- either:

- **Add this repository**: Settings -> Add-ons -> Add-on Store -> ⋮ menu ->
  Repositories -> add `https://github.com/sinise/autoterm-heater-control`,
  then install "Autoterm Heater Debug" from the store.
- **Or copy manually**: copy `autoterm-debug-addon/` into `/addons/` on the
  Home Assistant host, then install it from the local add-ons list.

See the add-on's `DOCS.md` for wiring, configuration options, and what you
get once it's running.

## Safety notes

- This controls a real combustion appliance. The physical panel keeps
  working normally the entire time the add-on runs (nothing about the
  passthrough relay is ever disabled) -- it's always available as a manual
  fallback.
- The **stop** command is well-confirmed (three independent real captures,
  verified live). **Start** is confirmed for preheat and thermostat modes
  but the duration encoding has at least one known inconsistency -- see
  `docs/PROTOCOL.md`. Watch the entities/log on first use of any command
  rather than assuming success.
- The auto-thermostat loop only acts on fresh cabin-temperature readings
  (it ignores stale/missing data) and rate-limits its own actions, but it
  is still code controlling a fuel-burning appliance unattended --
  supervise it through at least one full cycle before trusting it
  unattended overnight.
- Debug mode (extended telemetry probing) sends an experimental handshake
  frame toward the heater on the live bus -- off by default; read the
  add-on's DOCS.md before turning it on.

## Roadmap

- Nail down the `type04`/`type06` unidentified message pair.
- Resolve the preheat duration encoding inconsistency.

## Credits

Frame format and device-ID hunches for this family of heater protocols
drew on prior community reverse-engineering of similar Planar/Autoterm
units:
- github.com/kalutep/AutotermHeaterController
- github.com/prclm/AutotermHeaterController
- github.com/schroeder-robert/autoterm-air-2d-serial-control

Everything specific to the 5D variant here (CRC identification, frame
layout, device roles, state machine, confirmed commands) was independently
reverse-engineered from scratch against real hardware.

## License

MIT -- see `LICENSE`.
