// Landing tab: put a craft down on a waypoint, or fly a spent booster
// back to the pad.
//
// Both routes share the same descent guidance, and both now handle reentry
// orientation and parachutes on their own -- previously the docs told you
// to point a heatshield manually before commanding a return.

import * as api from "../core/api.js";
import { reconcileCards } from "../components/vessel-card.js";
import * as telemetry from "../core/telemetry.js";

const cards = new Map();
let unsubscribe = null;
let waypoints = [];
let waypointTimer = null;

// A craft still on the pad has nothing to land; a booster that has left it
// does. Filtering here rather than in CSS means the tab genuinely only
// builds cards for relevant craft.
const GROUNDED_SITUATIONS = ["pre_launch", "landed", "splashed"];

function isLandingCandidate(vessel) {
  const situation = (vessel.telemetry || {}).situation;
  return !GROUNDED_SITUATIONS.includes(situation);
}

export function mount(container) {
  container.innerHTML = `
    <div class="tab-intro">
      <h2>Landing &amp; Recovery</h2>
      <p class="hint">
        The deorbit burn is aimed: the burn point is chosen so the predicted
        impact falls near the waypoint, which costs no extra fuel. Reentry is
        flown retrograde so a tagged <code>heatshield.*</code> part faces the
        airflow, and tagged <code>chute.*</code> parachutes are deployed once
        it's safe to. Craft still on the ground are hidden here.
      </p>
    </div>
    <div class="card-grid" id="landing-vessels"></div>
    <div class="empty-note" id="landing-empty" style="display:none;">
      Nothing in flight to land right now.
    </div>
  `;

  const grid = container.querySelector("#landing-vessels");
  const empty = container.querySelector("#landing-empty");

  refreshWaypoints();
  waypointTimer = setInterval(refreshWaypoints, 5000);

  unsubscribe = telemetry.subscribe((vessels) => {
    const candidates = vessels.filter(isLandingCandidate);
    empty.style.display = candidates.length ? "none" : "block";
    reconcileCards(grid, candidates, cards, { controls: buildLandingControls });
    for (const card of cards.values()) refreshWaypointOptions(card);
  });
}

export function unmount() {
  if (unsubscribe) unsubscribe();
  unsubscribe = null;
  clearInterval(waypointTimer);
  cards.clear();
}

async function refreshWaypoints() {
  try {
    waypoints = await api.listWaypoints();
  } catch (e) {
    // kRPC not connected, or we're in the VAB where the call isn't valid.
    // Keep whatever list we had.
  }
  for (const card of cards.values()) refreshWaypointOptions(card);
}

function refreshWaypointOptions(card) {
  const select = card.root.querySelector(".landing-waypoint");
  if (!select) return;
  // Don't rebuild the list while it's open -- that closes the dropdown
  // mid-choice, and this runs on every telemetry tick.
  if (document.activeElement === select) return;
  if (select.options.length - 1 === waypoints.length) return;

  const current = select.value;
  select.innerHTML = '<option value="">-- waypoint --</option>' +
    waypoints.map((wp, i) => `<option value="${i}">${wp.name} (${wp.body})</option>`).join("");
  select.value = current;
}

function buildLandingControls(host, vessel) {
  host.innerHTML = `
    <div class="control-group">
      <span class="group-label">Land at</span>
      <select class="landing-waypoint"><option value="">-- waypoint --</option></select>
      <button class="landing-start primary">Land at waypoint</button>
    </div>
    <div class="control-group">
      <span class="group-label">Recover</span>
      <button class="booster-return">Return to KSC</button>
    </div>
    <div class="control-group">
      <button class="ap-abort abort">Abort</button>
    </div>
  `;

  const el = (sel) => host.querySelector(sel);
  const card = () => cards.get(vessel.id);
  const status = (msg, kind) => card() && card().setStatus(msg, kind);

  el(".landing-start").addEventListener("click", async () => {
    const wp = waypoints[Number(el(".landing-waypoint").value)];
    if (!wp) {
      status("pick a waypoint first", "error");
      return;
    }
    try {
      await api.startLanding(vessel.id, wp.latitude, wp.longitude);
    } catch (e) {
      status(`landing rejected: ${e.message}`, "error");
    }
  });

  el(".booster-return").addEventListener("click", async () => {
    try {
      await api.startBoosterReturn(vessel.id);
    } catch (e) {
      status(`return rejected: ${e.message}`, "error");
    }
  });

  el(".ap-abort").addEventListener("click", () => {
    api.abortAutopilot(vessel.id).catch((e) => status(`abort failed: ${e.message}`, "error"));
  });

  refreshWaypointOptions({ root: host.closest(".vessel-card") || host });
}
