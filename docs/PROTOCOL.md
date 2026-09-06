# Autoterm 5D UART protocol notes

Reverse-engineered from passive capture and live testing against one real
Autoterm 5D diesel heater + comfort panel. Treat this as a strong working
model, not a vendor spec -- several parts are marked as unconfirmed below,
and other units/firmware revisions may differ.

## Physical layer

- 2400 baud, 8N1
- Two independent full-duplex UART wires between panel and heater (not a
  shared bus): one carries panel-driven traffic, the other heater-driven
  traffic. Confirmed content-wise, not assumed.
- A solid shared ground between any tap/proxy point and the heater/panel
  circuit is required -- a missing ground produced pure garbage on this
  setup before it was fixed.

## Frame format

```
AA | dev(1) | len(2, little-endian) | type(1) | payload(len bytes) | crc16(2, big-endian)
```

- Total frame length = 7 + len
- CRC is **CRC-16/MODBUS** (poly 0xA001 reflected, init 0xFFFF), computed
  over the entire frame **including the leading 0xAA**, transmitted
  most-significant-byte-first (the reverse of standard Modbus wire order).
  Identified by brute-forcing every common CRC-16 variant against captured
  frames -- this one was the only exact match.

## Device (sender) byte

| Byte | Role |
|---|---|
| `0x03` | **Panel/display.** Originates every start/stop handshake (it's the device with physical buttons) and reports a plain 1-byte cabin-temperature reading. |
| `0x04` | **Heater.** Reports the rich 18-byte status frame (state machine, fault code, coolant temp, timers) -- makes sense as the heater's own telemetry. |
| `0x00`, `0x02` | Secondary heater identities, used for short acks. |

**Important history:** for most of one capture session these two roles were
assumed backwards (0x03 = heater, 0x04 = panel), based on an unverified
assumption about which physical wire was tapped where. That assumption
produced a coherent-looking but wrong protocol model, and specifically
caused early command-injection attempts to send the right bytes to the
*wrong* device with the *wrong* sender byte -- two independent bugs
stacking to produce a clean "no reaction, no error" failure that looked
like a wiring fault. It was caught by noticing that the "heater's" 1-byte
report was cabin temperature (physically implausible for a device mounted
away from the cabin) while the "panel's" report carried internal telemetry
(fault codes, coolant temp -- implausible for a display to know
independently). If your own capture seems internally consistent but the
physical behavior doesn't match, re-question this assumption first.

## Message types seen

| Type | Direction | Payload | Meaning |
|---|---|---|---|
| `0x0f` | heater query (empty) -> panel reply (18 bytes) | see below | Main status poll, ~1/sec |
| `0x11` | panel report (1 byte) -> heater ack (empty) | cabin temp, °C | Cabin temperature reading |
| `0x01` | panel -> heater ack | 2 bytes | Start handshake (mode marker or duration -- see below) |
| `0x02` | panel -> heater ack | 2 bytes, big-endian u16 | Preheat duration in minutes (preheat mode only) |
| `0x03` | panel -> heater ack | empty | **Stop**, single exchange. Also reused as a *sustained, rapid-fire* (sub-second) heartbeat throughout the post-stop cooldown/fan-purge phase -- same type code, very different role by repetition rate. |
| `0x04` (dev `0x02`) | heater-side ack | empty | Generic ack following the start handshake |
| `0x04`, `0x06` (dev `0x03`/`0x04`, larger payload) | unprompted, both directions | 5 bytes each | **Unidentified.** Seen twice, ~7 minutes apart, unrelated to any button press. Payload looked plausibly date/time-like in one case (`0a 01 0d 03 01`) but this was never confirmed. Worth investigating with a longer capture. |

Every other frame type not listed here was never observed.

## Heater status frame (dev04, type0f, 18-byte payload)

Indices are 0-based into the payload (i.e. after the 5-byte header, before
the 2-byte CRC).

| Idx | Field | Notes |
|---|---|---|
| `[0]` | Main state | `0x00` idle, `0x02` running, `0x03` late-run, `0x04` cooldown/fan-purge, `0x05` final-shutdown-stage, then back to `0x00` |
| `[1]` | Sub-stage within `[0]` | Resets on every state change |
| `[2]` | **Fault code** | Plain decimal-as-integer (e.g. `0x4e` = 78 = "check fuel system"). `0` = no fault. Confirmed exact match against a real panel-displayed fault code. **Latches** until the next successful ignition -- the panel's own "clear fault" button does *not* reset this over the wire, confirmed by watching it stay non-zero for 5+ minutes after clearing on the display. |
| `[3]`, `[4]` | Coolant/water temperature | Jitter ±1 of each other; tracked the panel's own displayed water-temp readout (42→52°C during a preheat cycle) closely. Averaging the two is a reasonable smoothing. |
| `[6]` | Noisy, voltage-like | No clean state correlation found; likely raw ADC/supply voltage telemetry. |
| `[7]` | Slowly rising sensor | Climbs steadily during a burn; candidate exhaust/heat-exchanger temp. Not the setpoint (a hypothesis that was tested and falsified). |
| `[9]` | Elapsed minutes since ignition | Increments roughly once/minute, latches (does not reset) at stop. |
| `[11]` | Elapsed seconds since ignition | Increments ~1/sec, wraps at 256, freezes when combustion stops. |
| `[12]` | Burner-active flag | `0xff` while actively burning, `0x00` otherwise. |
| `[16]` | Separate elapsed/cooldown counter | Keeps incrementing into the cooldown phase after `[11]` has frozen. |

Target/setpoint temperature was **never found anywhere on the wire** in
either direction, across many capture hours and multiple modes. It appears
to live entirely in the panel and never gets transmitted -- consistent with
the panel doing its own thermostat comparison and only ever telling the
heater a plain start/stop (see "confirmed commands" below).

## Confirmed commands

All are injected impersonating the **panel** (sender byte `0x03`), sent out
whichever physical port's TX line actually reaches the real heater's RX pin
-- confirm this on your own wiring before trusting a static port assignment
(see the device-role warning above; getting it backwards produces silent
no-op, not an error).

**Start, preheat mode**, byte-for-byte reproducible:
```
type01, payload = 00 1e      (fixed marker, usually)
  ~1.5s later
type02, payload = <minutes, big-endian u16>   (the real duration)
```
Verified with 30, 70, and 120-minute preheat starts. The 120-minute case
broke the "fixed marker" pattern entirely -- `type01` carried the actual
duration directly (`00 78` = 120) with **no** `type02` at all. The encoding
isn't fully nailed down; test and watch the bus rather than assuming.

**Start, thermostat mode:**
```
type01, payload = 00 22      (fixed marker, in every case observed)
```
No `type02` ever follows in thermostat mode -- consistent with the panel
managing its own stop timing rather than giving the heater a duration.

**Stop:**
```
type03, empty payload
```
The best-confirmed command here -- derived from three independent real
manual stops in passive capture (never a timeout; each was a deliberate
button press), all showing the identical signature 1-2 seconds before the
state byte flips to cooldown. Verified live: a single injected `type03`
frame took the heater from actively running to a full clean stop-and-idle
cycle (~4 minutes) with zero fault, matching real button-press behavior
exactly.

## What's still open

- The `type04`/`type06` unprompted messages (5-byte payloads, ~7 min apart,
  unrelated to button presses).
- Why the preheat duration encoding shifts between a two-frame
  (marker+duration) and single-frame (direct duration) form.
- Whether there's a maximum/minimum duration, and how out-of-range values
  are rejected (never tested -- avoid testing extremes on a live fuel
  system without supervision).
- Fine timing: request/reply gaps are consistently ~30-65ms (panel
  responding to the heater's poll) and frame timestamps in this project are
  computed from wire time (bytes-since-start), not read()-completion time --
  see the monitor script's comments if reusing this for new timing-sensitive
  analysis.
