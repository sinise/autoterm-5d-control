# Autoterm Heater Debug add-on

Everything the regular [Autoterm Heater add-on](../autoterm-addon/DOCS.md)
does, plus tools for continuing the protocol reverse-engineering: optional
extended-telemetry probing across 19 vendor heater profiles, a full
raw-traffic capture log, and sensors for every field decoded so far. This
is **not** needed for normal day-to-day heater control -- install the
regular add-on for that. Install this one instead of it when you want the
extra data or are helping debug the protocol further, including on heater
models other than the Autoterm 5D / Flow 5 this project was originally
built against.

Full protocol derivation lives in `docs/PROTOCOL.md` in the
[main repo](https://github.com/sinise/autoterm-heater-control) -- read it,
especially "Extended diagnostic-mode telemetry", before enabling debug mode.

## Before you install

Same prerequisites as the regular add-on: this owns both UART ports
directly, so **do not** run it alongside `autoterm_web.py`/`autoterm_proxy.py`
or the regular Autoterm Heater add-on on the same ports -- only one process
can hold a serial port open. Install **either** the regular add-on **or**
this one, not both at once (they'll fail to start if you try -- the second
one to start won't be able to open the ports).

This add-on is safe to install and run with debug mode and capture logging
both left **off** (the default) -- in that mode it behaves exactly like the
regular add-on. The extra risk described below only applies once you
actually turn debug mode on.

## Wiring: connecting the Pi to the heater

Same wiring regardless of which add-on you install -- duplicated here
rather than linked, since cross-add-on links don't resolve inside Home
Assistant's own add-on documentation viewer.

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
after a replug (see `CONTEXT.md` in the main repo for why this matters and
how to check).

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

**If you're seeing "no communication" glitches on the panel or the heater
seeming to restart every 30-40 minutes with debug mode off**, that's not
this issue -- see the 2.1.0 entry in CHANGELOG.md. The same quiet-gap
protection above turned out to be missing from every other command this
add-on injects too (Start preheat/thermostat, Stop, Start pump, including
the automatic ones Auto thermostat/Prevent freezing send on their own),
which is a much more likely cause of periodic disruption with debug mode
off. Fixed in 2.1.0.

## Heater profile: other models

The extended telemetry frame's field formulas (byte offsets, state/mode
names, fault names) are model-specific. The **Heater profile** option
selects which model's formulas decode the frame -- 19 choices, extracted
from the vendor diagnostic tool's own per-model `Profiles/*.pfl` files
plus its `language.res` string table (plaintext data files read directly,
not a decompile of the tool itself), the same way `autoterm_flow_5`'s
fields were originally derived and cross-checked against a real capture.

**Only `autoterm_flow_5`, `binar_5s`, and `binar_5s_next` are confirmed
against real hardware** -- the latter two share the exact same internal
codename (`BINAR-5S`) as Flow 5 in the vendor's own `.pfl` files, meaning
byte-identical field data, not a separate guess. Every other option below
is read straight from the vendor tool's own data and has **never been
validated**: the byte offsets could be wrong, the field set could be
incomplete (some models expose 5-6 temperature-ish registers; only 4
slots are wired up here -- see "Known limitation" below), and it isn't
even confirmed that the extended-telemetry mechanism itself (the `PUBR0`
handshake, the `dev02`/`type01` frame) works the same way on that model,
or applies at all. The add-on logs a warning at startup, and a **Heater
profile** sensor shows the active selection with a `(NOT TESTED)` suffix,
for anything but those three.

| Option value | Vendor tool's display name | Internal codename | Status |
|---|---|---|---|
| `14tc_10_molex` | 14TC-10 MOLEX | 4TC-10 MOLEX | untested |
| `autoterm_air_2d` | AUTOTERM AIR 2D | PLANAR-2MK | untested |
| `autoterm_air_4d` | AUTOTERM AIR 4D | PLANAR-44MK | untested |
| `autoterm_air_8d` | AUTOTERM AIR 8D | PLANAR-8D | untested |
| `autoterm_air_9d` | AUTOTERM AIR 9D | PLANAR-9D | untested |
| `autoterm_flow_5` | AUTOTERM FLOW 5 | BINAR-5S | **tested** |
| `binar_5s_next` | BINAR-5S-NEXT | BINAR-5S | **tested** (byte-identical data to Flow 5, same internal codename) |
| `binar_5s` | BINAR-5S | BINAR-5S | **tested** (byte-identical data to Flow 5, same internal codename) |
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

## Thermostat keep-alive (experimental)

**Background:** the heater has been observed self-stopping (going idle on
its own) roughly every 30-40 minutes even with Auto thermostat or Prevent
freezing enabled -- confirmed from a real overnight capture that neither
of those two features ever sends a `stop` themselves in that window; the
heater goes idle on its own, and they only ever restart it afterwards.
It's also confirmed, both from the code and from that same capture, that
`start_thermostat()` never sends any duration/timeout to the heater --
only preheat mode does. Since a setpoint/duration for thermostat mode was
never found on the wire in either direction to begin with (see
docs/PROTOCOL.md), one untested theory is that the real physical panel
periodically re-affirms the "start thermostat" marker while running, and
this add-on never has -- so the heater may be timing out a stale
thermostat-mode session on its own.

**What the switch does:** while it's on, and the heater is confirmed
running (not idle) because *this add-on* put it into thermostat mode --
manually, via Auto thermostat, or via Prevent freezing -- it re-sends the
exact same 9-byte marker frame every 10 minutes. It never sends anything
new, and it deliberately does nothing after a preheat start, a pump-only
start, or a deliberate stop (even while the heater is still mid-cooldown,
not yet back to idle) -- see `StatusModel.note_start_mode()` if you want
the exact conditions.

This is genuinely experimental -- off by default, and untested against
real hardware over a full cycle at the time of writing. Turn it on, then
compare against a run with it off (or use a capture log across both) to
see whether it actually changes the self-stop interval.

## Bypass (disable all injection)

**What it does:** while this switch is on, the add-on stops sending
*anything* it wouldn't otherwise be asked to by the physical panel --
Start preheat/thermostat/Stop/Start pump (manual or automatic, including
Auto thermostat and Prevent freezing), the debug handshake, and the
thermostat keep-alive above are all suspended (each attempt is logged
instead of sent). The real panel keeps talking to the real heater exactly
as it always does -- this only stops the *add-on's own* commands, not the
passive relay.

**Why you'd use it:** to capture a clean baseline showing what the heater
actually does entirely on its own (or driven only by the physical panel),
with zero chance that anything this add-on injects is a contributing
factor -- useful when you're not yet sure whether a symptom (like the
30-40 minute self-stop above) is something this add-on is doing versus
something the heater/panel already do by themselves.

**Logging:** turning Bypass on starts a separate log file,
`/config/autoterm_debug/bypass_<timestamp>.log` -- same format as the
normal capture log, but its own file and its own on/off state, so a
bypass test is captured cleanly regardless of whether the regular
**Capture raw traffic log** switch happens to be on or off. The **Bypass
log file** and **Bypass log size** sensors show the current file without
needing to go find it. Turning Bypass off closes that file (with an end
marker); turning it on again later starts a new one.

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

- **Switch**: Debug mode, Capture raw traffic log, Thermostat keep-alive
  (experimental), Bypass (disable all injection)
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
  state, Fan current, Capture log file, Capture log size, Bypass log file,
  Bypass log size
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
- **Panel briefly shows "no communication", or the heater seems to restart
  on its own every 30-40 minutes -- including with debug mode off**: fixed
  in 2.1.0, see "Debug mode: extended telemetry probing" above and
  CHANGELOG.md. Not specific to `PUBR0` -- every injected command
  (including Auto thermostat/Prevent freezing's automatic ones) had the
  same collision risk.
