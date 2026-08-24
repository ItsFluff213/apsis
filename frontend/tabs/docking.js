// Docking tab: pick two craft, dock them, move resources between them.
//
// Replaces a placeholder note that said this wasn't built. The backend
// side is backend/autopilots/docking.py.

import * as api from "../core/api.js";
import { fmt } from "../core/format.js";
import { reconcileCards } from "../components/vessel-card.js";
import * as telemetry from "../core/telemetry.js";

const cards = new Map();
let unsubscribe = null;
let allVessels = [];

// Resources worth offering by name. Anything else can still be typed in --
// mods add plenty -- but these cover stock refuelling and cargo.
const COMMON_RESOURCES = [
  "LiquidFuel", "Oxidizer", "MonoPropellant", "ElectricCharge", "XenonGas", "Ore",
];

// Docking is only meaningful for craft actually in flight.
const GROUNDED = ["pre_launch", "landed", "splashed"];

function isDockingCandidate(vessel) {
  return !GROUNDED.includes((vessel.telemetry || {}).situation);
}

export function mount(container) {
  container.innerHTML = `
    <div class="tab-intro">
      <h2>Docking, Refuel &amp; Cargo</h2>
      <p class="hint">
        Rendezvous, approach and dock with another craft, then move resources
        between them. Both craft must already be in orbit around the same
        body, and the active craft needs RCS with translation authority.
        Tag a port <code>dock.front</code> to choose which one is used.
      </p>
    </div>
    <div class="card-grid" id="docking-vessels"></div>
    <div class="empty-note" id="docking-empty" style="display:none;">
      Need at least two craft in flight to dock anything.
    </div>
  `;

  const grid = container.querySelector("#docking-vessels");
  const empty = container.querySelector("#docking-empty");

  unsubscribe = telemetry.subscribe((vessels) => {
    allVessels = vessels;
    const candidates = vessels.filter(isDockingCandidate);
    empty.style.display = candidates.length >= 2 ? "none" : "block";
    reconcileCards(grid, candidates, cards, { controls: buildDockingControls });
    for (const [id, card] of cards) refreshTargetOptions(card, id);
  });
}

export function unmount() {
  if (unsubscribe) unsubscribe();
  unsubscribe = null;
  cards.clear();
}

function refreshTargetOptions(card, ownId) {
  const select = card.root.querySelector(".dock-target");
  if (!select || document.activeElement === select) return;

  // Only craft around the same body can be docked with, so don't offer the
  // rest -- the backend would reject them anyway, and a dropdown of
  // impossible choices is worse than a short one.
  const own = allVessels.find((v) => v.id === ownId);
  const ownBody = own && own.telemetry ? own.telemetry.body : null;
  const options = allVessels.filter(
    (v) => v.id !== ownId && isDockingCandidate(v) && (!ownBody || (v.telemetry || {}).body === ownBody),
  );

  const signature = options.map((v) => v.id).join("|");
  if (select.dataset.signature === signature) return;
  select.dataset.signature = signature;

  const current = select.value;
  select.innerHTML = '<option value="">-- target craft --</option>' +
    options.map((v) => `<option value="${escapeAttr(v.id)}">${escapeHtml(v.name)}</option>`).join("");
  select.value = current;
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
  ));
}
const escapeAttr = escapeHtml;

function buildDockingControls(host, vessel) {
  host.innerHTML = `
    <div class="control-group">
      <span class="group-label">Dock with</span>
      <select class="dock-target"><option value="">-- target craft --</option></select>
      <button class="dock-start primary">Rendezvous &amp; dock</button>
    </div>
    <div class="control-group">
      <span class="group-label">Transfer</span>
      <select class="res-name">
        ${COMMON_RESOURCES.map((r) => `<option value="${r}">${r}</option>`).join("")}
      </select>
      <input class="res-amount" type="number" placeholder="amount (blank = all)" />
      <select class="res-direction">
        <option value="out">give to target</option>
        <option value="in">take from target</option>
      </select>
      <button class="res-start">Transfer</button>
    </div>
    <p class="hint">Transfer only works once docked -- KSP treats the joined craft as one vessel.</p>
    <div class="control-group">
      <button class="ap-abort abort">Abort</button>
    </div>
  `;

  const el = (sel) => host.querySelector(sel);
  const card = () => cards.get(vessel.id);
  const status = (msg, kind) => card() && card().setStatus(msg, kind);

  el(".dock-start").addEventListener("click", async () => {
    const targetId = el(".dock-target").value;
    if (!targetId) {
      status("pick a target craft first", "error");
      return;
    }
    try {
      await api.startDocking(vessel.id, targetId);
    } catch (e) {
      status(`docking rejected: ${e.message}`, "error");
    }
  });

  el(".res-start").addEventListener("click", async () => {
    const raw = el(".res-amount").value.trim();
    const amount = raw === "" ? null : Number(raw);
    if (amount !== null && (!Number.isFinite(amount) || amount <= 0)) {
      status("amount must be a positive number, or blank for everything", "error");
      return;
    }
    try {
      await api.transferResource(
        vessel.id, el(".res-name").value, amount, el(".res-direction").value === "out",
      );
    } catch (e) {
      status(`transfer rejected: ${e.message}`, "error");
    }
  });

  el(".ap-abort").addEventListener("click", () => {
    api.abortAutopilot(vessel.id).catch((e) => status(`abort failed: ${e.message}`, "error"));
  });
}
