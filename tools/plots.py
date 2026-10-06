#!/usr/bin/env python3
"""Plots over longer spans for the viewer's plot panel (13 h / Week / Month / 6 mo / Year),
drawn on demand by the local server (tools/serve.py, /api/plot) and cached for the fleet run.

The 13-hour plot of each site and spacecraft is drawn by the fleet job itself (run_fleet.py
timelines). For longer spans:
- a Moon site's Sun and Earth rows come from the terrain module's series, which the fleet job
  saves here (save_series): the next 30 days at 5 minutes (week, month) and the next 365 days at
  30 minutes (6 months, year), starting at the run;
- links and spacecraft sunlight come from a longer GMAT propagation (tools/longrun.py, run on
  the first request for a span and cached for the fleet run: about 25 s for a week / month,
  2.5 minutes for six months / a year). TLE spacecraft are propagated a week only.
A week shows each 3 hours' total, a month and longer each UTC day's: the share of the time in
sunlight or with the Earth in view, and the time in contact on each link (the 13 h view keeps the
orbit-by-orbit curves).
"""
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
SERIES_DIR = ROOT / "data" / "series"
CHART_DIR = ROOT / "data" / "charts"
FLEET = ROOT / "data" / "fleet.json"
SPANS = {"week": (7, 30, "next 7 days"), "month": (30, 30, "next 30 days"),
         "6mo": (182.5, 365, "next 6 months"), "year": (365, 365, "next 365 days")}
def partner_label(label, tag, L, span_s):
    """A link / spacecraft label, with why its line stops early (TLE week, impact)."""
    if tag in L["impacts"] and L["impacts"][tag] < span_s:
        return f"{label} (hits the Moon, day {L['impacts'][tag] / 86400:.0f})"
    if tag in L["limits"] and L["limits"][tag] < span_s:
        return f"{label} (TLE: {L['limits'][tag] / 86400:.0f} d)"
    return label


def slug(text):
    return "".join(ch if ch.isalnum() else "-" for ch in text.lower()).strip("-")


def save_series(key, series30, series365, run):
    """A Moon site's Sun and Earth series (terrain.visibility over the next 30 and 365 days)."""
    SERIES_DIR.mkdir(parents=True, exist_ok=True)
    arrays = {}
    for days, (t, v) in ((30, series30), (365, series365)):
        arrays[f"t{days}"] = np.asarray(t, dtype=np.float64)
        arrays[f"sun{days}"] = np.asarray(v["sun"], dtype=np.float32)
        arrays[f"earth{days}"] = np.asarray(v["earth_clear"], dtype=np.float32)
    np.savez_compressed(SERIES_DIR / f"{slug(key)}.npz", run=np.array(run.timestamp()), **arrays)


def cached(key, span):
    """The plot's path if it is already drawn for this fleet run, else None."""
    fleet = json.loads(FLEET.read_text())
    run = datetime.strptime(fleet["generated"], "%d %b %Y %H:%M:%S.%f").replace(tzinfo=timezone.utc)
    out = CHART_DIR / f"span-{slug(key)}-{span}_{run:%Y%m%d%H%M}.png"
    return f"data/charts/{out.name}" if out.exists() and out.with_suffix(".glb").exists() else None


def span_plot(key, span):
    """Draw (or reuse) the plot of key ("place:<name>", "station:<name>", "craft:<tag>") over
    span; returns its path under the project ("data/charts/span-....png", a .glb and an .svg
    beside it), or raises ValueError."""
    from charts import SERIES, timeline_chart
    if span not in SPANS:
        raise ValueError(f"unknown span {span!r}")
    fleet = json.loads(FLEET.read_text())
    run = datetime.strptime(fleet["generated"], "%d %b %Y %H:%M:%S.%f").replace(tzinfo=timezone.utc)
    name = f"span-{slug(key)}-{span}_{run:%Y%m%d%H%M}.png"
    out = CHART_DIR / name
    if out.exists() and out.with_suffix(".glb").exists():
        return f"data/charts/{name}"
    kind, _, ident = key.partition(":")
    if kind == "craft":
        label = fleet["names"].get(ident, ident)
        body = fleet.get("craft", {}).get(ident, {}).get("body", "Earth")
    elif kind in ("place", "station"):
        label, body = ident, None
        for site in fleet["places" if kind == "place" else "ground_stations"]:
            if site["name"] == ident:
                body = site.get("body", "Earth")
        if body is None:
            raise ValueError(f"no {kind} called {ident!r}")
    else:
        raise ValueError(f"bad key {key!r}")
    days, source, words = SPANS[span]
    end = run + timedelta(days=days)
    times = [run, end]
    rows = []
    series = SERIES_DIR / f"{slug(key)}.npz"
    if kind != "craft" and body == "Moon" and series.exists():
        d = np.load(series)
        t = d[f"t{source}"]
        keep = t <= days * 86400 + 1
        when = [run + timedelta(seconds=float(x)) for x in t[keep]]
        rows.append({"title": "Sun's disc in view (above the terrain)", "ylabel": "%", "ylim": (0, 105),
                     "series": [("Sun", SERIES[1], when, 100 * d[f"sun{source}"][keep])]})
        rows.append({"title": "Earth above the terrain (direct to Earth)", "ylabel": "°",
                     "series": [("Earth", SERIES[0], when, d[f"earth{source}"][keep])], "ref": (0, "in sight above")})
    if kind in ("craft", "station"):
        import longrun
        from charts import colour
        L = longrun.load(span)
        span_s = days * 86400
        week = span == "week"
        craft_order = list(fleet["craft"])
        stations = fleet["ground_stations"]

        bin_s = 3 * 3600 if week else 86400
        per = "each 3 hours" if week else "each day"
        unit = "% of time"

        def row_series(label, col, values, how):
            """Per 3 hours over a week, else per UTC day (mean share, or time in contact)."""
            when, v = longrun.binned(L, values, how, bin_s)
            return (label, col, when, v)

        if kind == "craft":
            tag = ident
            if tag not in L["pos"]:
                raise ValueError(f"no trajectory for {tag}")
            sun = longrun.sunlight(L, tag)
            rows.append({"title": f"Share of {per} in sunlight (Earth and Moon shadows)",
                         "ylabel": unit, "ylim": (0, 105),
                         "series": [row_series(partner_label("Sun", tag, L, span_s), SERIES[1], 100 * sun, "mean")]})
            if body == "Moon":
                clear = longrun.earth_clearance(L, tag)
                seen = np.where(np.isnan(clear), np.nan, 100.0 * (clear > 0))
                rows.append({"title": f"Share of {per} with the Earth in view (direct to Earth)",
                             "ylabel": unit, "ylim": (0, 105),
                             "series": [row_series("Earth", SERIES[0], seen, "mean")]})
            partners = [(st, st["name"], colour(k)) for k, st in enumerate(stations)]
            links = [(partners_st, tag, name, col) for partners_st, name, col in partners]
        else:
            st = next(x for x in stations if x["name"] == ident)
            links = [(st, tag, partner_label(fleet["names"].get(tag, tag), tag, L, span_s), colour(craft_order.index(tag)))
                     for tag in craft_order if tag in L["pos"]]
        series = []
        for st, tag, link_label, col in links:
            el, vis = longrun.station_view(L, st, tag)
            if not vis.any():
                continue
            valid = ~np.isnan(L["pos"][tag]).any(axis=1)
            series.append(row_series(link_label, col, np.where(valid, vis.astype(float), np.nan),
                                     "minutes" if week else "hours"))
        rows.append({"title": f"Links: time in contact {per}", "ylabel": "minutes" if week else "hours",
                     "ylim": (0, None), "series": series, "legend": True, "empty": "no contact over this span"})
        if kind == "craft":
            note = partner_label("", ident, L, span_s).strip()
            if note:
                label = f"{label} {note}"
    if not rows:
        rows.append({"title": "Nothing to plot", "ylabel": "", "series": [], "empty": "an Earth place has no Sun, "
                     "direct-to-Earth or link rows"})
    timeline_chart(out, f"{label}: Sun, direct-to-Earth and link times (UTC), {words}", times, rows, run, span)
    return f"data/charts/{name}"


if __name__ == "__main__":
    # python tools/plots.py "station:LID 1" week
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    print(span_plot(sys.argv[1], sys.argv[2]))
