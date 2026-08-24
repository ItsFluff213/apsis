// Standalone satellites / constellation manager.
//
// This page is deliberately self-contained. It shares nothing with the
// main dashboard except `core/`, which is dependency-free, and it talks to
// the backend only over the REST endpoints in core/api.js -- no shared
// globals, no shared DOM, no import from tabs/ or from the dashboard entry
// point. Lifting it out to run on its own means copying this directory
// plus core/ and changing nothing else.
//
// It is reachable two ways: embedded as a tab in the main dashboard, and
// directly at /satellites/. Both load this same file.

import * as api from "../core/api.js";
import { ALL_BODY_NAMES } from "../core/bodies.js";
import { categoryOf, colorFor, svgIconFor } from "../core/icons.js";
import { fmt } from "../core/format.js";
import * as telemetry from "../core/telemetry.js";

const POLL_MS = 5000;

let constellations = [];
let satellites = [];
let lastConstellationsJson = "";
let listEl = null;
let satellitesEl = null;
let statusEl = null;
let pollTimer = null;
let unsubscribe = null;

export function mount(container, { standalone = false } = {}) {
  container.innerHTML = `
    <div class="tab-intro">
      <h2>Satellite Constellations</h2>
      <p class="hint">
        Group satellites into constellations and deploy them into evenly
        spaced slots. A communications constellation targets the body's
        geostationary altitude automatically; a custom one takes whatever
        altitude and inclination you give it. Drag a satellite onto a
        constellation to assign it.
      </p>
    </div>

    <div class="constellations-panel">
      <div class="constellations-header">
        <h3>Constellations</h3>
        <button id="new-constellation-btn" class="ghost-btn">+ New constellation</button>
      </div>

      <form id="new-constellation-form" class="new-constellation-form" style="display:none;">
        <input id="nc-name" type="text" placeholder="name" required />
        <select id="nc-body"></select>
        <select id="nc-kind">
          <option value="communications">Communications (geostationary)</option>
          <option value="custom">Custom</option>
        </select>
        <input id="nc-altitude" type="number" placeholder="altitude km" style="display:none;" />
        <input id="nc-inclination" type="number" placeholder="inclination deg" value="0" style="display:none;" />
        <button type="submit" class="primary">Create</button>
        <button type="button" id="nc-cancel">Cancel</button>
      </form>

      <div id="constellations-list" class="constellations-list"></div>

      <div class="sync-bar">
        <label for="sync-source-url">Sync from teammate:</label>
        <input id="sync-source-url" type="text" placeholder="http://192.168.1.23:8000" />
        <button id="sync-pull-btn">Pull</button>
        <span id="sync-status"></span>
      </div>
    </div>

    <h3 class="section-heading">Satellites</h3>
    <div id="satellite-cards" class="card-grid"></div>
    <div class="empty-note" id="satellites-empty" style="display:none;">
      No craft are tagged as satellites yet. Set a craft's type to
      <code>satellite</code> from the Overview tab, or tag its probe core
      in-game.
    </div>
  `;

  listEl = container.querySelector("#constellations-list");
  satellitesEl = container.querySelector("#satellite-cards");
  statusEl = container.querySelector("#sync-status");

  wireCreateForm(container);
  wireSync(container);

  // Standalone means nobody else has opened the telemetry socket.
  if (standalone) telemetry.connect();

  unsubscribe = telemetry.subscribe((vessels) => {
    satellites = vessels.filter((v) => categoryOf(v) === "satellite");
    container.querySelector("#satellites-empty").style.display = satellites.length ? "none" : "block";
    renderSatellites();
    // Member rows show live vessel names, so a rename elsewhere should
    // show up here too.
    renderConstellations();
  });

  refreshConstellations();
  pollTimer = setInterval(refreshConstellations, POLL_MS);
}

export function unmount() {
  if (unsubscribe) unsubscribe();
  unsubscribe = null;
  clearInterval(pollTimer);
  constellations = [];
  lastConstellationsJson = "";
}

// --- Constellation list --------------------------------------------------

async function refreshConstellations() {
  let fresh;
  try {
    fresh = await api.listConstellations();
  } catch (e) {
    return; // keep showing the last known list rather than blanking it
  }

  // Skip the rebuild when nothing has actually changed. The list is
  // rebuilt from scratch on render, which was wiping any open edit-orbit
  // form on every poll -- the confirmed cause of the edit menu "instantly
  // hiding" right after being opened.
  const freshJson = JSON.stringify(fresh);
  if (freshJson === lastConstellationsJson) return;
  lastConstellationsJson = freshJson;
  constellations = fresh;
  renderConstellations();
}

function nameFor(vesselId) {
  const match = satellites.find((v) => v.id === vesselId);
  return match ? match.name : vesselId;
}

function renderConstellations() {
  if (!listEl) return;

  if (!constellations.length) {
    listEl.innerHTML = `<div class="empty-note">No constellations yet.</div>`;
    return;
  }

  listEl.innerHTML = "";
  for (const c of constellations) {
    listEl.appendChild(buildConstellationBox(c));
  }
}

function buildConstellationBox(c) {
  const box = document.createElement("div");
  box.className = "constellation-box";
  const isCustom = c.kind === "custom";
  const altLabel = isCustom ? `${Math.round(c.altitude_m / 1000)} km` : "geostationary";

  box.innerHTML = `
    <button class="delete-btn" title="Delete constellation">&times;</button>
    ${isCustom ? '<button class="edit-orbit-btn" title="Edit orbit">&#9998;</button>' : ""}
    <h4>${escapeHtml(c.name)}</h4>
    <div class="meta">
      ${escapeHtml(c.body)} &middot; ${escapeHtml(c.kind)} &middot; ${altLabel}${isCustom ? `, ${fmt(c.inclination_deg, 1)}&deg;` : ""}
    </div>
    ${isCustom ? `
      <div class="edit-orbit-form" style="display:none;">
        <input class="edit-orbit-alt" type="number" placeholder="altitude km" value="${Math.round(c.altitude_m / 1000)}" />
        <input class="edit-orbit-incl" type="number" placeholder="inclination deg" value="${c.inclination_deg}" />
        <button class="edit-orbit-save">Save</button>
      </div>` : ""}
    <div class="members"></div>
  `;

  const membersEl = box.querySelector(".members");
  if (!c.members.length) {
    membersEl.innerHTML = `<div class="dim">drop a satellite here</div>`;
  } else {
    for (const vesselId of c.members) {
      const row = document.createElement("div");
      row.className = "member";
      row.innerHTML = `<span>${escapeHtml(nameFor(vesselId))}</span><button title="Remove">&times;</button>`;
      row.querySelector("button").addEventListener("click", () => {
        run(() => api.removeConstellationMember(c.id, vesselId));
      });
      membersEl.appendChild(row);
    }
  }

  box.querySelector(".delete-btn").addEventListener("click", () => {
    run(() => api.deleteConstellation(c.id));
  });

  if (isCustom) {
    const form = box.querySelector(".edit-orbit-form");
    box.querySelector(".edit-orbit-btn").addEventListener("click", () => {
      form.style.display = form.style.display === "none" ? "flex" : "none";
    });
    box.querySelector(".edit-orbit-save").addEventListener("click", () => {
      const altitudeM = Number(box.querySelector(".edit-orbit-alt").value) * 1000;
      const inclinationDeg = Number(box.querySelector(".edit-orbit-incl").value);
      run(() => api.updateConstellationOrbit(c.id, altitudeM, inclinationDeg));
    });
  }

  box.addEventListener("dragover", (e) => {
    e.preventDefault();
    box.classList.add("drag-over");
  });
  box.addEventListener("dragleave", () => box.classList.remove("drag-over"));
  box.addEventListener("drop", (e) => {
    e.preventDefault();
    box.classList.remove("drag-over");
    const vesselId = e.dataTransfer.getData("text/plain");
    if (vesselId) run(() => api.addConstellationMember(c.id, vesselId));
  });

  return box;
}

/** Run a mutating call, then force the next poll to actually re-render. */
async function run(fn) {
  try {
    await fn();
  } catch (e) {
    setSyncStatus(e.message, "bad");
    return;
  }
  lastConstellationsJson = "";
  refreshConstellations();
}

// --- Satellite cards -----------------------------------------------------

function constellationOf(vesselId) {
  return constellations.find((c) => c.members.includes(vesselId)) || null;
}

function renderSatellites() {
  if (!satellitesEl) return;

  // Rebuilt wholesale rather than reconciled: these cards are small, carry
  // no text inputs to clobber, and the set changes rarely.
  satellitesEl.innerHTML = "";
  for (const vessel of satellites) {
    satellitesEl.appendChild(buildSatelliteCard(vessel));
  }
}

function buildSatelliteCard(vessel) {
  const card = document.createElement("div");
  card.className = "vessel-card";
  card.draggable = true;
  card.dataset.vesselId = vessel.id;

  const assigned = constellationOf(vessel.id);
  const t = vessel.telemetry || {};
  const job = vessel.autopilot;

  card.innerHTML = `
    <div class="vessel-header">
      <span class="type-icon">${svgIconFor("satellite", colorFor("satellite"))}</span>
      <span class="vessel-name-static">${escapeHtml(vessel.name)}</span>
    </div>
    <div class="telemetry">
      <div>Body: <span class="val">${escapeHtml(t.body || "-")}</span></div>
      <div>Altitude: <span class="val">${fmt(t.altitude)} m</span></div>
      <div>Inclination: <span class="val">${fmt(t.inclination_deg, 1)}&deg;</span></div>
      <div>Situation: <span class="val">${escapeHtml(t.situation || "-")}</span></div>
    </div>
    <div class="control-group">
      <span class="constellation-label">${assigned ? escapeHtml(assigned.name) : "unassigned"}</span>
      <button class="deploy-start primary" ${assigned ? "" : "disabled"}>Deploy</button>
    </div>
    <div class="job-status ${job ? job.status : ""}">${job ? escapeHtml(`${job.kind}: ${job.status} - ${job.message}`) : ""}</div>
  `;

  card.addEventListener("dragstart", (e) => {
    e.dataTransfer.setData("text/plain", vessel.id);
    card.classList.add("dragging");
  });
  card.addEventListener("dragend", () => card.classList.remove("dragging"));

  const deployBtn = card.querySelector(".deploy-start");
  deployBtn.addEventListener("click", async () => {
    const constellation = constellationOf(vessel.id);
    if (!constellation) return;
    const statusLine = card.querySelector(".job-status");
    try {
      await api.deployToConstellation(constellation.id, vessel.id);
    } catch (e) {
      statusLine.textContent = `deploy rejected: ${e.message}`;
      statusLine.className = "job-status error";
    }
  });

  return card;
}

// --- Create form and sync ------------------------------------------------

function wireCreateForm(container) {
  const form = container.querySelector("#new-constellation-form");
  const bodySelect = container.querySelector("#nc-body");
  const kindSelect = container.querySelector("#nc-kind");
  const altitude = container.querySelector("#nc-altitude");
  const inclination = container.querySelector("#nc-inclination");

  bodySelect.innerHTML = ALL_BODY_NAMES.map((n) => `<option value="${n}">${n}</option>`).join("");

  container.querySelector("#new-constellation-btn").addEventListener("click", () => {
    form.style.display = form.style.display === "none" ? "flex" : "none";
  });
  container.querySelector("#nc-cancel").addEventListener("click", () => {
    form.style.display = "none";
    form.reset();
  });

  // A communications constellation derives both altitude and inclination
  // from the body it orbits, so asking for them would be misleading.
  kindSelect.addEventListener("change", () => {
    const isCustom = kindSelect.value === "custom";
    altitude.style.display = isCustom ? "inline-block" : "none";
    inclination.style.display = isCustom ? "inline-block" : "none";
  });

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const kind = kindSelect.value;
    const payload = {
      name: container.querySelector("#nc-name").value,
      body: bodySelect.value,
      kind,
      altitude_m: kind === "custom" ? Number(altitude.value) * 1000 : null,
      inclination_deg: kind === "custom" ? Number(inclination.value) : 0,
    };
    if (kind === "custom" && (!Number.isFinite(payload.altitude_m) || payload.altitude_m <= 0)) {
      setSyncStatus("a custom constellation needs an altitude in km", "bad");
      return;
    }
    try {
      await api.createConstellation(payload);
    } catch (err) {
      setSyncStatus(err.message, "bad");
      return;
    }
    form.reset();
    form.style.display = "none";
    lastConstellationsJson = "";
    refreshConstellations();
  });
}

function wireSync(container) {
  const input = container.querySelector("#sync-source-url");
  container.querySelector("#sync-pull-btn").addEventListener("click", async () => {
    const sourceUrl = input.value.trim();
    if (!sourceUrl) return;
    setSyncStatus("pulling...", "");
    try {
      const data = await api.syncConstellations(sourceUrl);
      setSyncStatus(`merged ${data.merged.length} constellation(s)`, "ok");
      lastConstellationsJson = "";
      refreshConstellations();
    } catch (e) {
      setSyncStatus(e.message, "bad");
    }
  });
}

function setSyncStatus(text, kind) {
  if (!statusEl) return;
  statusEl.textContent = text;
  statusEl.className = kind;
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
  ));
}
