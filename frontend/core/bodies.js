// The Kerbol system: its structure, and the live positions polled from
// /api/system.
//
// Layout rules, unchanged from the original implementation because they
// are the result of trial and error against what actually reads well:
//
//   * Planets (direct children of the Sun) use their REAL semi-major axis
//     once /api/system loads it, scaled by SMA_SCALE -- true relative
//     proportions, like zooming out in KSP's own tracking station. The
//     schematic `radius` below is only the fallback shown before that
//     first loads.
//   * Moons keep a small fixed schematic radius around their parent
//     regardless. At true interplanetary scale a moon's orbit is a
//     fraction of a pixel wide, so real-scale moons would vanish into
//     their planet's dot at any zoom that also shows the system.
//   * Moon radii use widening rather than linear steps, so a planet with
//     several moons (Jool's five) doesn't bunch them into a solid blob.
//
// The (x, y) plane produced here is handed to map3d.js, which renders it
// as real 3D (x, 0, z) world coordinates.

const SMA_SCALE = 170000000; // world-units per metre, calibrated so Kerbin ~= 80 units

export const SYSTEM_TREE = {
  name: "Sun", radius: 0, angle: 0, topLevel: false, children: [
    { name: "Moho", radius: 40, angle: 10, topLevel: true },
    { name: "Eve", radius: 60, angle: 60, topLevel: true, children: [{ name: "Gilly", radius: 15, angle: 0 }] },
    { name: "Kerbin", radius: 80, angle: 120, topLevel: true, children: [
        { name: "Mun", radius: 15, angle: 0 }, { name: "Minmus", radius: 24, angle: 140 },
    ] },
    { name: "Duna", radius: 100, angle: 170, topLevel: true, children: [{ name: "Ike", radius: 15, angle: 0 }] },
    { name: "Dres", radius: 118, angle: 220, topLevel: true },
    { name: "Jool", radius: 138, angle: 270, topLevel: true, children: [
        { name: "Laythe", radius: 13, angle: 0 }, { name: "Vall", radius: 20, angle: 72 },
        { name: "Tylo", radius: 28, angle: 144 }, { name: "Bop", radius: 37, angle: 216 },
        { name: "Pol", radius: 47, angle: 288 },
    ] },
    { name: "Eeloo", radius: 155, angle: 320, topLevel: true },
  ],
};

export const ALL_BODY_NAMES = (function flatten(node, out) {
  out.push(node.name);
  for (const child of node.children || []) flatten(child, out);
  return out;
})(SYSTEM_TREE, []);

// Planets orbit the Sun; moons orbit a planet. The transfer UI needs them
// separated because they route to different endpoints -- a moon transfer
// never leaves its parent's sphere of influence, an interplanetary one has
// to solve an ejection angle. See backend/autopilots/planet_transfer.py.
export const PLANET_NAMES = SYSTEM_TREE.children.map((p) => p.name);
export const MOON_NAMES = SYSTEM_TREE.children.flatMap((p) => (p.children || []).map((m) => m.name));

export function findSystemNode(name, node = SYSTEM_TREE) {
  if (node.name === name) return node;
  for (const child of node.children || []) {
    const found = findSystemNode(name, child);
    if (found) return found;
  }
  return null;
}

/** Which planet a moon belongs to, or null if it isn't a moon. */
export function parentPlanetOf(moonName) {
  for (const planet of SYSTEM_TREE.children) {
    if ((planet.children || []).some((m) => m.name === moonName)) return planet.name;
  }
  return null;
}

// --- Live body data ------------------------------------------------------

const live = {
  angles: new Map(),   // name -> absolute angle deg (argp + true anomaly)
  sma: new Map(),      // name -> semi-major axis, m
  radius: new Map(),   // name -> real instantaneous orbital radius, m
  ecc: new Map(),      // name -> eccentricity
  argp: new Map(),     // name -> argument of periapsis, deg
};

export async function refreshSystem() {
  try {
    const res = await fetch("/api/system");
    const bodies = await res.json();
    live.angles = new Map(bodies.map((b) => [b.name, b.angle_deg]));
    live.sma = new Map(bodies.filter((b) => b.semi_major_axis_m).map((b) => [b.name, b.semi_major_axis_m]));
    live.radius = new Map(bodies.filter((b) => b.radius_m).map((b) => [b.name, b.radius_m]));
    live.ecc = new Map(bodies.map((b) => [b.name, b.eccentricity || 0]));
    live.argp = new Map(bodies.map((b) => [b.name, b.argument_of_periapsis_deg || 0]));
  } catch (e) {
    // kRPC not connected yet, or the request failed. Keep the last known
    // data rather than blanking the map.
  }
  return layoutSystem();
}

// Real instantaneous orbital radius (not the fixed semi-major axis), so a
// visibly eccentric body -- Moho, Eeloo -- sits where it actually is right
// now rather than on a circle it only touches at two points.
function radiusFor(node) {
  if (node.topLevel && live.radius.has(node.name)) {
    return live.radius.get(node.name) / SMA_SCALE;
  }
  return node.radius;
}

/**
 * World-space layout with the Sun at the origin, handed to map3d.js as
 * (x, 0, z). Each top-level entry also carries its real orbit shape so the
 * map can draw the actual ellipse rather than a circle through the current
 * radius.
 */
export function layoutSystem() {
  const positions = new Map();

  function place(node, originX, originY) {
    const angle = live.angles.has(node.name) ? live.angles.get(node.name) : node.angle;
    const radius = radiusFor(node);
    const rad = (angle * Math.PI) / 180;
    const x = originX + radius * Math.cos(rad);
    const y = originY + radius * Math.sin(rad);

    let orbitShape = null;
    if (node.topLevel && live.sma.has(node.name)) {
      orbitShape = {
        smaScaled: live.sma.get(node.name) / SMA_SCALE,
        eccentricity: live.ecc.get(node.name) || 0,
        argpDeg: live.argp.get(node.name) || 0,
      };
    }

    positions.set(node.name, {
      x, y, isMoon: !node.topLevel && node.name !== "Sun", originX, originY, orbitShape,
    });
    for (const child of node.children || []) place(child, x, y);
  }

  place(SYSTEM_TREE, 0, 0);
  return positions;
}
