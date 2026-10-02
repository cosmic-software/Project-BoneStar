#!/usr/bin/env python3
"""Refresh tle/<catalog>.tle from CelesTrak.

    python tools/update_tles.py

For each tle/*.tle, downloads the current TLE for that catalog number and replaces the file
only if the download is valid (name line + two 69-character element lines with correct
checksums and the same catalog number) and its epoch is newer than the one on disk.
Prints one line per spacecraft; exits non-zero if any download failed.
"""
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TLE_DIR = ROOT / "tle"
URL = "https://celestrak.org/NORAD/elements/gp.php?CATNR={}&FORMAT=TLE"


def parse(text):
    """Return (lines, catalog, epoch) for a valid 3-line TLE, else raise ValueError."""
    lines = [l.rstrip() for l in text.splitlines() if l.strip()]
    if len(lines) != 3 or not lines[1].startswith("1 ") or not lines[2].startswith("2 "):
        raise ValueError("expected a name line and two element lines")
    for line in lines[1:]:
        digits = sum(int(c) if c.isdigit() else (c == "-") for c in line[:68]) % 10
        if len(line) != 69 or digits != int(line[68]):
            raise ValueError(f"bad length or checksum in line {line[0]}")
    catalog = lines[1][2:7].strip()
    if lines[2][2:7].strip() != catalog:
        raise ValueError("catalog numbers differ between the element lines")
    yy, day = int(lines[1][18:20]), float(lines[1][20:32])
    epoch = datetime(2000 + yy if yy < 57 else 1900 + yy, 1, 1, tzinfo=timezone.utc) + timedelta(days=day - 1)
    return lines, catalog, epoch


def main():
    failed = 0
    for path in sorted(TLE_DIR.glob("*.tle")):
        try:
            _, catalog, old_epoch = parse(path.read_text())
        except ValueError as e:
            print(f"{path.name}: existing file invalid ({e}); skipped")
            failed += 1
            continue
        try:
            req = urllib.request.Request(URL.format(catalog), headers={"User-Agent": "BoneStar/1.0"})
            with urllib.request.urlopen(req, timeout=30) as resp:
                text = resp.read().decode("ascii", errors="replace")
            lines, new_catalog, new_epoch = parse(text)
            if new_catalog != catalog:
                raise ValueError(f"got catalog {new_catalog}")
        except Exception as e:
            print(f"{catalog}: download failed ({e}); kept TLE from {old_epoch:%Y-%m-%d %H:%M} UTC")
            failed += 1
            continue
        if new_epoch > old_epoch:
            path.write_text("\n".join(lines) + "\n")
            print(f"{catalog} {lines[0].strip()}: updated, epoch {old_epoch:%Y-%m-%d %H:%M} -> {new_epoch:%Y-%m-%d %H:%M} UTC")
        else:
            print(f"{catalog} {lines[0].strip()}: already current (epoch {old_epoch:%Y-%m-%d %H:%M} UTC)")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
