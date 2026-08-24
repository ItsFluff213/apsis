"""Reads KSP's built-in part 'tag' field to give parts functional roles.

Tag a part in the VAB/SPH (right-click a part > Tag) using a
`category.detail` convention, e.g.:
    engine.landing      - the engine used for a landing burn
    engine.sustainer    - the main ascent engine
    decoupler.stage1    - the decoupler that drops stage 1
    dock.front          - the docking port used to dock nose-first
    dock.cargo          - a docking port used for cargo/fuel transfer
    antenna.comm        - a comms antenna

Untagged craft fall back to best-effort auto-detection (bottom-most engine,
outermost docking port, etc.) so the system is still usable without tagging,
but tagging is the reliable path and is what the autopilots check first.
"""


def _tag_category(tag: str):
    if not tag:
        return None, None
    tag = tag.strip()
    if not tag:
        return None, None
    if "." in tag:
        category, _, detail = tag.partition(".")
        return category.lower(), detail.lower()
    return tag.lower(), None


def get_tagged_parts(vessel):
    """Returns {category: {detail_or_'default': part}} for every tagged part."""
    result = {}
    for part in vessel.parts.all:
        try:
            tag = part.tag
        except Exception:
            # Same kRPC null-reference quirk as vessel.parts.controlling
            # (confirmed live: Part.get_Tag() throws for some parts, e.g.
            # debris from a destroyed craft) -- skip just this one part
            # rather than losing the whole vessel's tag summary, which was
            # otherwise skipping list_vessels() for every vessel each tick.
            continue
        category, detail = _tag_category(tag)
        if category is None:
            continue
        result.setdefault(category, {})[detail or "default"] = part
    return result


def get_vessel_role_tag(vessel):
    """The vessel's own role, read from its controlling part's tag (the
    probe core or command pod -- click it in-game and set its Tag, same
    `category.detail` convention as part roles, e.g. `booster.stage1`).
    Returns (category, detail), both None if the core is untagged or the
    vessel has no controlling part at all (e.g. debris with no probe core
    or command pod -- kRPC raises a server-side null-reference error for
    this rather than returning None, confirmed against a real debris
    vessel, so it must be caught explicitly here rather than just checked
    for None).
    """
    try:
        controlling = vessel.parts.controlling
        if controlling is None:
            return None, None
        tag = controlling.tag
    except Exception:
        # Confirmed live: a vessel that's unloaded/out of physics range
        # (0 parts reported, e.g. a distant landed craft) can still hand
        # back a non-None `controlling` part whose own .tag getter throws
        # the same server-side null-reference error -- not just
        # `vessel.parts.controlling` itself. Both need covering, or this
        # exception escapes _resolve_type and takes down list_vessels()
        # for every vessel, not just this one.
        return None, None
    return _tag_category(tag)


def find_by_tag(vessel, tag: str):
    """Exact match on a full tag string, e.g. 'engine.landing'."""
    for part in vessel.parts.all:
        if part.tag == tag:
            return part
    return None


def get_engines(vessel):
    tagged = get_tagged_parts(vessel).get("engine", {})
    if tagged:
        return list(tagged.values())
    return [p for p in vessel.parts.engines]


def get_landing_engine(vessel):
    tagged = get_tagged_parts(vessel).get("engine", {})
    if "landing" in tagged:
        return tagged["landing"]
    engines = vessel.parts.engines
    if not engines:
        return None
    # Fallback: engine physically lowest on the vessel (most negative z in
    # the vessel's own reference frame) is assumed to be the landing engine.
    return min(
        engines,
        key=lambda e: e.part.position(vessel.reference_frame)[1],
    )


def get_decouplers_by_stage(vessel):
    """{stage_number: [decoupler_parts]} using 'decoupler.stageN' tags where
    present, falling back to the part's actual KSP stage number."""
    tagged = get_tagged_parts(vessel).get("decoupler", {})
    by_stage = {}
    for detail, part in tagged.items():
        if detail and detail.startswith("stage"):
            try:
                stage_num = int(detail[len("stage"):])
                by_stage.setdefault(stage_num, []).append(part)
                continue
            except ValueError:
                pass
        by_stage.setdefault(part.stage, []).append(part)
    if by_stage:
        return by_stage
    for decoupler in vessel.parts.decouplers:
        by_stage.setdefault(decoupler.part.stage, []).append(decoupler.part)
    return by_stage


def get_docking_ports(vessel):
    tagged = get_tagged_parts(vessel).get("dock", {})
    if tagged:
        return tagged
    ports = vessel.parts.docking_ports
    return {f"port{i}": p.part for i, p in enumerate(ports)}


def get_front_docking_port(vessel):
    tagged = get_tagged_parts(vessel).get("dock", {})
    if "front" in tagged:
        return tagged["front"]
    ports = vessel.parts.docking_ports
    if not ports:
        return None
    # Fallback: docking port physically highest/forward-most on the vessel.
    return max(ports, key=lambda p: p.part.position(vessel.reference_frame)[1]).part


def get_role_summary(vessel):
    """Human-readable summary of tagged roles, for the dashboard."""
    tagged = get_tagged_parts(vessel)
    summary = {}
    for category, details in tagged.items():
        summary[category] = sorted(details.keys())
    return summary


def list_parts(vessel):
    """Read-only view of a vessel's parts and their current tags, for the
    dashboard's Parts panel.

    Role assignment itself happens in-game (right-click a part -> Tag,
    requires Advanced Tweakables) rather than from the dashboard: a craft
    often has several visually-identical parts (e.g. four landing legs),
    and a flat list on a web page can't show which physical part is which
    the way clicking it in the VAB/flight view can. Tags are native KSP
    part data, so they're saved inside the .sfs save file itself -- no
    extra bookkeeping needed on our side for them to survive a save reload.
    """
    result = []
    for index, part in enumerate(vessel.parts.all):
        result.append({
            "index": index,
            "name": part.name,
            "title": part.title,
            "stage": part.stage,
            "tag": part.tag,
            "is_engine": part.engine is not None,
            "is_decoupler": part.decoupler is not None,
            "is_docking_port": part.docking_port is not None,
        })
    return result
