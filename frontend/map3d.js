// Real 3D System Overview using Three.js. Exposes a small API on
// window.Map3D that app.js (a plain classic script, not a module) calls
// into: init() once, then setBodies()/setVessels() every time fresh data
// arrives over the existing telemetry WebSocket / system-info poll -- no
// separate network traffic of its own, this only renders what app.js
// already fetches.
//
// Bodies are placed using the exact same flat top-down layout app.js's own
// layoutSystem() already computes (x/y in schematic-or-real-scale units) --
// just reinterpreted as 3D (x, 0, z) world coordinates, so the actual
// distances/scaling logic isn't duplicated here. Vessels get a real 3D
// position: an ellipse in a plane tilted by the vessel's own orbital
// inclination around its parent body, so an inclined orbit visibly tilts
// out of the ecliptic instead of always lying flat.
import * as THREE from "three";
import { OrbitControls } from "three/addons/controls/OrbitControls.js";

const CATEGORY_COLORS = {
  booster: 0xff8a4d, satellite: 0x4da3ff, docking: 0xf472b6, station: 0xc084fc,
  capsule: 0x4dd28c, lander: 0xffd24d, probe: 0x7d8aa8, unknown: 0xdbe4f5,
};

// Approximate real coloring for each stock body, so the map reads at a
// glance instead of every planet/moon being the same generic grey-blue
// sphere. Anything not listed (custom/modded bodies) falls back to a
// neutral grey.
const BODY_COLORS = {
  Moho: 0x9c8060,
  Eve: 0x9a5fc0,
  Gilly: 0x8a8478,
  Kerbin: 0x4d97c9,
  Mun: 0xa8a8a4,
  Minmus: 0x7fe0b8,
  Duna: 0xc1602c,
  Ike: 0x9a9088,
  Dres: 0x8a7864,
  Jool: 0x6fae4a,
  Laythe: 0x3f7fbf,
  Vall: 0xcfe6ec,
  Tylo: 0xb8b4ac,
  Bop: 0x8a6a4a,
  Pol: 0xd4b988,
  Eeloo: 0xe8e6e2,
};
const DEFAULT_BODY_COLOR = 0x9aa4bf;

// --- Object sizing ------------------------------------------------------
//
// Everything drawn here is scaled every frame to hold a roughly constant
// SIZE ON SCREEN, rather than a constant size in world units.
//
// Fixed world sizes cannot work on a map like this, because the map spans
// four orders of magnitude. Planet orbits run from Moho at 31 world units
// out to Eeloo at 530, while a planet sphere was a fixed radius of 3.2 and
// a vessel cone 2.8 long. Framing the whole system therefore drew planets
// about 2px across, moons about 1px, and vessels under a pixel -- i.e.
// invisible, which is exactly when you most want to see where everything
// is. Zooming in to one planet had the opposite problem: the same fixed
// sphere swelled to fill the view and swallowed its own moons.
//
// Scaling by distance from the camera fixes both ends at once, and is what
// KSP's own tracking station does. BASE_SIZES are relative visual weights,
// not physical radii -- the real ones differ by far too much to draw
// literally (Jool's radius is 100x Gilly's), but a gas giant should still
// read as bigger than Moho at a glance.
const ANGULAR_SIZE = 0.014; // world units of radius per unit of camera distance
const MIN_WORLD_SIZE = 0.6; // stops objects vanishing when the camera is right on top
const MAX_WORLD_SIZE = 14; // stops a close-up planet swallowing the whole view

const GAS_GIANTS = new Set(["Jool"]);
const BASE_SIZES = { sun: 2.6, gasGiant: 1.7, planet: 1.0, moon: 0.62, vessel: 0.42 };

function baseSizeForBody(name, isMoon) {
  if (isMoon) return BASE_SIZES.moon;
  if (GAS_GIANTS.has(name)) return BASE_SIZES.gasGiant;
  return BASE_SIZES.planet;
}

/**
 * Resize one object so it subtends about the same angle regardless of how
 * far away the camera is. `extra` is a multiplier for transient emphasis,
 * e.g. highlighting the active vessel.
 */
function applyScreenSize(mesh, baseSize, extra = 1) {
  const distance = camera.position.distanceTo(mesh.position);
  const size = Math.min(MAX_WORLD_SIZE, Math.max(MIN_WORLD_SIZE, distance * ANGULAR_SIZE));
  mesh.scale.setScalar(size * baseSize * extra);
}

/** Rescale everything in the scene. Called once per frame from animate(). */
function updateObjectScales() {
  if (sunMesh) applyScreenSize(sunMesh, BASE_SIZES.sun);
  for (const entry of bodyMeshes.values()) {
    applyScreenSize(entry.mesh, entry.baseSize);
  }
  for (const entry of vesselIcons.values()) {
    applyScreenSize(entry.mesh, BASE_SIZES.vessel, entry.activeBoost || 1);
  }
}

let scene, camera, renderer, controls, container;
// The map is built lazily, when the Overview tab first mounts its
// container -- so every public entry point has to tolerate being called
// before that has happened (another tab polling telemetry, say).
let ready = false;
let resizeObserver = null;
let sunMesh;
const bodyMeshes = new Map(); // name -> {mesh, ring}
const vesselIcons = new Map(); // vessel id -> {mesh}
let hoverTargets = []; // [{mesh, vessel}]
const raycaster = new THREE.Raycaster();
raycaster.params.Points = { threshold: 4 };
const pointer = new THREE.Vector2();
let lastPointerEvent = null;
let lastVesselList = [];

// Resolved in init(), not at module load: like the canvas container, this
// element belongs to the Overview tab and doesn't exist until it mounts.
let tooltipEl = null;

function fmt(n, digits = 0) {
  if (n === undefined || n === null || Number.isNaN(n)) return "-";
  return Number(n).toLocaleString(undefined, { maximumFractionDigits: digits });
}

// The canvas must track the container's real, current size -- never a
// hardcoded guess. Two ways that used to go wrong:
//
//   * init() ran at page load and read clientWidth. If the map was hidden
//     or the layout hadn't settled, that reads 0, so it fell back to a
//     hardcoded 700 and stayed 700px wide until something happened to
//     trigger a window resize.
//   * The only resize listener was on `window`. But the container also
//     changes width when a tab switch shows/hides panels around it, or on
//     any other reflow -- none of which fire a window resize event, so the
//     canvas kept rendering at a stale size and looked stretched.
//
// measure() reads both dimensions off the element (height included, rather
// than duplicating the CSS value in JS), and a ResizeObserver below reacts
// to the size changing for any reason at all.
function measure() {
  const width = container.clientWidth;
  const height = container.clientHeight;
  return { width, height, valid: width > 0 && height > 0 };
}

/**
 * Move the existing renderer into a freshly-created container.
 *
 * The Overview tab is unmounted when you switch away, which clears its DOM
 * -- taking the canvas with it -- and mounts a brand new container element
 * when you come back. Rebuilding the whole scene each time would be
 * wasteful and would lose the camera position, so the renderer is kept and
 * simply re-parented. Without this the map silently vanished for good
 * after the first tab switch: init() saw `ready` and returned early, so
 * nothing ever put the canvas back.
 */
function reattach(element) {
  container = element;
  tooltipEl = document.getElementById("map-tooltip");
  element.appendChild(renderer.domElement);
  if (resizeObserver) {
    resizeObserver.disconnect();
    resizeObserver.observe(element);
  }
  onResize();
}

function init() {
  const element = document.getElementById("map3d-container");
  // The map lives inside the Overview tab, which is mounted on demand, so
  // this can legitimately be called before the container exists. Report
  // failure instead of throwing; the caller retries once the tab mounts.
  if (!element) return false;

  if (ready) {
    if (element !== container) reattach(element);
    return true;
  }

  container = element;
  tooltipEl = document.getElementById("map-tooltip");

  // Fall back only for the very first frame, when the element may not be
  // laid out yet; the ResizeObserver corrects it as soon as it is.
  const { width: w0, height: h0, valid } = measure();
  const width = valid ? w0 : 700;
  const height = valid ? h0 : 350;

  scene = new THREE.Scene();
  scene.background = new THREE.Color(0x060a12);

  camera = new THREE.PerspectiveCamera(45, width / height, 0.1, 100000);
  camera.position.set(0, 220, 320);

  renderer = new THREE.WebGLRenderer({ antialias: true });
  renderer.setSize(width, height);
  renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
  container.appendChild(renderer.domElement);

  controls = new OrbitControls(camera, renderer.domElement);
  controls.enableDamping = true;
  controls.dampingFactor = 0.08;
  controls.screenSpacePanning = false;
  controls.minDistance = 20;
  controls.maxDistance = 8000;
  controls.target.set(0, 0, 0);

  scene.add(new THREE.AmbientLight(0x556677, 1.2));
  const sunLight = new THREE.PointLight(0xffe9a8, 2.5, 0, 0);
  scene.add(sunLight);

  // Sun
  sunMesh = new THREE.Mesh(
    new THREE.SphereGeometry(1, 24, 24),
    new THREE.MeshBasicMaterial({ color: 0xffe9a8 }),
  );
  scene.add(sunMesh);

  // Starfield
  const starGeo = new THREE.BufferGeometry();
  const starCount = 800;
  const positions = new Float32Array(starCount * 3);
  for (let i = 0; i < starCount; i++) {
    const r = 3000 + Math.random() * 4000;
    const theta = Math.random() * Math.PI * 2;
    const phi = Math.acos(2 * Math.random() - 1);
    positions[i * 3] = r * Math.sin(phi) * Math.cos(theta);
    positions[i * 3 + 1] = r * Math.cos(phi);
    positions[i * 3 + 2] = r * Math.sin(phi) * Math.sin(theta);
  }
  starGeo.setAttribute("position", new THREE.BufferAttribute(positions, 3));
  scene.add(new THREE.Points(starGeo, new THREE.PointsMaterial({ color: 0x8fa0c9, size: 1.2, sizeAttenuation: false })));

  renderer.domElement.addEventListener("pointermove", onPointerMove);
  renderer.domElement.addEventListener("pointerleave", () => {
    tooltipEl.style.display = "none";
    lastPointerEvent = null;
  });
  // Fires whenever the container's box changes for ANY reason -- window
  // resize, tab switch revealing it, a panel above it appearing, the
  // browser zoom changing. A window-resize listener catches only the first
  // of those.
  if (typeof ResizeObserver !== "undefined") {
    resizeObserver = new ResizeObserver(onResize);
    resizeObserver.observe(container);
  } else {
    window.addEventListener("resize", onResize);
  }

  ready = true;
  animate();
  return true;
}

function onResize() {
  if (!container || !renderer) return;
  const { width, height, valid } = measure();
  // A hidden element measures 0x0. Skip rather than baking in a degenerate
  // aspect ratio -- the observer fires again with real numbers the moment
  // it becomes visible.
  if (!valid) return;
  camera.aspect = width / height;
  camera.updateProjectionMatrix();
  renderer.setSize(width, height);
}

function onPointerMove(e) {
  const rect = renderer.domElement.getBoundingClientRect();
  pointer.x = ((e.clientX - rect.left) / rect.width) * 2 - 1;
  pointer.y = -((e.clientY - rect.top) / rect.height) * 2 + 1;
  lastPointerEvent = { clientX: e.clientX, clientY: e.clientY };
}

function updateHover() {
  if (!lastPointerEvent) return;
  raycaster.setFromCamera(pointer, camera);
  const meshes = hoverTargets.map(t => t.mesh);
  const hits = raycaster.intersectObjects(meshes);
  if (hits.length === 0) {
    tooltipEl.style.display = "none";
    return;
  }
  const hit = hoverTargets.find(t => t.mesh === hits[0].object);
  if (!hit) {
    tooltipEl.style.display = "none";
    return;
  }
  const t = hit.vessel.telemetry || {};
  tooltipEl.innerHTML = `
    <strong>${hit.vessel.name}</strong><br>
    ${t.body || "-"} &middot; ${t.situation || "-"}<br>
    Alt: <span class="val">${fmt(t.altitude)}</span> m &nbsp; Speed: <span class="val">${fmt(t.speed, 1)}</span> m/s<br>
    Ap/Pe: <span class="val">${fmt(t.apoapsis_altitude)}</span> / <span class="val">${fmt(t.periapsis_altitude)}</span> m
  `;
  const containerRect = container.getBoundingClientRect();
  tooltipEl.style.display = "block";
  tooltipEl.style.left = `${lastPointerEvent.clientX - containerRect.left + 12}px`;
  tooltipEl.style.top = `${lastPointerEvent.clientY - containerRect.top - 8}px`;
}

function animate() {
  requestAnimationFrame(animate);
  controls.update();
  // Must run after controls.update() (the camera may have just moved) and
  // before rendering, so sizes match the frame actually drawn.
  updateObjectScales();
  updateHover();
  renderer.render(scene, camera);
}

function resetView() {
  if (!ready) return;

  camera.position.set(0, 220, 320);
  controls.target.set(0, 0, 0);
  controls.update();
}

// positions: Map(name -> {x, y, isMoon, originX, originY, orbitShape}) from
// app.js's layoutSystem() -- (x, y) here is reinterpreted as the (x, z)
// ground-plane coordinate. When orbitShape is present (topLevel planets,
// once /api/system has loaded real orbital elements) the ring is drawn as
// the body's actual ellipse -- true semi-major axis, eccentricity, and
// periapsis orientation -- instead of a circle through wherever the body
// currently sits, which only looks right for a near-circular orbit.
function ellipseRingPoints(smaScaled, eccentricity, argpDeg) {
  const segments = 128;
  const points = [];
  const argpRad = (argpDeg * Math.PI) / 180;
  for (let i = 0; i <= segments; i++) {
    const trueAnomaly = (i / segments) * Math.PI * 2;
    const r = (smaScaled * (1 - eccentricity * eccentricity)) / (1 + eccentricity * Math.cos(trueAnomaly));
    const absoluteAngle = argpRad + trueAnomaly;
    points.push(new THREE.Vector3(r * Math.cos(absoluteAngle), 0, r * Math.sin(absoluteAngle)));
  }
  return points;
}

function setBodies(positions) {
  if (!ready) return;

  for (const [name, pos] of positions) {
    if (name === "Sun") continue;
    let entry = bodyMeshes.get(name);
    if (!entry) {
      // Unit radius: actual on-screen size comes from the per-frame scale
      // in updateObjectScales(), so the geometry is just a unit shape.
      const color = BODY_COLORS[name] !== undefined ? BODY_COLORS[name] : DEFAULT_BODY_COLOR;
      const mesh = new THREE.Mesh(
        new THREE.SphereGeometry(1, 20, 20),
        new THREE.MeshStandardMaterial({ color, roughness: 0.75, emissive: color, emissiveIntensity: 0.12 }),
      );
      scene.add(mesh);

      const ringGeo = new THREE.BufferGeometry();
      const ring = new THREE.Line(ringGeo, new THREE.LineBasicMaterial({ color, transparent: true, opacity: 0.45 }));
      scene.add(ring);

      entry = { mesh, ring, ringKind: null, baseSize: baseSizeForBody(name, pos.isMoon) };
      bodyMeshes.set(name, entry);
    }
    entry.mesh.position.set(pos.x, 0, pos.y);

    if (pos.orbitShape) {
      // Real ellipse, centered on the parent body (origin), not on this
      // body's own position -- an ellipse isn't centered on either focus.
      const key = `${pos.orbitShape.smaScaled}|${pos.orbitShape.eccentricity}|${pos.orbitShape.argpDeg}`;
      if (entry.ringKind !== key) {
        const pts = ellipseRingPoints(pos.orbitShape.smaScaled, pos.orbitShape.eccentricity, pos.orbitShape.argpDeg);
        entry.ring.geometry.dispose();
        entry.ring.geometry = new THREE.BufferGeometry().setFromPoints(pts);
        entry.ringKind = key;
      }
      entry.ring.position.set(pos.originX, 0, pos.originY);
      entry.ring.scale.set(1, 1, 1);
    } else {
      // Moons: schematic circle around their parent, as before.
      if (entry.ringKind !== "circle") {
        const pts = [];
        const segments = 128;
        for (let i = 0; i <= segments; i++) {
          const a = (i / segments) * Math.PI * 2;
          pts.push(new THREE.Vector3(Math.cos(a), 0, Math.sin(a)));
        }
        entry.ring.geometry.dispose();
        entry.ring.geometry = new THREE.BufferGeometry().setFromPoints(pts);
        entry.ringKind = "circle";
      }
      entry.ring.position.set(pos.originX, 0, pos.originY);
      const orbitRadius = Math.hypot(pos.x - pos.originX, pos.y - pos.originY) || 0.001;
      entry.ring.scale.set(orbitRadius, 1, orbitRadius);
    }
  }
}

// Schematic (not-to-scale) tilted-ellipse shape around the vessel's parent
// body -- log-scaled size (so a LKO hop and an interplanetary orbit are
// both visible on the same map) but real eccentricity/inclination/phase,
// tilted genuinely out of the ecliptic plane in 3D rather than faking it
// with a 2D rotation. Shared by the point-position and full-trajectory-
// trace functions below so they can never disagree with each other.
function vesselOrbitShape(telemetry) {
  const apo = Math.max(telemetry.apoapsis_altitude || 0, 0);
  const peri = Math.max(telemetry.periapsis_altitude || 0, 0);
  const avgAlt = (apo + peri) / 2;
  const semiMajor = Math.min(16, 4 + Math.log10(1 + avgAlt) * 1.6);
  const ecc = Math.min(Math.max(telemetry.eccentricity || 0, 0), 0.9);
  const semiMinor = semiMajor * Math.sqrt(1 - ecc * ecc);
  const inclinationRad = ((telemetry.inclination_deg || 0) * Math.PI) / 180;
  return { semiMajor, semiMinor, inclinationRad };
}

function tiltedEllipsePoint(shape, trueAnomalyRad) {
  const flatX = shape.semiMajor * Math.cos(trueAnomalyRad);
  const flatZ = shape.semiMinor * Math.sin(trueAnomalyRad);
  // Tilt the in-plane Z component out of the ecliptic by inclination.
  const y = flatZ * Math.sin(shape.inclinationRad);
  const z = flatZ * Math.cos(shape.inclinationRad);
  return new THREE.Vector3(flatX, y, z);
}

function vesselLocalPosition(telemetry) {
  const shape = vesselOrbitShape(telemetry);
  const trueAnomalyRad = ((telemetry.true_anomaly_deg || 0) * Math.PI) / 180;
  return tiltedEllipsePoint(shape, trueAnomalyRad);
}

// Direction of travel at the vessel's current point on its (schematic)
// orbit -- the tangent to the ellipse at its current true anomaly, found
// via a small finite-difference step in the direction of increasing true
// anomaly (the standard convention for prograde motion, same one the rest
// of this file already assumes when sweeping 0->2pi for the trajectory
// line). This is what actually orients the cone's tip along the real
// orbital motion instead of a fixed default direction.
function vesselHeadingDirection(telemetry) {
  const shape = vesselOrbitShape(telemetry);
  const trueAnomalyRad = ((telemetry.true_anomaly_deg || 0) * Math.PI) / 180;
  const eps = 0.01;
  const p1 = tiltedEllipsePoint(shape, trueAnomalyRad);
  const p2 = tiltedEllipsePoint(shape, trueAnomalyRad + eps);
  return new THREE.Vector3(p2.x - p1.x, p2.y - p1.y, p2.z - p1.z).normalize();
}

function vesselTrajectoryPoints(telemetry) {
  const shape = vesselOrbitShape(telemetry);
  const segments = 96;
  const points = [];
  for (let i = 0; i <= segments; i++) {
    points.push(tiltedEllipsePoint(shape, (i / segments) * Math.PI * 2));
  }
  return points;
}

function setVessels(vessels) {
  if (!ready) { lastVesselList = vessels; return; }

  lastVesselList = vessels;
  const seen = new Set();
  hoverTargets = [];

  for (const vessel of vessels) {
    const t = vessel.telemetry;
    if (!t || t.error || !t.body) continue;
    const bodyEntry = bodyMeshes.get(t.body);
    const bodyPos = bodyEntry ? bodyEntry.mesh.position : new THREE.Vector3(0, 0, 0);

    seen.add(vessel.id);
    let entry = vesselIcons.get(vessel.id);
    if (!entry) {
      const category = CATEGORY_COLORS[vessel.type] !== undefined ? vessel.type : "unknown";
      // A cone reads as "a craft" at a glance (and shows heading via its
      // point) where a plain sphere was just a dot. Built around unit size
      // -- kept slightly taller than wide so the nose direction is
      // readable -- with on-screen size applied per frame by
      // updateObjectScales(). Nose points along +Y (Three.js convention).
      const mesh = new THREE.Mesh(
        new THREE.ConeGeometry(0.62, 2.0, 12),
        new THREE.MeshBasicMaterial({ color: CATEGORY_COLORS[category] }),
      );
      const trajGeo = new THREE.BufferGeometry();
      const trajectory = new THREE.Line(
        trajGeo,
        new THREE.LineBasicMaterial({ color: CATEGORY_COLORS[category], transparent: true, opacity: 0.55 }),
      );
      scene.add(mesh);
      scene.add(trajectory);
      entry = { mesh, trajectory, category: null, trajKey: null, activeBoost: 1 };
      vesselIcons.set(vessel.id, entry);
    }
    if (entry.category !== vessel.type) {
      const category = CATEGORY_COLORS[vessel.type] !== undefined ? vessel.type : "unknown";
      entry.mesh.material.color.setHex(CATEGORY_COLORS[category]);
      entry.trajectory.material.color.setHex(CATEGORY_COLORS[category]);
      entry.category = vessel.type;
    }

    const local = vesselLocalPosition(t);
    entry.mesh.position.set(bodyPos.x + local.x, local.y, bodyPos.z + local.z);
    // Recorded rather than applied directly: updateObjectScales() owns the
    // scale each frame, so setting it here would just be overwritten.
    entry.activeBoost = vessel.is_active ? 1.6 : 1.0;
    // Point the cone's tip (ConeGeometry's default +Y axis) along the
    // vessel's actual direction of travel instead of a fixed orientation.
    const heading = vesselHeadingDirection(t);
    entry.mesh.quaternion.setFromUnitVectors(new THREE.Vector3(0, 1, 0), heading);

    // Rebuild the trajectory line only when the orbit shape actually
    // changed (not every tick -- true anomaly alone changing doesn't
    // change the ellipse, just where on it the vessel sits).
    const trajKey = `${t.apoapsis_altitude}|${t.periapsis_altitude}|${t.inclination_deg}|${t.eccentricity}`;
    if (entry.trajKey !== trajKey) {
      const pts = vesselTrajectoryPoints(t);
      entry.trajectory.geometry.dispose();
      entry.trajectory.geometry = new THREE.BufferGeometry().setFromPoints(pts);
      entry.trajKey = trajKey;
    }
    entry.trajectory.position.copy(bodyPos);

    hoverTargets.push({ mesh: entry.mesh, vessel });
  }

  for (const [id, entry] of vesselIcons) {
    if (!seen.has(id)) {
      scene.remove(entry.mesh);
      scene.remove(entry.trajectory);
      vesselIcons.delete(id);
    }
  }
}

let transferPreview = null; // {line, marker}

// points: array of {x,y,z} relative to parentBodyName's current position.
// arrivalMarker: {x,y,z}, same relative frame -- where the target moon is
// predicted to be when the craft arrives (should sit right at the ellipse's
// far end if the underlying math is correct, which is the whole point of
// showing this before committing to the actual burn).
function showTransferPreview(parentBodyName, points, arrivalMarker) {
  if (!ready) return;

  clearTransferPreview();
  const bodyEntry = bodyMeshes.get(parentBodyName);
  const origin = bodyEntry ? bodyEntry.mesh.position : new THREE.Vector3(0, 0, 0);

  const geo = new THREE.BufferGeometry().setFromPoints(
    points.map(p => new THREE.Vector3(p.x, p.y, p.z)),
  );
  const line = new THREE.Line(geo, new THREE.LineBasicMaterial({ color: 0xffffff, transparent: true, opacity: 0.85 }));
  line.position.copy(origin);
  scene.add(line);

  const marker = new THREE.Mesh(
    new THREE.SphereGeometry(1.3, 12, 12),
    new THREE.MeshBasicMaterial({ color: 0xffffff, transparent: true, opacity: 0.9 }),
  );
  marker.position.set(origin.x + arrivalMarker.x, origin.y + arrivalMarker.y, origin.z + arrivalMarker.z);
  scene.add(marker);

  transferPreview = { line, marker };
}

function clearTransferPreview() {
  if (!transferPreview) return;
  scene.remove(transferPreview.line);
  scene.remove(transferPreview.marker);
  transferPreview.line.geometry.dispose();
  transferPreview = null;
}

// Built lazily rather than on script load: the container belongs to the
// Overview tab, which mounts on demand. isReady() lets the tab decide
// whether it still needs to call init().
window.Map3D = {
  init, isReady: () => ready,
  setBodies, setVessels, resetView, showTransferPreview, clearTransferPreview,
};

// Try once now in case the container already exists (it does when the
// Overview tab is the default), and leave it to the tab otherwise.
init();
