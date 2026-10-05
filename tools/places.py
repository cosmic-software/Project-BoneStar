#!/usr/bin/env python3
"""Places and ground stations, from places.xlsx and groundstations.xlsx.

    python tools/places.py          check both files and print what the viewer will get

The fleet job (tools/run_fleet.py) calls read_places() and read_ground_stations() and puts
the results in data/fleet.json, so they show on the Earth in the viewer.

places.xlsx, sheet "Places", one place per row under the header row:

    Name | Latitude | Longitude | Altitude AGL (m) | Ground elevation (m)

groundstations.xlsx, sheet "Ground stations": the same five columns, then

    Frequency (MHz) | Beam FOV (deg) | Link

- Frequency: the station's operating frequency in MHz (e.g. 2250 for S-band).
- Beam FOV: the full cone angle the station sees, centred straight up. 180 = horizon to
  horizon; 170 = down to 5 deg above the horizon. A spacecraft inside it is in contact.
- Link: "1-way" (the station only receives: downlink) or "2-way" (uplink and downlink).

- Latitude / Longitude: decimal degrees (north and east positive), or with a hemisphere
  letter ("28.6 N", "80.6 W"), or degrees-minutes-seconds ("28 36 30.2 N", 28°36'30.2"N).
- Altitude AGL: metres above the ground (an antenna on a 10 m mast = 10). Blank = 0.
- Ground elevation: metres above mean sea level, optional. Left blank, it is looked up from
  the Copernicus GLO-90 terrain model via the Open-Meteo elevation API (free, no key) and
  cached in data/elevation_cache.json, so each place is looked up once.

Height above the WGS84 ellipsoid is taken as ground elevation + AGL. Mean sea level differs
from the ellipsoid by the geoid height (-106 to +85 m worldwide); that is ignored, which is
0.001 viewer units (1 unit = 100 km).

Either spreadsheet can also be saved as .ods (LibreOffice's own format): places.ods,
groundstations.ods. If both an .xlsx and an .ods exist, the one saved most recently is used.
Only the standard library is used to read them (both are zips of XML files), so nothing
needs installing.
"""
import json
import math
import re
import struct
import sys
import urllib.request
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PLACES_XLSX = ROOT / "places.xlsx"
STATIONS_XLSX = ROOT / "groundstations.xlsx"
ELEVATION_CACHE = ROOT / "data" / "elevation_cache.json"
MARKER_GLB = ROOT / "models" / "place.glb"
STATION_GLB = ROOT / "models" / "station.glb"
GRID_GLB = ROOT / "models" / "grid.glb"
SELECT_GLB = ROOT / "models" / "select.glb"
LINK_GLBS = {"2-way": ROOT / "models" / "link_2way.glb", "1-way": ROOT / "models" / "link_1way.glb"}
PLACE_COLOUR = (1.0, 0.85, 0.2)         # yellow
STATION_COLOUR = (0.3, 0.9, 1.0)        # cyan
ELEVATION_API = "https://api.open-meteo.com/v1/elevation"

WGS84_A = 6378.137              # km, equatorial radius
WGS84_F = 1 / 298.257223563
MOON_RADIUS_KM = 1737.4          # IAU mean lunar radius

NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
      "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
      "rel": "http://schemas.openxmlformats.org/package/2006/relationships"}


class PlacesError(Exception):
    pass


# ---- .xlsx reading (standard library) ----

def _column_index(ref):
    """'C7' -> 2"""
    n = 0
    for c in re.match(r"[A-Z]+", ref).group():
        n = n * 26 + ord(c) - 64
    return n - 1


ODS = {"table": "urn:oasis:names:tc:opendocument:xmlns:table:1.0",
       "office": "urn:oasis:names:tc:opendocument:xmlns:office:1.0",
       "text": "urn:oasis:names:tc:opendocument:xmlns:text:1.0"}


def read_ods_sheet(path, sheet_name):
    """As read_sheet, for a LibreOffice .ods file."""
    t, o = f"{{{ODS['table']}}}", f"{{{ODS['office']}}}"
    with zipfile.ZipFile(path) as z:
        root = ET.fromstring(z.read("content.xml"))
    tables = root.findall(".//table:table", ODS)
    table = next((s for s in tables if s.get(t + "name") == sheet_name), tables[0])
    rows = []
    for row in table.iter(t + "table-row"):
        cells = []
        for c in row:
            if c.tag not in (t + "table-cell", t + "covered-table-cell"):
                continue
            kind = c.get(o + "value-type")
            if kind in ("float", "percentage", "currency"):
                value = float(c.get(o + "value"))
            elif kind == "date":
                value = c.get(o + "date-value")          # ISO 8601, e.g. 2026-10-05T00:00:00
            elif kind is None:
                value = None
            else:
                value = "\n".join("".join(p.itertext()) for p in c.findall("text:p", ODS))
            cells += [value] * int(c.get(t + "number-columns-repeated", "1"))
        while cells and cells[-1] is None:        # trailing blanks are repeated to column 1024
            cells.pop()
        repeat = int(row.get(t + "number-rows-repeated", "1"))
        rows += [cells] * (repeat if cells else min(repeat, 1))
    return rows


def pick_file(path):
    """path (.xlsx) or the .ods beside it, whichever was saved last; None if neither exists."""
    found = [p for p in (path, path.with_suffix(".ods")) if p.exists()]
    return max(found, key=lambda p: p.stat().st_mtime) if found else None


def read_sheet(path, sheet_name):
    """Rows of the named sheet (or the first sheet) as lists of str / float / None."""
    if path.suffix.lower() == ".ods":
        return read_ods_sheet(path, sheet_name)
    with zipfile.ZipFile(path) as z:
        book = ET.fromstring(z.read("xl/workbook.xml"))
        rels = ET.fromstring(z.read("xl/_rels/workbook.xml.rels"))
        targets = {r.get("Id"): r.get("Target") for r in rels.findall("rel:Relationship", NS)}
        sheets = book.findall("m:sheets/m:sheet", NS)
        sheet = next((s for s in sheets if s.get("name") == sheet_name), sheets[0])
        target = targets[sheet.get(f"{{{NS['r']}}}id")].lstrip("/")
        target = target if target.startswith("xl/") else "xl/" + target
        shared = []
        if "xl/sharedStrings.xml" in z.namelist():
            for si in ET.fromstring(z.read("xl/sharedStrings.xml")).findall("m:si", NS):
                shared.append("".join(t.text or "" for t in si.iter(f"{{{NS['m']}}}t")))
        rows = []
        for row in ET.fromstring(z.read(target)).findall("m:sheetData/m:row", NS):
            cells = {}
            for c in row.findall("m:c", NS):
                kind, v = c.get("t"), c.find("m:v", NS)
                if kind == "inlineStr":
                    value = "".join(t.text or "" for t in c.iter(f"{{{NS['m']}}}t"))
                elif v is None or v.text is None:
                    value = None
                elif kind == "s":
                    value = shared[int(v.text)]
                elif kind in ("str", "e"):
                    value = v.text
                elif kind == "b":
                    value = v.text == "1"
                else:
                    value = float(v.text)
                cells[_column_index(c.get("r"))] = value
            rows.append([cells.get(i) for i in range(max(cells) + 1)] if cells else [])
        return rows


# ---- values ----

def parse_angle(value, positive, negative, limit, what):
    """Degrees from a number, '28.6 N', '-80.6', '28 36 30.2 N' or 28°36'30.2"N."""
    if isinstance(value, float):
        deg = value
    else:
        text = str(value).strip().upper()
        sign = 1
        if text and text[-1] in positive + negative:
            sign = -1 if text[-1] in negative else 1
            text = text[:-1]
        elif text and text[0] in positive + negative:
            sign = -1 if text[0] in negative else 1
            text = text[1:]
        parts = [p for p in re.split(r"[\s°º'′\"″:]+", text) if p]
        try:
            nums = [float(p) for p in parts]
        except ValueError:
            raise PlacesError(f"{what} '{value}' is not a number of degrees")
        if not 1 <= len(nums) <= 3 or any(n < 0 for n in nums[1:]) or any(n >= 60 for n in nums[1:]):
            raise PlacesError(f"{what} '{value}': use decimal degrees or 'deg min sec'")
        whole = abs(nums[0]) + sum(n / 60 ** (k + 1) for k, n in enumerate(nums[1:]))
        deg = sign * (-whole if text.lstrip().startswith("-") else whole)
    if not -limit <= deg <= limit:
        raise PlacesError(f"{what} {deg} is outside -{limit}..{limit}")
    return deg


def parse_metres(value, what):
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    try:
        return float(str(value).strip().lower().removesuffix("m").strip()) if isinstance(value, str) else value
    except ValueError:
        raise PlacesError(f"{what} '{value}' is not a number of metres")


def geodetic_to_ecef(lat_deg, lon_deg, h_km):
    """WGS84 geodetic -> Earth-fixed X, Y, Z (km)."""
    lat, lon = math.radians(lat_deg), math.radians(lon_deg)
    e2 = WGS84_F * (2 - WGS84_F)
    n = WGS84_A / math.sqrt(1 - e2 * math.sin(lat) ** 2)
    return [(n + h_km) * math.cos(lat) * math.cos(lon),
            (n + h_km) * math.cos(lat) * math.sin(lon),
            (n * (1 - e2) + h_km) * math.sin(lat)]


def lookup_elevations(points):
    """Ground elevation (m above sea level) for each (lat, lon), cached by position."""
    cache = json.loads(ELEVATION_CACHE.read_text()) if ELEVATION_CACHE.exists() else {}
    key = lambda p: f"{p[0]:.5f},{p[1]:.5f}"
    missing = sorted({key(p): p for p in points if key(p) not in cache}.values())
    for i in range(0, len(missing), 100):           # the API takes up to 100 points a call
        batch = missing[i:i + 100]
        url = (f"{ELEVATION_API}?latitude={','.join(f'{p[0]:.5f}' for p in batch)}"
               f"&longitude={','.join(f'{p[1]:.5f}' for p in batch)}")
        try:
            with urllib.request.urlopen(url, timeout=20) as r:
                heights = json.load(r)["elevation"]
        except Exception as e:
            raise PlacesError(f"ground elevation lookup failed ({e}); fill in the "
                              "'Ground elevation (m)' column or try again when online")
        for p, h in zip(batch, heights):
            cache[key(p)] = h
    if missing:
        ELEVATION_CACHE.parent.mkdir(parents=True, exist_ok=True)
        ELEVATION_CACHE.write_text(json.dumps(cache, indent=1))
    return [cache[key(p)] for p in points]


def parse_link(value):
    text = re.sub(r"[\s_-]+", "", str(value or "")).lower()
    if text in ("1way", "1", "1.0", "oneway"):
        return "1-way"
    if text in ("2way", "2", "2.0", "twoway"):
        return "2-way"
    raise PlacesError(f"Link '{value}' should be 1-way or 2-way")


def parse_body(value):
    """Earth (also blank) or Moon."""
    text = str(value or "").strip().lower()
    if text in ("", "earth"):
        return "Earth"
    if text in ("moon", "luna"):
        return "Moon"
    raise PlacesError(f"Body '{value}' should be Earth or Moon (blank = Earth)")


def moon_fixed(lat_deg, lon_deg, h_km):
    """Moon body-fixed X, Y, Z (km) on a sphere of the mean lunar radius."""
    la, lo = math.radians(lat_deg), math.radians(lon_deg)
    r = MOON_RADIUS_KM + h_km
    return [r * math.cos(la) * math.cos(lo), r * math.cos(la) * math.sin(lo), r * math.sin(la)]


def parse_station(cells):
    """The ground-station columns: Frequency (MHz), Beam FOV (deg), Link."""
    freq, fov, link = cells
    freq_mhz = parse_metres(freq, "Frequency")      # same rules: a number, blank = None
    fov_deg = parse_metres(fov, "Beam FOV")
    if freq_mhz is None or freq_mhz <= 0:
        raise PlacesError("Frequency (MHz) is needed and must be above 0")
    if fov_deg is None or not 0 < fov_deg <= 180:
        raise PlacesError("Beam FOV (deg) is needed, above 0 and at most 180")
    return {"freq_mhz": freq_mhz, "fov_deg": fov_deg, "link": parse_link(link)}


def read_places(path=PLACES_XLSX):
    """[{name, lat, lon, agl_m, ground_m, ground_source, ecef_km}] -- [] if there is no file."""
    return read_sites(path, "Places")


def read_ground_stations(path=STATIONS_XLSX):
    """As read_places, plus freq_mhz, fov_deg and link ("1-way" / "2-way")."""
    return read_sites(path, "Ground stations", parse_station)


def read_sites(path, sheet, extra=None):
    """The five location columns of each row, plus extra(the next three columns) if given,
    then Body (Earth / Moon, blank = Earth): column F for places, I for ground stations.
    Moon sites: positions in the Moon's body-fixed frame on a sphere of the mean radius; ground
    elevation (metres above that radius) is never looked up -- blank = 0."""
    path = pick_file(path)
    if path is None:
        return []
    print(f"Reading {path.name}")
    try:
        rows = read_sheet(path, sheet)
    except (zipfile.BadZipFile, KeyError, ET.ParseError) as e:
        raise PlacesError(f"{path.name} could not be read as a spreadsheet ({e})")
    width = 9 if extra else 6
    places, errors, names = [], [], set()
    for n, row in enumerate(rows[1:], start=2):     # row 1 is the header
        row = (row + [None] * width)[:width]
        if all(c is None or (isinstance(c, str) and not c.strip()) for c in row):
            continue
        name, lat, lon, agl, ground = row[:5]
        try:
            name = (f"{name:g}" if isinstance(name, float) else str(name or "")).strip()
            if not name:
                raise PlacesError("Name is empty")
            if name in names:
                raise PlacesError(f"Name '{name}' is used twice")
            if lat is None or lon is None:
                raise PlacesError("Latitude and Longitude are both needed")
            place = {"name": name,
                     "lat": parse_angle(lat, "N", "S", 90, "Latitude"),
                     "lon": parse_angle(lon, "E", "W", 180, "Longitude"),
                     "agl_m": parse_metres(agl, "Altitude AGL") or 0.0,
                     "ground_m": parse_metres(ground, "Ground elevation"),
                     "body": parse_body(row[width - 1])}
            if extra:
                place.update(extra(row[5:8]))
            if place["body"] == "Moon" and place["ground_m"] is None:
                place["ground_m"], place["ground_source"] = 0.0, "mean lunar radius"
            names.add(name)
            places.append(place)
        except PlacesError as e:
            errors.append(f"{path.name} row {n}: {e}")
    if errors:
        raise PlacesError("\n".join(errors))
    lookup = [p for p in places if p["ground_m"] is None]
    for p, h in zip(lookup, lookup_elevations([(p["lat"], p["lon"]) for p in lookup])):
        p["ground_m"], p["ground_source"] = h, "Copernicus GLO-90 (Open-Meteo)"
    for p in places:
        p.setdefault("ground_source", path.name)
        fixed = moon_fixed if p["body"] == "Moon" else geodetic_to_ecef
        # body-fixed position: Earth-fixed (WGS84) or Moon-fixed (sphere)
        p["ecef_km"] = [round(c, 6) for c in fixed(p["lat"], p["lon"], (p["ground_m"] + p["agl_m"]) / 1000)]
    return places


# ---- models (unlit, flat colour; the viewer places and scales them) ----

def write_glb(path, name, verts, parts, mode):
    """parts: [(indices, colour)], one primitive and unlit material each."""
    vdata = b"".join(struct.pack("<3f", *v) for v in verts)
    data, views, accessors, prims, materials = vdata, [], [], [], []
    views.append({"buffer": 0, "byteOffset": 0, "byteLength": len(vdata), "target": 34962})
    accessors.append({"bufferView": 0, "componentType": 5126, "count": len(verts), "type": "VEC3",
                      "min": [min(v[k] for v in verts) for k in range(3)],
                      "max": [max(v[k] for v in verts) for k in range(3)]})
    for k, (indices, colour) in enumerate(parts):
        idata = b"".join(struct.pack("<H", i) for i in indices)
        views.append({"buffer": 0, "byteOffset": len(data), "byteLength": len(idata), "target": 34963})
        accessors.append({"bufferView": len(views) - 1, "componentType": 5123, "count": len(indices), "type": "SCALAR"})
        prims.append({"attributes": {"POSITION": 0}, "indices": len(accessors) - 1, "mode": mode, "material": k})
        materials.append({"name": f"{name}{k}", "extensions": {"KHR_materials_unlit": {}},
                          "pbrMetallicRoughness": {"baseColorFactor": list(colour) + [1], "metallicFactor": 0, "roughnessFactor": 1}})
        data += idata + b"\0" * (-len(idata) % 4)
    gltf = {
        "asset": {"version": "2.0", "generator": "BoneStar tools/places.py"},
        "extensionsUsed": ["KHR_materials_unlit"],
        "scene": 0, "scenes": [{"nodes": [0]}], "nodes": [{"mesh": 0, "name": name}],
        "meshes": [{"name": name, "primitives": prims}],
        "materials": materials,
        "buffers": [{"byteLength": len(data)}],
        "bufferViews": views,
        "accessors": accessors,
    }
    js = json.dumps(gltf, separators=(",", ":")).encode()
    js += b" " * (-len(js) % 4)
    glb = struct.pack("<III", 0x46546C67, 2, 12 + 8 + len(js) + 8 + len(data))
    glb += struct.pack("<II", len(js), 0x4E4F534A) + js + struct.pack("<II", len(data), 0x004E4942) + data
    path.write_bytes(glb)


def write_marker(path=MARKER_GLB, colour=PLACE_COLOUR, segments=16, rings=12):
    """A unit-radius UV sphere (the viewer scales it with the camera distance)."""
    verts = []
    for i in range(rings + 1):
        th = math.pi * i / rings
        for j in range(segments + 1):
            ph = 2 * math.pi * j / segments
            verts.append((math.sin(th) * math.cos(ph), math.cos(th), math.sin(th) * math.sin(ph)))
    idx = []
    for i in range(rings):
        for j in range(segments):
            a, b = i * (segments + 1) + j, (i + 1) * (segments + 1) + j
            idx += [a, a + 1, b, b, a + 1, b + 1]
    write_glb(path, "Marker", verts, [(idx, colour)], 4)


def write_link(path, dashed, colour=STATION_COLOUR, dashes=24):
    """A line from (0,0,0) to (0,1,0): the viewer turns it toward the spacecraft and stretches
    it to the distance. Solid for 2-way, dashed for 1-way."""
    verts = [(0, 0, 0), (0, 1, 0)]
    if dashed:
        verts = [(0, (k + f) / dashes, 0) for k in range(dashes) for f in (0, 0.55)]
    write_glb(path, "Link", verts, [(list(range(len(verts))), colour)], 1)


def write_grid(path=GRID_GLB, step=10, res=2, radius=1.0015,
               colour=(0.55, 0.62, 0.75), main_colour=(1.0, 0.78, 0.3)):
    """Latitude/longitude lines every `step` deg on a sphere of `radius` (unit = Earth radius;
    1.0015 is ~10 km up, just clear of the surface), Earth-fixed like earth.glb: the viewer
    scales it to the Earth and turns it with it. The equator and the Greenwich meridian are
    main_colour, the rest colour. GMAT Earth-fixed X,Y,Z -> glTF (-X, Z, Y) (glTFast negates X
    on import, giving Unity X, Z, Y as everywhere else in the viewer)."""
    verts, minor, main = [], [], []

    def point(lat, lon):
        la, lo = math.radians(lat), math.radians(lon)
        x, y, z = math.cos(la) * math.cos(lo), math.cos(la) * math.sin(lo), math.sin(la)
        verts.append((-x * radius, z * radius, y * radius))
        return len(verts) - 1

    def line(points, out):
        for a, b in zip(points, points[1:]):
            out += [a, b]

    for lon in range(-180, 180, step):          # meridians, pole to pole
        line([point(lat, lon) for lat in range(-90, 91, res)], main if lon == 0 else minor)
    for lat in range(-90 + step, 90, step):     # parallels
        line([point(lat, lon) for lon in range(-180, 181, res)], main if lat == 0 else minor)
    write_glb(path, "Grid", verts, [(minor, colour), (main, main_colour)], 1)


def write_select(path=SELECT_GLB, inner=0.82, colour=(1.0, 1.0, 1.0)):
    """A white square frame in the XY plane, outer half-size 1, inner half-size `inner`: the
    viewer puts it around the selected place or station, facing the camera. Each quad is in
    both windings, so it shows whichever way round the importer turns it."""
    verts = [(-1, -1, 0), (1, -1, 0), (1, 1, 0), (-1, 1, 0),
             (-inner, -inner, 0), (inner, -inner, 0), (inner, inner, 0), (-inner, inner, 0)]
    idx = []
    for k in range(4):
        o0, o1, i0, i1 = k, (k + 1) % 4, 4 + k, 4 + (k + 1) % 4
        quad = [o0, o1, i1, o0, i1, i0]
        idx += quad + quad[::-1]
    write_glb(path, "Select", verts, [(idx, colour)], 4)


def write_models():
    """The marker and link models, where missing (run_fleet.py calls this)."""
    if not MARKER_GLB.exists():
        write_marker(MARKER_GLB, PLACE_COLOUR)
    if not STATION_GLB.exists():
        write_marker(STATION_GLB, STATION_COLOUR)
    if not GRID_GLB.exists():
        write_grid()
    if not SELECT_GLB.exists():
        write_select()
    for link, path in LINK_GLBS.items():
        if not path.exists():
            write_link(path, link == "1-way")


if __name__ == "__main__":
    try:
        found = [("Place", p) for p in read_places()] + [("Station", p) for p in read_ground_stations()]
    except PlacesError as e:
        sys.exit(str(e))
    if not found:
        sys.exit(f"no places in {PLACES_XLSX.name} or {STATIONS_XLSX.name}")
    for kind, p in found:
        extra = f", {p['freq_mhz']:g} MHz, FOV {p['fov_deg']:g} deg, {p['link']}" if kind == "Station" else ""
        print(f"{kind} {p['name']}: {p['lat']:.6f}, {p['lon']:.6f}, ground {p['ground_m']:.1f} m "
              f"({p['ground_source']}) + {p['agl_m']:.1f} m AGL{extra} -> ECEF km {p['ecef_km']}")
