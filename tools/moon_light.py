#!/usr/bin/env python3
"""Bakes the Sun's light into the Moon models, so the viewer can draw the Moon unlit.

Why: WebVerse's renderer draws sun shadows only within 50 units (5000 km) of the camera, from one
2048 px shadow map (its URP asset; scripts cannot change it). Close to the Moon the lit surface
then shadows itself through that coarse map and goes a dull grey. An unlit material receives no
shadows, so the light is drawn into the colour maps here instead:

    colour = LROC colour x (GAIN x cos(incidence) x sun visible + AMBIENT)

- incidence: from each texel's terrain normal (the model's own normal maps, built by
  tools/make_moon.py from LOLA), so slopes facing the Sun are brighter;
- sun visible: the share of the Sun's disc above the terrain horizon toward the Sun (cast
  shadows), found by marching across the LOLA elevation map (16 px/deg) toward the Sun's
  azimuth, with the Moon's curvature -- the method tools/terrain.py uses for the sites' sunlight;
- GAIN = 0.22 x 6.25: the old base colour factor times the viewer's sun intensity, so the
  sunlit side keeps the brightness it had when lit by the runtime;
- AMBIENT: a faint stand-in for earthshine and the runtime's sky light, so the night side's
  terrain is just visible.

The Sun is taken where it is at one moment (the middle of the viewer's playback window); it
moves ~0.5 deg/h across the Moon, so a baked model is up to ~3 deg off at the window's ends. The
fleet job bakes again on every run.

    python tools/moon_light.py [levels]     bake for the Sun in data/fleet.json (a check)

The baked copies go to models/lit/moon_<level>x_<stamp>.glb (not committed): geometry and
heights as the built model, KHR_materials_unlit, the lit colour maps, no normal maps. The stamp
makes each run's file name new, since WebVerse caches models by URL.
"""
import io
import json
import math
import os
import struct
import sys
import time
from pathlib import Path

import numpy as np
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent))
from make_moon import R_KM, bilinear, load_dem, polar_axes, polar_grid, body_dirs   # noqa: E402

Image.MAX_IMAGE_PIXELS = None
ROOT = Path(__file__).resolve().parent.parent
LIT_DIR = ROOT / "models" / "lit"
SOURCE = {1: ROOT / "models" / "moon.glb", 2: ROOT / "models" / "moon_2x.glb", 4: ROOT / "models" / "moon_4x.glb"}
# WebVerse's local copy of models/lit (stale baked Moons are removed from it with ours)
WV_CACHE_LIT = Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "webverse" / "wv_cache" / \
    "http~" / "localhost~8000" / "models" / "lit"
GAIN = 0.22 * 6.25
AMBIENT = 0.03
SUN_RADIUS_DEG = 0.2666          # the Sun's disc seen from the Moon (~1 AU)
SHADOW_DEM = "ldem_16_uint.tif"  # 16 px/deg (1.9 km) for the horizon march
MARCH_STEPS = np.geomspace(1.0, 300.0, 48)   # km along the ground toward the Sun
MAX_HORIZON_DEG = 35.0           # no lunar horizon is higher: above this the Sun is always seen
MAX_SHADOW_PX = {"eq": 4096, "polar": 2048}  # shadows worked out at most at this width, then scaled
CHUNK = 1 << 18                  # texels per march batch


# ---- colour ----

def srgb_to_linear(c):
    c = c / 255.0
    return np.where(c <= 0.04045, c / 12.92, ((c + 0.055) / 1.055) ** 2.4)


def linear_to_srgb(x):
    x = np.clip(x, 0.0, 1.0)
    s = np.where(x <= 0.0031308, x * 12.92, 1.055 * np.power(x, 1 / 2.4) - 0.055)
    return np.round(s * 255).astype(np.uint8)


# ---- sunlight ----

def sun_visible(up, sun, dem):
    """Share of the Sun's disc above the terrain for points with unit body-axis normals `up`
    (N, 3) and the Sun's unit direction `sun` (3,): 1 = whole disc, 0 = none."""
    a = math.radians(SUN_RADIUS_DEG)
    el = np.arcsin(np.clip(up @ sun, -1, 1))
    out = np.where(el > a, 1.0, 0.0)
    todo = np.nonzero((el > -a) & (el < math.radians(MAX_HORIZON_DEG)))[0]
    for c0 in range(0, len(todo), CHUNK):
        idx = todo[c0:c0 + CHUNK]
        u = up[idx]
        d = sun - (u @ sun)[:, None] * u                       # toward the Sun along the ground
        d /= np.linalg.norm(d, axis=1, keepdims=True)
        lat0 = np.degrees(np.arcsin(np.clip(u[:, 2], -1, 1)))
        lon0 = np.degrees(np.arctan2(u[:, 1], u[:, 0]))
        eye = R_KM + bilinear(dem, lat0, lon0) + 0.002         # 2 m above the ground
        horizon = np.full(len(idx), -np.pi / 2)
        for dist in MARCH_STEPS:
            t = dist / R_KM
            q = math.cos(t) * u + math.sin(t) * d
            h = bilinear(dem, np.degrees(np.arcsin(np.clip(q[:, 2], -1, 1))), np.degrees(np.arctan2(q[:, 1], q[:, 0])))
            r = R_KM + h
            np.maximum(horizon, np.arctan2(r * math.cos(t) - eye, r * math.sin(t)), out=horizon)
        out[idx] = np.clip((el[idx] - horizon) / (2 * a) + 0.5, 0, 1)
    return out


def shadow_map(up_fn, w, h, sun, dem, max_w):
    """sun_visible on a grid of at most max_w wide, scaled to w x h. up_fn(w, h) -> (h, w, 3)."""
    sw = min(w, max_w)
    sh = max(1, round(h * sw / w))
    up = up_fn(sw, sh).reshape(-1, 3)
    vis = sun_visible(up, sun, dem).reshape(sh, sw).astype(np.float32)
    if (sw, sh) != (w, h):
        vis = np.asarray(Image.fromarray(vis, "F").resize((w, h), Image.Resampling.BILINEAR))
    return vis


def eq_up(w, h):
    lat = np.radians(90 - 180 * (np.arange(h) + 0.5) / h)
    lon = np.radians(-180 + 360 * (np.arange(w) + 0.5) / w)
    LON, LAT = np.meshgrid(lon, lat)
    return body_dirs(LAT, LON)[0]


def polar_up(south):
    def f(w, h):
        lat, lon = polar_grid(w, south)
        return body_dirs(lat, lon)[0]
    return f


def decode_normals(png):
    n = png.astype(np.float32) / 127.5 - 1.0
    return n / np.linalg.norm(n, axis=-1, keepdims=True)


def light(colour, nmap, frames, vis, sun):
    """Lit colour (uint8 sRGB): frames = (T, B, up) body-axis arrays (h, w, 3) of the texels."""
    T, B, up = frames
    n = decode_normals(nmap)
    normal = n[..., 0:1] * T + n[..., 1:2] * B + n[..., 2:3] * up
    cos_i = np.clip(normal @ sun, 0, None)
    factor = GAIN * cos_i * vis + AMBIENT
    return linear_to_srgb(srgb_to_linear(colour.astype(np.float32)) * factor[..., None])


def bake_images(images, sun, dem):
    """images: {"eq": (colour, normal), "north": ..., "south": ...} -> lit colour per key."""
    out = {}
    colour, nmap = images["eq"]
    h, w = colour.shape[:2]
    lat = np.radians(90 - 180 * (np.arange(h) + 0.5) / h)
    lon = np.radians(-180 + 360 * (np.arange(w) + 0.5) / w)
    LON, LAT = np.meshgrid(lon, lat)
    up, east, north = body_dirs(LAT, LON)
    if nmap.shape[:2] != (h, w):
        nmap = np.asarray(Image.fromarray(nmap).resize((w, h), Image.Resampling.BILINEAR))
    vis = shadow_map(eq_up, w, h, sun, dem, MAX_SHADOW_PX["eq"])
    out["eq"] = light(colour, nmap, (east, north, up), vis, sun)
    del up, east, north
    for key, south in (("north", False), ("south", True)):
        colour, nmap = images[key]
        size = colour.shape[0]
        plat, plon = polar_grid(size, south)
        up = body_dirs(plat, plon)[0]
        T, B = polar_axes(plat, plon, south)
        if nmap.shape[:2] != (size, size):
            nmap = np.asarray(Image.fromarray(nmap).resize((size, size), Image.Resampling.BILINEAR))
        vis = shadow_map(polar_up(south), size, size, sun, dem, MAX_SHADOW_PX["polar"])
        out[key] = light(colour, nmap, (T, B, up), vis, sun)
    return out


# ---- glb ----

def read_glb(path):
    data = path.read_bytes()
    jlen = struct.unpack("<I", data[12:16])[0]
    gltf = json.loads(data[20:20 + jlen])
    blen = struct.unpack("<I", data[20 + jlen:24 + jlen])[0]
    return gltf, data[28 + jlen:28 + jlen + blen]


def view_bytes(gltf, binary, v):
    bv = gltf["bufferViews"][v]
    o = bv.get("byteOffset", 0)
    return binary[o:o + bv["byteLength"]]


def material_images(gltf):
    """{material name key: (colour image index, normal image index)} -- eq / north / south."""
    keys = {"LROC equatorial": "eq", "LROC north polar": "north", "LROC south polar": "south"}
    out = {}
    for m in gltf["materials"]:
        tex = gltf["textures"]
        c = tex[m["pbrMetallicRoughness"]["baseColorTexture"]["index"]]["source"]
        n = tex[m["normalTexture"]["index"]]["source"] if "normalTexture" in m else None
        out[keys[m["name"]]] = (c, n)
    return out


def bake(level, sun_mf, out_path, note=None):
    """Write the lit, unlit-material copy of a Moon level for the Sun at sun_mf (Moon-fixed ME,
    any length). Returns seconds taken."""
    t0 = time.time()
    src = SOURCE[level]
    gltf, binary = read_glb(src)
    sun = np.asarray(sun_mf, dtype=np.float64)
    sun = sun / np.linalg.norm(sun)
    which = material_images(gltf)
    decode = lambda i: np.asarray(Image.open(io.BytesIO(view_bytes(gltf, binary, gltf["images"][i]["bufferView"]))))
    images = {k: (decode(c)[..., :3], decode(n)[..., :3]) for k, (c, n) in which.items()}
    lit = bake_images(images, sun, load_dem(SHADOW_DEM))

    # new buffer: every non-image view as it was, then the lit colour maps; normal maps dropped
    image_views = {img["bufferView"] for img in gltf["images"]}
    new_bin, views, remap = bytearray(), [], {}
    for v, bv in enumerate(gltf["bufferViews"]):
        if v in image_views:
            continue
        while len(new_bin) % 4:
            new_bin.append(0)
        nb = dict(bv, byteOffset=len(new_bin))
        new_bin += view_bytes(gltf, binary, v)
        remap[v] = len(views)
        views.append(nb)
    keys = {"LROC equatorial": "eq", "LROC north polar": "north", "LROC south polar": "south"}
    sampler = {keys[m["name"]]: gltf["textures"][m["pbrMetallicRoughness"]["baseColorTexture"]["index"]].get("sampler", 0)
               for m in gltf["materials"]}
    images, textures = [], []
    for key in ("eq", "north", "south"):
        b = io.BytesIO()
        Image.fromarray(lit[key]).save(b, "JPEG", quality=92)
        while len(new_bin) % 4:
            new_bin.append(0)
        views.append({"buffer": 0, "byteOffset": len(new_bin), "byteLength": len(b.getvalue())})
        new_bin += b.getvalue()
        images.append({"bufferView": len(views) - 1, "mimeType": "image/jpeg"})
        textures.append({"source": len(images) - 1, "sampler": sampler[key]})
    while len(new_bin) % 4:
        new_bin.append(0)
    for a in gltf["accessors"]:
        a["bufferView"] = remap[a["bufferView"]]
    order = ["eq", "north", "south"]
    for m in gltf["materials"]:
        m.pop("normalTexture", None)
        pbr = m["pbrMetallicRoughness"]
        pbr["baseColorFactor"] = [1.0, 1.0, 1.0, 1.0]
        pbr["baseColorTexture"]["index"] = order.index(keys[m["name"]])
        m["extensions"] = {"KHR_materials_unlit": {}}
    gltf["extensionsUsed"] = sorted(set(gltf.get("extensionsUsed", [])) | {"KHR_materials_unlit"})
    gltf["images"], gltf["textures"], gltf["bufferViews"] = images, textures, views
    gltf["buffers"] = [{"byteLength": len(new_bin)}]
    root = gltf["nodes"][gltf["scenes"][0]["nodes"][0]]
    ex = root.setdefault("extras", {})
    if "heights_km_bufferView" in ex:
        ex["heights_km_bufferView"] = remap[ex["heights_km_bufferView"]]
    ex["lit"] = {"sun_dir_moon_fixed": [round(float(c), 9) for c in sun], "gain": GAIN, "ambient": AMBIENT,
                 "note": note or ""}
    gltf["asset"]["generator"] = "BoneStar tools/make_moon.py + tools/moon_light.py"

    js = json.dumps(gltf, separators=(",", ":")).encode()
    js += b" " * (-len(js) % 4)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_path.with_suffix(".part")
    with open(tmp, "wb") as f:
        f.write(struct.pack("<III", 0x46546C67, 2, 12 + 8 + len(js) + 8 + len(new_bin)))
        f.write(struct.pack("<II", len(js), 0x4E4F534A) + js)
        f.write(struct.pack("<II", len(new_bin), 0x004E4942) + new_bin)
    tmp.replace(out_path)
    return time.time() - t0


def lit_name(level, stamp):
    return f"moon_{level}x_{stamp}.glb"


def clean(keep):
    """Remove baked Moons other than the names in `keep`, here and from WebVerse's cache."""
    for folder in (LIT_DIR, WV_CACHE_LIT):
        if folder.is_dir():
            for f in folder.glob("moon_*x_*.glb"):
                if f.name not in keep:
                    try:
                        f.unlink()
                    except OSError:
                        pass        # WebVerse may hold it open; it goes next time


# ---- for the local server: a level's bake for the current fleet run ----

def fleet_lit(stamp=None):
    """fleet.json moon.lit ({stamp, t, sun_mf, files}); ValueError if missing or (when a stamp
    is given) from another fleet run."""
    lit = json.loads((ROOT / "data" / "fleet.json").read_text()).get("moon", {}).get("lit")
    if not lit:
        raise ValueError("data/fleet.json has no baked Moon: rerun tools/run_fleet.py")
    if stamp is not None and stamp != lit["stamp"]:
        raise ValueError(f"fleet run {stamp} is no longer current ({lit['stamp']}): reload the viewer")
    return lit


def cached(level, stamp=None):
    """The level's bake for the current run ("models/lit/..."), or None if not baked yet."""
    lit = fleet_lit(stamp)
    if level not in SOURCE or not SOURCE[level].exists():
        raise ValueError(f"the {level}x Moon is not built (python tools/make_moon.py {level})")
    path = LIT_DIR / lit_name(level, lit["stamp"])
    return f"models/lit/{path.name}" if path.exists() else None


def bake_level(level, stamp=None):
    lit = fleet_lit(stamp)
    path = LIT_DIR / lit_name(level, lit["stamp"])
    secs = bake(level, lit["sun_mf"], path, f"fleet run {lit['stamp']}, t = {lit['t']:.0f} s")
    print(f"Moon {level}x baked for fleet run {lit['stamp']}: {path.name}, {path.stat().st_size / 1e6:.1f} MB "
          f"in {secs:.0f} s", flush=True)
    return f"models/lit/{path.name}"


if __name__ == "__main__":
    for lv in [int(a) for a in sys.argv[1:]] or [1]:
        print(cached(lv) or bake_level(lv))
