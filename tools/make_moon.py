#!/usr/bin/env python3
"""Builds the Moon models from NASA's CGI Moon Kit (SVS 4720, public domain):

    python tools/make_moon.py            all levels: models/moon.glb (1x), moon_2x.glb, moon_4x.glb
    python tools/make_moon.py 2 4        just those levels

The source maps are downloaded once into data/moon_source/ (not committed): the LRO LROC WAC
colour map and the LRO LOLA elevation map (16-bit, half-metres; height km = value / 2000 - 10,
above the 1737.4 km mean radius).

Level  vertex grid    triangles  colour (equatorial)  normal map   polar maps   elevation
  1x   512 x 256        262 144  2048 x 1024          2048 x 1024  1024 x 1024  16 px/deg
  2x   1024 x 512     1 048 576  4096 x 2048          4096 x 2048  2048 x 2048  16 px/deg
  4x   2048 x 1024    4 194 304  8192 x 4096          8192 x 4096  4096 x 4096  64 px/deg

The Moon's own coordinate system is kept at every level and for any texture size, because the
UVs are defined by latitude and longitude, not by pixels:
  TEXCOORD_0 (equatorial): equirectangular, u = 0.5 + lon/360, v = 0.5 - lat/180 (the maps are
    centred on 0 deg longitude, east positive).
  TEXCOORD_1 (polar): polar stereographic about the vertex's own pole, PDS / LOLA convention on
    the unit sphere -- south: x = 2 tan(45 + lat/2) sin(lon), y = 2 tan(45 + lat/2) cos(lon);
    north: x = 2 tan(45 - lat/2) sin(lon), y = -2 tan(45 - lat/2) cos(lon) -- then
    u = 0.5 + x / (2 E), v = 0.5 - y / (2 E), E = 2 tan(17.5 deg), so a polar image's edge is
    at 55 deg latitude on its axes (its corners reach ~47 deg).
Every vertex carries both. Latitudes from 60 deg to the poles are drawn with the polar maps
(one image per pole), the band between with the equatorial maps; any map in either projection
and at any resolution drops in.

Geometry: a latitude/longitude grid, each vertex at radius 1 + h / 1737.4 (h = LOLA height,
bilinear; the pole vertices take the mean of the polar row so the fan closes), cut into tiles
(30 deg of latitude x 22.5 deg of longitude) of under 65 536 vertices each -- 16-bit indices,
and Unity culls the tiles that are out of view. Vertex normals are the sphere's: the normal maps
carry all the slopes (baked from the same LOLA data), so they are not counted twice. Tangents
follow each map's +u (equatorial: east; polar: the image's x axis, which stays defined at the
pole). glTF positions are (-X, Z, Y) of the Moon-fixed point, as earth.glb (glTFast mirrors X).
The mesh's extras give the vertex heights (km) so tools/places.py can put lunar sites on the
drawn surface.
"""
import io
import json
import math
import struct
import sys
import urllib.request
from pathlib import Path

import numpy as np
from PIL import Image

Image.MAX_IMAGE_PIXELS = None
ROOT = Path(__file__).resolve().parent.parent
SOURCE_DIR = ROOT / "data" / "moon_source"
SOURCE_URL = "https://svs.gsfc.nasa.gov/vis/a000000/a004700/a004720/"
R_KM = 1737.4
POLAR_EDGE_DEG = 55.0                    # polar images reach this latitude on their axes
POLAR_E = 2 * math.tan(math.radians((90 - POLAR_EDGE_DEG) / 2))
# Base colour factor on the LROC map. The viewer's sun is a directional light of intensity 6.25
# (orbit.js SUN_INTENSITY); Unity's lit shader gives colour x intensity x cos(incidence), so at
# 0.22 the map's median (linear luminance 0.34) shows at 0.47 and its brightest 1% (0.59) at 0.82
# under a high Sun, short of white (0.4 saturated half the sunlit surface).
ALBEDO = 0.22
CAP_DEG = 60.0                           # polar maps used poleward of this
LAT_BREAKS = [90, 60, 30, 0, -30, -60, -90]
SECTORS = 16                             # tiles in longitude
LEVELS = {
    1: {"file": "moon.glb", "seg": 512, "ring": 256, "colour": "lroc_color_poles_2k.tif",
        "dem": "ldem_16_uint.tif", "normal": 2048, "polar": 1024},
    2: {"file": "moon_2x.glb", "seg": 1024, "ring": 512, "colour": "lroc_color_poles_4k.tif",
        "dem": "ldem_16_uint.tif", "normal": 4096, "polar": 2048},
    4: {"file": "moon_4x.glb", "seg": 2048, "ring": 1024, "colour": "lroc_color_poles_8k.tif",
        "dem": "ldem_64_uint.tif", "normal": 8192, "polar": 4096},
}


# ---- sources ----

def source(name):
    path = SOURCE_DIR / name
    if not path.exists():
        SOURCE_DIR.mkdir(parents=True, exist_ok=True)
        print(f"Downloading {name} from NASA SVS ...")
        part = path.with_suffix(path.suffix + ".part")
        with urllib.request.urlopen(SOURCE_URL + name, timeout=600) as r, open(part, "wb") as f:
            while True:
                chunk = r.read(1 << 22)
                if not chunk:
                    break
                f.write(chunk)
        part.rename(path)
    return path


def load_dem(name):
    """LOLA height (km above 1737.4), float32, rows from 90 N, columns from 180 W."""
    a = np.asarray(Image.open(source(name)))
    return (a.astype(np.float32) / 2000.0 - 10.0)


def load_colour(name):
    return np.asarray(Image.open(source(name)).convert("RGB"))


def bilinear(img, lat, lon):
    """Sample an equirectangular map (pixel centres, wraps in longitude) at lat/lon (deg).
    Works for 2-D (H, W) and 3-D (H, W, C) arrays."""
    h, w = img.shape[:2]
    y = np.clip((90.0 - lat) / 180.0 * h - 0.5, 0, h - 1)
    x = (lon + 180.0) / 360.0 * w - 0.5
    y0 = np.minimum(np.floor(y).astype(np.int64), h - 2)
    x0 = np.floor(x).astype(np.int64)
    fy, fx = y - y0, x - x0
    x0w, x1w = x0 % w, (x0 + 1) % w
    if img.ndim == 3:
        fy, fx = fy[..., None], fx[..., None]
    a = img[y0, x0w] * (1 - fx) + img[y0, x1w] * fx
    b = img[y0 + 1, x0w] * (1 - fx) + img[y0 + 1, x1w] * fx
    return a * (1 - fy) + b * fy


def box_resize(a, size):
    """Area-average a float map to size (w, h)."""
    return np.asarray(Image.fromarray(a.astype(np.float32), "F").resize(size, Image.Resampling.BOX),
                      dtype=np.float64)


# ---- frames (Moon-fixed body axes; glTF = (-X, Z, Y)) ----

def body_dirs(lat, lon):
    """Unit up, east and north (arrays (..., 3), body axes) at lat/lon in radians."""
    cl, sl, co, so = np.cos(lat), np.sin(lat), np.cos(lon), np.sin(lon)
    up = np.stack([cl * co, cl * so, sl], -1)
    east = np.stack([-so, co, np.zeros_like(lat)], -1)
    north = np.stack([-sl * co, -sl * so, cl], -1)
    return up, east, north


def to_gltf(v):
    return np.stack([-v[..., 0], v[..., 2], v[..., 1]], -1)


def polar_xy(lat, lon, south):
    """Polar stereographic x, y (unit sphere, PDS convention), lat/lon in radians."""
    if south:
        rho = 2 * np.tan(np.pi / 4 + lat / 2)
        return rho * np.sin(lon), rho * np.cos(lon)
    rho = 2 * np.tan(np.pi / 4 - lat / 2)
    return rho * np.sin(lon), -rho * np.cos(lon)


def polar_latlon(x, y, south):
    rho = np.hypot(x, y)
    if south:
        return 2 * np.arctan(rho / 2) - np.pi / 2, np.arctan2(x, y)
    return np.pi / 2 - 2 * np.arctan(rho / 2), np.arctan2(x, -y)


def polar_axes(lat, lon, south):
    """The polar image's +x (T) and up (-v, B) directions on the surface, body axes."""
    _, east, north = body_dirs(lat, lon)
    s, c = np.sin(lon)[..., None], np.cos(lon)[..., None]
    if south:
        return s * north + c * east, c * north - s * east
    return -s * north + c * east, c * north + s * east


# ---- normal maps ----

def encode_normals(nx, ny):
    n = np.stack([nx, ny, np.ones_like(nx)], -1)
    n /= np.linalg.norm(n, axis=-1, keepdims=True)
    return np.round((n + 1) * 127.5).clip(0, 255).astype(np.uint8)


def equatorial_normal_map(dem, w, h):
    """Tangent space (T = east, B = north), from the DEM area-averaged to w x h."""
    height = box_resize(dem, (w, h))
    lat_c = np.radians(90 - 180 * (np.arange(h) + 0.5) / h)
    dlon, dlat = 2 * np.pi / w, np.pi / h
    rows = np.arange(h)
    rn, rs = np.maximum(rows - 1, 0), np.minimum(rows + 1, h - 1)
    sn = (height[rn] - height[rs]) / ((rs - rn)[:, None] * dlat * R_KM)
    se = np.empty_like(height)
    ks = np.empty(h, dtype=int)
    for r in range(h):
        # east slope over ~constant ground length (k ~ 1/cos lat columns): the converging
        # columns near the poles would otherwise blow up (the polar maps cover them anyway)
        c = math.cos(lat_c[r])
        k = int(min(max(round(1 / c), 1), w // 8))
        ks[r] = k
        se[r] = (np.roll(height[r], -k) - np.roll(height[r], k)) / (2 * k * dlon * R_KM * c)
    return encode_normals(-se, -sn), height, ks


def polar_grid(size, south):
    """lat, lon (radians) of a polar image's texel centres, and their unit body positions."""
    u = (np.arange(size) + 0.5) / size
    x = (u - 0.5) * 2 * POLAR_E
    y = (0.5 - u) * 2 * POLAR_E                  # rows: v down = y down
    xx, yy = np.meshgrid(x, y)
    lat, lon = polar_latlon(xx, yy, south)
    return lat, lon


def polar_maps(dem, colour, size, south):
    """Colour and normal map (T = image +x, B = image up) of one pole, size x size."""
    lat, lon = polar_grid(size, south)
    lat_d, lon_d = np.degrees(lat), np.degrees(lon)
    rgb = np.round(bilinear(colour.astype(np.float32), lat_d, lon_d)).clip(0, 255).astype(np.uint8)
    height = bilinear(dem, lat_d, lon_d).astype(np.float64)
    up, _, _ = body_dirs(lat, lon)
    pos = up * R_KM
    cols = np.arange(size)
    cl, cr = np.maximum(cols - 1, 0), np.minimum(cols + 1, size - 1)
    sx = (height[:, cr] - height[:, cl]) / np.linalg.norm(pos[:, cr] - pos[:, cl], axis=-1)
    su = (height[cl, :] - height[cr, :]) / np.linalg.norm(pos[cl, :] - pos[cr, :], axis=-1)   # rows up
    return rgb, encode_normals(-sx, -su), height


# ---- glTF ----

class Glb:
    def __init__(self):
        self.bin = bytearray()
        self.views, self.accessors = [], []

    def view(self, data, target=None):
        while len(self.bin) % 4:
            self.bin.append(0)
        v = {"buffer": 0, "byteOffset": len(self.bin), "byteLength": len(data)}
        if target:
            v["target"] = target
        self.bin += data
        self.views.append(v)
        return len(self.views) - 1

    def accessor(self, array, kind, ctype, target, minmax=False):
        a = {"bufferView": self.view(array.tobytes(), target), "componentType": ctype,
             "count": int(array.shape[0]), "type": kind}
        if minmax:
            a["min"] = [float(x) for x in array.min(0)]
            a["max"] = [float(x) for x in array.max(0)]
        self.accessors.append(a)
        return len(self.accessors) - 1

    def image(self, img, fmt):
        b = io.BytesIO()
        if fmt == "JPEG":
            Image.fromarray(img).save(b, "JPEG", quality=90)
        else:
            Image.fromarray(img).save(b, "PNG", compress_level=6)
        return self.view(b.getvalue()), "image/jpeg" if fmt == "JPEG" else "image/png"


def build(level):
    cfg = LEVELS[level]
    seg, ring = cfg["seg"], cfg["ring"]
    print(f"== {level}x: {seg} x {ring} vertices -> models/{cfg['file']}")
    dem = load_dem(cfg["dem"])
    colour = load_colour(cfg["colour"])
    print(f"   elevation {dem.shape[1]} x {dem.shape[0]}, km {dem.min():.2f} .. {dem.max():.2f}; "
          f"colour {colour.shape[1]} x {colour.shape[0]}")

    # vertex grid
    lat_d = 90 - 180 * np.arange(ring + 1) / ring
    lon_d = -180 + 360 * np.arange(seg + 1) / seg
    LON, LAT = np.meshgrid(lon_d, lat_d)
    hv = bilinear(dem, LAT, LON).astype(np.float64)
    hv[0, :] = dem[0].mean()
    hv[-1, :] = dem[-1].mean()
    lat, lon = np.radians(LAT), np.radians(LON)
    up, east, north = body_dirs(lat, lon)
    pos = to_gltf(up * (1 + hv / R_KM)[..., None])
    nrm = to_gltf(up)
    uv0 = np.stack([0.5 + LON / 360, 0.5 - LAT / 180], -1)
    south = LAT < 0
    xs, ys = polar_xy(lat, lon, True)
    xn, yn = polar_xy(lat, lon, False)
    px, py = np.where(south, xs, xn), np.where(south, ys, yn)
    uv1 = np.stack([0.5 + px / (2 * POLAR_E), 0.5 - py / (2 * POLAR_E)], -1)
    print(f"   vertex heights km {hv.min():.3f} .. {hv.max():.3f}")

    # tangents: equatorial = east; polar = the polar image's +x; w from cross(N, T) . B
    def tangents(T, B):
        Tg, Bg = to_gltf(T), to_gltf(B)
        w = np.sign(np.einsum("...k,...k", np.cross(nrm, Tg), Bg))
        w[w == 0] = 1
        return np.concatenate([Tg, w[..., None]], -1)
    tan_eq = tangents(east, north)
    Ts, Bs = polar_axes(lat, lon, True)
    Tn, Bn = polar_axes(lat, lon, False)
    tan_pol = tangents(np.where(south[..., None], Ts, Tn), np.where(south[..., None], Bs, Bn))

    # textures
    nmap, eq_height, ks = equatorial_normal_map(dem, cfg["normal"], cfg["normal"] // 2)
    polar = {}
    for name, is_south in (("north", False), ("south", True)):
        polar[name] = polar_maps(dem, colour, cfg["polar"], is_south)
    print(f"   maps: equatorial {colour.shape[1]} x {colour.shape[0]} + normal {nmap.shape[1]} x {nmap.shape[0]}, "
          f"polar {cfg['polar']} x {cfg['polar']} (colour + normal) per pole")

    g = Glb()
    images, textures = [], []
    for img, fmt in ((colour, "JPEG"), (nmap, "PNG"), (polar["north"][0], "JPEG"), (polar["north"][1], "PNG"),
                     (polar["south"][0], "JPEG"), (polar["south"][1], "PNG")):
        v, mime = g.image(img, fmt)
        images.append({"bufferView": v, "mimeType": mime})
        textures.append({"source": len(images) - 1, "sampler": 0 if len(images) <= 2 else 1})
    samplers = [{"magFilter": 9729, "minFilter": 9987, "wrapS": 10497, "wrapT": 33071},   # equatorial
                {"magFilter": 9729, "minFilter": 9987, "wrapS": 33071, "wrapT": 33071}]   # polar
    base = {"baseColorFactor": [ALBEDO, ALBEDO, ALBEDO, 1], "metallicFactor": 0, "roughnessFactor": 1}
    materials = [
        {"name": "LROC equatorial", "pbrMetallicRoughness": dict(base, baseColorTexture={"index": 0, "texCoord": 0}),
         "normalTexture": {"index": 1, "texCoord": 0}},
        {"name": "LROC north polar", "pbrMetallicRoughness": dict(base, baseColorTexture={"index": 2, "texCoord": 1}),
         "normalTexture": {"index": 3, "texCoord": 1}},
        {"name": "LROC south polar", "pbrMetallicRoughness": dict(base, baseColorTexture={"index": 4, "texCoord": 1}),
         "normalTexture": {"index": 5, "texCoord": 1}},
    ]

    # tiles
    row_breaks = [int(round((90 - b) / 180 * ring)) for b in LAT_BREAKS]
    col_breaks = [int(round(k * seg / SECTORS)) for k in range(SECTORS + 1)]
    nodes, meshes = [], []
    tris = 0
    for bi in range(len(row_breaks) - 1):
        r0, r1 = row_breaks[bi], row_breaks[bi + 1]
        mid = (LAT_BREAKS[bi] + LAT_BREAKS[bi + 1]) / 2
        mat = 1 if mid > CAP_DEG else (2 if mid < -CAP_DEG else 0)
        for si in range(SECTORS):
            c0, c1 = col_breaks[si], col_breaks[si + 1]
            sl = (slice(r0, r1 + 1), slice(c0, c1 + 1))
            nr, nc = r1 - r0 + 1, c1 - c0 + 1
            assert nr * nc <= 65536
            flat = lambda a: np.ascontiguousarray(a[sl].reshape(nr * nc, -1), dtype=np.float32)
            ii, jj = np.meshgrid(np.arange(nr - 1), np.arange(nc - 1), indexing="ij")
            a = (ii * nc + jj).ravel()
            b = a + nc
            idx = np.stack([a, b, a + 1, a + 1, b, b + 1], -1).ravel().astype("<u2")
            tris += len(idx) // 3
            attrs = {"POSITION": g.accessor(flat(pos), "VEC3", 5126, 34962, minmax=True),
                     "NORMAL": g.accessor(flat(nrm), "VEC3", 5126, 34962),
                     "TANGENT": g.accessor(flat(tan_pol if mat else tan_eq), "VEC4", 5126, 34962),
                     "TEXCOORD_0": g.accessor(flat(uv0), "VEC2", 5126, 34962),
                     "TEXCOORD_1": g.accessor(flat(uv1), "VEC2", 5126, 34962)}
            ind = g.accessor(idx, "SCALAR", 5123, 34963)
            meshes.append({"name": f"Moon {bi}-{si}", "primitives": [{"attributes": attrs, "indices": ind,
                                                                         "material": mat}]})
            nodes.append({"mesh": len(meshes) - 1, "name": f"Moon {bi}-{si}"})
    heights = g.view(np.ascontiguousarray(hv, dtype="<f4").tobytes())
    gltf = {
        "asset": {"version": "2.0", "generator": "BoneStar tools/make_moon.py"},
        "scene": 0, "scenes": [{"nodes": [len(nodes)]}],
        "nodes": nodes + [{"name": "Moon", "children": list(range(len(nodes))),
                           "extras": {"level": level, "grid": [seg, ring], "radius_km": R_KM,
                                      "heights_km_bufferView": heights,
                                      "uv0": "equirectangular: u = 0.5 + lon/360, v = 0.5 - lat/180",
                                      "uv1": f"polar stereographic (PDS), unit sphere, own pole, "
                                             f"u = 0.5 + x/(2E), v = 0.5 - y/(2E), E = {POLAR_E:.9f} "
                                             f"(edge {POLAR_EDGE_DEG:g} deg)",
                                      "cap_deg": CAP_DEG}}],
        "meshes": meshes, "materials": materials, "textures": textures, "samplers": samplers,
        "images": images, "accessors": g.accessors, "bufferViews": g.views,
        "buffers": [{"byteLength": len(g.bin)}],
    }
    while len(g.bin) % 4:
        g.bin.append(0)
    gltf["buffers"][0]["byteLength"] = len(g.bin)
    js = json.dumps(gltf, separators=(",", ":")).encode()
    js += b" " * (-len(js) % 4)
    out = ROOT / "models" / cfg["file"]
    with open(out, "wb") as f:
        f.write(struct.pack("<III", 0x46546C67, 2, 12 + 8 + len(js) + 8 + len(g.bin)))
        f.write(struct.pack("<II", len(js), 0x4E4F534A) + js)
        f.write(struct.pack("<II", len(g.bin), 0x004E4942) + g.bin)
    print(f"   {out.name}: {out.stat().st_size / 1e6:.1f} MB, {len(nodes)} tiles, "
          f"{(seg + 1) * (ring + 1)} grid vertices, {tris} triangles")
    return {"level": level, "hv": hv, "eq_height": eq_height, "ks": ks, "polar": polar, "nmap": nmap}


if __name__ == "__main__":
    for lv in [int(a) for a in sys.argv[1:]] or sorted(LEVELS):
        build(lv)
