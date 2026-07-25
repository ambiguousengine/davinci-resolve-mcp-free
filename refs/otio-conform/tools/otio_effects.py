"""Print every NON-DEFAULT effect parameter, per clip, from a Resolve OTIO export.

Effects live at clip["effects"][n]["metadata"]["Resolve_OTIO"] — not on the clip's
own metadata block. Read-only.

  python otio_effects.py <file.otio> [--all]     --all = include defaults
"""
import json
import sys


def is_default(p):
    if p.get("Key Frames"):
        return False
    if "Default Parameter Value" not in p:
        return False
    return p["Parameter Value"] == p["Default Parameter Value"]


def dump(path, show_all=False):
    d = json.load(open(path, encoding="utf-8"))
    print("### %s" % path)
    tmeta = d.get("metadata", {}).get("Resolve_OTIO", {})
    if tmeta:
        print("  timeline meta: %s" % json.dumps(tmeta))
    for tr in d["tracks"]["children"]:
        kind = tr.get("kind")
        print("\n[TRACK %s %r] meta=%s" % (
            kind, tr.get("name"),
            json.dumps(tr.get("metadata", {}).get("Resolve_OTIO", {}))))
        for ch in tr.get("children", []):
            schema = ch.get("OTIO_SCHEMA", "")
            if schema.startswith("Transition"):
                print("  TRANSITION %s in=%s out=%s" % (
                    ch.get("transition_type"),
                    ch["in_offset"]["value"], ch["out_offset"]["value"]))
                continue
            if schema.startswith("Gap"):
                print("  GAP len=%s" % ch.get("source_range", {}).get(
                    "duration", {}).get("value"))
                continue
            print("  CLIP %r" % ch.get("name"))
            for eff in ch.get("effects", []):
                em = eff.get("metadata", {}).get("Resolve_OTIO", {})
                ename = em.get("Effect Name", eff.get("name"))
                params = em.get("Parameters", [])
                live = params if show_all else [p for p in params if not is_default(p)]
                if not live:
                    continue
                print("    EFFECT %r (Type %s)" % (ename, em.get("Effect Type")))
                for p in live:
                    kf = p.get("Key Frames") or {}
                    line = "      %-28s = %s" % (p.get("Parameter ID"),
                                                 p.get("Parameter Value"))
                    if "Default Parameter Value" in p and not kf:
                        line += "   (default %s)" % p["Default Parameter Value"]
                    print(line)
                    for fr in sorted(kf, key=lambda x: int(x)):
                        print("          kf frame %-6s -> %s" % (fr, kf[fr].get("Value")))


if __name__ == "__main__":
    dump(sys.argv[1], "--all" in sys.argv)
