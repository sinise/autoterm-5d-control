#!/usr/bin/env python3
"""
Autoterm 5D inline proxy + command injection + web dashboard.

Combines the transparent passthrough proxy with a local web UI that can
inject start/stop commands toward the heater, impersonating the panel.
Runs as ONE process because the relay threads hold the only open handles on
the serial ports -- a separate process could not safely write to them too.

Device roles (corrected after a day of having them backwards): dev03 is the
comfort panel/display -- it originates every start/stop handshake (it's the
one with physical buttons) and reports a plain cabin-temperature reading.
dev04 is the heater -- it reports the rich status frame (state machine,
fault code, coolant temp, timers), which makes far more sense as the
heater's own telemetry than as something a display would independently
know. The heater also uses two secondary identities, dev00 and dev02, for
short acks. Getting this backwards for most of a day is *why* early
injection attempts sent nothing anywhere near the real heater: commands
were built with the wrong sender byte AND (because of an unrelated port
mixup during rewiring) written out the port wired to the panel, not the
heater.

Confirmed encodings (from hours of passive capture + repeated live tests):

    Start handshake, originated by the panel(0x03), injected out the port
    wired to the heater:
        type01 payload = 00 1e   -- fixed marker for PREHEAT mode (usually)
        type01 payload = 00 22   -- fixed marker for THERMOSTAT mode (usually)
        type02 payload = <minutes, big-endian u16>   -- PREHEAT ONLY, real duration

    The heater acks each with a matching type01/type02 of its own (and a
    separate dev00/dev02 ack) -- that's its response, not something we send.

    One real preheat start (~120 min) broke the "fixed marker" pattern
    entirely: type01 carried the actual duration directly (00 78 = 120) with
    no type02 at all. So the encoding isn't fully nailed down -- treat these
    as our best evidence, not a certainty, and watch the bus when testing.

    THERMOSTAT mode never sends type02: the panel manages its own stop timing
    and the wire protocol carries no target temperature at all -- that lives
    panel-side only, confirmed by its total absence from every captured frame.

    Stop, originated by the panel(0x03), injected out the port wired to the
    heater:
        type03, empty payload

    Confirmed from three independent manual stops in the passive capture: a
    single type03/type03 exchange (never seen during normal idle/running
    polling) appears 1-2s before the state byte flips to cooldown, every
    time. Distinct from the *sustained, rapid-fire* type03 heartbeat that
    runs throughout the cooldown phase afterward -- same type code, but one
    lone exchange here versus hundreds of repeats there.

    Both start and stop were tried live with the (wrong) dev04 sender byte
    out the (wrong) port and produced zero reaction -- consistent with, not
    proof against, the corrected version below. Still unverified live as of
    this fix -- watch the bus closely on first use. The physical panel keeps
    working normally throughout (passthrough is never disabled), so it
    remains available as a fallback.
"""

import argparse
import http.server
import json
import os
import socketserver
import sys
import threading
import time
from collections import deque
from datetime import datetime

import serial

from autoterm_monitor import Framer, KNOWN_DEV, crc_bytes

STATE_NAMES = {
    0x00: "idle",
    0x02: "running",
    0x03: "late-run",
    0x04: "cooldown",
    0x05: "final-shutdown",
}


def build_frame(dev, type_, payload=b""):
    raw = bytes([0xAA, dev]) + len(payload).to_bytes(2, "little") + bytes([type_]) + payload
    return raw + crc_bytes(raw)


def decode_status_payload(payload):
    """Heater's (dev04) 18-byte type0f payload -> named fields, per the protocol map."""
    if len(payload) < 18:
        return {}
    return {
        "state_raw": payload[0],
        "state": STATE_NAMES.get(payload[0], f"unknown(0x{payload[0]:02x})"),
        "substate": payload[1],
        "fault": payload[2],
        "coolant_temp": (payload[3] + payload[4]) / 2,
        "elapsed_min": payload[9],
        "elapsed_sec": payload[11],
        "burner_active": payload[12] == 0xFF,
    }


class StatusModel:
    def __init__(self):
        self.lock = threading.Lock()
        self.status = {}
        self.status_ts = None
        self.cabin_temp = None
        self.cabin_temp_ts = None
        self.last_frame_ts = None
        self.log = deque(maxlen=200)
        self.last_command = None

    def note_frame(self, ts, direction, raw):
        dev = raw[1]
        type_ = raw[4] if len(raw) > 4 else None
        payload = raw[5:-2]
        with self.lock:
            self.last_frame_ts = ts
            if dev == 0x04 and type_ == 0x0F and len(payload) == 18:
                self.status = decode_status_payload(payload)
                self.status_ts = ts
            elif dev == 0x03 and type_ == 0x11 and len(payload) == 1:
                self.cabin_temp = payload[0]
                self.cabin_temp_ts = ts
            devname = KNOWN_DEV.get(dev, f"0x{dev:02x}")
            self.log.append(
                f"{_iso(ts)}  {direction}  dev={devname} type={type_:02x}  {payload.hex(' ')}"
            )

    def note_command(self, text):
        with self.lock:
            self.last_command = {"text": text, "ts": time.time()}
            self.log.append(f"{_iso(time.time())}  >>> INJECTED: {text}")

    def get_last_frame_ts(self):
        with self.lock:
            return self.last_frame_ts

    def snapshot(self):
        with self.lock:
            now = time.time()
            return {
                "status": self.status,
                "status_age": None if self.status_ts is None else now - self.status_ts,
                "cabin_temp": self.cabin_temp,
                "cabin_temp_age": None if self.cabin_temp_ts is None else now - self.cabin_temp_ts,
                "last_command": self.last_command,
                "log": list(self.log)[-40:],
            }


def _iso(ts):
    return datetime.fromtimestamp(ts).strftime("%H:%M:%S.%f")[:-3]


class Relay(threading.Thread):
    def __init__(self, name, src, dst, dst_lock, model, log_fh, stop_evt):
        super().__init__(daemon=True, name=name)
        self.label = name
        self.src = src
        self.dst = dst
        self.dst_lock = dst_lock
        self.model = model
        self.log_fh = log_fh
        self.stop_evt = stop_evt
        self.framer = Framer()
        self.byte_count = 0
        self.error = None

    def run(self):
        while not self.stop_evt.is_set():
            try:
                data = self.src.read(1)
                if not data:
                    continue
                data += self.src.read(self.src.in_waiting)
            except serial.SerialException as e:
                self.error = str(e)
                self.stop_evt.set()
                return

            ts = time.time()
            self.byte_count += len(data)

            try:
                with self.dst_lock:
                    self.dst.write(data)
            except serial.SerialException as e:
                self.error = str(e)
                self.stop_evt.set()
                return

            for ev in self.framer.feed(data):
                if ev[0] != "frame":
                    continue
                raw, crc_ok = ev[1], ev[2]
                if crc_ok:
                    self.model.note_frame(ts, self.label, raw)
                line = f"{_iso(ts)}  {self.label}  {'OK' if crc_ok else 'BAD'}  {raw.hex(' ')}\n"
                self.log_fh.write(line)


# Confirmed on real hardware (see autoterm-debug-addon's DebugSender /
# CHANGELOG 1.5.0/2.1.0): a frame written to heater_ser while the panel's
# own query/reply exchange is mid-flight can corrupt that exchange. Applies
# to anything Commander injects, including AutoThermostat's automatic
# stop/start-thermostat calls, not just a manual button press.
QUIET_GAP = 0.25
MAX_EXTRA_WAIT = 2.0


def wait_for_quiet_bus(model, stop_evt):
    deadline = time.time() + MAX_EXTRA_WAIT
    while time.time() < deadline and not stop_evt.is_set():
        last = model.get_last_frame_ts()
        if last is None or time.time() - last >= QUIET_GAP:
            return
        time.sleep(0.05)
    # Gave up waiting for a quiet gap -- send anyway rather than delaying
    # indefinitely if the bus is unusually busy.


class Commander:
    """Builds and injects command frames toward the heater, impersonating the panel."""

    def __init__(self, heater_ser, heater_lock, model, log_fh, stop_evt):
        self.heater_ser = heater_ser
        self.heater_lock = heater_lock
        self.model = model
        self.log_fh = log_fh
        self.stop_evt = stop_evt

    def _send(self, dev, type_, payload=b""):
        wait_for_quiet_bus(self.model, self.stop_evt)
        frame = build_frame(dev, type_, payload)
        try:
            with self.heater_lock:
                n = self.heater_ser.write(frame)
            self.log_fh.write(f"{_iso(time.time())}  INJECT  sent {n}B  {frame.hex(' ')}\n")
            self.model.note_command(f"sent {frame.hex(' ')}")
        except Exception as e:
            self.log_fh.write(f"{_iso(time.time())}  INJECT  FAILED  {frame.hex(' ')}  error={e!r}\n")
            self.model.note_command(f"FAILED to send {frame.hex(' ')}: {e!r}")
            raise

    # Sender byte 0x03 = panel/display -- confirmed to be the device that
    # originates every start/stop handshake. We impersonate it here. The
    # dev00/dev02 acks seen in captures are the HEATER's own replies, not
    # something to fabricate ourselves.

    def start_preheat(self, minutes):
        minutes = max(0, min(int(minutes), 600))
        self.log_fh.write(f"{_iso(time.time())}  INJECT  requested: start preheat {minutes}min\n")
        self._send(0x03, 0x01, bytes([0x00, 0x1E]))
        time.sleep(1.5)
        self._send(0x03, 0x02, minutes.to_bytes(2, "big"))

    def start_thermostat(self):
        self.log_fh.write(f"{_iso(time.time())}  INJECT  requested: start thermostat\n")
        self._send(0x03, 0x01, bytes([0x00, 0x22]))

    def stop(self):
        self.log_fh.write(f"{_iso(time.time())}  INJECT  requested: stop\n")
        self._send(0x03, 0x03)


class AutoThermostat(threading.Thread):
    """Software hysteresis loop: stop at target+1, start (thermostat mode) at
    target-1. Runs independently of the manual buttons -- it only acts when
    enabled, and only on live, fresh cabin-temp readings, never on stale or
    missing data. A minimum interval between its own actions keeps it from
    thrashing if the reading oscillates near a boundary."""

    HYSTERESIS = 1.0
    MIN_ACTION_INTERVAL = 90.0
    MAX_READING_AGE = 10.0
    POLL_INTERVAL = 3.0

    def __init__(self, model, commander, log_fh, stop_evt):
        super().__init__(daemon=True, name="auto-thermostat")
        self.model = model
        self.commander = commander
        self.log_fh = log_fh
        self.stop_evt = stop_evt
        self.lock = threading.Lock()
        self.enabled = False
        self.target = 20.0
        self.last_action_ts = 0.0
        self.last_note = None
        # Cooldown after a real stop runs for minutes with state != "idle" the
        # whole time -- without this latch we'd re-send stop every
        # MIN_ACTION_INTERVAL for the entire cooldown. Cleared once idle.
        self.stop_pending = False

    def configure(self, enabled=None, target=None):
        with self.lock:
            if enabled is not None:
                self.enabled = bool(enabled)
            if target is not None:
                self.target = float(target)

    def snapshot(self):
        with self.lock:
            return {"enabled": self.enabled, "target": self.target, "last_note": self.last_note}

    def run(self):
        while not self.stop_evt.wait(self.POLL_INTERVAL):
            with self.lock:
                enabled, target = self.enabled, self.target
            if not enabled:
                continue
            now = time.time()
            if now - self.last_action_ts < self.MIN_ACTION_INTERVAL:
                continue

            snap = self.model.snapshot()
            cabin, age = snap["cabin_temp"], snap["cabin_temp_age"]
            state = snap["status"].get("state")
            if cabin is None or age is None or age > self.MAX_READING_AGE or state is None:
                continue

            if state == "idle":
                self.stop_pending = False

            try:
                if cabin >= target + self.HYSTERESIS and state != "idle" and not self.stop_pending:
                    note = f"cabin {cabin} >= {target + self.HYSTERESIS} -> stop"
                    self.log_fh.write(f"{_iso(now)}  AUTO  {note}\n")
                    self.commander.stop()
                    self.last_action_ts = now
                    self.stop_pending = True
                    with self.lock:
                        self.last_note = note
                elif cabin <= target - self.HYSTERESIS and state == "idle":
                    note = f"cabin {cabin} <= {target - self.HYSTERESIS} -> start thermostat"
                    self.log_fh.write(f"{_iso(now)}  AUTO  {note}\n")
                    self.commander.start_thermostat()
                    self.last_action_ts = now
                    with self.lock:
                        self.last_note = note
            except Exception as e:
                self.log_fh.write(f"{_iso(now)}  AUTO  action failed: {e!r}\n")


PAGE = """<!doctype html>
<html><head><meta charset="utf-8">
<title>Autoterm control</title>
<style>
body { font-family: system-ui, sans-serif; background:#111; color:#eee; margin:0; padding:20px; }
h1 { font-size:1.3em; margin:0 0 16px; }
.cards { display:flex; flex-wrap:wrap; gap:12px; margin-bottom:20px; }
.card { background:#1c1c1c; border:1px solid #333; border-radius:8px; padding:12px 16px; min-width:140px; }
.card .label { font-size:0.75em; color:#999; text-transform:uppercase; letter-spacing:0.05em; }
.card .value { font-size:1.6em; font-weight:600; margin-top:4px; }
.state-idle { color:#888; } .state-running { color:#4caf50; }
.state-cooldown, .state-late-run { color:#ff9800; } .state-final-shutdown { color:#ff9800; }
.fault { color:#f44336; }
.stale { color:#f44336; font-size:0.7em; margin-left:6px; }
.controls { display:flex; gap:10px; align-items:center; margin-bottom:20px; flex-wrap:wrap; }
button { background:#2a2a2a; color:#eee; border:1px solid #444; border-radius:6px; padding:10px 16px; cursor:pointer; font-size:0.95em; }
button:hover { background:#3a3a3a; }
button.stop { border-color:#f44336; color:#f44336; }
input[type=number] { width:70px; background:#1c1c1c; color:#eee; border:1px solid #444; border-radius:6px; padding:8px; }
#log { background:#0a0a0a; border:1px solid #333; border-radius:8px; padding:10px; height:300px; overflow-y:auto; font-family:monospace; font-size:0.8em; white-space:pre-wrap; }
.warn { color:#ff9800; font-size:0.85em; margin-bottom:16px; }
</style></head>
<body>
<h1>Autoterm 5D control</h1>
<div class="warn">This is the first live test of injected commands -- watch the log below when you use any button. The physical panel keeps working normally the whole time as a fallback.</div>
<div class="cards" id="cards"></div>
<div class="controls">
  <button onclick="startPreheat()">Start preheat</button>
  <input type="number" id="minutes" value="30" min="1" max="600"> min
  <button onclick="startThermostat()">Start thermostat</button>
  <button class="stop" onclick="stopHeater()">Stop</button>
</div>
<div class="controls">
  <label><input type="checkbox" id="autoEnabled" onchange="setAuto()"> Auto thermostat</label>
  <span>target</span>
  <input type="number" id="autoTarget" value="20" min="5" max="35" step="0.5" onchange="setAuto()"> &deg;C
  <span id="autoNote" style="color:#999; font-size:0.85em;"></span>
</div>
<div id="log"></div>
<script>
async function refresh() {
  const r = await fetch('/api/status');
  const d = await r.json();
  const s = d.status || {};
  const stale = (age) => age === null || age > 5;
  const ageTag = (age) => stale(age) ? '<span class="stale">STALE</span>' : '';
  const stateClass = 'state-' + (s.state || 'idle');
  document.getElementById('cards').innerHTML = `
    <div class="card"><div class="label">State ${ageTag(d.status_age)}</div><div class="value ${stateClass}">${s.state ?? '-'}</div></div>
    <div class="card"><div class="label">Fault</div><div class="value ${s.fault ? 'fault' : ''}">${s.fault ? s.fault : 'none'}</div></div>
    <div class="card"><div class="label">Cabin temp ${ageTag(d.cabin_temp_age)}</div><div class="value">${d.cabin_temp ?? '-'}&deg;C</div></div>
    <div class="card"><div class="label">Coolant temp</div><div class="value">${s.coolant_temp ?? '-'}&deg;C</div></div>
    <div class="card"><div class="label">Elapsed</div><div class="value">${s.elapsed_min ?? 0}m ${s.elapsed_sec ?? 0}s</div></div>
    <div class="card"><div class="label">Burner</div><div class="value">${s.burner_active ? 'ON' : 'off'}</div></div>
  `;
  document.getElementById('log').textContent = (d.log || []).join('\\n');
  document.getElementById('log').scrollTop = 1e9;

  const t = d.thermostat || {};
  if (!autoEditing) {
    document.getElementById('autoEnabled').checked = !!t.enabled;
    document.getElementById('autoTarget').value = t.target ?? 20;
  }
  document.getElementById('autoNote').textContent = t.enabled ? (t.last_note || 'watching...') : '';
}
async function post(path, body) {
  const r = await fetch(path, {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(body||{})});
  if (!r.ok) {
    const d = await r.json().catch(() => ({error: 'unknown error'}));
    alert('Command failed: ' + (d.error || r.status));
  }
  refresh();
}
function startPreheat() { post('/api/start_preheat', {minutes: parseInt(document.getElementById('minutes').value)}); }
function startThermostat() { post('/api/start_thermostat'); }
function stopHeater() { if (confirm('Send stop command?')) post('/api/stop'); }
let autoEditing = false;
function setAuto() {
  autoEditing = true;
  const enabled = document.getElementById('autoEnabled').checked;
  const target = parseFloat(document.getElementById('autoTarget').value);
  post('/api/thermostat', {enabled, target}).then(() => { autoEditing = false; });
}
setInterval(refresh, 1000);
refresh();
</script>
</body></html>"""


def make_handler(model, commander, auto):
    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            pass

        def do_GET(self):
            if self.path == "/":
                body = PAGE.encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif self.path == "/api/status":
                snap = model.snapshot()
                snap["thermostat"] = auto.snapshot()
                body = json.dumps(snap).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                self.send_response(404)
                self.end_headers()

        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length) if length else b"{}"
            try:
                data = json.loads(raw or b"{}")
            except json.JSONDecodeError:
                data = {}

            try:
                if self.path == "/api/start_preheat":
                    commander.start_preheat(data.get("minutes", 30))
                elif self.path == "/api/start_thermostat":
                    commander.start_thermostat()
                elif self.path == "/api/stop":
                    commander.stop()
                elif self.path == "/api/thermostat":
                    auto.configure(enabled=data.get("enabled"), target=data.get("target"))
                else:
                    self.send_response(404)
                    self.end_headers()
                    return
            except Exception as e:
                body = json.dumps({"ok": False, "error": repr(e)}).encode()
                self.send_response(500)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return

            body = b'{"ok": true}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    return Handler


class ThreadingHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--panel", default="/dev/ttyUSB1", help="port wired to the panel")
    ap.add_argument("--heater", default="/dev/ttyUSB3", help="port wired to the heater")
    ap.add_argument("--baud", type=int, default=2400)
    ap.add_argument("--http-port-start", type=int, default=8083)
    ap.add_argument("--http-port-end", type=int, default=8090)
    ap.add_argument("--logdir", default=os.path.expanduser("~/autoterm_logs"))
    args = ap.parse_args()

    os.makedirs(args.logdir, exist_ok=True)
    logpath = os.path.join(args.logdir, f"web_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log")

    try:
        panel_ser = serial.Serial(args.panel, args.baud, timeout=0.05)
        heater_ser = serial.Serial(args.heater, args.baud, timeout=0.05)
    except serial.SerialException as e:
        print(f"ERROR opening serial ports: {e}", file=sys.stderr)
        return 1

    model = StatusModel()
    stop_evt = threading.Event()
    panel_lock = threading.Lock()
    heater_lock = threading.Lock()
    log_fh = open(logpath, "a", buffering=1)

    panel_to_heater = Relay("PANEL->HEATER", panel_ser, heater_ser, heater_lock, model, log_fh, stop_evt)
    heater_to_panel = Relay("HEATER->PANEL", heater_ser, panel_ser, panel_lock, model, log_fh, stop_evt)
    commander = Commander(heater_ser, heater_lock, model, log_fh, stop_evt)
    auto = AutoThermostat(model, commander, log_fh, stop_evt)

    panel_to_heater.start()
    heater_to_panel.start()
    auto.start()

    httpd = None
    for port in range(args.http_port_start, args.http_port_end + 1):
        try:
            httpd = ThreadingHTTPServer(("0.0.0.0", port), make_handler(model, commander, auto))
            break
        except OSError:
            continue
    if httpd is None:
        print(f"ERROR: no free port in {args.http_port_start}-{args.http_port_end}", file=sys.stderr)
        stop_evt.set()
        panel_ser.close()
        heater_ser.close()
        return 1

    port_file = os.path.join(args.logdir, "web_port.txt")
    with open(port_file, "w") as f:
        f.write(str(httpd.server_address[1]))

    print(f"Autoterm web control on http://0.0.0.0:{httpd.server_address[1]}/")
    print(f"panel={args.panel}  heater={args.heater}  baud={args.baud}")
    print(f"log: {logpath}")
    print(f"port written to: {port_file}")
    print("Ctrl-C to stop.")

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop_evt.set()
        httpd.shutdown()
        panel_to_heater.join(timeout=1.0)
        heater_to_panel.join(timeout=1.0)
        auto.join(timeout=1.0)
        log_fh.close()
        panel_ser.close()
        heater_ser.close()
        print("stopped")

    return 0


if __name__ == "__main__":
    sys.exit(main())
