#!/usr/bin/env python3
"""Lunar terrain for the fleet job: horizon masks, sunlight and line of sight for Moon sites.

Everything here is in the Moon's Mean Earth / polar axis frame (MOON_ME), the frame of NASA's
LOLA elevation and LROC colour maps and of the sites' latitudes and longitudes. GMAT's Luna
body-fixed axes are a principal-axis frame (MOON_PA), so the Earth's and the Sun's Moon-fixed
directions from GMAT are turned with PA_TO_ME first (see pa_to_me).

Horizon mask: for each azimuth (0.25 deg steps, from north through east) the highest elevation
angle of the terrain, marched out along the great circle to 600 km over the LOLA map (the best
one in data/moon_source/: 64 px/deg, else 16), with the Moon's curvature: a point at arc
distance s and height h is seen at atan2(r1 cos(s/R) - r0, r1 sin(s/R)), r1 = R + h,
r0 = R + ground + antenna height. The observer stands on the same LOLA surface (bilinear), so a
blank ground elevation is the LOLA height there. Masks are cached in data/terrain_cache.json.

Sunlight: the share of the solar disc above the horizon -- the disc (its true angular size)
cut into vertical strips, each against the mask at its own azimuth, so a sloping horizon is
handled. Line of sight: a target is visible when its elevation is above the mask at its
azimuth (used for the Earth, and for spacecraft in the passes).

Over the run's window (60 s, from the frame report) and over the next 30 and 365 days (a short
GMAT run reporting the Earth and the Sun in the Moon-fixed frame every 5 / 30 minutes):
sunlit share, mean disc fraction, the longest stretch without sun, and the same for the Earth.
"""
import json
import math
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np

from places import site_key

ROOT = Path(__file__).resolve().parent.parent
SOURCE_DIR = ROOT / "data" / "moon_source"
CACHE = ROOT / "data" / "terrain_cache.json"
LONG_SCRIPT = ROOT / "data" / "terrain_frames.script"
LONG_CSV = {30: ROOT / "data" / "terrain_frames_30d.csv", 365: ROOT / "data" / "terrain_frames_365d.csv"}
LONG_STEP = {30: 300, 365: 1800}          # s between samples
R_KM = 1737.4
SUN_RADIUS_KM = 695700.0
EARTH_RADIUS_KM = 6378.1363
AZ_STEP = 0.25                            # deg between mask azimuths
MAX_KM = 600.0                            # how far the horizon march goes
DISC_STRIPS = 32
DEMS = ["ldem_64_uint.tif", "ldem_16_uint.tif"]   # best first; half-metres, km = v / 2000 - 10

# MOON_PA -> MOON_ME (DE421): the frame kernel's TKFRAME_31007 angles (67.92, 78.56, 0.30
# arcsec about axes 3, 2, 1). Measured against GMAT with Luna on SPICE MOON_PA and MOON_ME over
# 30 days: v_ME = K^T v_PA with K = [67.92"]_3 [78.56"]_2 [0.30"]_1 (SPICE frame rotations),
# matching to 0.0000"; a constant 103.85" rotation (~0.9 km at the surface). GMAT's default
# Luna axes (DE405 libration) differ from SPICE MOON_PA (DE421) by a further ~5" (~43 m).


def _frot(axis, t):
    c, s = math.cos(t), math.sin(t)
    if axis == 1:
        return np.array([[1, 0, 0], [0, c, s], [0, -s, c]])
    if axis == 2:
        return np.array([[c, 0, -s], [0, 1, 0], [s, 0, c]])
    return np.array([[c, s, 0], [-s, c, 0], [0, 0, 1]])


_A = [math.radians(a / 3600) for a in (67.92, 78.56, 0.30)]
PA_TO_ME = (_frot(3, _A[0]) @ _frot(2, _A[1]) @ _frot(1, _A[2])).T


def pa_to_me(v):
    """A Moon-fixed vector from GMAT's (principal axis) frame into the Mean Earth frame."""
    return [float(x) for x in PA_TO_ME @ np.asarray(v, dtype=float)]


# ---- elevation map ----

_dem = None


def dem_name():
    for name in DEMS:
        if (SOURCE_DIR / name).exists():
            return name
    return None


def load_dem():
    global _dem
    if _dem is None:
        from PIL import Image
        Image.MAX_IMAGE_PIXELS = None
        name = dem_name()
        print(f"Terrain: loading {name}")
        _dem = np.asarray(Image.open(SOURCE_DIR / name)).astype(np.float32) / 2000.0 - 10.0
    return _dem


def dem_height(lat, lon):
    """LOLA height (km above 1737.4) at lat/lon (deg, arrays), bilinear between pixel centres."""
    dem = load_dem()
    h, w = dem.shape
    y = np.clip((90.0 - lat) / 180.0 * h - 0.5, 0, h - 1)
    x = (lon + 180.0) / 360.0 * w - 0.5
    y0 = np.minimum(np.floor(y).astype(np.int64), h - 2)
    x0 = np.floor(x).astype(np.int64)
    fy, fx = y - y0, x - x0
    x0w, x1w = x0 % w, (x0 + 1) % w
    a = dem[y0, x0w] * (1 - fx) + dem[y0, x1w] * fx
    b = dem[y0 + 1, x0w] * (1 - fx) + dem[y0 + 1, x1w] * fx
    return a * (1 - fy) + b * fy


def local_frame(lat_deg, lon_deg):
    """Up, east, north (unit, Moon-fixed) at a site."""
    la, lo = math.radians(lat_deg), math.radians(lon_deg)
    up = np.array([math.cos(la) * math.cos(lo), math.cos(la) * math.sin(lo), math.sin(la)])
    east = np.array([-math.sin(lo), math.cos(lo), 0.0])
    north = np.cross(up, east)
    return up, east, north


def distances(near=0.025, far=0.001):
    """Arc distances (km) of the horizon march: from one LOLA pixel out (closer in, the mask
    would only follow the kinks of the bilinear interpolation inside the site's own pixel),
    25 m steps to 25 km, then 0.1 % of the distance (features further out subtend less), to
    MAX_KM. Measured at the example south-pole sites against 12.5 m / 0.05 % steps: within
    0.25 deg (p99 0.05-0.13 deg) -- the pixel-scale shape the map can't resolve."""
    start = math.pi * R_KM / load_dem().shape[0]
    s = list(np.arange(start, 25.0, near))
    while s[-1] < MAX_KM:
        s.append(s[-1] * (1 + far))
    return np.array(s)


def horizon_mask(lat_deg, lon_deg, eye_km, az_step=AZ_STEP, max_km=MAX_KM, dists=None):
    """Horizon elevation (deg) at each azimuth k * az_step from north through east, for an eye
    at R + eye_km above the point; plus the distance (km) of the horizon-setting terrain."""
    up, east, north = local_frame(lat_deg, lon_deg)
    s = distances() if dists is None else dists
    s = s[s <= max_km]
    d = s / R_KM
    r0 = R_KM + eye_km
    masks, where = [], []
    all_az = np.radians(np.arange(0, 360, az_step))
    for a0 in range(0, len(all_az), 90):                    # in chunks, to bound memory
        az = all_az[a0:a0 + 90]
        t = np.cos(az)[:, None, None] * north + np.sin(az)[:, None, None] * east   # (A, 1, 3)
        p = np.cos(d)[None, :, None] * up + np.sin(d)[None, :, None] * t            # (A, S, 3)
        lat = np.degrees(np.arcsin(np.clip(p[..., 2], -1, 1)))
        lon = np.degrees(np.arctan2(p[..., 1], p[..., 0]))
        r1 = R_KM + dem_height(lat, lon).astype(np.float64)
        el = np.degrees(np.arctan2(r1 * np.cos(d)[None, :] - r0, r1 * np.sin(d)[None, :]))
        masks.append(el.max(1))
        where.append(s[el.argmax(1)])
    return np.concatenate(masks), np.concatenate(where)


def mask_at(mask, az_deg):
    """The mask (array over azimuth) at azimuths az_deg, periodic linear interpolation."""
    n = len(mask)
    x = (np.asarray(az_deg) % 360.0) / (360.0 / n)
    i = np.floor(x).astype(np.int64)
    f = x - i
    return mask[i % n] * (1 - f) + mask[(i + 1) % n] * f


def disc_fraction(el, az, radius, mask, strips=DISC_STRIPS):
    """Share of a disc (centre at elevation el, azimuth az, angular radius radius; deg,
    arrays) above the horizon mask, by vertical strips each against the mask at its azimuth."""
    el, az, radius = (np.atleast_1d(np.asarray(v, dtype=float)) for v in (el, az, radius))
    u = (np.arange(strips) + 0.5) / strips * 2 - 1                         # strip centres, -1..1
    x = u[None, :] * radius[:, None]                                       # deg across the sky
    half = np.sqrt(np.maximum(radius[:, None] ** 2 - x ** 2, 0))
    cos_el = np.maximum(np.cos(np.radians(el)), 1e-6)[:, None]
    horizon = mask_at(mask, az[:, None] + x / cos_el)
    vis = np.clip(el[:, None] + half - horizon, 0, 2 * half)
    width = 2 * radius / strips
    return (vis.sum(1) * width) / (np.pi * radius ** 2)


def look(site_km, up, east, north, target_km):
    """Elevation, azimuth (deg) and distance (km) of targets (N x 3, Moon-fixed km) from a site."""
    d = np.asarray(target_km, dtype=float) - site_km
    dist = np.linalg.norm(d, axis=1)
    el = np.degrees(np.arcsin(np.clip(d @ up / dist, -1, 1)))
    az = np.degrees(np.arctan2(d @ east, d @ north)) % 360.0
    return el, az, dist


# ---- sites ----

def _cache():
    try:
        return json.loads(CACHE.read_text())
    except (OSError, ValueError):
        return {}


def prepare(sites):
    """For each Moon site: the LOLA ground height (when the spreadsheet left it blank; the site
    then stands on LOLA instead of a model's mesh) and its horizon mask. Sets site["terrain"]
    = {dem, ground_m, eye_m, mask (list), mask_km}, and fixes ground_m / ecef_km. Returns
    False when no LOLA map is available (data/moon_source/, see tools/make_moon.py)."""
    moon = [s for s in sites if s.get("body") == "Moon"]
    if not moon:
        return True
    name = dem_name()
    if name is None:
        print("Terrain: no LOLA map in data/moon_source (run tools/make_moon.py); Moon sites use a smooth sphere")
        return False
    from places import moon_fixed
    cache, changed = _cache(), False
    for s in moon:
        typed = not s.get("ground_source", "").startswith(("LOLA", "mean"))
        key = f"{name}|{s['lat']:.7f}|{s['lon']:.7f}|{s['ground_m'] if typed else 'LOLA'}|{s['agl_m']:.3f}|{AZ_STEP}|{MAX_KM}"
        if key not in cache:
            ground_km = s["ground_m"] / 1000 if typed else float(dem_height(np.array(s["lat"]), np.array(s["lon"])))
            mask, mask_km = horizon_mask(s["lat"], s["lon"], ground_km + s["agl_m"] / 1000)
            cache[key] = {"ground_m": round(ground_km * 1000, 1), "mask": [round(float(m), 4) for m in mask],
                          "mask_km": [round(float(k), 2) for k in mask_km]}
            changed = True
        c = cache[key]
        if not typed:
            s["ground_m"], s["ground_source"] = c["ground_m"], f"LOLA ({name.split('_')[1]} px/deg)"
            s["ecef_km"] = [round(v, 6) for v in moon_fixed(s["lat"], s["lon"], (s["ground_m"] + s["agl_m"]) / 1000)]
        s["terrain"] = {"dem": name, "ground_m": c["ground_m"], "eye_m": round(c["ground_m"] + s["agl_m"], 2),
                        "mask": np.array(c["mask"]), "mask_km": c["mask_km"]}
    if changed:
        CACHE.write_text(json.dumps(cache))
    return True


def visibility(site, earth_mf, sun_mf):
    """Sun disc fraction, the Sun's elevation, and the Earth's elevation above the terrain
    (deg; > 0 = in sight) and disc fraction, for Moon-fixed (ME) Earth and Sun positions
    (N x 3 km, from the Moon's centre)."""
    tr = site["terrain"]
    site_km = np.asarray(site["ecef_km"], dtype=float)
    up, east, north = local_frame(site["lat"], site["lon"])
    sel, saz, sdist = look(site_km, up, east, north, sun_mf)
    sun = disc_fraction(sel, saz, np.degrees(np.arcsin(SUN_RADIUS_KM / sdist)), tr["mask"])
    eel, eaz, edist = look(site_km, up, east, north, earth_mf)
    earth_clear = eel - mask_at(tr["mask"], eaz)
    earth = disc_fraction(eel, eaz, np.degrees(np.arcsin(EARTH_RADIUS_KM / edist)), tr["mask"])
    return {"sun": sun, "sun_el": sel, "sun_az": saz, "earth_clear": earth_clear, "earth": earth,
            "earth_el": eel, "earth_az": eaz}


def longest_run(flags, step_s):
    """Longest stretch (hours) of consecutive True samples."""
    best = run = 0
    for f in flags:
        run = run + 1 if f else 0
        best = max(best, run)
    return best * step_s / 3600.0


def stats(vis, step_s):
    sun, earth = vis["sun"], vis["earth_clear"] > 0
    return {"sunlit_pct": round(100 * float((sun > 0).mean()), 2),
            "full_sun_pct": round(100 * float((sun >= 0.999).mean()), 2),
            "mean_disc_pct": round(100 * float(sun.mean()), 2),
            "longest_dark_h": round(longest_run(sun <= 0, step_s), 2),
            "earth_pct": round(100 * float(earth.mean()), 2),
            "longest_no_earth_h": round(longest_run(~earth, step_s), 2),
            "step_s": step_s}


# ---- the next 30 / 365 days ----

def gmat_time(t):
    return t.strftime("%d %b %Y %H:%M:%S.%f")[:-3]


def long_frames(start, gmat_console):
    """{days: (t (s after start), earth_mf (N x 3, ME km), sun_mf)} for 30 and 365 days, from a
    GMAT run (a reference spacecraft as the clock, the Earth and the Sun in MoonFixed)."""
    s = ["% Generated by tools/terrain.py -- edit that, not this.",
         "Create CoordinateSystem MoonFixed;", "GMAT MoonFixed.Origin = Luna;", "GMAT MoonFixed.Axes = BodyFixed;",
         "Create ForceModel FMRef;", "GMAT FMRef.CentralBody = Earth;", "GMAT FMRef.PointMasses = {Earth};"]
    order = sorted(LONG_CSV)
    for days in order:
        step = LONG_STEP[days]
        s += [f"Create Propagator Prop{days};", f"GMAT Prop{days}.FM = FMRef;", f"GMAT Prop{days}.Type = RungeKutta89;",
              f"GMAT Prop{days}.InitialStepSize = {step};", f"GMAT Prop{days}.MinStep = {step};",
              f"GMAT Prop{days}.MaxStep = {step};", f"GMAT Prop{days}.Accuracy = 1e-3;",
              # a GEO-sized clock orbit: fixed steps of up to 30 min stay within the accuracy
              f"Create Spacecraft Clock{days};", f"GMAT Clock{days}.DateFormat = UTCGregorian;",
              f"GMAT Clock{days}.Epoch = '{gmat_time(start)}';", f"GMAT Clock{days}.CoordinateSystem = EarthMJ2000Eq;",
              f"GMAT Clock{days}.DisplayStateType = Keplerian;", f"GMAT Clock{days}.SMA = 42164;",
              f"GMAT Clock{days}.ECC = 0.001;", f"GMAT Clock{days}.INC = 10;",
              f"Create ReportFile Rep{days};", f"GMAT Rep{days}.Filename = '{LONG_CSV[days].as_posix()}';",
              f"GMAT Rep{days}.Add = {{Clock{days}.ElapsedSecs, Earth.MoonFixed.X, Earth.MoonFixed.Y, Earth.MoonFixed.Z, "
              "Sun.MoonFixed.X, Sun.MoonFixed.Y, Sun.MoonFixed.Z};",
              f"GMAT Rep{days}.Precision = 16;", f"GMAT Rep{days}.WriteHeaders = true;",
              f"GMAT Rep{days}.FixedWidth = false;", f"GMAT Rep{days}.Delimiter = ',';"]
    s += ["BeginMissionSequence;"] + [f"Propagate Prop{d}(Clock{d}) {{Clock{d}.ElapsedDays = {d}}};" for d in order]
    LONG_SCRIPT.write_text("\n".join(s) + "\n")
    run = subprocess.run([str(gmat_console), "--run", str(LONG_SCRIPT), "--exit"],
                         cwd=Path(gmat_console).parent, capture_output=True, text=True)
    if "successful" not in run.stdout:
        sys.exit("GMAT terrain-frame run failed:\n" + run.stdout[-2000:] + run.stderr[-2000:])
    out = {}
    for days in order:
        rows, seen = [], set()
        for line in LONG_CSV[days].read_text().splitlines()[1:]:
            f = line.split(",")
            if len(f) != 7 or f[0] in seen:
                continue                       # rows repeat while the other clock propagates
            seen.add(f[0])
            rows.append([float(v) for v in f])
        a = np.array(rows)
        a = a[np.argsort(a[:, 0])]
        out[days] = (a[:, 0], a[:, 1:4] @ PA_TO_ME.T, a[:, 4:7] @ PA_TO_ME.T)
    return out


def assess(sites, window, start, gmat_console):
    """Terrain results for every Moon site prepared by prepare(): {name: {...}} for fleet.json.
    window: (t list, earth_mf, sun_mf) at the frame report's 60 s samples."""
    moon = [s for s in sites if s.get("terrain")]
    if not moon:
        return {}
    long = long_frames(start, gmat_console)
    out = {}
    for s in moon:
        tr = s["terrain"]
        t, e, su = window
        now = visibility(s, np.asarray(e), np.asarray(su))
        res = {"dem": tr["dem"], "ground_m": tr["ground_m"], "eye_m": tr["eye_m"], "az_step": AZ_STEP,
               "mask": [round(float(m), 3) for m in tr["mask"]],
               "window": {"t0": t[0], "step": t[1] - t[0],
                          "sun": [round(float(v), 4) for v in now["sun"]],
                          "earth_clear": [round(float(v), 3) for v in now["earth_clear"]],
                          "sun_el": [round(float(v), 3) for v in now["sun_el"]]}}
        for days, (lt, le, ls) in long.items():
            v = visibility(s, le, ls)
            res[f"next_{days}d"] = stats(v, LONG_STEP[days])
            if days == 30:
                s["_long30"] = (lt, v)            # for the chart
            s[f"_series{days}"] = (lt, v)         # for the plot panel's longer spans (tools/plots.py)
        out[site_key(s)] = res
        st = res["next_30d"]
        print(f"Terrain {s['name']}: ground {tr['ground_m']:.0f} m (LOLA), horizon {tr['mask'].min():.2f} .. "
              f"{tr['mask'].max():.2f} deg; next 30 d: sunlit {st['sunlit_pct']}%, longest dark "
              f"{st['longest_dark_h']} h, Earth in sight {st['earth_pct']}%; next 365 d: sunlit "
              f"{res['next_365d']['sunlit_pct']}%, Earth {res['next_365d']['earth_pct']}%")
    return out
