# AI usage

This project was built in collaboration with Claude (Anthropic), used as a
coding assistant throughout: the Python backend (kRPC integration, the
autopilots, the maneuver-planning math), the web dashboard frontend, and
the compiled C# KSP plugin (`ModuleApsisRole` / `ModuleApsisPartRole`) were
all written with Claude's help, then iteratively debugged against a live
KSP game.

Design decisions, live testing, and verification (launches, landings,
constellation deployments, etc. actually flown in-game to confirm behavior)
were done by the project's author, with Claude implementing and iterating
based on what those live tests showed. Bugs found by flying real missions
are called out directly in code comments where relevant, rather than
hidden -- this is a hobby project built and tested incrementally, not a
polished, professionally-audited release, and mod stability should be
judged accordingly.

If you run into a bug, a design decision that seems off, or behavior that
doesn't match what's documented, please open an issue -- this project is
still actively evolving.
