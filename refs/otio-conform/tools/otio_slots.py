import json, sys
d = json.load(open(sys.argv[1], encoding="utf-8"))
for tr in d["tracks"]["children"]:
    print("[TRACK %s %r]" % (tr.get("kind"), tr.get("name")))
    for ch in tr.get("children", []):
        if ch.get("OTIO_SCHEMA","").startswith(("Gap","Transition")): continue
        print("  CLIP %r  effects=%d" % (ch.get("name"), len(ch.get("effects",[]))))
        for i,eff in enumerate(ch.get("effects", [])):
            em = eff.get("metadata", {}).get("Resolve_OTIO", {})
            print("    [%d] Type %-5s %-34s Enabled=%-5s params=%d"
                  % (i, em.get("Type"), em.get("Effect Name"), em.get("Enabled"),
                     len(em.get("Parameters",[]))))
