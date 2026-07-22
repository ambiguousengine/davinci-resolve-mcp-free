#!/usr/bin/env python
"""
pool_cleanup.py --- find media pool entries nothing uses, safely.

    python pool_cleanup.py                 # report only (read-only, always safe)
    python pool_cleanup.py --move          # also sweep candidates into "_UNUSED (review)"
    python pool_cleanup.py --json out.json # write the candidate list

Requires DaVinci Resolve running with the CursorBridge script started
(Workspace > Scripts > CursorBridge), and a project open.

--------------------------------------------------------------------------
WHY THIS EXISTS, AND THE TRAP IT AVOIDS  (read before changing anything)
--------------------------------------------------------------------------
On 2026-07-22 a cleanup was attempted using Resolve's per-clip "Usage"
property alone. It orphaned 27 clips across 4 timelines.

The reason: **Usage only counts timelines Resolve has actually loaded.** On a
freshly opened project nearly everything reads 0, which is indistinguishable
from "nothing uses this". One file read Usage=1 early on and Usage=7 after all
24 timelines had been opened.

So this script never trusts one signal:

  SIGNAL 1 (observed)  walk every timeline and record the File Path of every
                       clip on every track. Ground truth for what is on an edit.
  SIGNAL 2 (Resolve's) the Usage count --- read only AFTER every timeline has
                       been opened by the walk, which is what warms it up.

An entry is a candidate ONLY if both agree it is unused. Where they disagree
it is left alone --- and they disagree a lot, because SIGNAL 1 cannot see inside
Fusion compositions or compound clips. In the 2026-07-22 run, 29 files were on
no timeline yet Resolve knew they were used; those were the Parallax Skipper FX
sources. Trusting SIGNAL 1 alone would have swept up live Fusion work.

Two categories are NEVER proposed, regardless of what the signals say:

  * REAL TIMELINES. Your deliverable cuts live in the media pool too, and
    nothing "uses" them --- QMC_Storm_Cut_5.3 looks exactly like an unused item.
  * COMPOUND CLIPS. In the same run, 76 of 77 were genuinely nested inside
    cut-map timelines. They LOOK like junk (01_xray-hull appearing three times)
    and are the guts of the edit.

Deletion is deliberately not implemented. The script moves candidates to a
review bin; a human deletes in Resolve's UI after looking. That split exists
because the irreversible half of this job earned its way into human hands.
"""
import argparse
import json
import sys
import urllib.error
import urllib.request

BRIDGE = "http://127.0.0.1:9876"
REVIEW_BIN_NAME = "_UNUSED (review)"
REVIEW_BIN_PARENT = "Master/MEDIA"
MAX_TRACKS = 8


def get(path, timeout=90):
    with urllib.request.urlopen(BRIDGE + path, timeout=timeout) as r:
        return json.load(r)


def post(path, body, timeout=180):
    req = urllib.request.Request(
        BRIDGE + path, data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def die(msg):
    print("ERROR: %s" % msg)
    sys.exit(1)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--move", action="store_true",
                    help="sweep candidates into the review bin (reversible)")
    ap.add_argument("--json", metavar="FILE", help="write the candidate list")
    args = ap.parse_args()

    try:
        status = get("/status", timeout=10)
    except Exception as e:
        die("bridge not responding at %s (%s).\n"
            "Start it in Resolve: Workspace > Scripts > CursorBridge" % (BRIDGE, e))
    if not status.get("connected"):
        die("bridge is up but Resolve reports not connected")

    proj = get("/project")
    n_tl = proj.get("timelineCount") or proj.get("settings", {}).get("timelineCount")
    if not n_tl:
        die("could not read the project's timeline count")
    print("project: %s   timelines: %s" % (proj.get("name"), n_tl))

    # ---- SIGNAL 1: walk every timeline. Also warms Usage for SIGNAL 2. -------
    original = None
    try:
        original = get("/timeline").get("name")
    except Exception:
        pass

    used_paths, used_compound_names, tl_names = set(), set(), set()
    print("walking %d timelines" % n_tl, end="", flush=True)
    for i in range(1, int(n_tl) + 1):
        try:
            nm = post("/timeline/switch", {"index": i}).get("timeline")
        except Exception:
            continue
        if nm:
            tl_names.add(nm)
        for tt in ("video", "audio"):
            for t in range(1, MAX_TRACKS + 1):
                try:
                    d = get("/timeline/clips?track_type=%s&track_index=%d" % (tt, t), timeout=30)
                except Exception:
                    continue
                if "error" in d:
                    continue
                for c in d.get("clips", []):
                    fp = c.get("File Path")
                    if fp:
                        used_paths.add(fp.lower())
                    else:
                        nme = c.get("name")
                        if nme and nme != "Text+":
                            used_compound_names.add(nme)
        print(".", end="", flush=True)
    print(" done")

    if original:
        for i in range(1, int(n_tl) + 1):
            try:
                if post("/timeline/switch", {"index": i}).get("timeline") == original:
                    break
            except Exception:
                pass

    # ---- SIGNAL 2: Usage, now warm --------------------------------------------
    audit = get("/mediapool/audit", timeout=120)
    items = audit["items"]
    media = [i for i in items if not i["isTimelineOrGenerator"]]
    non_media = [i for i in items if i["isTimelineOrGenerator"]]
    real_timelines = [i for i in non_media if i["name"] in tl_names]
    compounds = [i for i in non_media if i["name"] not in tl_names]

    candidates, disagree = [], []
    for i in media:
        on_tl = i["filePath"].lower() in used_paths
        usage = i["usage"] or 0
        if not on_tl and usage == 0:
            candidates.append(i)
        elif not on_tl or usage == 0:
            disagree.append((i, on_tl, usage))

    orphan_compounds = [i for i in compounds
                        if i["name"] not in used_compound_names and (i["usage"] or 0) == 0]

    print()
    print("pool entries              : %d" % len(items))
    print("  media files             : %d" % len(media))
    print("  compound clips          : %d  (never proposed --- usually nested in cuts)" % len(compounds))
    print("  real timelines          : %d  (never proposed --- these are the deliverables)" % len(real_timelines))
    print()
    print("distinct files on an edit : %d" % len(used_paths))
    print("BOTH signals say unused   : %d   <- candidates" % len(candidates))
    print("signals DISAGREE          : %d   <- left alone (often Fusion/compound sources)" % len(disagree))
    print("compounds with no nesting : %d   <- reported only, never swept" % len(orphan_compounds))

    if candidates:
        print()
        print("candidates:")
        for i in sorted(candidates, key=lambda x: (x["folder"], x["name"])):
            print("   %-44s %s" % (i["name"][:43], i["folder"]))

    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(candidates, fh, indent=1)
        print("\nwrote %s" % args.json)

    if args.move and candidates:
        try:
            post("/mediapool/folder/create",
                 {"name": REVIEW_BIN_NAME, "parentPath": REVIEW_BIN_PARENT})
        except Exception:
            pass  # already exists
        target = "%s/%s" % (REVIEW_BIN_PARENT, REVIEW_BIN_NAME)
        res = post("/mediapool/clips/move_by_id",
                   {"mediaIds": [i["mediaId"] for i in candidates], "targetFolder": target})
        print("\nmoved %s/%s into '%s'  (success=%s)"
              % (res.get("moved"), len(candidates), target, res.get("success")))
        print("Nothing was deleted. Review the bin in Resolve, then delete there if happy.")
    elif args.move:
        print("\nnothing to move")


if __name__ == "__main__":
    main()
