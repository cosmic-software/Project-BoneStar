# Project BoneStar

A pipeline for turning GMAT-propagated spacecraft orbits into a live, browser-viewable
scene. GMAT does the orbital mechanics; the browser side (WebVerse) only ever consumes
state and interpolates for playback -- it does no orbital computation itself.

The current test case tracks the **ISS** and the **Hubble Space Telescope** from their
public TLEs.

## Pipeline

```
tle/<catalog number>.tle         TLEs from any outside source (CelesTrak for the test case)
        |
tools/run_fleet.py               runs GMAT headless: SGP4 propagation of each TLE
        |
data/oem/<catalog number>.oem    one CCSDS OEM per spacecraft (EME2000, 60 s steps,
        |                        1 hour before the run to 12 hours after it)
        v
data/fleet.json                  all spacecraft merged for the viewer
        |
webverse/Scripts/orbit.js        fetches it over HTTP and flies each spacecraft in real time
```

## Layout

```
tle/      input TLEs, one file per spacecraft: name line + the two element lines
tools/    run_fleet.py (the fleet job), gmat_to_webverse.py (single-orbit demo converter)
gmat/     GMAT scenario scripts (the single-orbit demo)
data/     generated output: OEMs, JSON, the generated GMAT script (not committed)
models/   probe.glb (spacecraft), earth.glb (placeholder grid globe, unit radius)
webverse/ the WebVerse world: index.veml (scene) + Scripts/orbit.js (playback, camera)
```

## Running it

Run the fleet job (needs GMAT R2026a; set `GMAT_CONSOLE` if it isn't at
`F:\gmat-win-R2026a\bin\GmatConsole.exe`):

```
python tools\run_fleet.py
```

It checks each TLE (line length, checksum), writes `data/fleet_run.script`, runs
GmatConsole headless, and writes `data/oem/*.oem` and `data/fleet.json`. This is the step
to schedule, e.g. every 6 hours after refreshing the TLEs.

GMAT reads TLEs through its SGP4 propagator plugin (`SPICESGP4`) but does not write them;
it writes the OEM ephemeris files, which carry GMAT's own trajectory.

### Viewing it in WebVerse

Serve the project root, then open the world directly in the WebVerse address bar:

```
python -m http.server 8000
```
```
http://localhost:8000/webverse/index.veml
```

Don't go through a WorldHub `visitworld` link: it forces a WorldHub login first.

Playback starts at the moment the fleet job ran and runs in real time, so run the job just
before viewing for current positions. It holds the last position at the end of the
12-hour window. Scale is 1 unit = 100 km; the spacecraft model is hugely exaggerated so it
stays visible.

| Control | Action |
|---|---|
| Left-drag | orbit the camera around the current focus |
| W / S, = / - , right-drag | zoom |
| Left arrow | next spacecraft: the camera rides with it, 1 unit out |
| Right arrow, R | Earth view (starts 4 Earth radii out) |

### Adding a spacecraft

1. Put its TLE in `tle/<catalog number>.tle`.
2. Add a mesh entity to `webverse/index.veml` with `tag="<catalog number>"` (copy the ISS one
   and give it a new `id` UUID).
3. Rerun `python tools\run_fleet.py`.

## JSON shape

```json
{
  "epoch": "01 Oct 2026 07:36:00.000",
  "generated": "01 Oct 2026 08:36:00.000",
  "generated_t": 3600.0,
  "names": { "25544": "ISS (ZARYA)", "20580": "HST" },
  "objects": {
    "25544": [
      {"t": 0, "pos": [x, y, z], "vel": [vx, vy, vz]},
      ...
    ]
  }
}
```

`t` is seconds after `epoch` (UTC); positions are km and velocities km/s in EarthMJ2000Eq.
`generated_t` is where the run time falls in the window, and is where playback starts.

## Also included: single-orbit demo

`gmat/orbit_default.script` propagates one spacecraft from 17 Sep 2026 12:00 UTC in a
45 deg LEO test orbit (perigee altitude 350 km, eccentricity 0.02) for one full orbit:

```
"F:\gmat-win-R2026a\bin\GmatConsole.exe" --run gmat\orbit_default.script
python tools\gmat_to_webverse.py data\orbit_default.txt SC data\orbit_default.json
```

The viewer now reads `data/fleet.json`, not this demo's output.

## Status

Working locally for the ISS and Hubble. Not built yet: a real-time clock sync (the viewer
counts forward from the job's run time), scheduling the fleet job, and an Assets side panel
-- `webverse/panels/` is written but switched off, because in the current WebVerse runtime
its screen canvas reports a size of 0x0, so the panel gets no area.

## License

MIT, see [LICENSE](LICENSE).
