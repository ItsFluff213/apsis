"""Auto-docking: rendezvous with another craft, align, dock, and move
resources between the two once joined.

Previously this was the one advertised feature with no implementation at
all -- a placeholder string in the dashboard saying it wasn't built yet.

The job splits into four phases with genuinely different characters, which
is why they are four separate functions rather than one loop:

  1. Rendezvous -- orbital mechanics. Kilometres to hundreds of kilometres,
     solved with timed maneuver nodes (Hohmann transfer + phasing), exactly
     like every other transfer in this project.
  2. Approach -- no longer orbital mechanics. Inside a few kilometres the
     two craft are effectively in the same orbit, and what matters is the
     relative position/velocity vector, flown on RCS.
  3. Alignment and contact -- attitude, not translation: get the two ports
     facing each other and creep in.
  4. Resource transfer -- once hard-docked, a plumbing problem.

Scope, stated up front rather than discovered in flight:
  * Both craft must already be in orbit around the same body. This does not
    launch to a rendezvous or transfer between bodies -- do that first with
    the ascent and transfer autopilots.
  * The plane match corrects inclination *magnitude* at the ascending node.
    It does not correct a difference in longitude of ascending node, which
    is a genuinely expensive burn and usually means the target was launched
    into a different plane entirely. Badly mismatched planes will show up
    as a rendezvous that never closes; launch into the target's plane
    instead.
  * The approach assumes RCS with translation authority on all axes and
    enough monopropellant. A craft without RCS cannot dock this way, and
    kRPC gives no way to fake it.
"""

import math

from backend import orbital, parts
from backend.autopilots import maneuver

# Rendezvous is "done" and the RCS approach begins at this separation.
APPROACH_HANDOVER_M = 2000.0

# Where to park before the final axial run-in: straight out along the
# target port's normal, so the last stretch is a straight line with no
# lateral correction needed.
HOLD_POINT_M = 50.0

# Closing speeds. Deliberately timid -- a docking collision destroys both
# craft, and the time saved by rushing is trivial next to the transfer that
# got here.
FAR_CLOSING_SPEED_MS = 20.0
NEAR_CLOSING_SPEED_MS = 2.0
CONTACT_SPEED_MS = 0.3

ALIGNMENT_TOLERANCE_DEG = 2.0
LATERAL_TOLERANCE_M = 0.4


def _relative_position(vessel, frame):
    """Vessel's position in some other object's reference frame."""
    return vessel.position(frame)


def _distance(a, b):
    return math.sqrt(sum((x - y) ** 2 for x, y in zip(a, b)))


def _set_translation(control, direction_in_vessel_frame):
    """Drive the RCS translation controls along a direction expressed in
    the vessel's own reference frame.

    kRPC's vessel frame is x=right, y=forward (out of the nose), z=out of
    the bottom -- so "up" is -z, which is the one that catches people out.
    Components are clamped to [-1, 1] since they are throttle fractions,
    not velocities.
    """
    x, y, z = direction_in_vessel_frame

    def clamp(v):
        return max(-1.0, min(1.0, v))

    control.right = clamp(x)
    control.forward = clamp(y)
    control.up = clamp(-z)


def _stop_translation(control):
    control.right = 0.0
    control.forward = 0.0
    control.up = 0.0


def _pick_port(vessel, port_label=None):
    """Choose which docking port on a craft to use. An explicit port label
    wins -- matched against parts.get_docking_ports's labelling, which
    covers both a real `dock.<detail>` tag and the synthetic `port0`,
    `port1`, ... labels it falls back to for untagged ports, so a label
    handed back by list_docking_ports always resolves here too, not just a
    manually-typed tag. Otherwise the `dock.front` convention; otherwise any
    free port."""
    labelled = parts.get_docking_ports(vessel)
    if port_label and port_label in labelled:
        return labelled[port_label].docking_port
    tagged = parts.get_tagged_parts(vessel).get("dock", {})
    if "front" in tagged:
        return tagged["front"].docking_port

    for port in vessel.parts.docking_ports:
        try:
            if port.state.name == "ready":
                return port
        except Exception:
            continue
    return None


# --- Phase 1: rendezvous -------------------------------------------------

def _match_planes(client, vessel, job, target):
    """Null the inclination difference at the ascending node, if it is big
    enough to matter. Left alone below the threshold: a plane change is one
    of the most expensive burns there is, and a fraction of a degree costs
    far more to fix than it costs to fly through."""
    own_inclination = math.degrees(vessel.orbit.inclination)
    target_inclination = math.degrees(target.orbit.inclination)
    if abs(own_inclination - target_inclination) <= 0.5:
        return

    job.message = f"matching {target.name}'s orbital plane"
    node = maneuver.change_inclination_node(client, vessel, target_inclination)
    maneuver.execute_node(client, vessel, job, node)


def _hohmann_to_target_orbit(client, vessel, job, target):
    """Transfer to the target's orbital altitude, timed so the target is
    where the vessel arrives.

    Same two-impulse Hohmann as an interplanetary transfer, one more level
    down -- the required departure phase angle comes from the same
    orbital.hohmann_phase_angle used for planets.
    """
    sc = client.space_center
    body = vessel.orbit.body
    mu = body.gravitational_parameter
    frame = body.non_rotating_reference_frame

    r1 = vessel.orbit.semi_major_axis
    r2 = target.orbit.semi_major_axis
    if abs(r1 - r2) / max(r2, 1.0) < 0.005:
        return  # already effectively co-orbital; phasing alone will do it

    required_phase = orbital.hohmann_phase_angle(mu, r1, r2)
    own_angle = orbital.angle_of(vessel.position(frame))
    target_angle = orbital.angle_of(target.position(frame))
    current_phase = target_angle - own_angle

    rate_own = 2 * math.pi / vessel.orbit.period
    rate_target = 2 * math.pi / target.orbit.period
    relative_rate = rate_target - rate_own
    if abs(relative_rate) < 1e-12:
        return

    delta = (required_phase - current_phase) % (2 * math.pi)
    wait = delta / relative_rate if relative_rate > 0 else (delta - 2 * math.pi) / relative_rate
    departure_ut = sc.ut + wait

    if wait > 30:
        job.message = f"waiting for the transfer window to {target.name}"
        while sc.ut < departure_ut - 30:
            job.check_abort()
            sc.warp_to(min(sc.ut + 600, departure_ut - 20))
        while sc.ut < departure_ut - 5:
            job.check_abort()
            job.sleep(0.2)

    job.message = f"transferring to {target.name}'s orbit"
    target_altitude = r2 - body.equatorial_radius
    node = maneuver.adjust_other_apsis_now(client, vessel, target_altitude)
    maneuver.execute_node(client, vessel, job, node)

    # Circularize at the new altitude, or the vessel just swings back down
    # past the target every orbit instead of staying alongside it.
    job.message = "circularizing alongside the target"
    node = maneuver.circularize_node(client, vessel, at="apoapsis" if r2 > r1 else "periapsis")
    maneuver.execute_node(client, vessel, job, node)


def _phase_to_target(client, vessel, job, target, max_attempts=6):
    """Close the remaining along-track gap by nudging the orbital period,
    drifting, and re-circularizing -- the standard way to catch something
    in the same orbit, since simply thrusting toward it raises the orbit
    and makes the vessel slower, not faster."""
    sc = client.space_center
    body = vessel.orbit.body
    frame = body.non_rotating_reference_frame

    for _ in range(max_attempts):
        job.check_abort()
        separation = _distance(vessel.position(frame), target.position(frame))
        if separation <= APPROACH_HANDOVER_M:
            return True

        own_angle = orbital.angle_of(vessel.position(frame))
        target_angle = orbital.angle_of(target.position(frame))
        angle_to_close = math.degrees(orbital.signed_angle_difference(target_angle, own_angle))
        if abs(angle_to_close) < 0.05:
            # Angularly together but still far apart means the gap is
            # radial or out-of-plane, which phasing cannot fix.
            return separation <= APPROACH_HANDOVER_M

        job.message = f"phasing {angle_to_close:.1f} deg to close on {target.name}"
        node, phase_period, phase_periapsis = maneuver.phasing_node(
            client, vessel, angle_to_close, num_orbits=1, burn_at="periapsis",
        )
        min_safe = (body.atmosphere_depth or 0) + 10000
        if phase_periapsis < min_safe:
            node.remove()
            node, phase_period, phase_periapsis = maneuver.phasing_node(
                client, vessel, angle_to_close, num_orbits=3, burn_at="periapsis",
            )
            if phase_periapsis < min_safe:
                node.remove()
                job.message = "can't phase safely -- too large a gap to close from this orbit"
                return False

        maneuver.execute_node(client, vessel, job, node)

        end_ut = sc.ut + phase_period
        job.message = "drifting into position"
        if end_ut - sc.ut > 30:
            sc.warp_to(end_ut - 20)
        while sc.ut < end_ut:
            job.check_abort()
            job.sleep(0.5)

        job.message = "recircularizing"
        node = maneuver.circularize_node(client, vessel, at="apoapsis")
        maneuver.execute_node(client, vessel, job, node)

    return _distance(vessel.position(frame), target.position(frame)) <= APPROACH_HANDOVER_M


# --- Phase 2/3: RCS approach, alignment, contact -------------------------

def _kill_relative_velocity(client, vessel, job, target, tolerance=0.2, timeout_s=120):
    """Match velocity with the target. Everything in the approach assumes a
    near-stationary starting point relative to the target, so this runs
    before any deliberate translation."""
    control = vessel.control
    control.rcs = True
    target_frame = target.reference_frame
    job.message = "matching velocity with target"

    elapsed = 0.0
    while elapsed < timeout_s:
        job.check_abort()
        relative_velocity = vessel.velocity(target_frame)
        speed = orbital.magnitude(relative_velocity)
        if speed <= tolerance:
            _stop_translation(control)
            return True
        # Thrust opposite the relative velocity, expressed in our own frame.
        correction = client.space_center.transform_direction(
            tuple(-c for c in orbital.norm(relative_velocity)), target_frame, vessel.reference_frame,
        )
        gain = min(1.0, speed / 5.0)
        _set_translation(control, tuple(c * gain for c in correction))
        job.sleep(0.1)
        elapsed += 0.1

    _stop_translation(control)
    return False


def _fly_to_offset(client, vessel, job, reference_frame, offset, arrive_within,
                   max_speed, timeout_s=600):
    """Translate on RCS until the vessel sits at `offset` (a position in
    `reference_frame`), holding a speed proportional to remaining distance
    so it decelerates into the point instead of sailing past it."""
    sc = client.space_center
    control = vessel.control
    control.rcs = True

    elapsed = 0.0
    while elapsed < timeout_s:
        job.check_abort()
        position = vessel.position(reference_frame)
        error = tuple(o - p for o, p in zip(offset, position))
        distance = orbital.magnitude(error)
        if distance <= arrive_within:
            _stop_translation(control)
            return True

        # Desired velocity: toward the point, slowing as it approaches.
        desired_speed = max(0.1, min(max_speed, distance * 0.3))
        desired_velocity = tuple(c * desired_speed for c in orbital.norm(error))
        actual_velocity = vessel.velocity(reference_frame)
        correction = tuple(d - a for d, a in zip(desired_velocity, actual_velocity))

        command = sc.transform_direction(
            orbital.norm(correction), reference_frame, vessel.reference_frame,
        )
        gain = min(1.0, orbital.magnitude(correction) / 2.0)
        _set_translation(control, tuple(c * gain for c in command))

        job.message = f"approaching -- {distance:.0f} m to go"
        job.sleep(0.1)
        elapsed += 0.1

    _stop_translation(control)
    return False


def _align_with_port(client, vessel, job, own_port, target_port, timeout_s=120):
    """Point our port's normal straight into the target port's normal.

    Two ports dock when they face each other, so our port must point along
    the *negative* of the target port's outward normal. The autopilot is
    told to hold that using our own port as the reference frame, which also
    keeps the roll matched -- some port types need that to latch.
    """
    ap = vessel.auto_pilot
    job.message = "aligning with docking port"

    ap.reference_frame = target_port.reference_frame
    # In a docking port's own frame, +y is straight out of the port face.
    # We must present our face to it, i.e. point back down its axis.
    ap.target_direction = (0, -1, 0)
    ap.target_roll = 0.0
    ap.engaged = True

    elapsed = 0.0
    while elapsed < timeout_s:
        job.check_abort()
        if ap.error <= ALIGNMENT_TOLERANCE_DEG:
            return True
        job.sleep(0.2)
        elapsed += 0.2
    return False


def _final_approach(client, vessel, job, own_port, target_port, timeout_s=300):
    """The last few metres: creep straight down the port axis until the
    ports latch. Lateral drift is corrected continuously, because at this
    range a small sideways error is a glancing collision rather than a
    dock."""
    sc = client.space_center
    control = vessel.control
    control.rcs = True
    frame = target_port.reference_frame

    job.message = "final approach"
    elapsed = 0.0
    while elapsed < timeout_s:
        job.check_abort()

        state = target_port.state.name
        if state in ("docked", "docking"):
            _stop_translation(control)
            return True

        # Our port's position in the target port's frame. +y is the axis
        # we should be coming in along; x and z are pure error.
        position = own_port.part.position(frame)
        axial = position[1]
        lateral = math.sqrt(position[0] ** 2 + position[2] ** 2)

        if axial <= 0.15 and lateral <= LATERAL_TOLERANCE_M:
            # Close enough that the magnets should take it from here.
            _stop_translation(control)
            job.sleep(1.0)
            if target_port.state.name in ("docked", "docking"):
                return True

        # Aim for a point just inside the target port, so the controller
        # always has a little closing bias.
        target_offset = (0.0, -0.05, 0.0)
        error = tuple(t - p for t, p in zip(target_offset, position))

        # Lateral error gets priority: never close axially while off-axis.
        closing_speed = CONTACT_SPEED_MS if lateral <= LATERAL_TOLERANCE_M else 0.0
        desired_velocity = (
            error[0] * 0.5,
            -closing_speed if axial > 0 else closing_speed,
            error[2] * 0.5,
        )
        actual_velocity = vessel.velocity(frame)
        correction = tuple(d - a for d, a in zip(desired_velocity, actual_velocity))
        command = sc.transform_direction(
            orbital.norm(correction), frame, vessel.reference_frame,
        )
        gain = min(1.0, orbital.magnitude(correction) / 0.5)
        _set_translation(control, tuple(c * gain for c in command))

        job.message = f"docking -- {axial:.1f} m axial, {lateral:.2f} m lateral"
        job.sleep(0.1)
        elapsed += 0.1

    _stop_translation(control)
    return False


# --- Entry points --------------------------------------------------------

def run_docking(client, registry, vessel, job, target_vessel_id, own_port_tag=None, target_port_tag=None,
                skip_rendezvous=False):
    """Rendezvous with another craft and dock with it.

    skip_rendezvous=True is the "just docking" mode: go straight to the RCS
    approach (phase 2 onward) and never touch phase 1's plane-match/Hohmann/
    phasing sequence, regardless of how far apart the craft currently are.

    Phase 1 already skips itself automatically when the craft are already
    within APPROACH_HANDOVER_M -- so ordinarily nothing needs to ask for
    this. It exists for the times that automatic distance check isn't what
    you want: the craft were brought close by hand (or by a previous manual
    rendezvous) but the *shape* of their orbits still differs enough that
    the distance check wouldn't trigger reliably, or you'd simply rather
    fly the approach yourself-by-autopilot without spending time and fuel
    on a rendezvous sequence you know you don't need.
    """
    sc = client.space_center

    target = registry.get_vessel_object(target_vessel_id)
    if target is None:
        raise ValueError(f"unknown target vessel {target_vessel_id!r}")
    if target == vessel:
        raise ValueError("a craft cannot dock with itself")
    if target.orbit.body != vessel.orbit.body:
        raise ValueError(
            f"{vessel.name} is at {vessel.orbit.body.name} but {target.name} is at "
            f"{target.orbit.body.name} -- both craft must be around the same body to dock"
        )

    if vessel != sc.active_vessel:
        sc.active_vessel = vessel
        job.sleep(0.5)
    if sc.rails_warp_factor != 0 or sc.physics_warp_factor != 0:
        sc.rails_warp_factor = 0
        sc.physics_warp_factor = 0
        job.sleep(1.0)

    # Only our own port is picked here -- `vessel` was just made the active
    # vessel above, and the active vessel is always fully loaded, so this
    # is always safe to check immediately.
    own_port = _pick_port(vessel, own_port_tag)
    if own_port is None:
        raise ValueError(f"{vessel.name} has no free docking port -- tag one as dock.front if it does")

    sc.target_vessel = target

    # --- Phase 1: rendezvous (unless explicitly skipped) ---
    frame = vessel.orbit.body.non_rotating_reference_frame
    distance = _distance(vessel.position(frame), target.position(frame))
    if skip_rendezvous:
        job.message = f"skipping rendezvous -- going straight to approach ({distance:.0f} m out)"
    elif distance > APPROACH_HANDOVER_M:
        _match_planes(client, vessel, job, target)
        _hohmann_to_target_orbit(client, vessel, job, target)
        if not _phase_to_target(client, vessel, job, target):
            raise ValueError(
                f"couldn't close on {target.name} -- the orbits are probably too dissimilar "
                f"for this to fix from here (see the plane-matching limits in docking.py)"
            )

    # The target's port is picked HERE, not before phase 1, and deliberately
    # not any earlier. kRPC returns an empty part list -- not an error, not
    # a warning, just zero parts -- for any vessel outside physics loading
    # range, which looks structurally identical to "this craft genuinely
    # has no docking port." Confirmed live: picking the target's port before
    # rendezvous misdiagnosed a real, ported station as portless, because it
    # was still hundreds of km away and kRPC couldn't see any of its parts
    # yet. By this point in the sequence the craft should be within
    # APPROACH_HANDOVER_M (or the caller asserted skip_rendezvous, meaning
    # they're claiming to already be close), so the target should actually
    # be loaded -- but check for the unloaded case explicitly anyway rather
    # than let it silently reappear as the same misleading "no free
    # docking port" message under different circumstances.
    if len(target.parts.all) == 0:
        raise ValueError(
            f"{target.name} isn't loaded into physics range, so its parts can't be inspected yet -- "
            f"get within a few km of it (or don't pass skip_rendezvous if you're not actually close) "
            f"before docking can pick a port on it"
        )
    target_port = _pick_port(target, target_port_tag)
    if target_port is None:
        raise ValueError(f"{target.name} has no free docking port")

    # --- Phase 2: RCS approach ---
    sc.target_docking_port = target_port
    control = vessel.control
    control.sas = False
    control.rcs = True

    try:
        if not _kill_relative_velocity(client, vessel, job, target):
            raise ValueError("couldn't null relative velocity -- is there RCS fuel and thruster authority?")

        # Park on the port's axis before approaching, so the run-in is a
        # straight line rather than a curve that has to be corrected all
        # the way down.
        job.message = "moving to the approach corridor"
        if not _fly_to_offset(
            client, vessel, job, target_port.reference_frame,
            offset=(0.0, HOLD_POINT_M, 0.0), arrive_within=3.0, max_speed=FAR_CLOSING_SPEED_MS,
        ):
            raise ValueError("timed out flying to the approach hold point")

        _kill_relative_velocity(client, vessel, job, target)

        # --- Phase 3: align and dock ---
        if not _align_with_port(client, vessel, job, own_port, target_port):
            raise ValueError("couldn't hold docking alignment")

        if not _fly_to_offset(
            client, vessel, job, target_port.reference_frame,
            offset=(0.0, 5.0, 0.0), arrive_within=1.0, max_speed=NEAR_CLOSING_SPEED_MS,
        ):
            raise ValueError("timed out closing to contact range")

        if not _final_approach(client, vessel, job, own_port, target_port):
            raise ValueError("final approach timed out without a latch")
    finally:
        _stop_translation(control)
        vessel.auto_pilot.engaged = False

    job.message = f"docked with {target.name}"


def run_resource_transfer(client, vessel, job, resource_name, amount=None, to_target=True):
    """Move a resource between two docked craft.

    Direction is expressed relative to this vessel: to_target=True pushes
    the resource out of this craft (refuelling the thing it is docked to),
    False pulls it in. `amount` of None means "as much as will move".

    Requires the craft to be docked already -- once docked, KSP treats both
    as a single vessel, so the transfer is between two parts of one craft
    rather than between two vessels.
    """
    sc = client.space_center

    # Find a part holding the resource, and a part with room for it. After
    # docking, both craft's tanks are parts of the same vessel.
    sources = []
    destinations = []
    for part in vessel.parts.all:
        try:
            res = part.resources
            if resource_name not in res.names:
                continue
            if res.amount(resource_name) > 0.01:
                sources.append(part)
            if res.max(resource_name) - res.amount(resource_name) > 0.01:
                destinations.append(part)
        except Exception:
            continue

    if not sources:
        raise ValueError(f"no part on {vessel.name} is holding any {resource_name}")
    if not destinations:
        raise ValueError(f"no part on {vessel.name} has room for more {resource_name}")

    # Fullest source to emptiest destination -- moves the most per transfer
    # and avoids picking a nearly-empty tank as the source.
    source = max(sources, key=lambda p: p.resources.amount(resource_name))
    destination = max(
        (d for d in destinations if d != source),
        key=lambda p: p.resources.max(resource_name) - p.resources.amount(resource_name),
        default=None,
    )
    if destination is None:
        raise ValueError(f"only one part holds {resource_name} -- nothing to transfer between")

    if not to_target:
        source, destination = destination, source

    max_amount = amount if amount is not None else source.resources.amount(resource_name)
    job.message = f"transferring {max_amount:.0f} units of {resource_name}"

    transfer = sc.ResourceTransfer.start(source, destination, resource_name, max_amount)
    while not transfer.complete:
        job.check_abort()
        job.sleep(0.2)

    job.message = f"transferred {transfer.amount:.0f} units of {resource_name}"
