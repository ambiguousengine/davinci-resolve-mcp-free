"""Pre-flight scan of a Resolve OTIO export -- what is in this timeline, and can we touch it?

    python preflight.py <timeline.otio>
    python preflight.py --export <TimelineName>      # export the CURRENT timeline first

Exit codes:  0 = GREEN (everything recognised)   1 = AMBER (unknowns present)
             2 = RED (known blocker)             3 = could not read the file

THE DESIGN RULE THAT MATTERS
----------------------------
Anything this script does not RECOGNISE is reported, not ignored. The registries below
list only what has actually been authored and measured. A schema, effect type or media
reference that is not in them comes out as UNKNOWN -- which is the honest answer, because
"we have never tested that" and "that works" are not the same statement.

Never add an entry to PROVEN on the strength of a read-back or a return value. It goes in
when a render or an exported frame has been measured against a control.
"""
import json
import sys
import os

try:                                    # Windows consoles default to cp1252
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
except Exception:                       # noqa: BLE001 - older/redirected streams
    pass
from collections import Counter, defaultdict

# ── registries ────────────────────────────────────────────────────────────────
# Authored AND verified by measurement (rendered audio / exported frames vs a control).
PROVEN_EFFECTS = {
    36: "Video Faders (videoFaderIn/Out)",
    62: "Fairlight Clip Volume and Fades (volume, faderOut)",
    63: "Fairlight Equaliser Band (frequency, dB gain, qFactor)",
    64: "Fairlight Clip Equaliser (master enable)",
    67: "Fairlight Clip Pitch (semiTones)",
    72: "Fairlight Clip Pan (pan)",
}

# Present on every clean clip with empty Parameters. Harmless; never authored.
INERT_EFFECTS = {
    1: "Composite", 2: "Transform", 3: "Cropping", 22: "Retime and Scaling",
    43: "Lens Correction", 59: "Dynamic Zoom", 85: "Immersive Transform",
    86: "Immersive Output Transform", 87: "Source Cropping",
    88: "Source Transform", 90: "Source Looks",
}

PROVEN_SCHEMAS = {
    "Clip", "Gap", "Track", "Stack", "Timeline", "ExternalReference",
    "Transition",        # dissolves -- authored, re-export confirmed
    "LinearTimeWarp",    # retime -- frames byte-identical at the predicted source time
    "TimeEffect",        # speed ramps incl. eased -- byte-identical on all segments
    "Effect", "TimeRange", "RationalTime", "SerializableCollection",
}

# Known to break, with the known remedy.
BLOCKERS = {
    "MissingReference": (
        "Fusion title or generator -- BLOCKS the OTIO import outright.",
        "Remedy: placeholder substitution + comp restore, then PIXEL-VERIFY the restore. "
        "Proven on ONE static title only; multiple/animated titles are untested."),
}


def walk(node, kind, out, depth=0, path="timeline"):
    """Recursively collect everything interesting. Nested Stacks are the compound-clip tell."""
    schema = str(node.get("OTIO_SCHEMA", "")).split(".")[0]
    out["schemas"][schema] += 1

    if schema == "Stack" and depth > 0:
        out["nested_stacks"].append(path)

    for eff in node.get("effects", []) or []:
        em = (eff.get("metadata") or {}).get("Resolve_OTIO", {})
        # The key is "Type". NOT "Effect Type" -- that spelling silently yields None and
        # makes every lookup miss, so everything reads as UNKNOWN. Cost a false AMBER on
        # the first run of this script, and otio_effects.py shipped with the same bug.
        etype = em.get("Type")
        enabled = em.get("Enabled", True)
        params = em.get("Parameters", []) or []
        live = [p for p in params
                if p.get("Key Frames")
                or ("Default Parameter Value" in p
                    and p.get("Parameter Value") != p["Default Parameter Value"])]
        if live:
            bucket = "effects_in_use" if enabled else "effects_disabled"
            out[bucket][etype].append((path, em.get("Effect Name"),
                                       [p.get("Parameter ID") for p in live]))
        if etype is not None:
            out["effect_types_seen"].add(etype)
        eschema = str(eff.get("OTIO_SCHEMA", "")).split(".")[0]
        if eschema not in PROVEN_SCHEMAS:
            out["unknown_schemas"][eschema].append(path)

    mr = node.get("media_reference") or node.get("media_references")
    if isinstance(mr, dict):
        refs = mr.values() if "OTIO_SCHEMA" not in mr else [mr]
        for r in refs:
            if not isinstance(r, dict):
                continue
            rs = str(r.get("OTIO_SCHEMA", "")).split(".")[0]
            out["ref_types"][rs] += 1
            if rs in BLOCKERS:
                out["blockers"][rs].append(path)
            elif rs not in PROVEN_SCHEMAS:
                out["unknown_schemas"][rs].append(path)
            tgt = r.get("target_url")
            if tgt:
                out["media"][tgt] += 1
            ar = r.get("available_range") or {}
            rate = (ar.get("start_time") or {}).get("rate")
            if rate:
                out["source_rates"][rate] += 1

    if schema not in PROVEN_SCHEMAS and schema not in BLOCKERS:
        out["unknown_schemas"][schema].append(path)

    kids = node.get("children") or (node.get("tracks", {}) or {}).get("children") or []
    for i, ch in enumerate(kids):
        walk(ch, kind, out, depth + 1, "%s/%s[%d]" % (path, schema, i))


def scan(path):
    try:
        with open(path, encoding="utf-8") as fh:
            doc = json.load(fh)
    except Exception as exc:                                   # noqa: BLE001
        print("RED  could not read %s\n     %s" % (path, exc))
        return 3

    out = {
        "schemas": Counter(), "ref_types": Counter(), "media": Counter(),
        "source_rates": Counter(), "effect_types_seen": set(),
        "effects_in_use": defaultdict(list), "effects_disabled": defaultdict(list),
        "unknown_schemas": defaultdict(list),
        "blockers": defaultdict(list), "nested_stacks": [],
    }
    walk(doc, "timeline", out)

    tl_rate = ((doc.get("global_start_time") or {}).get("rate")
               or (doc.get("tracks", {}).get("source_range") or {}).get("duration", {}).get("rate"))

    print("=" * 74)
    print("PRE-FLIGHT  %s" % os.path.basename(path))
    print("=" * 74)

    tracks = [t for t in doc.get("tracks", {}).get("children", [])]
    v = sum(1 for t in tracks if t.get("kind") == "Video")
    a = sum(1 for t in tracks if t.get("kind") == "Audio")
    print("\nSTRUCTURE   %dV / %dA | %d clip(s) | timeline rate %s"
          % (v, a, out["schemas"].get("Clip", 0), tl_rate or "?"))

    # ── media ────────────────────────────────────────────────────────────────
    print("\nMEDIA       %d distinct source file(s)" % len(out["media"]))
    for m, n in out["media"].most_common(12):
        print("              %3dx %s" % (n, m))
    if len(out["media"]) > 12:
        print("              ... and %d more" % (len(out["media"]) - 12))
    if len(out["media"]) > 1:
        print("            [WARN] UNTESTED -- every verified test used a SINGLE source file.")

    if len(out["source_rates"]) > 1:
        print("            [STOP] MIXED SOURCE RATES: %s"
              % ", ".join(str(r) for r in sorted(out["source_rates"])))
        print("               Never tested. Retime and ramp maths assume one timebase.")

    # ── effects actually in use ──────────────────────────────────────────────
    print("\nEFFECTS IN USE (non-default parameters only)")
    if not out["effects_in_use"]:
        print("              none -- a clean cut")
    for etype, uses in sorted(out["effects_in_use"].items(),
                              key=lambda kv: (kv[0] is None, kv[0])):
        if etype in PROVEN_EFFECTS:
            tag, note = "[OK]   PROVEN ", PROVEN_EFFECTS[etype]
        elif etype in INERT_EFFECTS:
            tag, note = "[WARN] UNTESTED", INERT_EFFECTS[etype] + " -- present but never authored"
        else:
            tag, note = "[WARN] UNKNOWN ", "effect type %s has never been seen or tested" % etype
        print("            %s %s" % (tag, note))
        for p, name, ids in uses[:3]:
            print("                        %s -> %s" % (name, ", ".join(str(i) for i in ids)))
        if len(uses) > 3:
            print("                        ... and %d more clip(s)" % (len(uses) - 3))

    if out["effects_disabled"]:
        print("\nDISABLED SLOTS CARRYING VALUES (present, switched off -- not rendering)")
        for etype, uses in out["effects_disabled"].items():
            name = (PROVEN_EFFECTS.get(etype) or INERT_EFFECTS.get(etype)
                    or "effect type %s" % etype)
            print("            %-46s x%d" % (name, len(uses)))
        print("            Authorable: an authored 'Enabled': true IS honoured on import.")
        print("            Just remember the real trap is Default Parameter Value, not this.")

    # ── structural risks ─────────────────────────────────────────────────────
    print("\nSTRUCTURE RISKS")
    clean = True
    if out["nested_stacks"]:
        clean = False
        print("            [STOP] %d NESTED STACK(S) -- almost certainly compound clips."
              % len(out["nested_stacks"]))
        print("               THIS IS THE ONE THAT CAN INVALIDATE THE WHOLE METHOD.")
        print("               A compound clip is not an effect, it is a container: if it")
        print("               does not survive export->mutate->import, the round trip itself")
        print("               is unsafe on this timeline, not merely missing a feature.")
        for p in out["nested_stacks"][:5]:
            print("                 %s" % p)
    for b, paths in out["blockers"].items():
        clean = False
        why, remedy = BLOCKERS[b]
        print("            [STOP] %s x%d -- %s\n               %s" % (b, len(paths), why, remedy))
    if out["schemas"].get("Transition"):
        print("            [OK]   %d transition(s) -- authorable, verified"
              % out["schemas"]["Transition"])
    for s in ("LinearTimeWarp", "TimeEffect"):
        if out["schemas"].get(s):
            print("            [OK]   %d %s -- retime/ramp, authorable, verified"
                  % (out["schemas"][s], s))
    if clean and not out["unknown_schemas"]:
        print("            none")

    if out["unknown_schemas"]:
        print("\nUNRECOGNISED -- never tested, so NOT safe to assume")
        for s, paths in out["unknown_schemas"].items():
            print("            [WARN] %-28s x%d   e.g. %s" % (s, len(paths), paths[0]))

    # ── verdict ──────────────────────────────────────────────────────────────
    print("\n" + "-" * 74)
    if out["blockers"] or out["nested_stacks"] or len(out["source_rates"]) > 1:
        print("VERDICT  [STOP] RED -- do NOT round-trip this timeline yet.")
        print("         Resolve the items above first. A silent failure here damages a lock.")
        rc = 2
    elif out["unknown_schemas"] or len(out["media"]) > 1 or any(
            e not in PROVEN_EFFECTS for e in out["effects_in_use"]):
        print("VERDICT  [WARN] AMBER -- round trip is probably fine, but this timeline contains")
        print("         things no test has covered. Verify against a reference render, and")
        print("         treat anything listed above as unproven until measured.")
        rc = 1
    else:
        print("VERDICT  [OK]   GREEN -- everything here has been authored and measured before.")
        rc = 0
    print("-" * 74)
    print("Reminder: GREEN means 'matches what we tested', not 'guaranteed'. The")
    print("whole-timeline reference check still applies before anything ships.")
    return rc


if __name__ == "__main__":
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        sys.exit(3)
    if args[0] == "--export":
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from bridge import call                                  # noqa: E402
        dest = os.path.abspath("preflight_export.otio")
        print(call("/timeline/export", {"fileName": dest, "exportType": "OTIO"}))
        sys.exit(scan(dest))
    sys.exit(scan(args[0]))
