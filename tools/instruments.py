#!/usr/bin/env python3
"""Instrument cameras from spacecraft models.

    python tools/instruments.py     list the cameras found in models/source/*.glb

Author a spacecraft model with one or more cameras in it (in Blender: add a Camera object,
parent it to the model, point it along the instrument's boresight, set its field of view, and
export glTF with cameras included) and save it as models/source/<name>.glb. The fleet job then:

- reads every node of object type camera: where it sits and points in the model and its
  vertical field of view (glTF cameras look along their local -Z, up +Y), converted to the
  axes WebVerse gives the loaded model (glTFast mirrors X), and passes them to the viewer in
  data/fleet.json ("instruments");
- writes models/<name>.glb, the copy the viewer loads, with the cameras taken out. WebVerse
  loads models with glTFast's default settings, which turn a glTF camera into a live Unity
  camera -- one per spacecraft -- that would draw over the viewer's own camera. The copy is
  rewritten only when its content changes (WebVerse caches models by URL).

The viewer's Show data (pie menu) moves its camera to the instrument camera and frames the
instrument's field of view (WebVerse scripts cannot change the camera's own field of view).
"""
import json
import math
import struct
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SOURCE_DIR = ROOT / "models" / "source"
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


def prepare_models():
    """{model file name: [cameras]} for every models/source/*.glb, writing the camera-free copy
    the viewer loads into models/ when its content differs."""
    instruments = {}
    for src in sorted(SOURCE_DIR.glob("*.glb")):
        gltf, rest = read_glb(src)
        instruments[src.name] = cameras(gltf)
        served = MODELS_DIR / src.name
        clean = strip_cameras(gltf)
        current = read_glb(served) if served.exists() else None
        if current is None or current[0] != clean or current[1] != rest:
            write_glb(served, clean, rest)
            print(f"models/{src.name}: rewritten from models/source/{src.name} without its cameras "
                  f"(WebVerse caches models: clear its cached copy if the geometry changed)")
    return instruments


if __name__ == "__main__":
    found = {src.name: cameras(read_glb(src)[0]) for src in sorted(SOURCE_DIR.glob("*.glb"))}
    if not found:
        sys.exit(f"no models in {SOURCE_DIR}")
    for name, cams in found.items():
        print(f"{name}: {len(cams)} camera(s)")
        for c in cams:
            print(f"  {c['name']}: at {c['pos']}, looking {c['fwd']}, up {c['up']}, "
                  f"vertical FOV {c['yfov_deg']:g} deg, aspect {c['aspect']:g}")
