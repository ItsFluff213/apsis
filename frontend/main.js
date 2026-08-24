// Dashboard entry point: tab routing, the connection indicator, and the
// save-profile bar.
//
// Everything else lives in its tab module. A tab is mounted when selected
// and unmounted when left, so only the visible view holds DOM and only the
// visible view is subscribed to telemetry.

import * as api from "./core/api.js";
import * as telemetry from "./core/telemetry.js";

import * as overview from "./tabs/overview.js";
import * as orbit from "./tabs/orbit.js";
import * as landing from "./tabs/landing.js";
import * as docking from "./tabs/docking.js";
import * as satellites from "./satellites/app.js";

// The satellites view is the same module the standalone /satellites/ page
// loads. It is mounted here without the `standalone` flag, so it shares
// this page's telemetry socket instead of opening a second one.
const TABS = { overview, orbit, satellites, landing, docking };

const contentEl = document.getElementById("tab-content");
let activeName = null;
let activeModule = null;

function showTab(name) {
  if (name === activeName) return;

  if (activeModule && activeModule.unmount) activeModule.unmount();
  contentEl.innerHTML = "";

  activeName = name;
  activeModule = TABS[name];
  document.body.dataset.tab = name;

  for (const btn of document.querySelectorAll("#tabs .tab-btn")) {
    btn.classList.toggle("active", btn.dataset.tab === name);
  }

  if (activeModule) activeModule.mount(contentEl);
}

for (const btn of document.querySelectorAll("#tabs .tab-btn")) {
  btn.addEventListener("click", () => showTab(btn.dataset.tab));
}

// --- Connection indicator ------------------------------------------------

const connStatusEl = document.getElementById("conn-status");
const CONNECTION_LABELS = {
  connecting: ["connecting...", "bad"],
  open: ["connected to KSP", "ok"],
  "waiting-for-ksp": ["waiting for KSP (start the game + kRPC server)", "bad"],
  closed: ["disconnected, retrying...", "bad"],
};

telemetry.subscribeConnection((state) => {
  const [text, cls] = CONNECTION_LABELS[state] || [state, ""];
  connStatusEl.textContent = text;
  connStatusEl.className = cls;
});

// --- Save profile --------------------------------------------------------

const profileSelect = document.getElementById("profile-select");
const profileNewInput = document.getElementById("profile-new");

async function refreshProfile() {
  try {
    const data = await api.getProfile();
    profileSelect.innerHTML = data.profiles.map((p) => `<option value="${p}">${p}</option>`).join("");
    profileSelect.value = data.active;
  } catch (e) {
    // Pure sqlite and independent of kRPC, but the server itself may not
    // be up yet on a cold start.
  }
}

document.getElementById("profile-switch-btn").addEventListener("click", async () => {
  const name = profileNewInput.value.trim() || profileSelect.value;
  if (!name) return;
  try {
    await api.setProfile(name);
  } catch (e) {
    return;
  }
  profileNewInput.value = "";
  await refreshProfile();
  // Constellations are scoped per profile, so whatever is on screen is now
  // the wrong save's. Remount the active tab to reload it.
  const current = activeName;
  activeName = null;
  showTab(current);
});

// --- Boot ----------------------------------------------------------------

refreshProfile();
telemetry.connect();
showTab("overview");
