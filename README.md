# Apsis

A local web dashboard that flies Kerbal Space Program craft autonomously via
[kRPC](https://krpc.github.io/krpc/). It handles ascent to orbit, transfers to
moons and to other planets, landing at a waypoint, returning a spent booster
to KSC, docking with refuelling and cargo transfer, and deploying satellites
into evenly spaced constellations -- with a live 3D map of the whole system.

Transfer planning is done in-house. Earlier versions required computing a
trajectory on an external website and pasting the result in; that is no
longer needed for anything.

**This project was built with AI assistance (Claude) throughout, and is
still an actively-evolving hobby project, not a polished/audited release.**
See [AI_USAGE.md](AI_USAGE.md) for details on how it was built and tested.

## Setup

**No Python needed** -- grab `Apsis.exe` and the `GameData-Apsis.zip` plugin
from the [latest release](https://github.com/ItsFluff213/apsis/releases):

1. Install [ModuleManager](https://forum.kerbalspaceprogram.com/topic/50533-*)
   and the [kRPC mod](https://forum.kerbalspaceprogram.com/topic/69313-*)
   into KSP's `GameData` folder. This project targets kRPC **0.6.0**.
2. Unzip `GameData-Apsis.zip` so `GameData/Apsis/` ends up inside your KSP
   `GameData` folder.
3. Launch KSP, load a save, and start the kRPC server: **Mods menu (top of
   screen in-game) > kRPC > Start server** (defaults: address `127.0.0.1`,
   RPC port `50000`, stream port `50001` — matches this project's defaults).
4. Run `Apsis.exe` -- it opens the dashboard in your browser automatically.
   It will wait and retry until it can reach the kRPC server, so it's fine
   to start this before or after step 3. Keep its console window open while
   you use the dashboard; closing it stops Apsis.

### Running from source instead

If you'd rather run it from source (e.g. to make changes yourself):

1. Do steps 1–3 above.
2. Install Python 3.10+ and the dependencies: `pip install -r requirements.txt`
3. Run `python -m backend.main`, then open `http://localhost:8000`.

Set `APSIS_PORT` to serve on a different port, e.g. if something else
already holds 8000:

```bash
APSIS_PORT=8010 python -m backend.main
```

The orbital-mechanics helpers have tests that need neither KSP nor kRPC:

```bash
python -m pytest tests/ -q
```

## Tagging parts for roles

Role assignment happens **in-game, not from the dashboard**: right-click a
part in the VAB/SPH (or in flight) and set its **Tag** field using a
`category.detail` convention. This is deliberate -- a craft often has
several visually-identical parts (e.g. four landing legs), and a flat list
on a web page can't show you which physical part is which the way clicking
it in the game can. The dashboard's Parts panel is read-only, for checking
what's already tagged.

If you don't see a **Tag** field on right-click, turn on **Advanced
Tweakables**: main menu (or pause menu) > Settings > Gameplay > Advanced
Tweakables.

Tags are native KSP part data, so they're saved inside the `.sfs` save file
itself -- reloading a save won't lose any role assignment.

The autopilots look for these tags first, and fall back to best-effort
auto-detection if a craft isn't tagged at all:

| Tag                | Meaning                                       |
|--------------------|-----------------------------------------------|
| `engine.landing`   | Engine used for the landing burn              |
| `engine.sustainer` | Main ascent engine                            |
| `decoupler.stage1` | Decoupler that drops stage 1 (stage2, ...)    |
| `dock.front`       | Docking port used to dock nose-first          |
| `dock.cargo`       | Docking port used for cargo/fuel transfer     |
| `antenna.comm`     | Comms antenna                                 |
| `heatshield.main`  | Heatshield to keep facing the airflow on reentry |
| `chute.main`       | Parachute (`chute.drogue` for a drogue chute) |

## Using the dashboard

An **Overview** map shows every vessel's live position, colour-coded by
category, with planets on their real (scaled) orbits. Below it, vessels are
grouped into sections by type -- set a vessel's type from its card to move it
between sections.

Every vessel kRPC can see shows up as a card with its identity (kRPC 0.6.0
exposes no persistent vessel GUID, so this is the vessel's in-game name,
kept distinct if two vessels currently share a name), live telemetry, and
its tagged part roles. You can rename a vessel or set its type inline; both
persist in `data/autopilot.db` (sqlite) across restarts. Note: renaming a
craft inside KSP itself starts a new identity in the registry rather than
following the old one -- a real limitation of not having a stable ID to key
off of.

The remaining tabs each carry one job. Every autopilot has an **Abort**
button that hands control back to you from wherever the job had got to.

### Orbit

- **Launch to orbit** -- enter target altitude and inclination. Switches to
  the vessel if needed, launches, flies a gravity turn, stages automatically
  as each stage empties, and circularizes.

  This is a real gravity turn, not a pitch schedule: after a small pitch kick
  at 60 m/s it holds surface prograde and lets gravity do the turning, so
  thrust stays aligned with velocity instead of fighting the airstream.
  Throttle is capped at terminal velocity, measured from the craft's actual
  drag rather than a rule of thumb. It reports roughly how much delta-v the
  climb used, so you can compare launches.

- **Transfer to** -- pick any moon or planet. Moons and planets are
  different problems under the hood (a moon transfer never leaves the parent
  body's sphere of influence; a planet transfer has to escape it in a
  specific direction, at a specific point in the parking orbit), but the
  dashboard routes that for you. **Preview** shows the wait until the window,
  the cruise time and the burn cost before you commit, and draws a moon
  transfer on the map.

  Scope: these are direct Hohmann-class transfers. Gravity assists are not
  planned -- for a multi-flyby grand tour you still want a dedicated planner
  such as [KSP-MGA-Planner](https://nmisyats.github.io/KSP-MGA-Planner/).

### Satellites

Group satellites into constellations and deploy them into evenly spaced
slots. A *communications* constellation targets the body's geostationary
altitude automatically; a *custom* one takes the altitude and inclination
you give it. Drag a satellite card onto a constellation to assign it, then
hit Deploy -- it transfers to the shell, matches the plane, phases into the
largest gap between existing members, and releases its transfer stage.

This view also runs on its own at **`/satellites/`**, with no dependency on
the rest of the dashboard.

### Landing

- **Land at waypoint** -- pick an in-game waypoint. Deorbits if still in
  orbit, then flies a suicide-burn descent.

  The deorbit burn is aimed: the burn point is chosen so the predicted impact
  falls near the waypoint, which costs no extra fuel since it's the same burn
  either way. The prediction is a vacuum Kepler fall including the body's
  rotation during the descent, so expect to land in the right region rather
  than on a dime -- it's a guided descent, not a precision-landing solver.

- **Return to KSC** -- meant for a spent booster right after it separates
  during ascent. Boosts back toward the pad, then runs the same descent
  targeting it.

Both hold retrograde through reentry so a tagged heatshield faces the
airflow, and deploy tagged parachutes once dynamic pressure is safe. A craft
with chutes but no propellant descends on canopy rather than being written
off.

### Docking

Rendezvous with another craft, dock with it, and move resources between
them. The sequence is plane match, Hohmann transfer, phasing, RCS approach
to a hold point on the port axis, alignment, then a slow axial run-in.

Requirements and limits:

- Both craft must already be in orbit around the same body. This does not
  launch to a rendezvous or transfer between bodies -- use the other tabs
  first.
- The active craft needs RCS with translation authority and monopropellant.
- The plane match corrects inclination magnitude, not a mismatched longitude
  of ascending node. Badly mismatched planes show up as a rendezvous that
  never closes; launch into the target's plane instead.
- Resource transfer only works once docked, since KSP treats the joined
  craft as a single vessel.

## Multiplayer / shared saves

Each player runs their own copy against their own kRPC connection. The
Satellites view can pull another player's constellation list over the
network and merge it into the active profile, so a shared save can keep one
consistent picture of who owns which orbital slots.

## What's next (not yet built)

- Station-keeping: holding a constellation slot against drift over time
- Gravity-assist trajectories (the current transfers are direct only)
- Surface rover routing
