// Project BoneStar -- WebVerse viewer.
// Loads data/fleet.json (written by tools/run_fleet.py: TLEs -> GMAT headless -> CCSDS OEM)
// and flies one mesh per spacecraft, tagged with its catalog number in index.veml.
// Playback starts at the moment the fleet job ran and advances in real time; it holds the
// last position at the end of the window instead of looping. Positions are scaled from km
// to world units (1 unit = 100 km).

const EPHEMERIS_URL = "http://localhost:8000/data/fleet.json";
const KM_TO_WORLD_UNITS = 1 / 100;

// ---- Floating origin ----
// Unity keeps positions as 32-bit floats: ~3,840 units out (the Moon) they step by ~0.00024
// units (24 m), enough to make the panels shake by a pixel or two and close terrain shimmer
// (measured in a recording: panels moved 0.08 px a frame near the Earth, 0.36-0.56 px at the
// Moon). So the script keeps every position in its own (64-bit) world -- the Earth's centre at
// 0, 1 unit = 100 km, as before -- and Unity's origin follows the camera: whatever goes to Unity
// has origin subtracted (ToUnity), whatever comes back has it added (FromUnity). When the camera
// is REBASE_DISTANCE from the origin, the origin moves to it (Rebase); things placed every frame
// follow by themselves, the few that never move (the Earth, its atmosphere and grid, Earth orbit
// lines) are put back by PlaceStatics.
const REBASE_DISTANCE = 2;       // units (200 km)
var origin = { x: 0, y: 0, z: 0 };
var rebases = 0;                 // origin moves since the last report (orbiting a body from afar
var rebaseReportFrame = -1e9;    // moves it every frame: reported at most every REPORT_EVERY frames)
const REBASE_REPORT_EVERY = 600;

function ToUnity(v) {
    return new Vector3(v.x - origin.x, v.y - origin.y, v.z - origin.z);
}

function FromUnity(v) {
    return new Vector3(v.x + origin.x, v.y + origin.y, v.z + origin.z);
}

function SetWorld(entity, v) {
    entity.SetPosition(ToUnity(v), false);
}

function GetWorld(entity) {
    return FromUnity(entity.GetPosition(false));
}

function CameraWorld() {
    return FromUnity(Camera.GetPosition(false));
}

function SetCameraWorld(v) {
    Camera.SetPosition(ToUnity(v), false);
}

function Rebase() {
    var c = CameraWorld();
    var dx = c.x - origin.x, dy = c.y - origin.y, dz = c.z - origin.z;
    if (dx * dx + dy * dy + dz * dz < REBASE_DISTANCE * REBASE_DISTANCE) {
        return;
    }
    origin = { x: c.x, y: c.y, z: c.z };
    SetCameraWorld(c);
    PlaceStatics();
    rebases++;
    if (infoFrame - rebaseReportFrame >= REBASE_REPORT_EVERY) {
        Report("origin moved to " + c.x.toFixed(2) + ", " + c.y.toFixed(2) + ", " + c.z.toFixed(2)
            + " (" + Math.sqrt(c.x * c.x + c.y * c.y + c.z * c.z).toFixed(1) + " units from the Earth; "
            + rebases + " moves since the last report)");
        rebases = 0;
        rebaseReportFrame = infoFrame;
    }
}

// Entities that stay put in the world: back to their places after the origin moved.
function PlaceStatics() {
    var zero = new Vector3(0, 0, 0);
    var fixed = [Entity.GetByTag("Earth"), Entity.GetByTag("Atmosphere"), Entity.Get(GRID_ID)];
    for (var i = 0; i < fixed.length; i++) {
        if (fixed[i] !== null) {
            SetWorld(fixed[i], zero);
        }
    }
    for (var tag in orbitSegments) {
        for (var k = 0; k < orbitSegments[tag].length; k++) {
            var seg = orbitSegments[tag][k];
            var line = Entity.Get(seg.id);
            if (line !== null && (!seg.moon || moonNow !== null)) {
                SetWorld(line, seg.moon ? moonNow.world : zero);   // Moon-centred lines sit on the Moon
            }
        }
    }
}

var fleet = null;         // { catalog number: [ {t, pos, vel}, ... ] }
var fleetNames = {};      // { catalog number: name from the TLE }
var fleetTags = [];       // catalog numbers, in the order the left arrow cycles through them
var windowEnd = 0;
var windowEndReported = false;
var fleetGeneratedT = 0;
// Playback runs on the real clock. WebVerse replaces JavaScript's Date with its own class:
// Date.now is a property giving LOCAL time as fields (year, dayOfYear, hour, ... millisecond).
// fleet.json's "clock" gives the epoch (seconds since 1 Jan UTC) and the machine's UTC offset.
// (Counting timer ticks ran ~14% slow: the runtime drops the remainder each time an interval
// fires, so a 0.1 s interval really fires every ~0.117 s at 60 fps.)
var clockRef = null;
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
//   G                    atmosphere (glow and night side) on / off
//   O                    all orbit lines on / off (each spacecraft also has an [x] box)
//   P                    places (places.xlsx) on / off; ground stations: their [x] box
//   L                    10 deg latitude / longitude grid on / off (also its panel row)
//   V (hold)             pie menu: Show info / Show data (details or next passes for the
//                        selection), Align camera (look along a spacecraft's velocity), Align
//                        vehicle (a spacecraft's attitude mode)
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
    return tag === "Earth" ? EARTH_FRAMING : (tag === "Moon" ? MOON_FRAMING : SPACECRAFT_FRAMING);
}

var focusTag = "Earth";
var focusEntity = null;   // null = world origin (Earth's centre); otherwise the spacecraft followed
var focusScale = 1;       // parent's scale: local offsets are in its scaled space
var camYaw = START_YAW;
var camPitch = START_PITCH;
var camDistance = EARTH_FRAMING.start;

function PlaceCamera() {
    if (siteView !== null) {
        PlaceSiteCamera();
        return;
    }
    if (alignCamera && focusEntity !== null) {
        UpdateAlign();
        return;
    }
    var yaw = camYaw * Math.PI / 180;
    var pitch = camPitch * Math.PI / 180;
    // Camera looks along forward = (cos p sin y, -sin p, cos p cos y); sit opposite it.
    var ox = -camDistance * Math.cos(pitch) * Math.sin(yaw);
    var oy = camDistance * Math.sin(pitch);
    var oz = -camDistance * Math.cos(pitch) * Math.cos(yaw);
    if (focusEntity === null) {
        SetCameraWorld(new Vector3(ox, oy, oz));
    } else {
        // Offset from the spacecraft in world space. The camera is not parented to it (the
        // spacecraft now turn with their attitude modes, which would spin a child camera), so
        // FollowCraft calls this every frame after the spacecraft has moved.
        var p = GetWorld(focusEntity);
        SetCameraWorld(new Vector3(p.x + ox, p.y + oy, p.z + oz));
    }
    Camera.SetEulerRotation(new Vector3(camPitch, camYaw, 0), false);
}

function SetFocus(tag) {
    if (instrumentView !== null) {
        ExitInstrumentView();
    }
    Camera.AttachToEntity(null);
    if (tag === "Earth") {
        focusEntity = null;
        focusScale = 1;
    } else {
        var entity = Entity.GetByTag(tag);
        if (entity === null) {
            Report("focus: no entity tagged " + tag);
            return;
        }
        focusEntity = entity;
        focusScale = entity.GetScale().x;
    }
    focusTag = tag;
    alignCamera = false;
    if (siteView !== null) {
        siteView = null;       // leave the place / station view
        hudDirty = true;       // drops its Ground view row
    }
    camDistance = Framing(tag).start;
    PlaceCamera();
    HudSelect(tag);
    Report("focus " + tag + (fleetNames[tag] ? " (" + fleetNames[tag] + ")" : "")
        + " dist " + camDistance + " parent scale " + focusScale);
}

function Zoom(factor) {
    if (instrumentView !== null) {
        return;
    }
    if (siteView !== null) {
        SiteZoom(factor);
        return;
    }
    var f = Framing(focusTag);
    camDistance = Math.max(f.min, Math.min(f.max, camDistance * factor));
}

var leftWasDown = false;
var gWasDown = false;
var oWasDown = false;
var pWasDown = false;
var lWasDown = false;
var fWasDown = false;
var atmosphereOn = true;

function UpdateCamera() {
    var changed = false;
    var look = Input.GetLookValue();
    var moved = look.x !== 0 || look.y !== 0;
    if (Input.GetLeft() && moved && panelMove !== null) {
        panelOffset[panelMove].x += look.x * PX_TO_VIEW;   // mouse right / up = panel right / up
        panelOffset[panelMove].y += look.y * PX_TO_VIEW;
        panelMoved = true;
    } else if (Input.GetLeft() && moved && !pieOpen && instrumentView === null) {
        if (siteView !== null) {
            SiteDrag(look.x, look.y);
        } else {
            alignCamera = false;           // dragging lets go of Align camera
            camYaw += look.x * ORBIT_DEG_PER_PIXEL;
            camPitch = Math.max(-89, Math.min(89, camPitch - look.y * ORBIT_DEG_PER_PIXEL));
        }
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
    // O: orbit lines on/off
    var oDown = Input.GetKeyValue("o");
    if (oDown && !oWasDown) {
        ToggleOrbits();
    }
    oWasDown = oDown;
    // P: places on/off
    var pDown = Input.GetKeyValue("p");
    if (pDown && !pWasDown) {
        ToggleLayer("places");
    }
    pWasDown = pDown;
    // F: measure the frame rate (server log)
    var fDown = Input.GetKeyValue("f");
    if (fDown && !fWasDown) {
        StartFpsWatch();
        Report("fps: measuring");
    }
    fWasDown = fDown;
    // L: grid on/off
    var lDown = Input.GetKeyValue("l");
    if (lDown && !lWasDown) {
        ToggleGrid(BodyInView());
    }
    lWasDown = lDown;
    // G: atmosphere glow on/off
    var gDown = Input.GetKeyValue("g");
    if (gDown && !gWasDown) {
        var air = Entity.GetByTag("Atmosphere");
        if (air !== null) {
            atmosphereOn = !atmosphereOn;
            air.SetVisibility(atmosphereOn);
            Report("atmosphere " + (atmosphereOn ? "on" : "off"));
        }
    }
    gWasDown = gDown;
    if (Input.GetKeyValue("ArrowRight") && (focusTag !== "Earth" || siteView !== null)) {
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

// One update per frame, in a fixed order, so nothing is placed from a stale position: move the
// spacecraft, turn them toward their attitude modes and turn the Earth, then apply camera
// input and bring the camera to the selected site or spacecraft, then put the labels and the
// panels where the camera now is. Separate timers for these made the panel and labels jitter.
function Tick() {
    Step("Rebase", Rebase);
    Step("UpdateOrbit", UpdateOrbit);
    Step("UpdateAttitudes", UpdateAttitudes);
    Step("UpdateCamera", UpdateCamera);
    Step("UpdateClick", UpdateClick);
    Step("FollowSite", FollowSite);
    Step("FollowCraft", FollowCraft);
    Step("UpdateInstrumentView", UpdateInstrumentView);
    Step("UpdateSites", UpdateSites);
    Step("UpdateLabels", UpdateLabels);
    Step("UpdateSelectBox", UpdateSelectBox);
    Step("UpdateHud", UpdateHud);
    Step("UpdateInfo", UpdateInfo);
    Step("UpdateAttitudePanel", UpdateAttitudePanel);
    Step("UpdateChartPanel", UpdateChartPanel);
    Step("UpdatePlotPanel", UpdatePlotPanel);
    Step("UpdatePie", UpdatePie);
    Step("UpdateFpsWatch", UpdateFpsWatch);
}

// One step of Tick: a step that throws is reported (its name and the error; the first three
// times, then every 600th) and the rest of the frame still runs.
var stepErrors = {};

function Step(name, fn) {
    try {
        fn();
    } catch (e) {
        var n = (stepErrors[name] || 0) + 1;
        stepErrors[name] = n;
        if (n <= 3 || n % 600 === 0) {
            Report("tick error in " + name + " (" + n + "x): " + e);
        }
    }
}


// ---- Assets panel ----
// A heads-up panel built from the runtime's own text and buttons (the approach in Dylan Baker's
// WebVerse samples) on a WORLD-space canvas kept just in front of the camera. Screen-space
// canvases never showed up in this runtime (the HTML panels loaded but stayed invisible),
// while world-space canvases do -- the spacecraft labels use one.
// Each row is a text with a translucent button laid over it: the runtime's button is an image
// with no text, and a text on top would take the click, so the text goes underneath.
// Panels are drawn HUD_DISTANCE in front of the camera, just past its 0.3 near plane, so that
// terrain close to the camera (riding with a low lunar orbiter, ~80 km = 0.8 units up) does not
// cover them. Their layout is kept in view units at HUD_REF (0.6, where it was calibrated):
// placing a panel moves it in to HUD_DISTANCE and shrinks it by VIEW_K, so it looks the same.
const HUD_REF = 0.6;
const HUD_DISTANCE = 0.35;
const VIEW_K = HUD_DISTANCE / HUD_REF;
// Placement in view units at HUD_REF. Measured from a 1903x1025 screenshot: ~1459 px per
// unit (so the vertical FOV is ~59 deg) and the view centre at ~(953, 496) px. The top edge
// sits below WebVerse's address bar (~150 px from the top of the window), the right edge ~210
// px in from the right. It scales with the window, since it is fixed in angle, not pixels.
const HUD_RIGHT_EDGE = 0.505;
const HUD_TOP_EDGE = 0.237;
// UI_SCALE shrinks every panel and the pie menu together (text included): 1 = the original
// size (~240 px wide Assets panel on a 1903 px window), 0.7 = 70%.
const UI_SCALE = 0.7;
const HUD_WORLD_WIDTH = 0.165 * UI_SCALE;
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
// Moving panels: each panel has a "::" grip. Click it, then drag (left button) and the panel
// follows the mouse; releasing drops it. The camera stays still meanwhile. Offsets are in view
// units at HUD_REF from each panel's home corner (lost on reload).
const PX_TO_VIEW = 1 / 1459;  // view units per mouse pixel at HUD_REF (calibrated, see above)
const GRIP_COLOR = new Color(0.5, 0.7, 1, 1);
const GRIP_ARMED = new Color(1, 0.85, 0.3, 1);
var panelOffset = { hud: { x: 0, y: 0 }, info: { x: 0, y: 0 }, att: { x: 0, y: 0 }, chart: { x: 0, y: 0 },
    plot: { x: 0, y: 0 } };
var panelMove = null;         // the panel being moved ("hud", "info", "att"), or null
var panelMoved = false;       // dragged since its grip was clicked
var hudOpen = true;

function MakeRowButton(canvas, onClick, x, y, w, h, color) {
    var button = ButtonEntity.Create(canvas, onClick, new Vector2(x, y), new Vector2(w, h));
    button.SetVisibility(true);
    button.SetBaseColor(color);
    button.SetColors(TINT_NORMAL, TINT_HOVER, TINT_PRESS, TINT_NORMAL);
    return button;
}

function MakeText(canvas, words, x, y, w, h, color, font) {
    var text = TextEntity.Create(canvas, words, font || HUD_FONT, new Vector2(x, y), new Vector2(w, h));
    text.SetVisibility(true);
    text.SetColor(color);
    text.SetTextAlignment(TextAlignment.Center);
    return text;
}

// Rows: Earth, then three tabs -- Spacecraft, Places, Ground stations -- and the open tab's
// list, HUD_PAGE_ROWS at a time with a "< 1-6 of 14 >" row to page through a longer one.
// Clicking a spacecraft rides along with it; clicking a place or station centres it (as
// clicking it on the globe does). Each tab's first row has an [x] box for the whole tab: all
// orbit lines / show places / show ground stations; a spacecraft row's box is its orbit line.
// Switching tabs or paging rebuilds the panel (on the next frame, not inside the click).
const HUD_PAGE_ROWS = 6;
const GRID_BOX_COLOR = new Color(1, 0.78, 0.3, 1);   // the grids' gold
const HUD_ITEM_FONT = 38;
const HUD_TAB_FONT = 30;
const HUD_INDENT = 0.06;            // item rows, as a fraction of the panel width
const HUD_TABS = [{ key: "craft", title: "Spacecraft" }, { key: "places", title: "Places" },
    { key: "stations", title: "Ground\nstations" }];
var hudTab = "craft";
var hudPages = { craft: 0, places: 0, stations: 0 };
var hudDirty = false;               // rebuild the panel on the next frame

// The panel's rows below the ASSETS header, top to bottom.
function HudLayout() {
    var white = new Color(1, 1, 1, 1);
    var rows = [{ tag: "Earth", name: "Earth", onClick: "SelectAsset('Earth');", color: white, box: "grid:Earth" },
        { tag: "Moon", name: "Moon", onClick: "SelectAsset('Moon');", color: new Color(0.85, 0.85, 0.8, 1), box: "grid:Moon",
            res: true }];
    if (moonResOpen) {
        for (var m = 0; m < MOON_LEVELS.length; m++) {
            var lv = MOON_LEVELS[m];
            var built = !!moonBuilt[String(lv)];
            rows.push({ tag: "moonres-" + lv, item: true, onClick: "SelectMoonLevel(" + lv + ");",
                name: (lv === moonLevel ? "> " : "") + lv + "x  " + MOON_LEVEL_TRIANGLES[lv] + " triangles"
                    + (built ? "" : "  (not built)"),
                color: built ? new Color(0.85, 0.85, 0.8, 1) : new Color(0.5, 0.5, 0.5, 1) });
        }
    }
    rows.push({ tabs: true });
    var items = [];
    if (hudTab === "craft") {
        rows.push({ tag: "all-orbits", name: "All orbit lines", onClick: "ToggleOrbits();",
            color: new Color(1, 0.85, 0.55, 1), box: "orbits" });
        for (var n = 0; n < fleetTags.length; n++) {
            var t = fleetTags[n];
            items.push({ tag: t, name: fleetNames[t] || t, onClick: "SelectAsset('" + t + "');",
                color: white, box: "orbit:" + t });
        }
    } else {
        var layer = layers[hudTab];
        rows.push({ tag: "layer-" + hudTab, name: "Show " + layer.title.toLowerCase(),
            onClick: "ToggleLayer('" + hudTab + "');", color: layer.color, box: "layer:" + hudTab });
        for (var i = 0; i < layer.sites.length; i++) {
            var site = layer.sites[i];
            items.push({ tag: "site-" + site.id, name: site.name, onClick: "CentreSiteById('" + site.id + "');",
                color: layer.color });
        }
    }
    var count = items.length;
    if (count > 0) {
        var pages = Math.ceil(count / HUD_PAGE_ROWS);
        hudPages[hudTab] = Math.min(hudPages[hudTab], pages - 1);
        var first = hudPages[hudTab] * HUD_PAGE_ROWS, last = Math.min(count, first + HUD_PAGE_ROWS);
        for (var j = first; j < last; j++) {
            items[j].item = true;
            rows.push(items[j]);
        }
        if (pages > 1) {
            rows.push({ pager: true, name: (first + 1) + "-" + last + " of " + count, color: items[0].color });
        }
    }
    if (siteView !== null) {
        rows.push({ tag: "groundview", name: siteView.mode === "ground" ? "Back to orbit view" : "Ground view (1 km up)",
            onClick: "ToggleGroundView();", color: layers[siteView.site.layer].color });
    }
    rows.push({ tag: "update", name: updateStatus, onClick: "RequestUpdate();", color: new Color(0.6, 1, 0.75, 1),
        update: true });
    return rows;
}

// Called once the fleet is loaded, and again whenever the rows change.
function CreateHud() {
    var layout = HudLayout();
    var canvas = CanvasEntity.Create(null, new Vector3(0, 0, 0), new Quaternion(0, 0, 0, 1),
        new Vector3(1, 1, 1), false, null, "assets-hud");
    canvas.SetVisibility(true);
    canvas.MakeWorldCanvas();
    // header + the rows
    var height = HUD_PAD + (layout.length + 1) * (HUD_ROW + HUD_GAP) + HUD_PAD;
    canvas.SetSize(new Vector2(HUD_W, height));
    hud = { canvas: canvas, rows: [], parts: [], body: [], height: height };
    var fx = HUD_PAD / HUD_W, fw = 1 - 2 * fx, fh = HUD_ROW / height;
    // Children are attached keeping their world size, so the canvas must still be at scale 1
    // while they are created; it is shrunk (PlaceHud) only afterwards. Shrinking it first gave
    // every child a ~2270x local scale and the panel filled the whole view.
    // Created first, so it is drawn underneath everything else (and its clicks do nothing).
    hud.background = HudPart(MakeRowButton(canvas, "", 0, 0, 1, 1, PANEL_COLOR), true);
    var fy = HUD_PAD / height;
    var gripW = 0.16 * fw;
    hud.grip = HudPart(MakeText(canvas, "::", fx, fy, gripW, fh, panelMove === "hud" ? GRIP_ARMED : GRIP_COLOR), false);
    HudPart(MakeRowButton(canvas, "StartMove('hud');", fx, fy, gripW, fh, ROW_COLOR), false);
    hud.header = HudPart(MakeText(canvas, "ASSETS  -", fx + gripW, fy, fw - gripW, fh, new Color(0.5, 0.7, 1, 1)), false);
    hud.headerButton = HudPart(MakeRowButton(canvas, "ToggleAssets();", fx + gripW, fy, fw - gripW, fh, ROW_COLOR), false);
    var boxW = 0.2 * fw, gap = 0.02;
    for (var i = 0; i < layout.length; i++) {
        var row = layout[i];
        var y = (HUD_PAD + (i + 1) * (HUD_ROW + HUD_GAP)) / height;
        if (row.tabs) {
            // one button per tab, the open one highlighted
            var tabW = fw / HUD_TABS.length;
            for (var k = 0; k < HUD_TABS.length; k++) {
                var tx = fx + k * tabW;
                HudPart(MakeText(canvas, HUD_TABS[k].title, tx, y, tabW, fh,
                    HUD_TABS[k].key === "craft" ? new Color(1, 1, 1, 1) : layers[HUD_TABS[k].key].color, HUD_TAB_FONT), true);
                HudPart(MakeRowButton(canvas, "SelectTab('" + HUD_TABS[k].key + "');", tx + 0.005, y, tabW - 0.01, fh,
                    HUD_TABS[k].key === hudTab ? ROW_SELECTED : ROW_COLOR), true);
            }
            continue;
        }
        if (row.pager) {
            // "<" | "1-6 of 14" | ">"
            var arrowW = 0.18 * fw;
            HudPart(MakeText(canvas, "<", fx, y, arrowW, fh, row.color), true);
            HudPart(MakeRowButton(canvas, "PageTab(-1);", fx, y, arrowW, fh, ROW_COLOR), true);
            HudPart(MakeText(canvas, row.name, fx + arrowW, y, fw - 2 * arrowW, fh, row.color, HUD_ITEM_FONT), true);
            HudPart(MakeText(canvas, ">", fx + fw - arrowW, y, arrowW, fh, row.color), true);
            HudPart(MakeRowButton(canvas, "PageTab(1);", fx + fw - arrowW, y, arrowW, fh, ROW_COLOR), true);
            continue;
        }
        var x = fx + (row.item ? HUD_INDENT * fw : 0);
        var resW = 0.26 * fw;
        var nameW = fx + fw - x - (row.box ? boxW + gap : 0) - (row.res ? resW + gap : 0);
        var rec = { tag: row.tag, box: row.box || null,
            text: HudPart(MakeText(canvas, row.name, x, y, nameW, fh, row.color, row.item ? HUD_ITEM_FONT : HUD_FONT), true),
            button: HudPart(MakeRowButton(canvas, row.onClick, x, y, nameW, fh, ROW_COLOR), true) };
        var next = x + nameW + gap;
        if (row.res) {
            // the Moon's resolution: a small drop-down (its list opens as rows below)
            HudPart(MakeText(canvas, MoonResLabel(), next, y, resW, fh, row.color, HUD_ITEM_FONT), true);
            HudPart(MakeRowButton(canvas, "ToggleMoonRes();", next, y, resW, fh, moonResOpen ? ROW_SELECTED : ROW_COLOR), true);
            next += resW + gap;
        }
        if (row.box) {
            rec.boxText = HudPart(MakeText(canvas, BoxText(row.box), next, y, boxW, fh,
                row.box.indexOf("orbit") === 0 ? new Color(1, 0.85, 0.55, 1)
                    : (row.box.indexOf("grid") === 0 ? GRID_BOX_COLOR : row.color)), true);
            rec.boxButton = HudPart(MakeRowButton(canvas, BoxClick(row.box), next, y, boxW, fh,
                ROW_COLOR), true);
        }
        if (row.update) {
            hud.updateText = rec.text;
        }
        hud.rows.push(rec);
    }
    HudSelect(focusTag);
    ApplyHudOpen();
    PlaceHud();
    Report("hud: created with " + layout.length + " rows");
}

// Remember every element of the panel, so it can be deleted and rebuilt; body = all but the header.
function HudPart(entity, body) {
    hud.parts.push(entity);
    if (body) {
        hud.body.push(entity);
    }
    return entity;
}

function RebuildHud() {
    hudDirty = false;
    if (hud === null) {
        return;
    }
    for (var i = 0; i < hud.parts.length; i++) {
        hud.parts[i].Delete();
    }
    hud.canvas.Delete();
    hud = null;
    CreateHud();
}

function SelectTab(key) {
    if (key !== hudTab) {
        hudTab = key;
        hudDirty = true;
        Report("hud: tab " + key);
    }
}

function PageTab(step) {
    var count = hudTab === "craft" ? fleetTags.length : layers[hudTab].sites.length;
    var pages = Math.max(1, Math.ceil(count / HUD_PAGE_ROWS));
    hudPages[hudTab] = (hudPages[hudTab] + step + pages) % pages;
    hudDirty = true;
}

// What a row's [x] box shows, and what clicking it does.
function BoxText(box) {
    if (box.indexOf("grid:") === 0) {
        return gridOn[box.slice(5)] ? "[#]" : "[ ]";       // # = a grid
    }
    if (box === "orbits") {
        var allOn = fleetTags.length > 0;
        for (var n = 0; n < fleetTags.length; n++) {
            allOn = allOn && orbitOn[fleetTags[n]] === true;
        }
        return allOn ? "[x]" : "[ ]";
    }
    if (box.indexOf("orbit:") === 0) {
        return OrbitBox(box.slice(6));
    }
    return LayerBox(box.slice(6));      // "layer:<key>"
}

function BoxClick(box) {
    if (box.indexOf("grid:") === 0) {
        return "ToggleGrid('" + box.slice(5) + "');";
    }
    if (box === "orbits") {
        return "ToggleOrbits();";
    }
    if (box.indexOf("orbit:") === 0) {
        return "ToggleOrbit('" + box.slice(6) + "');";
    }
    return "ToggleLayer('" + box.slice(6) + "');";
}

function RefreshHudBoxes() {
    if (hud === null) {
        return;
    }
    for (var i = 0; i < hud.rows.length; i++) {
        if (hud.rows[i].boxText) {
            hud.rows[i].boxText.SetText(BoxText(hud.rows[i].box));
        }
    }
}

function ReportHud() {
    var sc = hud.canvas.GetScale();
    var p = GetWorld(hud.canvas);
    var c = CameraWorld();
    var dx = p.x - c.x, dy = p.y - c.y, dz = p.z - c.z;
    var child = hud.rows[0].button.GetScale();
    Report("hud: scale " + sc.x.toFixed(6) + " (want " + (HUD_WORLD_WIDTH / HUD_W).toFixed(6)
        + "), row button local scale " + child.x.toFixed(3) + " (want 1), distance from camera "
        + Math.sqrt(dx * dx + dy * dy + dz * dz).toFixed(3));
}

function SelectAsset(tag) {
    Report("hud: clicked " + tag);
    if (targetPendingFor !== null && tag !== "Earth" && tag !== targetPendingFor) {
        SetTarget({ kind: "craft", tag: tag });
        return;
    }
    SetFocus(tag);
}

function ToggleAssets() {
    hudOpen = !hudOpen;
    ApplyHudOpen();
    Report("hud: " + (hudOpen ? "opened" : "closed"));
}

function ApplyHudOpen() {
    for (var i = 0; i < hud.body.length; i++) {
        hud.body[i].SetVisibility(hudOpen);
    }
    hud.header.SetText(hudOpen ? "ASSETS  -" : "ASSETS  +");
}

// Highlight the row the camera is centred on: a clicked place or station, else the focus.
function HudSelect(tag) {
    if (hud === null) {
        return;
    }
    var selected = siteView !== null ? "site-" + siteView.site.id : focusTag;
    for (var i = 0; i < hud.rows.length; i++) {
        hud.rows[i].button.SetBaseColor(hud.rows[i].tag === selected ? ROW_SELECTED : ROW_COLOR);
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
    if (hudDirty) {
        RebuildHud();
    }
    PlaceHud();
}

function PlaceHud() {
    var s = HUD_WORLD_WIDTH / HUD_W;
    hud.canvas.SetScale(new Vector3(s * VIEW_K, s * VIEW_K, s * VIEW_K), false);
    var q = Camera.GetRotation(false);
    var p = CameraWorld();
    var f = Rotate(q, 0, 0, 1), r = Rotate(q, 1, 0, 0), u = Rotate(q, 0, 1, 0);
    // centre of the panel, from its top-right corner (home corner + where it has been moved)
    var right = HUD_RIGHT_EDGE + panelOffset.hud.x - HUD_WORLD_WIDTH / 2;
    var up = HUD_TOP_EDGE + panelOffset.hud.y - HUD_WORLD_WIDTH * hud.height / HUD_W / 2;
    SetWorld(hud.canvas, new Vector3(
        p.x + f[0] * HUD_DISTANCE + (r[0] * right + u[0] * up) * VIEW_K,
        p.y + f[1] * HUD_DISTANCE + (r[1] * right + u[1] * up) * VIEW_K,
        p.z + f[2] * HUD_DISTANCE + (r[2] * right + u[2] * up) * VIEW_K));
    hud.canvas.SetRotation(q, false);
}

// ---- Pie menu (hold V) ----
// While V is held, four options sit around a hub in the middle of the view; releasing V hides
// it. Built like the Assets panel: native text + buttons on a world-space canvas placed in
// front of the camera every frame (children created at scale 1, the canvas shrunk after).
// While it is open, left-drag doesn't orbit the camera, so the options can be clicked.
const PIE_W = 600;                 // canvas units (square)
const PIE_WORLD_WIDTH = 0.20 * UI_SCALE;   // at HUD_REF: ~290 px at UI_SCALE 1
const PIE_FONT = 36;
const PIE_COLOR = new Color(0.08, 0.09, 0.13, 0.88);
const PIE_OPTIONS = [              // id, label, x, y, w, h (fractions of the canvas, top-left)
    ["align-camera", "Align camera", 0.27, 0.03, 0.46, 0.22],
    ["align-vehicle", "Align\nvehicle", 0.66, 0.37, 0.32, 0.26],
    ["show-data", "Show data", 0.27, 0.75, 0.46, 0.22],
    ["show-info", "Show\ninfo", 0.02, 0.37, 0.32, 0.26]
];
var pie = null;                    // { canvas, hub }
var pieOpen = false;

function CreatePie() {
    var canvas = CanvasEntity.Create(null, new Vector3(0, 0, 0), new Quaternion(0, 0, 0, 1),
        new Vector3(1, 1, 1), false, null, "pie-menu");
    canvas.SetVisibility(true);
    canvas.MakeWorldCanvas();
    canvas.SetSize(new Vector2(PIE_W, PIE_W));
    pie = { canvas: canvas };
    for (var i = 0; i < PIE_OPTIONS.length; i++) {
        var o = PIE_OPTIONS[i];
        MakeRowButton(canvas, "", o[2], o[3], o[4], o[5], PIE_COLOR);
        MakeText(canvas, o[1], o[2], o[3], o[4], o[5], new Color(1, 1, 1, 1), PIE_FONT);
        MakeRowButton(canvas, "PieChoose('" + o[0] + "');", o[2], o[3], o[4], o[5], ROW_COLOR);
    }
    MakeRowButton(canvas, "", 0.42, 0.42, 0.16, 0.16, new Color(0.5, 0.7, 1, 0.35));
    pie.hub = MakeText(canvas, "V", 0.42, 0.42, 0.16, 0.16, new Color(0.5, 0.7, 1, 1), PIE_FONT);
    PlacePie();
    canvas.SetVisibility(false);   // hidden until V is held
}

function PlacePie() {
    var s = PIE_WORLD_WIDTH / PIE_W;
    pie.canvas.SetScale(new Vector3(s * VIEW_K, s * VIEW_K, s * VIEW_K), false);
    var q = Camera.GetRotation(false);
    var p = CameraWorld();
    var f = Rotate(q, 0, 0, 1);
    SetWorld(pie.canvas, new Vector3(p.x + f[0] * HUD_DISTANCE, p.y + f[1] * HUD_DISTANCE,
        p.z + f[2] * HUD_DISTANCE));
    pie.canvas.SetRotation(q, false);
}

function UpdatePie() {
    if (pie === null) {
        return;
    }
    var vDown = Input.GetKeyValue("v");
    if (vDown !== pieOpen) {
        pieOpen = vDown;
        pie.canvas.SetVisibility(pieOpen);
        Report("pie: " + (pieOpen ? "open" : "closed"));
    }
    if (pieOpen) {
        PlacePie();
    }
}

// Pie menu actions. They act on the selection: a place or station (siteView), else the
// focused spacecraft, else the Earth.
//   Show info      the info panel: details and next passes of the selection (it follows it)
//   Show data      a spacecraft: the view from its instrument camera (see "Instrument view")
//   Align camera   a spacecraft: look along its velocity vector (see UpdateAlign)
//   Align vehicle  a spacecraft: the attitude panel (Sun / LVLH / Nadir / Zenith / Normal /
//                  Target / Hold, see "Attitude")
function PieChoose(option) {
    Report("pie: chose " + option);
    if (option === "show-info") {
        ToggleInfo("info");
    } else if (option === "show-data") {
        ToggleInstrumentView();
    } else if (option === "align-camera") {
        ToggleAlignCamera();
    } else if (option === "align-vehicle") {
        ToggleAttitudePanel();
    }
}

// ---- Info panel (pie menu: Show info) ----
// Top left of the view, built like the Assets panel (a world-space canvas in front of the
// camera, the same scale), in 10 pt text: at the Assets panel's ~0.48 px per canvas unit,
// 28 canvas units is ~13.3 px, i.e. 10 pt at 96 dpi. For a spacecraft it lists the osculating
// orbital elements GMAT reported (data/fleet.json "elements", every 60 s, interpolated here);
// for a place or station its location (and a station's settings and current contacts).
const INFO_W = 700;                // canvas units (a pass line is ~45 characters)
const INFO_FONT = 28;              // 10 pt
const INFO_LINE = 36;
const INFO_LINES = 32;
const INFO_PAD = 16;
const INFO_REFRESH_FRAMES = 10;    // rewrite the text every 10 frames
var info = null;                   // { canvas, background, text, height }
var infoOn = false;
var infoMode = "info";             // "info" (Show info) or "data" (Show data: passes)
var infoFrame = 0;
var fleetElements = {};            // catalog -> { fields, rows }

function CreateInfo() {
    var canvas = CanvasEntity.Create(null, new Vector3(0, 0, 0), new Quaternion(0, 0, 0, 1),
        new Vector3(1, 1, 1), false, null, "info-panel");
    canvas.SetVisibility(true);
    canvas.MakeWorldCanvas();
    var height = 2 * INFO_PAD + INFO_LINES * INFO_LINE;
    canvas.SetSize(new Vector2(INFO_W, height));
    info = { canvas: canvas, height: height };
    // children at canvas scale 1, the canvas shrunk afterwards (see CreateHud)
    info.background = MakeRowButton(canvas, "", 0, 0, 1, 1, PANEL_COLOR);
    var gripW = 60 / INFO_W, gripH = 50 / height;
    info.text = MakeText(canvas, "", INFO_PAD / INFO_W, INFO_PAD / height, 1 - 2 * INFO_PAD / INFO_W - gripW,
        1 - 2 * INFO_PAD / height, new Color(1, 1, 1, 1), INFO_FONT);
    info.grip = MakeText(canvas, "::", 1 - INFO_PAD / INFO_W - gripW, INFO_PAD / height, gripW, gripH, GRIP_COLOR, INFO_FONT);
    MakeRowButton(canvas, "StartMove('info');", 1 - INFO_PAD / INFO_W - gripW, INFO_PAD / height, gripW, gripH, ROW_COLOR);
    info.text.SetTextAlignment(TextAlignment.Left);
    PlaceInfo();
    canvas.SetVisibility(false);
}

// The same panel for both: choosing the mode that is showing closes it, the other switches.
function ToggleInfo(mode) {
    if (info === null) {
        CreateInfo();
    }
    infoOn = !(infoOn && infoMode === mode);
    infoMode = mode;
    info.canvas.SetVisibility(infoOn);
    infoFrame = 0;
    UpdateInfo();
    Report("info panel " + (infoOn ? mode : "off"));
}

// Mirror of the Assets panel: its top-left corner at the left edge of the view.
function PlaceInfo() {
    var s = HUD_WORLD_WIDTH / HUD_W;
    var width = INFO_W * s, height = info.height * s;
    info.canvas.SetScale(new Vector3(s * VIEW_K, s * VIEW_K, s * VIEW_K), false);
    var q = Camera.GetRotation(false);
    var p = CameraWorld();
    var f = Rotate(q, 0, 0, 1), r = Rotate(q, 1, 0, 0), u = Rotate(q, 0, 1, 0);
    var right = -HUD_RIGHT_EDGE + panelOffset.info.x + width / 2;
    var up = HUD_TOP_EDGE + panelOffset.info.y - height / 2;
    SetWorld(info.canvas, new Vector3(
        p.x + f[0] * HUD_DISTANCE + (r[0] * right + u[0] * up) * VIEW_K,
        p.y + f[1] * HUD_DISTANCE + (r[1] * right + u[1] * up) * VIEW_K,
        p.z + f[2] * HUD_DISTANCE + (r[2] * right + u[2] * up) * VIEW_K));
    info.canvas.SetRotation(q, false);
}

function UpdateInfo() {
    infoFrame++;
    if (info === null || !infoOn) {
        return;
    }
    PlaceInfo();
    if (infoFrame % INFO_REFRESH_FRAMES === 1 || INFO_REFRESH_FRAMES === 1) {
        info.text.SetText(InfoText());
    }
}

function InfoText() {
    var lines = [];
    if (siteView !== null) {
        var site = siteView.site;
        lines.push(site.name);
        lines.push((site.link ? "Ground station" : "Place") + " on the " + site.body + "   " + UtcString(elapsedSeconds));
        lines.push("");
        lines.push("Latitude    " + Deg(site.lat, 5));
        lines.push("Longitude   " + Deg(site.lon, 5));
        lines.push("Ground      " + (site.terrain ? site.terrain.ground_m : site.groundKm * 1000).toFixed(0)
            + (site.body === "Moon" ? " m above the mean radius" + (site.terrain ? " (LOLA)" : "") : " m above sea level"));
        lines.push("AGL         " + site.aglM.toFixed(1) + " m");
        if (site.terrain) {
            lines.push("");
            lines = lines.concat(TerrainLines(site));
        }
        if (site.link) {
            lines.push("");
            for (var ri = 0; ri < site.radios.length; ri++) {
                lines.push((ri === 0 ? "Radios      " : "            ") + RadioWords(site.radios[ri]));
            }
            lines.push("Beam FOV    " + site.fovDeg + " deg (to " + (90 - site.fovDeg / 2).toFixed(1) + " deg elevation)");
            lines.push("Link        " + site.link);
            var inBeam = [];
            for (var tag in site.links) {
                if (linkShown[site.links[tag]]) {
                    inBeam.push(fleetNames[tag] || tag);
                }
            }
            lines.push("In beam     " + (inBeam.length > 0 ? inBeam.join(", ") : "none"));
            lines.push("");
            lines = lines.concat(BudgetSection(site, null));
            lines.push("");
            lines = lines.concat(PassLines("craft", site.name, 4));
        }
        if (siteView.mode === "ground") {
            lines.push("");
            lines.push("Ground view: looking " + Deg(((siteView.lookAz % 360) + 360) % 360, 1) + " az, "
                + Deg(siteView.lookEl, 1) + " el");
        }
    } else if (focusTag !== "Earth") {
        var e = ElementsAt(focusTag, elapsedSeconds);
        lines.push((fleetNames[focusTag] || focusTag) + "  (" + focusTag + ")");
        lines.push(UtcString(elapsedSeconds));
        var about = fleetElements[focusTag] ? fleetElements[focusTag] : {};
        lines.push("Osculating elements about the " + (about.body || "Earth") + ", "
            + (about.body === "Moon" ? "Moon equator of date" : "EarthMJ2000Eq") + " (" + (about.source || "GMAT") + ")");
        lines.push("");
        if (e === null) {
            lines.push("No elements in fleet.json: rerun tools/run_fleet.py");
        } else {
            lines.push("SMA      " + e.sma.toFixed(3) + " km");
            lines.push("ECC      " + e.ecc.toFixed(6));
            lines.push("INC      " + Deg(e.inc, 4));
            lines.push("RAAN     " + Deg(e.raan, 4));
            lines.push("AOP      " + Deg(e.aop, 4));
            lines.push("TA       " + Deg(e.ta, 4));
            lines.push("AOP+TA   " + Deg((e.aop + e.ta) % 360, 4) + "  (argument of latitude)");
            lines.push("");
            lines.push("Period   " + (e.period / 60).toFixed(2) + " min");
            var bodyR = about.body === "Moon" ? MOON_RADIUS_KM : EARTH_RADIUS_KM;
            lines.push("Apoapsis   " + (e.rapo - bodyR).toFixed(1) + " km alt");
            lines.push("Periapsis  " + (e.rper - bodyR).toFixed(1) + " km alt");
            lines.push("");
            lines.push("Attitude " + AttitudeStatus(focusTag));
            if (alignCamera) {
                lines.push("Camera aligned to the velocity vector");
            }
            if (instrumentView !== null) {
                lines.push("Viewing through " + instrumentView.cam.name);
            }
            lines.push("");
            lines = lines.concat(BudgetSection(null, focusTag));
            lines.push("");
            lines = lines.concat(PassLines("station", focusTag, 3));
        }
    } else {
        lines.push("Earth");
        lines.push(UtcString(elapsedSeconds));
        lines.push("");
        lines.push("Select a spacecraft, place or ground station");
        lines.push("to see its details here.");
        lines.push("");
        lines = lines.concat(PassLines("both", null, 8));
    }
    return lines.join("\n");
}

const EARTH_RADIUS_KM = 6378.1363;

function Deg(value, places) {
    return value.toFixed(places) + "°";
}

// Elements at time t (seconds after the window epoch): linear between GMAT's 60 s rows, the
// angles the short way round. null if fleet.json has none for this spacecraft.
function ElementsAt(tag, t) {
    var el = fleetElements[tag];
    if (!el || el.rows.length < 2) {
        return null;
    }
    var rows = el.rows, lo = 0, hi = rows.length - 1;
    if (t <= rows[0][0]) {
        hi = 1;
    } else if (t >= rows[hi][0]) {
        lo = hi - 1;
    } else {
        while (hi - lo > 1) {
            var mid = (lo + hi) >> 1;
            if (rows[mid][0] <= t) {
                lo = mid;
            } else {
                hi = mid;
            }
        }
    }
    var a = rows[lo], b = rows[hi];
    var u = Math.max(0, Math.min(1, (t - a[0]) / (b[0] - a[0])));
    var out = {};
    for (var k = 1; k < el.fields.length; k++) {
        var name = el.fields[k];
        var d = b[k] - a[k];
        if (name === "raan" || name === "aop" || name === "ta") {
            d = ((d % 360) + 540) % 360 - 180;
            out[name] = ((a[k] + d * u) % 360 + 360) % 360;
        } else {
            out[name] = a[k] + d * u;
        }
    }
    return out;
}

// "05 Oct 2026 01:23:45 UTC" for t seconds after the window epoch (from fleet.json's clock).
function UtcString(t) {
    if (clockRef === null) {
        return "";
    }
    var year = clockRef.epoch_year;
    var secs = clockRef.epoch_doy_s + t;
    var leap = (year % 4 === 0 && year % 100 !== 0) || year % 400 === 0;
    if (secs >= (leap ? 366 : 365) * 86400) {
        secs -= (leap ? 366 : 365) * 86400;
        year++;
        leap = (year % 4 === 0 && year % 100 !== 0) || year % 400 === 0;
    }
    var day = Math.floor(secs / 86400), rest = secs - day * 86400;
    var months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
    var lengths = [31, leap ? 29 : 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31];
    var m = 0;
    while (day >= lengths[m]) {
        day -= lengths[m];
        m++;
    }
    var pad = function (n) { return (n < 10 ? "0" : "") + n; };
    var h = Math.floor(rest / 3600), mi = Math.floor((rest % 3600) / 60), s = Math.floor(rest % 60);
    return pad(day + 1) + " " + months[m] + " " + year + " " + pad(h) + ":" + pad(mi) + ":" + pad(s) + " UTC";
}

// ---- Align camera (pie menu) ----
// For the focused spacecraft: the camera sits behind it on its velocity vector, looking
// forward along it, with "up" away from the Earth (so the Earth stays below). Kept each frame
// as the spacecraft turns along its orbit; zoom still works; choose Align camera again, drag,
// or change the focus to let go. (There is no attitude data yet, so no boresight: the
// spacecraft models are not turned.)
var alignCamera = false;

function ToggleAlignCamera() {
    if (focusTag === "Earth" || siteView !== null) {
        Report("align camera: select a spacecraft first");
        return;
    }
    alignCamera = !alignCamera;
    if (!alignCamera) {
        PlaceCamera();
    }
    infoFrame = 0;
    Report("align camera " + (alignCamera ? "on (velocity)" : "off") + " for " + focusTag);
}

// Velocity (km/s, GMAT axes) at time t: linear between the 60 s states.
function VelocityAt(track, t) {
    var i = 0;
    while (i < track.length - 2 && track[i + 1].t <= t) {
        i++;
    }
    var a = track[i], b = track[i + 1];
    var u = Math.max(0, Math.min(1, (t - a.t) / (b.t - a.t)));
    return [a.vel[0] + (b.vel[0] - a.vel[0]) * u, a.vel[1] + (b.vel[1] - a.vel[1]) * u,
        a.vel[2] + (b.vel[2] - a.vel[2]) * u];
}

// Every frame, after the spacecraft have moved: keep the camera with the focused spacecraft
// (it is not parented to it), on the velocity line when Align camera is on.
function FollowCraft() {
    if (instrumentView !== null || siteView !== null || focusEntity === null) {
        return;
    }
    if (!alignCamera) {
        PlaceCamera();
        return;
    }
    UpdateAlign();
}

function UpdateAlign() {
    if (!alignCamera || focusEntity === null || fleet === null || !fleet[focusTag]) {
        return;
    }
    var v = RelativeState(focusTag).v;                  // relative to the body orbited
    var fwd = [v[0], v[2], v[1]];                       // GMAT -> Unity axes
    var len = Math.sqrt(Dot3(fwd, fwd));
    fwd = [fwd[0] / len, fwd[1] / len, fwd[2] / len];
    var p = GetWorld(focusEntity);
    var c = CraftBody(focusTag) === "Moon" && moonNow !== null ? moonNow.world : new Vector3(0, 0, 0);
    var up = [p.x - c.x, p.y - c.y, p.z - c.z];          // away from the body's centre
    // behind the spacecraft on the velocity line
    SetCameraWorld(new Vector3(p.x - fwd[0] * camDistance, p.y - fwd[1] * camDistance,
        p.z - fwd[2] * camDistance));
    Camera.SetRotation(LookRotation(fwd, up), false);
}


// ---- Attitude (pie menu: Align vehicle) ----
// Each spacecraft model turns toward the attitude its mode asks for, at most SLEW_DEG_PER_S
// (a rate-limited slerp each frame). Body axes of probe.glb: the telescope aperture is at the
// model's +X end (glTFast mirrors X, so Unity local -X) -- the boresight -- and the solar
// arrays run along local Y. Each mode points the boresight at a primary direction and turns
// the arrays toward a secondary one:
//   Sun     boresight at the Sun (GMAT's direction for this run), arrays along the orbit normal
//   LVLH    boresight along the velocity (ram), arrays along the orbit normal: fixed in the
//           local-vertical / local-horizontal frame, so it turns once per orbit
//   Nadir   boresight at the Earth's centre, arrays along the orbit normal
//   Zenith  boresight straight away from the Earth, arrays along the orbit normal
//   Normal  boresight along the orbit normal (r x v), arrays along the velocity
//   Target  boresight at a chosen place, ground station or spacecraft, held as both move;
//           after choosing Target, the next place / station / spacecraft selected becomes the
//           target (the view does not move)
//   Hold    keep the current attitude (inertial hold) -- every spacecraft starts here
const SLEW_DEG_PER_S = 10;
const ATT_MODES = [["sun", "Sun"], ["lvlh", "LVLH"], ["nadir", "Nadir"], ["zenith", "Zenith"],
    ["normal", "Normal"], ["target", "Target"], ["hold", "Hold"]];
var attitudes = {};          // tag -> { q: [x, y, z, w], mode, target, error (deg to go) }
var attitudeLastT = null;
var sunDir = [1, 0, 0];      // GMAT axes, from fleet.json
var targetPendingFor = null; // spacecraft waiting for its target to be picked

function Attitude(tag) {
    if (!attitudes[tag]) {
        attitudes[tag] = { q: [0, 0, 0, 1], mode: "hold", target: null, error: 0 };
    }
    return attitudes[tag];
}

function UpdateAttitudes() {
    if (fleet === null) {
        return;
    }
    var dt = attitudeLastT === null ? 0 : Math.max(0, Math.min(1, elapsedSeconds - attitudeLastT));
    attitudeLastT = elapsedSeconds;
    var maxStep = SLEW_DEG_PER_S * dt * Math.PI / 180;
    for (var n = 0; n < fleetTags.length; n++) {
        var tag = fleetTags[n];
        var entity = Entity.GetByTag(tag);
        if (entity === null) {
            continue;
        }
        var att = Attitude(tag);
        var want = DesiredAttitude(tag, att, entity);
        if (want !== null) {
            att.q = SlewToward(att.q, want, maxStep);
            att.error = QuatAngle(att.q, want) * 180 / Math.PI;
        } else {
            att.error = 0;
        }
        entity.SetRotation(new Quaternion(att.q[0], att.q[1], att.q[2], att.q[3]), false);
    }
}

// The attitude the mode asks for now, as [x, y, z, w]; null = hold the current one.
function DesiredAttitude(tag, att, entity) {
    var rel = RelativeState(tag);                        // about the body orbited, GMAT axes
    var r = Unit3(rel.p);
    var v = Unit3(rel.v);
    var h = Unit3(Cross3(r, v));                          // orbit normal
    var primary = null, secondary = h;
    if (att.mode === "sun") {
        primary = Unit3(sunDir);
    } else if (att.mode === "lvlh") {
        primary = v;
    } else if (att.mode === "nadir") {
        primary = [-r[0], -r[1], -r[2]];
    } else if (att.mode === "zenith") {
        primary = r;
    } else if (att.mode === "normal") {
        primary = h;
        secondary = v;
    } else if (att.mode === "target") {
        var t = TargetPosition(att.target, tag);
        if (t === null) {
            return null;
        }
        var p = GetWorld(entity);
        primary = Unit3([t.x - p.x, t.z - p.z, t.y - p.y]);  // Unity -> GMAT axes
    }
    if (primary === null) {
        return null;
    }
    // GMAT -> Unity axes (swap Y and Z) for the model's rotation
    return BodyQuat([primary[0], primary[2], primary[1]], [secondary[0], secondary[2], secondary[1]],
        [v[0], v[2], v[1]]);
}

// World position (Unity) of a target, or null.
function TargetPosition(target, selfTag) {
    if (target === null) {
        return null;
    }
    if (target.kind === "site") {
        return target.site.world;
    }
    if (target.tag === selfTag) {
        return null;
    }
    var e = Entity.GetByTag(target.tag);
    return e === null ? null : GetWorld(e);
}

// Rotation (Unity axes) putting the boresight (local -X) along b and the arrays (local Y) as
// close to s as possible; `fallback` replaces s when s is (nearly) along b.
function BodyQuat(b, s, fallback) {
    var d = Dot3(s, b);
    var y = [s[0] - d * b[0], s[1] - d * b[1], s[2] - d * b[2]];
    if (Math.sqrt(Dot3(y, y)) < 1e-3) {
        d = Dot3(fallback, b);
        y = [fallback[0] - d * b[0], fallback[1] - d * b[1], fallback[2] - d * b[2]];
    }
    y = Unit3(y);
    var x = [-b[0], -b[1], -b[2]];
    var z = [x[1] * y[2] - x[2] * y[1], x[2] * y[0] - x[0] * y[2], x[0] * y[1] - x[1] * y[0]];
    return BasisQuat(x, y, z);
}

// Rotate q toward w by at most maxStep radians (shortest way).
function SlewToward(q, w, maxStep) {
    var d = q[0] * w[0] + q[1] * w[1] + q[2] * w[2] + q[3] * w[3];
    if (d < 0) {
        w = [-w[0], -w[1], -w[2], -w[3]];
        d = -d;
    }
    var angle = 2 * Math.acos(Math.min(1, d));
    if (angle <= maxStep || angle < 1e-6) {
        return w;
    }
    var t = maxStep / angle, theta = angle / 2, sinT = Math.sin(theta);
    var a = Math.sin((1 - t) * theta) / sinT, c = Math.sin(t * theta) / sinT;
    var out = [a * q[0] + c * w[0], a * q[1] + c * w[1], a * q[2] + c * w[2], a * q[3] + c * w[3]];
    var len = Math.sqrt(out[0] * out[0] + out[1] * out[1] + out[2] * out[2] + out[3] * out[3]);
    return [out[0] / len, out[1] / len, out[2] / len, out[3] / len];
}

function QuatAngle(q, w) {
    var d = Math.abs(q[0] * w[0] + q[1] * w[1] + q[2] * w[2] + q[3] * w[3]);
    return 2 * Math.acos(Math.min(1, d));
}

function Unit3(a) {
    var l = Math.sqrt(Dot3(a, a));
    return [a[0] / l, a[1] / l, a[2] / l];
}

function Cross3(a, b) {
    return [a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0]];
}

function AttitudeStatus(tag) {
    var att = Attitude(tag);
    var name = "Hold";
    for (var i = 0; i < ATT_MODES.length; i++) {
        if (ATT_MODES[i][0] === att.mode) {
            name = ATT_MODES[i][1];
        }
    }
    if (att.mode === "target") {
        if (targetPendingFor === tag) {
            name += " (pick a place, station or spacecraft)";
        } else if (att.target !== null) {
            name += ": " + (att.target.kind === "site" ? att.target.site.name : (fleetNames[att.target.tag] || att.target.tag));
        }
    }
    if (att.mode !== "hold" && att.error > 0.1) {
        return name + ", slewing (" + att.error.toFixed(0) + "° to go)";
    }
    return name + (att.mode === "hold" ? "" : ", holding");
}

// A place, station or spacecraft picked while a spacecraft waits for its target.
function SetTarget(target) {
    var att = Attitude(targetPendingFor);
    att.target = target;
    att.mode = "target";
    Report("attitude: " + targetPendingFor + " targets "
        + (target.kind === "site" ? target.site.name : target.tag));
    targetPendingFor = null;
    RefreshAttitudePanel();
}

// ---- Attitude panel ----
// Opened by Align vehicle for the focused spacecraft: its name, a button per mode (two
// columns; the current one highlighted) and a status line. Sits on the left, under the info
// panel's space, built like the other panels.
const ATT_ROW = 80;
const ATT_GAP = 10;
var attPanel = null;         // { canvas, height, header, status, buttons: { mode: button } }
var attPanelOn = false;
var attPanelTag = null;

function ToggleAttitudePanel() {
    if (siteView !== null || focusTag === "Earth") {
        Report("align vehicle: select a spacecraft first");
        return;
    }
    if (attPanelOn && attPanelTag === focusTag) {
        attPanelOn = false;
        targetPendingFor = null;
    } else {
        attPanelOn = true;
        attPanelTag = focusTag;
    }
    if (attPanel === null) {
        CreateAttitudePanel();
    }
    attPanel.canvas.SetVisibility(attPanelOn);
    RefreshAttitudePanel();
}

function CreateAttitudePanel() {
    var canvas = CanvasEntity.Create(null, new Vector3(0, 0, 0), new Quaternion(0, 0, 0, 1),
        new Vector3(1, 1, 1), false, null, "attitude-panel");
    canvas.SetVisibility(true);
    canvas.MakeWorldCanvas();
    var rows = 2 + Math.ceil(ATT_MODES.length / 2);      // header, mode rows, status
    var height = 2 * INFO_PAD + rows * (ATT_ROW + ATT_GAP);
    canvas.SetSize(new Vector2(INFO_W, height));
    attPanel = { canvas: canvas, height: height, buttons: {} };
    var fx = INFO_PAD / INFO_W, fw = 1 - 2 * fx, fh = ATT_ROW / height;
    MakeRowButton(canvas, "", 0, 0, 1, 1, PANEL_COLOR);
    var gripW = 0.12 * fw;
    attPanel.grip = MakeText(canvas, "::", fx, INFO_PAD / height, gripW, fh, GRIP_COLOR, HUD_FONT);
    MakeRowButton(canvas, "StartMove('att');", fx, INFO_PAD / height, gripW, fh, ROW_COLOR);
    attPanel.header = MakeText(canvas, "", fx + gripW, INFO_PAD / height, fw - gripW, fh, new Color(0.5, 0.7, 1, 1), HUD_FONT);
    var colW = (fw - 0.02) / 2;
    for (var i = 0; i < ATT_MODES.length; i++) {
        var x = fx + (i % 2) * (colW + 0.02);
        var y = (INFO_PAD + (1 + Math.floor(i / 2)) * (ATT_ROW + ATT_GAP)) / height;
        MakeText(canvas, ATT_MODES[i][1], x, y, colW, fh, new Color(1, 1, 1, 1), HUD_FONT);
        attPanel.buttons[ATT_MODES[i][0]] = MakeRowButton(canvas, "SetAttitudeMode('" + ATT_MODES[i][0] + "');",
            x, y, colW, fh, ROW_COLOR);
    }
    attPanel.status = MakeText(canvas, "", fx, (INFO_PAD + (rows - 1) * (ATT_ROW + ATT_GAP)) / height, fw, fh,
        new Color(0.6, 1, 0.75, 1), INFO_FONT);
    PlaceAttitudePanel();
}

function SetAttitudeMode(mode) {
    if (attPanelTag === null) {
        return;
    }
    var att = Attitude(attPanelTag);
    att.mode = mode;
    targetPendingFor = mode === "target" ? attPanelTag : null;   // pick (or re-pick) the target
    RefreshAttitudePanel();
    Report("attitude: " + attPanelTag + " " + mode);
}

function RefreshAttitudePanel() {
    if (attPanel === null || attPanelTag === null) {
        return;
    }
    var att = Attitude(attPanelTag);
    attPanel.header.SetText("Attitude: " + (fleetNames[attPanelTag] || attPanelTag));
    for (var i = 0; i < ATT_MODES.length; i++) {
        attPanel.buttons[ATT_MODES[i][0]].SetBaseColor(ATT_MODES[i][0] === att.mode ? ROW_SELECTED : ROW_COLOR);
    }
    attPanel.status.SetText(AttitudeStatus(attPanelTag));
}

// Under the info panel's space on the left (whether or not the info panel is showing).
function AttitudePanelTop() {
    var s = HUD_WORLD_WIDTH / HUD_W;
    return HUD_TOP_EDGE - (2 * INFO_PAD + INFO_LINES * INFO_LINE) * s - 0.01;
}

function PlaceAttitudePanel() {
    var s = HUD_WORLD_WIDTH / HUD_W;
    attPanel.canvas.SetScale(new Vector3(s * VIEW_K, s * VIEW_K, s * VIEW_K), false);
    var q = Camera.GetRotation(false);
    var p = CameraWorld();
    var f = Rotate(q, 0, 0, 1), r = Rotate(q, 1, 0, 0), u = Rotate(q, 0, 1, 0);
    var right = -HUD_RIGHT_EDGE + panelOffset.att.x + INFO_W * s / 2;
    var up = AttitudePanelTop() + panelOffset.att.y - attPanel.height * s / 2;
    SetWorld(attPanel.canvas, new Vector3(
        p.x + f[0] * HUD_DISTANCE + (r[0] * right + u[0] * up) * VIEW_K,
        p.y + f[1] * HUD_DISTANCE + (r[1] * right + u[1] * up) * VIEW_K,
        p.z + f[2] * HUD_DISTANCE + (r[2] * right + u[2] * up) * VIEW_K));
    attPanel.canvas.SetRotation(q, false);
}

function UpdateAttitudePanel() {
    if (attPanel === null || !attPanelOn) {
        return;
    }
    PlaceAttitudePanel();
    if (infoFrame % INFO_REFRESH_FRAMES === 0) {
        attPanel.status.SetText(AttitudeStatus(attPanelTag));
    }
}

// Whether a click (view units) is on the info or attitude panel, so it does not pick a site.
function OverLeftPanels(sx, sy) {
    var s = HUD_WORLD_WIDTH / HUD_W;
    var x = sx * HUD_REF, y = sy * HUD_REF;
    var inside = function (off, top, height) {
        var left = -HUD_RIGHT_EDGE + off.x;
        return x >= left && x <= left + INFO_W * s && y <= top + off.y && y >= top + off.y - height;
    };
    if (infoOn && info !== null && inside(panelOffset.info, HUD_TOP_EDGE, info.height * s)) {
        return true;
    }
    return attPanelOn && attPanel !== null && inside(panelOffset.att, AttitudePanelTop(), attPanel.height * s);
}

// ---- Instrument view (pie menu: Show data) ----
// For the focused spacecraft: the viewer's camera moves to the instrument camera found in its
// model (models/source/<model>.glb, read by tools/instruments.py into fleet.json "instruments")
// and looks where it looks, following the spacecraft's attitude. WebVerse scripts cannot set
// the camera's field of view (only X3D worlds can), so a white frame marks the instrument's own
// field of view in the ~59 deg view: what the instrument sees is inside the frame (no frame when
// the instrument's view is wider than the screen's). Show data again, Earth / Moon, R, the arrow
// keys or another spacecraft leave it. The camera can't be dragged or zoomed meanwhile.
const FRAME_ID = "b0e5a000-0000-4000-a000-000000000004";
const FRAME_DISTANCE = 0.37;       // between the panels (0.35) and the labels (0.40)
const VIEW_HALF_FOV_DEG = 29.5;    // the camera's measured vertical field of view is ~59 deg
var instrumentDefs = {};           // model file -> [ { name, pos, fwd, up, yfov_deg, aspect } ]
var instrumentView = null;         // { tag, cam } while the view is on
var frameShown = false;
var frameTag = null;               // { canvas, text } -- the "INSTRUMENT ..." caption

function ToggleInstrumentView() {
    if (instrumentView !== null) {
        ExitInstrumentView();
        PlaceCamera();
        return;
    }
    if (siteView !== null || focusTag === "Earth" || focusTag === "Moon") {
        Report("instrument view: select a spacecraft first");
        return;
    }
    var model = fleetCraft[focusTag] && fleetCraft[focusTag].model ? fleetCraft[focusTag].model : "probe.glb";
    var cams = instrumentDefs[model] || [];
    if (cams.length === 0) {
        Report("instrument view: no camera in models/source/" + model);
        return;
    }
    alignCamera = false;
    instrumentView = { tag: focusTag, cam: cams[0] };
    Report("instrument view: " + cams[0].name + " on " + focusTag + ", " + cams[0].yfov_deg + " deg");
}

function ExitInstrumentView() {
    instrumentView = null;
    ShowFrame(false);
}

function CreateFrame() {
    MeshEntity.Create(null, SELECT_URL, [SELECT_URL], new Vector3(0, 0, 0), new Quaternion(0, 0, 0, 1),
        FRAME_ID, "OnFrameLoaded");
    var canvas = CanvasEntity.Create(null, new Vector3(0, 0, 0), new Quaternion(0, 0, 0, 1),
        new Vector3(1, 1, 1), false, null, "instrument-caption");
    canvas.SetVisibility(true);
    canvas.MakeWorldCanvas();
    canvas.SetSize(new Vector2(900, 60));
    var text = MakeText(canvas, "", 0, 0, 1, 1, new Color(1, 1, 1, 1), INFO_FONT);
    frameTag = { canvas: canvas, text: text };
    canvas.SetVisibility(false);
}

function OnFrameLoaded(frame) {
    frame.SetVisibility(false);
}

function ShowFrame(show) {
    var frame = Entity.Get(FRAME_ID);
    if (frame !== null && show !== frameShown) {
        frame.SetVisibility(show);
    }
    frameShown = show;
    if (frameTag !== null) {
        frameTag.canvas.SetVisibility(instrumentView !== null);
    }
}

// Every frame, after the spacecraft have moved and turned.
function UpdateInstrumentView() {
    if (instrumentView === null) {
        return;
    }
    var e = Entity.GetByTag(instrumentView.tag);
    if (e === null) {
        return;
    }
    var att = Attitude(instrumentView.tag);
    var q = { x: att.q[0], y: att.q[1], z: att.q[2], w: att.q[3] };
    var p = GetWorld(e), sc = e.GetScale().x, c = instrumentView.cam;
    var o = Rotate(q, c.pos[0] * sc, c.pos[1] * sc, c.pos[2] * sc);
    var f = Rotate(q, c.fwd[0], c.fwd[1], c.fwd[2]), u = Rotate(q, c.up[0], c.up[1], c.up[2]);
    var cam = new Vector3(p.x + o[0], p.y + o[1], p.z + o[2]);
    var rot = LookRotation(f, u);
    SetCameraWorld(cam);
    Camera.SetRotation(rot, false);
    // the instrument's field of view, framed at FRAME_DISTANCE
    var halfH = Math.tan(c.yfov_deg * Math.PI / 360) * FRAME_DISTANCE;
    var fits = c.yfov_deg / 2 < VIEW_HALF_FOV_DEG;
    ShowFrame(fits);
    var cq = Camera.GetRotation(false);
    var fw = Rotate(cq, 0, 0, 1), up = Rotate(cq, 0, 1, 0);
    var frame = Entity.Get(FRAME_ID);
    if (frame !== null && fits) {
        SetWorld(frame, new Vector3(cam.x + fw[0] * FRAME_DISTANCE, cam.y + fw[1] * FRAME_DISTANCE,
            cam.z + fw[2] * FRAME_DISTANCE));
        frame.SetRotation(cq, false);
        frame.SetScale(new Vector3(halfH * (c.aspect || 1), halfH, halfH), false);
    }
    if (frameTag !== null) {
        var kf = FRAME_DISTANCE / HUD_REF;        // the caption's layout is in view units at HUD_REF
        var s = HUD_WORLD_WIDTH / HUD_W * kf;
        var above = fits ? halfH + 0.012 * kf : (HUD_TOP_EDGE + 0.012) * kf;
        frameTag.canvas.SetScale(new Vector3(s, s, s), false);
        SetWorld(frameTag.canvas, new Vector3(cam.x + fw[0] * FRAME_DISTANCE + up[0] * above,
            cam.y + fw[1] * FRAME_DISTANCE + up[1] * above, cam.z + fw[2] * FRAME_DISTANCE + up[2] * above));
        frameTag.canvas.SetRotation(cq, false);
        frameTag.text.SetText("INSTRUMENT  " + c.name + " on " + (fleetNames[instrumentView.tag] || instrumentView.tag)
            + "   field of view " + c.yfov_deg.toFixed(1) + "°" + (fits ? " (frame)" : " (wider than this view)"));
    }
}

// ---- Link budgets (in Show info) ----
// The equations of tools/linkbudget.py (free space, clear sky), worked out live from the current
// range, radio by radio: a station radio (groundstations, Station radios: gain, noise temperature,
// Tx power) and a spacecraft radio (spacecraft.xlsx, Radios) make a link when their downlink
// frequencies are in the same band. Downlink at the spacecraft radio's downlink frequency:
// spacecraft EIRP, station G/T; uplink (2-way) at its uplink frequency: station EIRP, spacecraft
// G/T. Margin = Eb/N0 - the radio's required Eb/N0.
const LIGHT_SPEED = 299792458.0;
const BOLTZMANN_DB = -228.6;
// IEEE letter bands (MHz, lower edge), as linkbudget.BANDS
const BANDS = [[300, "UHF"], [1000, "L"], [2000, "S"], [4000, "C"], [8000, "X"], [12000, "Ku"], [18000, "K"],
    [27000, "Ka"], [40000, "V"], [75000, "W"], [110000, "mm"]];
var fleetRadios = {};            // tag -> [ { name, band, down_mhz, up_mhz, tx_w, gain_dbi, rate_bps, ... } ]

function Log10(x) {
    return Math.log(x) / Math.LN10;
}

function Have(v) {
    return v !== null && v !== undefined;
}

function BandOf(mhz) {
    if (!Have(mhz)) {
        return null;
    }
    var name = "VHF";
    for (var i = 0; i < BANDS.length; i++) {
        if (mhz >= BANDS[i][0]) {
            name = BANDS[i][1];
        }
    }
    return name;
}

// [{ st, sc, band }]: every station radio / spacecraft radio pair in the same downlink band (a
// spacecraft radio with no frequency -- the old Payloads sheet -- pairs with every station radio).
function RadioPairs(stationRadios, craftRadios) {
    var out = [];
    for (var i = 0; i < stationRadios.length; i++) {
        for (var k = 0; k < craftRadios.length; k++) {
            var sr = stationRadios[i], cr = craftRadios[k];
            var band = Have(cr.down_mhz) ? BandOf(cr.down_mhz) : BandOf(sr.down_mhz);
            if (band !== null && band === BandOf(sr.down_mhz)) {
                out.push({ st: sr, sc: cr, band: band });
            }
        }
    }
    return out;
}

function LinkBudget(rangeKm, st, sc, link) {
    var losses = sc.losses_db || 0;
    var out = { down: null, up: null };
    var fDown = Have(sc.down_mhz) ? sc.down_mhz : st.down_mhz;
    if (Have(fDown) && Have(sc.tx_w) && Have(sc.gain_dbi) && Have(sc.rate_bps) && Have(sc.ebn0_req_db)
        && Have(st.gain_dbi) && Have(st.tsys_k)) {
        var fspl = 20 * Log10(4 * Math.PI * rangeKm * 1e3 * fDown * 1e6 / LIGHT_SPEED);
        var eirp = 10 * Log10(sc.tx_w) + sc.gain_dbi;
        var gt = st.gain_dbi - 10 * Log10(st.tsys_k);
        var ebn0 = eirp - fspl - losses + gt - BOLTZMANN_DB - 10 * Log10(sc.rate_bps);
        out.down = { freq: fDown, fspl: fspl, ebn0: ebn0, margin: ebn0 - sc.ebn0_req_db };
    }
    var fUp = Have(sc.up_mhz) ? sc.up_mhz : (Have(st.up_mhz) ? st.up_mhz : fDown);
    if (link === "2-way" && Have(fUp) && Have(st.tx_w) && Have(st.gain_dbi) && Have(sc.gt_dbk)
        && Have(sc.up_rate_bps) && Have(sc.ebn0_req_db)) {
        var fsplUp = 20 * Log10(4 * Math.PI * rangeKm * 1e3 * fUp * 1e6 / LIGHT_SPEED);
        var eirpUp = 10 * Log10(st.tx_w) + st.gain_dbi;
        var ebn0Up = eirpUp - fsplUp - losses + sc.gt_dbk - BOLTZMANN_DB - 10 * Log10(sc.up_rate_bps);
        out.up = { freq: fUp, fspl: fsplUp, ebn0: ebn0Up, margin: ebn0Up - sc.ebn0_req_db };
    }
    return out;
}

function Signed(x) {
    return (x >= 0 ? "+" : "") + x.toFixed(1);
}

function Rate(bps) {
    return bps >= 1e6 ? (bps / 1e6).toFixed(2) + " Mbps" : (bps / 1000).toFixed(1) + " kbps";
}

// "S-band 2250 / up 2050 MHz" for a radio
function RadioWords(r) {
    return r.name + " " + r.down_mhz + (Have(r.up_mhz) ? " / up " + r.up_mhz : "") + " MHz";
}

// Lines for one station / spacecraft pair in contact (label = the other end's name): a budget
// for every radio pair in a shared band.
function BudgetLines(site, tag, label) {
    var craft = Entity.GetByTag(tag);
    if (craft === null) {
        return [];
    }
    var q = GetWorld(craft), p = site.trueWorld;
    var rangeKm = Math.sqrt((q.x - p.x) * (q.x - p.x) + (q.y - p.y) * (q.y - p.y) + (q.z - p.z) * (q.z - p.z))
        / KM_TO_WORLD_UNITS;
    var lines = [label + "   range " + rangeKm.toFixed(0) + " km"];
    var craftRadios = fleetRadios[tag] || [];
    if (craftRadios.length === 0) {
        return lines.concat(["   no radio for " + (fleetNames[tag] || tag) + " (spacecraft.xlsx, sheet Radios)"]);
    }
    var found = RadioPairs(site.radios, craftRadios);
    if (found.length === 0) {
        var bands = function (rs) {
            var b = [];
            for (var i = 0; i < rs.length; i++) {
                b.push(rs[i].band || "?");
            }
            return b.join(", ");
        };
        return lines.concat(["   no shared band (station: " + bands(site.radios) + "; spacecraft: " + bands(craftRadios) + ")"]);
    }
    for (var k = 0; k < found.length; k++) {
        var pr = found[k];
        var b = LinkBudget(rangeKm, pr.st, pr.sc, site.link);
        lines.push("   " + pr.band + "-band  " + pr.sc.name + " <> " + pr.st.name);
        if (b.down !== null) {
            lines.push("      down " + b.down.freq + " MHz " + Rate(pr.sc.rate_bps) + ": Eb/N0 " + b.down.ebn0.toFixed(1)
                + " dB, margin " + Signed(b.down.margin) + " dB" + (b.down.margin < 0 ? "  NOT CLOSING" : ""));
        } else {
            lines.push("      down: missing values (station gain / noise temp, or the radio's power, gain, rate)");
        }
        if (b.up !== null) {
            lines.push("      up " + b.up.freq + " MHz " + Rate(pr.sc.up_rate_bps) + ": Eb/N0 " + b.up.ebn0.toFixed(1)
                + " dB, margin " + Signed(b.up.margin) + " dB" + (b.up.margin < 0 ? "  NOT CLOSING" : ""));
        }
    }
    return lines;
}

// The budget section of Show info: a station's contacts, or a spacecraft's stations.
function BudgetSection(site, tag) {
    var lines = ["Link budget (live)"];
    if (site !== null) {
        for (var t in site.links) {
            if (linkShown[site.links[t]]) {
                lines = lines.concat(BudgetLines(site, t, fleetNames[t] || t));
            }
        }
    } else {
        var stations = layers.stations.sites;
        for (var i = 0; i < stations.length; i++) {
            var id = stations[i].links[tag];
            if (id && linkShown[id]) {
                lines = lines.concat(BudgetLines(stations[i], tag, stations[i].name));
            }
        }
    }
    if (lines.length === 1) {
        lines.push(site !== null ? "no spacecraft in the beam now" : "no ground station in contact now");
    }
    return lines;
}

// ---- Lunar terrain (tools/terrain.py) ----
// fleet.json "terrain": per Moon site its horizon mask (the terrain's elevation every az_step
// deg from north through east, from LOLA), the Sun's disc in view and the Earth's clearance over
// the window (60 s samples), and the next 30 / 365 days' figures. The mask also gates link
// lines, as the fleet job's passes are gated.
var fleetTerrain = {};           // site name -> results
var fleetTerrainCharts = {};     // site name -> "data/charts/terrain-<name>.png"

// A site's key in fleet.json's per-site results: a place and a station may share a name.
function SiteKey(site) {
    return (site.layer === "stations" ? "station:" : "place:") + site.name;
}

// The mask at an azimuth (deg), linear between its samples.
function HorizonAt(terrain, az) {
    var m = terrain.mask, n = m.length;
    var x = (((az % 360) + 360) % 360) / terrain.az_step;
    var i = Math.floor(x), f = x - i;
    return m[i % n] * (1 - f) + m[(i + 1) % n] * f;
}

// The window sample nearest now.
function TerrainNow(terrain) {
    var w = terrain.window;
    var i = Math.round((elapsedSeconds - w.t0) / w.step);
    i = Math.max(0, Math.min(w.sun.length - 1, i));
    return { sun: w.sun[i], sunEl: w.sun_el[i], earthClear: w.earth_clear[i] };
}

function Hours(h) {
    return h >= 48 ? (h / 24).toFixed(1) + " d" : h.toFixed(1) + " h";
}

function TerrainLines(site) {
    var tr = site.terrain, lines = [];
    var now = TerrainNow(tr);
    lines.push("Sunlight    " + (100 * now.sun).toFixed(0) + "% of the Sun's disc in view (Sun "
        + now.sunEl.toFixed(2) + "\u00b0 up)");
    lines.push("Earth       " + (now.earthClear > 0 ? "in sight, " + now.earthClear.toFixed(2) + "\u00b0 above the terrain"
        : "hidden, " + (-now.earthClear).toFixed(2) + "\u00b0 below the terrain"));
    var spans = [["next_30d", "Next 30 d "], ["next_365d", "Next 365 d"]];
    for (var k = 0; k < spans.length; k++) {
        var st = tr[spans[k][0]];
        lines.push(spans[k][1] + "  Sun " + st.sunlit_pct.toFixed(1) + "% (longest without " + Hours(st.longest_dark_h)
            + "), Earth " + st.earth_pct.toFixed(1) + "%");
    }
    lines.push("Terrain     LOLA " + tr.dem.split("_")[1] + " px/deg, horizon " + Math.min.apply(null, tr.mask).toFixed(1)
        + " to " + Math.max.apply(null, tr.mask).toFixed(1) + "\u00b0");
    return lines;
}

// ---- Panels with pictures ----
// The chart and plot panels show matplotlib PNGs. WebVerse's own picture loader (ImageEntity,
// and button / dropdown images) fails in this runtime once a world has been unloaded -- its
// helper's instance is cleared on unload and never set again (EntityAPIHelper.ClearEntityMapping),
// so every load throws before downloading -- and WebVerse always opens its home world first.
// Models still load, so each picture comes as a .glb beside its PNG (tools/charts.py
// picture_glb: a unit quad textured with it, unlit, blended so it casts no shadow), placed on its
// panel every frame, turned with the camera and a hair nearer than the panel. Pictures that load
// after their panel closed are removed when they arrive.
const PX_UNITS = HUD_W / (HUD_WORLD_WIDTH * 1459);  // canvas units per view pixel (at the calibration)
const PICTURE_LIFT = 0.002;      // world units nearer the camera than the panel
var pictureSerial = 0;
var doomedPictures = [];         // ids of pictures whose panel closed while they were loading

function NewPanel(tagName, w, h) {
    var canvas = CanvasEntity.Create(null, new Vector3(0, 0, 0), new Quaternion(0, 0, 0, 1),
        new Vector3(1, 1, 1), false, null, tagName);
    canvas.SetVisibility(true);
    canvas.MakeWorldCanvas();
    canvas.SetSize(new Vector2(w, h));
    return { canvas: canvas, parts: [], pictures: [], w: w, height: h };
}

// A picture (models' .glb path under DATA_BASE_URL) at x, y, w, h canvas units from the panel's
// top-left corner.
function PanelPicture(panel, glb, x, y, w, h) {
    pictureSerial++;
    var id = "b0e5a0f0-0000-4000-a000-" + ("000000000000" + pictureSerial.toString(16)).slice(-12);
    var url = DATA_BASE_URL + glb;
    MeshEntity.Create(null, url, [url], new Vector3(0, 0, 0), new Quaternion(0, 0, 0, 1), id, "OnPanelPictureLoaded");
    panel.pictures.push({ id: id, x: x, y: y, w: w, h: h, shown: false });
}

function OnPanelPictureLoaded(entity) {
    // placed (and shown) by PlacePanel on the next frame; nothing to do here
}

// Put a panel's canvas and its pictures in front of the camera: right / up = the panel's centre
// in view units at HUD_REF.
function PlacePanel(panel, right, up) {
    var s = HUD_WORLD_WIDTH / HUD_W;
    var q = Camera.GetRotation(false);
    var p = CameraWorld();
    var f = Rotate(q, 0, 0, 1), r = Rotate(q, 1, 0, 0), u = Rotate(q, 0, 1, 0);
    var at = function (dx, dy, lift) {
        return new Vector3(
            p.x + f[0] * (HUD_DISTANCE - lift) + (r[0] * dx + u[0] * dy) * VIEW_K,
            p.y + f[1] * (HUD_DISTANCE - lift) + (r[1] * dx + u[1] * dy) * VIEW_K,
            p.z + f[2] * (HUD_DISTANCE - lift) + (r[2] * dx + u[2] * dy) * VIEW_K);
    };
    panel.canvas.SetScale(new Vector3(s * VIEW_K, s * VIEW_K, s * VIEW_K), false);
    SetWorld(panel.canvas, at(right, up, 0));
    panel.canvas.SetRotation(q, false);
    for (var i = 0; i < panel.pictures.length; i++) {
        var pic = panel.pictures[i];
        var e = Entity.Get(pic.id);
        if (e === null) {
            continue;                     // still loading
        }
        var dx = right + (pic.x + pic.w / 2 - panel.w / 2) * s;
        var dy = up + (panel.height / 2 - pic.y - pic.h / 2) * s;
        SetWorld(e, at(dx, dy, PICTURE_LIFT));
        e.SetRotation(q, false);
        e.SetScale(new Vector3(pic.w * s * VIEW_K, pic.h * s * VIEW_K, 1), false);
        if (!pic.shown) {
            e.SetVisibility(true);
            pic.shown = true;
        }
    }
}

function DeletePanel(panel) {
    for (var i = 0; i < panel.parts.length; i++) {
        panel.parts[i].Delete();
    }
    panel.canvas.Delete();
    for (var k = 0; k < panel.pictures.length; k++) {
        var e = Entity.Get(panel.pictures[k].id);
        if (e !== null) {
            e.Delete();
        } else {
            doomedPictures.push(panel.pictures[k].id);
        }
    }
}

function SweepPictures() {
    for (var i = doomedPictures.length - 1; i >= 0; i--) {
        var e = Entity.Get(doomedPictures[i]);
        if (e !== null) {
            e.Delete();
            doomedPictures.splice(i, 1);
        }
    }
}

function PictureGlb(png) {
    return png.replace(/\.png$/, ".glb");
}

// ---- Chart panel (with Show info) ----
// While Show info is open on a ground station or a Moon site, its chart (tools/charts.py: a
// station's contacts, elevation and margin per pass; a Moon site's terrain chart) shows in a
// panel along the bottom, left of the Assets panel. A Moon ground station has both, with a
// Passes / Terrain switch. "::" grip to move it, like the other panels.
const CHART_W = Math.round(460 * PX_UNITS);   // the chart's 4.6 x 5.6 in at 100 px per inch
const CHART_H = Math.round(560 * PX_UNITS);
const CHART_HEAD = 50;           // the grip row above the picture
const CHART_BOTTOM = -0.335;     // view units at HUD_REF: the panel's home bottom edge
const CHART_RIGHT = HUD_RIGHT_EDGE - HUD_WORLD_WIDTH - 0.01;   // home right edge: left of the Assets panel
var fleetCharts = {};            // station name -> "data/charts/<name>.png"
var chartPanel = null;           // { canvas, parts, pictures, grip, file, w, height, status }
var chartTab = "passes";         // a Moon ground station has both: "passes" or "terrain"

function ChartTab(tab) {
    chartTab = tab;
}

function UpdateChartPanel() {
    var want = null;
    if (infoOn && siteView !== null) {
        var name = siteView.site.name;
        var passes = siteView.site.link ? (fleetCharts[name] || null) : null;
        var land = fleetTerrainCharts[SiteKey(siteView.site)] || null;
        want = passes !== null && land !== null ? (chartTab === "terrain" ? land : passes) : (passes || land);
    }
    if (chartPanel !== null && chartPanel.file !== want) {
        DeletePanel(chartPanel);
        chartPanel = null;
    }
    if (chartPanel === null && want !== null) {
        CreateChartPanel(want);
    }
    if (chartPanel !== null) {
        var s = HUD_WORLD_WIDTH / HUD_W;
        PlacePanel(chartPanel, CHART_RIGHT + panelOffset.chart.x - CHART_W * s / 2,
            CHART_BOTTOM + panelOffset.chart.y + chartPanel.height * s / 2);
    }
}

function CreateChartPanel(file) {
    var height = CHART_HEAD + CHART_H;
    chartPanel = NewPanel("chart-panel", CHART_W, height);
    var canvas = chartPanel.canvas;
    chartPanel.file = file;
    var head = CHART_HEAD / height;
    chartPanel.parts.push(MakeRowButton(canvas, "", 0, 0, 1, 1, PANEL_COLOR));
    chartPanel.grip = MakeText(canvas, "::", 0.01, 0, 0.06, head, panelMove === "chart" ? GRIP_ARMED : GRIP_COLOR, INFO_FONT);
    chartPanel.parts.push(chartPanel.grip);
    chartPanel.parts.push(MakeRowButton(canvas, "StartMove('chart');", 0.01, 0, 0.06, head, ROW_COLOR));
    var name = siteView !== null ? siteView.site.name : "";
    var both = siteView !== null && siteView.site.link && fleetCharts[name] && fleetTerrainCharts[SiteKey(siteView.site)];
    if (both) {
        var tabs = [["passes", "Passes"], ["terrain", "Terrain"]];
        for (var k = 0; k < tabs.length; k++) {
            var x = 0.09 + k * 0.17;
            chartPanel.parts.push(MakeText(canvas, tabs[k][1], x, 0, 0.16, head, new Color(1, 1, 1, 1), INFO_FONT));
            chartPanel.parts.push(MakeRowButton(canvas, "ChartTab('" + tabs[k][0] + "');", x, 0, 0.16, head,
                chartTab === tabs[k][0] ? ROW_SELECTED : ROW_COLOR));
        }
    }
    PanelPicture(chartPanel, PictureGlb(file), 0, CHART_HEAD, CHART_W, CHART_H);
    // the status text starts after the Passes / Terrain switch when there is one
    AddSaveButton(chartPanel, "chart", both ? 0.45 : 0.09);
    Report("chart: " + file);
}

function OverChartPanel(sx, sy) {
    if (chartPanel === null) {
        return false;
    }
    var s = HUD_WORLD_WIDTH / HUD_W;
    var x = sx * HUD_REF - panelOffset.chart.x, y = sy * HUD_REF - panelOffset.chart.y;
    return x <= CHART_RIGHT && x >= CHART_RIGHT - CHART_W * s && y >= CHART_BOTTOM
        && y <= CHART_BOTTOM + chartPanel.height * s;
}

// ---- Plot panel (under the info panel) ----
// With Show info open on a Moon site, a ground station or a spacecraft, its XY plot of the Sun,
// direct-to-Earth and link times over the run's window (tools/charts.py timeline_chart, fleet.json
// "timelines") hangs under the info panel and moves with it ("::" grip to move it on its own).
const PLOT_W = Math.round(500 * PX_UNITS);    // the plot's 5.0 x 4.0 in at 100 px per inch
const PLOT_H = Math.round(400 * PX_UNITS);
const PLOT_GAP = 0.004;          // view units between the info panel and the plot panel
var fleetTimelines = {};         // site name or "craft:<tag>" -> "data/charts/timeline-<name>.png"
var plotPanel = null;            // { canvas, parts, pictures, grip, file, w, height, status }

// The span (box next to the save icon): 13 h is the fleet run's own plot (fleet.json
// "timelines"); the longer ones are drawn on request by the local server (/api/plot,
// tools/plots.py) and kept per site and span until the next fleet run. While one is being
// drawn, the panel keeps the plot it shows and says so.
const PLOT_SPANS = [["window", "13 h"], ["week", "Week"], ["month", "Month"], ["6mo", "6 mo"], ["year", "Year"]];
var plotSpan = "window";
var plotSpanOpen = false;          // the span list is showing in the panel's head row
var spanFiles = {};                // "<key>|<span>" -> "data/charts/span-....png"
var spanPending = null;            // the "<key>|<span>" being drawn by the server
var spanFailed = {};               // "<key>|<span>" -> why it failed
var spanRetryAt = {};              // "<key>|<span>" -> the frame to ask again (the server is working on it)
const SPAN_RETRY_FRAMES = 120;     // ~2 s

function PlotKey() {
    if (!infoOn || infoMode !== "info" || info === null) {
        return null;
    }
    if (siteView !== null) {
        return SiteKey(siteView.site);
    }
    return focusTag !== "Earth" && focusTag !== "Moon" ? "craft:" + focusTag : null;
}

function SpanName(span) {
    for (var i = 0; i < PLOT_SPANS.length; i++) {
        if (PLOT_SPANS[i][0] === span) {
            return PLOT_SPANS[i][1];
        }
    }
    return span;
}

function ToggleSpanList() {
    plotSpanOpen = !plotSpanOpen;
    ShowSpanList();
}

function ShowSpanList() {
    if (plotPanel === null) {
        return;
    }
    for (var i = 0; i < plotPanel.spanParts.length; i++) {
        plotPanel.spanParts[i].SetVisibility(plotSpanOpen);
    }
    plotPanel.status.SetVisibility(!plotSpanOpen);
}

function ChooseSpan(span) {
    plotSpan = span;
    plotSpanOpen = false;
    ShowSpanList();
    Report("plot span: " + span);
}

function OnSpanPlot(body) {
    var done = spanPending;
    spanPending = null;
    var result = null;
    try {
        result = JSON.parse(body);
    } catch (e) {
    }
    if (result !== null && result.ok) {
        spanFiles[done] = result.file;
    } else if (result !== null && result.pending) {
        spanRetryAt[done] = infoFrame + SPAN_RETRY_FRAMES;   // being drawn: ask again shortly
    } else {
        spanFailed[done] = result !== null ? result.error : "no answer (is tools/serve.py running, and restarted?)";
        Report("plot " + done + " failed: " + spanFailed[done]);
    }
}

function UpdatePlotPanel() {
    SweepPictures();
    var key = PlotKey();
    var want = null, waiting = null;
    if (key !== null && fleetTimelines[key]) {
        if (plotSpan === "window") {
            want = fleetTimelines[key];
        } else {
            var k = key + "|" + plotSpan;
            if (spanFiles[k]) {
                want = spanFiles[k];
            } else {
                // keep what is showing for this site meanwhile; ask the server once
                want = plotPanel !== null && plotPanel.key === key ? plotPanel.file : fleetTimelines[key];
                if (spanFailed[k]) {
                    waiting = SpanName(plotSpan) + ": " + spanFailed[k];
                } else {
                    // the first plot of a span may need a GMAT propagation (tools/longrun.py)
                    waiting = "Drawing " + SpanName(plotSpan).toLowerCase() + "... (GMAT, first time "
                        + (plotSpan === "6mo" || plotSpan === "year" ? "~2.5 min" : "~30 s") + ")";
                    if (spanPending === null && infoFrame >= (spanRetryAt[k] || 0)) {
                        spanPending = k;
                        HTTPNetworking.Fetch(DATA_BASE_URL + "api/plot?key=" + encodeURIComponent(key) + "&span="
                            + plotSpan, "OnSpanPlot");
                    }
                }
            }
        }
    }
    if (plotPanel !== null && plotPanel.file !== want) {
        DeletePanel(plotPanel);
        plotPanel = null;
    }
    if (plotPanel === null && want !== null) {
        CreatePlotPanel(want);
        plotPanel.key = key;
    }
    if (plotPanel !== null) {
        var note = waiting !== null ? waiting : "";
        if (note !== plotPanel.note) {           // (a "Saved ..." message stays until this changes)
            plotPanel.status.SetText(note);
            plotPanel.note = note;
        }
        var s = HUD_WORLD_WIDTH / HUD_W;
        PlacePanel(plotPanel, -HUD_RIGHT_EDGE + panelOffset.info.x + panelOffset.plot.x + PLOT_W * s / 2,
            PlotPanelTop() + panelOffset.plot.y - plotPanel.height * s / 2);
    }
}

function CreatePlotPanel(file) {
    var height = CHART_HEAD + PLOT_H;
    plotPanel = NewPanel("plot-panel", PLOT_W, height);
    var canvas = plotPanel.canvas;
    plotPanel.file = file;
    var head = CHART_HEAD / height;
    plotPanel.parts.push(MakeRowButton(canvas, "", 0, 0, 1, 1, PANEL_COLOR));
    plotPanel.grip = MakeText(canvas, "::", 0.01, 0, 0.06, head, panelMove === "plot" ? GRIP_ARMED : GRIP_COLOR, INFO_FONT);
    plotPanel.parts.push(plotPanel.grip);
    plotPanel.parts.push(MakeRowButton(canvas, "StartMove('plot');", 0.01, 0, 0.06, head, ROW_COLOR));
    PanelPicture(plotPanel, PictureGlb(file), 0, CHART_HEAD, PLOT_W, PLOT_H);
    // the span box, left of the save icon, and its list (hidden until the box is clicked)
    var boxW = 0.11, boxX = 1 - (50 + 10) / PLOT_W - boxW - 0.01;
    AddSaveButton(plotPanel, "plot", 0.09, boxX - 0.01);
    plotPanel.note = "";
    plotPanel.parts.push(MakeText(canvas, SpanName(plotSpan) + " v", boxX, 0, boxW, head, new Color(1, 1, 1, 1), 24));
    plotPanel.parts.push(MakeRowButton(canvas, "ToggleSpanList();", boxX, 0, boxW, head, ROW_SELECTED));
    plotPanel.spanParts = [];
    var optW = 0.105, optX = boxX - PLOT_SPANS.length * optW - 0.01;
    for (var i = 0; i < PLOT_SPANS.length; i++) {
        var x = optX + i * optW;
        var t = MakeText(canvas, PLOT_SPANS[i][1], x, 0, optW - 0.005, head,
            PLOT_SPANS[i][0] === plotSpan ? new Color(1, 0.85, 0.3, 1) : new Color(1, 1, 1, 1), 24);
        var b = MakeRowButton(canvas, "ChooseSpan('" + PLOT_SPANS[i][0] + "');", x, 0, optW - 0.005, head, ROW_COLOR);
        plotPanel.parts.push(t);
        plotPanel.parts.push(b);
        plotPanel.spanParts.push(t);
        plotPanel.spanParts.push(b);
    }
    ShowSpanList();
    Report("plot: " + file);
}

// Top edge (view units at HUD_REF, before the panel's own offset): just under the info panel.
function PlotPanelTop() {
    var s = HUD_WORLD_WIDTH / HUD_W;
    return HUD_TOP_EDGE + panelOffset.info.y - info.height * s - PLOT_GAP;
}

function OverPlotPanel(sx, sy) {
    if (plotPanel === null || info === null) {
        return false;
    }
    var s = HUD_WORLD_WIDTH / HUD_W;
    var x = sx * HUD_REF, y = sy * HUD_REF;
    var left = -HUD_RIGHT_EDGE + panelOffset.info.x + panelOffset.plot.x;
    var top = PlotPanelTop() + panelOffset.plot.y;
    return x >= left && x <= left + PLOT_W * s && y <= top && y >= top - plotPanel.height * s;
}

// ---- Saving plots ----
// The save icon (a picture, models/save_icon.glb) on the plot and chart panels: the local
// server (tools/serve.py, /api/save) copies the shown PNG and the SVG beside it into exports/
// with the time saved; the panel's head row says where they went.
const SAVE_ICON_GLB = "models/save_icon.glb";
var saveFrom = null;             // "plot" or "chart" while a save is on its way

function AddSaveButton(panel, key, left, right) {
    var size = 40, pad = 5;
    var x = panel.w - size - 2 * pad;
    PanelPicture(panel, SAVE_ICON_GLB, x + pad, pad, size, size);
    panel.parts.push(MakeRowButton(panel.canvas, "SavePlot('" + key + "');", x / panel.w, 0, (size + 2 * pad) / panel.w,
        CHART_HEAD / panel.height, ROW_COLOR));
    var end = right !== undefined ? right : x / panel.w - 0.01;     // the status text's right edge
    panel.status = MakeText(panel.canvas, "", left, 0, end - left, CHART_HEAD / panel.height,
        new Color(0.6, 1, 0.75, 1), 22);
    panel.parts.push(panel.status);
}

function SavePlot(key) {
    var panel = key === "plot" ? plotPanel : chartPanel;
    if (panel === null || saveFrom !== null) {
        return;
    }
    saveFrom = key;
    panel.status.SetText("Saving...");
    HTTPNetworking.Fetch(DATA_BASE_URL + "api/save?file=" + panel.file, "OnPlotSaved");
    Report("save: " + panel.file);
}

function OnPlotSaved(body) {
    var panel = saveFrom === "plot" ? plotPanel : chartPanel;
    saveFrom = null;
    var result = null;
    try {
        result = JSON.parse(body);
    } catch (e) {
    }
    var words = result !== null && result.ok && result.saved.length > 0
        ? "Saved " + result.saved[0].replace(".png", "") + " (.png, .svg)"
        : "Save failed (is tools/serve.py running?)";
    if (panel !== null) {
        panel.status.SetText(words);
    }
    Report("save: " + words);
}

// ---- Passes (in Show info) ----
// fleet.json "passes" (tools/run_fleet.py): each spacecraft's passes through each ground
// station's beam over the run's window. The info panel lists the next ones still to end: for a
// selected station its passes, for a spacecraft its passes over every station, otherwise all.
var fleetPasses = [];

// The next passes still to end, as lines for the info panel: by "craft" for a station (named
// key), by "station" for a spacecraft (tag key), "both" for all.
function PassLines(by, key, max) {
    var list = [], lines = [];
    for (var i = 0; i < fleetPasses.length; i++) {
        var p = fleetPasses[i];
        if (p.los < elapsedSeconds) {
            continue;
        }
        if ((by === "craft" && p.station !== key) || (by === "station" && p.catalog !== key)) {
            continue;
        }
        list.push(p);
    }
    lines.push("Next passes (AOS - LOS UTC, duration, max el)");
    if (list.length === 0) {
        lines.push("none left in this run's window (Update TLEs starts a new one)");
    }
    for (var k = 0; k < list.length && k < max; k++) {
        var q = list[k];
        var who = by === "craft" ? q.name : (by === "station" ? q.station : q.name + " / " + q.station);
        var dur = q.los - q.aos;
        var now = (q.aos_by === "terrain" || q.los_by === "terrain" ? "   terrain" : "")
            + (q.aos <= elapsedSeconds ? "   NOW" : "");
        lines.push(who + "   " + UtcString(q.aos).slice(12, 20) + " - " + UtcString(q.los).slice(12, 20)
            + "   " + Math.floor(dur / 60) + "m" + ("0" + Math.round(dur % 60)).slice(-2) + "s   "
            + q.max_el.toFixed(1) + "\u00b0" + now);
    }
    if (list.length > max) {
        lines.push("... " + (list.length - max) + " more");
    }
    return lines;
}

// ---- Orbit lines ----
// tools/run_fleet.py writes, per spacecraft, one-period orbit lines centred every half period
// across the window (data/tracks/<catalog>_<run>_<n>.glb, unlit glTF line strips in world
// units). Orbits precess (the ISS's by ~5 deg/day), so one line drifts off its spacecraft
// within hours; showing the segment centred nearest the current time keeps each spacecraft in
// the middle of its drawn orbit. The Assets panel checkbox (or the O key) shows / hides them.
// Each segment is created with a known id so it can be found again with Entity.Get.
const DATA_BASE_URL = "http://localhost:8000/";
var orbitSegments = {};       // tag -> [ { id, t } ]
var orbitActive = {};         // tag -> index of the segment shown
var orbitOn = {};             // tag -> true when that spacecraft's orbit line is shown
var orbitIdCounter = 0;
var orbitLinesLoaded = 0;

function OrbitBox(tag) {
    return orbitOn[tag] ? "[x]" : "[ ]";
}

function NextOrbitId() {
    orbitIdCounter++;
    var hex = orbitIdCounter.toString(16);
    while (hex.length < 12) {
        hex = "0" + hex;
    }
    return "b0e5a000-0000-4000-8000-" + hex;
}

function CreateOrbitLines(tracks) {
    // remove the previous set (after an update)
    for (var oldTag in orbitSegments) {
        for (var i = 0; i < orbitSegments[oldTag].length; i++) {
            var old = Entity.Get(orbitSegments[oldTag][i].id);
            if (old !== null) {
                old.Delete();
            }
        }
    }
    orbitSegments = {};
    orbitActive = {};
    orbitLinesLoaded = 0;
    for (var tag in tracks) {
        orbitSegments[tag] = [];
        var segs = tracks[tag].segments;
        for (var k = 0; k < segs.length; k++) {
            var id = NextOrbitId();
            var url = DATA_BASE_URL + segs[k].file;
            orbitSegments[tag].push({ id: id, t: segs[k].t, moon: tracks[tag].origin === "Moon" });
            MeshEntity.Create(null, url, [url], ToUnity(new Vector3(0, 0, 0)), new Quaternion(0, 0, 0, 1),
                id, "OnOrbitLineLoaded");
        }
    }
}

function OnOrbitLineLoaded(line) {
    orbitLinesLoaded++;
    ShowOrbitLines();          // set every loaded segment's visibility, including this one
}

// Index of the segment centred nearest time t.
function NearestSegment(segs, t) {
    var best = 0;
    for (var k = 1; k < segs.length; k++) {
        if (Math.abs(segs[k].t - t) < Math.abs(segs[best].t - t)) {
            best = k;
        }
    }
    return best;
}

function ShowOrbitLines() {
    for (var tag in orbitSegments) {
        var segs = orbitSegments[tag];
        var active = NearestSegment(segs, elapsedSeconds);
        orbitActive[tag] = active;
        for (var k = 0; k < segs.length; k++) {
            var line = Entity.Get(segs[k].id);
            if (line !== null) {
                line.SetVisibility(orbitOn[tag] === true && k === active);
            }
        }
    }
}

// Called every playback tick: switch segments only when "now" moves past a half period.
function UpdateOrbitLines() {
    // a Moon orbiter's lines are Moon-centred: keep the one showing on the Moon
    for (var mt in orbitSegments) {
        var shown = orbitSegments[mt][orbitActive[mt]];
        if (shown && shown.moon && moonNow !== null) {
            var line = Entity.Get(shown.id);
            if (line !== null) {
                SetWorld(line, moonNow.world);
            }
        }
    }
    for (var tag in orbitSegments) {
        if (NearestSegment(orbitSegments[tag], elapsedSeconds) !== orbitActive[tag]) {
            ShowOrbitLines();
            return;
        }
    }
}

// One spacecraft's orbit line on/off (its [x] box in the Assets panel).
function ToggleOrbit(tag) {
    orbitOn[tag] = !orbitOn[tag];
    ShowOrbitLines();
    RefreshOrbitBoxes();
    Report("orbit line " + tag + " " + (orbitOn[tag] ? "on" : "off") + " (" + orbitLinesLoaded + " loaded)");
}

// O key: all on if any is off, otherwise all off.
function ToggleOrbits() {
    var allOn = true;
    for (var n = 0; n < fleetTags.length; n++) {
        allOn = allOn && orbitOn[fleetTags[n]] === true;
    }
    for (var m = 0; m < fleetTags.length; m++) {
        orbitOn[fleetTags[m]] = !allOn;
    }
    ShowOrbitLines();
    RefreshOrbitBoxes();
    Report("orbit lines all " + (!allOn ? "on" : "off"));
}

function RefreshOrbitBoxes() {
    RefreshHudBoxes();
}

// ---- Update TLEs ----
// Asks tools/serve.py to refresh the TLEs from CelesTrak and rerun the fleet job (GMAT
// headless), then reloads data/fleet.json in place. Takes a few seconds to a minute.
var updating = false;
var updateStatus = "Update TLEs";

function SetUpdateStatus(text) {
    updateStatus = text;
    if (hud !== null) {
        hud.updateText.SetText(text);
    }
}

function RequestUpdate() {
    if (updating) {
        return;
    }
    updating = true;
    SetUpdateStatus("Updating...");
    Report("update: requested");
    HTTPNetworking.Fetch(DATA_BASE_URL + "api/update", "OnUpdateDone");
}

function OnUpdateDone(body) {
    var result = null;
    try {
        result = JSON.parse(body);
    } catch (e) {
    }
    if (result === null || !result.ok) {
        updating = false;
        SetUpdateStatus("Update failed");
        Report("update: failed " + (result ? result.output.slice(-300) : "(no response)"));
        return;
    }
    Report("update: done, reloading fleet");
    HTTPNetworking.Fetch(EPHEMERIS_URL, "OnFleetReloaded");
}

function OnFleetReloaded(body) {
    updating = false;
    if (!body) {
        SetUpdateStatus("Update failed");
        Report("update: fleet reload failed");
        return;
    }
    var parsed = JSON.parse(body);
    ApplyFleet(parsed);
    SetUpdateStatus("Updated " + parsed.generated.slice(12, 17) + " UTC");
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
// Place labels sit centred just ABOVE their dot and ground-station labels just BELOW theirs,
// "above" meaning up on screen (the camera's up axis), so the two never overlap: the label's
// near edge is this far from the dot's edge (fraction of camera distance).
const SITE_LABEL_GAP_PER_DISTANCE = 0.004;
// Labels are drawn this far from the camera (see UpdateLabels): just behind the panels
// (HUD_DISTANCE 0.35), so they never cover them, and in front of everything else.
const LABEL_DRAW_DISTANCE = 0.40;
var labels = {};                // key -> { canvas, text, site, lines }: a spacecraft tag, or "site-<id>"
var pendingLabel = null;        // onLoaded callbacks fire synchronously inside Create()

function CreateLabels() {
    for (var n = 0; n < fleetTags.length; n++) {
        CreateLabel(fleetTags[n], fleetNames[fleetTags[n]] || fleetTags[n], new Color(1, 1, 1, 1), null);
    }
}

// site: null for a spacecraft (placed from its entity), otherwise the place or station it names;
// lines: 1, or 2 for a two-line label
function CreateLabel(key, words, color, site, lines) {
    pendingLabel = { key: key, words: words, color: color, site: site, lines: lines || 1 };
    CanvasEntity.Create(null, new Vector3(0, 0, 0), new Quaternion(0, 0, 0, 1), new Vector3(1, 1, 1),
        false, null, "label-" + key, "OnLabelCanvasLoaded");
}

function OnLabelCanvasLoaded(canvas) {
    var key = pendingLabel.key;
    canvas.SetVisibility(true);
    canvas.MakeWorldCanvas();
    canvas.SetSize(new Vector2(LABEL_W, LABEL_H * pendingLabel.lines));
    labels[key] = { canvas: canvas, text: null, site: pendingLabel.site, lines: pendingLabel.lines, shown: true };
    TextEntity.Create(canvas, pendingLabel.words, LABEL_FONT, new Vector2(0, 0), new Vector2(1, 1),
        null, "label-text-" + key, "OnLabelTextLoaded");
}

function OnLabelTextLoaded(text) {
    var key = pendingLabel.key;
    text.SetVisibility(true);
    text.SetColor(pendingLabel.color);
    text.SetTextAlignment(TextAlignment.Center);
    labels[key].text = text;
    var size = labels[key].canvas.GetSize();
    Report("label: " + key + " (" + pendingLabel.words + ") canvas " + size.x + "x" + size.y);
}

function UpdateLabels() {
    var cam = CameraWorld();
    var camRot = Camera.GetRotation(false);
    var up = Rotate(camRot, 0, 1, 0);          // screen up, in world space
    for (var tag in labels) {
        var p;
        if (labels[tag].site !== null) {
            p = labels[tag].site.world;
        } else {
            var entity = Entity.GetByTag(tag);
            if (entity === null) {
                continue;
            }
            p = GetWorld(entity);
        }
        var dx = p.x - cam.x, dy = p.y - cam.y, dz = p.z - cam.z;
        var dist = Math.sqrt(dx * dx + dy * dy + dz * dz);
        var scale = LABEL_WIDTH_PER_DISTANCE * dist / LABEL_W;
        var canvas = labels[tag].canvas;
        var site = labels[tag].site;
        // Hidden while its object is behind the Earth (the label itself is drawn in front of
        // everything, so the Earth no longer hides it), or while its layer is switched off.
        var show = !BehindBodies(cam, p) && (site === null || layers[site.layer].on);
        if (show !== labels[tag].shown) {
            canvas.SetVisibility(show);
            labels[tag].shown = show;
        }
        if (!show) {
            continue;
        }
        var lx = p.x, ly = p.y, lz = p.z;
        if (site !== null) {
            // centre offset = dot radius + gap + half the label's height, up for places, down for stations
            var half = 0.5 * LABEL_WIDTH_PER_DISTANCE * LABEL_H * labels[tag].lines / LABEL_W;
            var off = (SITE_SIZE_PER_DISTANCE + SITE_LABEL_GAP_PER_DISTANCE + half) * dist * (site.below ? -1 : 1);
            lx += up[0] * off; ly += up[1] * off; lz += up[2] * off;
        } else {
            ly += LABEL_RAISE_PER_DISTANCE * dist;
        }
        // Slide the label along the line of sight to LABEL_DRAW_DISTANCE from the camera and
        // shrink it by the same ratio: identical on screen, but in front of the Earth and the
        // atmosphere shells, which used to cut through labels near the surface.
        var ex = lx - cam.x, ey = ly - cam.y, ez = lz - cam.z;
        var k = LABEL_DRAW_DISTANCE / Math.sqrt(ex * ex + ey * ey + ez * ez);
        SetWorld(canvas, new Vector3(cam.x + ex * k, cam.y + ey * k, cam.z + ez * k));
        canvas.SetRotation(camRot, false);
        canvas.SetScale(new Vector3(scale * k, scale * k, scale * k), false);
    }
}


// ---- The Moon ----
// models/moon.glb: a unit sphere with NASA's LRO LROC colour map, laid out like earth.glb,
// raised to LOLA terrain at true scale (+-10 km) with a matching normal map
// (local axes = the Moon-fixed axes in the viewer's (X, Z, Y) order), scaled to the mean lunar
// radius. fleet.json "moon" gives it every 60 s from GMAT: its Earth-centred EarthMJ2000Eq
// position and velocity (km, km/s; Hermite-interpolated here, as the spacecraft are) and q, its
// rotation in the viewer's axes (Moon-fixed -> world, including libration; interpolated
// linearly and renormalised -- it turns ~0.009 deg a minute). Lit by the same directional sun,
// so it shows its phase. The Assets panel's Moon row centres the view on it.
// Resolution levels (tools/make_moon.py): 1x 512 x 256 vertices (models/moon.glb), 2x and 4x
// (moon_2x.glb, moon_4x.glb; built locally, too big to commit). fleet.json moon.levels lists the
// ones built. The "1x v" box on the Assets panel's Moon row picks one: the new model loads under
// its own id and the old one is removed only once it is in, so the Moon never disappears; lunar
// sites move to the terrain that level draws (ground_m_levels). Each level is tiles of the same
// lat/lon grid with two UV maps (equatorial and polar), see make_moon.py.
const MOON_LEVELS = [1, 2, 4];
const MOON_LEVEL_IDS = { 1: "b0e5a000-0000-4000-a000-000000000003", 2: "b0e5a000-0000-4000-a000-000000000013",
    4: "b0e5a000-0000-4000-a000-000000000023" };
const MOON_LEVEL_TRIANGLES = { 1: "262k", 2: "1.0M", 4: "4.2M" };
var moonBuilt = { "1": "models/moon.glb" };   // from fleet.json moon.levels once it is loaded
var moonLevel = 1;                            // the level shown
var moonLoading = null;                       // the level being loaded, or null
var moonResOpen = false;                      // the level list is open in the Assets panel
const MOON_RADIUS_KM = 1737.4;
const MOON_RADIUS = MOON_RADIUS_KM * KM_TO_WORLD_UNITS;
const MOON_FRAMING = { start: 4 * MOON_RADIUS, min: 1.05 * MOON_RADIUS, max: 60 * MOON_RADIUS };
var moonSamples = [];           // [ [t, x, y, z, vx, vy, vz, qx, qy, qz, qw], ... ]
var moonNow = null;             // { km: [x, y, z], kmVel, world: Vector3, q: { x, y, z, w } } (GMAT km / Unity)

function CreateMoon() {
    LoadMoonLevel(1);
}

function LoadMoonLevel(level) {
    moonLoading = level;
    var url = DATA_BASE_URL + moonBuilt[String(level)];
    MeshEntity.Create(null, url, [url], new Vector3(0, 0, 0), new Quaternion(0, 0, 0, 1),
        MOON_LEVEL_IDS[level], "OnMoonLoaded");
}

function OnMoonLoaded(moon) {
    var level = moonLoading;
    moonLoading = null;
    var old = level !== moonLevel ? Entity.Get(MOON_LEVEL_IDS[moonLevel]) : null;
    if (old !== null) {
        old.tag = "";
    }
    moon.tag = "Moon";              // so SetFocus / Entity.GetByTag find it like the spacecraft
    moon.SetScale(new Vector3(MOON_RADIUS, MOON_RADIUS, MOON_RADIUS), false);
    moonLevel = level;
    UpdateMoon();
    moon.SetVisibility(true);
    if (old !== null) {
        if (focusEntity === old) {
            focusEntity = moon;   // the camera was following the Moon: follow the new model
        }
        old.Delete();
    }
    ApplyMoonGround();
    hudDirty = true;
    StartFpsWatch();
    Report("moon: " + level + "x loaded");
}

// The level box's words: "2x v", or "4x ..." while that level loads.
function MoonResLabel() {
    return moonLoading !== null && moonLoading !== moonLevel ? moonLoading + "x ..." : moonLevel + "x v";
}

function ToggleMoonRes() {
    moonResOpen = !moonResOpen;
    hudDirty = true;
}

function SelectMoonLevel(level) {
    moonResOpen = false;
    hudDirty = true;
    if (level === moonLevel || moonLoading !== null) {
        return;
    }
    if (!moonBuilt[String(level)]) {
        Report("moon: " + level + "x is not built (python tools/make_moon.py " + level + ")");
        return;
    }
    LoadMoonLevel(level);
    Report("moon: loading " + level + "x");
}

// Lunar sites stand on the terrain the shown level draws (a typed ground elevation wins).
function SiteGroundKm(site) {
    var levels = site.groundLevels;
    if (site.body === "Moon" && levels && levels[String(moonLevel)] !== undefined) {
        return levels[String(moonLevel)] / 1000;
    }
    return site.groundM / 1000;
}

function ApplyMoonGround() {
    var keys = ["places", "stations"];
    for (var k = 0; k < keys.length; k++) {
        var sites = layers[keys[k]] ? layers[keys[k]].sites : [];
        for (var n = 0; n < sites.length; n++) {
            if (sites[n].body === "Moon") {
                sites[n].groundKm = SiteGroundKm(sites[n]);
                sites[n].heightKm = sites[n].groundKm + sites[n].aglM / 1000;
            }
        }
    }
}

// ---- Frame rate ----
// Frames per second over 5 s windows, three times, sent to the server log ("fps: ..."): after a
// Moon level loads, and on F for the view at hand. Tick runs once per frame.
var fpsWatch = null;          // { start, frames, reports }

function WallSeconds() {
    var d = Date.now;
    return (d.dayOfYear - 1) * 86400 + d.hour * 3600 + d.minute * 60 + d.second + d.millisecond / 1000;
}

function StartFpsWatch() {
    fpsWatch = { start: WallSeconds(), frames: 0, reports: 0 };
}

function UpdateFpsWatch() {
    if (fpsWatch === null) {
        return;
    }
    fpsWatch.frames++;
    var now = WallSeconds();
    var dt = now - fpsWatch.start;
    if (dt < 0) {                 // the day rolled over
        StartFpsWatch();
    } else if (dt >= 5) {
        Report("fps: " + (fpsWatch.frames / dt).toFixed(1) + " over " + dt.toFixed(1) + " s, Moon " + moonLevel
            + "x, camera " + (focusTag || "?"));
        fpsWatch.reports++;
        if (fpsWatch.reports >= 3) {
            fpsWatch = null;
        } else {
            fpsWatch.start = now;
            fpsWatch.frames = 0;
        }
    }
}

// The Moon at time t: position (GMAT km and Unity world units), velocity and rotation.
function MoonAt(t) {
    var s = moonSamples;
    if (s.length < 2) {
        return null;
    }
    var i = 0;
    while (i < s.length - 2 && s[i + 1][0] <= t) {
        i++;
    }
    var a = s[i], b = s[i + 1];
    var h = b[0] - a[0];
    var u = Math.max(0, Math.min(1, (t - a[0]) / h));
    var h00 = 2 * u * u * u - 3 * u * u + 1, h10 = u * u * u - 2 * u * u + u;
    var h01 = -2 * u * u * u + 3 * u * u, h11 = u * u * u - u * u;
    var km = [], vel = [];
    for (var k = 0; k < 3; k++) {
        km.push(h00 * a[1 + k] + h10 * h * a[4 + k] + h01 * b[1 + k] + h11 * h * b[4 + k]);
        vel.push(a[4 + k] + (b[4 + k] - a[4 + k]) * u);
    }
    var sign = a[7] * b[7] + a[8] * b[8] + a[9] * b[9] + a[10] * b[10] < 0 ? -1 : 1;
    var q = [];
    for (var j = 0; j < 4; j++) {
        q.push(a[7 + j] + (sign * b[7 + j] - a[7 + j]) * u);
    }
    var ql = Math.sqrt(q[0] * q[0] + q[1] * q[1] + q[2] * q[2] + q[3] * q[3]);
    return { km: km, kmVel: vel,
        world: new Vector3(km[0] * KM_TO_WORLD_UNITS, km[2] * KM_TO_WORLD_UNITS, km[1] * KM_TO_WORLD_UNITS),
        q: { x: q[0] / ql, y: q[1] / ql, z: q[2] / ql, w: q[3] / ql } };
}

function UpdateMoon() {
    moonNow = MoonAt(elapsedSeconds);
    var moon = Entity.Get(MOON_LEVEL_IDS[moonLevel]);
    if (moon === null || moonNow === null) {
        return;
    }
    SetWorld(moon, moonNow.world);
    moon.SetRotation(new Quaternion(moonNow.q.x, moonNow.q.y, moonNow.q.z, moonNow.q.w), false);
    var grid = Entity.Get(MOON_GRID_ID);
    if (grid !== null && gridOn.Moon) {
        SetWorld(grid, moonNow.world);
        grid.SetRotation(new Quaternion(moonNow.q.x, moonNow.q.y, moonNow.q.z, moonNow.q.w), false);
    }
}

// A spacecraft's position and velocity (GMAT km, km/s) relative to the body it orbits.
function RelativeState(tag) {
    var p = PositionAt(fleet[tag], elapsedSeconds), v = VelocityAt(fleet[tag], elapsedSeconds);
    if (CraftBody(tag) === "Moon" && moonNow !== null) {
        p = [p[0] - moonNow.km[0], p[1] - moonNow.km[1], p[2] - moonNow.km[2]];
        v = [v[0] - moonNow.kmVel[0], v[1] - moonNow.kmVel[1], v[2] - moonNow.kmVel[2]];
    }
    return { p: p, v: v };
}

var fleetCraft = {};             // tag -> { body, source }

function CraftBody(tag) {
    return fleetCraft[tag] ? fleetCraft[tag].body : "Earth";
}

// Whether the Earth or the Moon hides p from the camera (spheres; a point on or under a
// body's surface counts as hidden exactly when it is below its horizon).
function BehindBodies(cam, p) {
    if (BehindSphere(cam, p, new Vector3(0, 0, 0), EARTH_RADIUS)) {
        return true;
    }
    return moonNow !== null && BehindSphere(cam, p, moonNow.world, MOON_RADIUS);
}

function BehindSphere(cam, p, c, radius) {
    var px = p.x - c.x, py = p.y - c.y, pz = p.z - c.z;
    var r = Math.min(radius, Math.sqrt(px * px + py * py + pz * pz) * 0.99999);
    return SegmentHitsSphere(cam, p, c, r, true);
}

// Whether the segment a-b passes through the sphere (centre c); strict: only between its ends.
function SegmentHitsSphere(a, b, c, radius, strict) {
    var dx = b.x - a.x, dy = b.y - a.y, dz = b.z - a.z;
    var fx = a.x - c.x, fy = a.y - c.y, fz = a.z - c.z;
    var len2 = dx * dx + dy * dy + dz * dz;
    var t = -(fx * dx + fy * dy + fz * dz) / len2;
    if (strict ? (t <= 0 || t >= 1) : false) {
        return false;
    }
    t = Math.max(0, Math.min(1, t));
    var cx = fx + dx * t, cy = fy + dy * t, cz = fz + dz * t;
    return cx * cx + cy * cy + cz * cz < radius * radius;
}

// Spacecraft listed in fleet.json without a mesh entity in index.veml get one from script:
// probe.glb at scale 0.05 (as in index.veml), tagged with their tag once loaded.
const CRAFT_URL = DATA_BASE_URL + "models/probe.glb";
var craftPending = {};           // entity id -> tag
var craftIdCounter = 0;

function CreateMissingCraft() {
    for (var n = 0; n < fleetTags.length; n++) {
        var tag = fleetTags[n];
        var already = false;
        for (var pid in craftPending) {
            already = already || craftPending[pid] === tag;
        }
        if (Entity.GetByTag(tag) !== null || already) {
            continue;
        }
        craftIdCounter++;
        var hex = craftIdCounter.toString(16);
        while (hex.length < 12) {
            hex = "0" + hex;
        }
        var id = "b0e5a000-0000-4000-b000-" + hex;
        craftPending[id] = tag;
        MeshEntity.Create(null, CRAFT_URL, [CRAFT_URL], new Vector3(0, 0, 0), new Quaternion(0, 0, 0, 1),
            id, "OnCraftLoaded");
        Report("fleet: creating a model for " + tag + " (" + fleetNames[tag] + ")");
    }
}

function OnCraftLoaded(entity) {
    for (var id in craftPending) {
        var e = Entity.Get(id);
        if (e !== null) {
            e.tag = craftPending[id];
            e.SetScale(new Vector3(0.05, 0.05, 0.05), false);
            e.SetVisibility(true);
            delete craftPending[id];
        }
    }
}

// ---- Places and ground stations ----
// From places.xlsx and groundstations.xlsx (tools/places.py). fleet.json gives each site's
// Earth-fixed (WGS84) position in km; every tick it is turned with the Earth -- by the same
// GMAT rotation angle that turns the textured globe -- so it stays on its spot. Each is a small
// flat-coloured sphere (unit radius), scaled with the camera distance so it stays the same size
// on screen, with a label: yellow for places, cyan for ground stations.
// Ground stations also get a link line to each spacecraft, shown while that spacecraft is
// inside the station's beam (a cone of fov_deg, centred straight up): solid for a 2-way link,
// dashed for 1-way. The line models run from (0,0,0) to (0,1,0); each tick a visible one is
// placed at the station, turned toward the spacecraft and stretched to the distance.
const SITE_SIZE_PER_DISTANCE = 0.006;   // marker radius as a fraction of camera distance
const LINK_URLS = { "2-way": DATA_BASE_URL + "models/link_2way.glb", "1-way": DATA_BASE_URL + "models/link_1way.glb" };
var layers = {
    places: { title: "Places", marker: DATA_BASE_URL + "models/place.glb",
        color: new Color(1, 0.85, 0.2, 1), on: true, sites: [] },
    stations: { title: "Ground stations", marker: DATA_BASE_URL + "models/station.glb",
        color: new Color(0.3, 0.9, 1, 1), on: true, sites: [] }
};
const LAYER_KEYS = ["places", "stations"];
var linkShown = {};             // link entity id -> currently visible
var siteIdCounter = 0;

function NextSiteId() {
    siteIdCounter++;
    var hex = siteIdCounter.toString(16);
    while (hex.length < 12) {
        hex = "0" + hex;
    }
    return "b0e5a000-0000-4000-9000-" + hex;
}

function DeleteEntity(id) {
    var old = Entity.Get(id);
    if (old !== null) {
        old.Delete();
    }
}

// Rebuild one layer's markers, labels and links from fleet.json (start-up and after an update).
function CreateSites(key, list) {
    var layer = layers[key];
    for (var i = 0; i < layer.sites.length; i++) {
        var old = layer.sites[i];
        DeleteEntity(old.id);
        for (var oldTag in old.links) {
            DeleteEntity(old.links[oldTag]);
            delete linkShown[old.links[oldTag]];
        }
        if (labels["site-" + old.id]) {
            labels["site-" + old.id].canvas.Delete();
            delete labels["site-" + old.id];
        }
    }
    layer.sites = [];
    for (var n = 0; n < list.length; n++) {
        var lat = list[n].lat * Math.PI / 180, lon = list[n].lon * Math.PI / 180;
        var site = { name: list[n].name, ecef: list[n].ecef_km, id: NextSiteId(), world: null, upWorld: null,
            groundM: list[n].ground_m, groundLevels: list[n].ground_m_levels || null,
            body: list[n].body || "Earth",
            // straight up (the ellipsoid normal), Earth-fixed
            up: [Math.cos(lat) * Math.cos(lon), Math.cos(lat) * Math.sin(lon), Math.sin(lat)], links: {},
            east: [-Math.sin(lon), Math.cos(lon), 0],
            north: [-Math.sin(lat) * Math.cos(lon), -Math.sin(lat) * Math.sin(lon), Math.cos(lat)],
            lat: list[n].lat, lon: list[n].lon, aglM: list[n].agl_m,
            fovDeg: list[n].fov_deg, radios: list[n].radios || [],
            below: key === "stations",     // label below the dot (places: above)
            layer: key };
        site.terrain = site.body === "Moon" ? (fleetTerrain[SiteKey(site)] || null) : null;
        site.groundKm = SiteGroundKm(site);
        site.heightKm = site.groundKm + site.aglM / 1000;
        layer.sites.push(site);
        SiteWorld(site);
        MeshEntity.Create(null, layer.marker, [layer.marker], ToUnity(site.world), new Quaternion(0, 0, 0, 1),
            site.id, "OnSiteLoaded");
        var words = site.name, lines = 1;
        if (key === "stations") {
            site.cosHalfFov = Math.cos(list[n].fov_deg * Math.PI / 360);
            site.link = list[n].link;
            words += "\n" + list[n].freq_mhz + " MHz  " + list[n].link;
            lines = 2;
            for (var k = 0; k < fleetTags.length; k++) {
                var linkId = NextSiteId();
                site.links[fleetTags[k]] = linkId;
                linkShown[linkId] = false;
                MeshEntity.Create(null, LINK_URLS[site.link], [LINK_URLS[site.link]], ToUnity(site.world),
                    new Quaternion(0, 0, 0, 1), linkId, "OnLinkLoaded");
            }
        }
        CreateLabel("site-" + site.id, words, layer.color, site, lines);
    }
    ShowLayer(key);
    Report(key + ": " + layer.sites.length);
}

function OnSiteLoaded(marker) {
    for (var n = 0; n < LAYER_KEYS.length; n++) {
        ShowLayer(LAYER_KEYS[n]);
    }
}

function OnLinkLoaded(link) {
    link.SetVisibility(false);  // UpdateSites shows it while the spacecraft is in the beam
}

// Earth-fixed -> inertial (turned about Z by the Earth's rotation angle, as RotateEarth turns
// the globe), then GMAT -> Unity axes (swap Y and Z); positions also km -> world units.
//   trueWorld: the WGS84 position (used for the ground-station beam test);
//   world: where the dot and label are drawn -- straight up from the globe's centre along the
//   site's latitude/longitude, at the globe's radius + its height. The globe is a sphere with
//   the map painted on by latitude, so this puts the dot on its map pixel and on the visible
//   surface; the WGS84 point is up to ~21 km under the sphere and ~20 km off the map.
function SiteWorld(site) {
    if (site.body === "Moon") {
        MoonSiteWorld(site);
        return;
    }
    var a = (earthRotation0 + earthRate * elapsedSeconds) * Math.PI / 180;
    var c = Math.cos(a), s = Math.sin(a), e = site.ecef, u = site.up;
    site.trueWorld = new Vector3((c * e[0] - s * e[1]) * KM_TO_WORLD_UNITS, e[2] * KM_TO_WORLD_UNITS,
        (s * e[0] + c * e[1]) * KM_TO_WORLD_UNITS);
    site.upWorld = [c * u[0] - s * u[1], u[2], s * u[0] + c * u[1]];
    var ea = site.east, no = site.north;
    site.eastWorld = [c * ea[0] - s * ea[1], ea[2], s * ea[0] + c * ea[1]];
    site.northWorld = [c * no[0] - s * no[1], no[2], s * no[0] + c * no[1]];
    var r = EARTH_RADIUS + site.heightKm * KM_TO_WORLD_UNITS;
    site.world = new Vector3(site.upWorld[0] * r, site.upWorld[1] * r, site.upWorld[2] * r);
}

// A Moon site: its Moon-fixed vectors in the viewer's (X, Z, Y) order, turned by the Moon's
// rotation and moved to its position; drawn on the sphere of the mean radius + its height.
function MoonSiteWorld(site) {
    var m = moonNow !== null ? moonNow : { world: new Vector3(0, 0, 0), q: { x: 0, y: 0, z: 0, w: 1 } };
    var turn = function (v) { return Rotate(m.q, v[0], v[2], v[1]); };
    var e = turn(site.ecef);
    site.trueWorld = new Vector3(m.world.x + e[0] * KM_TO_WORLD_UNITS, m.world.y + e[1] * KM_TO_WORLD_UNITS,
        m.world.z + e[2] * KM_TO_WORLD_UNITS);
    site.upWorld = turn(site.up);
    site.eastWorld = turn(site.east);
    site.northWorld = turn(site.north);
    var r = MOON_RADIUS + site.heightKm * KM_TO_WORLD_UNITS;
    site.world = new Vector3(m.world.x + site.upWorld[0] * r, m.world.y + site.upWorld[1] * r,
        m.world.z + site.upWorld[2] * r);
}

// Centre and radius (world units) of the body a site is on.
function SiteBody(site) {
    if (site.body === "Moon" && moonNow !== null) {
        return { centre: moonNow.world, radius: MOON_RADIUS };
    }
    return { centre: new Vector3(0, 0, 0), radius: EARTH_RADIUS };
}

// Rotation taking +Y onto the unit vector (x, y, z): about axis Y x d by angle acos(y).
function RotationFromUp(x, y, z) {
    var len = Math.sqrt(z * z + x * x);
    if (len < 1e-9) {
        return y > 0 ? new Quaternion(0, 0, 0, 1) : new Quaternion(1, 0, 0, 0);
    }
    var half = Math.acos(Math.max(-1, Math.min(1, y))) / 2;
    return new Quaternion(z / len * Math.sin(half), 0, -x / len * Math.sin(half), Math.cos(half));
}

function UpdateSites() {
    var cam = CameraWorld();
    for (var n = 0; n < LAYER_KEYS.length; n++) {
        var layer = layers[LAYER_KEYS[n]];
        for (var i = 0; i < layer.sites.length; i++) {
            var site = layer.sites[i];
            SiteWorld(site);
            var p = site.world;
            var marker = Entity.Get(site.id);
            if (marker !== null) {
                var dx = p.x - cam.x, dy = p.y - cam.y, dz = p.z - cam.z;
                var r = SITE_SIZE_PER_DISTANCE * Math.sqrt(dx * dx + dy * dy + dz * dz);
                SetWorld(marker, p);
                marker.SetScale(new Vector3(r, r, r), false);
            }
            for (var tag in site.links) {
                UpdateLink(site, tag, layer.on);
            }
        }
    }
}

// Show the link while the spacecraft is inside the station's beam, stretched from one to the other.
function UpdateLink(site, tag, layerOn) {
    var linkId = site.links[tag];
    var craft = Entity.GetByTag(tag);
    var link = Entity.Get(linkId);
    if (link === null) {
        return;
    }
    var show = false;
    var q = null;
    if (layerOn && craft !== null) {
        // in the beam? -- from the true (WGS84) position
        q = GetWorld(craft);
        var p = site.trueWorld, u = site.upWorld;
        var tx = q.x - p.x, ty = q.y - p.y, tz = q.z - p.z;
        var tdist = Math.sqrt(tx * tx + ty * ty + tz * tz);
        show = tdist > 0 && (tx * u[0] + ty * u[1] + tz * u[2]) / tdist >= site.cosHalfFov;
        // the other body must not be in the way (the site's own is ruled out by the beam)
        if (show && site.body === "Moon") {
            show = !SegmentHitsSphere(p, q, new Vector3(0, 0, 0), EARTH_RADIUS, false);
        } else if (show && moonNow !== null) {
            show = !SegmentHitsSphere(p, q, moonNow.world, MOON_RADIUS, false);
        }
        // nor the terrain around a Moon station (its horizon mask)
        if (show && site.terrain) {
            var e = site.eastWorld, n = site.northWorld;
            var el = Math.asin((tx * u[0] + ty * u[1] + tz * u[2]) / tdist) * 180 / Math.PI;
            var az = Math.atan2(tx * e[0] + ty * e[1] + tz * e[2], tx * n[0] + ty * n[1] + tz * n[2]) * 180 / Math.PI;
            show = el >= HorizonAt(site.terrain, az);
        }
    }
    if (show) {
        // drawn from the dot as shown on the globe to the spacecraft
        var w = site.world;
        var dx = q.x - w.x, dy = q.y - w.y, dz = q.z - w.z;
        var dist = Math.sqrt(dx * dx + dy * dy + dz * dz);
        SetWorld(link, w);
        link.SetRotation(RotationFromUp(dx / dist, dy / dist, dz / dist), false);
        link.SetScale(new Vector3(1, dist, 1), false);
    }
    if (show !== linkShown[linkId]) {
        link.SetVisibility(show);
        linkShown[linkId] = show;
        Report("link " + site.name + " - " + tag + (show ? " in beam" : " out of beam"));
    }
}

function ShowLayer(key) {
    var layer = layers[key];
    for (var i = 0; i < layer.sites.length; i++) {
        var marker = Entity.Get(layer.sites[i].id);
        if (marker !== null) {
            marker.SetVisibility(layer.on);
        }
    }
    // labels follow layer.on in UpdateLabels
}

// Places / Ground stations on or off (their [x] boxes in the Assets panel; P for places).
function ToggleLayer(key) {
    layers[key].on = !layers[key].on;
    ShowLayer(key);
    RefreshLayerBoxes();
    Report(key + " " + (layers[key].on ? "on" : "off"));
}

function LayerBox(key) {
    return layers[key].on ? "[x]" : "[ ]";
}

function RefreshLayerBoxes() {
    RefreshHudBoxes();
}

// ---- Click a place or ground station to centre it ----
// A left click (press and release with almost no mouse movement; more is an orbit drag) on a
// dot or its label selects that site: see "Place / station view" below.
// Picking: Input.GetPointerRaycast casts a ray from the mouse, but it reports only the first
// collider hit -- usually the atmosphere shells around the globe -- so it is used just for the
// click's direction; which dot or label lies along that direction is worked out here, in view
// units (offset from the view centre / distance ahead). Clicks on the Assets panel are ignored.
const CLICK_MAX_DRAG = 6;            // total mouse movement (px) still counted as a click
const SITE_CLICK_RADIUS = 2.5;       // a dot's click area, in dot radii
var mouseWasDown = false;
var clickDrag = 0;

function UpdateClick() {
    var down = Input.GetLeft();
    if (down && !mouseWasDown) {
        clickDrag = 0;
    }
    if (down) {
        var look = Input.GetLookValue();
        clickDrag += Math.abs(look.x) + Math.abs(look.y);
    }
    if (!down && mouseWasDown && panelMove !== null && panelMoved) {
        EndMove();
    } else if (!down && mouseWasDown && clickDrag <= CLICK_MAX_DRAG && !pieOpen) {
        PickSite();
    }
    mouseWasDown = down;
}

// A panel's grip: arm it for moving (again: disarm). The next left-drag moves it.
function StartMove(key) {
    panelMove = panelMove === key ? null : key;
    panelMoved = false;
    ShowGrips();
    Report("panel move: " + (panelMove || "off"));
}

function EndMove() {
    Report("panel " + panelMove + " moved to " + panelOffset[panelMove].x.toFixed(3) + ", "
        + panelOffset[panelMove].y.toFixed(3));
    panelMove = null;
    panelMoved = false;
    ShowGrips();
}

function ShowGrips() {
    var grips = { hud: hud !== null ? hud.grip : null, info: info !== null ? info.grip : null,
        att: attPanel !== null ? attPanel.grip : null, chart: chartPanel !== null ? chartPanel.grip : null,
        plot: plotPanel !== null ? plotPanel.grip : null };
    for (var key in grips) {
        if (grips[key]) {
            grips[key].SetColor(panelMove === key ? GRIP_ARMED : GRIP_COLOR);
        }
    }
}

function PickSite() {
    var hit = Input.GetPointerRaycast(new Vector3(0, 0, 1), 0);
    if (hit === null || hit === undefined) {
        Report("click: the pointer ray hit nothing");   // empty space, or no colliders
        return;
    }
    var cam = CameraWorld();
    var q = Camera.GetRotation(false);
    var f = Rotate(q, 0, 0, 1), r = Rotate(q, 1, 0, 0), u = Rotate(q, 0, 1, 0);
    var hp = FromUnity(hit.hitPoint);
    var dx = hp.x - cam.x, dy = hp.y - cam.y, dz = hp.z - cam.z;
    var df = dx * f[0] + dy * f[1] + dz * f[2];
    if (df <= 0) {
        return;
    }
    // the click, in view units
    var sx = (dx * r[0] + dy * r[1] + dz * r[2]) / df;
    var sy = (dx * u[0] + dy * u[1] + dz * u[2]) / df;
    if (OverHud(sx, sy) || OverLeftPanels(sx, sy) || OverChartPanel(sx, sy) || OverPlotPanel(sx, sy)) {
        return;
    }
    var best = null, bestScore = Infinity;
    for (var n = 0; n < LAYER_KEYS.length; n++) {
        var layer = layers[LAYER_KEYS[n]];
        if (!layer.on) {
            continue;
        }
        for (var i = 0; i < layer.sites.length; i++) {
            var site = layer.sites[i], p = site.world;
            var vx = p.x - cam.x, vy = p.y - cam.y, vz = p.z - cam.z;
            var vf = vx * f[0] + vy * f[1] + vz * f[2];
            if (vf <= 0 || BehindBodies(cam, p)) {
                continue;
            }
            var k = Math.sqrt(vx * vx + vy * vy + vz * vz) / vf;   // sizes are set per distance
            var ex = sx - (vx * r[0] + vy * r[1] + vz * r[2]) / vf;
            var ey = sy - (vx * u[0] + vy * u[1] + vz * u[2]) / vf;
            // on the dot: score 0..1 (nearest wins); on its label: 1.5
            var score = Math.sqrt(ex * ex + ey * ey) / (SITE_CLICK_RADIUS * SITE_SIZE_PER_DISTANCE * k);
            if (score > 1) {
                var lines = site.link ? 2 : 1;
                var halfW = 0.5 * LABEL_WIDTH_PER_DISTANCE * k;
                var halfH = 0.5 * LABEL_WIDTH_PER_DISTANCE * LABEL_H * lines / LABEL_W * k;
                var off = (SITE_SIZE_PER_DISTANCE * k + SITE_LABEL_GAP_PER_DISTANCE * k + halfH) * (site.below ? -1 : 1);
                score = Math.abs(ex) <= halfW && Math.abs(ey - off) <= halfH ? 1.5 : Infinity;
            }
            if (score < bestScore) {
                best = site;
                bestScore = score;
            }
        }
    }
    if (best !== null) {
        CentreSite(best);
    } else {
        Report("click: hit " + (hit.entity ? hit.entity.tag : "?") + ", no place or station there");
    }
}

// Whether a point in view units is on the Assets panel (only its header row when collapsed).
function OverHud(sx, sy) {
    if (hud === null) {
        return false;
    }
    var s = HUD_WORLD_WIDTH / HUD_W;
    var h = (hudOpen ? hud.height : HUD_PAD + HUD_ROW + HUD_GAP) * s;
    var x = sx * HUD_REF - panelOffset.hud.x, y = sy * HUD_REF - panelOffset.hud.y;
    return x >= HUD_RIGHT_EDGE - HUD_WORLD_WIDTH && x <= HUD_RIGHT_EDGE && y <= HUD_TOP_EDGE && y >= HUD_TOP_EDGE - h;
}

// ---- Place / station view ----
// Selecting a place or station (a click on it, or its row in the panel) locks the camera to it:
// the site becomes the pivot, in its own ground frame (east, north, up) that turns with the
// Earth, so the view stays fixed to the ground. The camera starts where it is and settles
// straight above the site; dragging orbits around it, zoom changes the distance (down to
// SITE_MIN_DIST: the camera's 0.3-unit near plane would clip the ground closer in). A white
// square marks the site. Earth / R / the arrow keys / a spacecraft leave the view.
// The panel's "Ground view" row puts the camera 1 km above the site's ground, looking up
// (dragging looks around), to watch spacecraft pass over; "Back to orbit view" returns.
// Note: the near plane hides everything within 30 km of the camera, so in the ground view
// the nearby ground and the first 30 km of a station's link line are not drawn.
const SITE_MIN_DIST = 0.5;           // closest orbit-view distance, world units (50 km)
const SITE_SETTLE_EASE = 0.12;       // fraction of the way to straight-above made each frame
const GROUND_DEG_PER_PIXEL = 0.5;    // look speed in the ground view
const GROUND_EYE_KM = 1;             // eye height above the site's ground
const SELECT_ID = "b0e5a000-0000-4000-a000-000000000002";
const SELECT_URL = DATA_BASE_URL + "models/select.glb";
const SELECT_SIZE_PER_DISTANCE = 0.014;   // square half-size as a fraction of camera distance
var siteView = null;   // { site, mode: "orbit" | "ground", az, el, dist, settle, lookAz, lookEl }
var selectShown = false;

function CentreSite(site) {
    if (targetPendingFor !== null) {
        SetTarget({ kind: "site", site: site });
        return;
    }
    if (focusTag !== "Earth") {
        SetFocus("Earth");
    }
    // start from where the camera is now, in the site's ground frame
    SiteWorld(site);
    var cam = CameraWorld();
    var v = [cam.x - site.world.x, cam.y - site.world.y, cam.z - site.world.z];
    var dist = Math.max(SITE_MIN_DIST, Math.sqrt(v[0] * v[0] + v[1] * v[1] + v[2] * v[2]));
    var up = Dot3(v, site.upWorld) / dist;
    siteView = { site: site, mode: "orbit", dist: dist, settle: true,
        el: Math.asin(Math.max(-1, Math.min(1, up))) * 180 / Math.PI,
        az: Math.atan2(Dot3(v, site.eastWorld), Dot3(v, site.northWorld)) * 180 / Math.PI,
        lookAz: 0, lookEl: 30 };
    siteView.el = Math.max(2, siteView.el);
    hudDirty = true;                 // adds the Ground view row
    HudSelect(focusTag);
    Report("site view: " + site.name);
}

// A place or station row in the Assets panel.
function CentreSiteById(id) {
    for (var n = 0; n < LAYER_KEYS.length; n++) {
        var sites = layers[LAYER_KEYS[n]].sites;
        for (var i = 0; i < sites.length; i++) {
            if (sites[i].id === id) {
                CentreSite(sites[i]);
                return;
            }
        }
    }
}

function ToggleGroundView() {
    if (siteView === null) {
        return;
    }
    if (siteView.mode === "orbit") {
        siteView.mode = "ground";
        siteView.lookAz = HighestCraftAzimuth(siteView.site);
        siteView.lookEl = 30;
    } else {
        siteView.mode = "orbit";
    }
    hudDirty = true;
    PlaceSiteCamera();
    Report("site view: " + siteView.mode + " at " + siteView.site.name);
}

// Azimuth (deg from north through east) of the spacecraft highest in the site's sky.
function HighestCraftAzimuth(site) {
    var best = -2, az = 0;
    for (var n = 0; n < fleetTags.length; n++) {
        var craft = Entity.GetByTag(fleetTags[n]);
        if (craft === null) {
            continue;
        }
        var q = GetWorld(craft), p = site.trueWorld;
        var d = [q.x - p.x, q.y - p.y, q.z - p.z];
        var len = Math.sqrt(Dot3(d, d));
        var up = Dot3(d, site.upWorld) / len;
        if (up > best) {
            best = up;
            az = Math.atan2(Dot3(d, site.eastWorld), Dot3(d, site.northWorld)) * 180 / Math.PI;
        }
    }
    return az;
}

function SiteDrag(dx, dy) {
    if (siteView.mode === "ground") {
        siteView.lookAz += dx * GROUND_DEG_PER_PIXEL;
        siteView.lookEl = Math.max(-10, Math.min(89.9, siteView.lookEl + dy * GROUND_DEG_PER_PIXEL));
    } else {
        siteView.settle = false;     // the user has taken over
        siteView.az += dx * ORBIT_DEG_PER_PIXEL;
        siteView.el = Math.max(2, Math.min(89.9, siteView.el - dy * ORBIT_DEG_PER_PIXEL));
    }
}

function SiteZoom(factor) {
    if (siteView.mode === "orbit") {
        siteView.dist = Math.max(SITE_MIN_DIST, Math.min(EARTH_FRAMING.max, siteView.dist * factor));
    }
}

// Every frame: the site has turned with the Earth, so place the camera again.
function FollowSite() {
    if (siteView === null) {
        return;
    }
    SiteWorld(siteView.site);
    if (siteView.mode === "orbit" && siteView.settle) {
        siteView.el += (89.9 - siteView.el) * SITE_SETTLE_EASE;
    }
    PlaceSiteCamera();
}

// A direction in the site's ground frame: azimuth from north through east, elevation above
// the horizon (deg). Also returns the direction it moves toward as elevation grows (the
// camera's up).
function GroundDirection(site, azDeg, elDeg) {
    var az = azDeg * Math.PI / 180, el = elDeg * Math.PI / 180;
    var e = site.eastWorld, n = site.northWorld, u = site.upWorld;
    var h = [Math.sin(az) * e[0] + Math.cos(az) * n[0], Math.sin(az) * e[1] + Math.cos(az) * n[1],
        Math.sin(az) * e[2] + Math.cos(az) * n[2]];
    var ce = Math.cos(el), se = Math.sin(el);
    return { dir: [ce * h[0] + se * u[0], ce * h[1] + se * u[1], ce * h[2] + se * u[2]],
        up: [-se * h[0] + ce * u[0], -se * h[1] + ce * u[1], -se * h[2] + ce * u[2]] };
}

function PlaceSiteCamera() {
    var site = siteView.site, w = site.world, pos, forward, up;
    if (siteView.mode === "ground") {
        var body = SiteBody(site);
        var r = body.radius + (site.groundKm + GROUND_EYE_KM) * KM_TO_WORLD_UNITS;
        var u = site.upWorld;
        pos = [body.centre.x + u[0] * r, body.centre.y + u[1] * r, body.centre.z + u[2] * r];
        var look = GroundDirection(site, siteView.lookAz, siteView.lookEl);
        forward = look.dir;
        up = look.up;
    } else {
        // camera out along (az, el) from the site, looking back at it
        var g = GroundDirection(site, siteView.az, siteView.el);
        pos = [w.x + g.dir[0] * siteView.dist, w.y + g.dir[1] * siteView.dist, w.z + g.dir[2] * siteView.dist];
        forward = [-g.dir[0], -g.dir[1], -g.dir[2]];
        up = g.up;
    }
    SetCameraWorld(new Vector3(pos[0], pos[1], pos[2]));
    Camera.SetRotation(LookRotation(forward, up), false);
}

function Dot3(a, b) {
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2];
}

// The rotation (as [x, y, z, w]) whose matrix has columns r, u, f: local X, Y, Z -> r, u, f.
function BasisQuat(r, u, f) {
    var m00 = r[0], m10 = r[1], m20 = r[2], m01 = u[0], m11 = u[1], m21 = u[2], m02 = f[0], m12 = f[1], m22 = f[2];
    var tr = m00 + m11 + m22, S;
    if (tr > 0) {
        S = Math.sqrt(tr + 1) * 2;
        return [(m21 - m12) / S, (m02 - m20) / S, (m10 - m01) / S, 0.25 * S];
    }
    if (m00 > m11 && m00 > m22) {
        S = Math.sqrt(1 + m00 - m11 - m22) * 2;
        return [0.25 * S, (m01 + m10) / S, (m02 + m20) / S, (m21 - m12) / S];
    }
    if (m11 > m22) {
        S = Math.sqrt(1 + m11 - m00 - m22) * 2;
        return [(m01 + m10) / S, 0.25 * S, (m12 + m21) / S, (m02 - m20) / S];
    }
    S = Math.sqrt(1 + m22 - m00 - m11) * 2;
    return [(m02 + m20) / S, (m12 + m21) / S, 0.25 * S, (m10 - m01) / S];
}

// Unity's Quaternion.LookRotation: +Z along forward, +Y as close to up as possible.
function LookRotation(forward, upHint) {
    var fl = Math.sqrt(Dot3(forward, forward));
    var f = [forward[0] / fl, forward[1] / fl, forward[2] / fl];
    var r = [upHint[1] * f[2] - upHint[2] * f[1], upHint[2] * f[0] - upHint[0] * f[2], upHint[0] * f[1] - upHint[1] * f[0]];
    var rl = Math.sqrt(Dot3(r, r));
    r = [r[0] / rl, r[1] / rl, r[2] / rl];
    var u = [f[1] * r[2] - f[2] * r[1], f[2] * r[0] - f[0] * r[2], f[0] * r[1] - f[1] * r[0]];
    var q = BasisQuat(r, u, f);
    return new Quaternion(q[0], q[1], q[2], q[3]);
}

// ---- Selection square ----
// models/select.glb (a white square frame, half-size 1) around the selected site, facing the
// camera and kept the same size on screen. Like the labels it is drawn LABEL_DRAW_DISTANCE from
// the camera (shrunk to match), so the globe and atmosphere never cut it, and it is hidden
// while the site is behind the Earth, in the ground view, or with its layer switched off.
function CreateSelectBox() {
    MeshEntity.Create(null, SELECT_URL, [SELECT_URL], new Vector3(0, 0, 0), new Quaternion(0, 0, 0, 1),
        SELECT_ID, "OnSelectBoxLoaded");
}

function OnSelectBoxLoaded(box) {
    box.SetVisibility(false);
    selectShown = false;
}

function UpdateSelectBox() {
    var box = Entity.Get(SELECT_ID);
    if (box === null) {
        return;
    }
    var cam = CameraWorld();
    var site = siteView !== null ? siteView.site : null;
    var show = site !== null && siteView.mode === "orbit" && layers[site.layer].on && !BehindBodies(cam, site.world);
    if (show !== selectShown) {
        box.SetVisibility(show);
        selectShown = show;
    }
    if (!show) {
        return;
    }
    var p = site.world;
    var dx = p.x - cam.x, dy = p.y - cam.y, dz = p.z - cam.z;
    var dist = Math.sqrt(dx * dx + dy * dy + dz * dz);
    var k = LABEL_DRAW_DISTANCE / dist;
    var half = SELECT_SIZE_PER_DISTANCE * dist * k;
    SetWorld(box, new Vector3(cam.x + dx * k, cam.y + dy * k, cam.z + dz * k));
    box.SetRotation(Camera.GetRotation(false), false);
    box.SetScale(new Vector3(half, half, half), false);
}

// ---- 10 degree grids ----
// Latitude / longitude lines every 10 deg (models/grid.glb, written by tools/places.py: unit
// radius 1.0015 -- ~10 km above the Earth; the equator and the prime meridian in gold), one on
// each body. The Moon's is scaled up to MOON_GRID_LIFT_KM above the mean radius, clear of the
// highest LOLA terrain on moon.glb (10.7 km). grid.glb is laid out like earth.glb and moon.glb, so the
// Earth's grid turns with the Earth (RotateEarth) and the Moon's is placed and turned with the
// Moon (UpdateMoon). Each is switched by the [#] box on its body's row in the Assets panel (L:
// the grid of the body in view). Off at start.
const GRID_URL = DATA_BASE_URL + "models/grid.glb";
const GRID_ID = "b0e5a000-0000-4000-a000-000000000001";
const MOON_GRID_ID = "b0e5a000-0000-4000-a000-000000000005";
const MOON_GRID_LIFT_KM = 12;
var gridOn = { Earth: false, Moon: false };

function CreateGrid() {
    MeshEntity.Create(null, GRID_URL, [GRID_URL], ToUnity(new Vector3(0, 0, 0)), new Quaternion(0, 0, 0, 1),
        GRID_ID, "OnGridLoaded");
    MeshEntity.Create(null, GRID_URL, [GRID_URL], new Vector3(0, 0, 0), new Quaternion(0, 0, 0, 1),
        MOON_GRID_ID, "OnMoonGridLoaded");
}

function OnGridLoaded(grid) {
    grid.SetScale(new Vector3(EARTH_RADIUS, EARTH_RADIUS, EARTH_RADIUS), false);
    grid.SetVisibility(gridOn.Earth);
}

function OnMoonGridLoaded(grid) {
    var s = (MOON_RADIUS_KM + MOON_GRID_LIFT_KM) * KM_TO_WORLD_UNITS / 1.0015;
    grid.SetScale(new Vector3(s, s, s), false);
    grid.SetVisibility(gridOn.Moon);
}

function ToggleGrid(body) {
    gridOn[body] = !gridOn[body];
    var grid = Entity.Get(body === "Moon" ? MOON_GRID_ID : GRID_ID);
    if (grid !== null) {
        grid.SetVisibility(gridOn[body]);
    }
    RefreshHudBoxes();
    Report(body + " grid " + (gridOn[body] ? "on" : "off"));
}

// The body in view: the Moon when it, or a site on it, is selected; otherwise the Earth.
function BodyInView() {
    return focusTag === "Moon" || (siteView !== null && siteView.site.body === "Moon") ? "Moon" : "Earth";
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
    ApplyFleet(JSON.parse(body));
    CreateLabels();
    CreateHud();
}

// Use a fleet.json: positions, Earth rotation, Sun, orbit lines. Called on start-up and again
// after "Update TLEs" (same spacecraft, new data, playback restarted at the new run time).
function ApplyFleet(parsed) {
    fleet = parsed.objects;
    fleetNames = parsed.names;
    fleetTags = [];
    windowEnd = 0;
    windowEndReported = false;
    for (var tag in fleet) {
        fleetTags.push(tag);
        var track = fleet[tag];
        windowEnd = Math.max(windowEnd, track[track.length - 1].t);
    }
    fleetCraft = parsed.craft || {};
    instrumentDefs = parsed.instruments || {};
    fleetRadios = parsed.radios || {};
    fleetCharts = parsed.charts || {};
    fleetTerrain = parsed.terrain || {};
    fleetTerrainCharts = parsed.terrain_charts || {};
    fleetTimelines = parsed.timelines || {};
    spanFiles = {};                  // a new fleet run: the longer spans are drawn afresh
    spanFailed = {};
    spanRetryAt = {};
    moonSamples = parsed.moon ? parsed.moon.samples : [];
    if (parsed.moon && parsed.moon.levels) {
        moonBuilt = parsed.moon.levels;
    }
    CreateMissingCraft();
    fleetGeneratedT = parsed.generated_t;
    fleetElements = parsed.elements || {};
    fleetPasses = parsed.passes || [];
    sunDir = parsed.sun_dir;
    clockRef = parsed.clock ? parsed.clock : null;
    elapsedSeconds = ClockSeconds();
    earthRotation0 = parsed.earth.rotation_deg;
    earthRate = parsed.earth.rate_deg_per_s;
    moonNow = MoonAt(elapsedSeconds);    // Moon sites are placed from it straight away
    PointSun(parsed.sun_dir);
    if (parsed.tracks) {
        CreateOrbitLines(parsed.tracks);
    }
    CreateSites("places", parsed.places || []);
    CreateSites("stations", parsed.ground_stations || []);
    for (var at in attitudes) {
        var tg = attitudes[at].target;
        if (tg !== null && tg.kind === "site") {
            var found = null, sites = layers[tg.site.layer].sites;
            for (var si = 0; si < sites.length; si++) {
                if (sites[si].name === tg.site.name) {
                    found = sites[si];
                }
            }
            attitudes[at].target = found === null ? null : { kind: "site", site: found };
        }
    }
    if (siteView !== null) {
        // keep the view on the same site, now a new object (or leave it if the site is gone)
        var same = null, old = siteView.site, list = layers[old.layer].sites;
        for (var s = 0; s < list.length; s++) {
            if (list[s].name === old.name) {
                same = list[s];
            }
        }
        if (same === null) {
            siteView = null;
        } else {
            siteView.site = same;
        }
    }
    if (hud !== null) {
        hudDirty = true;       // the lists may have changed
    }
    Report("fleet: " + fleetTags.join(",") + " window from " + parsed.epoch + " UTC; clock now t "
        + elapsedSeconds.toFixed(1) + " s (job ran at t " + parsed.generated_t + " s, " + parsed.generated + " UTC)");
}

// The VEML light is created as a Unity point light (the runtime's default), which doesn't
// reach the Earth at this scale. Make it a directional sun shining from GMAT's Sun direction.
// Lights the Earth, the glow and the spacecraft. WebVerse gives VEML worlds no ambient
// control, so the models' base colours are scaled down (Earth 0.4) and the sun raised to
// match: day sides look as they would at a 2.5 sun, with 2.5x the usual sheen (the soft
// luminous look) and a dimmer, ambient-lit night side. The night mask (in atmosphere.glb, 50%,
// turned away from the Sun below) darkens the night side further.
const SUN_INTENSITY = 6.25;

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
    PointNightMask(-sunDir[0], -sunDir[2], -sunDir[1]);
    Report("sun: directional, pitch " + pitch.toFixed(2) + " yaw " + yaw.toFixed(2)
        + ", intensity " + SUN_INTENSITY + " set " + propsSet);
}

// Point the night mask's pole (+Y of atmosphere.glb, local midnight) along the Unity direction (dx, dy, dz)
// away from the Sun: the rotation taking +Y onto d is about axis Y x d by angle acos(dy).
function PointNightMask(dx, dy, dz) {
    var night = Entity.GetByTag("Atmosphere");
    if (night === null) {
        Report("night: no entity tagged Atmosphere");
        return;
    }
    var ax = dz, az = -dx;
    var len = Math.sqrt(ax * ax + az * az);
    var angle = Math.acos(Math.max(-1, Math.min(1, dy)));
    var q = len < 1e-9 ? new Quaternion(0, 0, 0, 1)
        : new Quaternion(ax / len * Math.sin(angle / 2), 0, az / len * Math.sin(angle / 2), Math.cos(angle / 2));
    night.SetRotation(q, false);
    Report("night: pole toward (" + dx.toFixed(4) + ", " + dy.toFixed(4) + ", " + dz.toFixed(4) + ")");
}

// Turn the Earth so its texture's Greenwich meridian points where GMAT says it does.
// A GMAT rotation of +angle about Z is a Unity rotation of -angle about Y (axes swapped).
function RotateEarth(t) {
    var earth = Entity.GetByTag("Earth");
    if (earth === null) {
        return;
    }
    var half = -(earthRotation0 + earthRate * t) * Math.PI / 360;
    var turn = new Quaternion(0, Math.sin(half), 0, Math.cos(half));
    earth.SetRotation(turn, false);
    var grid = Entity.Get(GRID_ID);
    if (grid !== null) {
        grid.SetRotation(turn, false);     // Earth-fixed, like the globe's texture
    }
}

// Seconds since the fleet window's epoch, now, from the local clock. Falls back to the run time
// for a fleet.json without a "clock" entry.
function ClockSeconds() {
    if (clockRef === null) {
        return fleetGeneratedT;
    }
    var d = Date.now;
    var local = (d.dayOfYear - 1) * 86400 + d.hour * 3600 + d.minute * 60 + d.second + d.millisecond / 1000;
    var t = local - clockRef.utc_offset_s;
    if (d.year > clockRef.epoch_year) {            // window crossing New Year
        var y = clockRef.epoch_year;
        t += ((y % 4 === 0 && y % 100 !== 0) || y % 400 === 0 ? 366 : 365) * 86400;
    }
    return t - clockRef.epoch_doy_s;
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
    elapsedSeconds = ClockSeconds();
    if (elapsedSeconds < 0) {
        elapsedSeconds = 0;
    }
    if (elapsedSeconds > windowEnd) {
        elapsedSeconds = windowEnd;   // hold the last position; rerun tools/run_fleet.py
        if (!windowEndReported) {
            Report("fleet: end of the ephemeris window reached");
            windowEndReported = true;
        }
    }
    RotateEarth(elapsedSeconds);
    UpdateMoon();
    UpdateOrbitLines();
    for (var n = 0; n < fleetTags.length; n++) {
        var entity = Entity.GetByTag(fleetTags[n]);
        if (entity === null) {
            continue;
        }
        var p = PositionAt(fleet[fleetTags[n]], elapsedSeconds);
        // GMAT is right-handed Z-up, Unity left-handed Y-up: swapping Y and Z converts both.
        SetWorld(entity, new Vector3(p[0] * KM_TO_WORLD_UNITS, p[2] * KM_TO_WORLD_UNITS,
            p[1] * KM_TO_WORLD_UNITS));
    }
}

// ---- Start-up ----
PlaceCamera();
CreatePie();
CreateGrid();
CreateMoon();
CreateSelectBox();
CreateFrame();
Time.SetInterval(`Tick();`, 0);   // 0 = every frame
HTTPNetworking.Fetch(EPHEMERIS_URL, "OnEphemerisLoaded");
