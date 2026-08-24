// Overview tab: the 3D system map, plus every craft grouped by category.
//
// This is the read-only "where is everything" view. It carries no autopilot
// controls at all -- the other tabs own those.

import { CATEGORY_COLORS, CATEGORY_LABELS, CATEGORY_ORDER, categoryOf, svgIconFor } from "../core/icons.js";
import { refreshSystem } from "../core/bodies.js";
import { createVesselCard } from "../components/vessel-card.js";
import * as telemetry from "../core/telemetry.js";

const cards = new Map();
const sections = new Map(); // category -> { section, grid }
let unsubscribe = null;
let systemTimer = null;
let lastCategoryOrderKey = "";
let legendEl = null;
let vesselsEl = null;

export function mount(container) {
  container.innerHTML = `
    <div class="map-overview">
      <div class="map-header">
        <h2>System Overview</h2>
        <button id="map-reset" class="ghost-btn">Reset view</button>
      </div>
      <div class="map-canvas-wrap">
        <div id="map3d-container"></div>
        <div id="map-tooltip" class="map-tooltip" style="display:none;"></div>
      </div>
      <p class="map-hint">Drag to orbit &middot; scroll to zoom &middot; right-drag to pan &middot; hover a craft for details</p>
      <div id="map-legend" class="map-legend"></div>
    </div>
    <div id="overview-vessels"></div>
  `;

  legendEl = container.querySelector("#map-legend");
  vesselsEl = container.querySelector("#overview-vessels");

  container.querySelector("#map-reset").addEventListener("click", () => {
    if (window.Map3D) window.Map3D.resetView();
  });

  // map3d.js binds to #map3d-container, which only exists once this tab has
  // mounted. Call this unconditionally on every mount, not just the first:
  // leaving the tab destroys the container along with the canvas inside it,
  // so a return visit needs the renderer re-parented into the new one.
  if (window.Map3D) window.Map3D.init();

  pollSystem();
  systemTimer = setInterval(pollSystem, 5000);

  unsubscribe = telemetry.subscribe(render);
}

export function unmount() {
  if (unsubscribe) unsubscribe();
  unsubscribe = null;
  clearInterval(systemTimer);
  cards.clear();
  sections.clear();
  lastCategoryOrderKey = "";
}

async function pollSystem() {
  const positions = await refreshSystem();
  if (window.Map3D) window.Map3D.setBodies(positions);
}

function getSection(category) {
  let entry = sections.get(category);
  if (entry) return entry;

  const section = document.createElement("div");
  section.className = `category-section category-${category}`;
  section.innerHTML = `<h3 class="category-header">${CATEGORY_LABELS[category] || category}</h3>`;
  const grid = document.createElement("div");
  grid.className = "category-grid";
  section.appendChild(grid);
  vesselsEl.appendChild(section);

  entry = { section, grid };
  sections.set(category, entry);
  return entry;
}

function render(vessels) {
  const seen = new Set();
  const used = new Set();

  for (const vessel of vessels) {
    seen.add(vessel.id);
    const category = categoryOf(vessel);
    used.add(category);

    let card = cards.get(vessel.id);
    if (!card) {
      card = createVesselCard(vessel, { draggable: true });
      cards.set(vessel.id, card);
    } else {
      card.update(vessel);
    }

    const { grid } = getSection(category);
    if (card.root.parentElement !== grid) grid.appendChild(card.root);
  }

  for (const [id, card] of [...cards]) {
    if (!seen.has(id)) {
      card.root.remove();
      cards.delete(id);
    }
  }

  for (const [category, { section }] of sections) {
    section.style.display = used.has(category) ? "block" : "none";
  }

  // Enforce a stable top-down order, but only touch the DOM when the set
  // of visible categories has actually changed. Re-appending every section
  // on each telemetry tick (twice a second) continuously reordered the page
  // even when nothing had changed, which interrupted any open dropdown --
  // the confirmed cause of the dashboard "glitching" while a select was
  // open.
  const orderKey = CATEGORY_ORDER.filter((c) => sections.has(c)).join(",");
  if (orderKey !== lastCategoryOrderKey) {
    lastCategoryOrderKey = orderKey;
    for (const category of CATEGORY_ORDER) {
      const entry = sections.get(category);
      if (entry) vesselsEl.appendChild(entry.section);
    }
  }

  if (window.Map3D) window.Map3D.setVessels(vessels);

  legendEl.innerHTML = CATEGORY_ORDER
    .filter((c) => used.has(c))
    .map((c) => `<div>${svgIconFor(c, CATEGORY_COLORS[c])} ${CATEGORY_LABELS[c]}</div>`)
    .join("");
}
