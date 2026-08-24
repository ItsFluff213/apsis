"""Parses a trajectory plan copy-pasted from the KSP-MGA-Planner tool
(https://nmisyats.github.io/KSP-MGA-Planner/) into a sequence of burns this
program can execute -- no orbital-mechanics search/optimization happens
here, that's what the external planner is for. This just turns its text
output into absolute UTs and prograde/normal/radial delta-v vectors, the
same node representation `maneuver.execute_node` already knows how to burn.

Flyby steps in the plan carry no burn of their own (a gravity assist is a
free trajectory change from the preceding DSM having been aimed correctly)
-- they're kept only as informational waypoints so the executor can log
progress and, more importantly, warp through the encounter without ever
mistaking a flyby for a step that needs a burn.
"""

import re

# Stock KSP calendar -- 6-hour day, 426.09 day year (2556.5 hours). The
# planner's "T+ Yy - Dd - HH:MM:SS" output uses this same convention, so
# every step is converted with these same two constants and stays
# consistent relative to the others even if this differs slightly from
# some other calendar.
DAY_SECONDS = 6 * 3600
YEAR_SECONDS = int(2556.5 * 3600)

_TIME_RE = re.compile(r"T\+\s*(\d+)y\s*-\s*(\d+)d\s*-\s*(\d+):(\d+):(\d+)")
_STEP_HEADER_RE = re.compile(r"^(\S.*?):\s*$")
_FIELD_RE = re.compile(r"^\s*([A-Za-z][A-Za-z /]*):\s*(.+?)\s*$")


class PlanParseError(ValueError):
    pass


def _parse_time_offset(text: str) -> float:
    match = _TIME_RE.search(text)
    if not match:
        raise PlanParseError(f"couldn't parse a 'T+ Yy - Dd - HH:MM:SS' date from: {text!r}")
    years, days, hours, minutes, seconds = (int(g) for g in match.groups())
    return years * YEAR_SECONDS + days * DAY_SECONDS + hours * 3600 + minutes * 60 + seconds


def _split_steps(text: str):
    """Yields (name, [body_lines]) for each top-level (2-space-indented,
    not more) 'Name:' header in the Steps section."""
    lines = text.splitlines()
    current_name = None
    current_lines = []
    for line in lines:
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip(" "))
        header = _STEP_HEADER_RE.match(line.strip())
        if indent <= 2 and header and not line.strip().startswith(("Date", "SOI", "Prograde", "Normal", "Radial",
                                                                      "Ejection", "Periapsis", "Inclination")):
            if current_name is not None:
                yield current_name, current_lines
            current_name = header.group(1).strip()
            current_lines = []
        elif current_name is not None:
            current_lines.append(line)
    if current_name is not None:
        yield current_name, current_lines


def parse_plan(text: str):
    """Returns a list of steps in order:
        {"type": "burn", "name": ..., "ut_offset_s": ..., "prograde": ...,
         "normal": ..., "radial": ...}
        {"type": "flyby", "name": ..., "soi_enter_offset_s": ...,
         "soi_exit_offset_s": ..., "periapsis_km": ..., "inclination_deg": ...}
    Raises PlanParseError with a human-readable message if the pasted text
    doesn't look like a KSP-MGA-Planner output (missing a Steps section, or
    a step with no parseable date/ΔV at all).
    """
    if "Steps:" not in text:
        raise PlanParseError("no 'Steps:' section found -- paste the full plan output, not just a summary")
    steps_text = text.split("Steps:", 1)[1]

    steps = []
    for name, body_lines in _split_steps(steps_text):
        body = "\n".join(body_lines)
        fields = {}
        for line in body_lines:
            m = _FIELD_RE.match(line)
            if m:
                fields.setdefault(m.group(1).strip(), m.group(2).strip())

        if "SOI enter date" in fields or "SOI exit date" in fields:
            if "SOI enter date" not in fields or "SOI exit date" not in fields:
                raise PlanParseError(f"flyby step {name!r} is missing an SOI enter/exit date")
            steps.append({
                "type": "flyby",
                "name": name,
                "soi_enter_offset_s": _parse_time_offset(fields["SOI enter date"]),
                "soi_exit_offset_s": _parse_time_offset(fields["SOI exit date"]),
                "periapsis_km": _first_number(fields.get("Periapsis altitude", "0")),
                "inclination_deg": _first_number(fields.get("Inclination", "0")),
            })
            continue

        if "Date" not in fields:
            raise PlanParseError(f"step {name!r} has no 'Date:' line and isn't a flyby -- unrecognized format")
        if "Prograde" not in fields or "Normal" not in fields or "Radial" not in fields:
            raise PlanParseError(f"step {name!r} has a Date but no Prograde/Normal/Radial ΔV breakdown")
        steps.append({
            "type": "burn",
            "name": name,
            "ut_offset_s": _parse_time_offset(fields["Date"]),
            "prograde": _first_number(fields["Prograde"]),
            "normal": _first_number(fields["Normal"]),
            "radial": _first_number(fields["Radial"]),
        })

    if not steps:
        raise PlanParseError("no steps found in the pasted plan")
    return steps


def parse_sequence_name(text: str) -> str:
    """Best-effort extraction of the 'Sequence: Kerbin-Duna-...' line, for
    display only -- falls back to an empty string if the format doesn't
    match, since this is purely cosmetic and shouldn't block execution."""
    match = re.search(r"^Sequence:\s*(.+?)\s*$", text, re.MULTILINE)
    return match.group(1) if match else ""


def _first_number(text: str) -> float:
    match = re.search(r"-?\d+(\.\d+)?", text)
    if not match:
        raise PlanParseError(f"expected a number in {text!r}")
    return float(match.group(0))
