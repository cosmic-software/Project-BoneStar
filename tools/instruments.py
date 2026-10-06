#!/usr/bin/env python3
"""Spacecraft models: their size, their instrument cameras, and the copy the viewer loads.

    python tools/instruments.py     list the models in models/ with their size and cameras

Every model lives in models/ (a .glb; spacecraft.xlsx, sheet Models, says which spacecraft
uses which; probe.glb otherwise). Build them like probe.glb, in the exported .glb's axes:

    +X  along the velocity: the front, and the instrument boresight (the attitude modes point
        this axis: LVLH along the velocity, Nadir at the body below, Sun, Target ...)
    Y   along the solar arrays (the attitude modes keep the arrays on the orbit normal)

(Blender's glTF export with +Y Up, the default, writes Blender's Z as the file's Y: in Blender,
the arrays along Z.) A model may carry cameras for the instrument view (in Blender: add a Camera, parent it
to the model, point it along the instrument, set its field of view, export with cameras). The
fleet job then:

- reads each camera: where it sits and points in the model and its vertical field of view
  (glTF cameras look along their local -Z, up +Y), converted to the axes WebVerse gives the
  loaded model (glTFast mirrors X), into data/fleet.json ("instruments");
- writes models/view/<name>.glb, the copy the viewer loads, without the cameras: WebVerse loads
  models with glTFast's default settings, which turn a glTF camera into a live Unity camera that
  would draw over the viewer's own. A model without cameras is loaded as it is. Your file in
  models/ is never changed;
- measures the model's longest side, so every spacecraft is drawn at the same size on screen
  (MODEL_LENGTH units: hugely enlarged, so it can be seen at all at 1 unit = 100 km).
"""
import json
import math
import struct
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MODELS_DIR = ROOT / "models"


def read_glb(path):
    b = Path(path).read_bytes()
    n = struct.unpack("<I", b[12:16])[0]
    gltf = json.loads(b[20:20 + n])
    rest = b[20 + n:]                     # the BIN chunk (header included), untouched
    return gltf, rest


def write_glb(path, gltf, rest):
    js = json.dumps(gltf, separators=(",", ":")).encode()
    js += b" " * (-len(js) % 4)
    glb = struct.pack("<III", 0x46546C67, 2, 12 + 8 + len(js) + len(rest))
    glb += struct.pack("<II", len(js), 0x4E4F534A) + js + rest
    Path(path).write_bytes(glb)


def qmul(a, b):
    ax, ay, az, aw = a
    bx, by, bz, bw = b
    return [aw * bx + ax * bw + ay * bz - az * by, aw * by - ax * bz + ay * bw + az * bx,
            aw * bz + ax * by - ay * bx + az * bw, aw * bw - ax * bx - ay * by - az * bz]


def qrot(q, v):
    x, y, z = v
    tx, ty, tz = 2 * (q[1] * z - q[2] * y), 2 * (q[2] * x - q[0] * z), 2 * (q[0] * y - q[1] * x)
    return [x + q[3] * tx + (q[1] * tz - q[2] * ty), y + q[3] * ty + (q[2] * tx - q[0] * tz),
            z + q[3] * tz + (q[0] * ty - q[1] * tx)]


def world_transforms(gltf):
    """Each node's (translation, rotation, scale) in the model's root space (no shear)."""
    nodes = gltf.get("nodes", [])
    parent = {}
    for i, nd in enumerate(nodes):
        for c in nd.get("children", []):
            parent[c] = i
    out = {}

    def get(i):
        if i in out:
            return out[i]
        nd = nodes[i]
        if "matrix" in nd:
            raise ValueError(f"node {nd.get('name', i)} uses a matrix; export with TRS")
        t, r, s = nd.get("translation", [0, 0, 0]), nd.get("rotation", [0, 0, 0, 1]), nd.get("scale", [1, 1, 1])
        if i in parent:
            pt, pr, ps = get(parent[i])
            t = [pt[k] + v for k, v in enumerate(qrot(pr, [t[k] * ps[k] for k in range(3)]))]
            r = qmul(pr, r)
            s = [ps[k] * s[k] for k in range(3)]
        out[i] = (t, r, s)
        return out[i]
    for i in range(len(nodes)):
        get(i)
    return out


def cameras(gltf):
    """[{name, pos, fwd, up, yfov_deg, aspect}] in the loaded model's Unity axes (X mirrored)."""
    found = []
    tr = world_transforms(gltf)
    for i, nd in enumerate(gltf.get("nodes", [])):
        if "camera" not in nd:
            continue
        cam = gltf["cameras"][nd["camera"]]
        if cam.get("type") != "perspective":
            continue
        t, r, _ = tr[i]
        fwd, up = qrot(r, [0, 0, -1]), qrot(r, [0, 1, 0])        # glTF cameras look down -Z
        mirror = lambda v: [round(-v[0], 6), round(v[1], 6), round(v[2], 6)]
        p = cam["perspective"]
        found.append({"name": nd.get("name") or cam.get("name") or f"camera {len(found) + 1}",
                      "pos": mirror(t), "fwd": mirror(fwd), "up": mirror(up),
                      "yfov_deg": round(math.degrees(p["yfov"]), 6), "aspect": p.get("aspectRatio", 1.0)})
    return found


def strip_cameras(gltf):
    """The same glTF without cameras (a copy)."""
    g = json.loads(json.dumps(gltf))
    g.pop("cameras", None)
    for nd in g.get("nodes", []):
        nd.pop("camera", None)
    return g


VIEW_DIR = MODELS_DIR / "view"
MODEL_LENGTH = 1.64            # units: a model's longest side on screen (probe.glb's at scale 0.05)


def longest_side(gltf):
    """The model's longest bounding-box side (its own units), from every mesh's bounds."""
    tr = world_transforms(gltf)
    lo, hi = [math.inf] * 3, [-math.inf] * 3
    for i, nd in enumerate(gltf.get("nodes", [])):
        if "mesh" not in nd:
            continue
        t, r, sc = tr[i]
        for pr in gltf["meshes"][nd["mesh"]]["primitives"]:
            acc = gltf["accessors"][pr["attributes"]["POSITION"]]
            mn, mx = acc["min"], acc["max"]
            for cx in (mn[0], mx[0]):
                for cy in (mn[1], mx[1]):
                    for cz in (mn[2], mx[2]):
                        v = qrot(r, [cx * sc[0], cy * sc[1], cz * sc[2]])
                        for k in range(3):
                            lo[k], hi[k] = min(lo[k], v[k] + t[k]), max(hi[k], v[k] + t[k])
    return max(hi[k] - lo[k] for k in range(3))


def prepare_models(names):
    """{model file name: {cameras, url, scale}} for each model named (files in models/): the
    URL the viewer loads (models/view/<name> when the model has cameras), and the scale that
    draws it MODEL_LENGTH long."""
    out = {}
    for name in sorted(set(names)):
        src = MODELS_DIR / name
        if not src.exists():
            raise ValueError(f"no model models/{name}")
        gltf, rest = read_glb(src)
        cams = cameras(gltf)
        url = f"models/{name}"
        if cams:
            VIEW_DIR.mkdir(exist_ok=True)
            served = VIEW_DIR / name
            clean = strip_cameras(gltf)
            current = read_glb(served) if served.exists() else None
            if current is None or current[0] != clean or current[1] != rest:
                write_glb(served, clean, rest)
                print(f"models/view/{name}: written from models/{name} without its cameras "
                      f"(WebVerse caches models: clear its cached copy if the geometry changed)")
            url = f"models/view/{name}"
        out[name] = {"cameras": cams, "url": url, "scale": round(MODEL_LENGTH / longest_side(gltf), 6)}
    return out


if __name__ == "__main__":
    for src in sorted(MODELS_DIR.glob("*.glb")):
        gltf = read_glb(src)[0]
        cams = cameras(gltf)
        print(f"{src.name}: longest side {longest_side(gltf):.2f}, {len(cams)} camera(s)")
        for c in cams:
            print(f"  {c['name']}: at {c['pos']}, looking {c['fwd']}, up {c['up']}, "
                  f"vertical FOV {c['yfov_deg']:g} deg, aspect {c['aspect']:g}")
