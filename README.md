# KSP Autonomous Fleet Control

A local web dashboard that flies Kerbal Space Program craft autonomously via
[kRPC](https://krpc.github.io/krpc/). Built so far: a vessel registry, live
telemetry, part-role tagging (read-only from the dashboard; assigned
in-game), an ascent-to-orbit autopilot, orbital maneuver helpers, a general
waypoint-landing autopilot, and a booster return-to-KSC autopilot. Not yet
built: auto-docking and satellite constellation management (see
`config/bodies.yaml` for the per-body parking orbit data already in place
for that).

## Setup

1. Install the [kRPC mod](https://forum.kerbalspaceprogram.com/topic/69313-*)
   into KSP's `GameData` folder. This project targets kRPC **0.6.0** --
   match the `krpc` PyPI package version in `requirements.txt` to whatever
   kRPC mod version you actually have installed (check in-game: Mods menu >
   kRPC), since the RPC API has changed between versions before.
2. Install Python 3.10+ and the dependencies:

   ```bash
   pip install -r requirements.txt
   ```

3. Launch KSP, load a save, and start the kRPC server: **Mods menu (top of
   screen in-game) > kRPC > Start server** (defaults: address `127.0.0.1`,
   RPC port `50000`, stream port `50001` — matches this project's defaults).
4. Run the dashboard server:

   ```bash
   python -m backend.main
   ```

   It will wait and retry until it can reach the kRPC server, so it's fine
   to start this before or after step 3.
5. Open `http://localhost:8000` in a browser.

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

| Tag              | Meaning                                    |
|------------------|---------------------------------------------|
| `engine.landing` | Engine used for the landing burn            |
| `engine.sustainer` | Main ascent engine                        |
| `decoupler.stage1` | Decoupler that drops stage 1 (stage2, ...)|
| `dock.front`     | Docking port used to dock nose-first        |
| `dock.cargo`     | Docking port used for cargo/fuel transfer   |
| `antenna.comm`   | Comms antenna                               |

## Using the dashboard

An **Overview** map at the top shows every vessel's live position on
whichever celestial body you select, color-coded by category. Below it,
vessels are grouped into sections by type (Boosters, Satellites, Stations,
Capsules, Landers, Probes, Unsorted) -- set a vessel's type from its card to
move it between sections.

Every vessel kRPC can see shows up as a card with its identity (kRPC 0.6.0
exposes no persistent vessel GUID, so this is the vessel's in-game name,
kept distinct if two vessels currently share a name), live telemetry, and
its tagged part roles. You can rename a vessel or set its type inline; both
persist in `data/autopilot.db` (sqlite) across restarts. Note: renaming a
craft inside KSP itself starts a new identity in the registry rather than
following the old one -- a real limitation of not having a stable ID to key
off of.

Autopilots, all with an **Abort** button that hands control back to you from
wherever the job was when you stopped it:

- **Launch to orbit** -- enter target apoapsis/periapsis (meters) and
  inclination (degrees). Switches to the vessel if needed, launches,
  gravity-turns, auto-stages (using tagged decouplers where present), and
  circularizes.
- **Land at waypoint** -- pick an in-game waypoint from the dropdown.
  Deorbits if still in orbit, then runs a suicide-burn descent. This is an
  approximate guided descent, not a precision-landing solver -- expect to
  land near the waypoint, not exactly on it.
- **Return to KSC** -- meant for a spent booster stage right after it
  separates during ascent. Boosts back toward the pad, then runs the same
  suicide-burn descent targeting it. Doesn't handle heatshield orientation
  during reentry -- point one manually first if your craft needs it.

## What's next (not yet built)

- Auto-docking (approach, align, dock) plus refuel/cargo transfer
- Satellite constellation manager: phasing, station-keeping, orientation,
  enforcing the per-body parking orbit in `config/bodies.yaml`
