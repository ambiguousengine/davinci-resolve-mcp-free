import json, sys
d = json.load(open(sys.argv[1], encoding="utf-8"))
want = sys.argv[2] if len(sys.argv) > 2 else "Pitch"
for tr in d["tracks"]["children"]:
    if tr.get("kind") != "Audio": continue
    for ch in tr.get("children", []):
        for eff in ch.get("effects", []):
            em = eff.get("metadata", {}).get("Resolve_OTIO", {})
            if want.lower() in str(em.get("Effect Name","")).lower():
                print(json.dumps(eff, indent=2)[:2600]); print("---")
