#!/usr/bin/env python3
"""
Autoterm 5D inline UART proxy.

Phase 2 wiring: the Pi now sits BETWEEN the panel and the heater instead of
just tapping their TX lines. Both directions must be physically broken and
routed through the Pi:

    panel TX  -> ttyUSB1 RX     ttyUSB1 TX  -> panel RX     (panel port)
    heater TX -> ttyUSB3 RX     ttyUSB3 TX  -> heater RX    (heater port)

The direct panel<->heater wire pair MUST be physically disconnected. If it
is still connected in parallel with the Pi, the panel's and heater's real
transmissions still reach each other directly, and the Pi's relayed copy
arrives as a second, slightly-delayed echo on top of it -- both sides will
see garbled/doubled frames. This is a wiring precondition, not something
software can detect for you.

Relay design: every byte read from one side is written to the other side
immediately, with no buffering, re-parsing, or re-encoding. Frame decoding
(via autoterm_monitor.Framer) runs on a copy of the same bytes purely for
logging -- a bug there can never delay or alter what is forwarded. Nothing
here injects, drops, or modifies frames; it is a transparent bridge. Command
injection is a deliberate later step, not part of this script.
"""

import argparse
import os
import queue
import sys
import threading
import time
from datetime import datetime

import serial

from autoterm_monitor import Framer, KNOWN_DEV, crc_bytes


def _iso(ts):
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3]


class Relay(threading.Thread):
    """Reads raw bytes from `src` and writes them to `dst` with no delay.

    Also feeds a copy of every chunk to a Framer + log_queue for decoding,
    entirely decoupled from the forwarding path.
    """

    def __init__(self, name, src: serial.Serial, dst: serial.Serial, log_queue, stop_evt):
        super().__init__(daemon=True, name=name)
        self.label = name
        self.src = src
        self.dst = dst
        self.log_queue = log_queue
        self.stop_evt = stop_evt
        self.framer = Framer()
        self.byte_count = 0
        self.frame_count = 0
        self.error = None
        self.last_activity = None

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
            self.last_activity = ts
            self.byte_count += len(data)

            # Forward first, unconditionally -- this is the only path that matters
            # for the physical bus. Logging/decoding happens after, off the hot path.
            try:
                self.dst.write(data)
            except serial.SerialException as e:
                self.error = str(e)
                self.stop_evt.set()
                return

            for ev in self.framer.feed(data):
                if ev[0] == "frame":
                    self.frame_count += 1
                self.log_queue.put((ts, self.label, ev))


class LogWriter(threading.Thread):
    def __init__(self, log_queue, logfile, stop_evt, quiet_repeats=True):
        super().__init__(daemon=True, name="logwriter")
        self.log_queue = log_queue
        self.stop_evt = stop_evt
        self.quiet_repeats = quiet_repeats
        self.fh = open(logfile, "a", buffering=1)
        self.last_by_stream = {}

    def run(self):
        while not self.stop_evt.is_set() or not self.log_queue.empty():
            try:
                ts, label, ev = self.log_queue.get(timeout=0.2)
            except queue.Empty:
                continue
            if ev[0] == "stray":
                data = ev[1]
                self.fh.write(f"{_iso(ts)}  {label}  STRAY {len(data)}B  {data.hex(' ')}\n")
                continue
            _, raw, crc_ok = ev
            status = "OK " if crc_ok else "BAD"
            dev = raw[1]
            devname = KNOWN_DEV.get(dev, f"0x{dev:02x}")
            payload = raw[5:-2].hex(" ")
            line = (f"{_iso(ts)}  {label}  {status}  dev={devname:6s}  "
                    f"{raw.hex(' ')}  | {payload}")
            self.fh.write(line + "\n")

            stream = (label, raw[4] if len(raw) > 4 else None)
            changed = self.last_by_stream.get(stream) != raw
            self.last_by_stream[stream] = raw
            if not crc_ok:
                print(f"\033[0;31m{line}\033[0m")
            elif changed or not self.quiet_repeats:
                print(line)

    def close(self):
        self.fh.close()


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--panel", default="/dev/ttyUSB3", help="port wired to the panel")
    ap.add_argument("--heater", default="/dev/ttyUSB1", help="port wired to the heater")
    ap.add_argument("--baud", type=int, default=2400)
    ap.add_argument("--logdir", default=os.path.expanduser("~/autoterm_logs"))
    ap.add_argument("--log", default=None)
    ap.add_argument("--all", action="store_true", help="print every frame, not just changes")
    ap.add_argument("--status-every", type=float, default=10.0)
    args = ap.parse_args()

    os.makedirs(args.logdir, exist_ok=True)
    logpath = args.log or os.path.join(
        args.logdir, f"proxy_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log")

    print("Autoterm INLINE PROXY -- transparent passthrough, no injection")
    print(f"  panel  port : {args.panel}")
    print(f"  heater port : {args.heater}")
    print(f"  baud        : {args.baud} 8N1")
    print(f"  log         : {logpath}")
    print()
    print("Reminder: the direct panel<->heater wire pair must be physically")
    print("disconnected. If both sides still see each other's real TX line in")
    print("addition to the Pi's relayed copy, expect garbled/doubled frames.")
    print()

    try:
        panel_ser = serial.Serial(args.panel, args.baud, bytesize=serial.EIGHTBITS,
                                  parity=serial.PARITY_NONE, stopbits=serial.STOPBITS_ONE,
                                  timeout=0.05)
        heater_ser = serial.Serial(args.heater, args.baud, bytesize=serial.EIGHTBITS,
                                   parity=serial.PARITY_NONE, stopbits=serial.STOPBITS_ONE,
                                   timeout=0.05)
    except serial.SerialException as e:
        print(f"ERROR opening serial ports: {e}", file=sys.stderr)
        return 1

    stop_evt = threading.Event()
    log_queue = queue.Queue()

    panel_to_heater = Relay("PANEL->HEATER", panel_ser, heater_ser, log_queue, stop_evt)
    heater_to_panel = Relay("HEATER->PANEL", heater_ser, panel_ser, log_queue, stop_evt)
    writer = LogWriter(log_queue, logpath, stop_evt, quiet_repeats=not args.all)

    panel_to_heater.start()
    heater_to_panel.start()
    writer.start()

    print("Relay running. Ctrl-C to stop.\n")

    last_status = time.time()
    try:
        while not stop_evt.is_set():
            time.sleep(0.2)
            for relay in (panel_to_heater, heater_to_panel):
                if relay.error:
                    print(f"\nERROR on {relay.label}: {relay.error}", file=sys.stderr)
                    stop_evt.set()
            if args.status_every and time.time() - last_status >= args.status_every:
                last_status = time.time()
                for relay in (panel_to_heater, heater_to_panel):
                    age = "-" if relay.last_activity is None else f"{time.time() - relay.last_activity:.1f}s ago"
                    print(f"\033[0;90m[{relay.label}] {relay.byte_count}B, "
                          f"{relay.frame_count} frames, "
                          f"{relay.framer.resyncs} resyncs, "
                          f"{relay.framer.bad_crc} bad-crc, last activity {age}\033[0m")
    except KeyboardInterrupt:
        pass
    finally:
        stop_evt.set()
        panel_to_heater.join(timeout=1.0)
        heater_to_panel.join(timeout=1.0)
        writer.join(timeout=1.0)
        writer.close()
        panel_ser.close()
        heater_ser.close()
        print("\nStopped. Final stats:")
        for relay in (panel_to_heater, heater_to_panel):
            print(f"  {relay.label}: {relay.byte_count}B, {relay.frame_count} frames, "
                  f"{relay.framer.resyncs} resyncs, {relay.framer.bad_crc} bad-crc")
        print(f"log written to {logpath}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
