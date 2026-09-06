#!/usr/bin/env python3
"""
Autoterm 5D <-> Home Assistant bridge (Supervisor add-on).

Runs INSTEAD of autoterm_web.py, not alongside it -- a serial port can only
have one owner, and this add-on owns both legs of the inline proxy directly.
It does exactly what autoterm_web.py does (transparent passthrough relay +
command injection impersonating the panel + software auto-thermostat), minus
its own HTTP dashboard, plus MQTT with Home Assistant MQTT discovery so the
heater shows up as a device with sensors, a climate entity, and buttons.

The physical panel keeps working normally the whole time -- passthrough is
never disabled, so it remains a manual fallback exactly as it does with
autoterm_web.py.

Device roles, frame layout, and confirmed commands are reused verbatim from
autoterm_web.py / docs/PROTOCOL.md in the main repo -- NOT re-derived here.
In short: dev03 = panel (originates start/stop, reports cabin temp), dev04 =
heater (rich 18-byte status frame). Commands are injected as dev03 (panel)
out the port wired to the heater.
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
log = logging.getLogger("autoterm")

NODE_ID = "autoterm5d"
DISCOVERY_PREFIX = "homeassistant"
STATE_TOPIC = f"autoterm/{NODE_ID}/state"
AVAILABILITY_TOPIC = f"autoterm/{NODE_ID}/availability"
CMD_PREFIX = f"autoterm/{NODE_ID}/cmd"
STATE_FILE = "/data/autoterm_state.json"
STALE_AFTER = 5.0

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


def build_frame(dev, type_, payload=b""):
    raw = bytes([0xAA, dev]) + len(payload).to_bytes(2, "little") + bytes([type_]) + payload
    return raw + crc_bytes(raw)


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
# Port autodiscovery
#
# Only usable while nothing else has the ports open -- this runs as a
# one-shot `--discover-ports` invocation from run.sh, before the bridge
# opens panel_port/heater_port for real, not while the relay is live.
#
# Two phases, matching how the wiring actually behaves once the Pi sits
# inline (direct panel<->heater wire cut): the panel appears to free-run its
# own traffic (cabin-temp reports) regardless of whether the heater leg is
# connected to anything, so it's identifiable by pure listening. The heater
# side has no known free-running broadcast -- its 18-byte status is a *reply*
# to an empty type0f "query" (see docs/PROTOCOL.md), so finding it requires
# sending that same query and listening for the dev04 reply. That empty
# type0f frame is the only thing ever sent during discovery -- never
# type01/02/03 (start/stop), which actuate the real heater and must never be
# used just to probe a port.
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


class StatusModel:
    def __init__(self, preheat_minutes_default):
        self.lock = threading.Lock()
        self.status = {}
        self.status_ts = None
        self.cabin_temp = None
        self.cabin_temp_ts = None
        self.last_command = None
        persisted = load_persisted()
        self.preheat_minutes = persisted.get("preheat_minutes", preheat_minutes_default)

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

    def snapshot(self, auto_snapshot):
        with self.lock:
            now = time.time()
            status_age = None if self.status_ts is None else now - self.status_ts
            cabin_age = None if self.cabin_temp_ts is None else now - self.cabin_temp_ts
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
            }
            snap.update(self.status)
            snap["auto_enabled"] = auto_snapshot["enabled"]
            snap["auto_target"] = auto_snapshot["target"]
            snap["auto_last_note"] = auto_snapshot["last_note"]
            return snap


class Relay(threading.Thread):
    """Transparent byte-for-byte passthrough in one direction, plus decoding."""

    def __init__(self, name, src, dst, dst_lock, model, stop_evt):
        super().__init__(daemon=True, name=name)
        self.label = name
        self.src = src
        self.dst = dst
        self.dst_lock = dst_lock
        self.model = model
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
                if ev[0] != "frame":
                    continue
                raw, crc_ok = ev[1], ev[2]
                if crc_ok:
                    self.model.note_frame(ts, self.label, raw)


class Commander:
    """Builds and injects command frames toward the heater, impersonating the panel."""

    def __init__(self, heater_ser, heater_lock, model):
        self.heater_ser = heater_ser
        self.heater_lock = heater_lock
        self.model = model

    def _send(self, dev, type_, payload=b""):
        frame = build_frame(dev, type_, payload)
        try:
            with self.heater_lock:
                self.heater_ser.write(frame)
            log.info("INJECT sent %s", frame.hex(" "))
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

            snap = self.model.snapshot(self.snapshot())
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

    return entries


class Bridge:
    def __init__(self, cfg):
        self.cfg = cfg
        self.stop_evt = threading.Event()
        self._shutdown_done = False
        self.heater_lock = threading.Lock()
        self.panel_lock = threading.Lock()
        self.model = StatusModel(cfg["preheat_default"])
        self.cmd_queue = queue.Queue()

        self.panel_ser = serial.Serial(cfg["panel_port"], cfg["baud"], timeout=0.05)
        self.heater_ser = serial.Serial(cfg["heater_port"], cfg["baud"], timeout=0.05)

        self.commander = Commander(self.heater_ser, self.heater_lock, self.model)
        self.auto = AutoThermostat(self.model, self.commander, self.stop_evt, cfg["auto_target_default"])

        self.panel_to_heater = Relay(
            "PANEL->HEATER", self.panel_ser, self.heater_ser, self.heater_lock, self.model, self.stop_evt)
        self.heater_to_panel = Relay(
            "HEATER->PANEL", self.heater_ser, self.panel_ser, self.panel_lock, self.model, self.stop_evt)

        self.mqtt = mqtt.Client(client_id=f"{NODE_ID}-bridge", clean_session=True)
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
            "start_preheat", "start_thermostat", "stop",
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
        elif suffix == "preheat_minutes/set":
            self.model.set_preheat_minutes(max(1, min(int(float(payload)), 600)))
        elif suffix == "auto_mode/set":
            self.auto.configure(enabled=(payload.lower() == "heat"))
        elif suffix == "auto_target/set":
            self.auto.configure(target=float(payload))
        else:
            log.warning("unhandled command topic %s", topic)

    def _publish_state(self):
        snap = self.model.snapshot(self.auto.snapshot())
        self.mqtt.publish(STATE_TOPIC, json.dumps(snap), retain=True)

    def _heartbeat(self):
        while not self.stop_evt.wait(2.0):
            self._publish_state()

    # -- lifecycle ------------------------------------------------------

    def run(self):
        # Relay + command handling start regardless of MQTT status -- the
        # physical panel must keep working even if Home Assistant/MQTT is
        # completely unreachable, same as autoterm_web.py's passthrough.
        self.panel_to_heater.start()
        self.heater_to_panel.start()
        self.auto.start()
        threading.Thread(target=self._command_worker, daemon=True).start()
        threading.Thread(target=self._heartbeat, daemon=True).start()
        threading.Thread(target=self._start_mqtt, daemon=True).start()

        log.info(
            "Autoterm bridge running: panel=%s heater=%s baud=%s (MQTT connecting in background: %s:%s)",
            self.cfg["panel_port"], self.cfg["heater_port"], self.cfg["baud"],
            self.cfg["mqtt_host"], self.cfg["mqtt_port"],
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
            # connect_async + loop_start hands connection (and automatic
            # reconnection on broker downtime) to paho's background thread,
            # instead of a blocking connect() that raises straight into this
            # thread on a bad/unreachable host.
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
        self.panel_ser.close()
        self.heater_ser.close()


def cfg_from_env():
    def env_float(name, default):
        v = os.environ.get(name)
        return float(v) if v else default

    def env_int(name, default):
        v = os.environ.get(name)
        return int(v) if v else default

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
