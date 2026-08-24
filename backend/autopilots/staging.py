"""Shared auto-staging: drop a spent stage so the next one can take over.

Every autopilot that runs an engine needs the same sequence -- notice the
current stage is dry, cut throttle, fire whatever decoupler belongs to that
stage (preferring one the user tagged in-game), activate the stage, let the
craft settle, then commit back to thrust. This used to exist twice, written
separately: once in ascent.py for the launch climb, and once in maneuver.py
(`_stage_if_dry`) for burns that outlast their stage. Both carried the same
pair of state (which decouplers belong to which stage, which stages have
already been fired) and the same kRPC quirk workarounds, so they live here
once now and every autopilot -- including ones added later -- gets the
behavior for free.

The two call sites differ in exactly two ways, both preserved as arguments
rather than flattened away:

  * Whether an empty-fuel check gates the staging (see `is_stage_empty`).
    During a burn, thrust reading zero always means dry -- the engine was
    already firing. On the pad it does not: the very first activation
    ignites the top stage, which also reads zero thrust beforehand because
    nothing has fired yet. Confirmed live: applying the burn-time rule at
    launch blocked ignition entirely and the rocket never left the pad.
  * How long to settle afterward. A mid-burn stage just needs the new
    engine to spool up; an ascent stage needs the autopilot to reconfirm
    attitude after the mass/inertia shift of separation before going back
    to full thrust.
"""

from backend import parts

FUEL_TYPES = ("LiquidFuel", "Oxidizer", "SolidFuel")
NO_THRUST = 0.1  # N -- below this the stage is producing nothing usable


class Stager:
    """Tracks staging state for one vessel across a whole autopilot job.

    Build one per job (not per tick) -- it caches the vessel's tagged
    decouplers and remembers which stages it has already fired, so the same
    stage can never be dropped twice.
    """

    def __init__(self, vessel):
        self.vessel = vessel
        self.decouplers_by_stage = parts.get_decouplers_by_stage(vessel)
        self.fired_stages = set()

    def is_stage_empty(self, stage_num):
        """True if the given stage's parts hold no meaningful propellant.

        `available_thrust` hitting zero already implies the *active* engine
        is dry, but that only describes the engine's current draw. This
        checks the stage's actual remaining resources, so a stage is only
        ever dropped once it is genuinely spent -- not merely because the
        engine that happens to be firing reads no thrust.
        """
        try:
            res = self.vessel.resources_in_decouple_stage(stage_num, cumulative=False)
            for name in FUEL_TYPES:
                if name in res.names and res.amount(name) > 0.1:
                    return False
            return True
        except Exception:
            return True  # can't tell -- don't block staging on an unknown

    def should_stage(self, verify_empty=True):
        """Whether the current stage is spent and hasn't been dropped yet.

        `verify_empty=False` skips the propellant check -- correct during a
        burn, where the engine was already firing so zero thrust can only
        mean dry, and required at launch, where the top stage reads zero
        thrust simply because it hasn't ignited.
        """
        stage_num = self.vessel.control.current_stage
        if stage_num in self.fired_stages:
            return False
        if self.vessel.available_thrust >= NO_THRUST:
            return False
        if verify_empty and not self.is_stage_empty(stage_num):
            return False
        return True

    def stage(self, job, settle=None, label="staged"):
        """Drop the current stage and activate the next one.

        Throttle is cut for the separation instant itself -- holding full
        thrust through the moment of decoupling maximizes plume impingement
        and collision torque against the departing stage -- then restored to
        whatever it was, after `settle` runs.

        Deliberately does NOT touch the autopilot's attitude target. An
        earlier version read live flight.pitch/heading here to "hold current
        attitude" and caused a hard ~180 degree flip: heading is only
        meaningful when not pointed near-vertical, and a rocket is still
        near pitch=90 for its first stage or two, so reading heading there
        can return a near-arbitrary value. Whatever target the caller
        already has in effect stays in effect.

        Returns the stage number that was dropped.
        """
        control = self.vessel.control
        stage_num = control.current_stage
        was_throttle = control.throttle
        control.throttle = 0.0

        for decoupler_part in self.decouplers_by_stage.get(stage_num, []):
            try:
                decoupler = decoupler_part.decoupler
                if decoupler and not decoupler.decoupled:
                    decoupler.decouple()
            except Exception:
                # kRPC can throw a null-reference error reading .decoupler
                # on some parts (observed on a real craft). The
                # activate_next_stage() below still fires the stage's own
                # staging action regardless.
                pass

        control.activate_next_stage()
        self.fired_stages.add(stage_num)
        job.message = f"{label} (stage {stage_num})"

        if settle is not None:
            settle(job)
        control.throttle = was_throttle
        return stage_num

    def stage_if_dry(self, job, verify_empty=True, settle=None, label="staged"):
        """Convenience: stage only if `should_stage` says to. Returns True
        if a stage was actually dropped, so a control loop can `continue`."""
        if not self.should_stage(verify_empty=verify_empty):
            return False
        self.stage(job, settle=settle, label=label)
        return True


def settle_briefly(seconds=0.3):
    """Settle strategy: just wait, letting the new stage's engine ignite
    and spool up. Enough mid-burn, where attitude is already established."""
    def _settle(job):
        job.sleep(seconds)
    return _settle


def settle_on_attitude(auto_pilot, max_error_deg=5.0, timeout_s=3.0):
    """Settle strategy: wait for the autopilot to confirm it's back on
    target before returning to thrust.

    A fixed pause can restore full throttle while still meaningfully
    off-target (e.g. from the centre-of-mass shift at separation), which
    just burns hard in the wrong direction and wastes delta-v. Capped so a
    genuinely stuck autopilot can't stall the ascent forever.
    """
    def _settle(job):
        elapsed = 0.0
        while auto_pilot.error > max_error_deg and elapsed < timeout_s:
            job.check_abort()
            job.sleep(0.1)
            elapsed += 0.1
    return _settle
