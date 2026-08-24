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

let scene, camera, renderer, controls, container;
let sunMesh;
const bodyMeshes = new Map(); // name -> {mesh, ring}
const vesselIcons = new Map(); // vessel id -> {mesh}
let hoverTargets = []; // [{mesh, vessel}]
const raycaster = new THREE.Raycaster();
raycaster.params.Points = { threshold: 4 };
const pointer = new THREE.Vector2();
let lastPointerEvent = null;
let lastVesselList = [];

const tooltipEl = document.getElementById("map-tooltip");

function fmt(n, digits = 0) {
  if (n === undefined || n === null || Number.isNaN(n)) return "-";
  return Number(n).toLocaleString(undefined, { maximumFractionDigits: digits });
}

function init() {
  container = document.getElementById("map3d-container");
  const width = container.clientWidth || 700;
  const height = 350;

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
    new THREE.SphereGeometry(6, 24, 24),
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
  window.addEventListener("resize", onResize);

  animate();
}

function onResize() {
  if (!container) return;
  const width = container.clientWidth || 700;
  const height = 350;
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
  updateHover();
  renderer.render(scene, camera);
}

function resetView() {
  camera.position.set(0, 220, 320);
  controls.target.set(0, 0, 0);
  controls.update();
}

// positions: Map(name -> {x, y, isMoon}) from app.js's layoutSystem() --
// (x, y) here is reinterpreted as the (x, z) ground-plane coordinate.
function setBodies(positions) {
  for (const [name, pos] of positions) {
    if (name === "Sun") continue;
    let entry = bodyMeshes.get(name);
    if (!entry) {
      const radius = pos.isMoon ? 1.4 : 2.6;
      const color = BODY_COLORS[name] !== undefined ? BODY_COLORS[name] : DEFAULT_BODY_COLOR;
      const mesh = new THREE.Mesh(
        new THREE.SphereGeometry(radius, 20, 20),
        new THREE.MeshStandardMaterial({ color, roughness: 0.75, emissive: color, emissiveIntensity: 0.12 }),
      );
      scene.add(mesh);

      const ringPoints = [];
      const segments = 128;
      for (let i = 0; i <= segments; i++) {
        const a = (i / segments) * Math.PI * 2;
        ringPoints.push(new THREE.Vector3(Math.cos(a), 0, Math.sin(a)));
      }
      const ringGeo = new THREE.BufferGeometry().setFromPoints(ringPoints);
      const ring = new THREE.Line(ringGeo, new THREE.LineBasicMaterial({ color, transparent: true, opacity: 0.45 }));
      scene.add(ring);

      entry = { mesh, ring };
      bodyMeshes.set(name, entry);
    }
    entry.mesh.position.set(pos.x, 0, pos.y);
    const orbitRadius = Math.hypot(pos.x, pos.y) || 0.001;
    entry.ring.scale.set(orbitRadius, 1, orbitRadius);
  }
}

// Schematic (not-to-scale) tilted-ellipse placement around the vessel's
// parent body -- same "log-scaled size, real phase/inclination" approach
// the old 2D canvas map used, just genuinely tilted out of the ecliptic
// plane in 3D instead of faking it with a 2D rotation.
function vesselLocalPosition(telemetry) {
  const apo = Math.max(telemetry.apoapsis_altitude || 0, 0);
  const peri = Math.max(telemetry.periapsis_altitude || 0, 0);
  const avgAlt = (apo + peri) / 2;
  const semiMajor = Math.min(16, 4 + Math.log10(1 + avgAlt) * 1.6);
  const ecc = Math.min(Math.max(telemetry.eccentricity || 0, 0), 0.9);
  const semiMinor = semiMajor * Math.sqrt(1 - ecc * ecc);
  const inclinationRad = ((telemetry.inclination_deg || 0) * Math.PI) / 180;
  const trueAnomalyRad = ((telemetry.true_anomaly_deg || 0) * Math.PI) / 180;

  const flatX = semiMajor * Math.cos(trueAnomalyRad);
  const flatZ = semiMinor * Math.sin(trueAnomalyRad);
  // Tilt the in-plane Z component out of the ecliptic by inclination.
  const y = flatZ * Math.sin(inclinationRad);
  const z = flatZ * Math.cos(inclinationRad);
  return new THREE.Vector3(flatX, y, z);
}

function setVessels(vessels) {
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
      const mesh = new THREE.Mesh(
        new THREE.SphereGeometry(0.9, 8, 8),
        new THREE.MeshBasicMaterial({ color: CATEGORY_COLORS[category] }),
      );
      scene.add(mesh);
      entry = { mesh, category: null };
      vesselIcons.set(vessel.id, entry);
    }
    if (entry.category !== vessel.type) {
      const category = CATEGORY_COLORS[vessel.type] !== undefined ? vessel.type : "unknown";
      entry.mesh.material.color.setHex(CATEGORY_COLORS[category]);
      entry.category = vessel.type;
    }

    const local = vesselLocalPosition(t);
    entry.mesh.position.set(bodyPos.x + local.x, local.y, bodyPos.z + local.z);
    entry.mesh.scale.setScalar(vessel.is_active ? 1.6 : 1.0);

    hoverTargets.push({ mesh: entry.mesh, vessel });
  }

  for (const [id, entry] of vesselIcons) {
    if (!seen.has(id)) {
      scene.remove(entry.mesh);
      vesselIcons.delete(id);
    }
  }
}

window.Map3D = { init, setBodies, setVessels, resetView };
init();
