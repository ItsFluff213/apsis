// Orbit tab: get to orbit, and go somewhere else from there.
//
// This merges what used to be two separate tabs with a hard line between
// them. "Launch" handled ascent; "Interplanetary" handled transfers, and
// only worked by pasting text from an external website into a textarea
// before any craft could be commanded. Now that the backend computes
// planet transfers itself (backend/autopilots/planet_transfer.py), the
// textarea and the whole paste-a-plan workflow are gone, and moons and
// planets are one "transfer to" control -- the distinction between them is
// a backend routing detail, not something the user should have to care
// about.

import * as api from "../core/api.js";
import { MOON_NAMES, PLANET_NAMES, findSystemNode, parentPlanetOf } from "../core/bodies.js";
import { fmt, fmtDuration, fmtSpeed } from "../core/format.js";
import { reconcileCards } from "../components/vessel-card.js";
import * as telemetry from "../core/telemetry.js";

const cards = new Map();
let unsubscribe = null;

export function mount(container) {
  container.innerHTML = `
    <div class="tab-intro">
      <h2>Orbit</h2>
      <p class="hint">
        Launch to a target orbit, or send a craft already in orbit to another
        body. Transfer windows, ejection burns and capture are all computed
        here -- no external planner needed.
      </p>
    </div>
    <div class="card-grid" id="orbit-vessels"></div>
    <div class="empty-note" id="orbit-empty" style="display:none;">
      No craft visible yet. Start KSP with the kRPC server running.
    </div>
  `;

  const grid = container.querySelector("#orbit-vessels");
  const empty = container.querySelector("#orbit-empty");

  unsubscribe = telemetry.subscribe((vessels) => {
    empty.style.display = vessels.length ? "none" : "block";
    reconcileCards(grid, vessels, cards, { showParts: true, controls: buildOrbitControls });
    // A craft that has just changed sphere of influence can reach a
    // different set of destinations than it could a moment ago.
    for (const card of cards.values()) {
      const host = card.root.querySelector(".vessel-controls");
      if (host && host.refreshTargets) host.refreshTargets();
    }
  });
}

export function unmount() {
  if (unsubscribe) unsubscribe();
  unsubscribe = null;
  cards.clear();
}

function buildOrbitControls(host, vessel) {
  host.innerHTML = `
    <div class="control-group">
      <span class="group-label">Launch</span>
      <input class="ap-alt" type="number" placeholder="altitude km" value="90" />
      <input class="ap-incl" type="number" placeholder="incl deg" value="0" />
      <button class="ap-polar" title="Set inclination to 90 degrees">Polar</button>
      <button class="ap-start primary">Launch to orbit</button>
    </div>
    <div class="control-group">
      <span class="group-label">Transfer to</span>
      <select class="tr-target"></select>
      <input class="tr-periapsis" type="number" placeholder="arrival orbit km" value="50" />
      <input class="tr-incl" type="number" placeholder="arrival incl deg (optional)" />
      <button class="tr-preview">Preview</button>
      <button class="tr-start primary">Start transfer</button>
    </div>
    <div class="control-group">
      <span class="group-label">Park at</span>
      <input class="tr-parking" type="number" placeholder="departure orbit km (blank = current)" />
    </div>
    <div class="transfer-preview" style="display:none;"></div>
    <div class="control-group">
      <button class="ap-abort abort">Abort</button>
    </div>
  `;

  const el = (sel) => host.querySelector(sel);
  const targetSelect = el(".tr-target");
  const previewBox = el(".transfer-preview");

  // Only offer destinations actually reachable from where the craft is,
  // and rebuild the list when it moves between bodies.
  //
  // This list used to be every moon and every planet, unconditionally,
  // which invited failures that looked like the autopilot was broken. A
  // craft in Mun orbit offered "Gilly" would accept the command and then
  // fail with "'Gilly' is not a satellite of Mun", because a moon transfer
  // only ever searches the current parent body's own moons.
  function refreshTargets() {
    const currentBody = (telemetry.getVessel(vessel.id) || {}).telemetry?.body;
    if (!currentBody || targetSelect.dataset.forBody === currentBody) return;
    targetSelect.dataset.forBody = currentBody;

    // Moons of whatever the craft currently orbits. From a moon there are
    // none -- you have to climb out to the planet first.
    const reachableMoons = MOON_NAMES.filter((n) => parentPlanetOf(n) === currentBody);
    // Any planet other than the one we're at (or the one our moon belongs
    // to); departing from a moon escapes to its planet automatically.
    const departurePlanet = PLANET_NAMES.includes(currentBody) ? currentBody : parentPlanetOf(currentBody);
    const reachablePlanets = PLANET_NAMES.filter((n) => n !== departurePlanet);

    const previous = targetSelect.value;
    targetSelect.innerHTML = `
      ${reachableMoons.length ? `<optgroup label="Moons of ${currentBody}">
        ${reachableMoons.map((n) => `<option value="${n}" data-kind="moon">${n}</option>`).join("")}
      </optgroup>` : ""}
      <optgroup label="Planets">
        ${reachablePlanets.map((n) => `<option value="${n}" data-kind="planet">${n}</option>`).join("")}
      </optgroup>
    `;
    if ([...targetSelect.options].some((o) => o.value === previous)) targetSelect.value = previous;
  }

  refreshTargets();
  host.refreshTargets = refreshTargets;

  const card = () => cards.get(vessel.id);
  const status = (msg, kind) => card() && card().setStatus(msg, kind);

  el(".ap-polar").addEventListener("click", () => { el(".ap-incl").value = 90; });

  el(".ap-start").addEventListener("click", async () => {
    const altitudeM = Number(el(".ap-alt").value) * 1000;
    if (!Number.isFinite(altitudeM) || altitudeM <= 0) {
      status("enter a target altitude in km", "error");
      return;
    }
    try {
      await api.startAscent(vessel.id, altitudeM, altitudeM, Number(el(".ap-incl").value) || 0);
    } catch (e) {
      status(`launch rejected: ${e.message}`, "error");
    }
  });

  el(".ap-abort").addEventListener("click", () => {
    api.abortAutopilot(vessel.id).catch((e) => status(`abort failed: ${e.message}`, "error"));
  });

  function selectedTarget() {
    const option = targetSelect.selectedOptions[0];
    return { name: targetSelect.value, kind: option ? option.dataset.kind : "moon" };
  }

  function transferArgs() {
    const periapsisM = Number(el(".tr-periapsis").value) * 1000;
    const inclRaw = el(".tr-incl").value.trim();
    const parkingRaw = el(".tr-parking").value.trim();
    return {
      periapsisM,
      inclinationDeg: inclRaw === "" ? null : Number(inclRaw),
      // Blank means "use whatever orbit the craft is already in", which
      // still gets circularized before departure if it needs it.
      parkingAltitudeM: parkingRaw === "" ? null : Number(parkingRaw) * 1000,
    };
  }

  el(".tr-start").addEventListener("click", async () => {
    const { name, kind } = selectedTarget();
    const { periapsisM, inclinationDeg, parkingAltitudeM } = transferArgs();
    if (!Number.isFinite(periapsisM) || periapsisM <= 0) {
      status("enter an arrival orbit altitude in km", "error");
      return;
    }
    try {
      if (kind === "planet") {
        await api.startPlanetTransfer(vessel.id, name, periapsisM, inclinationDeg, parkingAltitudeM);
      } else {
        await api.startMoonTransfer(vessel.id, name, periapsisM, inclinationDeg);
      }
    } catch (e) {
      status(`transfer rejected: ${e.message}`, "error");
    }
  });

  el(".tr-preview").addEventListener("click", async () => {
    const button = el(".tr-preview");
    const { name, kind } = selectedTarget();
    button.disabled = true;
    button.textContent = "Calculating...";
    try {
      if (kind === "planet") {
        await previewPlanet(vessel, name, previewBox);
      } else {
        await previewMoon(vessel, name, previewBox);
      }
    } catch (e) {
      previewBox.style.display = "block";
      previewBox.innerHTML = `<span class="bad">Preview failed: ${e.message}</span>`;
    } finally {
      button.disabled = false;
      button.textContent = "Preview";
    }
  });
}

async function previewPlanet(vessel, targetName, box) {
  const plan = await api.previewPlanetTransfer(vessel.id, targetName);
  box.style.display = "block";
  box.innerHTML = `
    <div class="preview-title">${plan.origin_name} &rarr; ${plan.target_name}</div>
    <div class="preview-grid">
      <div><span class="dim">Window in</span> ${fmtDuration(plan.wait_s)}</div>
      <div><span class="dim">Cruise</span> ${fmtDuration(plan.transfer_time_s)}</div>
      <div><span class="dim">Ejection burn</span> ${fmtSpeed(plan.ejection_dv)}</div>
      <div><span class="dim">Escape speed</span> ${fmtSpeed(plan.v_infinity)}</div>
      <div><span class="dim">Phase angle</span> ${fmt(plan.phase_angle_deg, 1)}&deg;</div>
      <div><span class="dim">Ejection angle</span> ${fmt(plan.ejection_angle_deg, 1)}&deg;</div>
    </div>
    <p class="hint">
      Direct Hohmann transfer. Gravity assists aren't planned -- for a
      multi-flyby grand tour you still want a dedicated planner.
    </p>
  `;
}

async function previewMoon(vessel, moonName, box) {
  const plan = await api.previewMoonTransfer(vessel.id, moonName);

  box.style.display = "block";
  box.innerHTML = `
    <div class="preview-title">Transfer to ${plan.moon_name}</div>
    <div class="preview-grid">
      <div><span class="dim">Burn in</span> ${fmtDuration(plan.burn_in_s)}</div>
      <div><span class="dim">Arrive in</span> ${fmtDuration(plan.arrival_in_s)}</div>
      <div><span class="dim">Apoapsis</span> ${fmt(plan.r_apo_m / 1000)} km</div>
      <div><span class="dim">Inclination</span> ${fmt(plan.inclination_deg, 1)}&deg;</div>
    </div>
  `;

  drawMoonTransferOnMap(plan, moonName);
}

// Draw the planned ellipse on the 3D overview. Moons are drawn at a fixed
// schematic radius around their parent (see core/bodies.js), so the
// trajectory has to be compressed by the same factor or it won't line up
// with where the moon is actually shown.
function drawMoonTransferOnMap(plan, moonName) {
  const moonNode = findSystemNode(moonName);
  if (!moonNode || !window.Map3D) return;

  const scale = moonNode.radius / plan.moon_orbital_radius_m;
  const a = ((plan.r_peri_m + plan.r_apo_m) / 2) * scale;
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

  // Arrival is exactly the apoapsis, half a revolution from periapsis.
  const arrivalAngle = periapsisRad + Math.PI;
  const arrivalMarker = {
    x: plan.r_apo_m * scale * Math.cos(arrivalAngle),
    y: 0,
    z: plan.r_apo_m * scale * Math.sin(arrivalAngle),
  };

  const parent = parentPlanetOf(moonName) || "Kerbin";
  window.Map3D.showTransferPreview(parent, points, arrivalMarker);
}
