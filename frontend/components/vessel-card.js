// A vessel card that builds only the controls its host tab asks for.
//
// This is the change that makes the tabs real. Previously one card
// template contained every control group for every tab -- launch, landing,
// booster return, constellation, interplanetary, docking -- and tabs were
// faked by CSS rules hiding the groups that didn't belong
// (`body[data-tab="landing"] .group-ascent { display: none }` and a dozen
// more). Every card carried the whole dashboard's DOM whether it was
// visible or not, the visibility logic lived in a stylesheet rather than
// anywhere you would look for it, and no part of the UI could be extracted
// on its own.
//
// Now a tab constructs cards with the control groups it actually wants,
// and provides the behaviour for them. The card owns identity, telemetry
// and status; the tab owns its own controls.

import { fmt } from "../core/format.js";
import { VESSEL_TYPES, categoryOf, colorFor, svgIconFor } from "../core/icons.js";
import * as api from "../core/api.js";

const TELEMETRY_FIELDS = [
  ["body", "Body", (t) => t.body || "-"],
  ["situation", "Situation", (t) => t.situation || "-"],
  ["altitude", "Altitude", (t) => `${fmt(t.altitude)} m`],
  ["speed", "Speed", (t) => `${fmt(t.speed, 1)} m/s`],
  ["apoapsis", "Apoapsis", (t) => `${fmt(t.apoapsis_altitude)} m`],
  ["periapsis", "Periapsis", (t) => `${fmt(t.periapsis_altitude)} m`],
  ["inclination", "Inclination", (t) => `${fmt(t.inclination_deg, 1)}°`],
  ["stage", "Stage", (t) => (t.stage ?? "-")],
];

/**
 * @param vessel  the vessel snapshot from telemetry
 * @param options.controls  called once with (container, vessel) to build
 *                          this tab's own controls into the card
 * @param options.compact   omit telemetry and parts (used by the map
 *                          overview, where cards are thumbnails)
 * @param options.draggable make the card a drag source of its vessel id
 * @param options.showParts include the collapsible read-only parts panel
 */
export function createVesselCard(vessel, options = {}) {
  const { controls, compact = false, draggable = false, showParts = false } = options;

  const root = document.createElement("div");
  root.className = "vessel-card";
  root.dataset.vesselId = vessel.id;

  const header = document.createElement("div");
  header.className = "vessel-header";
  header.innerHTML = `
    <input class="vessel-name" value="" />
    <span class="type-icon"></span>
    <select class="vessel-type" title="Same as tagging the core/cockpit part in-game -- either way works"></select>
  `;
  root.appendChild(header);

  const nameInput = header.querySelector(".vessel-name");
  const typeIcon = header.querySelector(".type-icon");
  const typeSelect = header.querySelector(".vessel-type");
  typeSelect.innerHTML = VESSEL_TYPES.map((t) => `<option value="${t}">${t}</option>`).join("");

  nameInput.addEventListener("change", () => {
    api.renameVessel(vessel.id, nameInput.value).catch((e) => setStatus(`rename failed: ${e.message}`, "error"));
  });
  typeSelect.addEventListener("change", () => {
    api.setVesselRole(vessel.id, typeSelect.value).catch((e) => setStatus(`couldn't set role: ${e.message}`, "error"));
  });

  const els = { name: nameInput, typeIcon, type: typeSelect, telemetry: {} };

  if (!compact) {
    const detail = document.createElement("div");
    detail.className = "vessel-detail";
    detail.innerHTML = `
      <div class="ksp-name"></div>
      <div class="telemetry">
        ${TELEMETRY_FIELDS.map(([key, label]) => `<div>${label}: <span class="val f-${key}"></span></div>`).join("")}
      </div>
      <div class="roles"></div>
    `;
    root.appendChild(detail);
    els.kspName = detail.querySelector(".ksp-name");
    els.roles = detail.querySelector(".roles");
    for (const [key] of TELEMETRY_FIELDS) {
      els.telemetry[key] = detail.querySelector(`.f-${key}`);
    }
  }

  if (showParts) {
    root.appendChild(buildPartsPanel(vessel));
  }

  if (controls) {
    const controlsHost = document.createElement("div");
    controlsHost.className = "vessel-controls";
    root.appendChild(controlsHost);
    controls(controlsHost, vessel);
  }

  const statusEl = document.createElement("div");
  statusEl.className = "job-status";
  root.appendChild(statusEl);

  // Local status line, used for request errors that aren't autopilot job
  // state. Cleared automatically so a stale error doesn't sit there
  // looking like the current state of the craft.
  let localStatusTimer = null;
  function setStatus(text, kind = "") {
    statusEl.textContent = text;
    statusEl.className = `job-status ${kind}`;
    clearTimeout(localStatusTimer);
    if (text) localStatusTimer = setTimeout(() => { statusEl.textContent = ""; }, 6000);
  }

  if (draggable) {
    root.draggable = true;
    root.addEventListener("dragstart", (e) => {
      e.dataTransfer.setData("text/plain", vessel.id);
      root.classList.add("dragging");
    });
    root.addEventListener("dragend", () => root.classList.remove("dragging"));
  }

  const card = { root, els, setStatus, update: null };
  card.update = (fresh) => updateCard(card, fresh, { compact });
  card.update(vessel);
  return card;
}

function updateCard(card, vessel, { compact }) {
  const { els, root } = card;
  root.classList.toggle("active", !!vessel.is_active);

  // Never overwrite a field the user is currently typing in -- telemetry
  // ticks twice a second, and clobbering an in-progress rename made the
  // input unusable.
  if (document.activeElement !== els.name) els.name.value = vessel.name;
  if (document.activeElement !== els.type) els.type.value = vessel.type;

  els.typeIcon.innerHTML = svgIconFor(categoryOf(vessel), colorFor(categoryOf(vessel)));
  els.typeIcon.title = vessel.role_detail || "";

  if (compact) return;

  els.kspName.textContent = `in-game name: ${vessel.ksp_name}`;

  const t = vessel.telemetry || {};
  if (t.error) {
    els.telemetry.situation.textContent = "unreachable";
    for (const [key] of TELEMETRY_FIELDS) {
      if (key !== "situation") els.telemetry[key].textContent = "-";
    }
  } else {
    for (const [key, , read] of TELEMETRY_FIELDS) {
      els.telemetry[key].textContent = read(t);
    }
  }

  const roleEntries = Object.entries(vessel.roles || {});
  els.roles.innerHTML = roleEntries.length
    ? "Tagged roles: " + roleEntries.map(([cat, d]) => `<code>${cat}: ${d.join(", ")}</code>`).join(" ")
    : "No tagged roles (using auto-detection fallback)";

  // Autopilot job state wins over any local message.
  const job = vessel.autopilot;
  if (job) {
    const statusEl = root.querySelector(".job-status");
    statusEl.textContent = `${job.kind}: ${job.status} - ${job.message}`;
    statusEl.className = `job-status ${job.status}`;
  }
}

function buildPartsPanel(vessel) {
  const wrapper = document.createElement("div");
  wrapper.className = "group-parts";
  wrapper.innerHTML = `
    <button class="parts-toggle">Parts &#9662;</button>
    <div class="parts-panel" style="display:none;"></div>
  `;
  const toggle = wrapper.querySelector(".parts-toggle");
  const panel = wrapper.querySelector(".parts-panel");

  toggle.addEventListener("click", async () => {
    const opening = panel.style.display === "none";
    panel.style.display = opening ? "block" : "none";
    if (!opening) return;

    // Read-only by design: role assignment happens in-game (right-click a
    // part -> Tag), because a craft often has several visually identical
    // parts (four landing legs) and a flat web list can't show you which
    // physical one is which the way clicking it in the VAB can.
    panel.innerHTML = `<div class="hint">loading parts...</div>`;
    try {
      const parts = await api.listParts(vessel.id);
      panel.innerHTML = "";
      const table = document.createElement("div");
      table.className = "parts-table";
      for (const part of parts) {
        const hint = [
          part.is_engine && "engine",
          part.is_decoupler && "decoupler",
          part.is_docking_port && "dock",
        ].filter(Boolean).join("/");
        const row = document.createElement("div");
        row.className = "part-row";
        row.innerHTML = `
          <span class="part-title" title="${part.name}">${part.title}
            <span class="dim">(stage ${part.stage}${hint ? ", " + hint : ""})</span>
          </span>
          <span class="${part.tag ? "part-tag" : "dim"}">${part.tag || "untagged"}</span>
        `;
        table.appendChild(row);
      }
      panel.appendChild(table);
    } catch (e) {
      panel.innerHTML = `<div class="bad">failed to load parts: ${e.message}</div>`;
    }
  });

  return wrapper;
}

/**
 * Keep a keyed map of cards in sync with a vessel list: create, update and
 * remove as needed, reusing existing DOM nodes.
 *
 * Reuse matters beyond efficiency -- rebuilding a card wipes any dropdown
 * the user has open or text they are mid-way through typing, and this runs
 * twice a second.
 */
export function reconcileCards(container, vessels, cards, createOptions) {
  const seen = new Set();

  for (const vessel of vessels) {
    seen.add(vessel.id);
    let card = cards.get(vessel.id);
    if (!card) {
      card = createVesselCard(vessel, createOptions);
      cards.set(vessel.id, card);
      container.appendChild(card.root);
    } else {
      card.update(vessel);
    }
  }

  for (const [id, card] of [...cards]) {
    if (!seen.has(id)) {
      card.root.remove();
      cards.delete(id);
    }
  }
}
