// Every backend call the dashboard makes, in one place.
//
// Two reasons this is a module rather than fetch() scattered through the
// views. First, error handling: the old code called fetch() and ignored
// the result, so a rejected autopilot request (bad altitude, vessel not in
// orbit, kRPC disconnected) looked exactly like a successful one -- the
// button clicked and nothing happened. `request` surfaces the backend's
// error detail instead. Second, the standalone satellites page needs the
// constellation and vessel endpoints and nothing else; having them named
// here is what makes "it only talks to the backend over REST" checkable
// rather than a claim.

async function request(path, { method = "GET", body } = {}) {
  const options = { method };
  if (body !== undefined) {
    options.headers = { "Content-Type": "application/json" };
    options.body = JSON.stringify(body);
  }

  const res = await fetch(path, options);
  let payload = null;
  try {
    payload = await res.json();
  } catch (e) {
    payload = null; // some endpoints legitimately return no body
  }

  if (!res.ok) {
    const detail = (payload && payload.detail) || `HTTP ${res.status}`;
    throw new Error(detail);
  }
  return payload;
}

// --- Vessels -------------------------------------------------------------

export const listVessels = () => request("/api/vessels");
export const renameVessel = (id, name) =>
  request(`/api/vessels/${encodeURIComponent(id)}/name`, { method: "POST", body: { name } });
export const setVesselRole = (id, category, detail = "") =>
  request(`/api/vessels/${encodeURIComponent(id)}/role`, { method: "POST", body: { category, detail } });
export const listParts = (id) => request(`/api/vessels/${encodeURIComponent(id)}/parts`);

// --- Autopilot -----------------------------------------------------------

const autopilot = (id, suffix) => `/api/autopilot/${encodeURIComponent(id)}${suffix}`;

export const startAscent = (id, targetApoapsisM, targetPeriapsisM, inclinationDeg) =>
  request(autopilot(id, "/ascent"), {
    method: "POST",
    body: {
      target_apoapsis_m: targetApoapsisM,
      target_periapsis_m: targetPeriapsisM,
      target_inclination_deg: inclinationDeg,
    },
  });

export const startMoonTransfer = (id, moonName, periapsisM, inclinationDeg) =>
  request(autopilot(id, "/moon-transfer"), {
    method: "POST",
    body: {
      moon_name: moonName,
      target_periapsis_m: periapsisM,
      target_inclination_deg: inclinationDeg,
    },
  });

export const previewMoonTransfer = (id, moonName) =>
  request(autopilot(id, `/moon-transfer/preview?moon_name=${encodeURIComponent(moonName)}`));

export const startPlanetTransfer = (id, targetBodyName, periapsisM, inclinationDeg) =>
  request(autopilot(id, "/planet-transfer"), {
    method: "POST",
    body: {
      target_body_name: targetBodyName,
      target_periapsis_m: periapsisM,
      target_inclination_deg: inclinationDeg,
    },
  });

export const previewPlanetTransfer = (id, targetBodyName) =>
  request(autopilot(id, `/planet-transfer/preview?target_body_name=${encodeURIComponent(targetBodyName)}`));

export const startLanding = (id, lat, lon) =>
  request(autopilot(id, "/landing"), { method: "POST", body: { target_lat: lat, target_lon: lon } });

export const startBoosterReturn = (id) => request(autopilot(id, "/booster-return"), { method: "POST" });

export const startDocking = (id, targetVesselId, ownPortTag = null, targetPortTag = null) =>
  request(autopilot(id, "/dock"), {
    method: "POST",
    body: { target_vessel_id: targetVesselId, own_port_tag: ownPortTag, target_port_tag: targetPortTag },
  });

export const transferResource = (id, resourceName, amount = null, toTarget = true) =>
  request(autopilot(id, "/transfer-resource"), {
    method: "POST",
    body: { resource_name: resourceName, amount, to_target: toTarget },
  });

export const abortAutopilot = (id) => request(autopilot(id, "/abort"), { method: "POST" });

// --- Constellations (also used by the standalone satellites page) --------

export const listConstellations = () => request("/api/constellations");
export const createConstellation = (payload) =>
  request("/api/constellations", { method: "POST", body: payload });
export const updateConstellationOrbit = (cid, altitudeM, inclinationDeg) =>
  request(`/api/constellations/${cid}/orbit`, {
    method: "PATCH",
    body: { altitude_m: altitudeM, inclination_deg: inclinationDeg },
  });
export const deleteConstellation = (cid) => request(`/api/constellations/${cid}`, { method: "DELETE" });
export const addConstellationMember = (cid, vesselId) =>
  request(`/api/constellations/${cid}/members`, { method: "POST", body: { vessel_id: vesselId } });
export const removeConstellationMember = (cid, vesselId) =>
  request(`/api/constellations/${cid}/members/${encodeURIComponent(vesselId)}`, { method: "DELETE" });
export const deployToConstellation = (cid, vesselId) =>
  request(`/api/constellations/${cid}/deploy/${encodeURIComponent(vesselId)}`, { method: "POST" });
export const syncConstellations = (sourceUrl) =>
  request("/api/constellations/sync/pull", { method: "POST", body: { source_url: sourceUrl } });

// --- Misc ----------------------------------------------------------------

export const listWaypoints = () => request("/api/waypoints");
export const getProfile = () => request("/api/profile");
export const setProfile = (name) => request("/api/profile", { method: "POST", body: { name } });
