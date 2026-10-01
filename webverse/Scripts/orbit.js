// Project BoneStar -- WebVerse viewer.
// Loads data/fleet.json (written by tools/run_fleet.py: TLEs -> GMAT headless -> CCSDS OEM)
// and flies one mesh per spacecraft, tagged with its catalog number in index.veml.
// Playback starts at the moment the fleet job ran and advances in real time; it holds the
// last position at the end of the window instead of looping. Positions are scaled from km
// to world units (1 unit = 100 km).

const EPHEMERIS_URL = "http://localhost:8000/data/fleet.json";
const KM_TO_WORLD_UNITS = 1 / 100;

var fleet = null;         // { catalog number: [ {t, pos, vel}, ... ] }
var fleetNames = {};      // { catalog number: name from the TLE }
var fleetTags = [];       // catalog numbers, in the order the left arrow cycles through them
var windowEnd = 0;
var windowEndReported = false;
// Jint (WebVerse's JS engine) has no Date.now(), so the clock counts UpdateOrbit ticks.
const ORBIT_TICK_SECONDS = 0.1;
var elapsedSeconds = 0;   // seconds after the fleet window's epoch

// ---- Orbit camera ----
// VEML has no camera element -- the runtime owns the camera and scripts place it.
// The camera orbits a focus object and always aims at its centre: Earth (the start, 4 radii
// out) or a spacecraft (the camera becomes a child of it, so it rides along, starting 1 unit
// away). Controls:
//   left-drag            orbit around the focus
//   W / S  or  = / -     zoom in / out      (no scroll wheel in the runtime's input API)
//   right-drag up/down   zoom in / out
//   Left arrow           next spacecraft view (camera rides with it, 1 unit out)
//   Right arrow          Earth view
//   R                    back to the starting view (Earth)
const EARTH_RADIUS = 6378.1363 * KM_TO_WORLD_UNITS;
const START_YAW = 0;
const START_PITCH = 15;
const ORBIT_DEG_PER_PIXEL = 1.0;  // measured: mouse deltas arrive as 3-7 px steps; 0.2 was barely visible
const KEY_ZOOM_PER_TICK = 1.01;   // per 0.01 s tick
const DRAG_ZOOM_PER_PIXEL = 0.01;

// Framing, in world units. Every spacecraft uses probe.glb at scale 0.05: its pivot is its
// centre and its bounding sphere has radius 0.865, so 1 unit is just outside it.
const EARTH_FRAMING = { start: 4 * EARTH_RADIUS, min: 1.05 * EARTH_RADIUS, max: 40 * EARTH_RADIUS };
const SPACECRAFT_FRAMING = { start: 1.0, min: 0.9, max: 100 };

function Framing(tag) {
    return tag === "Earth" ? EARTH_FRAMING : SPACECRAFT_FRAMING;
}

var focusTag = "Earth";
var focusEntity = null;   // null = world origin (Earth's centre); otherwise the camera's parent
var focusScale = 1;       // parent's scale: local offsets are in its scaled space
var camYaw = START_YAW;
var camPitch = START_PITCH;
var camDistance = EARTH_FRAMING.start;

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
    camDistance = Framing(tag).start;
    PlaceCamera();
    PanelCall("assets-panel", "setSelected('" + tag + "')");
    Report("focus " + tag + (fleetNames[tag] ? " (" + fleetNames[tag] + ")" : "")
        + " dist " + camDistance + " parent scale " + focusScale);
}

function Zoom(factor) {
    var f = Framing(focusTag);
    camDistance = Math.max(f.min, Math.min(f.max, camDistance * factor));
}

var leftWasDown = false;

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
    // Stand-in for the Assets panel until it renders: left arrow = next spacecraft,
    // right arrow = Earth view. (Key names are the runtime's: DesktopInput.cs.)
    var leftDown = Input.GetKeyValue("ArrowLeft");
    var leftPressed = leftDown && !leftWasDown;   // one switch per press, not per tick held
    leftWasDown = leftDown;
    if (leftPressed && fleetTags.length > 0) {
        var next = (fleetTags.indexOf(focusTag) + 1) % fleetTags.length;   // Earth (-1) -> first
        SetFocus(fleetTags[next]);
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
// Production WebVerse builds drop script Logging output, so events (focus changes, UI set-up,
// fleet loading) are sent to the local server as requests (GET /__diag?...) and read from
// its request log.
const DIAG_URL = "http://localhost:8000/__diag?";

function Report(msg) {
    HTTPNetworking.Fetch(DIAG_URL + encodeURIComponent(msg), "OnReportSent");
}

function OnReportSent(body) {
}

// ---- Fleet playback ----
// The runtime calls this with the response body as a string, not a response
// object (HTTPNetworking.cs) -- an empty string means the fetch failed.
function OnEphemerisLoaded(body) {
    if (!body) {
        Report("fleet: failed to load " + EPHEMERIS_URL);
        return;
    }
    var parsed = JSON.parse(body);
    fleet = parsed.objects;
    fleetNames = parsed.names;
    fleetTags = [];
    for (var tag in fleet) {
        fleetTags.push(tag);
        var track = fleet[tag];
        windowEnd = Math.max(windowEnd, track[track.length - 1].t);
        if (Entity.GetByTag(tag) === null) {
            Report("fleet: no entity tagged " + tag + " (" + fleetNames[tag] + ") in index.veml");
        }
    }
    elapsedSeconds = parsed.generated_t;   // start where "now" was when the fleet job ran
    Report("fleet: " + fleetTags.join(",") + " window from " + parsed.epoch + " UTC, starting at t "
        + elapsedSeconds + " s (" + parsed.generated + " UTC)");
    Time.SetInterval(`UpdateOrbit();`, ORBIT_TICK_SECONDS);
}

// Position (km) at time t on one track: cubic Hermite between the bracketing states, using
// their velocities, so it follows the orbit's curve instead of cutting the 60 s chords.
function PositionAt(track, t) {
    var i = 0;
    while (i < track.length - 2 && track[i + 1].t <= t) {
        i++;
    }
    var a = track[i];
    var b = track[i + 1];
    var h = b.t - a.t;
    var u = Math.max(0, Math.min(1, (t - a.t) / h));
    var h00 = 2 * u * u * u - 3 * u * u + 1;
    var h10 = u * u * u - 2 * u * u + u;
    var h01 = -2 * u * u * u + 3 * u * u;
    var h11 = u * u * u - u * u;
    var p = [];
    for (var k = 0; k < 3; k++) {
        p.push(h00 * a.pos[k] + h10 * h * a.vel[k] + h01 * b.pos[k] + h11 * h * b.vel[k]);
    }
    return p;
}

function UpdateOrbit() {
    if (fleet === null) {
        return;
    }
    elapsedSeconds += ORBIT_TICK_SECONDS;
    if (elapsedSeconds > windowEnd) {
        elapsedSeconds = windowEnd;   // hold the last position; rerun tools/run_fleet.py
        if (!windowEndReported) {
            Report("fleet: end of the ephemeris window reached");
            windowEndReported = true;
        }
    }
    for (var n = 0; n < fleetTags.length; n++) {
        var entity = Entity.GetByTag(fleetTags[n]);
        if (entity === null) {
            continue;
        }
        var p = PositionAt(fleet[fleetTags[n]], elapsedSeconds);
        // GMAT is right-handed Z-up, Unity left-handed Y-up: swapping Y and Z converts both.
        entity.SetPosition(new Vector3(p[0] * KM_TO_WORLD_UNITS, p[2] * KM_TO_WORLD_UNITS,
            p[1] * KM_TO_WORLD_UNITS), false);
    }
}

// ---- Start-up ----
PlaceCamera();
Time.SetInterval(`UpdateCamera();`, 0.01);
// CreateUI();  // off for now: the screen canvas reports size 0x0, so the panels get no area
HTTPNetworking.Fetch(EPHEMERIS_URL, "OnEphemerisLoaded");
