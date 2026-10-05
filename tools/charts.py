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

Drawn on the viewer's dark panel colour. Spacecraft colours follow a fixed categorical order
(the dataviz reference palette's dark steps), by the spacecraft's place in the fleet, so a
spacecraft keeps its colour on every station's chart.
"""
from datetime import timedelta

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


def colour(index):
    """Categorical colour by fixed fleet order; past eight, fold to muted ("other")."""
    return SERIES[index] if index < len(SERIES) else MUTED


def station_chart(path, station, passes, profiles, order, names, start, end):
    """Write the station's chart to path. passes: this station's passes; profiles[i]: the
    samples of passes[i] ([(t, elevation, range_km, margin_db or None)]); order: tag -> fleet
    index (colour); start / end: the window (UTC datetimes)."""
    plt.rcParams.update({"font.family": ["Segoe UI", "DejaVu Sans"], "font.size": 9,
                         "axes.edgecolor": BASELINE, "axes.labelcolor": INK_2, "xtick.color": MUTED,
                         "ytick.color": MUTED, "text.color": INK})
    fig, axes = plt.subplots(3, 1, figsize=(6.4, 7.8), dpi=100, facecolor=SURFACE,
                             gridspec_kw={"height_ratios": [1, 1.4, 1.4], "hspace": 0.6})
    fig.subplots_adjust(left=0.27, right=0.97, top=0.86, bottom=0.07)
    for ax in axes:
        ax.set_facecolor(SURFACE)
        ax.grid(True, color=GRID, linewidth=0.6)
        ax.set_axisbelow(True)
        for side in ("top", "right"):
            ax.spines[side].set_visible(False)
    fig.suptitle(f"{station['name']}  ({station['freq_mhz']:g} MHz, {station['link']}, beam {station['fov_deg']:g}°)",
                 color=INK, fontsize=11, x=0.03, ha="left", y=0.985)
    crafts = sorted({p["catalog"] for p in passes}, key=lambda t: order[t])

    # 1. contact timeline
    ax = axes[0]
    ax.set_title("Contacts over the run's window (UTC)", color=INK_2, fontsize=9, loc="left")
    for row, tag in enumerate(crafts):
        for p in passes:
            if p["catalog"] == tag:
                a = start + timedelta(seconds=p["aos"])
                ax.barh(row, (p["los"] - p["aos"]) / 86400, left=mdates.date2num(a), height=0.5,
                        color=colour(order[tag]), edgecolor=SURFACE, linewidth=1)
    ax.set_yticks(range(len(crafts)), [names[t] for t in crafts])
    ax.set_ylim(-0.6, max(len(crafts), 1) - 0.4)
    ax.set_xlim(mdates.date2num(start), mdates.date2num(end))
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M"))
    ax.tick_params(axis="y", colors=INK_2, length=0)
    if not crafts:
        ax.text(0.5, 0.5, "no passes in this window", transform=ax.transAxes, ha="center", va="center", color=MUTED)

    # 2. elevation through each pass
    ax = axes[1]
    ax.set_title("Elevation through each pass", color=INK_2, fontsize=9, loc="left")
    mask = 90 - station["fov_deg"] / 2
    ax.axhline(mask, color=MUTED, linewidth=1, linestyle=(0, (4, 3)))
    ax.text(0.995, mask + 1.5, f"beam edge {mask:g}°", transform=ax.get_yaxis_transform(), ha="right", va="bottom",
            color=MUTED, fontsize=8, bbox={"facecolor": SURFACE, "edgecolor": "none", "pad": 1.5}, zorder=5)
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
    ax.set_title("Downlink margin through each pass", color=INK_2, fontsize=9, loc="left")
    plotted = False
    for p, prof in zip(passes, profiles):
        pts = [(share(p, t), m) for t, _, _, m in prof if m is not None]
        if pts:
            ax.plot([x for x, _ in pts], [m for _, m in pts], color=colour(order[p["catalog"]]), linewidth=2)
            plotted = True
    ax.axhline(0, color=INK_2, linewidth=1)
    ax.text(0.995, 0, " 0 dB: closes above", transform=ax.get_yaxis_transform(), ha="right", va="bottom",
            color=INK_2, fontsize=8)
    ax.set_xlim(0, 100)
    ax.set_ylabel("margin (dB)")
    ax.set_xlabel("% of the pass (AOS to LOS)")
    if not plotted:
        ax.set_ylim(-10, 10)
        ax.text(0.5, 0.62, "no link budget: add the station's radio (groundstations, J-L)\n"
                "and the spacecraft's (spacecraft.xlsx, Payloads)", transform=ax.transAxes,
                ha="center", va="center", color=MUTED, fontsize=8)

    # legend (identity is never colour alone: names in the legend and on the timeline rows)
    if crafts:
        handles = [plt.Line2D([], [], color=colour(order[t]), linewidth=2) for t in crafts]
        fig.legend(handles, [names[t] for t in crafts], loc="upper left", bbox_to_anchor=(0.02, 0.955),
                   ncol=min(4, len(crafts)), frameon=False, labelcolor=INK_2, fontsize=8)
    fig.savefig(path, facecolor=SURFACE)
    plt.close(fig)
