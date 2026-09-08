# Autoterm 5D control

Reverse-engineered UART protocol, a passive monitor, an inline proxy, and a
web dashboard for controlling an **Autoterm 5D diesel heater** (and its
comfort panel) from a Raspberry Pi -- built for a boat installation, but the
protocol and tooling aren't boat-specific.

Status: passive decoding and live command injection (start/stop) are
**working and verified against real hardware**. A Home Assistant add-on is
also available -- see [`autoterm-addon/`](autoterm-addon/).

## What's in here

| Script | Purpose |
|---|---|
| `autoterm/autoterm_monitor.py` | Passive dual-port UART monitor. Frames, CRC-checks, and logs traffic in both directions. Never writes to the bus. |
| `autoterm/autoterm_analyze.py` | Offline analysis of monitor logs: CRC verification, frame inventory, per-byte field diffing across two captures, request/response timing. |
| `autoterm/autoterm_proxy.py` | Transparent inline passthrough proxy (once the panel-heater wire is physically cut and the Pi sits between them). Relays every byte immediately; never injects anything. |
| `autoterm/autoterm_web.py` | Everything `autoterm_proxy.py` does, plus a REST API, a self-contained web dashboard, command injection (start preheat/thermostat, stop), and a software hysteresis auto-thermostat loop. This is the one you actually run. |
| `tools/baud_sweep.sh` | Baud-rate discovery sweep, for bringing this up on unfamiliar hardware. |
| `docs/PROTOCOL.md` | Full protocol writeup: frame format, CRC, device roles, message catalog, state machine, confirmed commands, open questions. |
| `autoterm-addon/` | Home Assistant Supervisor add-on: owns the serial ports directly (runs instead of `autoterm_web.py`), publishes status and exposes controls via MQTT discovery. See its `DOCS.md`. |
| `autoterm-debug-addon/` | Same as `autoterm-addon/`, plus optional extended-telemetry probing and a downloadable raw traffic capture log, for continuing the protocol reverse-engineering. Install instead of `autoterm-addon/`, not alongside it. See its `DOCS.md`. |

## Hardware

- Raspberry Pi (any model with enough USB ports / a multi-port USB-serial
  adapter)
- A USB-to-UART adapter exposing (at least) two independent **5V TTL**
  serial ports -- not RS-232, and not 3.3V-only unless it's confirmed
  5V-tolerant on its inputs.
- Two data wires spliced into the panel<->heater harness -- see the wiring
  note below -- plus a shared ground with the heater/panel circuit.
  **A missing ground produces pure garbage, not silence** -- check this
  first if a capture looks like noise.
- On the reference harness, the two data wires are **yellow** (carries
  data from the heater to the panel) and **white** (carries data from the
  panel to the heater). There's also usually a **red +12V power** wire in
  the same harness -- **leave it alone**. It's not a data signal; feeding
  12V into a UART pin built for 3.3V/5V logic can permanently damage the
  adapter (and possibly the Pi behind it) if that input isn't rated for
  it. See [`autoterm-addon/DOCS.md`, "Wiring"](autoterm-addon/DOCS.md#wiring-connecting-the-pi-to-the-heater)
  for the full step-by-step and a diagram.

### Passive tap vs. inline proxy

Two distinct wiring modes, don't mix them up:

- **Passive tap** (`autoterm_monitor.py`): each port's RX is tapped onto an
  existing panel-TX or heater-TX wire, non-invasively. Nothing is cut,
  nothing is injected, zero risk to normal operation. Start here.
- **Inline proxy** (`autoterm_proxy.py` / `autoterm_web.py`): the direct
  panel<->heater wire pair is physically **cut** on both legs, and the Pi's
  two ports are wired in between -- each port's RX still taps what it used
  to, but its TX now drives the far end's RX. If the original direct wire
  is left connected in parallel with the Pi, both sides see their real
  signal plus a delayed echo, which looks exactly like corruption.

**Confirm which physical port reaches which device before trusting a
default port assignment.** On the reference hardware, the USB-serial
adapter re-enumerated during the rewiring (port names shifted), and
separately the device roles themselves were initially assumed backwards
(see `docs/PROTOCOL.md`) -- both mistakes produced no error and no
reaction, which is the most misleading failure mode here. Verify by
content: read a few seconds of traffic on each port and check which one is
reporting a heater-shaped rich status frame vs. a simple cabin-temperature
reading, rather than trusting a port number.

## Installing

### One-line install

```bash
curl -fsSL https://raw.githubusercontent.com/sinise/autoterm-5d-control/main/install.sh | bash
```

This installs OS dependencies, clones the repo, adds you to the `dialout`
group if needed, and sets up `autoterm-web.py` as a systemd service that
starts on boot and restarts on failure. See `install.sh` for the
environment variables that control install location and serial port
assignment (`AUTOTERM_PANEL_PORT`, `AUTOTERM_HEATER_PORT`, etc).

### Manual install

```bash
git clone https://github.com/sinise/autoterm-5d-control.git
cd autoterm-5d-control
./install.sh
```

### Uninstall

```bash
./uninstall.sh
```
Stops and removes the systemd service. Leaves the checkout and logs alone.

## Using it

Once running (`systemctl status autoterm-web`), find the port it bound to
(it scans 8083-8090 for the first free one):

```bash
cat ~/autoterm_logs/web_port.txt
```

Then open `http://<pi-address>:<that-port>/` in a browser. The dashboard
shows live decoded state (idle/running/cooldown/etc), fault code, cabin
temp, coolant temp, elapsed run time, and a raw frame log, plus:

- **Start preheat** (with a duration in minutes)
- **Start thermostat**
- **Stop**
- **Auto thermostat**: set a target cabin temperature; the heater is
  stopped at target+1°C and (re)started at target-1°C, entirely in
  software, independent of the manual buttons.

Follow live logs with `journalctl -u autoterm-web -f`. Every relayed frame
and every injected command is also written to a timestamped file under
`~/autoterm_logs/`.

### Passive monitoring / analysis only

If you just want to watch traffic without any proxy/injection risk:

```bash
python3 autoterm/autoterm_monitor.py --panel /dev/ttyUSB1 --heater /dev/ttyUSB3
python3 autoterm/autoterm_analyze.py inventory ~/autoterm_logs/capture_*.log
```

## Safety notes

- This controls a real combustion appliance. The physical panel keeps
  working normally the entire time the proxy/web service runs (nothing
  about passthrough is ever disabled) -- it's always available as a manual
  fallback.
- The **stop** command is well-confirmed (three independent real captures,
  verified live). **Start** is confirmed for preheat and thermostat modes
  but the duration encoding has at least one known inconsistency -- see
  `docs/PROTOCOL.md`. Watch the dashboard/log on first use of any command
  rather than assuming success.
- The auto-thermostat loop only acts on fresh cabin-temperature readings
  (it ignores stale/missing data) and rate-limits its own actions, but it
  is still new code controlling a fuel-burning appliance unattended --
  supervise it through at least one full cycle before trusting it
  unattended overnight.

## Home Assistant

[`autoterm-addon/`](autoterm-addon/) is a
Supervisor add-on (Docker) that runs **instead of** `autoterm-web` -- it
owns the serial ports directly and talks to Home Assistant over MQTT
discovery, so the heater shows up as a single device with sensors (state,
fault, cabin/coolant temp, elapsed run time), a climate entity driving the
same auto-thermostat hysteresis loop, and Start preheat/Start
thermostat/Stop buttons. See its `DOCS.md` for wiring prerequisites,
options, and installation (copy the folder to `/addons/` on the Home
Assistant host, or add this repo as a custom add-on repository).

[`autoterm-debug-addon/`](autoterm-debug-addon/) is the same add-on plus
protocol reverse-engineering tools: an optional periodic probe that unlocks
the vendor diagnostic tool's richer extended telemetry (fan speed, fuel
pump frequency, temperatures, voltage, named operating mode -- see
`docs/PROTOCOL.md`), sensors for all of it, and a toggleable raw traffic
capture log saved under `/config/autoterm_debug/` for further analysis. Install it instead
of `autoterm-addon/`, not alongside it -- see its `DOCS.md` before enabling
debug mode, it sends an experimental handshake frame toward the heater on
the live bus (a known issue where the heater's reply confused the physical
panel has been fixed by filtering that reply before it reaches the panel,
but treat it as still experimental beyond that).

## Roadmap

- **openHAB / InfluxDB integration** -- investigated but not built (this
  project moved to Home Assistant instead, see above). See `CONTEXT.md` for
  the openHAB-specific findings (openHAB 5.2.1 + InfluxDB 1.x confirmed
  running, Mosquitto/paho-mqtt not yet installed) if picking that up later.
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
