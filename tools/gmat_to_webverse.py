#!/usr/bin/env python3
"""Convert a GMAT fixed-width ReportFile ephemeris into JSON for the WebVerse bridge.

Usage:
    python gmat_to_webverse.py <report.txt> [object_name] [out.json]

GMAT writes ReportFile columns as fixed-width slots wider than the configured
ColumnWidth (here 26 chars/column, not the 23 set in the script), and repeats
the header row once per distinct Report command call site in the mission
sequence -- both handled below rather than assumed.
"""
import json
import sys
from pathlib import Path

COLUMN_WIDTH = 26
NUM_COLUMNS = 11  # ElapsedSecs, UTCGregorian, X, Y, Z, VX, VY, VZ, SunX, SunY, SunZ


def _unit(v):
    mag = (v[0] ** 2 + v[1] ** 2 + v[2] ** 2) ** 0.5
    return [v[0] / mag, v[1] / mag, v[2] / mag]


def parse_report(path):
    rows = []
    with open(path, "r") as f:
        for line in f:
            line = line.rstrip("\n")
            if not line.strip():
                continue

            fields = [
                line[i * COLUMN_WIDTH:(i + 1) * COLUMN_WIDTH].strip()
                for i in range(NUM_COLUMNS)
            ]

            if fields[0].startswith("SC.") or fields[0].startswith("Sun."):
                continue  # header row (repeats once per Report call site)

            try:
                t = float(fields[0])
            except ValueError:
                continue  # not a data row we recognize -- skip rather than crash

            pos = [float(fields[2]), float(fields[3]), float(fields[4])]
            sun_pos = [float(fields[8]), float(fields[9]), float(fields[10])]
            sun_dir = _unit([sun_pos[i] - pos[i] for i in range(3)])

            rows.append({
                "t": t,
                "utc": fields[1],
                "pos": pos,
                "vel": [float(fields[5]), float(fields[6]), float(fields[7])],
                "sun_dir": sun_dir,
            })
    return rows


def main():
    if len(sys.argv) < 2:
        print("usage: gmat_to_webverse.py <report.txt> [object_name] [out.json]")
        sys.exit(1)

    report_path = Path(sys.argv[1])
    object_name = sys.argv[2] if len(sys.argv) > 2 else "SC"
    out_path = Path(sys.argv[3]) if len(sys.argv) > 3 else report_path.with_suffix(".json")

    rows = parse_report(report_path)
    if not rows:
        print("No data rows parsed -- check column widths / report format.")
        sys.exit(1)

    payload = {
        "epoch": rows[0]["utc"],
        "objects": {
            object_name: [
                {"t": r["t"], "pos": r["pos"], "vel": r["vel"], "sun_dir": r["sun_dir"]}
                for r in rows
            ]
        },
    }

    out_path.write_text(json.dumps(payload, indent=2))
    print(f"Wrote {len(rows)} samples for '{object_name}' to {out_path}")


if __name__ == "__main__":
    main()
