// Shared conic-section math for drawing orbits on the 3D map.
//
// This exists because the same "point on an ellipse, tilted out of plane"
// calculation used to be written three times, independently, in three
// places -- the planet orbit rings, the live vessel orbit/trajectory, and
// the moon-transfer preview line -- and one of the three was wrong.
//
// The bug: a vessel's position was computed as
//   x = semiMajor * cos(trueAnomaly), z = semiMinor * sin(trueAnomaly)
// which is the CENTER-parametric form of an ellipse. That formula is only
// correct when the parameter is the ECCENTRIC anomaly. Feeding it the true
// anomaly instead -- which is what telemetry actually provides -- silently
// draws a mathematically different curve: same overall shape and apsides,
// but at any other point in the orbit the plotted position is wrong,
// growing to 10-15% of the orbit's radius at e=0.6 and worse for more
// eccentric orbits. Since a body sits at the *focus* of a real orbital
// ellipse, not its center, the correct relation between true anomaly and
// radius is the polar conic equation used below -- which the planet rings
// and the moon-transfer preview already happened to get right
// independently. This file makes that the only version, so a vessel with
// any real eccentricity (which is most orbits that aren't a circularized
// parking orbit) is now drawn where it actually is.
//
// Dependency-free, like the rest of core/ -- returns plain {x, y, z}
// objects, not THREE.Vector3, so callers can use whichever 3D library (or
// none) they like.

/**
 * Position on an orbit at a given true anomaly, in a frame where the
 * focus (the body being orbited) is the origin.
 *
 * `argumentOfPeriapsisRad` orients the ellipse within its own plane --
 * without it, every orbit is drawn as if periapsis pointed the same
 * arbitrary direction, which used to be the case for every vessel (the
 * data simply wasn't collected). `inclinationRad` then tilts that plane
 * out of the reference plane by rotating the in-plane "sideways" axis
 * into up/down -- a simplified, single-axis tilt (no separate longitude
 * of ascending node), consistent with the approximation already used
 * throughout this project's transfer-planning math, not a new one
 * introduced here.
 */
export function orbitPosition({
  semiMajor,
  eccentricity = 0,
  argumentOfPeriapsisRad = 0,
  inclinationRad = 0,
  trueAnomalyRad,
}) {
  const e = eccentricity;
  // Polar conic equation, focus at the origin. At e=0 this reduces to the
  // circular case (r = semiMajor) regardless of true anomaly, so circular
  // orbits render identically to before -- only eccentric ones change.
  const r = (semiMajor * (1 - e * e)) / (1 + e * Math.cos(trueAnomalyRad));
  const absoluteAngle = argumentOfPeriapsisRad + trueAnomalyRad;
  const flatX = r * Math.cos(absoluteAngle);
  const flatZ = r * Math.sin(absoluteAngle);
  return {
    x: flatX,
    y: flatZ * Math.sin(inclinationRad),
    z: flatZ * Math.cos(inclinationRad),
  };
}

/** The same orbit traced out as a closed loop of points, for drawing a ring/line. */
export function orbitRingPoints(params, segments = 128) {
  const points = [];
  for (let i = 0; i <= segments; i++) {
    points.push(orbitPosition({ ...params, trueAnomalyRad: (i / segments) * Math.PI * 2 }));
  }
  return points;
}
