const VESSEL_TYPES = ["unknown", "booster", "satellite", "docking", "station", "capsule", "lander", "probe"];
const CATEGORY_ORDER = ["booster", "satellite", "docking", "station", "capsule", "lander", "probe", "unknown"];
const CATEGORY_LABELS = {
  booster: "Boosters", satellite: "Satellites", docking: "Docking / Cargo", station: "Stations",
  capsule: "Capsules", lander: "Landers", probe: "Probes", unknown: "Unsorted",
};
const CATEGORY_COLORS = {
  booster: "#ff8a4d", satellite: "#4da3ff", docking: "#f472b6", station: "#c084fc",
  capsule: "#4dd28c", lander: "#ffd24d", probe: "#7d8aa8", unknown: "#dbe4f5",
};
// Hand-drawn per-category icon bodies (24x24 viewBox), replacing the old
// generic triangle/diamond/hexagon/square polygons -- these are meant to
// actually read as the thing they represent (a rocket, a dish-and-panels
// satellite, a capsule) rather than an arbitrary shape you have to learn to
// associate with a category. `{c}` is substituted with the category color.
const CATEGORY_ICONS = {
  booster: `
    <path d="M12 2c2.4 2 3.4 5.4 3.4 9.2 0 2.6-.5 4.9-1.3 6.8h-4.2c-.8-1.9-1.3-4.2-1.3-6.8C8.6 7.4 9.6 4 12 2z" fill="{c}"/>
    <path d="M8.6 13.5 5 17.5l2.4-.6 1.6-2.3z" fill="{c}"/>
    <path d="M15.4 13.5 19 17.5l-2.4-.6-1.6-2.3z" fill="{c}"/>
    <circle cx="12" cy="9" r="1.3" fill="#0e1420"/>
    <path d="M10.4 18h3.2l-.5 3.2a1.1 1.1 0 0 1-2.2 0z" fill="{c}"/>`,
  lander: `
    <rect x="8.5" y="6" width="7" height="7" rx="1.3" fill="{c}"/>
    <circle cx="12" cy="9.5" r="1.6" fill="#0e1420"/>
    <path d="M9 12.5 5.5 20M15 12.5 18.5 20M7.7 12.5 6.2 20M16.3 12.5 17.8 20" stroke="{c}" stroke-width="1.4" fill="none" stroke-linecap="round"/>
    <path d="M4.8 20h2.2M17 20h2.2" stroke="{c}" stroke-width="1.4" stroke-linecap="round"/>`,
  satellite: `
    <rect x="9.3" y="9.3" width="5.4" height="5.4" rx="1" fill="{c}"/>
    <path d="M2.5 6.5 8 9v6l-5.5 2.5z" fill="{c}"/>
    <path d="M21.5 6.5 16 9v6l5.5 2.5z" fill="{c}"/>
    <path d="M15 9 20 4M20 4h-2.6M20 4v2.6" stroke="{c}" stroke-width="1.3" fill="none" stroke-linecap="round" stroke-linejoin="round"/>`,
  station: `
    <rect x="4" y="10.5" width="16" height="3" rx="1" fill="{c}"/>
    <circle cx="12" cy="12" r="4.6" fill="none" stroke="{c}" stroke-width="1.6"/>
    <rect x="10.5" y="2.5" width="3" height="4" rx="1" fill="{c}"/>
    <rect x="10.5" y="17.5" width="3" height="4" rx="1" fill="{c}"/>`,
  capsule: `
    <path d="M8 21 6.5 12A5.5 6 0 0 1 12 3a5.5 6 0 0 1 5.5 9L16 21z" fill="{c}"/>
    <circle cx="12" cy="10.5" r="1.8" fill="#0e1420"/>`,
  probe: `
    <rect x="8.5" y="9" width="7" height="7" rx="1.5" fill="{c}"/>
    <path d="M12 9V4M12 4 9.5 2M12 4l2.5-2" stroke="{c}" stroke-width="1.3" fill="none" stroke-linecap="round"/>
    <path d="M5.5 12h3M15.5 12h3" stroke="{c}" stroke-width="1.4" stroke-linecap="round"/>`,
  docking: `
    <circle cx="12" cy="12" r="9" fill="none" stroke="{c}" stroke-width="2.2"/>
    <circle cx="12" cy="12" r="4" fill="{c}"/>`,
  unknown: `<circle cx="12" cy="12" r="8" fill="{c}"/>`,
};

function svgIconFor(category, color) {
  const body = (CATEGORY_ICONS[category] || CATEGORY_ICONS.unknown).replace(/\{c\}/g, color);
  return `<svg width="16" height="16" viewBox="0 0 24 24">${body}</svg>`;
}

const vesselsEl = document.getElementById("vessels");
const connStatusEl = document.getElementById("conn-status");
const cards = new Map(); // vessel id -> { root, els }
const categorySections = new Map(); // category -> { section, grid }
let lastCategoryOrderKey = ""; // avoids reordering category-section DOM nodes on every unchanged tick
let waypoints = [];
let constellations = [];

function findVesselConstellationId(vesselId) {
  const c = constellations.find(c => c.members.includes(vesselId));
  return c ? c.id : null;
}

const mapLegendEl = document.getElementById("map-legend");

// Layout of the Kerbol system. Planets (direct children of the Sun) use
// their REAL semi-major axis once /api/system loads it, scaled down by
// SMA_SCALE -- true relative proportions, just like zooming out in KSP's
// own tracking-station map, and the schematic `radius` below is only the
// fallback shown before that first loads. Moons keep a small fixed
// schematic radius around their parent regardless: at true interplanetary
// scale a moon's orbit is a fraction of a pixel wide, so real-scale moons
// would just disappear into their planet's dot at any zoom level that also
// shows the rest of the system. ANGLE is always live (from the same
// endpoint) so bodies actually move as game time passes. This (x, y) plane
// layout is handed to map3d.js, which renders it as real 3D (x, 0, z) world
// coordinates -- the actual depth/perspective comes from a real 3D camera
// there now, not a flattening hack here.
const SMA_SCALE = 170000000; // world-units per m, calibrated so Kerbin ~= 80 units
const SYSTEM_TREE = {
  name: "Sun", radius: 0, angle: 0, topLevel: false, children: [
    { name: "Moho", radius: 40, angle: 10, topLevel: true },
    { name: "Eve", radius: 60, angle: 60, topLevel: true, children: [{ name: "Gilly", radius: 12, angle: 0 }] },
    { name: "Kerbin", radius: 80, angle: 120, topLevel: true, children: [
        { name: "Mun", radius: 12, angle: 0 }, { name: "Minmus", radius: 18, angle: 140 },
    ] },
    { name: "Duna", radius: 100, angle: 170, topLevel: true, children: [{ name: "Ike", radius: 12, angle: 0 }] },
    { name: "Dres", radius: 118, angle: 220, topLevel: true },
    { name: "Jool", radius: 138, angle: 270, topLevel: true, children: [
        { name: "Laythe", radius: 10, angle: 0 }, { name: "Vall", radius: 14, angle: 72 },
        { name: "Tylo", radius: 18, angle: 144 }, { name: "Bop", radius: 22, angle: 216 },
        { name: "Pol", radius: 26, angle: 288 },
    ] },
    { name: "Eeloo", radius: 155, angle: 320, topLevel: true },
  ],
};

const ALL_BODY_NAMES = (function flatten(node, out) {
  out.push(node.name);
  for (const child of node.children || []) flatten(child, out);
  return out;
})(SYSTEM_TREE, []);

function findSystemNode(name, node = SYSTEM_TREE) {
  if (node.name === name) return node;
  for (const child of node.children || []) {
    const found = findSystemNode(name, child);
    if (found) return found;
  }
  return null;
}

// Moons only (a topLevel planet's own children), for the moon-transfer
// dropdown -- distinct from ALL_BODY_NAMES, which also includes the
// Sun-orbiting planets themselves.
const MOON_NAMES = SYSTEM_TREE.children.flatMap(planet => (planet.children || []).map(m => m.name));

let liveBodyAngles = new Map(); // name -> angle_deg (absolute, argp+true_anomaly), refreshed from /api/system
let liveBodySma = new Map(); // name -> semi_major_axis_m
let liveBodyRadius = new Map(); // name -> real instantaneous orbital radius (m)
let liveBodyEcc = new Map(); // name -> eccentricity
let liveBodyArgp = new Map(); // name -> argument_of_periapsis_deg

async function refreshSystem() {
  try {
    const res = await fetch("/api/system");
    const bodies = await res.json();
    liveBodyAngles = new Map(bodies.map(b => [b.name, b.angle_deg]));
    liveBodySma = new Map(bodies.filter(b => b.semi_major_axis_m).map(b => [b.name, b.semi_major_axis_m]));
    liveBodyRadius = new Map(bodies.filter(b => b.radius_m).map(b => [b.name, b.radius_m]));
    liveBodyEcc = new Map(bodies.map(b => [b.name, b.eccentricity || 0]));
    liveBodyArgp = new Map(bodies.map(b => [b.name, b.argument_of_periapsis_deg || 0]));
  } catch (e) {
    // kRPC not connected yet or request failed; keep the last known data.
  }
  if (window.Map3D) window.Map3D.setBodies(layoutSystem());
}

// Real instantaneous orbital radius (not the fixed semi-major axis) so a
// visibly eccentric body (Moho, Eeloo, ...) sits where it actually is
// in-game right now, not on a circle it only touches at two points.
function radiusFor(node) {
  if (node.topLevel && liveBodyRadius.has(node.name)) {
    return liveBodyRadius.get(node.name) / SMA_SCALE;
  }
  return node.radius;
}

// World-space layout (Sun at 0,0), handed to map3d.js as (x, 0, z).
// Each entry also carries the real orbit shape (scaled SMA, eccentricity,
// argument of periapsis) for topLevel bodies so map3d.js can draw the
// actual ellipse instead of a circle through the current radius.
function layoutSystem() {
  const positions = new Map(); // name -> {x, y, isMoon, orbitShape}
  function place(node, originX, originY) {
    const angle = liveBodyAngles.has(node.name) ? liveBodyAngles.get(node.name) : node.angle;
    const radius = radiusFor(node);
    const rad = (angle * Math.PI) / 180;
    const x = originX + radius * Math.cos(rad);
    const y = originY + radius * Math.sin(rad);
    let orbitShape = null;
    if (node.topLevel && liveBodySma.has(node.name)) {
      orbitShape = {
        smaScaled: liveBodySma.get(node.name) / SMA_SCALE,
        eccentricity: liveBodyEcc.get(node.name) || 0,
        argpDeg: liveBodyArgp.get(node.name) || 0,
      };
    }
    positions.set(node.name, {
      x, y, isMoon: node.radius < 30 && node.radius > 0,
      originX, originY, orbitShape,
    });
    for (const child of node.children || []) place(child, x, y);
  }
  place(SYSTEM_TREE, 0, 0);
  return positions;
}

async function refreshWaypoints() {
  try {
    const res = await fetch("/api/waypoints");
    waypoints = await res.json();
  } catch (e) {
    // kRPC not connected yet or request failed; keep the last known list.
  }
}

function fmt(n, digits = 0) {
  if (n === undefined || n === null || Number.isNaN(n)) return "-";
  return Number(n).toLocaleString(undefined, { maximumFractionDigits: digits });
}

function buildCard(vessel) {
  const root = document.createElement("div");
  root.className = "vessel-card";

  root.innerHTML = `
    <div class="vessel-header">
      <input class="vessel-name" value="${vessel.name}" />
      <span class="type-icon"></span>
      <select class="vessel-type" title="Same as tagging the core/cockpit part in-game -- either way works"></select>
    </div>
    <div class="vessel-detail">
      <div class="ksp-name" style="font-size:11px;color:var(--dim);margin-bottom:6px;"></div>
      <div class="telemetry">
        <div>Body: <span class="val f-body"></span></div>
        <div>Situation: <span class="val f-situation"></span></div>
        <div>Altitude: <span class="val f-altitude"></span> m</div>
        <div>Speed: <span class="val f-speed"></span> m/s</div>
        <div>Apoapsis: <span class="val f-apoapsis"></span> m</div>
        <div>Periapsis: <span class="val f-periapsis"></span> m</div>
        <div>Inclination: <span class="val f-inclination"></span>&deg;</div>
        <div>Stage: <span class="val f-stage"></span></div>
      </div>
      <div class="roles"></div>
    </div>
    <div class="group-parts">
      <button class="parts-toggle">Parts &#9662;</button>
      <div class="parts-panel" style="display:none;"></div>
    </div>
    <div class="ascent-form group-ascent">
      <button class="ap-start">Launch to 90km orbit</button>
      <button class="ap-abort abort">Abort</button>
    </div>
    <div class="ascent-form group-ascent">
      <input class="ap-custom-alt" type="number" placeholder="altitude km" value="90" />
      <input class="ap-custom-incl" type="number" placeholder="incl deg" value="0" />
      <button class="ap-polar-fill" title="Set inclination to 90&deg;">Polar</button>
      <button class="ap-custom-start">Launch custom orbit</button>
    </div>
    <div class="ascent-form group-landing">
      <select class="landing-waypoint"><option value="">-- waypoint --</option></select>
      <button class="landing-start">Land at waypoint</button>
    </div>
    <div class="ascent-form group-return">
      <button class="booster-return-start">Return to KSC</button>
    </div>
    <div class="group-constellation" style="display:none;">
      <span class="constellation-label">unassigned</span>
      <button class="deploy-start">Deploy</button>
    </div>
    <div class="group-interplanetary">
      <button class="interplanetary-start" disabled>Start Transfer</button>
    </div>
    <div class="group-moon-transfer">
      <select class="moon-transfer-target"></select>
      <input class="moon-transfer-periapsis" type="number" placeholder="periapsis km" value="50" />
      <input class="moon-transfer-incl" type="number" placeholder="incl deg (optional)" />
      <button class="moon-transfer-preview">Preview on map</button>
      <button class="moon-transfer-start">Transfer to moon</button>
    </div>
    <div class="job-status"></div>
  `;

  root.draggable = true;
  root.addEventListener("dragstart", (e) => {
    e.dataTransfer.setData("text/plain", vessel.id);
    root.classList.add("dragging");
  });
  root.addEventListener("dragend", () => root.classList.remove("dragging"));

  const els = {
    name: root.querySelector(".vessel-name"),
    type: root.querySelector(".vessel-type"),
    typeIcon: root.querySelector(".type-icon"),
    kspName: root.querySelector(".ksp-name"),
    body: root.querySelector(".f-body"),
    situation: root.querySelector(".f-situation"),
    altitude: root.querySelector(".f-altitude"),
    speed: root.querySelector(".f-speed"),
    apoapsis: root.querySelector(".f-apoapsis"),
    periapsis: root.querySelector(".f-periapsis"),
    inclination: root.querySelector(".f-inclination"),
    stage: root.querySelector(".f-stage"),
    roles: root.querySelector(".roles"),
    partsToggle: root.querySelector(".parts-toggle"),
    partsPanel: root.querySelector(".parts-panel"),
    apStart: root.querySelector(".ap-start"),
    apAbort: root.querySelector(".ap-abort"),
    apCustomAlt: root.querySelector(".ap-custom-alt"),
    apCustomIncl: root.querySelector(".ap-custom-incl"),
    apPolarFill: root.querySelector(".ap-polar-fill"),
    apCustomStart: root.querySelector(".ap-custom-start"),
    landingWaypoint: root.querySelector(".landing-waypoint"),
    landingStart: root.querySelector(".landing-start"),
    boosterReturnStart: root.querySelector(".booster-return-start"),
    constellationGroup: root.querySelector(".group-constellation"),
    constellationLabel: root.querySelector(".constellation-label"),
    deployStart: root.querySelector(".deploy-start"),
    interplanetaryStart: root.querySelector(".interplanetary-start"),
    moonTransferTarget: root.querySelector(".moon-transfer-target"),
    moonTransferPeriapsis: root.querySelector(".moon-transfer-periapsis"),
    moonTransferIncl: root.querySelector(".moon-transfer-incl"),
    moonTransferPreview: root.querySelector(".moon-transfer-preview"),
    moonTransferStart: root.querySelector(".moon-transfer-start"),
    jobStatus: root.querySelector(".job-status"),
  };

  els.moonTransferTarget.innerHTML = MOON_NAMES.map(n => `<option value="${n}">${n}</option>`).join("");

  els.name.addEventListener("change", () => {
    fetch(`/api/vessels/${vessel.id}/name`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name: els.name.value }),
    });
  });

  els.type.innerHTML = VESSEL_TYPES.map(t => `<option value="${t}">${t}</option>`).join("");
  els.type.addEventListener("change", () => {
    fetch(`/api/vessels/${vessel.id}/role`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ category: els.type.value }),
    });
  });

  function startAscent(altitudeM, inclinationDeg) {
    fetch(`/api/autopilot/${vessel.id}/ascent`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        target_apoapsis_m: altitudeM,
        target_periapsis_m: altitudeM,
        target_inclination_deg: inclinationDeg,
      }),
    });
  }

  els.apStart.addEventListener("click", () => startAscent(90000, 0));

  els.apCustomStart.addEventListener("click", () => {
    startAscent(Number(els.apCustomAlt.value) * 1000, Number(els.apCustomIncl.value));
  });

  els.apPolarFill.addEventListener("click", () => {
    els.apCustomIncl.value = 90;
  });

  els.apAbort.addEventListener("click", () => {
    fetch(`/api/autopilot/${vessel.id}/abort`, { method: "POST" });
  });

  els.landingStart.addEventListener("click", () => {
    const wp = waypoints[Number(els.landingWaypoint.value)];
    if (!wp) return;
    fetch(`/api/autopilot/${vessel.id}/landing`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ target_lat: wp.latitude, target_lon: wp.longitude }),
    });
  });

  els.boosterReturnStart.addEventListener("click", () => {
    fetch(`/api/autopilot/${vessel.id}/booster-return`, { method: "POST" });
  });

  els.deployStart.addEventListener("click", () => {
    const cid = findVesselConstellationId(vessel.id);
    if (cid == null) return;
    fetch(`/api/constellations/${cid}/deploy/${encodeURIComponent(vessel.id)}`, { method: "POST" });
  });

  els.interplanetaryStart.addEventListener("click", () => {
    if (!currentInterplanetaryPlan) return;
    fetch(`/api/autopilot/${vessel.id}/interplanetary`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ plan_text: currentInterplanetaryPlan.text }),
    });
  });

  els.moonTransferStart.addEventListener("click", () => {
    const inclRaw = els.moonTransferIncl.value.trim();
    fetch(`/api/autopilot/${vessel.id}/moon-transfer`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        moon_name: els.moonTransferTarget.value,
        target_periapsis_m: Number(els.moonTransferPeriapsis.value) * 1000,
        target_inclination_deg: inclRaw === "" ? null : Number(inclRaw),
      }),
    });
  });

  els.moonTransferPreview.addEventListener("click", async () => {
    const moonName = els.moonTransferTarget.value;
    els.moonTransferPreview.disabled = true;
    els.moonTransferPreview.textContent = "Calculating...";
    try {
      const res = await fetch(`/api/autopilot/${vessel.id}/moon-transfer/preview?moon_name=${encodeURIComponent(moonName)}`);
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        throw new Error(body.detail || `HTTP ${res.status}`);
      }
      const plan = await res.json();
      const moonNode = findSystemNode(moonName);
      if (!moonNode || !window.Map3D) return;

      // Scale real meters down to this moon's own schematic display radius
      // -- the same fixed-radius-around-Kerbin compression the map already
      // uses for moons, applied consistently to the transfer ellipse too
      // so it visually lines up with where the moon is actually drawn.
      const scale = moonNode.radius / plan.moon_orbital_radius_m;
      const a = (plan.r_peri_m + plan.r_apo_m) / 2 * scale;
      const e = (plan.r_apo_m - plan.r_peri_m) / (plan.r_apo_m + plan.r_peri_m);
      const periapsisRad = (plan.periapsis_angle_deg * Math.PI) / 180;
      const inclRad = (plan.inclination_deg * Math.PI) / 180;

      const points = [];
      const segments = 96;
      for (let i = 0; i <= segments; i++) {
        const trueAnomaly = (i / segments) * Math.PI * 2;
        const r = (a * (1 - e * e)) / (1 + e * Math.cos(trueAnomaly));
        const absAngle = periapsisRad + trueAnomaly;
        const flatX = r * Math.cos(absAngle);
        const flatZ = r * Math.sin(absAngle);
        points.push({ x: flatX, y: flatZ * Math.sin(inclRad), z: flatZ * Math.cos(inclRad) });
      }
      // Arrival point is exactly the apoapsis (trueAnomaly = pi from periapsis).
      const arrivalAbsAngle = periapsisRad + Math.PI;
      const arrivalMarker = {
        x: (plan.r_apo_m * scale) * Math.cos(arrivalAbsAngle),
        y: 0,
        z: (plan.r_apo_m * scale) * Math.sin(arrivalAbsAngle),
      };

      window.Map3D.showTransferPreview("Kerbin", points, arrivalMarker);
      els.moonTransferPreview.textContent = `Preview: burn in ${fmt(plan.burn_in_s)}s, arrive in ${fmt(plan.arrival_in_s)}s`;
    } catch (e) {
      els.moonTransferPreview.textContent = `Preview failed: ${e.message}`;
    } finally {
      els.moonTransferPreview.disabled = false;
      setTimeout(() => { els.moonTransferPreview.textContent = "Preview on map"; }, 4000);
    }
  });

  els.partsToggle.addEventListener("click", () => {
    const opening = els.partsPanel.style.display === "none";
    els.partsPanel.style.display = opening ? "block" : "none";
    if (opening) loadParts(vessel.id, els.partsPanel);
  });

  return { root, els };
}

async function loadParts(vesselId, panelEl) {
  // Read-only: role assignment happens in-game (right-click a part -> Tag),
  // not here -- a flat list on a web page can't show which physical part
  // is which when a craft has several identical ones (e.g. four legs).
  panelEl.innerHTML = `<div style="color:var(--dim);font-size:12px;">loading parts...</div>`;
  let parts;
  try {
    const res = await fetch(`/api/vessels/${vesselId}/parts`);
    parts = await res.json();
  } catch (e) {
    panelEl.innerHTML = `<div style="color:var(--bad);font-size:12px;">failed to load parts</div>`;
    return;
  }

  const table = document.createElement("div");
  table.style.cssText = "display:flex;flex-direction:column;gap:4px;max-height:260px;overflow-y:auto;margin:8px 0;";

  parts.forEach((part) => {
    const hint = [
      part.is_engine && "engine",
      part.is_decoupler && "decoupler",
      part.is_docking_port && "dock",
    ].filter(Boolean).join("/");

    const row = document.createElement("div");
    row.style.cssText = "display:flex;align-items:center;gap:6px;font-size:11px;";
    row.innerHTML = `
      <span style="flex:1;color:var(--text);" title="${part.name}">${part.title} <span style="color:var(--dim);">(stage ${part.stage}${hint ? ", " + hint : ""})</span></span>
      <span style="color:${part.tag ? "var(--accent)" : "var(--dim)"};">${part.tag || "untagged"}</span>
    `;
    table.appendChild(row);
  });

  panelEl.innerHTML = "";
  panelEl.appendChild(table);
}

function updateCard(card, vessel) {
  const { els, root } = card;
  root.classList.toggle("active", !!vessel.is_active);

  if (document.activeElement !== els.name) els.name.value = vessel.name;
  const typeColor = CATEGORY_COLORS[vessel.type] || "var(--dim)";
  els.typeIcon.innerHTML = svgIconFor(vessel.type, typeColor);
  els.typeIcon.title = vessel.role_detail || "";
  if (document.activeElement !== els.type) els.type.value = vessel.type;
  els.kspName.textContent = `in-game name: ${vessel.ksp_name}`;

  const t = vessel.telemetry || {};
  // Drives the Launch/Booster Landing tab's per-craft control split (see
  // style.css): launch controls only make sense for a craft still on the
  // surface, return/burn-up only for a booster that's left the pad.
  const grounded = ["pre_launch", "landed", "splashed"].includes(t.situation);
  root.dataset.grounded = grounded ? "true" : "false";
  root.dataset.roleBooster = vessel.type === "booster" ? "true" : "false";

  if (t.error) {
    els.situation.textContent = "unreachable";
  } else {
    els.body.textContent = t.body || "-";
    els.situation.textContent = t.situation || "-";
    els.altitude.textContent = fmt(t.altitude);
    els.speed.textContent = fmt(t.speed, 1);
    els.apoapsis.textContent = fmt(t.apoapsis_altitude);
    els.periapsis.textContent = fmt(t.periapsis_altitude);
    els.inclination.textContent = fmt(t.inclination_deg, 1);
    els.stage.textContent = t.stage ?? "-";
  }

  const roleEntries = Object.entries(vessel.roles || {});
  els.roles.innerHTML = roleEntries.length
    ? "Tagged roles: " + roleEntries.map(([cat, details]) => `<code>${cat}: ${details.join(", ")}</code>`).join(" ")
    : "No tagged roles (using auto-detection fallback)";

  if (vessel.type === "satellite") {
    els.constellationGroup.style.display = "flex";
    const constellation = constellations.find(c => c.members.includes(vessel.id));
    els.constellationLabel.textContent = constellation ? constellation.name : "unassigned";
    els.deployStart.disabled = !constellation;
  } else {
    els.constellationGroup.style.display = "none";
  }

  // Interplanetary tab: hide craft already assigned to a satellite
  // constellation (that's the Satellites tab's job) and disable the
  // transfer button until a plan has actually been parsed.
  root.dataset.inConstellation = findVesselConstellationId(vessel.id) != null ? "true" : "false";
  els.interplanetaryStart.disabled = !currentInterplanetaryPlan;

  if (document.activeElement !== els.landingWaypoint && els.landingWaypoint.options.length - 1 !== waypoints.length) {
    const current = els.landingWaypoint.value;
    els.landingWaypoint.innerHTML = '<option value="">-- waypoint --</option>' +
      waypoints.map((wp, i) => `<option value="${i}">${wp.name} (${wp.body})</option>`).join("");
    els.landingWaypoint.value = current;
  }

  const job = vessel.autopilot;
  if (job) {
    els.jobStatus.textContent = `${job.kind}: ${job.status} - ${job.message}`;
    els.jobStatus.className = `job-status ${job.status}`;
  } else {
    els.jobStatus.textContent = "";
    els.jobStatus.className = "job-status";
  }
}

function getCategorySection(category) {
  let entry = categorySections.get(category);
  if (!entry) {
    const section = document.createElement("div");
    section.className = `category-section category-${category}`;
    section.innerHTML = `<h3 class="category-header">${CATEGORY_LABELS[category] || category}</h3>`;
    const grid = document.createElement("div");
    grid.className = "category-grid";
    section.appendChild(grid);
    vesselsEl.appendChild(section);
    entry = { section, grid };
    categorySections.set(category, entry);
  }
  return entry;
}

function render(vessels) {
  const seen = new Set();
  const usedCategories = new Set();

  for (const vessel of vessels) {
    seen.add(vessel.id);
    let card = cards.get(vessel.id);
    if (!card) {
      card = buildCard(vessel);
      cards.set(vessel.id, card);
    }
    updateCard(card, vessel);

    const category = VESSEL_TYPES.includes(vessel.type) ? vessel.type : "unknown";
    usedCategories.add(category);
    const { grid } = getCategorySection(category);
    if (card.root.parentElement !== grid) grid.appendChild(card.root);
  }

  for (const [id, card] of cards) {
    if (!seen.has(id)) {
      card.root.remove();
      cards.delete(id);
    }
  }

  for (const [category, { section }] of categorySections) {
    section.style.display = usedCategories.has(category) ? "block" : "none";
  }

  // Enforce a stable top-down row order (CATEGORY_ORDER) regardless of the
  // order categories were first encountered in. Only actually touch the DOM
  // when the set of visible categories has changed since the last render --
  // appendChild every single tick (this runs on every telemetry update,
  // ~2x/sec) was reordering the whole page continuously even when nothing
  // changed, which could interrupt an open dropdown (e.g. the role
  // selector) mid-interaction. Confirmed as the cause of the dashboard
  // "glitching" whenever a select was open during a telemetry tick.
  const orderKey = CATEGORY_ORDER.filter(c => categorySections.has(c)).join(",");
  if (orderKey !== lastCategoryOrderKey) {
    lastCategoryOrderKey = orderKey;
    for (const category of CATEGORY_ORDER) {
      const entry = categorySections.get(category);
      if (entry) vesselsEl.appendChild(entry.section);
    }
  }

  lastVessels = vessels;
  if (window.Map3D) window.Map3D.setVessels(vessels);

  const usedForLegend = new Set(vessels.map(v => (VESSEL_TYPES.includes(v.type) ? v.type : "unknown")));
  mapLegendEl.innerHTML = CATEGORY_ORDER
    .filter(c => usedForLegend.has(c))
    .map(c => `<div>${svgIconFor(c, CATEGORY_COLORS[c])} ${CATEGORY_LABELS[c]}</div>`)
    .join("");
}

let lastVessels = [];

document.getElementById("map-reset").addEventListener("click", () => {
  if (window.Map3D) window.Map3D.resetView();
});

function connect() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${proto}://${location.host}/ws/telemetry`);

  ws.onopen = () => {
    connStatusEl.textContent = "connected";
    connStatusEl.className = "ok";
  };
  ws.onclose = () => {
    connStatusEl.textContent = "disconnected, retrying...";
    connStatusEl.className = "bad";
    setTimeout(connect, 2000);
  };
  ws.onerror = () => ws.close();
  ws.onmessage = (event) => {
    const data = JSON.parse(event.data);
    if (data.krpc_connected) {
      connStatusEl.textContent = "connected to KSP";
      connStatusEl.className = "ok";
    } else {
      connStatusEl.textContent = "waiting for KSP (start the game + kRPC server)";
      connStatusEl.className = "bad";
    }
    render(data.vessels || []);
  };
}

const PLACEHOLDER_TEXT = {
  docking: "Auto-docking, refueling, and cargo transfer aren't built yet -- staying a placeholder for now, since testing this properly needs a bigger multi-vessel setup (a station/target plus a docking-capable craft) than what's been used for the earlier passes.",
};

const placeholderEl = document.getElementById("placeholder");
document.querySelectorAll("#tabs .tab-btn").forEach((btn) => {
  btn.addEventListener("click", () => {
    document.querySelectorAll("#tabs .tab-btn").forEach((b) => b.classList.remove("active"));
    btn.classList.add("active");
    const tab = btn.dataset.tab;
    document.body.dataset.tab = tab;
    if (PLACEHOLDER_TEXT[tab]) {
      placeholderEl.textContent = PLACEHOLDER_TEXT[tab];
      placeholderEl.style.display = "block";
    } else {
      placeholderEl.style.display = "none";
    }
  });
});

// --- Constellations ---

const constellationsListEl = document.getElementById("constellations-list");
const ncForm = document.getElementById("new-constellation-form");
const ncBodySelect = document.getElementById("nc-body");
const ncKindSelect = document.getElementById("nc-kind");
const ncAltitude = document.getElementById("nc-altitude");
const ncInclination = document.getElementById("nc-inclination");

ncBodySelect.innerHTML = ALL_BODY_NAMES.map(n => `<option value="${n}">${n}</option>`).join("");

document.getElementById("new-constellation-btn").addEventListener("click", () => {
  ncForm.style.display = ncForm.style.display === "none" ? "flex" : "none";
});
document.getElementById("nc-cancel").addEventListener("click", () => {
  ncForm.style.display = "none";
  ncForm.reset();
});
ncKindSelect.addEventListener("change", () => {
  const isCustom = ncKindSelect.value === "custom";
  ncAltitude.style.display = isCustom ? "inline-block" : "none";
  ncInclination.style.display = isCustom ? "inline-block" : "none";
});

ncForm.addEventListener("submit", async (e) => {
  e.preventDefault();
  const kind = ncKindSelect.value;
  await fetch("/api/constellations", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      name: document.getElementById("nc-name").value,
      body: ncBodySelect.value,
      kind,
      altitude_m: kind === "custom" ? Number(ncAltitude.value) * 1000 : null,
      inclination_deg: kind === "custom" ? Number(ncInclination.value) : 0,
    }),
  });
  ncForm.reset();
  ncForm.style.display = "none";
  refreshConstellations();
});

let lastConstellationsJson = "";

async function refreshConstellations() {
  let fresh;
  try {
    const res = await fetch("/api/constellations");
    fresh = await res.json();
  } catch (e) {
    return;
  }
  // The whole list gets rebuilt from scratch on render (members, drag
  // targets, etc.), which was wiping out any open edit-orbit form on every
  // 5s poll even when nothing had actually changed -- confirmed as the
  // cause of the edit menu "instantly hiding" right after opening it. Skip
  // the rebuild entirely when the data is byte-for-byte the same as last
  // time, so an open form only gets disturbed by a render that has an
  // actual reason to happen (a real membership/orbit change).
  const freshJson = JSON.stringify(fresh);
  if (freshJson === lastConstellationsJson) return;
  lastConstellationsJson = freshJson;
  constellations = fresh;
  renderConstellations();
}

function renderConstellations() {
  constellationsListEl.innerHTML = "";
  for (const c of constellations) {
    const box = document.createElement("div");
    box.className = "constellation-box";
    const altKm = c.kind === "communications" ? "geostationary" : `${Math.round(c.altitude_m / 1000)} km`;
    const isCustom = c.kind === "custom";
    box.innerHTML = `
      <button class="delete-btn" title="Delete constellation">&times;</button>
      ${isCustom ? '<button class="edit-orbit-btn" title="Edit orbit">&#9998;</button>' : ""}
      <h4>${c.name}</h4>
      <div class="meta">${c.body} &middot; ${c.kind} &middot; ${altKm}${isCustom ? `, ${c.inclination_deg}&deg;` : ""}</div>
      ${isCustom ? `
        <div class="edit-orbit-form" style="display:none;">
          <input class="edit-orbit-alt" type="number" placeholder="altitude km" value="${Math.round(c.altitude_m / 1000)}" />
          <input class="edit-orbit-incl" type="number" placeholder="inclination deg" value="${c.inclination_deg}" />
          <button class="edit-orbit-save">Save</button>
        </div>
      ` : ""}
      <div class="members"></div>
    `;
    const membersEl = box.querySelector(".members");
    if (c.members.length === 0) {
      membersEl.innerHTML = `<div style="color:var(--dim);font-size:11px;">drop a satellite here</div>`;
    } else {
      for (const vesselId of c.members) {
        const card = cards.get(vesselId);
        const name = card ? card.els.name.value : vesselId;
        const row = document.createElement("div");
        row.className = "member";
        row.innerHTML = `<span>${name}</span><button title="Remove">&times;</button>`;
        row.querySelector("button").addEventListener("click", async () => {
          await fetch(`/api/constellations/${c.id}/members/${encodeURIComponent(vesselId)}`, { method: "DELETE" });
          refreshConstellations();
        });
        membersEl.appendChild(row);
      }
    }

    box.querySelector(".delete-btn").addEventListener("click", async () => {
      await fetch(`/api/constellations/${c.id}`, { method: "DELETE" });
      refreshConstellations();
    });

    if (isCustom) {
      const editForm = box.querySelector(".edit-orbit-form");
      box.querySelector(".edit-orbit-btn").addEventListener("click", () => {
        editForm.style.display = editForm.style.display === "none" ? "flex" : "none";
      });
      box.querySelector(".edit-orbit-save").addEventListener("click", async () => {
        const altitude_m = Number(box.querySelector(".edit-orbit-alt").value) * 1000;
        const inclination_deg = Number(box.querySelector(".edit-orbit-incl").value);
        await fetch(`/api/constellations/${c.id}/orbit`, {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ altitude_m, inclination_deg }),
        });
        refreshConstellations();
      });
    }

    box.addEventListener("dragover", (e) => {
      e.preventDefault();
      box.classList.add("drag-over");
    });
    box.addEventListener("dragleave", () => box.classList.remove("drag-over"));
    box.addEventListener("drop", async (e) => {
      e.preventDefault();
      box.classList.remove("drag-over");
      const vesselId = e.dataTransfer.getData("text/plain");
      if (!vesselId) return;
      await fetch(`/api/constellations/${c.id}/members`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ vessel_id: vesselId }),
      });
      refreshConstellations();
    });

    constellationsListEl.appendChild(box);
  }
}

const syncSourceUrl = document.getElementById("sync-source-url");
const syncPullBtn = document.getElementById("sync-pull-btn");
const syncStatus = document.getElementById("sync-status");

syncPullBtn.addEventListener("click", async () => {
  const sourceUrl = syncSourceUrl.value.trim();
  if (!sourceUrl) return;
  syncStatus.textContent = "pulling...";
  syncStatus.className = "";
  try {
    const res = await fetch("/api/constellations/sync/pull", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ source_url: sourceUrl }),
    });
    const data = await res.json();
    if (!res.ok) {
      syncStatus.textContent = data.detail || "sync failed";
      syncStatus.className = "bad";
      return;
    }
    syncStatus.textContent = `merged ${data.merged.length} constellation(s)`;
    syncStatus.className = "ok";
    lastConstellationsJson = ""; // force the next poll to actually re-render
    refreshConstellations();
  } catch (e) {
    syncStatus.textContent = "request failed";
    syncStatus.className = "bad";
  }
});

// --- Interplanetary ---

let currentInterplanetaryPlan = null; // { text, sequence, steps } once successfully parsed

const ipPlanText = document.getElementById("ip-plan-text");
const ipParseBtn = document.getElementById("ip-parse-btn");
const ipParseStatus = document.getElementById("ip-parse-status");
const ipPreview = document.getElementById("ip-preview");

function renderInterplanetaryPreview(plan) {
  const rows = plan.steps.map((s) => {
    if (s.type === "flyby") {
      return `<tr><td>${s.name}</td><td>flyby</td><td>periapsis ${s.periapsis_km} km, incl ${s.inclination_deg}&deg;</td></tr>`;
    }
    return `<tr><td>${s.name}</td><td>burn</td><td>prograde ${s.prograde}, normal ${s.normal}, radial ${s.radial} m/s</td></tr>`;
  }).join("");
  ipPreview.innerHTML = `
    <div class="ip-sequence">${plan.sequence || "(sequence)"}</div>
    <table>
      <thead><tr><th>Step</th><th>Type</th><th>Detail</th></tr></thead>
      <tbody>${rows}</tbody>
    </table>
  `;
  ipPreview.style.display = "block";
}

ipParseBtn.addEventListener("click", async () => {
  ipParseStatus.textContent = "parsing...";
  ipParseStatus.className = "";
  try {
    const res = await fetch("/api/autopilot/interplanetary/parse", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ plan_text: ipPlanText.value }),
    });
    const data = await res.json();
    if (!res.ok) {
      currentInterplanetaryPlan = null;
      ipParseStatus.textContent = data.detail || "failed to parse plan";
      ipParseStatus.className = "bad";
      ipPreview.style.display = "none";
      return;
    }
    currentInterplanetaryPlan = { text: ipPlanText.value, sequence: data.sequence, steps: data.steps };
    ipParseStatus.textContent = `parsed ${data.steps.length} step(s) -- ready to start on a craft below`;
    ipParseStatus.className = "ok";
    renderInterplanetaryPreview(currentInterplanetaryPlan);
  } catch (e) {
    currentInterplanetaryPlan = null;
    ipParseStatus.textContent = "request failed";
    ipParseStatus.className = "bad";
  }
});

// --- Save profile ---

const profileSelect = document.getElementById("profile-select");
const profileNewInput = document.getElementById("profile-new");
const profileSwitchBtn = document.getElementById("profile-switch-btn");

async function refreshProfile() {
  try {
    const res = await fetch("/api/profile");
    const data = await res.json();
    profileSelect.innerHTML = data.profiles.map(p => `<option value="${p}">${p}</option>`).join("");
    profileSelect.value = data.active;
  } catch (e) {
    // kRPC-independent (pure sqlite), but the server itself may not be up yet
  }
}

profileSwitchBtn.addEventListener("click", async () => {
  const name = profileNewInput.value.trim() || profileSelect.value;
  if (!name) return;
  await fetch("/api/profile", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name }),
  });
  profileNewInput.value = "";
  refreshProfile();
  refreshConstellations();
});

refreshProfile();
refreshWaypoints();
refreshSystem();
refreshConstellations();
setInterval(refreshWaypoints, 5000);
setInterval(refreshSystem, 5000);
setInterval(refreshConstellations, 5000);
connect();
