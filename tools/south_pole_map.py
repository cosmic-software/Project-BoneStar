#!/usr/bin/env python3
"""Footprint figure: how the LOLA elevation map (LDEM) is laid down at the lunar south pole.

    python tools/south_pole_map.py      -> docs/images/south-pole-layout.png

Drawn in the projection the Moon models use for their polar caps (tools/make_moon.py, UV1):
polar stereographic about the south pole, PDS / LOLA convention on the unit sphere,
x = 2 tan(45 + lat/2) sin(lon), y = 2 tan(45 + lat/2) cos(lon) -- 0 deg longitude up, 90 E right.

Left: the whole south polar image (edge at 55 S on its axes), 16 px/deg LOLA, with the
latitude / longitude grid, the 60 S line poleward of which the mesh draws with the polar maps,
and the 16 tile seams of the cap. Right: within 2.5 deg of the pole, 64 px/deg LOLA, with a
fine grid, the 1x mesh's vertices and the Moon sites from data/fleet.json.
"""
import json
import math
import sys
from pathlib import Path

import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.colors import LinearSegmentedColormap

sys.path.insert(0, str(Path(__file__).resolve().parent))
from make_moon import CAP_DEG, LEVELS, POLAR_E, POLAR_EDGE_DEG, R_KM, SECTORS, bilinear, load_dem, polar_latlon, polar_xy  # noqa: E402
from charts import BASELINE, FONT, GRID, INK, INK_2, MUTED, SERIES, SURFACE  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "docs" / "images" / "south-pole-layout.png"
# sequential blue (dataviz reference ramp, steps 700 -> 100): low terrain dark, high light
ELEV = LinearSegmentedColormap.from_list("elev", ["#0d366b", "#184f95", "#256abf", "#3987e5", "#6da7ec",
                                                  "#9ec5f4", "#cde2fb"])
ACCENT = SERIES[1]          # the 60 S cap line
SITE = SERIES[3]            # Moon sites


def xy(lat, lon):
    return polar_xy(np.radians(lat), np.radians(lon), True)


def relief(dem, half, n):
    """Elevation (km) and a hillshade on an n x n stereographic grid of half-width `half`."""
    c = np.linspace(-half, half, n)
    X, Y = np.meshgrid(c, -c)                     # rows top-down: +y up
    lat, lon = polar_latlon(X, Y, True)
    h = bilinear(dem, np.degrees(lat), np.degrees(lon)).astype(np.float64)
    px_km = 2 * half / n * R_KM / 2 * 2          # stereographic scale ~1 near the pole (rho = 2 tan)
    gy, gx = np.gradient(h, px_km)
    az, alt = math.radians(315), math.radians(35)  # light from the upper left
    slope = np.arctan(np.hypot(gx, gy))
    aspect = np.arctan2(-gy, gx)
    shade = np.sin(alt) * np.cos(slope) + np.cos(alt) * np.sin(slope) * np.cos(az - aspect)
    return h, np.clip(shade, 0, 1)


def draw_relief(ax, h, shade, half, vmin, vmax):
    rgb = ELEV((h - vmin) / (vmax - vmin))[..., :3]
    rgb = rgb * (0.45 + 0.55 * shade[..., None])
    ax.imshow(rgb, extent=(-half, half, -half, half), origin="upper", interpolation="bilinear", zorder=0)


def circle(ax, lat, **kw):
    t = np.linspace(0, 2 * np.pi, 721)
    r = 2 * math.tan(math.radians(45 + lat / 2))
    ax.plot(r * np.sin(t), r * np.cos(t), **kw)


def meridian(ax, lon, lat0, lat1=-90.0, **kw):
    la = np.linspace(lat0, lat1, 50)
    x, y = xy(la, np.full_like(la, lon))
    ax.plot(x, y, **kw)


def style(ax, half, title):
    ax.set_xlim(-half, half)
    ax.set_ylim(-half, half)
    ax.set_aspect("equal")
    ax.set_xticks([])
    ax.set_yticks([])
    for s in ax.spines.values():
        s.set_color(BASELINE)
    ax.set_title(title, color=INK, loc="left", fontsize=9, pad=6)


def main():
    plt.rcParams.update(FONT)
    fleet = json.loads((ROOT / "data" / "fleet.json").read_text())
    sites = {}
    for kind, rows in (("place", fleet["places"]), ("station", fleet["ground_stations"])):
        for s in rows:
            if s.get("body") == "Moon":
                sites.setdefault(s["name"], {"lat": s["lat"], "lon": s["lon"], "kinds": []})["kinds"].append(kind)

    fig = plt.figure(figsize=(11.0, 6.5), facecolor=SURFACE)
    ax1 = fig.add_axes([0.03, 0.14, 0.45, 0.76], facecolor=SURFACE)
    ax2 = fig.add_axes([0.52, 0.14, 0.45, 0.76], facecolor=SURFACE)

    # ---- left: the whole polar image ----
    dem16 = load_dem(LEVELS[1]["dem"])
    half = POLAR_E                                  # the polar image spans +-E on both axes
    h, shade = relief(dem16, half, 900)
    draw_relief(ax1, h, shade, half, -6.0, 6.0)
    grid = dict(color=INK_2, lw=0.5, alpha=0.55, zorder=2)
    for lat in range(-85, -45, 5):
        if lat != -CAP_DEG:
            circle(ax1, lat, **grid)
    for lon in range(0, 360, 30):
        meridian(ax1, lon, -40, -90, **grid)
    for k in range(SECTORS):                        # mesh tile seams in the cap (22.5 deg)
        meridian(ax1, -180 + 360 * k / SECTORS, -CAP_DEG, -90, color=INK, lw=0.6, ls=(0, (3, 3)), alpha=0.8, zorder=3)
    circle(ax1, -CAP_DEG, color=ACCENT, lw=1.6, zorder=4)
    for lat in range(-85, -50, 5):                  # latitude labels down the 45 E meridian
        x, y = xy(lat, 45.0)
        ax1.text(x, y, f"{-lat}°S", color=INK, fontsize=7, ha="center", va="center", zorder=5,
                 bbox=dict(boxstyle="round,pad=0.15", fc=SURFACE, ec="none", alpha=0.75))
    for lon in range(0, 360, 30):                   # longitude labels just inside the image edge
        name = "0°" if lon == 0 else (f"{lon}°E" if lon < 180 else ("180°" if lon == 180 else f"{360 - lon}°W"))
        x, y = xy(-52.5, lon)
        if abs(x) < half * 0.97 and abs(y) < half * 0.97:
            ax1.text(x, y, name, color=INK, fontsize=7, ha="center", va="center", zorder=5,
                     bbox=dict(boxstyle="round,pad=0.15", fc=SURFACE, ec="none", alpha=0.75))
    zoom = 2 * math.tan(math.radians(45 + (-87.5) / 2))
    ax1.add_patch(plt.Rectangle((-zoom, -zoom), 2 * zoom, 2 * zoom, fill=False, ec=INK, lw=1.0, zorder=6))
    ax1.text(zoom, -zoom, " right panel", color=INK, fontsize=7, ha="left", va="top", zorder=6,
             bbox=dict(boxstyle="round,pad=0.15", fc=SURFACE, ec="none", alpha=0.75))
    style(ax1, half, f"South polar image (UV1): edge at {POLAR_EDGE_DEG:g}°S on its axes; LOLA 16 px/deg")

    # ---- right: within 2.5 deg of the pole ----
    dem64 = load_dem(LEVELS[4]["dem"])
    h2, shade2 = relief(dem64, zoom, 900)
    draw_relief(ax2, h2, shade2, zoom, -6.0, 6.0)
    for lat in np.arange(-87.5, -89.99, -0.5):
        circle(ax2, lat, **grid)
    for lon in range(0, 360, 15):
        meridian(ax2, lon, -87.0, -90, **grid)
    for lat in (-88.0, -89.0):
        x, y = xy(lat, 45.0)
        ax2.text(x, y, f"{-lat:g}°S", color=INK, fontsize=7, ha="center", va="center", zorder=5,
                 bbox=dict(boxstyle="round,pad=0.15", fc=SURFACE, ec="none", alpha=0.75))
    for lon in range(0, 360, 45):
        name = "0°" if lon == 0 else (f"{lon}°E" if lon < 180 else ("180°" if lon == 180 else f"{360 - lon}°W"))
        x, y = xy(-87.75, lon)
        ax2.text(x, y, name, color=INK, fontsize=7, ha="center", va="center", zorder=5,
                 bbox=dict(boxstyle="round,pad=0.15", fc=SURFACE, ec="none", alpha=0.75))
    # the 1x mesh's vertices here: rows every 180/256 deg, columns every 360/512 deg
    seg, ring = LEVELS[1]["seg"], LEVELS[1]["ring"]
    rows = 90 - 180 * np.arange(ring + 1) / ring
    rows = rows[rows <= -87.4]
    cols = -180 + 360 * np.arange(seg) / seg
    LO, LA = np.meshgrid(cols, rows)
    vx, vy = xy(LA, LO)
    ax2.scatter(vx, vy, s=1.2, color=INK, alpha=0.7, linewidths=0, zorder=4, label="1x mesh vertices")
    for name, s in sites.items():
        x, y = xy(s["lat"], s["lon"])
        ax2.scatter([x], [y], s=46, color=SITE, edgecolors=SURFACE, linewidths=1.5, zorder=7)
        kinds = " + ".join(sorted(set(s["kinds"])))
        r = math.hypot(x, y) or 1.0                 # label pushed away from the pole
        ux, uy = x / r, y / r
        ax2.annotate(f"{name} ({kinds})\n{s['lat']:.4f}°, {s['lon']:.4f}°", (x, y), xytext=(46 * ux, 30 * uy),
                     textcoords="offset points", color=INK, fontsize=7, zorder=8,
                     ha="left" if ux > 0.2 else ("right" if ux < -0.2 else "center"), va="center",
                     arrowprops=dict(arrowstyle="-", color=INK_2, lw=0.6),
                     bbox=dict(boxstyle="round,pad=0.25", fc=SURFACE, ec="none", alpha=0.8))
    style(ax2, zoom, "Within 2.5° of the pole: LOLA 64 px/deg, 1x mesh vertices, Moon sites")

    # legend + colour bar
    handles = [plt.Line2D([], [], color=ACCENT, lw=1.6, label=f"{CAP_DEG:g}°S: polar maps (UV1) poleward, equatorial (UV0) above"),
               plt.Line2D([], [], color=INK, lw=0.6, ls=(0, (3, 3)), label="mesh tile seams in the cap (every 22.5° of longitude)"),
               plt.Line2D([], [], color=INK_2, lw=0.5, label="grid: left 5° lat / 30° lon; right 0.5° lat / 15° lon"),
               plt.Line2D([], [], color=INK, marker="o", ls="", ms=2, label="1x mesh vertices (0.70° lat x 0.70° lon)"),
               plt.Line2D([], [], color=SITE, marker="o", ls="", ms=6, label="Moon sites (places.xlsx / groundstations.xlsx)")]
    leg = fig.legend(handles=handles, loc="lower left", bbox_to_anchor=(0.03, 0.01), ncol=3, frameon=False,
                     fontsize=7, labelcolor=INK_2)
    cax = fig.add_axes([0.80, 0.075, 0.17, 0.018])
    cb = fig.colorbar(plt.cm.ScalarMappable(norm=plt.Normalize(-6, 6), cmap=ELEV), cax=cax, orientation="horizontal")
    cb.set_label("LOLA height above 1737.4 km (km)", color=INK_2, fontsize=7)
    cb.ax.tick_params(colors=INK_2, labelsize=7)
    cb.outline.set_edgecolor(BASELINE)
    fig.text(0.03, 0.955, "Lunar south pole: how the LOLA elevation map is laid down (polar stereographic, PDS convention, Moon ME frame)",
             color=INK, fontsize=10, weight="bold")
    fig.text(0.97, 0.955, "BoneStar tools/south_pole_map.py", color=MUTED, fontsize=7, ha="right")
    OUT.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(OUT, dpi=200, facecolor=SURFACE)
    print(f"Wrote {OUT.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
