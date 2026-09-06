#!/usr/bin/env python3
"""
Offline analysis for Autoterm capture logs produced by autoterm_monitor.py.

  verify   re-check the CRC of every frame in a log
  inventory  list distinct frames per direction/type with counts and timing
  fields   show which payload byte positions vary, and their observed values
  diff     compare two captures (or two time windows) byte position by position
  pairs    measure request/response timing between the two directions

Examples:
  ./autoterm_analyze.py inventory ~/autoterm_logs/capture_*.log
  ./autoterm_analyze.py fields idle.log --dir PANEL --type 0f
  ./autoterm_analyze.py diff idle.log pressed_power.log
  ./autoterm_analyze.py diff capture.log --split 08:31:05
"""

import argparse
import glob
import re
import sys
from collections import Counter, OrderedDict, defaultdict
from datetime import datetime

from autoterm_monitor import crc_bytes, KNOWN_DEV

LINE_RE = re.compile(
    r"^(?P<ts>\S+)\s+(?P<dir>HEATER|PANEL)\s+(?P<status>OK|BAD)\s+"
    r"dt=\s*(?P<dt>\S+)\s+dev=(?P<dev>[0-9a-f]{2})\s+type=(?P<type>[0-9a-f]{2})\s+"
    r"len=(?P<len>\d+)\s+(?P<raw>[0-9a-f ]+?)\s+\|"
)
MARK_RE = re.compile(r"^(?P<ts>\S+)\s+MARK\s+\S+\s+\S+\s+(?P<label>.*)$")


class Rec:
    __slots__ = ("ts", "dir", "status", "dev", "type", "raw", "label")

    def __init__(self, ts, dir_, status, dev, type_, raw, label=None):
        self.ts = ts
        self.dir = dir_
        self.status = status
        self.dev = dev
        self.type = type_
        self.raw = raw
        self.label = label

    @property
    def payload(self):
        return self.raw[5:-2]

    @property
    def stream(self):
        return f"{self.dir}/dev{self.dev:02x}/type{self.type:02x}"


def parse(paths):
    recs, marks = [], []
    for path in paths:
        with open(path) as fh:
            for line in fh:
                if line.startswith("#"):
                    continue
                m = LINE_RE.match(line)
                if m:
                    recs.append(Rec(
                        _t(m["ts"]), m["dir"].strip(), m["status"],
                        int(m["dev"], 16), int(m["type"], 16),
                        bytes.fromhex(m["raw"].replace(" ", "")),
                    ))
                    continue
                m = MARK_RE.match(line)
                if m:
                    marks.append((_t(m["ts"]), m["label"].strip()))
    recs.sort(key=lambda r: r.ts)
    return recs, marks


def _t(s):
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%S.%f").timestamp()


def _hhmmss(ts):
    return datetime.fromtimestamp(ts).strftime("%H:%M:%S.%f")[:-3]


def _spaced(b):
    return " ".join(f"{x:02x}" for x in b)


# --------------------------------------------------------------------------

def cmd_verify(recs, args):
    bad = 0
    for r in recs:
        ok = crc_bytes(r.raw[:-2]) == r.raw[-2:]
        if not ok:
            bad += 1
            print(f"{_hhmmss(r.ts)}  {r.dir}  CRC FAIL  "
                  f"calc={crc_bytes(r.raw[:-2]).hex()} rx={r.raw[-2:].hex()}  {_spaced(r.raw)}")
    print(f"\n{len(recs)} frames checked, {bad} CRC failures "
          f"({'clean' if not bad else 'CORRUPTION PRESENT'})")
    devs = Counter(r.dev for r in recs)
    print("device bytes seen: " +
          ", ".join(f"0x{d:02x} ({KNOWN_DEV.get(d, 'UNKNOWN')}) x{n}"
                    for d, n in sorted(devs.items())))


def cmd_inventory(recs, args):
    by_stream = defaultdict(list)
    for r in recs:
        by_stream[r.stream].append(r)
    for stream in sorted(by_stream):
        rs = by_stream[stream]
        variants = Counter(r.raw for r in rs)
        gaps = [b.ts - a.ts for a, b in zip(rs, rs[1:])]
        period = f"{sum(gaps)/len(gaps):.3f}s" if gaps else "n/a"
        print(f"\n{stream}   {len(rs)} frames, {len(variants)} distinct, "
              f"mean period {period}")
        for raw, n in variants.most_common(args.top):
            first = next(r for r in rs if r.raw == raw)
            print(f"   {n:6d} x  {_spaced(raw)}   (first {_hhmmss(first.ts)})")


def cmd_fields(recs, args):
    recs = _filter(recs, args)
    by_stream = defaultdict(list)
    for r in recs:
        by_stream[r.stream].append(r)
    for stream in sorted(by_stream):
        rs = by_stream[stream]
        payloads = [r.payload for r in rs]
        if not payloads:
            continue
        width = len(payloads[0])
        if any(len(p) != width for p in payloads):
            print(f"\n{stream}: variable payload width, skipping field analysis")
            continue
        print(f"\n{stream}   {len(rs)} frames, payload {width} bytes")
        print("   idx  distinct  values (count)")
        for i in range(width):
            vals = Counter(p[i] for p in payloads)
            tag = "CONST" if len(vals) == 1 else f"VAR({len(vals)})"
            shown = "  ".join(f"{v:02x}({n})" for v, n in vals.most_common(8))
            if len(vals) > 8:
                shown += "  ..."
            print(f"   [{i:2d}]  {tag:8s}  {shown}")


def cmd_pairs(recs, args):
    """Measure the gap between each panel frame and the next heater frame."""
    gaps = defaultdict(list)
    for a, b in zip(recs, recs[1:]):
        if a.dir != b.dir:
            gaps[(a.stream, b.stream)].append(b.ts - a.ts)
    print("transition timing (start-of-frame to start-of-frame):\n")
    for (src, dst), g in sorted(gaps.items(), key=lambda kv: -len(kv[1])):
        if len(g) < 2:
            continue
        g_sorted = sorted(g)
        print(f"  {src:28s} -> {dst:28s}  n={len(g):4d}  "
              f"min={min(g):.3f}  median={g_sorted[len(g)//2]:.3f}  max={max(g):.3f}")


def cmd_diff(recs_a, recs_b, args, label_a="A", label_b="B"):
    """Compare payload byte values between two sets, per stream."""
    def index(recs):
        d = defaultdict(list)
        for r in recs:
            d[r.stream].append(r.payload)
        return d

    ia, ib = index(recs_a), index(recs_b)
    streams = sorted(set(ia) | set(ib))
    for stream in streams:
        pa, pb = ia.get(stream, []), ib.get(stream, [])
        if not pa or not pb:
            print(f"\n{stream}: present only in "
                  f"{label_a if pa else label_b} ({len(pa) or len(pb)} frames)")
            continue
        width = min(len(pa[0]), len(pb[0]))
        rows = []
        for i in range(width):
            va = Counter(p[i] for p in pa if len(p) > i)
            vb = Counter(p[i] for p in pb if len(p) > i)
            if set(va) != set(vb):
                rows.append((i, va, vb))
        print(f"\n{stream}   {label_a}: {len(pa)} frames   {label_b}: {len(pb)} frames")
        if not rows:
            print("   no payload byte positions differ")
            continue
        for i, va, vb in rows:
            fa = " ".join(f"{v:02x}" for v, _ in va.most_common(6))
            fb = " ".join(f"{v:02x}" for v, _ in vb.most_common(6))
            only_b = sorted(set(vb) - set(va))
            marker = "  <== NEW VALUES: " + " ".join(f"{v:02x}" for v in only_b) if only_b else ""
            print(f"   [{i:2d}]  {label_a}: {fa:20s}  {label_b}: {fb:20s}{marker}")


def _filter(recs, args):
    out = recs
    if args.dir:
        out = [r for r in out if r.dir == args.dir.upper().strip()]
    if args.type is not None:
        t = int(args.type, 16)
        out = [r for r in out if r.type == t]
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["verify", "inventory", "fields", "diff", "pairs"])
    ap.add_argument("logs", nargs="+")
    ap.add_argument("--dir", default=None, help="filter: HEATER or PANEL")
    ap.add_argument("--type", default=None, help="filter: message type in hex, e.g. 0f")
    ap.add_argument("--top", type=int, default=10)
    ap.add_argument("--split", default=None,
                    help="for diff on a single log: HH:MM:SS boundary between before/after")
    ap.add_argument("--mark", default=None,
                    help="for diff on a single log: split at the MARK whose label contains this")
    args = ap.parse_args()

    paths = []
    for pat in args.logs:
        paths.extend(sorted(glob.glob(pat)) or [pat])

    if args.cmd == "diff":
        if len(paths) >= 2:
            a, _ = parse(paths[:1])
            b, _ = parse(paths[1:])
            cmd_diff(_filter(a, args), _filter(b, args), args,
                     paths[0].split("/")[-1], paths[1].split("/")[-1])
        else:
            recs, marks = parse(paths)
            recs = _filter(recs, args)
            boundary = None
            if args.mark:
                hits = [ts for ts, lbl in marks if args.mark.lower() in lbl.lower()]
                if not hits:
                    print(f"no MARK matching {args.mark!r}; marks present: "
                          f"{[l for _, l in marks]}", file=sys.stderr)
                    return 1
                boundary = hits[0]
            elif args.split:
                day = datetime.fromtimestamp(recs[0].ts).strftime("%Y-%m-%d")
                boundary = _t(f"{day}T{args.split}.000")
            else:
                print("single log diff needs --split HH:MM:SS or --mark LABEL",
                      file=sys.stderr)
                return 1
            before = [r for r in recs if r.ts < boundary]
            after = [r for r in recs if r.ts >= boundary]
            print(f"split at {_hhmmss(boundary)}: "
                  f"{len(before)} frames before, {len(after)} after")
            cmd_diff(before, after, args, "before", "after")
        return 0

    recs, _ = parse(paths)
    if not recs:
        print("no frames parsed", file=sys.stderr)
        return 1
    {"verify": cmd_verify, "inventory": cmd_inventory,
     "fields": cmd_fields, "pairs": cmd_pairs}[args.cmd](
        recs if args.cmd in ("verify", "inventory", "pairs") else recs, args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
