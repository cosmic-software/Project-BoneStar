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
var earthRotation0 = 0;   // Greenwich meridian's angle from +X at the epoch (deg, from GMAT)
var earthRate = 0;        // deg/s

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
    HudSelect(tag);
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
    UpdateLabels();
    UpdateHud();
}

// ---- Assets panel ----
// A heads-up panel built from the runtime's own text and buttons (the approach in Dylan Baker's
// WebVerse samples) on a WORLD-space canvas kept just in front of the camera. Screen-space
// canvases never showed up in this runtime (the HTML panels loaded but stayed invisible),
// while world-space canvases do -- the spacecraft labels use one.
// Each row is a text with a translucent button laid over it: the runtime's button is an image
// with no text, and a text on top would take the click, so the text goes underneath.
const HUD_DISTANCE = 0.6;     // in front of the camera: past its 0.3 near plane, nearer than any
                              // spacecraft (closest zoom is 0.9 units)
// Placement in view units at HUD_DISTANCE. Measured from a 1903x1025 screenshot: ~1459 px per
// unit (so the vertical FOV is ~59 deg) and the view centre at ~(953, 496) px. The top edge
// sits below WebVerse's address bar (~150 px from the top of the window), the right edge ~210
// px in from the right. It scales with the window, since it is fixed in angle, not pixels.
const HUD_RIGHT_EDGE = 0.505;
const HUD_TOP_EDGE = 0.237;
const HUD_WORLD_WIDTH = 0.165;
// Layout in canvas units: header, then one row per asset; the height fits the rows.
const HUD_W = 500;
const HUD_PAD = 12;
const HUD_ROW = 80;
const HUD_GAP = 12;
const HUD_FONT = 44;
// A button's image is white until coloured with SetBaseColor; SetColors only sets the hover /
// press TINTS (multiplied onto the image, and only applied on a state change), so tints are
// kept near white and the real colours go on the image.
const ROW_COLOR = new Color(1, 1, 1, 0.06);
const ROW_SELECTED = new Color(0.5, 0.7, 1, 0.35);
const PANEL_COLOR = new Color(0.08, 0.09, 0.13, 0.85);
const TINT_NORMAL = new Color(1, 1, 1, 1);
const TINT_HOVER = new Color(1.6, 1.6, 1.6, 1);
const TINT_PRESS = new Color(0.8, 0.8, 0.8, 1);
var hud = null;               // { canvas, background, header, rows: [ { tag, text, button } ] }
var hudOpen = true;

function MakeRowButton(canvas, onClick, x, y, w, h, color) {
    var button = ButtonEntity.Create(canvas, onClick, new Vector2(x, y), new Vector2(w, h));
    button.SetVisibility(true);
    button.SetBaseColor(color);
    button.SetColors(TINT_NORMAL, TINT_HOVER, TINT_PRESS, TINT_NORMAL);
    return button;
}

function MakeText(canvas, words, x, y, w, h, color) {
    var text = TextEntity.Create(canvas, words, HUD_FONT, new Vector2(x, y), new Vector2(w, h));
    text.SetVisibility(true);
    text.SetColor(color);
    text.SetTextAlignment(TextAlignment.Center);
    return text;
}

// Called once the fleet is loaded, so the rows can use the spacecraft names.
function CreateHud() {
    var canvas = CanvasEntity.Create(null, new Vector3(0, 0, 0), new Quaternion(0, 0, 0, 1),
        new Vector3(1, 1, 1), false, null, "assets-hud");
    canvas.SetVisibility(true);
    canvas.MakeWorldCanvas();
    var entries = [{ tag: "Earth", name: "Earth" }];
    for (var n = 0; n < fleetTags.length; n++) {
        entries.push({ tag: fleetTags[n], name: fleetNames[fleetTags[n]] || fleetTags[n] });
    }
    var height = HUD_PAD + (entries.length + 1) * (HUD_ROW + HUD_GAP) + HUD_PAD;
    canvas.SetSize(new Vector2(HUD_W, height));
    hud = { canvas: canvas, rows: [], height: height };
    var fx = HUD_PAD / HUD_W, fw = 1 - 2 * fx, fh = HUD_ROW / height;
    // Children are attached keeping their world size, so the canvas must still be at scale 1
    // while they are created; it is shrunk (PlaceHud) only afterwards. Shrinking it first gave
    // every child a ~2270x local scale and the panel filled the whole view.
    // Created first, so it is drawn underneath everything else (and its clicks do nothing).
    hud.background = MakeRowButton(canvas, "", 0, 0, 1, 1, PANEL_COLOR);
    var fy = HUD_PAD / height;
    hud.header = MakeText(canvas, "ASSETS  -", fx, fy, fw, fh, new Color(0.5, 0.7, 1, 1));
    hud.headerButton = MakeRowButton(canvas, "ToggleAssets();", fx, fy, fw, fh, ROW_COLOR);
    for (var i = 0; i < entries.length; i++) {
        var y = (HUD_PAD + (i + 1) * (HUD_ROW + HUD_GAP)) / height;
        var text = MakeText(canvas, entries[i].name, fx, y, fw, fh, new Color(1, 1, 1, 1));
        var button = MakeRowButton(canvas, "SelectAsset('" + entries[i].tag + "');",
            fx, y, fw, fh, ROW_COLOR);
        hud.rows.push({ tag: entries[i].tag, text: text, button: button });
    }
    HudSelect(focusTag);
    PlaceHud();
    Report("hud: created with " + entries.length + " rows");
    Time.SetTimeout(`ReportHud();`, 2000);   // milliseconds
}

function ReportHud() {
    var sc = hud.canvas.GetScale();
    var p = hud.canvas.GetPosition(false);
    var c = Camera.GetPosition(false);
    var dx = p.x - c.x, dy = p.y - c.y, dz = p.z - c.z;
    var child = hud.rows[0].button.GetScale();
    Report("hud: scale " + sc.x.toFixed(6) + " (want " + (HUD_WORLD_WIDTH / HUD_W).toFixed(6)
        + "), row button local scale " + child.x.toFixed(3) + " (want 1), distance from camera "
        + Math.sqrt(dx * dx + dy * dy + dz * dz).toFixed(3));
}

function SelectAsset(tag) {
    Report("hud: clicked " + tag);
    SetFocus(tag);
}

function ToggleAssets() {
    hudOpen = !hudOpen;
    hud.background.SetVisibility(hudOpen);
    for (var i = 0; i < hud.rows.length; i++) {
        hud.rows[i].text.SetVisibility(hudOpen);
        hud.rows[i].button.SetVisibility(hudOpen);
    }
    hud.header.SetText(hudOpen ? "ASSETS  -" : "ASSETS  +");
    Report("hud: " + (hudOpen ? "opened" : "closed"));
}

// Highlight the row the camera is centred on.
function HudSelect(tag) {
    if (hud === null) {
        return;
    }
    for (var i = 0; i < hud.rows.length; i++) {
        hud.rows[i].button.SetBaseColor(hud.rows[i].tag === tag ? ROW_SELECTED : ROW_COLOR);
    }
}

// v rotated by quaternion q
function Rotate(q, x, y, z) {
    var tx = 2 * (q.y * z - q.z * y), ty = 2 * (q.z * x - q.x * z), tz = 2 * (q.x * y - q.y * x);
    return [x + q.w * tx + (q.y * tz - q.z * ty),
            y + q.w * ty + (q.z * tx - q.x * tz),
            z + q.w * tz + (q.x * ty - q.y * tx)];
}

// Keep the panel fixed in the top-right of the view, facing the camera.
function UpdateHud() {
    if (hud === null) {
        return;
    }
    PlaceHud();
}

function PlaceHud() {
    var s = HUD_WORLD_WIDTH / HUD_W;
    hud.canvas.SetScale(new Vector3(s, s, s), false);
    var q = Camera.GetRotation(false);
    var p = Camera.GetPosition(false);
    var f = Rotate(q, 0, 0, 1), r = Rotate(q, 1, 0, 0), u = Rotate(q, 0, 1, 0);
    // centre of the panel, from its fixed top-right corner
    var right = HUD_RIGHT_EDGE - HUD_WORLD_WIDTH / 2;
    var up = HUD_TOP_EDGE - HUD_WORLD_WIDTH * hud.height / HUD_W / 2;
    hud.canvas.SetPosition(new Vector3(
        p.x + f[0] * HUD_DISTANCE + r[0] * right + u[0] * up,
        p.y + f[1] * HUD_DISTANCE + r[1] * right + u[1] * up,
        p.z + f[2] * HUD_DISTANCE + r[2] * right + u[2] * up), false);
    hud.canvas.SetRotation(q, false);
}

// ---- Spacecraft labels ----
// One small world-space canvas per spacecraft with its name. Each tick it is placed just
// above its spacecraft, turned to face the camera, and scaled with the camera distance so
// it reads the same size from the Earth view and from up close.
// Runtime details (StraightFour source): script-made canvases and text start hidden, text
// starts black, a world canvas needs an explicit size (text is sized as a fraction of it),
// and text positions are measured from the canvas's top-left corner.
const LABEL_W = 400;            // canvas size, in canvas units
const LABEL_H = 80;
const LABEL_FONT = 40;
const LABEL_WIDTH_PER_DISTANCE = 0.12;   // label width as a fraction of camera distance
const LABEL_RAISE_PER_DISTANCE = 0.03;   // gap above the spacecraft, same units
var labels = {};                // tag -> { canvas, text }
var pendingLabelTag = null;     // onLoaded callbacks fire synchronously inside Create()

function CreateLabels() {
    for (var n = 0; n < fleetTags.length; n++) {
        pendingLabelTag = fleetTags[n];
        CanvasEntity.Create(null, new Vector3(0, 0, 0), new Quaternion(0, 0, 0, 1), new Vector3(1, 1, 1),
            false, null, "label-" + fleetTags[n], "OnLabelCanvasLoaded");
    }
}

function OnLabelCanvasLoaded(canvas) {
    var tag = pendingLabelTag;
    canvas.SetVisibility(true);
    canvas.MakeWorldCanvas();
    canvas.SetSize(new Vector2(LABEL_W, LABEL_H));
    labels[tag] = { canvas: canvas, text: null };
    TextEntity.Create(canvas, fleetNames[tag] || tag, LABEL_FONT, new Vector2(0, 0), new Vector2(1, 1),
        null, "label-text-" + tag, "OnLabelTextLoaded");
}

function OnLabelTextLoaded(text) {
    var tag = pendingLabelTag;
    text.SetVisibility(true);
    text.SetColor(new Color(1, 1, 1, 1));
    text.SetTextAlignment(TextAlignment.Center);
    labels[tag].text = text;
    var size = labels[tag].canvas.GetSize();
    Report("label: " + tag + " (" + fleetNames[tag] + ") canvas " + size.x + "x" + size.y);
}

function UpdateLabels() {
    var cam = Camera.GetPosition(false);
    var camRot = Camera.GetRotation(false);
    for (var tag in labels) {
        var entity = Entity.GetByTag(tag);
        if (entity === null) {
            continue;
        }
        var p = entity.GetPosition(false);
        var dx = p.x - cam.x, dy = p.y - cam.y, dz = p.z - cam.z;
        var dist = Math.sqrt(dx * dx + dy * dy + dz * dz);
        var scale = LABEL_WIDTH_PER_DISTANCE * dist / LABEL_W;
        var canvas = labels[tag].canvas;
        canvas.SetPosition(new Vector3(p.x, p.y + LABEL_RAISE_PER_DISTANCE * dist, p.z), false);
        canvas.SetRotation(camRot, false);
        canvas.SetScale(new Vector3(scale, scale, scale), false);
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
    earthRotation0 = parsed.earth.rotation_deg;
    earthRate = parsed.earth.rate_deg_per_s;
    PointSun(parsed.sun_dir);
    Report("fleet: " + fleetTags.join(",") + " window from " + parsed.epoch + " UTC, starting at t "
        + elapsedSeconds + " s (" + parsed.generated + " UTC)");
    CreateLabels();
    CreateHud();
    Time.SetInterval(`UpdateOrbit();`, ORBIT_TICK_SECONDS);
}

// The VEML light is created as a Unity point light (the runtime's default), which doesn't
// reach the Earth at this scale. Make it a directional sun shining from GMAT's Sun direction.
// WebVerse gives VEML worlds no ambient control (ambient comes from its default sky and is
// never recomputed), so an ambient of 0.2 is emulated: earth.glb and probe.glb have their
// base colours scaled by 0.2 and the sun is raised by 1/0.2. Day side: as bright as with a
// 2.5 sun; night side (ambient only): 20% of what it was.
const AMBIENT_EQUIVALENT = 0.2;
const SUN_INTENSITY = 2.5 / AMBIENT_EQUIVALENT;

function PointSun(sunDir) {
    var sun = Entity.GetByTag("sun");
    if (sun === null) {
        Report("sun: no entity tagged sun");
        return;
    }
    sun.SetLightType(LightType.Directional);
    // Scripts can't lower the scene's ambient light, so the night side stays fairly lit; a
    // bright sun is what makes the day side and the terminator stand out.
    var propsSet = sun.SetLightProperties(new Color(1, 1, 1, 1), 6500, SUN_INTENSITY);
    // GMAT -> Unity axes (swap Y and Z); the light shines along its forward axis, away from the Sun.
    var fx = -sunDir[0], fy = -sunDir[2], fz = -sunDir[1];
    var pitch = Math.asin(-fy) * 180 / Math.PI;
    var yaw = Math.atan2(fx, fz) * 180 / Math.PI;
    sun.SetEulerRotation(new Vector3(pitch, yaw, 0), false);
    Report("sun: directional, pitch " + pitch.toFixed(2) + " yaw " + yaw.toFixed(2)
        + ", intensity " + SUN_INTENSITY + " set " + propsSet);
}

// Turn the Earth so its texture's Greenwich meridian points where GMAT says it does.
// A GMAT rotation of +angle about Z is a Unity rotation of -angle about Y (axes swapped).
function RotateEarth(t) {
    var earth = Entity.GetByTag("Earth");
    if (earth === null) {
        return;
    }
    var half = -(earthRotation0 + earthRate * t) * Math.PI / 360;
    earth.SetRotation(new Quaternion(0, Math.sin(half), 0, Math.cos(half)), false);
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
    RotateEarth(elapsedSeconds);
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
HTTPNetworking.Fetch(EPHEMERIS_URL, "OnEphemerisLoaded");
