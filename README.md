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
                                                  (fed to WebVerse -- ingestion
                                                   mechanism TBD)
```

## Layout

```
gmat/     GMAT scenario script(s)
tools/    Python bridge scripts (GMAT report -> JSON)
data/     generated ephemeris/JSON output (not committed -- see .gitignore)
```

## Running it

```
"G:\gmat-win-R2026a\bin\GmatConsole.exe" --run gmat\orbit_default.script
python tools\gmat_to_webverse.py data\orbit_default.txt SC data\orbit_default.json
```

`orbit_default.script` propagates a single spacecraft (SC) in a circular-ish 6778 km,
51.6 deg LEO orbit for one full period (~93 minutes), reporting position/velocity
(EarthMJ2000Eq, km and km/s) every 60 seconds. No burns, no targeting -- this is the
minimal scenario the rest of the pipeline is built around.

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
