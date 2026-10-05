#!/usr/bin/env python3
"""Spacecraft that don't come from a TLE: spacecraft.xlsx (or .ods), sheet "Spacecraft".

    python tools/spacecraft.py      check the sheet and print what the fleet job will get

Earth spacecraft with a TLE stay as tle/<catalog number>.tle files. This sheet adds the rest,
one per row under the header row:

    Name | Body | Source | Epoch (UTC) | SMA (km) | ECC | INC (deg) | RAAN (deg) | AOP (deg) | TA (deg) | OEM file

- Body: Earth or Moon (blank = Earth): the body the spacecraft orbits.
- Source: Elements or OEM.
  - Elements: osculating Keplerian elements at Epoch, about Body. Earth: EarthMJ2000Eq axes.
    Moon: the Moon's equator of date at Epoch (Z = the lunar pole then, X = the ascending node
    of the lunar equator on the Earth's J2000 equator, as GMAT's BodyInertial but with the
    current pole), so INC 90 is a polar lunar orbit. GMAT propagates them with that body's gravity field
    (Earth: JGM-3 8x8; Moon: LP165P 10x10) plus the Earth / Moon and Sun as point masses.
    Epoch must be at or before the start of the run's window (an hour before the run).
  - OEM: a CCSDS OEM trajectory file, played back as given (no propagation): its path in the
    OEM file column, absolute or relative to the project folder. CENTER_NAME EARTH or MOON;
    REF_FRAME EME2000, ICRF or GCRF (treated alike: they differ by ~0.02 arcsec); TIME_SYSTEM
    UTC, TAI, TT, TDB or GPS. It should cover the window; outside what it covers the
    spacecraft holds its first / last position.
- Epoch: an Excel date-time cell, or text like 05 Oct 2026 00:00:00 or 2026-10-05T00:00:00.
"""
import math
import re
import sys
import zipfile
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path

from places import PlacesError, parse_body, pick_file, read_sheet, MOON_RADIUS_KM

ROOT = Path(__file__).resolve().parent.parent
SPACECRAFT_XLSX = ROOT / "spacecraft.xlsx"
MU = {"Earth": 398600.4415, "Moon": 4902.8005821478}          # km^3/s^2 (GMAT's values)
RADIUS = {"Earth": 6378.1363, "Moon": MOON_RADIUS_KM}
TAI_MINUS_UTC = 37.0          # s, since 1 Jan 2017 (no leap second announced since)
OFFSET_TO_UTC = {"UTC": 0.0, "TAI": -TAI_MINUS_UTC, "GPS": 19.0 - TAI_MINUS_UTC,
                 "TT": -32.184 - TAI_MINUS_UTC, "TDB": -32.184 - TAI_MINUS_UTC}   # TDB = TT within 2 ms


def parse_epoch(value):
    if isinstance(value, float):          # an Excel date: days since 30 Dec 1899
        return datetime(1899, 12, 30, tzinfo=timezone.utc) + timedelta(days=value)
    text = str(value or "").strip().replace("UTC", "").strip()
    for fmt in ("%d %b %Y %H:%M:%S.%f", "%d %b %Y %H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S",
                "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    raise PlacesError(f"Epoch '{value}': use a date-time cell, 05 Oct 2026 00:00:00 or 2026-10-05T00:00:00")


def number(value, what, lo=None, hi=None):
    try:
        x = float(value)
    except (TypeError, ValueError):
        raise PlacesError(f"{what} '{value}' is not a number")
    if (lo is not None and x < lo) or (hi is not None and x > hi):
        raise PlacesError(f"{what} {x:g} is outside {lo:g}..{hi:g}")
    return x


def slug(name):
    return "sc-" + (re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "craft")


def read_spacecraft(path=SPACECRAFT_XLSX):
    """[{name, tag, body, source, epoch, elements, oem}] -- [] if there is no sheet."""
    path = pick_file(path)
    if path is None:
        return []
    print(f"Reading {path.name}")
    try:
        rows = read_sheet(path, "Spacecraft")
    except (zipfile.BadZipFile, KeyError, ET.ParseError) as e:
        raise PlacesError(f"{path.name} could not be read as a spreadsheet ({e})")
    craft, errors, tags = [], [], set()
    for n, row in enumerate(rows[1:], start=2):
        row = (row + [None] * 11)[:11]
        if all(c is None or (isinstance(c, str) and not c.strip()) for c in row):
            continue
        name, body, source, epoch, sma, ecc, inc, raan, aop, ta, oem = row
        try:
            name = (f"{name:g}" if isinstance(name, float) else str(name or "")).strip()
            if not name:
                raise PlacesError("Name is empty")
            tag = slug(name)
            if tag in tags:
                raise PlacesError(f"Name '{name}' is used twice")
            body = parse_body(body)
            source = str(source or "").strip().lower()
            c = {"name": name, "tag": tag, "body": body}
            if source == "elements":
                c["source"] = "elements"
                c["epoch"] = parse_epoch(epoch)
                c["elements"] = {"SMA": number(sma, "SMA (km)", RADIUS[body], 1e7),
                                 "ECC": number(ecc, "ECC", 0, 0.999),
                                 "INC": number(inc, "INC (deg)", 0, 180),
                                 "RAAN": number(raan, "RAAN (deg)"), "AOP": number(aop, "AOP (deg)"),
                                 "TA": number(ta, "TA (deg)")}
                if c["elements"]["SMA"] * (1 - c["elements"]["ECC"]) <= RADIUS[body]:
                    raise PlacesError(f"perigee is inside the {body} (SMA x (1 - ECC) <= {RADIUS[body]} km)")
            elif source == "oem":
                c["source"] = "oem"
                file = Path(str(oem or "").strip())
                if not str(file):
                    raise PlacesError("OEM file is empty")
                c["oem"] = file if file.is_absolute() else ROOT / file
                if not c["oem"].exists():
                    raise PlacesError(f"OEM file {c['oem']} not found")
            else:
                raise PlacesError(f"Source '{source}' should be Elements or OEM")
            tags.add(tag)
            craft.append(c)
        except PlacesError as e:
            errors.append(f"{path.name} row {n}: {e}")
    if errors:
        raise PlacesError("\n".join(errors))
    return craft


PAYLOAD_FIELDS = [("tx_w", "Tx power (W)"), ("gain_dbi", "Antenna gain (dBi)"), ("rate_bps", "Downlink rate (bps)"),
                  ("ebn0_req_db", "Required Eb/N0 (dB)"), ("gt_dbk", "Rx G/T (dB/K)"),
                  ("up_rate_bps", "Uplink rate (bps)"), ("losses_db", "Other losses (dB)")]


def read_payloads(path=SPACECRAFT_XLSX, tags_by_name=None):
    """Sheet "Payloads": each spacecraft's radio for link budgets, one row per spacecraft:

        Spacecraft | Tx power (W) | Antenna gain (dBi) | Downlink rate (bps) | Required Eb/N0 (dB)
        | Rx G/T (dB/K) | Uplink rate (bps) | Other losses (dB)

    Spacecraft is its name or catalog number (TLE spacecraft too). The downlink needs Tx power,
    gain, rate and required Eb/N0; the uplink (2-way stations) needs Rx G/T and the uplink rate.
    Other losses (pointing, polarisation, atmosphere, implementation) default to 0.
    Returns {tag: {field: value or None}}; {} without the sheet."""
    path = pick_file(path)
    if path is None:
        return {}
    try:
        rows = read_sheet(path, "Payloads")
    except (zipfile.BadZipFile, KeyError, ET.ParseError) as e:
        raise PlacesError(f"{path.name} could not be read as a spreadsheet ({e})")
    if not rows or str((rows[0] + [None])[0] or "").strip() != "Spacecraft":
        return {}                                   # no Payloads sheet (read_sheet fell back to the first)
    tags_by_name = tags_by_name or {}
    payloads, errors = {}, []
    for n, row in enumerate(rows[1:], start=2):
        row = (row + [None] * 8)[:8]
        if all(c is None or (isinstance(c, str) and not c.strip()) for c in row):
            continue
        who = (f"{row[0]:g}" if isinstance(row[0], float) else str(row[0] or "")).strip()
        tag = tags_by_name.get(who, tags_by_name.get(who.lower()))
        if tag is None:
            errors.append(f"{path.name} Payloads row {n}: no spacecraft called '{who}' (use its name or catalog number)")
            continue
        try:
            p = {}
            for (key, title), cell in zip(PAYLOAD_FIELDS, row[1:]):
                p[key] = None if cell is None or (isinstance(cell, str) and not cell.strip()) else number(cell, title)
            for key in ("tx_w", "rate_bps", "up_rate_bps"):
                if p[key] is not None and p[key] <= 0:
                    raise PlacesError(f"{dict(PAYLOAD_FIELDS)[key]} must be above 0")
            p["losses_db"] = p["losses_db"] or 0.0
            payloads[tag] = p
        except PlacesError as e:
            errors.append(f"{path.name} Payloads row {n}: {e}")
    if errors:
        raise PlacesError("\n".join(errors))
    return payloads


def read_oem_file(path):
    """All segments of a CCSDS OEM: [{center, frame, time_system, rows: [(utc, pos, vel)]}]."""
    segments, seg, mode = [], None, None
    for raw in Path(path).read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("COMMENT"):
            continue
        if line == "META_START":
            seg, mode = {"center": None, "frame": None, "time_system": "UTC", "rows": []}, "meta"
            segments.append(seg)
        elif line == "META_STOP":
            mode = "data"
        elif line == "COVARIANCE_START":
            mode = "covariance"
        elif line == "COVARIANCE_STOP":
            mode = "data"
        elif mode == "meta" and "=" in line:
            key, value = [v.strip() for v in line.split("=", 1)]
            if key == "CENTER_NAME":
                seg["center"] = value.upper()
            elif key == "REF_FRAME":
                seg["frame"] = value.upper()
            elif key == "TIME_SYSTEM":
                seg["time_system"] = value.upper()
        elif mode == "data" and seg is not None and "=" not in line:
            f = line.split()
            if len(f) < 7:
                continue
            seg["rows"].append((oem_time(f[0], seg["time_system"]), [float(v) for v in f[1:4]],
                                [float(v) for v in f[4:7]]))
    if not segments:
        raise PlacesError(f"{Path(path).name}: no OEM segments (META_START ... META_STOP)")
    for s in segments:
        if s["center"] not in ("EARTH", "MOON"):
            raise PlacesError(f"{Path(path).name}: CENTER_NAME {s['center']} -- only EARTH or MOON")
        if s["frame"] not in ("EME2000", "ICRF", "GCRF"):
            raise PlacesError(f"{Path(path).name}: REF_FRAME {s['frame']} -- only EME2000, ICRF or GCRF")
        if s["time_system"] not in OFFSET_TO_UTC:
            raise PlacesError(f"{Path(path).name}: TIME_SYSTEM {s['time_system']} -- only "
                              + ", ".join(OFFSET_TO_UTC))
    return segments


def oem_time(text, system):
    """An OEM epoch (YYYY-MM-DDThh:mm:ss[.f] or YYYY-DDDThh:mm:ss[.f]) as a UTC datetime."""
    date, _, clock = text.partition("T")
    whole, _, frac = clock.partition(".")
    if date.count("-") == 1:                       # day-of-year form
        y, doy = date.split("-")
        day = datetime(int(y), 1, 1) + timedelta(days=int(doy) - 1)
    else:
        day = datetime.strptime(date, "%Y-%m-%d")
    h, m, s = [int(v) for v in whole.split(":")]
    t = day.replace(tzinfo=timezone.utc) + timedelta(hours=h, minutes=m, seconds=s + float("0." + (frac or "0")))
    return t + timedelta(seconds=OFFSET_TO_UTC[system])


def kepler(r, v, mu):
    """Osculating SMA, ECC, INC, RAAN, AOP, TA (km, deg), period (s), apoapsis and periapsis
    radius (km) from a body-centred state in MJ2000Eq-parallel axes (GMAT's definitions)."""
    def cross(a, b):
        return [a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0]]

    def dot(a, b):
        return sum(x * y for x, y in zip(a, b))
    rn, vn = math.sqrt(dot(r, r)), math.sqrt(dot(v, v))
    h = cross(r, v)
    hn = math.sqrt(dot(h, h))
    node = [-h[1], h[0], 0.0]
    nn = math.sqrt(dot(node, node))
    e = [((vn * vn - mu / rn) * r[i] - dot(r, v) * v[i]) / mu for i in range(3)]
    ecc = math.sqrt(dot(e, e))
    sma = 1 / (2 / rn - vn * vn / mu)
    inc = math.degrees(math.acos(max(-1, min(1, h[2] / hn))))
    raan = math.degrees(math.atan2(node[1], node[0])) % 360 if nn > 1e-9 else 0.0
    if ecc > 1e-11 and nn > 1e-9:
        aop = math.degrees(math.acos(max(-1, min(1, dot(node, e) / (nn * ecc)))))
        aop = aop if e[2] >= 0 else 360 - aop
        ta = math.degrees(math.acos(max(-1, min(1, dot(e, r) / (ecc * rn)))))
        ta = ta if dot(r, v) >= 0 else 360 - ta
    else:
        aop, ta = 0.0, math.degrees(math.atan2(dot(cross(node, r), h) / hn, dot(node, r))) % 360 if nn > 1e-9 else 0.0
    period = 2 * math.pi * math.sqrt(sma ** 3 / mu) if sma > 0 else float("inf")
    return [sma, ecc, inc, raan, aop, ta, period, sma * (1 + ecc), sma * (1 - ecc)]


if __name__ == "__main__":
    try:
        found = read_spacecraft()
    except PlacesError as e:
        sys.exit(str(e))
    if not found:
        sys.exit(f"no spacecraft in {SPACECRAFT_XLSX.name}")
    for c in found:
        if c["source"] == "elements":
            el = ", ".join(f"{k} {v:g}" for k, v in c["elements"].items())
            print(f"{c['name']} ({c['tag']}): {c['body']}, elements at {c['epoch']:%Y-%m-%d %H:%M:%S} UTC: {el}")
        else:
            segs = read_oem_file(c["oem"])
            rows = [r for s in segs for r in s["rows"]]
            print(f"{c['name']} ({c['tag']}): {c['body']}, OEM {c['oem'].name}: {len(segs)} segment(s), "
                  f"{len(rows)} states, {rows[0][0]:%Y-%m-%d %H:%M} to {rows[-1][0]:%Y-%m-%d %H:%M} UTC, "
                  f"centre {segs[0]['center']}, {segs[0]['frame']}, {segs[0]['time_system']}")
