// Project BoneStar -- initial WebVerse test build.
// Loads the GMAT-generated ephemeris and drives the SC mesh entity's position from it,
// looping forever on the orbital period. Position values are scaled from km to world
// units (1 unit = 100 km) since raw km values are far too large for a Unity scene scale.

const EPHEMERIS_URL = "http://localhost:8000/data/orbit_default.json";
const KM_TO_WORLD_UNITS = 1 / 100;

var ephemeris = null;
var period = null;
// Jint (WebVerse's JS engine) has no Date.now(), so the clock counts UpdateOrbit ticks.
const ORBIT_TICK_SECONDS = 0.1;
var elapsedSeconds = 0;

// ---- Orbit camera ----
// VEML has no camera element -- the runtime owns the camera and scripts place it.
// The camera orbits a focus object and always aims at its centre. The focus is chosen in
// the Assets panel: Earth (the start: 4 radii out) or the probe (the camera becomes a child
// of the probe, so it rides along, starting 1 unit away). Controls:
//   left-drag            orbit around the focus
//   W / S  or  = / -     zoom in / out      (no scroll wheel in the runtime's input API)
//   right-drag up/down   zoom in / out
//   Left arrow           probe view (camera rides with the probe, 1 unit out)
//   Right arrow          Earth view
//   R                    back to the starting view (Earth)
const EARTH_RADIUS = 6378.1363 * KM_TO_WORLD_UNITS;
const START_YAW = 0;
const START_PITCH = 15;
const ORBIT_DEG_PER_PIXEL = 1.0;  // measured: mouse deltas arrive as 3-7 px steps; 0.2 was barely visible
const KEY_ZOOM_PER_TICK = 1.01;   // per 0.01 s tick
const DRAG_ZOOM_PER_PIXEL = 0.01;

// Per-focus framing, in world units. The probe model's pivot is its centre and its bounding
// sphere at scale 0.05 has radius 0.865, so 1 unit is just outside it.
const FOCI = {
    "Earth": { start: 4 * EARTH_RADIUS, min: 1.05 * EARTH_RADIUS, max: 40 * EARTH_RADIUS },
    "SC":    { start: 1.0,              min: 0.9,                 max: 100 }
};
var focusTag = "Earth";
var focusEntity = null;   // null = world origin (Earth's centre); otherwise the camera's parent
var focusScale = 1;       // parent's scale: local offsets are in its scaled space
var camYaw = START_YAW;
var camPitch = START_PITCH;
var camDistance = FOCI["Earth"].start;

function PlaceCamera() {
    var yaw = camYaw * Math.PI / 180;
    var pitch = camPitch * Math.PI / 180;
    // Camera looks along forward = (cos p sin y, -sin p, cos p cos y); sit opposite it.
    var ox = -camDistance * Math.cos(pitch) * Math.sin(yaw);
    var oy = camDistance * Math.sin(pitch);
    var oz = -camDistance * Math.cos(pitch) * Math.cos(yaw);
    if (focusEntity === null) {
        Camera.SetPosition(new Vector3(ox, oy, oz), false);
    } else {
        // Child of the focus (never rotated, only moved), so the world offset just needs
        // dividing by the parent's scale.
        Camera.SetPosition(new Vector3(ox / focusScale, oy / focusScale, oz / focusScale), true);
    }
    Camera.SetEulerRotation(new Vector3(camPitch, camYaw, 0), false);
}

function SetFocus(tag) {
    if (!FOCI[tag]) {
        return;
    }
    if (tag === "Earth") {
        Camera.AttachToEntity(null);
        focusEntity = null;
        focusScale = 1;
    } else {
        var entity = Entity.GetByTag(tag);
        if (entity === null) {
            Report("focus: no entity tagged " + tag);
            return;
        }
        Camera.AttachToEntity(entity);
        focusEntity = entity;
        focusScale = entity.GetScale().x;
    }
    focusTag = tag;
    camDistance = FOCI[tag].start;
    PlaceCamera();
    PanelCall("assets-panel", "setSelected('" + tag + "')");
    Report("focus " + tag + " dist " + camDistance + " parent scale " + focusScale);
}

function Zoom(factor) {
    var f = FOCI[focusTag];
    camDistance = Math.max(f.min, Math.min(f.max, camDistance * factor));
}

function UpdateCamera() {
    var changed = false;
    var look = Input.GetLookValue();
    var moved = look.x !== 0 || look.y !== 0;
    if (Input.GetLeft() && moved) {
        camYaw += look.x * ORBIT_DEG_PER_PIXEL;
        camPitch = Math.max(-89, Math.min(89, camPitch - look.y * ORBIT_DEG_PER_PIXEL));
        changed = true;
    } else if (Input.GetRight() && look.y !== 0) {
        Zoom(1 - look.y * DRAG_ZOOM_PER_PIXEL);   // drag up = in
        changed = true;
    }
    if (Input.GetKeyValue("w") || Input.GetKeyValue("=")) {
        Zoom(1 / KEY_ZOOM_PER_TICK);
        changed = true;
    }
    if (Input.GetKeyValue("s") || Input.GetKeyValue("-")) {
        Zoom(KEY_ZOOM_PER_TICK);
        changed = true;
    }
    // Stand-in for the Assets panel until it renders: left arrow = probe view,
    // right arrow = Earth view. (Key names are the runtime's: DesktopInput.cs.)
    if (Input.GetKeyValue("ArrowLeft") && focusTag !== "SC") {
        SetFocus("SC");
        return;
    }
    if (Input.GetKeyValue("ArrowRight") && focusTag !== "Earth") {
        SetFocus("Earth");
        return;
    }
    if (Input.GetKeyValue("r")) {
        camYaw = START_YAW;
        camPitch = START_PITCH;
        SetFocus("Earth");
        return;
    }
    if (changed) {
        PlaceCamera();
    }
}

// ---- Assets panel ----
// Two screen-space HTML panels docked on the right edge: a tab that is always shown, and
// the asset list it opens and closes. They are created here inside a screen canvas because
// html entities declared in VEML get no parent canvas and are never sized (see index.veml).
// Both post JSON strings to OnPanelMessage; the world answers by running JavaScript in the
// panel -- HTMLEntity has no SendMessage.
var assetsOpen = true;

// Called at the end of start-up: the engine fires these onLoaded callbacks synchronously
// inside Create(), so everything they use must already be defined.
function CreateUI() {
    CanvasEntity.Create(null, new Vector3(0, 0, 0), new Quaternion(0, 0, 0, 1), new Vector3(1, 1, 1),
        false, null, "ui-canvas", "OnCanvasLoaded");
}

var uiCanvas = null;

function OnCanvasLoaded(canvas) {
    // Script-created entities start hidden (the VEML loader shows its own); a hidden canvas
    // keeps its web views from ever initialising.
    canvas.SetVisibility(true);
    canvas.MakeScreenCanvas();
    uiCanvas = canvas;
    var size = canvas.GetSize();
    Report("ui: canvas created, screen canvas " + canvas.IsScreenCanvas() + ", size now " + size.x + "x" + size.y);
    // Panels are sized from the canvas's size at creation, and a new screen canvas only takes
    // the screen's size on Unity's next layout pass -- so create them a moment later.
    Time.SetTimeout(`CreatePanels();`, 500);   // milliseconds
}

function CreatePanels() {
    var size = uiCanvas.GetSize();
    Report("ui: creating panels, canvas size " + size.x + "x" + size.y);
    // Positions/sizes are fractions of the screen; position is the panel's centre,
    // measured from the top-left corner.
    HTMLEntity.Create(uiCanvas, new Vector2(0.98, 0.3), new Vector2(0.035, 0.16),
        null, "assets-tab", "OnPanelMessage", "OnTabLoaded");
    HTMLEntity.Create(uiCanvas, new Vector2(0.87, 0.3), new Vector2(0.17, 0.36),
        null, "assets-panel", "OnPanelMessage", "OnAssetsPanelLoaded");
}

function OnTabLoaded(panel) {
    panel.SetVisibility(true);
    panel.LoadFromURL("panels/assets-tab.html");
    Report("ui: assets-tab created");
}

function OnAssetsPanelLoaded(panel) {
    panel.SetVisibility(true);
    panel.LoadFromURL("panels/assets.html");
    Report("ui: assets-panel created");
}

function PanelCall(tag, js) {
    var panel = Entity.GetByTag(tag);
    if (panel !== null) {
        panel.ExecuteJavaScript(js, "");
    }
}

function OnPanelMessage(message) {
    var data;
    try {
        data = JSON.parse(message);
    } catch (e) {
        Report("panel: non-JSON message " + message);
        return;
    }
    if (data.type === "toggle-assets") {
        assetsOpen = !assetsOpen;
        var panel = Entity.GetByTag("assets-panel");
        if (panel !== null) {
            panel.SetVisibility(assetsOpen);
        }
        PanelCall("assets-tab", "setOpen(" + assetsOpen + ")");
    } else if (data.type === "select-asset") {
        SetFocus(data.tag);
    }
}

// ---- Diagnostics ----
// Production WebVerse builds drop script Logging output, so events (focus changes, UI set-up)
// are sent to the local server as requests (GET /__diag?...) and read from its request log.
const DIAG_URL = "http://localhost:8000/__diag?";

function Report(msg) {
    HTTPNetworking.Fetch(DIAG_URL + encodeURIComponent(msg), "OnReportSent");
}

function OnReportSent(body) {
}

PlaceCamera();
Time.SetInterval(`UpdateCamera();`, 0.01);
// CreateUI();  // off for now: the screen canvas reports size 0x0, so the panels get no area

HTTPNetworking.Fetch(EPHEMERIS_URL, "OnEphemerisLoaded");

// The runtime calls this with the response body as a string, not a response
// object (HTTPNetworking.cs) -- an empty string means the fetch failed.
function OnEphemerisLoaded(body) {
    if (!body) {
        Logging.LogError("Failed to load ephemeris from " + EPHEMERIS_URL + " (is the local server running?)");
        return;
    }

    var parsed = JSON.parse(body);
    ephemeris = parsed.objects.SC;
    period = ephemeris[ephemeris.length - 1].t;

    Logging.Log("Ephemeris loaded: " + ephemeris.length + " samples, period " + period + "s");

    Time.SetInterval(`UpdateOrbit();`, ORBIT_TICK_SECONDS);
}

function UpdateOrbit() {
    if (ephemeris === null) {
        return;
    }

    elapsedSeconds += ORBIT_TICK_SECONDS;
    var elapsed = elapsedSeconds % period;

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

    var sc = Entity.GetByTag("SC");
    if (sc === null) {
        Logging.LogError("UpdateOrbit: could not find entity tagged 'SC'.");
        return;
    }
    // GMAT is right-handed Z-up, Unity left-handed Y-up: swapping Y and Z converts both.
    sc.SetPosition(new Vector3(x, z, y), false);
}
