# Autoterm 5D Debug add-on

Everything the regular [Autoterm 5D add-on](../autoterm-addon/DOCS.md) does,
plus tools for continuing the protocol reverse-engineering: optional
extended-telemetry probing, a full raw-traffic capture log, and sensors for
every field decoded so far. This is **not** needed for normal day-to-day
heater control -- install the regular add-on for that. Install this one
instead of it when you want the extra data or are helping debug the
protocol further.

Full protocol derivation lives in `docs/PROTOCOL.md` in the
[main repo](https://github.com/sinise/autoterm-5d-control) -- read it,
especially "Extended diagnostic-mode telemetry", before enabling debug mode.

## Before you install

Same prerequisites as the regular add-on: this owns both UART ports
directly, so **do not** run it alongside `autoterm_web.py`/`autoterm_proxy.py`
or the regular Autoterm 5D add-on on the same ports -- only one process can
hold a serial port open. Install **either** the regular add-on **or** this
one, not both at once (they'll fail to start if you try -- the second one
to start won't be able to open the ports).

This add-on is safe to install and run with debug mode and capture logging
both left **off** (the default) -- in that mode it behaves exactly like the
regular add-on. The extra risk described below only applies once you
actually turn debug mode on.

## Debug mode: extended telemetry probing

**What it does:** periodically sends the vendor diagnostic tool's "PUBR0"
handshake frame out the heater port. On a direct PC<->heater connection,
that handshake makes the heater start streaming a much richer 58-byte
telemetry frame once per second -- fan speed (defined vs. measured),
fuel pump frequency, flame/liquid/overheat/board temperature, supply
voltage, and a named operating mode/sub-mode (Low/Middle/High/Ignition
stages/etc), instead of just the basic 18-byte status. See
`docs/PROTOCOL.md` for exactly which fields and their formulas.

**The 58-byte extended telemetry frame itself never reaches the physical
panel.** Confirmed directly on real hardware: the panel visibly gets
confused if it receives that frame (it's not something its own firmware
was ever designed to parse). Since 1.1.0, the add-on filters that specific
frame out of the heater->panel relay direction -- it's still decoded for
the sensors below, and still logged (marked "NOT forwarded (filtered)") if
capture logging is on, it just never lands on the panel's wire.

**A second, separate issue was found and (partially) fixed in 1.5.0:**
sending the handshake itself, while it's queued to go out the same
heater_port line the panel's own query/reply traffic is relayed over, can
corrupt that traffic -- confirmed by reconstructing this add-on's own
"Telemetry stale" logic against a real capture and matching it
second-for-second to Home Assistant's actual stale/OK history, and by
finding a real 18-byte heater reply missing 2 bytes immediately after a
handshake send. It happened on roughly half of handshake sends, not all --
consistent with a timing collision, not a guaranteed failure. 1.5.0 holds
the handshake until the bus has been quiet for 250ms before sending it,
which narrows the collision window, but this has not been re-validated
against a fresh long capture the way the panel-confusion fix was --
**treat debug mode as experimental**, not fully solved, and watch
`Telemetry stale`/the capture log after updating rather than assuming this
is now perfect.

There's also a **Send debug handshake now** button, for sending exactly one
handshake on demand instead of waiting for the periodic timer -- useful for
a single closely-watched test. It waits for the same quiet gap before
sending.

The **Extended telemetry active** binary sensor tells you whether the
heater is actually replying with the richer frame (turns on once a valid
58-byte extended frame has arrived within the last 5 seconds) -- if debug
mode is on but this stays off, the handshake isn't getting a reply, which
is itself useful information.

## Heater profile: other models

The extended telemetry frame's field formulas (byte offsets, state/mode
names, fault names) are model-specific. The **Heater profile** option
selects which model's formulas decode the frame -- 19 choices, extracted
from the vendor diagnostic tool's own per-model `Profiles/*.pfl` files
plus its `language.res` string table (plaintext data files read directly,
not a decompile of the tool itself), the same way `autoterm_flow_5`'s
fields were originally derived and cross-checked against a real capture.

**Only `autoterm_flow_5` is confirmed against real hardware.** Every other
option below is read straight from the vendor tool's own data and has
**never been validated**: the byte offsets could be wrong, the field set
could be incomplete (some models expose 5-6 temperature-ish registers;
only 4 slots are wired up here -- see "Known limitation" below), and it
isn't even confirmed that the extended-telemetry mechanism itself (the
`PUBR0` handshake, the `dev02`/`type01` frame) works the same way on that
model, or applies at all. The add-on logs a warning at startup, and a
**Heater profile** sensor shows the active selection with a `(NOT TESTED)`
suffix, for anything but Flow 5.

| Option value | Vendor tool's display name | Internal codename | Status |
|---|---|---|---|
| `14tc_10_molex` | 14TC-10 MOLEX | 4TC-10 MOLEX | untested |
| `autoterm_air_2d` | AUTOTERM AIR 2D | PLANAR-2MK | untested |
| `autoterm_air_4d` | AUTOTERM AIR 4D | PLANAR-44MK | untested |
| `autoterm_air_8d` | AUTOTERM AIR 8D | PLANAR-8D | untested |
| `autoterm_air_9d` | AUTOTERM AIR 9D | PLANAR-9D | untested |
| `autoterm_flow_5` | AUTOTERM FLOW 5 | BINAR-5S | **tested** |
| `binar_5s_next` | BINAR-5S-NEXT | BINAR-5S | untested (byte-identical to Flow 5's data, but not itself tested) |
| `binar_5s` | BINAR-5S | BINAR-5S | untested (byte-identical to Flow 5's data, but not itself tested) |
| `planar_2_with_flame_sensor` | PLANAR-2 with flame sensor | PLANAR-2 with flame sensor | untested |
| `planar_2d` | PLANAR-2D | PLANAR-2D | untested |
| `planar_2mk` | PLANAR-2MK | PLANAR-2MK | untested |
| `planar_44d_s_p` | PLANAR-44D-S-P | PLANAR-44D-SP | untested |
| `planar_44mk` | PLANAR-44MK | PLANAR-44MK | untested |
| `planar_4d_s_p` | PLANAR-4D-S-P | PLANAR-4D | untested |
| `planar_4d` | PLANAR-4D | PLANAR-4D | untested |
| `planar_8d_s_p` | PLANAR-8D-S-P | PLANAR-8D | untested |
| `planar_9d` | PLANAR-9D | PLANAR-9D | untested |
| `sputnik_2` | SPUTNIK-2 | SPUTNIK-2 | untested |
| `sputnik_3` | SPUTNIK-3 | Sputnik-3 | untested |

Several of these share an "internal codename" -- e.g. `autoterm_air_4d`
and `planar_44mk` are the exact same underlying protocol under a different
market name in the vendor tool, confirmed from the `.pfl` files
themselves (not a guess). Selecting either gives identical decoding.

**What stays the same regardless of this setting:** the base 18-byte
`type0f` protocol, all confirmed commands (Start/Stop/Prevent freezing/
etc), and the panel-filter fix (the extended frame is still never
forwarded to the physical panel) -- none of that is profile-specific, all
of it stays exactly as already confirmed for the 5D/Flow 5 hardware this
whole project is built against. Only the *decoding* of the extended
58-byte frame's contents changes.

**Known limitation:** some models expose more temperature-ish registers
(e.g. separate "in"/"out"/"heat exchanger"/"external sensor" readings)
than the 4 fixed slots (Flame/Liquid/Overheat/Board temperature) this
add-on has entities for -- extras beyond the first 4 (prioritized by
closest name match to Flow 5's own fields) aren't currently exposed. Their
formulas are still in the add-on's source if you want to add sensors for
them.

## Raw traffic capture log

**What it does:** the **Capture raw traffic log** switch turns on a
plain-text log of every message seen -- every valid frame, every bad-CRC
frame, and every stray (unparsed) byte -- tagged with who sent it:

- `display` -- the physical comfort panel
- `heater` -- the heater
- `rpi` -- this add-on itself (injected commands, and the debug handshake
  if debug mode is on)

Each line has a timestamp, the sender, CRC status, decoded `dev`/`type`/
`len` where applicable, and the full frame in hex -- the same convention
`autoterm_monitor.py` in the main repo uses, so it's directly comparable to
other captures in this project. Since 1.1.0, the add-on's own log messages
(info/warning/error -- MQTT status, serial errors, commands sent, etc) are
also written into this same file, tagged `log`, interleaved chronologically
with the traffic -- so one file is normally everything needed for further
analysis. You don't need to separately pull the Supervisor log tab unless
you're chasing something that happened *before* capture logging was turned
on, or something the add-on logs at a level below what gets mirrored here.

**Where the log goes:** `/config/autoterm_debug/capture_<timestamp>.log` --
deliberately `/config`, not `/share`, so it shows up right where the
**File editor** add-on (and most other file-browser add-ons) already opens
by default, with no extra navigation or config changes needed. Reachable
from outside the add-on itself via:

- The **File editor** / **Studio Code Server** add-on -- it's right there
  in the default file tree, under `autoterm_debug/`.
- The **Samba share** add-on (if installed and configured to expose
  `config`) -- browse to `\\<home-assistant-ip>\config\autoterm_debug\`
  from your PC.
- SSH into the Home Assistant host, if you have that set up.

Toggling the switch off closes the current file cleanly (with an end
marker) -- toggling it back on starts a **new** file rather than appending,
so each capture session is its own file. A capture is capped at
`capture_log_max_mb` (default 20MB, configurable) -- past that it stops
writing (logged as a warning) rather than filling up storage; toggle it off
and on again to start a fresh file if you hit the cap mid-session.

The **Capture log file** and **Capture log size** sensors show the current
file name and size without needing to go find it first.

**If you send a capture back for further analysis:** the whole point of
this feature is to make that loop easy -- grab the file, note roughly what
you did and when (e.g. "turned on Debug mode at the start, pressed Start
preheat around the 2 minute mark"), and it can be diffed against the
already-decoded fields the same way the extended-frame work was done.

## Configuration

All the regular add-on's options, plus:

| Option | Meaning |
|---|---|
| `debug_mode_default` | Whether Debug mode starts on when the add-on (re)starts. Live-togglable from Home Assistant afterward -- this is just the boot default. |
| `debug_interval_seconds_default` | Initial value of the Debug probe interval number entity. |
| `capture_log_default` | Whether the capture log starts on when the add-on (re)starts. |
| `capture_log_max_mb` | Size cap per capture file, in MB. |

Debug mode, the probe interval, and the capture log toggle are all
live-controllable from Home Assistant (switches/number entities below) and
persisted to the add-on's `/data` volume -- the `_default` options above
only matter on a fresh install or if `/data` is cleared.

## What you get

Everything the regular add-on's device has -- including the **Prevent
freezing** switch/target (frost-protection safety net, starts the heater
regardless of the auto-thermostat's state or a prior manual Stop -- see
the [regular add-on's DOCS.md](../autoterm-addon/DOCS.md#prevent-freezing)
for the full explanation) -- plus:

- **Switch**: Debug mode, Capture raw traffic log
- **Number**: Debug probe interval (seconds)
- **Button**: Send debug handshake now
- **Binary sensor**: Extended telemetry active, Glow plug
- **Sensors** (all extended-frame fields, populated only while Extended
  telemetry active is on): Mode of operation (named, e.g. "High", "middle",
  "glow plug warming up"), Mode code (numeric mirror, see below), Running
  time (extended), Defined revolutions, Measured revolutions, Fuel pump
  frequency, Flame temperature, Liquid temperature (extended), Overheat
  sensor temperature, Board temperature, Supply voltage, Fault (extended,
  named), Fault code (extended, numeric mirror), Engine state, Relay
  state, Fan current, Capture log file, Capture log size
- **Sensor** (always available): State code (numeric mirror of `State`),
  Heater profile (shows the active selection, `(NOT TESTED)` for anything
  but Flow 5 -- see "Heater profile: other models" above)

`State`, `Mode of operation`, and `Fault (extended, named)` are declared as
`enum` sensors (a fixed `options` list of every possible value) rather than
plain text -- genuinely useful for HA's own UI (dropdown-style display),
**but does not make them exportable to Prometheus/VictoriaMetrics** as
originally hoped: confirmed against a real instance that HA's Prometheus
integration tracks an enum sensor's availability/last-updated/change-count,
but never exports its actual text value as a metric (unlike the climate
entity's `mode`/`action`, which do get a proper metric). `State code`,
`Mode code` (`state*10+substate`), and `Fault code (extended)` are the
numeric mirrors that actually export -- see `grafana/` in the main repo,
which name-maps them back to text using Grafana's own value mappings
instead of relying on HA's exporter for that.

Field formulas are the vendor's own (read from its plaintext `.pfl`
profile, not reverse-engineered from scratch) and cross-checked against a
real capture -- see `docs/PROTOCOL.md`. The fault-code name table is a
partial, lower-confidence addition -- only "no fault" (code 0) was actually
observed in the reference capture; the rest of the names come from the
vendor's own string table but haven't been confirmed against a real fault.

## Troubleshooting

Same as the regular add-on (see its DOCS.md) for MQTT/entity issues. In
addition:

- **Extended sensors stay blank**: Debug mode is probably off, or the
  handshake isn't getting a reply -- check the **Extended telemetry
  active** binary sensor and the add-on log for "DEBUG sent PUBR0
  handshake" lines.
- **Capture log switch is on but no file appears**: check the add-on log
  for a "capture log started" line and the exact path logged, and confirm
  you actually have a way to browse `/config` (File editor/Studio Code
  Server add-on installed, or SSH). It should appear as `autoterm_debug/`
  right in the default file tree -- no extra navigation needed.
