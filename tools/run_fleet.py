#!/usr/bin/env python3
"""Fleet job: TLEs / elements / OEMs -> GMAT (headless) -> data/fleet.json for the viewer.

    python tools/run_fleet.py

Spacecraft come from tle/*.tle (Earth orbiters: name line + the two element lines,
propagated with GMAT's SGP4 propagator, SPICESGP4) and from spacecraft.xlsx (see
tools/spacecraft.py): Earth or Moon orbiters given as Keplerian elements (GMAT propagates them
with that body's gravity) or as OEM trajectory files (played back as given). GMAT writes one
CCSDS OEM per propagated spacecraft (EarthMJ2000Eq, Earth-centred -- lunar ones too); OEM-file
spacecraft centred on the Moon get the Moon's position added. Everything is merged into
data/fleet.json.

The window runs from 1 hour before the run to 12 hours after it, at 60 s steps, so a
late or missed run still leaves hours of track. GMAT converts SGP4's TEME output, so
the OEMs (and the JSON) are in EarthMJ2000Eq, the frame the viewer already uses. GMAT
also reports the Earth's rotation angle, the Sun's direction and the Moon's position and
orientation (data/frames.csv) so the viewer can turn the textured Earth, place and turn the
Moon, and light the day sides correctly.

It also reads places.xlsx and groundstations.xlsx (see tools/places.py) and adds their
body-fixed positions (Earth or Moon), so the viewer can turn them with their body.

This is the step to schedule (e.g. every 6 hours) once the TLEs are refreshed.
"""
import bisect
import json
import math
import struct
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from places import MOON_RADIUS_KM, PlacesError, read_ground_stations, read_places, write_models
from spacecraft import MU, kepler, read_oem_file, read_payloads, read_spacecraft
from instruments import prepare_models
from linkbudget import budget
from charts import station_chart

ROOT = Path(__file__).resolve().parent.parent
TLE_DIR = ROOT / "tle"
OEM_DIR = ROOT / "data" / "oem"
SCRIPT = ROOT / "data" / "fleet_run.script"
OUT_JSON = ROOT / "data" / "fleet.json"
FRAMES = ROOT / "data" / "frames.csv"
ELEMENTS_DIR = ROOT / "data" / "elements"
# Osculating orbital elements per spacecraft about the body it orbits. Earth: GMAT reports them
# (data/elements/<tag>.csv), INC / RAAN / AOP in EarthMJ2000Eq. Moon: in the Moon's equator of
# date (see moon_of_date), computed here from GMAT's trajectory with the same definitions.
ELEMENT_FIELDS = ["sma", "ecc", "inc", "raan", "aop", "ta", "period", "rapo", "rper"]
ELEMENT_PARAMS = ["Earth.SMA", "Earth.ECC", "EarthMJ2000Eq.INC", "EarthMJ2000Eq.RAAN", "EarthMJ2000Eq.AOP",
                  "Earth.TA", "Earth.OrbitPeriod", "Earth.RadApo", "Earth.RadPer"]
EPOCH_SCRIPT = ROOT / "data" / "epoch_frames.script"
EPOCH_FRAMES = ROOT / "data" / "epoch_frames.csv"
EARTH_RADIUS_KM = 6378.1363
TRACK_DIR = ROOT / "data" / "tracks"
CHART_DIR = ROOT / "data" / "charts"
CHART_STEP = 15.0               # s between samples in a pass's chart profile
MU_EARTH = 398600.4415          # km^3/s^2
KM_TO_UNITS = 1 / 100           # viewer scale: 1 unit = 100 km
TRACK_STEP = 20                 # s between orbit-line points (Hermite-interpolated)
TRACK_COLOURS = [(1.00, 0.62, 0.20), (0.35, 0.95, 0.55), (0.95, 0.40, 0.80),
                 (0.40, 0.75, 1.00), (1.00, 0.90, 0.30), (0.75, 0.55, 1.00)]
GMAT_CONSOLE = Path(os.environ.get("GMAT_CONSOLE", r"F:\gmat-win-R2026a\bin\GmatConsole.exe"))

EARTH_RATE_DEG_PER_S = 360.98564736629 / 86400.0   # sidereal rotation rate

WINDOW_BEFORE = timedelta(hours=1)
WINDOW_AFTER = timedelta(hours=12)
STEP_SECONDS = 60


def read_tles():
    """Return [(name, catalog_number, tle_path, tle_epoch_utc)] for each tle/*.tle."""
    fleet = []
    for path in sorted(TLE_DIR.glob("*.tle")):
        lines = [l.rstrip() for l in path.read_text().splitlines() if l.strip()]
        if len(lines) != 3 or not lines[1].startswith("1 ") or not lines[2].startswith("2 "):
            sys.exit(f"{path.name}: expected a name line and two element lines")
        for line in lines[1:]:
            digits = sum(int(c) if c.isdigit() else (c == "-") for c in line[:68]) % 10
            if len(line) != 69 or digits != int(line[68]):
                sys.exit(f"{path.name}: bad length or checksum in line {line[0]}")
        name = lines[0].strip()
        catalog = lines[1][2:7].strip()
        yy, day = int(lines[1][18:20]), float(lines[1][20:32])
        epoch = datetime(2000 + yy if yy < 57 else 1900 + yy, 1, 1, tzinfo=timezone.utc) + timedelta(days=day - 1)
        fleet.append((name, catalog, path, epoch))
    return fleet


def gmat_time(t):
    return t.strftime("%d %b %Y %H:%M:%S.") + f"{t.microsecond // 1000:03d}"


def write_script(crafts, start, end):
    """The GMAT run: every TLE and element spacecraft, plus FrameRef, a point-mass Earth
    orbiter on an exact 60 s grid across the window that carries the frame report (Earth
    rotation, Sun, Moon position and orientation) whatever the fleet is."""
    s = ["% Generated by tools/run_fleet.py -- edit that, not this.", ""]
    s += ["Create CoordinateSystem MoonFixed;", "GMAT MoonFixed.Origin = Luna;", "GMAT MoonFixed.Axes = BodyFixed;",
          "Create CoordinateSystem MoonMJ2000Eq;", "GMAT MoonMJ2000Eq.Origin = Luna;", "GMAT MoonMJ2000Eq.Axes = MJ2000Eq;", ""]
    s += ["Create Propagator TLEProp;",
          "GMAT TLEProp.Type = SPICESGP4;",
          f"GMAT TLEProp.InitialStepSize = {STEP_SECONDS};", ""]
    # Element spacecraft: the body's gravity field and the other bodies as point masses
    s += ["Create ForceModel FMEarth;", "GMAT FMEarth.CentralBody = Earth;", "GMAT FMEarth.PrimaryBodies = {Earth};",
          "GMAT FMEarth.GravityField.Earth.PotentialFile = 'JGM3.cof';",
          "GMAT FMEarth.GravityField.Earth.Degree = 8;", "GMAT FMEarth.GravityField.Earth.Order = 8;",
          "GMAT FMEarth.PointMasses = {Luna, Sun};",
          "Create ForceModel FMMoon;", "GMAT FMMoon.CentralBody = Luna;", "GMAT FMMoon.PrimaryBodies = {Luna};",
          "GMAT FMMoon.GravityField.Luna.PotentialFile = 'LP165P.cof';",
          "GMAT FMMoon.GravityField.Luna.Degree = 10;", "GMAT FMMoon.GravityField.Luna.Order = 10;",
          "GMAT FMMoon.PointMasses = {Earth, Sun};"]
    for body in ("Earth", "Moon"):
        s += [f"Create Propagator Prop{body};", f"GMAT Prop{body}.FM = FM{body};", f"GMAT Prop{body}.Type = RungeKutta89;",
              f"GMAT Prop{body}.InitialStepSize = 60;", f"GMAT Prop{body}.Accuracy = 1e-11;",
              f"GMAT Prop{body}.MinStep = 0.001;", f"GMAT Prop{body}.MaxStep = {STEP_SECONDS};"]
    s += ["Create ForceModel FMRef;", "GMAT FMRef.CentralBody = Earth;", "GMAT FMRef.PointMasses = {Earth};",
          "Create Propagator PropRef;", "GMAT PropRef.FM = FMRef;", "GMAT PropRef.Type = RungeKutta89;",
          f"GMAT PropRef.InitialStepSize = {STEP_SECONDS};", f"GMAT PropRef.MinStep = {STEP_SECONDS};",
          f"GMAT PropRef.MaxStep = {STEP_SECONDS};",
          "Create Spacecraft FrameRef;", "GMAT FrameRef.DateFormat = UTCGregorian;",
          f"GMAT FrameRef.Epoch = '{gmat_time(start)}';", "GMAT FrameRef.CoordinateSystem = EarthMJ2000Eq;",
          "GMAT FrameRef.DisplayStateType = Keplerian;", "GMAT FrameRef.SMA = 7000;", "GMAT FrameRef.ECC = 0.001;",
          "GMAT FrameRef.INC = 30;", "GMAT FrameRef.RAAN = 0;", "GMAT FrameRef.AOP = 0;", "GMAT FrameRef.TA = 0;", ""]
    for c in crafts:
        if c["source"] == "oem":
            continue
        sc, tag = c["gmat"], c["tag"]
        if c["source"] == "tle":
            s += [f"Create Spacecraft {sc};",
                  f"GMAT {sc}.EphemerisName = '{c['tle'].as_posix()}';",
                  f"GMAT {sc}.Id = '{tag}';"]                     # must match the TLE's catalog number
        elif c["body"] == "Earth":
            el = c["elements"]
            s += [f"Create Spacecraft {sc};", f"GMAT {sc}.DateFormat = UTCGregorian;",
                  f"GMAT {sc}.Epoch = '{gmat_time(c['epoch'])}';",
                  f"GMAT {sc}.CoordinateSystem = EarthMJ2000Eq;",
                  f"GMAT {sc}.DisplayStateType = Keplerian;"]
            s += [f"GMAT {sc}.{k} = {v!r};" for k, v in el.items()]
        else:
            # Moon: the elements are in the Moon's equator of date at the epoch; main() turned
            # them into this Moon-centred MJ2000Eq Cartesian state
            x = c["state_mj2000"]
            s += [f"Create Spacecraft {sc};", f"GMAT {sc}.DateFormat = UTCGregorian;",
                  f"GMAT {sc}.Epoch = '{gmat_time(c['epoch'])}';",
                  f"GMAT {sc}.CoordinateSystem = MoonMJ2000Eq;",
                  f"GMAT {sc}.DisplayStateType = Cartesian;"]
            s += [f"GMAT {sc}.{k} = {v!r};" for k, v in zip(("X", "Y", "Z", "VX", "VY", "VZ"), x)]
        s += [f"Create EphemerisFile OEM{sc};",
              f"GMAT OEM{sc}.Spacecraft = {sc};",
              f"GMAT OEM{sc}.Filename = '{(OEM_DIR / (tag + '.oem')).as_posix()}';",
              f"GMAT OEM{sc}.FileFormat = CCSDS-OEM;",
              f"GMAT OEM{sc}.EpochFormat = UTCGregorian;",
              f"GMAT OEM{sc}.InitialEpoch = '{gmat_time(start)}';",
              f"GMAT OEM{sc}.FinalEpoch = '{gmat_time(end)}';",
              f"GMAT OEM{sc}.StepSize = {STEP_SECONDS};",
              f"GMAT OEM{sc}.Interpolator = Lagrange;",
              f"GMAT OEM{sc}.InterpolationOrder = 7;",
              f"GMAT OEM{sc}.CoordinateSystem = EarthMJ2000Eq;",
              f"GMAT OEM{sc}.WriteEphemeris = true;", ""]
        if c["body"] != "Earth":
            continue
        s += [f"Create ReportFile Elem{sc};",
              f"GMAT Elem{sc}.Filename = '{(ELEMENTS_DIR / (tag + '.csv')).as_posix()}';",
              f"GMAT Elem{sc}.Add = {{{sc}.UTCGregorian, "
              + ", ".join(f"{sc}.{param}" for param in ELEMENT_PARAMS) + "};",
              f"GMAT Elem{sc}.Precision = 16;",
              f"GMAT Elem{sc}.WriteHeaders = true;",
              f"GMAT Elem{sc}.FixedWidth = false;",
              f"GMAT Elem{sc}.Delimiter = ',';", ""]
    # Frame report, from FrameRef: its inertial and Earth-fixed position give the Earth's
    # rotation angle; the Sun's position the lighting; the Moon's position / velocity, and the
    # Earth's and Sun's positions in the Moon-fixed frame, the Moon's place and orientation.
    ref = "FrameRef"
    s += ["Create ReportFile Frames;",
          f"GMAT Frames.Filename = '{FRAMES.as_posix()}';",
          f"GMAT Frames.Add = {{{ref}.UTCGregorian, {ref}.EarthMJ2000Eq.X, {ref}.EarthMJ2000Eq.Y, "
          f"{ref}.EarthFixed.X, {ref}.EarthFixed.Y, Sun.EarthMJ2000Eq.X, Sun.EarthMJ2000Eq.Y, "
          f"Sun.EarthMJ2000Eq.Z, Luna.EarthMJ2000Eq.X, Luna.EarthMJ2000Eq.Y, Luna.EarthMJ2000Eq.Z, "
          f"Luna.EarthMJ2000Eq.VX, Luna.EarthMJ2000Eq.VY, Luna.EarthMJ2000Eq.VZ, Earth.MoonFixed.X, "
          f"Earth.MoonFixed.Y, Earth.MoonFixed.Z, Sun.MoonFixed.X, Sun.MoonFixed.Y, Sun.MoonFixed.Z}};",
          "GMAT Frames.Precision = 16;",
          "GMAT Frames.WriteHeaders = true;",
          "GMAT Frames.FixedWidth = false;",
          "GMAT Frames.Delimiter = ',';", ""]
    s += ["BeginMissionSequence;",
          # first, so the frame report's rows are FrameRef's 60 s grid (rows written while the
          # other spacecraft propagate repeat FrameRef's last epoch and are skipped)
          f"Propagate PropRef(FrameRef) {{FrameRef.ElapsedSecs = {(end - start).total_seconds() + STEP_SECONDS:.3f}}};"]
    # One Propagate per spacecraft: each has its own epoch
    for c in crafts:
        if c["source"] == "oem":
            continue
        secs = (end - c["epoch"]).total_seconds() + STEP_SECONDS
        prop = "TLEProp" if c["source"] == "tle" else f"Prop{c['body']}"
        s += [f"Propagate {prop}({c['gmat']}) {{{c['gmat']}.ElapsedSecs = {secs:.3f}}};"]
    SCRIPT.write_text("\n".join(s) + "\n")


def read_oem(path):
    """Return (frame, [(utc datetime, [x,y,z], [vx,vy,vz])]) from a CCSDS OEM (km, km/s)."""
    frame, rows, in_data = None, [], False
    for line in path.read_text().splitlines():
        line = line.strip()
        if line.startswith("REF_FRAME"):
            frame = line.split("=")[1].strip()
        elif line == "META_STOP":
            in_data = True
        elif line == "META_START":
            in_data = False
        elif in_data and line and not line.startswith("COMMENT"):
            f = line.split()
            t = datetime.fromisoformat(f[0]).replace(tzinfo=timezone.utc)
            rows.append((t, [float(v) for v in f[1:4]], [float(v) for v in f[4:7]]))
    return frame, rows


def read_frames(start, now):
    """From GMAT's frame report: the Earth's rotation angle at the window start, the Sun's
    direction at run time, and the Moon samples.

    The rotation angle is FrameRef's inertial longitude minus its Earth-fixed longitude, i.e.
    where the Greenwich meridian points in EarthMJ2000Eq. Each row gives one estimate; they are
    reduced to the window start with the sidereal rate and averaged. (The viewer spins the
    Earth about the J2000 pole; the true pole is ~0.15 deg away, ~16 km at the surface.)

    Moon samples, one per row: [t, x, y, z, vx, vy, vz] (Earth-centred EarthMJ2000Eq, km, km/s)
    and R, the Moon-fixed -> EarthMJ2000Eq rotation, solved from the Earth's and the Sun's
    directions seen from the Moon in both frames (TRIAD).
    """
    rows, seen = [], set()
    lines = FRAMES.read_text().splitlines()
    for line in lines[1:]:
        f = [v.strip() for v in line.split(",")]
        if len(f) != 20 or f[0] in seen:
            continue  # the report repeats rows while the other spacecraft propagate
        seen.add(f[0])
        t = datetime.strptime(f[0], "%d %b %Y %H:%M:%S.%f").replace(tzinfo=timezone.utc)
        rows.append((t, [float(v) for v in f[1:]]))
    estimates, sun, moon = [], None, []
    for t, v in rows:
        xj, yj, xf, yf, sx, sy, sz = v[:7]
        dt = (t - start).total_seconds()
        if dt < 0:
            continue
        angle = math.degrees(math.atan2(yj, xj) - math.atan2(yf, xf)) - EARTH_RATE_DEG_PER_S * dt
        estimates.append(angle % 360.0)
        if sun is None or abs((t - now).total_seconds()) < abs((sun[0] - now).total_seconds()):
            sun = (t, (sx, sy, sz))
        lpos, lvel, earth_mf, sun_mf = v[7:10], v[10:13], v[13:16], v[16:19]
        earth_in = [-c for c in lpos]                                  # Earth seen from the Moon
        sun_in = [(sx, sy, sz)[k] - lpos[k] for k in range(3)]          # Sun seen from the Moon
        moon.append({"t": dt, "pos": lpos, "vel": lvel, "R": triad(earth_in, sun_in, earth_mf, sun_mf)})
    # circular mean, then the spread of the estimates as a check
    mean = math.degrees(math.atan2(sum(math.sin(math.radians(a)) for a in estimates),
                                   sum(math.cos(math.radians(a)) for a in estimates))) % 360.0
    spread = max(abs((a - mean + 180) % 360 - 180) for a in estimates)
    norm = math.sqrt(sum(c * c for c in sun[1]))
    return mean, spread, len(estimates), [c / norm for c in sun[1]], moon


def triad(a1, a2, b1, b2):
    """Rotation matrix R (rows) with R b = a, from two vector pairs (a in the target frame, b
    in the source frame), a1 / b1 exact, a2 / b2 fixing the roll."""
    def unit(v):
        n = math.sqrt(sum(c * c for c in v))
        return [c / n for c in v]

    def cross(u, w):
        return [u[1] * w[2] - u[2] * w[1], u[2] * w[0] - u[0] * w[2], u[0] * w[1] - u[1] * w[0]]
    ta = [unit(a1)]; ta.append(unit(cross(a1, a2))); ta.append(cross(ta[0], ta[1]))
    tb = [unit(b1)]; tb.append(unit(cross(b1, b2))); tb.append(cross(tb[0], tb[1]))
    return [[sum(ta[k][i] * tb[k][j] for k in range(3)) for j in range(3)] for i in range(3)]


def basis_quat(r, u, f):
    """Quaternion [x, y, z, w] of the rotation whose matrix has columns r, u, f (as orbit.js
    BasisQuat, so the viewer can use it directly)."""
    m00, m10, m20, m01, m11, m21, m02, m12, m22 = r[0], r[1], r[2], u[0], u[1], u[2], f[0], f[1], f[2]
    tr = m00 + m11 + m22
    if tr > 0:
        S = math.sqrt(tr + 1) * 2
        return [(m21 - m12) / S, (m02 - m20) / S, (m10 - m01) / S, 0.25 * S]
    if m00 > m11 and m00 > m22:
        S = math.sqrt(1 + m00 - m11 - m22) * 2
        return [0.25 * S, (m01 + m10) / S, (m02 + m20) / S, (m21 - m12) / S]
    if m11 > m22:
        S = math.sqrt(1 + m11 - m00 - m22) * 2
        return [(m01 + m10) / S, 0.25 * S, (m12 + m21) / S, (m02 - m20) / S]
    S = math.sqrt(1 + m22 - m00 - m11) * 2
    return [(m02 + m20) / S, (m12 + m21) / S, 0.25 * S, (m10 - m01) / S]


def unity_quat(R):
    """The Moon entity's rotation in the viewer: R (Moon-fixed -> EarthMJ2000Eq) with both
    frames' Y and Z swapped, as the Unity axes are (X, Z, Y) of GMAT's; moon.glb's local axes
    are the Moon-fixed ones in the same (X, Z, Y) order."""
    M = [[R[0][0], R[0][2], R[0][1]], [R[2][0], R[2][2], R[2][1]], [R[1][0], R[1][2], R[1][1]]]
    cols = [[M[i][j] for i in range(3)] for j in range(3)]
    return [round(c, 9) for c in basis_quat(*cols)]


def moon_of_date(R):
    """The Moon's equator of date as a rotation matrix (columns = its X, Y, Z axes in
    EarthMJ2000Eq): Z = the Moon's pole at that moment (Moon-fixed Z, from R), X = the ascending
    node of the lunar equator on the Earth's J2000 equator (Z_J2000 x pole) -- GMAT's
    BodyInertial construction, but with the current pole instead of the J2000 one."""
    z = [R[0][2], R[1][2], R[2][2]]
    n = math.sqrt(z[0] ** 2 + z[1] ** 2)
    x = [-z[1] / n, z[0] / n, 0.0]
    y = [z[1] * x[2] - z[2] * x[1], z[2] * x[0] - z[0] * x[2], z[0] * x[1] - z[1] * x[0]]
    return [[x[i], y[i], z[i]] for i in range(3)]


def elements_to_state(el, mu):
    """Position and velocity (km, km/s) from SMA, ECC, INC, RAAN, AOP, TA (km, deg)."""
    a, e = el["SMA"], el["ECC"]
    i, O, w, nu = (math.radians(el[k]) for k in ("INC", "RAAN", "AOP", "TA"))
    p = a * (1 - e * e)
    r = p / (1 + e * math.cos(nu))
    pf = [r * math.cos(nu), r * math.sin(nu), 0.0]
    vf = [-math.sqrt(mu / p) * math.sin(nu), math.sqrt(mu / p) * (e + math.cos(nu)), 0.0]
    cO, sO, ci, si, cw, sw = math.cos(O), math.sin(O), math.cos(i), math.sin(i), math.cos(w), math.sin(w)
    Q = [[cO * cw - sO * sw * ci, -cO * sw - sO * cw * ci, sO * si],
         [sO * cw + cO * sw * ci, -sO * sw + cO * cw * ci, -cO * si],
         [sw * si, cw * si, ci]]
    mul = lambda v: [sum(Q[r_][k] * v[k] for k in range(2)) for r_ in range(3)]
    return mul(pf), mul(vf)


def epoch_moon_frames(epochs):
    """R (Moon-fixed -> EarthMJ2000Eq) at each epoch, from a short GMAT run: one point-mass
    reference spacecraft per epoch reporting the Earth and the Sun seen from the Moon."""
    s = ["% Generated by tools/run_fleet.py -- edit that, not this.",
         "Create CoordinateSystem MoonFixed;", "GMAT MoonFixed.Origin = Luna;", "GMAT MoonFixed.Axes = BodyFixed;",
         "Create ForceModel FMRef;", "GMAT FMRef.CentralBody = Earth;", "GMAT FMRef.PointMasses = {Earth};",
         "Create Propagator PropRef;", "GMAT PropRef.FM = FMRef;", "GMAT PropRef.Type = RungeKutta89;",
         "GMAT PropRef.InitialStepSize = 60;", "GMAT PropRef.MinStep = 60;", "GMAT PropRef.MaxStep = 60;"]
    for k, t in enumerate(epochs):
        s += [f"Create Spacecraft Ref{k};", f"GMAT Ref{k}.DateFormat = UTCGregorian;", f"GMAT Ref{k}.Epoch = '{gmat_time(t)}';",
              f"GMAT Ref{k}.CoordinateSystem = EarthMJ2000Eq;", f"GMAT Ref{k}.DisplayStateType = Keplerian;",
              f"GMAT Ref{k}.SMA = 7000;", f"GMAT Ref{k}.ECC = 0.001;", f"GMAT Ref{k}.INC = 30;",
              f"Create ReportFile Rep{k};", f"GMAT Rep{k}.Filename = '{(EPOCH_FRAMES.parent / f'epoch_frames_{k}.csv').as_posix()}';",
              f"GMAT Rep{k}.Add = {{Ref{k}.UTCGregorian, Luna.EarthMJ2000Eq.X, Luna.EarthMJ2000Eq.Y, Luna.EarthMJ2000Eq.Z, "
              "Sun.EarthMJ2000Eq.X, Sun.EarthMJ2000Eq.Y, Sun.EarthMJ2000Eq.Z, Earth.MoonFixed.X, Earth.MoonFixed.Y, "
              "Earth.MoonFixed.Z, Sun.MoonFixed.X, Sun.MoonFixed.Y, Sun.MoonFixed.Z};",
              f"GMAT Rep{k}.Precision = 16;", f"GMAT Rep{k}.WriteHeaders = true;", f"GMAT Rep{k}.FixedWidth = false;",
              f"GMAT Rep{k}.Delimiter = ',';"]
    s += ["BeginMissionSequence;"] + [f"Propagate PropRef(Ref{k}) {{Ref{k}.ElapsedSecs = 60}};" for k in range(len(epochs))]
    EPOCH_SCRIPT.write_text("\n".join(s) + "\n")
    run = subprocess.run([str(GMAT_CONSOLE), "--run", str(EPOCH_SCRIPT), "--exit"],
                         cwd=GMAT_CONSOLE.parent, capture_output=True, text=True)
    if "successful" not in run.stdout:
        sys.exit("GMAT epoch-frame run failed:\n" + run.stdout[-2000:] + run.stderr[-2000:])
    out = []
    for k, t in enumerate(epochs):
        path = EPOCH_FRAMES.parent / f"epoch_frames_{k}.csv"
        row = next(l for l in path.read_text().splitlines()[1:]
                   if l.split(",")[0].strip() == gmat_time(t))            # the row at the epoch itself
        v = [float(x) for x in row.split(",")[1:]]
        lpos, sun, earth_mf, sun_mf = v[0:3], v[3:6], v[6:9], v[9:12]
        out.append(triad([-c for c in lpos], [sun[i] - lpos[i] for i in range(3)], earth_mf, sun_mf))
        path.unlink()
    return out


def moon_state(moon, t):
    """The Moon's Earth-centred position and velocity (km, km/s) and R at time t, by Hermite
    (position) / linear (velocity, R) interpolation between the 60 s samples."""
    times = [m["t"] for m in moon]
    i = max(0, min(len(moon) - 2, bisect.bisect_right(times, t) - 1))
    a, b = moon[i], moon[i + 1]
    pos = hermite((a["t"], a["pos"], a["vel"]), (b["t"], b["pos"], b["vel"]), t)
    u = (t - a["t"]) / (b["t"] - a["t"])
    vel = [a["vel"][k] + (b["vel"][k] - a["vel"][k]) * u for k in range(3)]
    R = [[a["R"][r][c] + (b["R"][r][c] - a["R"][r][c]) * u for c in range(3)] for r in range(3)]
    return pos, vel, R


def read_elements(catalog, start, end):
    """GMAT's element report for one spacecraft, inside the window: [[t, sma, ecc, ...]] with
    t in seconds after start (km, deg, s). Rows written while another spacecraft propagates
    repeat an epoch and are skipped, as in read_frames."""
    rows, seen = [], set()
    for line in (ELEMENTS_DIR / f"{catalog}.csv").read_text().splitlines()[1:]:
        f = [v.strip() for v in line.split(",")]
        if len(f) != 1 + len(ELEMENT_FIELDS) or f[0] in seen:
            continue
        seen.add(f[0])
        t = datetime.strptime(f[0], "%d %b %Y %H:%M:%S.%f").replace(tzinfo=timezone.utc)
        if start - timedelta(seconds=STEP_SECONDS) <= t <= end + timedelta(seconds=STEP_SECONDS):
            rows.append([round((t - start).total_seconds(), 3)] + [float(v) for v in f[1:]])
    rows.sort()
    return rows


PASS_SAMPLE = 10.0     # s between elevation samples while searching for passes


def find_passes(objects, names, stations, rotation0, rate, moon):
    """Passes of each spacecraft through each ground station's beam over the window:
    [{station, catalog, name, aos, los, max_t, max_el}] (t in seconds after the window epoch,
    elevation in deg), sorted by AOS. The beam is the station's cone of fov_deg about straight
    up, i.e. elevation >= 90 - fov/2, and the line of sight must miss the Earth and the Moon
    (spheres): the same tests the viewer uses to draw link lines, with the same Earth rotation
    (rotation0 + rate * t about the J2000 pole) and Moon samples. AOS / LOS are refined to
    0.05 s and the maximum to ~0.1 s."""
    passes = []
    for st in stations:
        lat, lon = math.radians(st["lat"]), math.radians(st["lon"])
        up_f = (math.cos(lat) * math.cos(lon), math.cos(lat) * math.sin(lon), math.sin(lat))
        site_f = st["ecef_km"]
        mask = 90.0 - st["fov_deg"] / 2.0
        on_moon = st.get("body") == "Moon"

        def station(t):
            """Station position and up, Earth-centred inertial."""
            if on_moon:
                mpos, _, R = moon_state(moon, t)
                return ([mpos[i] + sum(R[i][j] * site_f[j] for j in range(3)) for i in range(3)],
                        [sum(R[i][j] * up_f[j] for j in range(3)) for i in range(3)])
            a = math.radians(rotation0 + rate * t)
            c, s_ = math.cos(a), math.sin(a)
            rot = lambda v: [c * v[0] - s_ * v[1], s_ * v[0] + c * v[1], v[2]]   # Earth-fixed -> inertial
            return rot(site_f), rot(up_f)

        for catalog, track in objects.items():
            states = [(o["t"], o["pos"], o["vel"]) for o in track]
            times = [x[0] for x in states]

            def elevation(t):
                """Elevation (deg) of the spacecraft above the station's horizon; -90 while the
                Earth or the Moon (other than the station's own body) blocks the line of sight."""
                i = max(0, min(len(states) - 2, bisect.bisect_right(times, t) - 1))
                x = hermite(states[i], states[i + 1], t)
                p, up = station(t)
                d = [x[k] - p[k] for k in range(3)]
                dist = math.sqrt(sum(v * v for v in d))
                blockers = [((0.0, 0.0, 0.0), EARTH_RADIUS_KM)] if on_moon else [(moon_state(moon, t)[0], MOON_RADIUS_KM)]
                for centre, radius in blockers:
                    if segment_hits_sphere(p, x, centre, radius):
                        return -90.0
                return math.degrees(math.asin(sum(d[k] * up[k] for k in range(3)) / dist))

            def edge(t_out, t_in):                               # bisect the mask crossing
                while abs(t_in - t_out) > 0.05:
                    mid = (t_in + t_out) / 2
                    if elevation(mid) >= mask:
                        t_in = mid
                    else:
                        t_out = mid
                return (t_in + t_out) / 2

            t0, t1 = times[0], times[-1]
            t, prev_in, aos = t0, elevation(t0) >= mask, None
            if prev_in:
                aos = t0
            while t < t1:
                tn = min(t + PASS_SAMPLE, t1)
                now_in = elevation(tn) >= mask
                if now_in and not prev_in:
                    aos = edge(t, tn)
                if prev_in and not now_in or (now_in and tn >= t1):
                    los = edge(tn, t) if not now_in else t1
                    lo, hi = aos, los                            # golden-section search for the peak
                    g = (math.sqrt(5) - 1) / 2
                    while hi - lo > 0.1:
                        m1, m2 = hi - g * (hi - lo), lo + g * (hi - lo)
                        if elevation(m1) < elevation(m2):
                            lo = m1
                        else:
                            hi = m2
                    max_t = (lo + hi) / 2
                    passes.append({"station": st["name"], "catalog": catalog, "name": names[catalog],
                                   "aos": round(aos, 2), "los": round(los, 2), "max_t": round(max_t, 1),
                                   "max_el": round(elevation(max_t), 2)})
                prev_in, t = now_in, tn
    return sorted(passes, key=lambda x: x["aos"])


def look(st, track, t, rotation0, rate, moon):
    """(elevation deg, range km) of a spacecraft from a station at time t -- the geometry of
    find_passes (same station position / up, same Earth rotation and Moon samples), for the
    charts' pass profiles."""
    lat, lon = math.radians(st["lat"]), math.radians(st["lon"])
    up_f = (math.cos(lat) * math.cos(lon), math.cos(lat) * math.sin(lon), math.sin(lat))
    site_f = st["ecef_km"]
    if st.get("body") == "Moon":
        mpos, _, R = moon_state(moon, t)
        p = [mpos[i] + sum(R[i][j] * site_f[j] for j in range(3)) for i in range(3)]
        up = [sum(R[i][j] * up_f[j] for j in range(3)) for i in range(3)]
    else:
        a = math.radians(rotation0 + rate * t)
        c, s_ = math.cos(a), math.sin(a)
        p = [c * site_f[0] - s_ * site_f[1], s_ * site_f[0] + c * site_f[1], site_f[2]]
        up = [c * up_f[0] - s_ * up_f[1], s_ * up_f[0] + c * up_f[1], up_f[2]]
    states = [(o["t"], o["pos"], o["vel"]) for o in track]
    times = [x[0] for x in states]
    i = max(0, min(len(states) - 2, bisect.bisect_right(times, t) - 1))
    x = hermite(states[i], states[i + 1], t)
    d = [x[k] - p[k] for k in range(3)]
    dist = math.sqrt(sum(v * v for v in d))
    return math.degrees(math.asin(sum(d[k] * up[k] for k in range(3)) / dist)), dist


def segment_hits_sphere(a, b, centre, radius):
    """Whether the straight segment a-b passes through the sphere."""
    d = [b[k] - a[k] for k in range(3)]
    f = [a[k] - centre[k] for k in range(3)]
    dd = sum(v * v for v in d)
    t = max(0.0, min(1.0, -sum(f[k] * d[k] for k in range(3)) / dd))
    closest = [f[k] + d[k] * t for k in range(3)]
    return sum(v * v for v in closest) < radius * radius


def hermite(a, b, t):
    """Position (km) between two states at time t, using their velocities."""
    h = b[0] - a[0]; u = (t - a[0]) / h
    h00, h10 = 2 * u ** 3 - 3 * u ** 2 + 1, u ** 3 - 2 * u ** 2 + u
    h01, h11 = -2 * u ** 3 + 3 * u ** 2, u ** 3 - u ** 2
    return [h00 * a[1][k] + h10 * h * a[2][k] + h01 * b[1][k] + h11 * h * b[2][k] for k in range(3)]


def write_track(catalog, states, centre_t, colour, name, mu=MU_EARTH):
    """data/tracks/<name>.glb: one orbital period of the trajectory, centred on centre_t, as
    an unlit, opaque glTF LINE_STRIP in viewer units (GMAT X,Y,Z -> Unity X,Z,Y; glTF X is then
    negated because glTFast mirrors X on import). states are relative to the body orbited
    (Earth- or Moon-centred), mu is that body's."""
    t0, p0, v0 = states[0]
    r, v2 = math.sqrt(sum(c * c for c in p0)), sum(c * c for c in v0)
    a = 1 / (2 / r - v2 / mu)
    period = 2 * math.pi * math.sqrt(a ** 3 / mu)
    lo = max(states[0][0], centre_t - period / 2)
    hi = min(states[-1][0], lo + period)
    pts, i, t = [], 0, lo
    while t <= hi:
        while i < len(states) - 2 and states[i + 1][0] <= t:
            i += 1
        x, y, z = hermite(states[i], states[i + 1], t)
        pts.append((-x * KM_TO_UNITS, z * KM_TO_UNITS, y * KM_TO_UNITS))
        t += TRACK_STEP
    data = b"".join(struct.pack("<3f", *q) for q in pts)
    gltf = {
        "asset": {"version": "2.0", "generator": "BoneStar tools/run_fleet.py"},
        "extensionsUsed": ["KHR_materials_unlit"],
        "scene": 0, "scenes": [{"nodes": [0]}], "nodes": [{"mesh": 0, "name": f"Track{catalog}"}],
        "meshes": [{"name": f"Track{catalog}", "primitives": [{"attributes": {"POSITION": 0}, "mode": 3, "material": 0}]}],
        # BLEND (at full opacity): the runtime's directional sun casts shadows from opaque
        # meshes, lines included, which laid dark bands across the Moon and the Earth; blended
        # materials cast none (as the atmosphere shells show)
        "materials": [{"name": "Track", "alphaMode": "BLEND", "extensions": {"KHR_materials_unlit": {}},
                       "pbrMetallicRoughness": {"baseColorFactor": list(colour) + [1], "metallicFactor": 0, "roughnessFactor": 1}}],
        "buffers": [{"byteLength": len(data)}],
        "bufferViews": [{"buffer": 0, "byteOffset": 0, "byteLength": len(data), "target": 34962}],
        "accessors": [{"bufferView": 0, "componentType": 5126, "count": len(pts), "type": "VEC3",
                       "min": [min(q[k] for q in pts) for k in range(3)], "max": [max(q[k] for q in pts) for k in range(3)]}],
    }
    js = json.dumps(gltf, separators=(",", ":")).encode()
    js += b" " * (-len(js) % 4)
    glb = struct.pack("<III", 0x46546C67, 2, 12 + 8 + len(js) + 8 + len(data))
    glb += struct.pack("<II", len(js), 0x4E4F534A) + js + struct.pack("<II", len(data), 0x004E4942) + data
    TRACK_DIR.mkdir(parents=True, exist_ok=True)
    (TRACK_DIR / f"{name}.glb").write_bytes(glb)
    return period, len(pts)


def main():
    tles = read_tles()
    try:
        sheet = read_spacecraft()
        places = read_places()      # before GMAT, so a bad row stops the job early
        stations = read_ground_stations()
    except PlacesError as e:
        sys.exit(str(e))
    write_models()
    try:
        instruments = prepare_models()      # cameras in models/source/*.glb; camera-free copies served
    except (ValueError, KeyError) as e:
        sys.exit(f"models/source: {e}")
    for model, cams in instruments.items():
        for cam in cams:
            print(f"Instrument camera {cam['name']} in {model}: vertical FOV {cam['yfov_deg']:g} deg")
    now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    start, end = now - WINDOW_BEFORE, now + WINDOW_AFTER

    # one list: TLE spacecraft (tag = catalog number) then the sheet's (tag = sc-<name>)
    crafts = [{"tag": catalog, "name": name, "body": "Earth", "source": "tle", "tle": path, "epoch": epoch,
               "gmat": f"SC{catalog}"} for name, catalog, path, epoch in tles]
    for k, c in enumerate(sheet):
        if c["tag"] in {x["tag"] for x in crafts}:
            sys.exit(f"spacecraft {c['name']}: its tag {c['tag']} is already used")
        crafts.append(dict(c, gmat=f"SCE{k + 1}"))
    if not crafts:
        sys.exit(f"no spacecraft: no TLEs in {TLE_DIR} and no spacecraft sheet")
    # radios for link budgets (spacecraft.xlsx, sheet Payloads), by name or catalog number
    names_to_tags = {}
    for c in crafts:
        names_to_tags.update({c["name"]: c["tag"], c["name"].lower(): c["tag"], c["tag"]: c["tag"]})
    try:
        payloads = read_payloads(tags_by_name=names_to_tags)
    except PlacesError as e:
        sys.exit(str(e))
    for tag, pl in payloads.items():
        print(f"Payload radio for {tag}: " + ", ".join(f"{k} {v:g}" for k, v in pl.items() if v is not None))
    for c in crafts:
        if c["source"] != "oem" and c["epoch"] > start:
            kind = "TLE epoch" if c["source"] == "tle" else "Epoch"
            sys.exit(f"{c['name']}: {kind} {c['epoch']:%Y-%m-%d %H:%M:%S} UTC is after the window start "
                     f"{start:%Y-%m-%d %H:%M} UTC; it must be at or before it")
    # Moon elements are in the Moon's equator of date at their epoch: get the Moon's orientation
    # then from GMAT and turn them into a Moon-centred MJ2000Eq state for the main run
    lunar = [c for c in crafts if c["source"] == "elements" and c["body"] == "Moon"]
    if lunar:
        for c, R in zip(lunar, epoch_moon_frames([c["epoch"] for c in lunar])):
            B = moon_of_date(R)
            p, v = elements_to_state(c["elements"], MU["Moon"])
            turn = lambda x: [sum(B[i][j] * x[j] for j in range(3)) for i in range(3)]
            c["state_mj2000"] = turn(p) + turn(v)
            tilt = math.degrees(math.acos(max(-1, min(1, B[2][2]))))
            print(f"{c['name']}: elements in the Moon's equator of date at {c['epoch']:%Y-%m-%d %H:%M} UTC "
                  f"(lunar pole {tilt:.3f} deg from the Earth's J2000 pole)")
    OEM_DIR.mkdir(parents=True, exist_ok=True)
    for old in OEM_DIR.glob("*.oem"):
        old.unlink()
    ELEMENTS_DIR.mkdir(parents=True, exist_ok=True)
    for old in ELEMENTS_DIR.glob("*.csv"):
        old.unlink()
    write_script(crafts, start, end)

    run = subprocess.run([str(GMAT_CONSOLE), "--run", str(SCRIPT), "--exit"],
                         cwd=GMAT_CONSOLE.parent, capture_output=True, text=True)
    if "successful" not in run.stdout:
        sys.exit("GMAT run failed:\n" + run.stdout[-2000:] + run.stderr[-2000:])

    rotation, spread, n, sun_dir, moon = read_frames(start, now)
    print(f"Earth rotation at window start {rotation:.4f} deg ({n} GMAT samples, max spread "
          f"{spread:.4f} deg); Sun direction {[round(c, 4) for c in sun_dir]}; {len(moon)} Moon samples")

    objects, names = {}, {}
    for c in crafts:
        tag = c["tag"]
        if c["source"] == "oem":
            rows = oem_in_window(c, start, end, moon)
            how = f"OEM {c['oem'].name}, {len(rows)} states in the window"
        else:
            frame, rows = read_oem(OEM_DIR / f"{tag}.oem")
            if frame != "EME2000":
                sys.exit(f"{tag}.oem: unexpected frame {frame}")
            rows = [((t - start).total_seconds(), p, v) for t, p, v in rows]
            how = (f"TLE epoch {c['epoch']:%Y-%m-%d %H:%M} UTC" if c["source"] == "tle"
                   else f"elements at {c['epoch']:%Y-%m-%d %H:%M} UTC") + f", {len(rows)} states"
        objects[tag] = [{"t": round(t, 3), "pos": p, "vel": v} for t, p, v in rows]
        names[tag] = c["name"]
        print(f"{c['name']} ({tag}, {c['body']}): {how}")

    # Orbit lines: one period each, centred every half period across the window, so the viewer
    # can always show the one centred nearest "now" (orbits precess, e.g. ISS ~5 deg/day, so a
    # single line drifts off its spacecraft within hours). A Moon orbiter's lines are drawn
    # about the Moon (Moon-centred); the viewer moves them with it.
    tracks = {}
    TRACK_DIR.mkdir(parents=True, exist_ok=True)
    for old in TRACK_DIR.glob("*.glb"):
        old.unlink()
    for k, c in enumerate(crafts):
        tag = c["tag"]
        rows = relative_states(objects[tag], c["body"], moon)
        mu = MU[c["body"]]
        colour = TRACK_COLOURS[k % len(TRACK_COLOURS)]
        p0, v0 = rows[0][1], rows[0][2]
        a = 1 / (2 / math.sqrt(sum(x * x for x in p0)) - sum(x * x for x in v0) / mu)
        if a <= 0:
            print(f"{c['name']}: not in a closed orbit about the {c['body']}, no orbit lines")
            continue
        period = 2 * math.pi * math.sqrt(a ** 3 / mu)
        segments, centre, n = [], rows[0][0] + period / 2, 0
        while centre <= rows[-1][0] - period / 2 + 1e-6:
            seg = f"{tag}_{now:%Y%m%d%H%M}_{n:02d}"   # unique per run: WebVerse caches models by URL
            write_track(tag, rows, centre, colour, seg, mu)
            segments.append({"file": f"data/tracks/{seg}.glb", "t": round(centre, 1)})
            centre += period / 2; n += 1
        tracks[tag] = {"colour": colour, "period": round(period, 1), "segments": segments, "origin": c["body"]}
        print(f"{c['name']}: {len(segments)} orbit lines, period {period / 60:.1f} min about the {c['body']}")

    # Elements about the body orbited: GMAT's report for propagated spacecraft, computed from
    # the trajectory (same definitions, two-body osculating) for OEM-file ones.
    elements = {}
    for c in crafts:
        tag = c["tag"]
        if c["body"] == "Moon" or c["source"] == "oem":
            rows = [[round(t, 3)] + kepler(*element_axes(t, p, v, c["body"], moon), MU[c["body"]])
                    for t, p, v in relative_states(objects[tag], c["body"], moon)]
            source = "computed from the OEM" if c["source"] == "oem" else "computed from GMAT's trajectory"
        else:
            rows = read_elements(tag, start, end)
            source = "GMAT"
        elements[tag] = {"fields": ["t"] + ELEMENT_FIELDS, "rows": rows, "body": c["body"], "source": source}
        r = min(rows, key=lambda row: abs(row[0] - (now - start).total_seconds()))
        print(f"{c['name']}: elements now about the {c['body']} ({source}): SMA {r[1]:.3f} km, ECC {r[2]:.6f}, "
              f"INC {r[3]:.4f}, RAAN {r[4]:.4f}, AOP {r[5]:.4f}, TA {r[6]:.4f} deg ({len(rows)} rows)")

    passes = find_passes(objects, names, stations, rotation, EARTH_RATE_DEG_PER_S, moon)
    for ps in passes:
        aos = start + timedelta(seconds=ps["aos"])
        print(f"Pass {ps['name']} over {ps['station']}: AOS {aos:%H:%M:%S}, {(ps['los'] - ps['aos']) / 60:.1f} min, "
              f"max elevation {ps['max_el']:.1f} deg")

    # charts: per ground station, its passes' elevation and downlink margin (matplotlib)
    CHART_DIR.mkdir(parents=True, exist_ok=True)
    for old in CHART_DIR.glob("*.png"):
        old.unlink()
    charts, order = {}, {c["tag"]: k for k, c in enumerate(crafts)}
    for st in stations:
        mine = [ps for ps in passes if ps["station"] == st["name"]]
        profiles = []
        for ps in mine:
            prof, t = [], ps["aos"]
            while True:
                el, rng = look(st, objects[ps["catalog"]], t, rotation, EARTH_RATE_DEG_PER_S, moon)
                pl = payloads.get(ps["catalog"])
                down = budget(rng, st, pl)["down"] if pl else None
                prof.append((t, el, rng, down["margin_db"] if down else None))
                if t >= ps["los"]:
                    break
                t = min(t + CHART_STEP, ps["los"])
            profiles.append(prof)
            ps["profile_check"] = (look(st, objects[ps["catalog"]], ps["max_t"], rotation, EARTH_RATE_DEG_PER_S, moon)[0],
                                   look(st, objects[ps["catalog"]], ps["aos"], rotation, EARTH_RATE_DEG_PER_S, moon)[0])
        slug = "".join(ch if ch.isalnum() else "-" for ch in st["name"].lower()).strip("-")
        name = f"{slug}_{now:%Y%m%d%H%M}.png"          # unique per run: WebVerse caches by URL
        station_chart(CHART_DIR / name, st, mine, profiles, order, names, start, end)
        charts[st["name"]] = f"data/charts/{name}"
        print(f"Chart for {st['name']}: data/charts/{name} ({len(mine)} passes)")
    for ps in passes:
        ps.pop("profile_check", None)

    site_fields = lambda p: {"name": p["name"], "body": p["body"], "lat": p["lat"], "lon": p["lon"],
                             "agl_m": p["agl_m"], "ground_m": p["ground_m"], "ecef_km": p["ecef_km"]}
    payload = {
        "epoch": start.strftime("%d %b %Y %H:%M:%S.000"),          # UTC; t = seconds after this
        "generated": now.strftime("%d %b %Y %H:%M:%S.000"),
        "generated_t": (now - start).total_seconds(),              # where "now" was at run time
        # Greenwich meridian's angle from +X (EarthMJ2000Eq) at "epoch", and its rate
        "earth": {"rotation_deg": rotation, "rate_deg_per_s": EARTH_RATE_DEG_PER_S},
        "sun_dir": sun_dir,                                        # unit vector, EarthMJ2000Eq
        # the Moon every 60 s: Earth-centred EarthMJ2000Eq position / velocity (km, km/s), and
        # q, the Moon entity's rotation in the viewer's (Unity) axes (Moon-fixed -> world)
        "moon": {"radius_km": MOON_RADIUS_KM,
                 "samples": [[round(m["t"], 3)] + [round(x, 6) for x in m["pos"] + m["vel"]] + unity_quat(m["R"])
                             for m in moon]},
        "names": names,
        # each spacecraft: the body it orbits and where its trajectory came from
        "craft": {c["tag"]: {"body": c["body"], "source": c["source"], "model": "probe.glb"} for c in crafts},
        # instrument cameras found in models/source/<model>.glb, in the loaded model's axes:
        # {model: [{name, pos, fwd, up, yfov_deg, aspect}]} (see tools/instruments.py)
        "instruments": instruments,
        "tracks": tracks,                                          # orbit lines, one per half period
        # places.xlsx: body-fixed positions in km (Earth: WGS84; Moon: sphere), turned with the body
        "places": [site_fields(p) for p in places],
        # groundstations.xlsx: the same, plus frequency, beam FOV (full cone about straight up)
        # and link ("1-way" = receive only, "2-way")
        "ground_stations": [dict(site_fields(p), freq_mhz=p["freq_mhz"], fov_deg=p["fov_deg"], link=p["link"],
                                 gain_dbi=p["gain_dbi"], tsys_k=p["tsys_k"], tx_w=p["tx_w"]) for p in stations],
        # spacecraft radios for link budgets (spacecraft.xlsx, Payloads): {tag: {tx_w, gain_dbi,
        # rate_bps, ebn0_req_db, gt_dbk, up_rate_bps, losses_db}}; the viewer works out the budget
        # (tools/linkbudget.py's equations) live from the range
        "payloads": payloads,
        # per ground station, its chart (tools/charts.py): {station name: "data/charts/<name>.png"}
        "charts": charts,
        # Real-time clock for the viewer, whose Date.now is local time with fields only:
        # UTC = local - utc_offset_s; epoch as seconds since 1 Jan 00:00 UTC of epoch_year.
        "clock": {"epoch_year": start.year,
                  "epoch_doy_s": (start - datetime(start.year, 1, 1, tzinfo=timezone.utc)).total_seconds(),
                  "utc_offset_s": time.localtime().tm_gmtoff},
        "objects": objects,
        # osculating elements about the body orbited: {tag: {"fields", "rows": [[t, ...]], "body", "source"}}
        "elements": elements,
        # passes through each ground station's beam: [{station, catalog, name, aos, los, max_t, max_el}]
        "passes": passes,
    }
    OUT_JSON.write_text(json.dumps(payload))
    for p in places:
        print(f"Place {p['name']} ({p['body']}): {p['lat']:.5f}, {p['lon']:.5f}, ground {p['ground_m']:.0f} m + "
              f"{p['agl_m']:.0f} m AGL")
    for p in stations:
        print(f"Ground station {p['name']} ({p['body']}): {p['lat']:.5f}, {p['lon']:.5f}, ground {p['ground_m']:.0f} m + "
              f"{p['agl_m']:.0f} m AGL, {p['freq_mhz']:g} MHz, FOV {p['fov_deg']:g} deg, {p['link']}")
    print(f"Wrote {OUT_JSON} (window {start:%Y-%m-%d %H:%M} to {end:%Y-%m-%d %H:%M} UTC)")


def oem_in_window(c, start, end, moon):
    """An OEM-file spacecraft's states inside the window (plus one either side), Earth-centred
    EarthMJ2000Eq: [(t, pos, vel)], t in seconds after start. Moon-centred states get the
    Moon's position and velocity added."""
    try:
        segments = read_oem_file(c["oem"])
    except PlacesError as e:
        sys.exit(str(e))
    rows, seen = [], set()
    for seg in segments:
        for utc, p, v in seg["rows"]:
            t = (utc - start).total_seconds()
            if t in seen:
                continue
            seen.add(t)
            rows.append((t, p, v, seg["center"]))
    rows.sort(key=lambda r: r[0])
    t_end = (end - start).total_seconds()
    inside = [i for i, r in enumerate(rows) if 0 <= r[0] <= t_end]
    if not inside:
        span = f"{rows[0][0] / 3600:+.1f} h to {rows[-1][0] / 3600:+.1f} h" if rows else "nothing"
        sys.exit(f"{c['name']}: {c['oem'].name} covers {span} from the window start; the window is 0 to "
                 f"{t_end / 3600:.0f} h ({start:%Y-%m-%d %H:%M} to {end:%Y-%m-%d %H:%M} UTC)")
    rows = rows[max(0, inside[0] - 1):inside[-1] + 2]
    if rows[0][0] > 0 or rows[-1][0] < t_end:
        print(f"{c['name']}: {c['oem'].name} covers only {max(0, rows[0][0]) / 3600:.1f} h to "
              f"{min(t_end, rows[-1][0]) / 3600:.1f} h of the window; it holds its position outside that")
    out = []
    for t, p, v, center in rows:
        if center == "MOON":
            mp, mv, _ = moon_state(moon, max(moon[0]["t"], min(moon[-1]["t"], t)))
            p, v = [p[k] + mp[k] for k in range(3)], [v[k] + mv[k] for k in range(3)]
        out.append((t, p, v))
    return out


def element_axes(t, p, v, body, moon):
    """A body-centred state in the axes its elements use: EarthMJ2000Eq for the Earth (as is),
    the Moon's equator of date at time t for the Moon."""
    if body != "Moon":
        return p, v
    B = moon_of_date(moon_state(moon, max(moon[0]["t"], min(moon[-1]["t"], t)))[2])
    back = lambda x: [sum(B[k][i] * x[k] for k in range(3)) for i in range(3)]
    return back(p), back(v)


def relative_states(track, body, moon):
    """[(t, pos, vel)] relative to the body orbited (the Moon's state subtracted for the Moon)."""
    out = []
    for o in track:
        p, v = o["pos"], o["vel"]
        if body == "Moon":
            mp, mv, _ = moon_state(moon, max(moon[0]["t"], min(moon[-1]["t"], o["t"])))
            p, v = [p[k] - mp[k] for k in range(3)], [v[k] - mv[k] for k in range(3)]
        out.append((o["t"], p, v))
    return out


if __name__ == "__main__":
    main()
