#!/usr/bin/env python3
"""
Autoterm 5D <-> Home Assistant bridge, DEBUG variant (Supervisor add-on).

Everything autoterm-addon/autoterm_addon.py does (transparent passthrough
relay, command injection impersonating the panel, software auto-thermostat,
MQTT + Home Assistant MQTT discovery) -- NOT re-derived here, copied and
extended, since each Supervisor add-on is a separate Docker build and can
only see files inside its own directory.

Additional, debug-only features:

  - Optional periodic "PUBR0" handshake toward the heater (the vendor
    diagnostic tool's own literal command), which unlocks a much richer
    58-byte extended telemetry frame (dev02, type01). See docs/PROTOCOL.md,
    "Extended diagnostic-mode telemetry" -- field formulas below are the
    vendor's own (read from its plaintext .pfl profile), cross-checked
    against a real capture, NOT guessed.
  - UNCONFIRMED whether sending PUBR0 while the physical panel is also on
    the bus is safe -- it's OFF by default, and DOCS.md says to only ever
    enable it while watching the physical panel.
  - A toggleable raw traffic capture: every parsed frame and every stray
    (unparsed) byte, tagged with who sent it (display, heater, or this
    add-on itself), written to a human-readable log under /config so it's
    reachable from outside the add-on (Samba / File editor / SSH) without
    needing a dashboard of its own.
  - Sensors for every known extended-frame field, plus the existing base
    sensors.

Device roles, frame layout, and confirmed commands: dev03 = panel
(originates start/stop, reports cabin temp), dev04 = heater (rich 18-byte
status frame). Commands are injected as dev03 (panel) out the port wired to
the heater. See docs/PROTOCOL.md in the main repo.
"""

import glob
import json
import logging
import os
import queue
import signal
import sys
import threading
import time
from datetime import datetime

import paho.mqtt.client as mqtt
import serial

from autoterm_protocol import Framer, KNOWN_DEV, crc_bytes

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("autoterm-debug")

NODE_ID = "autoterm5d"
DISCOVERY_PREFIX = "homeassistant"
STATE_TOPIC = f"autoterm/{NODE_ID}/state"
AVAILABILITY_TOPIC = f"autoterm/{NODE_ID}/availability"
CMD_PREFIX = f"autoterm/{NODE_ID}/cmd"
STATE_FILE = "/data/autoterm_state.json"
STALE_AFTER = 5.0
EXT_STALE_AFTER = 5.0

STATE_NAMES = {
    0x00: "idle",
    0x02: "running",
    0x03: "late-run",
    0x04: "cooldown",
    0x05: "final-shutdown",
}

DEVICE_INFO = {
    "identifiers": [NODE_ID],
    "name": "Autoterm 5D Heater",
    "manufacturer": "Autoterm",
    "model": "5D",
}

# --------------------------------------------------------------------------
# Extended telemetry (dev02, type01, 58-byte payload) -- vendor-defined
# field formulas, read from Profiles/AUTOTERM FLOW 5.pfl in the vendor's
# own diagnostic tool (plaintext file, not a decompile), cross-checked
# against a real capture. See docs/PROTOCOL.md for the full derivation.
# --------------------------------------------------------------------------

# index = state*10 + substate, 0-based into this table of 44 vendor strings
# (state 0-4 x substate 0-9; unused combinations are "unknown").
EXT_MODE_TABLE = [
    "unknown", "waiting for a command", "cooling the flame sensor", "air blowing", "fuel pumping",
    "unknown", "unknown", "unknown", "unknown", "unknown",
    "waiting for temperature reduction", "locked", "unknown", "unknown", "unknown",
    "unknown", "unknown", "unknown", "unknown", "unknown",
    "cooling", "glow plug warming up", "preparation for ignition", "Ignition 1", "Ignition 2",
    "blowing", "combustion chamber heating", "blowing", "unknown", "unknown",
    "low", "unknown", "High", "unknown", "blowing",
    "waiting", "blowing", "pump only", "middle", "unknown",
    "blowing", "blowing", "blowing", "shutting down",
]

# Partial, NOT independently cross-validated against a real fault -- read
# from the vendor tool's own string table (language.res), matched to fault
# codes via its .pfl entries. Only code 0 ("no fault") was actually
# observed in the reference capture. Treat text as a strong hint, not gospel.
EXT_FAULT_NAMES = {
    0: "No faults", 1: "Overheat", 2: "Possible overheat", 3: "Overheat",
    4: "Liquid temperature sensor", 5: "Flame temperature sensor",
    6: "Board temperature sensor", 9: "Malfunction of a glow plug",
    10: "Turnover mismatch", 12: "Increased supply voltage", 13: "No ignition",
    14: "Faulty water pump", 15: "Low voltage", 16: "Blowing time exceeded",
    17: "Faulty fuel pump", 20: "No connection", 22: "Faulty fuel pump",
    24: "Temperature sensor off-scale", 25: "Temperature growing too fast",
    26: "Fan overloaded", 27: "Fan. No rotation", 28: "Fan. Autorotation",
    29: "Flame breaks too often", 30: "No connection", 37: "Overheat locking",
    78: "Flame break during running",
}


def extended_mode_name(state, substate):
    idx = state * 10 + substate
    if 0 <= idx < len(EXT_MODE_TABLE):
        return EXT_MODE_TABLE[idx]
    return f"unknown({state}.{substate})"


def decode_extended_payload(payload):
    """dev02, type01, 58-byte payload -> named fields. See module docstring."""
    if len(payload) < 56:
        return {}
    p = payload
    state, substate = p[0], p[1]
    fault = p[36]
    return {
        "ext_state_raw": state,
        "ext_substate_raw": substate,
        "ext_mode_name": extended_mode_name(state, substate),
        "ext_running_time_s": p[2] * 65536 + p[3] * 256 + p[4],
        "ext_defined_rev": p[11],
        "ext_measured_rev": p[12],
        "ext_glow_plug": p[13] > 0,
        "ext_fuel_pump_hz": round(p[15] / 10, 1),
        "ext_flame_temp_c": (p[17] * 256 + p[18]) - 273,
        "ext_liquid_temp_c": p[19],
        "ext_overheat_temp_c": p[20],
        "ext_board_temp_c": p[21],
        "ext_voltage": round((p[22] * 256 + p[23]) / 10, 1),
        "ext_fault_code": fault,
        "ext_fault_name": EXT_FAULT_NAMES.get(fault, f"unknown({fault})"),
        "ext_engine_state": p[51],
        "ext_relay_state": p[52],
        "ext_fan_current_ma": p[54] * 256 + p[55],
    }


def build_frame(dev, type_, payload=b""):
    raw = bytes([0xAA, dev]) + len(payload).to_bytes(2, "little") + bytes([type_]) + payload
    return raw + crc_bytes(raw)


def build_pubr0_frame():
    """The vendor diagnostic tool's literal handshake that unlocks the
    extended telemetry frame. Not dev/len/type/payload framing like the
    rest of the protocol -- a fixed 13-byte body ("PUBR0" + padding) plus
    the same CRC-16/MODBUS used everywhere else, but transmitted
    **least-significant-byte first** -- the reverse of every other frame in
    this protocol (crc_bytes() returns MSB-first). Confirmed against the
    real captured handshake, which ended `0f b0`, not `b0 0f`. See
    docs/PROTOCOL.md."""
    body = bytes([0xAA]) + b"PUBR0" + bytes([0x00, 0x00, 0x00, 0xFF, 0xFF, 0xFF, 0xFF])
    crc = crc_bytes(body)
    return body + bytes([crc[1], crc[0]])


def decode_status_payload(payload):
    """Heater's (dev04) 18-byte type0f payload -> named fields."""
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


def _iso(ts):
    return datetime.fromtimestamp(ts).strftime("%H:%M:%S.%f")[:-3]


# --------------------------------------------------------------------------
# Port autodiscovery (identical to the base add-on -- see its comments)
# --------------------------------------------------------------------------

HEATER_PROBE_ATTEMPTS = 5
HEATER_PROBE_TIMEOUT = 1.0


def list_candidate_ports():
    return sorted(set(glob.glob("/dev/ttyUSB*") + glob.glob("/dev/ttyACM*")))


def _discover_panel_port(candidates, baud, timeout):
    listeners = {}
    framers = {}
    for path in candidates:
        try:
            listeners[path] = serial.Serial(path, baud, timeout=0.1)
            framers[path] = Framer()
        except serial.SerialException as e:
            log.warning("discovery: could not open %s: %r", path, e)
    try:
        deadline = time.time() + timeout
        while time.time() < deadline:
            for path, ser in listeners.items():
                try:
                    data = ser.read(ser.in_waiting or 1)
                except serial.SerialException:
                    continue
                if not data:
                    continue
                for ev in framers[path].feed(data):
                    if ev[0] == "frame" and ev[2] and ev[1][1] == 0x03:
                        return path
        return None
    finally:
        for ser in listeners.values():
            ser.close()


def _discover_heater_port(candidates, baud):
    poll = build_frame(0x03, 0x0F, b"")
    for path in candidates:
        try:
            ser = serial.Serial(path, baud, timeout=0.1)
        except serial.SerialException as e:
            log.warning("discovery: could not open %s: %r", path, e)
            continue
        try:
            framer = Framer()
            for _ in range(HEATER_PROBE_ATTEMPTS):
                ser.write(poll)
                deadline = time.time() + HEATER_PROBE_TIMEOUT
                while time.time() < deadline:
                    data = ser.read(ser.in_waiting or 1)
                    if not data:
                        continue
                    for ev in framer.feed(data):
                        if ev[0] == "frame" and ev[2] and ev[1][1] == 0x04:
                            return path
        finally:
            ser.close()
    return None


def discover_ports(baud, panel_timeout=8.0):
    """Returns (panel_port, heater_port) or None. Never sends a start/stop
    command -- only the empty type0f status query, which is non-actuating."""
    candidates = list_candidate_ports()
    if len(candidates) < 2:
        log.error("discovery: need at least 2 serial candidates, found %s", candidates)
        return None

    log.info("discovery: listening for the panel on %s (up to %.0fs)", candidates, panel_timeout)
    panel_port = _discover_panel_port(candidates, baud, panel_timeout)
    if panel_port is None:
        log.error(
            "discovery: no dev03 (panel) frames seen on any candidate within %.0fs "
            "-- is the panel powered and actually wired to one of these ports?",
            panel_timeout,
        )
        return None
    log.info("discovery: panel found on %s -- probing remaining ports for the heater", panel_port)

    remaining = [p for p in candidates if p != panel_port]
    heater_port = _discover_heater_port(remaining, baud)
    if heater_port is None:
        log.error("discovery: no dev04 (heater) reply seen on any of %s", remaining)
        return None

    log.info("discovery: panel=%s heater=%s", panel_port, heater_port)
    return panel_port, heater_port


def load_persisted():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_persisted(data):
    try:
        with open(STATE_FILE, "w") as f:
            json.dump(data, f)
    except OSError as e:
        log.warning("could not persist state: %r", e)


# --------------------------------------------------------------------------
# Raw traffic capture -- toggleable, downloadable log of every message and
# who sent it. Written under /config (mapped config:rw in config.yaml) so
# it's reachable via Samba / File editor / SSH without this add-on needing
# a web server of its own.
# --------------------------------------------------------------------------

class CaptureLog:
    def __init__(self, directory, max_mb):
        self.directory = directory
        self.max_bytes = max_mb * 1024 * 1024
        self.lock = threading.Lock()
        self.fh = None
        self.path = None
        self.bytes_written = 0
        self.enabled = False
        self.capped = False

    def start(self):
        with self.lock:
            if self.fh is not None:
                return
            os.makedirs(self.directory, exist_ok=True)
            name = f"capture_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
            self.path = os.path.join(self.directory, name)
            self.fh = open(self.path, "a", buffering=1)
            self.bytes_written = 0
            self.capped = False
            self.enabled = True
            self.fh.write(f"# === autoterm debug capture start {datetime.now().isoformat()} ===\n")
            self.fh.write("# columns: time  sender  status  dev  type  len  full-frame-hex\n")
        log.info("capture log started: %s", self.path)

    def stop(self):
        with self.lock:
            self.enabled = False
            if self.fh is not None:
                self.fh.write(f"# === capture end {datetime.now().isoformat()} ===\n")
                self.fh.close()
                self.fh = None
        log.info("capture log stopped")

    def frame(self, ts, sender, raw, crc_ok):
        with self.lock:
            if not self.enabled or self.fh is None or self.capped:
                return
            dev = raw[1] if len(raw) > 1 else 0
            type_ = raw[4] if len(raw) > 4 else 0
            length = max(0, len(raw) - 7)
            status = "OK " if crc_ok else "BAD"
            line = (f"{_iso(ts)}  {sender:<11s}  {status}  "
                    f"dev={dev:02x} type={type_:02x} len={length:<3d}  {raw.hex(' ')}\n")
            self._write(line)

    def stray(self, ts, sender, data):
        with self.lock:
            if not self.enabled or self.fh is None or self.capped:
                return
            self._write(f"{_iso(ts)}  {sender:<11s}  STRAY {len(data)}B  {data.hex(' ')}\n")

    def raw_send(self, ts, sender, raw, note=""):
        with self.lock:
            if not self.enabled or self.fh is None or self.capped:
                return
            suffix = f"  # {note}" if note else ""
            self._write(f"{_iso(ts)}  {sender:<11s}  SENT      {raw.hex(' ')}{suffix}\n")

    def _write(self, line):
        # caller holds self.lock
        self.fh.write(line)
        self.bytes_written += len(line)
        if self.bytes_written > self.max_bytes and not self.capped:
            self.capped = True
            self.fh.write(f"# === capture stopped: reached the {self.max_bytes // (1024*1024)}MB cap ===\n")
            log.warning("capture log %s reached its size cap -- no longer writing "
                        "(toggle it off and back on to start a fresh file)", self.path)

    def status(self):
        with self.lock:
            return {
                "capture_log_enabled": self.enabled,
                "capture_log_file": os.path.basename(self.path) if self.path else None,
                "capture_log_bytes": self.bytes_written,
            }


class StatusModel:
    def __init__(self, preheat_minutes_default, debug_mode_default, debug_interval_default, capture_log_default):
        self.lock = threading.Lock()
        self.status = {}
        self.status_ts = None
        self.extended = {}
        self.extended_ts = None
        self.cabin_temp = None
        self.cabin_temp_ts = None
        self.last_command = None
        persisted = load_persisted()
        self.preheat_minutes = persisted.get("preheat_minutes", preheat_minutes_default)
        self.debug_mode = persisted.get("debug_mode", debug_mode_default)
        self.debug_interval = persisted.get("debug_interval", debug_interval_default)
        self.capture_log_wanted = persisted.get("capture_log_enabled", capture_log_default)

    def note_frame(self, ts, direction, raw):
        dev = raw[1]
        type_ = raw[4] if len(raw) > 4 else None
        payload = raw[5:-2]
        with self.lock:
            if dev == 0x04 and type_ == 0x0F and len(payload) == 18:
                self.status = decode_status_payload(payload)
                self.status_ts = ts
            elif dev == 0x03 and type_ == 0x11 and len(payload) == 1:
                self.cabin_temp = payload[0]
                self.cabin_temp_ts = ts
            elif dev == 0x02 and type_ == 0x01 and len(payload) == 58:
                self.extended = decode_extended_payload(payload)
                self.extended_ts = ts
        devname = KNOWN_DEV.get(dev, f"0x{dev:02x}")
        log.debug("%s dev=%s type=%s %s", direction, devname, type_, payload.hex(" "))

    def note_command(self, text):
        with self.lock:
            self.last_command = {"text": text, "ts": time.time()}

    def set_preheat_minutes(self, minutes):
        with self.lock:
            self.preheat_minutes = minutes
        persisted = load_persisted()
        persisted["preheat_minutes"] = minutes
        save_persisted(persisted)

    def get_preheat_minutes(self):
        with self.lock:
            return self.preheat_minutes

    def set_debug_mode(self, enabled):
        with self.lock:
            self.debug_mode = bool(enabled)
        persisted = load_persisted()
        persisted["debug_mode"] = bool(enabled)
        save_persisted(persisted)

    def set_debug_interval(self, seconds):
        seconds = max(5, min(int(seconds), 3600))
        with self.lock:
            self.debug_interval = seconds
        persisted = load_persisted()
        persisted["debug_interval"] = seconds
        save_persisted(persisted)

    def get_debug_settings(self):
        with self.lock:
            return self.debug_mode, self.debug_interval

    def set_capture_log_wanted(self, enabled):
        with self.lock:
            self.capture_log_wanted = bool(enabled)
        persisted = load_persisted()
        persisted["capture_log_enabled"] = bool(enabled)
        save_persisted(persisted)

    def get_capture_log_wanted(self):
        with self.lock:
            return self.capture_log_wanted

    def snapshot(self, auto_snapshot, capture_status):
        with self.lock:
            now = time.time()
            status_age = None if self.status_ts is None else now - self.status_ts
            cabin_age = None if self.cabin_temp_ts is None else now - self.cabin_temp_ts
            ext_age = None if self.extended_ts is None else now - self.extended_ts
            stale = (
                status_age is None or cabin_age is None
                or status_age > STALE_AFTER or cabin_age > STALE_AFTER
            )
            snap = {
                "cabin_temp": self.cabin_temp,
                "cabin_temp_age": cabin_age,
                "status_age": status_age,
                "stale": stale,
                "last_command": self.last_command,
                "preheat_minutes": self.preheat_minutes,
                "debug_mode": self.debug_mode,
                "debug_interval": self.debug_interval,
                "extended_active": ext_age is not None and ext_age <= EXT_STALE_AFTER,
                "extended_age": ext_age,
            }
            snap.update(self.status)
            snap.update(self.extended)
            snap["auto_enabled"] = auto_snapshot["enabled"]
            snap["auto_target"] = auto_snapshot["target"]
            snap["auto_last_note"] = auto_snapshot["last_note"]
            snap.update(capture_status)
            return snap


class Relay(threading.Thread):
    """Transparent byte-for-byte passthrough in one direction, plus decoding
    and (if enabled) raw capture logging."""

    def __init__(self, name, sender_label, src, dst, dst_lock, model, capture_log, stop_evt):
        super().__init__(daemon=True, name=name)
        self.label = name
        self.sender_label = sender_label
        self.src = src
        self.dst = dst
        self.dst_lock = dst_lock
        self.model = model
        self.capture_log = capture_log
        self.stop_evt = stop_evt
        self.framer = Framer()
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
                log.error("%s read failed: %r", self.label, e)
                self.stop_evt.set()
                return

            ts = time.time()
            try:
                with self.dst_lock:
                    self.dst.write(data)
            except serial.SerialException as e:
                self.error = str(e)
                log.error("%s write failed: %r", self.label, e)
                self.stop_evt.set()
                return

            for ev in self.framer.feed(data):
                if ev[0] == "stray":
                    self.capture_log.stray(ts, self.sender_label, ev[1])
                    continue
                raw, crc_ok = ev[1], ev[2]
                self.capture_log.frame(ts, self.sender_label, raw, crc_ok)
                if crc_ok:
                    self.model.note_frame(ts, self.label, raw)


class Commander:
    """Builds and injects command frames toward the heater, impersonating the panel."""

    def __init__(self, heater_ser, heater_lock, model, capture_log):
        self.heater_ser = heater_ser
        self.heater_lock = heater_lock
        self.model = model
        self.capture_log = capture_log

    def _send(self, dev, type_, payload=b""):
        frame = build_frame(dev, type_, payload)
        try:
            with self.heater_lock:
                self.heater_ser.write(frame)
            ts = time.time()
            log.info("INJECT sent %s", frame.hex(" "))
            self.capture_log.raw_send(ts, "rpi", frame)
            self.model.note_command(f"sent {frame.hex(' ')}")
        except Exception as e:
            log.error("INJECT failed %s: %r", frame.hex(" "), e)
            self.model.note_command(f"FAILED to send {frame.hex(' ')}: {e!r}")
            raise

    # Sender byte 0x03 = panel/display -- confirmed to be the device that
    # originates every start/stop handshake. We impersonate it here.

    def start_preheat(self, minutes):
        minutes = max(0, min(int(minutes), 600))
        log.info("requested: start preheat %dmin", minutes)
        self._send(0x03, 0x01, bytes([0x00, 0x1E]))
        time.sleep(1.5)
        self._send(0x03, 0x02, minutes.to_bytes(2, "big"))

    def start_thermostat(self):
        log.info("requested: start thermostat")
        self._send(0x03, 0x01, bytes([0x00, 0x22]))

    def stop(self):
        log.info("requested: stop")
        self._send(0x03, 0x03)

    def start_pump(self):
        # type 0x21, payload 00 28 -- confirmed against a real capture
        # (see docs/PROTOCOL.md, "New confirmed command: pump-only start").
        # Only this exact payload has been observed; it's sent verbatim
        # rather than parameterized since nothing else is confirmed safe.
        log.info("requested: start pump (ventilation only)")
        self._send(0x03, 0x21, bytes([0x00, 0x28]))


class DebugSender(threading.Thread):
    """Periodically re-sends the vendor diagnostic tool's PUBR0 handshake
    toward the heater, when debug mode is enabled -- unlocks the extended
    telemetry frame decoded by decode_extended_payload() above.

    UNCONFIRMED whether this is safe to do while the physical panel is also
    on the bus (see docs/PROTOCOL.md) -- off by default, and DOCS.md says to
    only enable it while watching the physical panel."""

    def __init__(self, heater_ser, heater_lock, model, capture_log, stop_evt):
        super().__init__(daemon=True, name="debug-sender")
        self.heater_ser = heater_ser
        self.heater_lock = heater_lock
        self.model = model
        self.capture_log = capture_log
        self.stop_evt = stop_evt

    def send_once(self):
        frame = build_pubr0_frame()
        try:
            with self.heater_lock:
                self.heater_ser.write(frame)
            ts = time.time()
            log.info("DEBUG sent PUBR0 handshake %s", frame.hex(" "))
            self.capture_log.raw_send(ts, "rpi", frame, note="PUBR0 handshake")
            self.model.note_command(f"sent PUBR0 handshake {frame.hex(' ')}")
        except Exception as e:
            log.error("DEBUG PUBR0 send failed: %r", e)

    def run(self):
        while not self.stop_evt.is_set():
            enabled, interval = self.model.get_debug_settings()
            if not enabled:
                if self.stop_evt.wait(1.0):
                    return
                continue
            self.send_once()
            if self.stop_evt.wait(max(5, interval)):
                return


class AutoThermostat(threading.Thread):
    """Software hysteresis loop: stop at target+1, start (thermostat mode) at
    target-1. Runs independently of manual commands -- only acts when
    enabled, only on live/fresh cabin-temp readings. Rate-limited between its
    own actions to avoid thrashing near a boundary."""

    HYSTERESIS = 1.0
    MIN_ACTION_INTERVAL = 90.0
    MAX_READING_AGE = 10.0
    POLL_INTERVAL = 3.0

    def __init__(self, model, commander, stop_evt, target_default):
        super().__init__(daemon=True, name="auto-thermostat")
        self.model = model
        self.commander = commander
        self.stop_evt = stop_evt
        self.lock = threading.Lock()
        persisted = load_persisted()
        self.enabled = persisted.get("auto_enabled", False)
        self.target = persisted.get("auto_target", target_default)
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
            persisted = load_persisted()
            persisted["auto_enabled"] = self.enabled
            persisted["auto_target"] = self.target
        save_persisted(persisted)

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

            snap = self.model.snapshot(self.snapshot(), {})
            cabin, age = snap["cabin_temp"], snap["cabin_temp_age"]
            state = snap.get("state")
            if cabin is None or age is None or age > self.MAX_READING_AGE or state is None:
                continue

            if state == "idle":
                self.stop_pending = False

            try:
                if cabin >= target + self.HYSTERESIS and state != "idle" and not self.stop_pending:
                    note = f"cabin {cabin} >= {target + self.HYSTERESIS} -> stop"
                    log.info("AUTO %s", note)
                    self.commander.stop()
                    self.last_action_ts = now
                    self.stop_pending = True
                    with self.lock:
                        self.last_note = note
                elif cabin <= target - self.HYSTERESIS and state == "idle":
                    note = f"cabin {cabin} <= {target - self.HYSTERESIS} -> start thermostat"
                    log.info("AUTO %s", note)
                    self.commander.start_thermostat()
                    self.last_action_ts = now
                    with self.lock:
                        self.last_note = note
            except Exception as e:
                log.error("AUTO action failed: %r", e)


def discovery_configs():
    """(topic, payload) pairs for every entity, published retained on connect."""
    base = {"availability_topic": AVAILABILITY_TOPIC, "device": DEVICE_INFO}

    def blank_to_none(field):
        return f"{{{{ value_json.{field} if value_json.{field} is not none else '' }}}}"

    entries = []

    # -- base sensors/controls (same as autoterm-addon) -------------------

    entries.append((f"{DISCOVERY_PREFIX}/sensor/{NODE_ID}/state/config", {
        **base, "name": "State", "unique_id": f"{NODE_ID}_state",
        "state_topic": STATE_TOPIC, "value_template": blank_to_none("state"),
        "icon": "mdi:radiator",
    }))
    entries.append((f"{DISCOVERY_PREFIX}/sensor/{NODE_ID}/fault/config", {
        **base, "name": "Fault code", "unique_id": f"{NODE_ID}_fault",
        "state_topic": STATE_TOPIC, "value_template": blank_to_none("fault"),
        "icon": "mdi:alert-circle-outline", "entity_category": "diagnostic",
    }))
    entries.append((f"{DISCOVERY_PREFIX}/sensor/{NODE_ID}/cabin_temp/config", {
        **base, "name": "Cabin temperature", "unique_id": f"{NODE_ID}_cabin_temp",
        "state_topic": STATE_TOPIC, "value_template": blank_to_none("cabin_temp"),
        "device_class": "temperature", "unit_of_measurement": "°C",
        "state_class": "measurement",
    }))
    entries.append((f"{DISCOVERY_PREFIX}/sensor/{NODE_ID}/coolant_temp/config", {
        **base, "name": "Coolant temperature", "unique_id": f"{NODE_ID}_coolant_temp",
        "state_topic": STATE_TOPIC, "value_template": blank_to_none("coolant_temp"),
        "device_class": "temperature", "unit_of_measurement": "°C",
        "state_class": "measurement",
    }))
    entries.append((f"{DISCOVERY_PREFIX}/sensor/{NODE_ID}/elapsed_min/config", {
        **base, "name": "Elapsed run time", "unique_id": f"{NODE_ID}_elapsed_min",
        "state_topic": STATE_TOPIC, "value_template": blank_to_none("elapsed_min"),
        "unit_of_measurement": "min", "icon": "mdi:timer-outline",
        "entity_category": "diagnostic",
    }))
    entries.append((f"{DISCOVERY_PREFIX}/binary_sensor/{NODE_ID}/burner/config", {
        **base, "name": "Burner active", "unique_id": f"{NODE_ID}_burner",
        "state_topic": STATE_TOPIC,
        "value_template": "{{ 'ON' if value_json.burner_active else 'OFF' }}",
        "device_class": "heat",
    }))
    entries.append((f"{DISCOVERY_PREFIX}/binary_sensor/{NODE_ID}/stale/config", {
        **base, "name": "Telemetry stale", "unique_id": f"{NODE_ID}_stale",
        "state_topic": STATE_TOPIC,
        "value_template": "{{ 'ON' if value_json.stale else 'OFF' }}",
        "device_class": "problem", "entity_category": "diagnostic",
    }))

    entries.append((f"{DISCOVERY_PREFIX}/climate/{NODE_ID}/thermostat/config", {
        **base, "name": "Autoterm thermostat", "unique_id": f"{NODE_ID}_climate",
        "modes": ["off", "heat"],
        "mode_state_topic": STATE_TOPIC,
        "mode_state_template": "{{ 'heat' if value_json.auto_enabled else 'off' }}",
        "mode_command_topic": f"{CMD_PREFIX}/auto_mode/set",
        "temperature_state_topic": STATE_TOPIC,
        "temperature_state_template": blank_to_none("auto_target"),
        "temperature_command_topic": f"{CMD_PREFIX}/auto_target/set",
        "current_temperature_topic": STATE_TOPIC,
        "current_temperature_template": blank_to_none("cabin_temp"),
        "action_topic": STATE_TOPIC,
        "action_template": (
            "{{ 'heating' if value_json.burner_active "
            "else ('idle' if value_json.auto_enabled else 'off') }}"
        ),
        "min_temp": 5, "max_temp": 35, "temp_step": 0.5, "temperature_unit": "C",
    }))

    entries.append((f"{DISCOVERY_PREFIX}/number/{NODE_ID}/preheat_minutes/config", {
        **base, "name": "Preheat duration", "unique_id": f"{NODE_ID}_preheat_minutes",
        "command_topic": f"{CMD_PREFIX}/preheat_minutes/set",
        "state_topic": STATE_TOPIC, "value_template": blank_to_none("preheat_minutes"),
        "min": 1, "max": 600, "step": 1, "unit_of_measurement": "min", "mode": "box",
        "icon": "mdi:timer-cog-outline",
    }))
    entries.append((f"{DISCOVERY_PREFIX}/button/{NODE_ID}/start_preheat/config", {
        **base, "name": "Start preheat", "unique_id": f"{NODE_ID}_start_preheat",
        "command_topic": f"{CMD_PREFIX}/start_preheat", "icon": "mdi:fire",
    }))
    entries.append((f"{DISCOVERY_PREFIX}/button/{NODE_ID}/start_thermostat/config", {
        **base, "name": "Start thermostat (manual)", "unique_id": f"{NODE_ID}_start_thermostat_manual",
        "command_topic": f"{CMD_PREFIX}/start_thermostat", "icon": "mdi:thermostat",
    }))
    entries.append((f"{DISCOVERY_PREFIX}/button/{NODE_ID}/stop/config", {
        **base, "name": "Stop", "unique_id": f"{NODE_ID}_stop",
        "command_topic": f"{CMD_PREFIX}/stop",
        "icon": "mdi:stop-circle-outline",
    }))
    entries.append((f"{DISCOVERY_PREFIX}/button/{NODE_ID}/start_pump/config", {
        **base, "name": "Start pump (ventilation only)", "unique_id": f"{NODE_ID}_start_pump",
        "command_topic": f"{CMD_PREFIX}/start_pump", "icon": "mdi:fan",
    }))

    # -- debug controls -----------------------------------------------------

    entries.append((f"{DISCOVERY_PREFIX}/switch/{NODE_ID}/debug_mode/config", {
        **base, "name": "Debug mode (extended telemetry probing)", "unique_id": f"{NODE_ID}_debug_mode",
        "state_topic": STATE_TOPIC, "value_template": "{{ 'ON' if value_json.debug_mode else 'OFF' }}",
        "command_topic": f"{CMD_PREFIX}/debug_mode/set", "icon": "mdi:bug-outline",
        "entity_category": "config",
    }))
    entries.append((f"{DISCOVERY_PREFIX}/number/{NODE_ID}/debug_interval/config", {
        **base, "name": "Debug probe interval", "unique_id": f"{NODE_ID}_debug_interval",
        "command_topic": f"{CMD_PREFIX}/debug_interval/set",
        "state_topic": STATE_TOPIC, "value_template": blank_to_none("debug_interval"),
        "min": 5, "max": 3600, "step": 1, "unit_of_measurement": "s", "mode": "box",
        "icon": "mdi:timer-sand", "entity_category": "config",
    }))
    entries.append((f"{DISCOVERY_PREFIX}/button/{NODE_ID}/send_debug_handshake/config", {
        **base, "name": "Send debug handshake now", "unique_id": f"{NODE_ID}_send_debug_handshake",
        "command_topic": f"{CMD_PREFIX}/send_debug_handshake", "icon": "mdi:handshake-outline",
        "entity_category": "config",
    }))
    entries.append((f"{DISCOVERY_PREFIX}/binary_sensor/{NODE_ID}/extended_active/config", {
        **base, "name": "Extended telemetry active", "unique_id": f"{NODE_ID}_extended_active",
        "state_topic": STATE_TOPIC,
        "value_template": "{{ 'ON' if value_json.extended_active else 'OFF' }}",
        "icon": "mdi:radar", "entity_category": "diagnostic",
    }))

    entries.append((f"{DISCOVERY_PREFIX}/switch/{NODE_ID}/capture_log/config", {
        **base, "name": "Capture raw traffic log", "unique_id": f"{NODE_ID}_capture_log",
        "state_topic": STATE_TOPIC, "value_template": "{{ 'ON' if value_json.capture_log_enabled else 'OFF' }}",
        "command_topic": f"{CMD_PREFIX}/capture_log/set", "icon": "mdi:record-rec",
        "entity_category": "config",
    }))
    entries.append((f"{DISCOVERY_PREFIX}/sensor/{NODE_ID}/capture_log_file/config", {
        **base, "name": "Capture log file", "unique_id": f"{NODE_ID}_capture_log_file",
        "state_topic": STATE_TOPIC, "value_template": blank_to_none("capture_log_file"),
        "icon": "mdi:file-document-outline", "entity_category": "diagnostic",
    }))
    entries.append((f"{DISCOVERY_PREFIX}/sensor/{NODE_ID}/capture_log_size/config", {
        **base, "name": "Capture log size", "unique_id": f"{NODE_ID}_capture_log_size",
        "state_topic": STATE_TOPIC,
        "value_template": "{{ (value_json.capture_log_bytes / 1024) | round(1) if value_json.capture_log_bytes is not none else '' }}",
        "unit_of_measurement": "KB", "icon": "mdi:file-chart-outline", "entity_category": "diagnostic",
    }))

    # -- extended telemetry sensors (populated only while debug mode is on
    #    and the heater is actually replying -- see "Extended telemetry
    #    active" above) --------------------------------------------------

    entries.append((f"{DISCOVERY_PREFIX}/sensor/{NODE_ID}/ext_mode_name/config", {
        **base, "name": "Mode of operation", "unique_id": f"{NODE_ID}_ext_mode_name",
        "state_topic": STATE_TOPIC, "value_template": blank_to_none("ext_mode_name"),
        "icon": "mdi:state-machine",
    }))
    entries.append((f"{DISCOVERY_PREFIX}/sensor/{NODE_ID}/ext_running_time/config", {
        **base, "name": "Running time (extended)", "unique_id": f"{NODE_ID}_ext_running_time",
        "state_topic": STATE_TOPIC,
        "value_template": "{{ (value_json.ext_running_time_s / 60) | round(1) if value_json.ext_running_time_s is not none else '' }}",
        "unit_of_measurement": "min", "icon": "mdi:timer-outline", "entity_category": "diagnostic",
    }))
    entries.append((f"{DISCOVERY_PREFIX}/sensor/{NODE_ID}/ext_defined_rev/config", {
        **base, "name": "Defined revolutions", "unique_id": f"{NODE_ID}_ext_defined_rev",
        "state_topic": STATE_TOPIC, "value_template": blank_to_none("ext_defined_rev"),
        "icon": "mdi:fan", "state_class": "measurement",
    }))
    entries.append((f"{DISCOVERY_PREFIX}/sensor/{NODE_ID}/ext_measured_rev/config", {
        **base, "name": "Measured revolutions", "unique_id": f"{NODE_ID}_ext_measured_rev",
        "state_topic": STATE_TOPIC, "value_template": blank_to_none("ext_measured_rev"),
        "icon": "mdi:fan", "state_class": "measurement",
    }))
    entries.append((f"{DISCOVERY_PREFIX}/binary_sensor/{NODE_ID}/ext_glow_plug/config", {
        **base, "name": "Glow plug", "unique_id": f"{NODE_ID}_ext_glow_plug",
        "state_topic": STATE_TOPIC,
        "value_template": "{{ 'ON' if value_json.ext_glow_plug else 'OFF' }}",
        "device_class": "heat",
    }))
    entries.append((f"{DISCOVERY_PREFIX}/sensor/{NODE_ID}/ext_fuel_pump_hz/config", {
        **base, "name": "Fuel pump frequency", "unique_id": f"{NODE_ID}_ext_fuel_pump_hz",
        "state_topic": STATE_TOPIC, "value_template": blank_to_none("ext_fuel_pump_hz"),
        "unit_of_measurement": "Hz", "icon": "mdi:gas-station-outline", "state_class": "measurement",
    }))
    entries.append((f"{DISCOVERY_PREFIX}/sensor/{NODE_ID}/ext_flame_temp/config", {
        **base, "name": "Flame temperature", "unique_id": f"{NODE_ID}_ext_flame_temp",
        "state_topic": STATE_TOPIC, "value_template": blank_to_none("ext_flame_temp_c"),
        "device_class": "temperature", "unit_of_measurement": "°C", "state_class": "measurement",
    }))
    entries.append((f"{DISCOVERY_PREFIX}/sensor/{NODE_ID}/ext_liquid_temp/config", {
        **base, "name": "Liquid temperature (extended)", "unique_id": f"{NODE_ID}_ext_liquid_temp",
        "state_topic": STATE_TOPIC, "value_template": blank_to_none("ext_liquid_temp_c"),
        "device_class": "temperature", "unit_of_measurement": "°C", "state_class": "measurement",
    }))
    entries.append((f"{DISCOVERY_PREFIX}/sensor/{NODE_ID}/ext_overheat_temp/config", {
        **base, "name": "Overheat sensor temperature", "unique_id": f"{NODE_ID}_ext_overheat_temp",
        "state_topic": STATE_TOPIC, "value_template": blank_to_none("ext_overheat_temp_c"),
        "device_class": "temperature", "unit_of_measurement": "°C", "state_class": "measurement",
    }))
    entries.append((f"{DISCOVERY_PREFIX}/sensor/{NODE_ID}/ext_board_temp/config", {
        **base, "name": "Board temperature", "unique_id": f"{NODE_ID}_ext_board_temp",
        "state_topic": STATE_TOPIC, "value_template": blank_to_none("ext_board_temp_c"),
        "device_class": "temperature", "unit_of_measurement": "°C", "state_class": "measurement",
    }))
    entries.append((f"{DISCOVERY_PREFIX}/sensor/{NODE_ID}/ext_voltage/config", {
        **base, "name": "Supply voltage", "unique_id": f"{NODE_ID}_ext_voltage",
        "state_topic": STATE_TOPIC, "value_template": blank_to_none("ext_voltage"),
        "device_class": "voltage", "unit_of_measurement": "V", "state_class": "measurement",
    }))
    entries.append((f"{DISCOVERY_PREFIX}/sensor/{NODE_ID}/ext_fault_name/config", {
        **base, "name": "Fault (extended, named)", "unique_id": f"{NODE_ID}_ext_fault_name",
        "state_topic": STATE_TOPIC, "value_template": blank_to_none("ext_fault_name"),
        "icon": "mdi:alert-circle-outline", "entity_category": "diagnostic",
    }))
    entries.append((f"{DISCOVERY_PREFIX}/sensor/{NODE_ID}/ext_engine_state/config", {
        **base, "name": "Engine state", "unique_id": f"{NODE_ID}_ext_engine_state",
        "state_topic": STATE_TOPIC, "value_template": blank_to_none("ext_engine_state"),
        "icon": "mdi:engine-outline", "entity_category": "diagnostic",
    }))
    entries.append((f"{DISCOVERY_PREFIX}/sensor/{NODE_ID}/ext_relay_state/config", {
        **base, "name": "Relay state", "unique_id": f"{NODE_ID}_ext_relay_state",
        "state_topic": STATE_TOPIC, "value_template": blank_to_none("ext_relay_state"),
        "icon": "mdi:electric-switch", "entity_category": "diagnostic",
    }))
    entries.append((f"{DISCOVERY_PREFIX}/sensor/{NODE_ID}/ext_fan_current/config", {
        **base, "name": "Fan current", "unique_id": f"{NODE_ID}_ext_fan_current",
        "state_topic": STATE_TOPIC, "value_template": blank_to_none("ext_fan_current_ma"),
        "device_class": "current", "unit_of_measurement": "mA", "state_class": "measurement",
        "entity_category": "diagnostic",
    }))

    return entries


class Bridge:
    def __init__(self, cfg):
        self.cfg = cfg
        self.stop_evt = threading.Event()
        self._shutdown_done = False
        self.heater_lock = threading.Lock()
        self.panel_lock = threading.Lock()
        self.model = StatusModel(
            cfg["preheat_default"], cfg["debug_mode_default"],
            cfg["debug_interval_default"], cfg["capture_log_default"],
        )
        self.cmd_queue = queue.Queue()

        self.capture_log = CaptureLog(cfg["capture_log_dir"], cfg["capture_log_max_mb"])
        if self.model.get_capture_log_wanted():
            self.capture_log.start()

        self.panel_ser = serial.Serial(cfg["panel_port"], cfg["baud"], timeout=0.05)
        self.heater_ser = serial.Serial(cfg["heater_port"], cfg["baud"], timeout=0.05)

        self.commander = Commander(self.heater_ser, self.heater_lock, self.model, self.capture_log)
        self.auto = AutoThermostat(self.model, self.commander, self.stop_evt, cfg["auto_target_default"])
        self.debug_sender = DebugSender(self.heater_ser, self.heater_lock, self.model, self.capture_log, self.stop_evt)

        self.panel_to_heater = Relay(
            "PANEL->HEATER", "display", self.panel_ser, self.heater_ser, self.heater_lock,
            self.model, self.capture_log, self.stop_evt)
        self.heater_to_panel = Relay(
            "HEATER->PANEL", "heater", self.heater_ser, self.panel_ser, self.panel_lock,
            self.model, self.capture_log, self.stop_evt)

        self.mqtt = mqtt.Client(client_id=f"{NODE_ID}-debug-bridge", clean_session=True)
        if cfg.get("mqtt_username"):
            self.mqtt.username_pw_set(cfg["mqtt_username"], cfg.get("mqtt_password") or None)
        self.mqtt.will_set(AVAILABILITY_TOPIC, payload="offline", retain=True)
        self.mqtt.on_connect = self._on_connect
        self.mqtt.on_message = self._on_message

    # -- MQTT ---------------------------------------------------------

    def _on_connect(self, client, userdata, flags, rc):
        if rc != 0:
            log.error("MQTT connect failed, rc=%s", rc)
            return
        log.info("MQTT connected")
        for topic, payload in discovery_configs():
            client.publish(topic, json.dumps(payload), retain=True)
        client.publish(AVAILABILITY_TOPIC, "online", retain=True)
        for suffix in (
            "auto_mode/set", "auto_target/set", "preheat_minutes/set",
            "start_preheat", "start_thermostat", "stop", "start_pump",
            "debug_mode/set", "debug_interval/set", "send_debug_handshake",
            "capture_log/set",
        ):
            client.subscribe(f"{CMD_PREFIX}/{suffix}")
        self._publish_state()

    def _on_message(self, client, userdata, msg):
        payload = msg.payload.decode(errors="replace").strip()
        self.cmd_queue.put((msg.topic, payload))

    def _command_worker(self):
        while not self.stop_evt.is_set():
            try:
                topic, payload = self.cmd_queue.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                self._handle_command(topic, payload)
            except Exception as e:
                log.error("command %s(%r) failed: %r", topic, payload, e)
            self._publish_state()

    def _handle_command(self, topic, payload):
        suffix = topic[len(CMD_PREFIX) + 1:]
        if suffix == "start_preheat":
            self.commander.start_preheat(self.model.get_preheat_minutes())
        elif suffix == "start_thermostat":
            self.commander.start_thermostat()
        elif suffix == "stop":
            self.commander.stop()
        elif suffix == "start_pump":
            self.commander.start_pump()
        elif suffix == "preheat_minutes/set":
            self.model.set_preheat_minutes(max(1, min(int(float(payload)), 600)))
        elif suffix == "auto_mode/set":
            self.auto.configure(enabled=(payload.lower() == "heat"))
        elif suffix == "auto_target/set":
            self.auto.configure(target=float(payload))
        elif suffix == "debug_mode/set":
            self.model.set_debug_mode(payload.upper() == "ON")
        elif suffix == "debug_interval/set":
            self.model.set_debug_interval(float(payload))
        elif suffix == "send_debug_handshake":
            self.debug_sender.send_once()
        elif suffix == "capture_log/set":
            wanted = payload.upper() == "ON"
            self.model.set_capture_log_wanted(wanted)
            if wanted:
                self.capture_log.start()
            else:
                self.capture_log.stop()
        else:
            log.warning("unhandled command topic %s", topic)

    def _publish_state(self):
        snap = self.model.snapshot(self.auto.snapshot(), self.capture_log.status())
        self.mqtt.publish(STATE_TOPIC, json.dumps(snap), retain=True)

    def _heartbeat(self):
        while not self.stop_evt.wait(2.0):
            self._publish_state()

    # -- lifecycle ------------------------------------------------------

    def run(self):
        # Relay + command handling start regardless of MQTT status -- the
        # physical panel must keep working even if Home Assistant/MQTT is
        # completely unreachable, same as the base add-on's passthrough.
        self.panel_to_heater.start()
        self.heater_to_panel.start()
        self.auto.start()
        self.debug_sender.start()
        threading.Thread(target=self._command_worker, daemon=True).start()
        threading.Thread(target=self._heartbeat, daemon=True).start()
        threading.Thread(target=self._start_mqtt, daemon=True).start()

        log.info(
            "Autoterm DEBUG bridge running: panel=%s heater=%s baud=%s "
            "(MQTT connecting in background: %s:%s, debug_mode default=%s, capture default=%s)",
            self.cfg["panel_port"], self.cfg["heater_port"], self.cfg["baud"],
            self.cfg["mqtt_host"], self.cfg["mqtt_port"],
            self.cfg["debug_mode_default"], self.cfg["capture_log_default"],
        )

        self.stop_evt.wait()

    def _start_mqtt(self):
        host, port = self.cfg["mqtt_host"], self.cfg["mqtt_port"]
        if not host:
            log.error(
                "No MQTT broker configured -- relay/passthrough keeps running, "
                "but Home Assistant entities need MQTT. Install the Mosquitto "
                "broker add-on, or set mqtt_host in this add-on's Configuration "
                "tab, then restart it."
            )
            return
        try:
            self.mqtt.connect_async(host, port, keepalive=30)
            self.mqtt.loop_start()
        except Exception as e:
            log.error("Could not start MQTT connection to %s:%s: %r", host, port, e)

    def shutdown(self):
        if self.stop_evt.is_set() and self._shutdown_done:
            return
        log.info("shutting down")
        self.stop_evt.set()
        self._shutdown_done = True
        try:
            self.mqtt.publish(AVAILABILITY_TOPIC, "offline", retain=True).wait_for_publish(timeout=2)
        except Exception:
            pass
        self.mqtt.loop_stop()
        try:
            self.mqtt.disconnect()
        except Exception:
            pass
        self.panel_to_heater.join(timeout=1.0)
        self.heater_to_panel.join(timeout=1.0)
        self.auto.join(timeout=1.0)
        self.debug_sender.join(timeout=1.0)
        self.capture_log.stop()
        self.panel_ser.close()
        self.heater_ser.close()


def cfg_from_env():
    def env_float(name, default):
        v = os.environ.get(name)
        return float(v) if v else default

    def env_int(name, default):
        v = os.environ.get(name)
        return int(v) if v else default

    def env_bool(name, default):
        v = os.environ.get(name)
        if v is None or v == "":
            return default
        return v.strip().lower() in ("1", "true", "yes", "on")

    return {
        "panel_port": os.environ.get("AUTOTERM_PANEL_PORT", "/dev/ttyUSB1"),
        "heater_port": os.environ.get("AUTOTERM_HEATER_PORT", "/dev/ttyUSB3"),
        "baud": env_int("AUTOTERM_BAUD", 2400),
        "preheat_default": env_int("AUTOTERM_PREHEAT_DEFAULT", 30),
        "auto_target_default": env_float("AUTOTERM_AUTO_TARGET_DEFAULT", 20.0),
        # run.sh always exports this var (possibly to an empty string when no
        # MQTT service/option is set), so a plain .get(..., default) default
        # never actually applies -- fall back explicitly on emptiness too.
        "mqtt_host": os.environ.get("AUTOTERM_MQTT_HOST") or "core-mosquitto",
        "mqtt_port": env_int("AUTOTERM_MQTT_PORT", 1883),
        "mqtt_username": os.environ.get("AUTOTERM_MQTT_USERNAME") or None,
        "mqtt_password": os.environ.get("AUTOTERM_MQTT_PASSWORD") or None,
        "debug_mode_default": env_bool("AUTOTERM_DEBUG_MODE_DEFAULT", False),
        "debug_interval_default": env_int("AUTOTERM_DEBUG_INTERVAL_DEFAULT", 60),
        "capture_log_default": env_bool("AUTOTERM_CAPTURE_LOG_DEFAULT", False),
        "capture_log_max_mb": env_int("AUTOTERM_CAPTURE_LOG_MAX_MB", 20),
        "capture_log_dir": os.environ.get("AUTOTERM_CAPTURE_LOG_DIR", "/config/autoterm_debug"),
    }


def main():
    if "--discover-ports" in sys.argv:
        baud = int(os.environ.get("AUTOTERM_BAUD", "2400"))
        found = discover_ports(baud)
        if found is None:
            return 1
        panel_port, heater_port = found
        # Machine-parseable lines for run.sh -- logging goes to stderr, so
        # stdout stays clean for these two.
        print(f"PANEL_PORT={panel_port}")
        print(f"HEATER_PORT={heater_port}")
        return 0

    cfg = cfg_from_env()
    try:
        bridge = Bridge(cfg)
    except serial.SerialException as e:
        log.error("could not open serial ports: %r", e)
        return 1

    def handle_signal(signum, frame):
        bridge.shutdown()

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    try:
        bridge.run()
    finally:
        bridge.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
