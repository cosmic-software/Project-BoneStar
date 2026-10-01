// Project BoneStar -- initial WebVerse test build.
// Loads the GMAT-generated ephemeris and drives the SC mesh entity's position from it,
// looping forever on the orbital period. Position values are scaled from km to world
// units (1 unit = 100 km) since raw km values are far too large for a Unity scene scale.

const EPHEMERIS_URL = "http://localhost:8000/data/orbit_default.json";
const KM_TO_WORLD_UNITS = 1 / 100;

var ephemeris = null;
var period = null;
var startTimeMs = null;

HTTPNetworking.Fetch(EPHEMERIS_URL, "OnEphemerisLoaded");

function OnEphemerisLoaded(response) {
    if (response.status !== 200) {
        Logging.LogError("Failed to load ephemeris: " + response.status + " " + response.statusText);
        return;
    }

    var parsed = JSON.parse(response.data);
    ephemeris = parsed.objects.SC;
    period = ephemeris[ephemeris.length - 1].t;
    startTimeMs = Date.now();

    Logging.Log("Ephemeris loaded: " + ephemeris.length + " samples, period " + period + "s");

    var sc = MeshEntity.Get("SC");
    if (sc !== null) {
        Camera.AttachToEntity(sc);
    } else {
        Logging.LogError("OnEphemerisLoaded: could not find entity tagged 'SC' to attach camera to.");
    }

    Time.SetInterval(`UpdateOrbit();`, 0.1);
}

function UpdateOrbit() {
    if (ephemeris === null) {
        return;
    }

    var elapsed = ((Date.now() - startTimeMs) / 1000.0) % period;

    // Find the bracketing samples and linearly interpolate position between them.
    var i = 0;
    while (i < ephemeris.length - 1 && ephemeris[i + 1].t <= elapsed) {
        i++;
    }
    var a = ephemeris[i];
    var b = ephemeris[Math.min(i + 1, ephemeris.length - 1)];
    var span = b.t - a.t;
    var frac = span > 0 ? (elapsed - a.t) / span : 0;

    var x = (a.pos[0] + (b.pos[0] - a.pos[0]) * frac) * KM_TO_WORLD_UNITS;
    var y = (a.pos[1] + (b.pos[1] - a.pos[1]) * frac) * KM_TO_WORLD_UNITS;
    var z = (a.pos[2] + (b.pos[2] - a.pos[2]) * frac) * KM_TO_WORLD_UNITS;

    var sc = MeshEntity.Get("SC");
    if (sc === null) {
        Logging.LogError("UpdateOrbit: could not find entity tagged 'SC'.");
        return;
    }
    sc.SetPosition(new Vector3(x, y, z), false);
}
