#!/usr/bin/env python3
"""
Autoterm 5D passive UART monitor.

Taps both directions of the heater <-> comfort panel link, splits the byte
stream into frames, verifies CRC, logs to file and prints a live summary.

Frame layout (confirmed against captured frames by CRC):

    AA | dev(1) | len(2, little-endian) | type(1) | payload(len) | crc16(2, big-endian)

    total frame length = 7 + len
    dev  0x03 = heater, 0x04 = comfort panel
    crc  CRC-16/MODBUS (poly 0xA001 reflected, init 0xFFFF) over frame[0:-2],
         transmitted most-significant byte first

Read-only. Never writes to the serial ports.
"""

import argparse
import os
import signal
import sys
import threading
import time
from collections import Counter, OrderedDict
from datetime import datetime

import serial

# Device byte. dev03 is the comfort panel/display (confirmed: it's the one
# reporting a plain cabin-temperature reading, and the one that originates
# every start/stop handshake, matching a device with physical buttons). The
# heater uses three sub-identities: 0x04 for its main status/settings report,
# plus 0x00 and 0x02 for short acks. Originally mislabeled the other way
# around for most of a day of captures before this was caught and fixed.
KNOWN_DEV = {0x00: "heater/ack", 0x02: "heater/ack2", 0x03: "panel", 0x04: "heater"}
MAX_PAYLOAD = 250          # sanity guard for the length field during resync
HEADER_LEN = 5             # AA dev len_lo len_hi type
CRC_LEN = 2


# --------------------------------------------------------------------------
# CRC-16/MODBUS
# --------------------------------------------------------------------------

def _make_table():
    table = []
    for i in range(256):
        crc = i
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
        table.append(crc)
    return table


_CRC_TABLE = _make_table()


def crc16_modbus(data: bytes) -> int:
    crc = 0xFFFF
    for b in data:
        crc = (crc >> 8) ^ _CRC_TABLE[(crc ^ b) & 0xFF]
    return crc


def crc_bytes(data: bytes) -> bytes:
    """CRC over data, in the order it appears on the wire (big-endian)."""
    return crc16_modbus(data).to_bytes(2, "big")


# --------------------------------------------------------------------------
# Framing
# --------------------------------------------------------------------------

class Frame:
    __slots__ = ("ts", "t0", "port", "direction", "raw", "crc_ok", "gap")

    def __init__(self, ts, t0, port, direction, raw, crc_ok, gap):
        self.ts = ts          # end of frame on the wire
        self.t0 = t0          # start of frame, back-calculated from byte count
        self.port = port
        self.direction = direction
        self.raw = raw
        self.crc_ok = crc_ok
        self.gap = gap

    @property
    def dev(self):
        return self.raw[1]

    @property
    def length(self):
        return int.from_bytes(self.raw[2:4], "little")

    @property
    def type(self):
        return self.raw[4]

    @property
    def payload(self):
        return self.raw[5:-CRC_LEN]

    @property
    def key(self):
        """Identity used for dedup / change detection."""
        return self.raw.hex()

    def hexs(self):
        return " ".join(f"{b:02x}" for b in self.raw)


class Framer:
    """Incremental 0xAA-synchronised frame splitter with CRC validation."""

    def __init__(self):
        self.buf = bytearray()
        self.resyncs = 0
        self.dropped = 0
        self.bad_crc = 0

    def feed(self, data: bytes):
        """Append bytes, return a list of ('frame', raw, crc_ok) / ('stray', bytes).

        Framing trusts the CRC rather than a device-byte whitelist, so frames
        using device values we have not catalogued yet are still captured.
        """
        self.buf.extend(data)
        out = []
        while True:
            # Anything before the first start byte is not part of a frame.
            start = self.buf.find(0xAA)
            if start < 0:
                if self.buf:
                    out.append(("stray", bytes(self.buf)))
                    self.dropped += len(self.buf)
                    self.buf.clear()
                break
            if start > 0:
                out.append(("stray", bytes(self.buf[:start])))
                self.dropped += start
                del self.buf[:start]

            if len(self.buf) < HEADER_LEN:
                break

            length = int.from_bytes(self.buf[2:4], "little")

            # Implausible length -> this 0xAA was payload data, not a start byte.
            if length > MAX_PAYLOAD:
                self._slip(out)
                continue

            total = HEADER_LEN + length + CRC_LEN
            if len(self.buf) < total:
                break  # wait for more bytes

            raw = bytes(self.buf[:total])
            if crc_bytes(raw[:-CRC_LEN]) == raw[-CRC_LEN:]:
                out.append(("frame", raw, True))
                del self.buf[:total]
            else:
                # Either a corrupt frame or a false 0xAA sync. A known device
                # byte means it was almost certainly a real frame that got
                # mangled, which is worth reporting; otherwise just resync.
                if raw[1] in KNOWN_DEV:
                    self.bad_crc += 1
                    out.append(("frame", raw, False))
                self._slip(out)
        return out

    def _slip(self, out):
        self.resyncs += 1
        out.append(("stray", bytes(self.buf[:1])))
        del self.buf[:1]


# --------------------------------------------------------------------------
# Capture
# --------------------------------------------------------------------------

class Tap(threading.Thread):
    def __init__(self, port, direction, baud, sink, stray_sink, stop_evt):
        super().__init__(daemon=True, name=direction.strip())
        self.port = port
        self.direction = direction
        self.baud = baud
        self.sink = sink
        self.stray_sink = stray_sink
        self.stop_evt = stop_evt
        self.byte_time = 10.0 / baud   # 8N1 -> 10 bit times per byte
        self.framer = Framer()
        self.last_ts = None
        self.byte_count = 0
        self.error = None

    def run(self):
        try:
            ser = serial.Serial(
                self.port, self.baud,
                bytesize=serial.EIGHTBITS,
                parity=serial.PARITY_NONE,
                stopbits=serial.STOPBITS_ONE,
                timeout=0.1,
            )
        except serial.SerialException as e:
            self.error = str(e)
            self.stop_evt.set()
            return

        with ser:
            while not self.stop_evt.is_set():
                try:
                    # Block on the first byte so the timestamp tracks real byte
                    # arrival. Reading a fixed count with a timeout instead would
                    # return on the timeout boundary and quantise every frame to
                    # the poll interval, which destroys cross-port ordering.
                    first = ser.read(1)
                    if not first:
                        continue
                    ts = time.time()
                    data = first + ser.read(ser.in_waiting)
                except serial.SerialException as e:
                    self.error = str(e)
                    self.stop_evt.set()
                    return
                self.byte_count += len(data)
                for ev in self.framer.feed(data):
                    if ev[0] == "stray":
                        self.stray_sink(ts, self.direction, ev[1])
                        continue
                    _, raw, crc_ok = ev
                    # A 25-byte panel frame occupies 104 ms of wire time at 2400
                    # baud vs 29 ms for a 7-byte one; comparing end timestamps
                    # across ports would read as a 75 ms skew that isn't real.
                    t0 = ts - len(raw) * self.byte_time
                    gap = None if self.last_ts is None else t0 - self.last_ts
                    self.last_ts = t0
                    self.sink(Frame(ts, t0, self.port, self.direction, raw, crc_ok, gap))


# --------------------------------------------------------------------------
# Logging / live view
# --------------------------------------------------------------------------

class Recorder:
    def __init__(self, logfile, quiet_repeats=True, show_bad=True):
        self.lock = threading.Lock()
        self.fh = open(logfile, "a", buffering=1)
        self.quiet_repeats = quiet_repeats
        self.show_bad = show_bad
        self.counts = Counter()          # frame hex -> times seen
        self.per_dir = Counter()         # direction -> frames
        self.bad = Counter()             # direction -> bad crc frames
        # Keyed on (direction, msg type): the heater cycles through several
        # message types, so comparing against the previous frame from that
        # direction alone would flag every single frame as changed.
        self.last_by_stream = {}         # (direction, type) -> last frame hex
        self.first_seen = OrderedDict()  # frame hex -> (ts, direction)
        self.total = 0
        self.stray_bytes = 0

    def header(self, note):
        self.fh.write(f"# === session start {datetime.now().isoformat()} :: {note}\n")

    def mark(self, label):
        ts = time.time()
        line = f"{_iso(ts)}  MARK   ------  ----  {label}"
        with self.lock:
            self.fh.write(line + "\n")
        print(f"\n\033[1;33m>>> {line}\033[0m")

    def record(self, frame: Frame):
        with self.lock:
            self.total += 1
            key = frame.key
            seen_before = key in self.counts
            self.counts[key] += 1
            self.per_dir[frame.direction] += 1
            if not frame.crc_ok:
                self.bad[frame.direction] += 1
            if not seen_before:
                self.first_seen[key] = (frame.ts, frame.direction)
            stream = (frame.direction, frame.type)
            changed = stream in self.last_by_stream and self.last_by_stream[stream] != key
            self.last_by_stream[stream] = key

            self.fh.write(self._log_line(frame) + "\n")

            if not frame.crc_ok:
                if self.show_bad:
                    print(f"\033[0;31m{self._log_line(frame)}\033[0m")
                return
            if not seen_before:
                print(f"\033[1;32m{self._log_line(frame)}   <-- NEW\033[0m")
            elif changed:
                print(f"\033[0;36m{self._log_line(frame)}   <-- changed\033[0m")
            elif not self.quiet_repeats:
                print(self._log_line(frame))

    def stray(self, ts, direction, data):
        """Bytes that were not part of a valid frame -- logged, never discarded
        silently, since unparsed traffic is exactly what we are hunting for."""
        with self.lock:
            self.stray_bytes += len(data)
            self.fh.write(f"{_iso(ts)}  {direction}  STRAY {len(data)}B  "
                          f"{data.hex(' ')}\n")

    def _log_line(self, f: Frame):
        status = "OK " if f.crc_ok else "BAD"
        gap = f"{f.gap:6.3f}" if f.gap is not None else "     -"
        payload = " ".join(f"{b:02x}" for b in f.payload)
        return (f"{_iso(f.t0)}  {f.direction}  {status}  dt={gap}  "
                f"dev={f.dev:02x} type={f.type:02x} len={f.length:<3d}  "
                f"{f.hexs()}  | {payload}")

    def summary(self):
        with self.lock:
            lines = []
            lines.append(f"frames: {self.total}   unique: {len(self.counts)}   "
                         f"stray bytes: {self.stray_bytes}")
            for d in sorted(self.per_dir):
                lines.append(f"  {d}: {self.per_dir[d]} frames, "
                             f"{self.bad[d]} bad CRC")
            lines.append("  top frames:")
            for key, n in self.counts.most_common(8):
                _, d = self.first_seen[key]
                lines.append(f"    {n:6d} x [{d.strip()}] {_spaced(key)}")
            return "\n".join(lines)

    def close(self):
        with self.lock:
            self.fh.write(f"# === session end {datetime.now().isoformat()}\n")
            self.fh.close()


def _iso(ts):
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3]


def _spaced(hexstr):
    return " ".join(hexstr[i:i + 2] for i in range(0, len(hexstr), 2))


# --------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--panel", default="/dev/ttyUSB1",
                    help="port tapping panel TX (panel -> heater)")
    ap.add_argument("--heater", default="/dev/ttyUSB3",
                    help="port tapping heater TX (heater -> panel)")
    ap.add_argument("--baud", type=int, default=2400)
    ap.add_argument("--log", default=None, help="log file path")
    ap.add_argument("--logdir", default=os.path.expanduser("~/autoterm_logs"))
    ap.add_argument("--note", default="", help="note recorded in the log header")
    ap.add_argument("--all", action="store_true",
                    help="print every frame, not just new/changed ones")
    ap.add_argument("--summary-every", type=float, default=15.0,
                    help="seconds between live summaries (0 to disable)")
    args = ap.parse_args()

    os.makedirs(args.logdir, exist_ok=True)
    logpath = args.log or os.path.join(
        args.logdir, f"capture_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log")

    rec = Recorder(logpath, quiet_repeats=not args.all)
    rec.header(args.note or "no note")

    stop = threading.Event()
    taps = [
        Tap(args.panel, "PANEL ", args.baud, rec.record, rec.stray, stop),
        Tap(args.heater, "HEATER", args.baud, rec.record, rec.stray, stop),
    ]

    print(f"Autoterm monitor  {args.baud} 8N1")
    print(f"  PANEL  -> heater : {args.panel}")
    print(f"  HEATER -> panel  : {args.heater}")
    print(f"  log              : {logpath}")
    print("Type a label + Enter to drop a MARK in the log (e.g. 'pressed power').")
    print("Ctrl-C to stop.\n")

    for t in taps:
        t.start()

    time.sleep(0.4)
    for t in taps:
        if t.error:
            print(f"ERROR opening {t.port}: {t.error}", file=sys.stderr)
            stop.set()
            rec.close()
            return 1

    signal.signal(signal.SIGINT, lambda *a: stop.set())
    signal.signal(signal.SIGTERM, lambda *a: stop.set())

    # Marker input on a background thread so it never blocks capture.
    def reader():
        while not stop.is_set():
            try:
                line = sys.stdin.readline()
            except Exception:
                return
            if not line:
                return
            rec.mark(line.strip() or "mark")

    if sys.stdin.isatty():
        threading.Thread(target=reader, daemon=True).start()

    last_sum = time.time()
    try:
        while not stop.is_set():
            time.sleep(0.2)
            if args.summary_every and time.time() - last_sum >= args.summary_every:
                last_sum = time.time()
                print(f"\n\033[0;90m--- {datetime.now().strftime('%H:%M:%S')} ---\n"
                      f"{rec.summary()}\033[0m\n")
    finally:
        stop.set()
        for t in taps:
            t.join(timeout=1.0)
        print("\n\n=== final summary ===")
        print(rec.summary())
        for t in taps:
            print(f"  {t.direction} {t.port}: {t.byte_count} bytes, "
                  f"{t.framer.resyncs} resyncs, {t.framer.dropped} stray bytes, "
                  f"{t.framer.bad_crc} bad CRC")
        rec.close()
        print(f"\nlog written to {logpath}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
