# Project BoneStar

A pipeline for turning a GMAT-propagated spacecraft orbit into a live, browser-viewable
scene. GMAT does the orbital mechanics; the browser side (WebVerse) only ever consumes
state and interpolates for playback -- it does no orbital computation itself.

## Pipeline

```
gmat/orbit_default.script  --(headless GMAT run)-->  data/orbit_default.txt
                                                            |
                                                   tools/gmat_to_webverse.py
                                                            |
                                                            v
                                                   data/orbit_default.json
                                                            |
                                                            |
                                          webverse/Scripts/orbit.js fetches it over
                                          HTTP and moves the probe along it
```

## Layout

```
gmat/     GMAT scenario script(s)
tools/    Python bridge scripts (GMAT report -> JSON)
data/     generated ephemeris/JSON output (not committed -- see .gitignore)
models/   probe.glb (spacecraft), earth.glb (placeholder grid globe, unit radius)
webverse/ the WebVerse world: index.veml (scene) + Scripts/orbit.js (playback, camera)
```

## Running it

```
"F:\gmat-win-R2026a\bin\GmatConsole.exe" --run gmat\orbit_default.script
python tools\gmat_to_webverse.py data\orbit_default.txt SC data\orbit_default.json
```

`orbit_default.script` propagates a single spacecraft (SC) from 17 Sep 2026 12:00 UTC in a
45 deg LEO test orbit -- perigee altitude 350 km, eccentricity 0.02 (SMA 6865.445 km, apogee
~625 km) -- for one full orbit (~94 minutes, perigee to perigee), reporting position/velocity
(EarthMJ2000Eq, km and km/s) every 60 seconds. No burns, no targeting -- this is the
minimal scenario the rest of the pipeline is built around.

### Viewing it in WebVerse

Serve the project root, then open the world directly in the WebVerse address bar:

```
python -m http.server 8000
```
```
http://localhost:8000/webverse/index.veml
```

Don't go through a WorldHub `visitworld` link: it forces a WorldHub login first.

Scale is 1 unit = 100 km; the probe model is hugely exaggerated so it stays visible.

| Control | Action |
|---|---|
| Left-drag | orbit the camera around the current focus |
| W / S, = / - , right-drag | zoom |
| Left arrow | probe view: the camera rides with the probe, 1 unit out |
| Right arrow, R | Earth view (starts 4 Earth radii out) |

`webverse/panels/` holds an Assets side panel that is written but switched off: in the
current WebVerse runtime its screen canvas reports a size of 0x0, so the panel gets no area.

## JSON shape

```json
{
  "epoch": "17 Sep 2026 11:59:22.966",
  "objects": {
    "SC": [
      {"t": 0, "pos": [x, y, z], "vel": [vx, vy, vz]},
      ...
    ]
  }
}
```

## Status

Working end-to-end for a single default orbit. Planned: a scheduled task re-running
GMAT every 6 hours to refresh the ephemeris, and a bridge into WebVerse (WorldOS /
MetaWorld) once the ingestion contract on that side is confirmed.

## License

MIT, see [LICENSE](LICENSE).
