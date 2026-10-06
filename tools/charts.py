#!/usr/bin/env python3
"""Ground-station charts (matplotlib), shown in the viewer's Show info for a selected station.

One PNG per station, three charts sharing nothing but the spacecraft colours:
  1. contact timeline (UTC): a row per spacecraft, a bar per pass;
  2. elevation through each pass, against the share of the pass from AOS to LOS (passes last
     from ~8 minutes in low Earth orbit to over an hour from the Moon; the timeline shows how
     long), with the beam's mask elevation;
  3. downlink margin through each pass, the same way (when the station and the spacecraft have
     radios), with the 0 dB line: above it the link closes.
Elevation and margin are separate charts on purpose (one measure per axis).

Each figure is drawn at its size on screen (its inches x 100 = view pixels at the viewer's
calibration, see orbit.js PX_UNITS) and saved at 200 dpi, twice that, so the viewer's
mipmapped texture stays sharp. Beside each PNG: an SVG (for the viewer's save button) and a
.glb, a flat quad carrying the PNG, which is what the viewer shows: WebVerse's own picture
loader (ImageEntity) fails after the first world is unloaded, while models still load.

Drawn on the viewer's dark panel colour. Spacecraft colours follow a fixed categorical order
(the dataviz reference palette's dark steps), by the spacecraft's place in the fleet, so a
spacecraft keeps its colour on every station's chart.
"""
import io
import json
import struct
from datetime import timedelta
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates

SURFACE = "#141721"          # the viewer's PANEL_COLOR (0.08, 0.09, 0.13)
INK = "#ffffff"
INK_2 = "#c3c2b7"
MUTED = "#898781"
GRID = "#2c2c2a"
BASELINE = "#383835"
SERIES = ["#3987e5", "#d95926", "#199e70", "#c98500", "#d55181", "#008300", "#9085e9", "#e66767"]


DPI = 400                    # the PNG (shown by the viewer, copied by its save button): 4 px per point
HOUR_LINE = "#5a5a55"        # the bold line on every hour of a time axis
FONT = {"font.family": ["Segoe UI", "DejaVu Sans"], "font.size": 8,
        "axes.edgecolor": BASELINE, "axes.labelcolor": INK_2, "xtick.color": MUTED,
        "ytick.color": MUTED, "text.color": INK}


def time_axis(ax, start, end, label=True, span="window"):
    """The standard UTC time axis for a span, small ticks and bold labelled lines:
        window (hours)  every quarter hour / every hour, "05" (at midnight also the date)
        week            every 6 hours / every day, "Tue 07"
        month           every day / every Monday, "07 Oct"
        6mo, year       every Monday / every month, "Oct" (in January also the year)
    and the dates in the axis title."""
    ax.set_xlim(mdates.date2num(start), mdates.date2num(end))
    if span == "window":
        major, minor = mdates.HourLocator(), mdates.MinuteLocator(byminute=[15, 30, 45])
        fmt = lambda t: t.strftime("%H\n%d %b") if t.hour == 0 else t.strftime("%H")
        what = "UTC hour"
    elif span == "week":
        major, minor = mdates.DayLocator(), mdates.HourLocator(byhour=[6, 12, 18])
        fmt = lambda t: t.strftime("%a %d")
        what = "UTC day"
    elif span == "month":
        major, minor = mdates.WeekdayLocator(byweekday=mdates.MO), mdates.DayLocator()
        fmt = lambda t: t.strftime("%d %b")
        what = "UTC, Mondays"
    else:
        major, minor = mdates.MonthLocator(), mdates.WeekdayLocator(byweekday=mdates.MO)
        fmt = lambda t: t.strftime("%b\n%Y") if t.month == 1 else t.strftime("%b")
        what = "UTC month"
    ax.xaxis.set_major_locator(major)
    ax.xaxis.set_minor_locator(minor)
    ax.xaxis.set_major_formatter(plt.FuncFormatter(lambda x, _pos: fmt(mdates.num2date(x))))
    ax.xaxis.grid(True, which="major", color=HOUR_LINE, linewidth=1.3)
    ax.xaxis.grid(False, which="minor")
    ax.tick_params(axis="x", which="major", length=5, width=1.3, colors=MUTED)
    ax.tick_params(axis="x", which="minor", length=3, width=0.8, colors=MUTED)
    if label:
        days = f"{start:%d %b %Y}" if start.date() == end.date() else f"{start:%d %b %Y} - {end:%d %b %Y}"
        ax.set_xlabel(f"{what}, {days}")


def footer(fig, when):
    """The timestamp along the bottom: which fleet run the chart comes from."""
    fig.text(0.99, 0.008, f"BoneStar - fleet run {when:%Y-%m-%d %H:%M} UTC", ha="right", va="bottom",
             color=MUTED, fontsize=6)


def save(fig, path):
    """The PNG (at DPI), an SVG beside it for the viewer's save button, and the quad .glb the
    viewer shows."""
    fig.savefig(path, facecolor=SURFACE, dpi=DPI)
    fig.savefig(path.with_suffix(".svg"), facecolor=SURFACE)
    picture_glb(path)


def picture_glb(png, out=None):
    """A .glb quad (unit square, centred, in glTF's XY plane) textured with the PNG, unlit,
    double-sided, blended (so it casts no shadow), with mipmaps. glTFast mirrors glTF X into
    Unity, so u = 0.5 - x: seen from the camera (the viewer turns the quad with it) the picture
    reads left to right; v = 0.5 - y puts its top row at the top."""
    png = Path(png)
    data = png.read_bytes()
    pos = [(-0.5, -0.5, 0.0), (0.5, -0.5, 0.0), (0.5, 0.5, 0.0), (-0.5, 0.5, 0.0)]
    uv = [(0.5 - x, 0.5 - y) for x, y, _ in pos]
    idx = [0, 2, 1, 0, 3, 2]
    vb = b"".join(struct.pack("<3f", *p) for p in pos)
    nb = b"".join(struct.pack("<3f", 0.0, 0.0, -1.0) for _ in pos)
    tb = b"".join(struct.pack("<2f", *t) for t in uv)
    ib = b"".join(struct.pack("<H", k) for k in idx) + b"\0\0"
    chunks = [vb, nb, tb, ib, data + b"\0" * (-len(data) % 4)]
    offs, o = [], 0
    for c in chunks:
        offs.append(o)
        o += len(c)
    binary = b"".join(chunks)
    gltf = {
        "asset": {"version": "2.0", "generator": "BoneStar tools/charts.py"},
        "extensionsUsed": ["KHR_materials_unlit"],
        "scene": 0, "scenes": [{"nodes": [0]}], "nodes": [{"mesh": 0, "name": png.stem}],
        "meshes": [{"primitives": [{"attributes": {"POSITION": 0, "NORMAL": 1, "TEXCOORD_0": 2},
                                    "indices": 3, "material": 0}]}],
        "materials": [{"name": "picture", "doubleSided": True, "alphaMode": "BLEND",
                       "pbrMetallicRoughness": {"baseColorTexture": {"index": 0}, "metallicFactor": 0,
                                                "roughnessFactor": 1},
                       "extensions": {"KHR_materials_unlit": {}}}],
        "textures": [{"source": 0, "sampler": 0}],
        "samplers": [{"magFilter": 9729, "minFilter": 9987, "wrapS": 33071, "wrapT": 33071}],
        "images": [{"bufferView": 4, "mimeType": "image/png"}],
        "buffers": [{"byteLength": len(binary)}],
        "bufferViews": [{"buffer": 0, "byteOffset": offs[0], "byteLength": len(vb), "target": 34962},
                        {"buffer": 0, "byteOffset": offs[1], "byteLength": len(nb), "target": 34962},
                        {"buffer": 0, "byteOffset": offs[2], "byteLength": len(tb), "target": 34962},
                        {"buffer": 0, "byteOffset": offs[3], "byteLength": 12, "target": 34963},
                        {"buffer": 0, "byteOffset": offs[4], "byteLength": len(data)}],
        "accessors": [{"bufferView": 0, "componentType": 5126, "count": 4, "type": "VEC3",
                       "min": [-0.5, -0.5, 0.0], "max": [0.5, 0.5, 0.0]},
                      {"bufferView": 1, "componentType": 5126, "count": 4, "type": "VEC3"},
                      {"bufferView": 2, "componentType": 5126, "count": 4, "type": "VEC2"},
                      {"bufferView": 3, "componentType": 5123, "count": 6, "type": "SCALAR"}],
    }
    js = json.dumps(gltf, separators=(",", ":")).encode()
    js += b" " * (-len(js) % 4)
    out = Path(out) if out else png.with_suffix(".glb")
    out.write_bytes(struct.pack("<III", 0x46546C67, 2, 12 + 8 + len(js) + 8 + len(binary))
                    + struct.pack("<II", len(js), 0x4E4F534A) + js
                    + struct.pack("<II", len(binary), 0x004E4942) + binary)
    return out


def short(name, n=16):
    """A name cut to n characters for tight labels (the full name is in the viewer)."""
    return name if len(name) <= n else name[:n - 1] + "…"


def colour(index):
    """Categorical colour by fixed fleet order; past eight, fold to muted ("other")."""
    return SERIES[index] if index < len(SERIES) else MUTED


def station_chart(path, station, passes, profiles, order, names, start, end, run=None):
    """Write the station's chart to path. passes: this station's passes; profiles[i]: the
    samples of passes[i] ([(t, elevation, range_km, margin_db or None)]); order: tag -> fleet
    index (colour); start / end: the window (UTC datetimes)."""
    plt.rcParams.update(FONT)
    fig, axes = plt.subplots(3, 1, figsize=(4.6, 5.6), dpi=100, facecolor=SURFACE,
                             gridspec_kw={"height_ratios": [1, 1.4, 1.4], "hspace": 0.95})
    fig.subplots_adjust(left=0.30, right=0.96, top=0.82, bottom=0.10)
    for ax in axes:
        ax.set_facecolor(SURFACE)
        ax.grid(True, color=GRID, linewidth=0.6)
        ax.set_axisbelow(True)
        for side in ("top", "right"):
            ax.spines[side].set_visible(False)
    bands = "+".join(dict.fromkeys(r["band"] for r in station["radios"]))
    fig.suptitle(f"{station['name']}  ({bands}-band, {station['link']}, beam {station['fov_deg']:g}°)",
                 color=INK, fontsize=9, x=0.03, ha="left", y=0.985)
    crafts = sorted({p["catalog"] for p in passes}, key=lambda t: order[t])

    # 1. contact timeline
    ax = axes[0]
    ax.set_title("Contacts over the run's window (UTC)", color=INK_2, fontsize=8, loc="left")
    for row, tag in enumerate(crafts):
        for p in passes:
            if p["catalog"] == tag:
                a = start + timedelta(seconds=p["aos"])
                ax.barh(row, (p["los"] - p["aos"]) / 86400, left=mdates.date2num(a), height=0.5,
                        color=colour(order[tag]), edgecolor=SURFACE, linewidth=1)
    ax.set_yticks(range(len(crafts)), [short(names[t]) for t in crafts])
    ax.set_ylim(-0.6, max(len(crafts), 1) - 0.4)
    time_axis(ax, start, end)
    ax.tick_params(axis="y", colors=INK_2, length=0)
    if not crafts:
        ax.text(0.5, 0.5, "no passes in this window", transform=ax.transAxes, ha="center", va="center", color=MUTED)

    # 2. elevation through each pass
    ax = axes[1]
    ax.set_title("Elevation through each pass", color=INK_2, fontsize=8, loc="left")
    mask = 90 - station["fov_deg"] / 2
    ax.axhline(mask, color=MUTED, linewidth=1, linestyle=(0, (4, 3)))
    ax.text(0.995, mask + 1.5, f"beam edge {mask:g}°", transform=ax.get_yaxis_transform(), ha="right", va="bottom",
            color=MUTED, fontsize=7, bbox={"facecolor": SURFACE, "edgecolor": "none", "pad": 1.5}, zorder=5)
    share = lambda p, t: 100 * (t - p["aos"]) / max(p["los"] - p["aos"], 1e-9)
    for p, prof in zip(passes, profiles):
        ax.plot([share(p, t) for t, _, _, _ in prof], [e for _, e, _, _ in prof], color=colour(order[p["catalog"]]),
                linewidth=2)
    ax.set_ylim(0, 90)
    ax.set_yticks([0, 30, 60, 90])
    ax.set_xlim(0, 100)
    ax.set_ylabel("elevation (°)")
    ax.set_xlabel("% of the pass (AOS to LOS)")

    # 3. downlink margin through each pass
    ax = axes[2]
    ax.set_title("Downlink margin through each pass, by radio", color=INK_2, fontsize=8, loc="left")
    # one line per spacecraft radio (colour: the spacecraft; line style: the radio, in a legend)
    keys = list(dict.fromkeys(k for prof in profiles for *_, m in prof for k in m))
    styles = {k: STYLES[i % len(STYLES)] for i, k in enumerate(keys)}
    plotted = False
    for p, prof in zip(passes, profiles):
        for key in keys:
            pts = [(share(p, t), m[key]) for t, _, _, m in prof if key in m]
            if pts:
                ax.plot([x for x, _ in pts], [y for _, y in pts], color=colour(order[p["catalog"]]), linewidth=2,
                        linestyle=styles[key])
                plotted = True
    if len(keys) > 1:
        ax.legend([plt.Line2D([], [], color=INK_2, linewidth=1.5, linestyle=styles[k]) for k in keys], keys,
                  loc="lower left", bbox_to_anchor=(0, 1.0), ncol=2, frameon=False, labelcolor=INK_2, fontsize=6,
                  borderaxespad=0.1, handlelength=2.5)
        ax.set_title(ax.get_title(loc="left"), color=INK_2, fontsize=8, loc="left", pad=4 + 10 * (-(-len(keys) // 2)))
    ax.axhline(0, color=INK_2, linewidth=1)
    ax.text(0.995, 0, " 0 dB: closes above", transform=ax.get_yaxis_transform(), ha="right", va="bottom",
            color=INK_2, fontsize=8)
    ax.set_xlim(0, 100)
    ax.set_ylabel("margin (dB)")
    ax.set_xlabel("% of the pass (AOS to LOS)")
    if not plotted:
        ax.set_ylim(-10, 10)
        ax.text(0.5, 0.25, "no link budget: the station and the spacecraft need\nradios in the same band "
                "(groundstations: Station radios;\nspacecraft.xlsx: Radios) with gain, noise temp, power, rate",
                transform=ax.transAxes,
                ha="center", va="center", color=MUTED, fontsize=7)

    # legend (identity is never colour alone: names in the legend and on the timeline rows)
    if crafts:
        handles = [plt.Line2D([], [], color=colour(order[t]), linewidth=2) for t in crafts]
        fig.legend(handles, [short(names[t]) for t in crafts], loc="upper left", bbox_to_anchor=(0.02, 0.955),
                   ncol=min(3, len(crafts)), frameon=False, labelcolor=INK_2, fontsize=7, columnspacing=1.0,
                   handlelength=1.5)
    footer(fig, run or start)
    save(fig, path)
    plt.close(fig)


SUN = SERIES[1]              # identity colours, fixed: the Sun orange, the Earth blue
EARTH = SERIES[0]
TERRAIN_FILL = "#2a2c33"
STYLES = ["-", (0, (5, 2)), (0, (1.5, 1.5)), (0, (6, 2, 1.5, 2))]   # radios on the margin chart


def _paths(az, el):
    """Azimuth / elevation lines broken where they wrap through north (no line across the plot)."""
    import numpy as np
    az, el = np.asarray(az, dtype=float).copy(), np.asarray(el, dtype=float).copy()
    jump = np.abs(np.diff(az)) > 180
    az[1:][jump] = np.nan
    return az, el


def _span(hours):
    return f"{hours / 24:.1f} d" if hours >= 48 else f"{hours:.1f} h"


def terrain_chart(path, site, res, long30, start):
    """A Moon site's terrain chart (tools/terrain.py results): its horizon with the Sun's and
    the Earth's paths over the next 30 days, the share of the Sun's disc in view, and when the
    Earth is in sight, with the 30 / 365-day figures."""
    import numpy as np
    plt.rcParams.update(FONT)
    fig, axes = plt.subplots(3, 1, figsize=(4.6, 5.6), dpi=100, facecolor=SURFACE,
                             gridspec_kw={"height_ratios": [1.7, 1.1, 0.45], "hspace": 0.75})
    fig.subplots_adjust(left=0.14, right=0.96, top=0.74, bottom=0.09)
    for ax in axes:
        ax.set_facecolor(SURFACE)
        ax.grid(True, color=GRID, linewidth=0.6)
        ax.set_axisbelow(True)
        for side in ("top", "right"):
            ax.spines[side].set_visible(False)
    kind = "ground station" if site.get("fov_deg") is not None else "place"
    fig.suptitle(f"{site['name']}  (lunar {kind}, {site['lat']:.4f}°, {site['lon']:.4f}°)",
                 color=INK, fontsize=9, x=0.03, ha="left", y=0.985)
    a, b = res["next_30d"], res["next_365d"]
    dem = res["dem"].split("_")[1]
    fig.text(0.03, 0.945, f"Next 30 d: Sun {a['sunlit_pct']:.1f}% of the time (whole disc {a['full_sun_pct']:.1f}%), "
             f"longest without {_span(a['longest_dark_h'])}\n"
             f"            Earth in sight {a['earth_pct']:.1f}%, longest out of sight {_span(a['longest_no_earth_h'])}\n"
             f"Next 365 d: Sun {b['sunlit_pct']:.1f}%, longest without {_span(b['longest_dark_h'])}; "
             f"Earth {b['earth_pct']:.1f}%\n"
             f"Terrain: LOLA {dem} px/deg; eye {res['eye_m']:.0f} m above the mean radius "
             f"({site['agl_m']:g} m above the ground)",
             color=INK_2, fontsize=7, va="top", linespacing=1.45)
    t, v = long30
    days = [start + timedelta(seconds=float(x)) for x in t]

    # 1. horizon with the Sun's and the Earth's paths
    ax = axes[0]
    ax.set_title("Horizon, with the Sun's and Earth's paths (next 30 d)", color=INK_2,
                 fontsize=8, loc="left")
    mask = np.asarray(res["mask"])
    az = np.arange(len(mask)) * res["az_step"]
    # centred on the Earth's mean direction (to the nearest compass point), so its libration
    # loop is never cut at the chart's edges
    ear = np.radians(v["earth_az"])
    centre = round(np.degrees(np.arctan2(np.sin(ear).mean(), np.cos(ear).mean())) / 45) * 45 % 360
    shift = lambda deg: (np.asarray(deg) - centre + 180) % 360 - 180
    order = np.argsort(shift(az))
    xs, ms = shift(az)[order], mask[order]
    xs, ms = np.concatenate([[-180], xs, [180]]), np.concatenate([[ms[-1]], ms, [ms[-1]]])
    lo = min(mask.min(), np.nanmin(v["earth_el"]), -2) - 1
    hi = max(mask.max(), np.nanmax(v["sun_el"]), np.nanmax(v["earth_el"])) + 3
    ax.fill_between(xs, lo, ms, color=TERRAIN_FILL, linewidth=0, zorder=1)
    ax.plot(xs, ms, color=MUTED, linewidth=1, zorder=2)
    sa, se = _paths(shift(v["sun_az"]), v["sun_el"])
    ea, ee = _paths(shift(v["earth_az"]), v["earth_el"])
    ax.plot(sa, se, color=SUN, linewidth=1.5, zorder=3)
    ax.plot(ea, ee, color=EARTH, linewidth=1.5, zorder=3)
    compass = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]
    ticks = np.arange(-180, 181, 45)
    ax.set_xlim(-180, 180)
    ax.set_xticks(ticks, [compass[int(((t + centre) % 360) // 45)] for t in ticks])
    ax.set_xlabel("azimuth", color=INK_2)
    ax.set_ylim(lo, hi)
    ax.set_ylabel("elevation (°)")
    handles = [plt.Line2D([], [], color=MUTED, linewidth=1), plt.Line2D([], [], color=SUN, linewidth=2),
               plt.Line2D([], [], color=EARTH, linewidth=2)]
    ax.legend(handles, ["terrain horizon", "Sun", "Earth"], loc="upper left", bbox_to_anchor=(0, 1.0), ncol=3,
              frameon=False, labelcolor=INK_2, fontsize=7, borderaxespad=0.2)

    # 2. share of the Sun's disc in view
    ax = axes[1]
    ax.set_title("Sun's disc in view (next 30 days)", color=INK_2, fontsize=8, loc="left")
    ax.plot(days, 100 * v["sun"], color=SUN, linewidth=1.5)
    ax.set_ylim(0, 105)
    ax.set_yticks([0, 50, 100])
    ax.set_ylabel("% of the disc")
    ax.set_xlim(days[0], days[-1])
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%d %b"))
    if not (np.asarray(v["sun"]) > 0).any():
        ax.text(0.5, 0.5, "the Sun never clears the terrain", transform=ax.transAxes, ha="center", va="center",
                color=MUTED)

    # 3. Earth in sight
    ax = axes[2]
    ax.set_title("Earth in sight (next 30 days)", color=INK_2, fontsize=8, loc="left")
    seen = np.asarray(v["earth_clear"]) > 0
    edges = np.flatnonzero(np.diff(np.concatenate([[0], seen.astype(int), [0]])))
    for k in range(0, len(edges), 2):
        a0, a1 = days[edges[k]], days[min(edges[k + 1], len(days) - 1)]
        ax.barh(0, mdates.date2num(a1) - mdates.date2num(a0), left=mdates.date2num(a0), height=0.6, color=EARTH)
    ax.set_yticks([])
    ax.set_ylim(-0.6, 0.6)
    ax.set_xlim(days[0], days[-1])
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%d %b"))
    if not seen.any():
        ax.text(0.5, 0, "never in sight", transform=ax.get_yaxis_transform(), ha="center", va="center", color=MUTED)
    footer(fig, start)
    save(fig, path)
    plt.close(fig)


def timeline_chart(path, title, times, rows, run=None, span="window"):
    """An XY plot over the run's window: one panel per row, sharing the UTC time axis.
    rows: [{title, ylabel, ylim, series: [(label, colour, times, values)], ref: (y, words) or None}]
    (NaN = not shown); times sets the x range. A row with one series needs no legend (its
    title names it); one with several gets a legend."""
    import numpy as np
    plt.rcParams.update(FONT)
    n = len(rows)
    fig, axes = plt.subplots(n, 1, figsize=(5.0, 4.0), dpi=100, facecolor=SURFACE, sharex=True, squeeze=False,
                             gridspec_kw={"hspace": 0.7})
    axes = axes[:, 0]
    fig.subplots_adjust(left=0.13, right=0.96, top=0.86 if n > 1 else 0.78, bottom=0.16)
    fig.suptitle(title, color=INK, fontsize=9, x=0.03, ha="left", y=0.975)
    x = mdates.date2num(times)
    for ax, row in zip(axes, rows):
        ax.set_facecolor(SURFACE)
        ax.grid(True, color=GRID, linewidth=0.6)
        ax.set_axisbelow(True)
        for side in ("top", "right"):
            ax.spines[side].set_visible(False)
        many = len(row["series"]) > 1 or (row.get("legend") and len(row["series"]) > 0)
        legend_rows = -(-len(row["series"]) // 4) if many else 0    # 4 entries a row
        ax.set_title(row["title"], color=INK_2, fontsize=8, loc="left", pad=4 + 10 * legend_rows)
        drawn = False
        for label, col, when, values in row["series"]:
            v = np.asarray(values, dtype=float)
            if np.isfinite(v).any():
                ax.plot(mdates.date2num(when), v, color=col, linewidth=1.8, label=label)
                drawn = True
        if row.get("ref") is not None:
            y, words = row["ref"]
            ax.axhline(y, color=MUTED, linewidth=1, linestyle=(0, (4, 3)))
            ax.text(0.995, y, f" {words}", transform=ax.get_yaxis_transform(), ha="right", va="bottom",
                    color=MUTED, fontsize=7, bbox={"facecolor": SURFACE, "edgecolor": "none", "pad": 1}, zorder=5)
        if row.get("ylim"):
            ax.set_ylim(*row["ylim"])
        ax.set_ylabel(row["ylabel"])
        if (len(row["series"]) > 1 or row.get("legend")) and drawn:
            # in a row of its own between the title and the plot, so it covers no data
            ax.legend(loc="lower left", bbox_to_anchor=(0, 1.0), ncol=min(4, len(row["series"])),
                      frameon=False, labelcolor=INK_2, fontsize=6, borderaxespad=0.1, columnspacing=1.0,
                      handlelength=1.5)
        if not drawn:
            ax.text(0.5, 0.5, row.get("empty", "none in this window"), transform=ax.transAxes, ha="center",
                    va="center", color=MUTED)
    for ax in axes:
        time_axis(ax, times[0], times[-1], label=ax is axes[-1], span=span)
    footer(fig, run or times[0])
    save(fig, path)
    plt.close(fig)
