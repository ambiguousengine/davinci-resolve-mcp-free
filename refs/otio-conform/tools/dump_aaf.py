"""Dump an AAF's structure — mobs, slots, segment types, and anything that
looks like level/pan automation. Read-only. AUD-1/AUD-2."""
import sys
import aaf2
from aaf2.components import (Sequence, SourceClip, OperationGroup, Transition,
                             Filler, Timecode, EdgeCode)

INDENT = "  "


def describe_param(p, depth):
    pad = INDENT * depth
    tname = type(p).__name__
    name = getattr(p, "name", None) or ""
    print("%s- param %s name=%r" % (pad, tname, name))
    # constant?
    try:
        v = p.value
        print("%s    value=%r (%s)" % (pad, v, type(v).__name__))
    except Exception as e:
        print("%s    value unavailable: %s" % (pad, e))
    # varying?
    pl = getattr(p, "pointlist", None)
    if pl is None:
        try:
            pl = p["PointList"].value
        except Exception:
            pl = None
    if pl is not None:
        pts = list(pl)
        print("%s    !! VARYING: %d control points" % (pad, len(pts)))
        for pt in pts[:12]:
            try:
                t = pt.time
            except Exception:
                t = "?"
            try:
                val = pt.value
            except Exception:
                val = "?"
            print("%s       t=%s value=%s" % (pad, t, val))
        if len(pts) > 12:
            print("%s       ... %d more" % (pad, len(pts) - 12))


def walk(seg, depth=0, limit=[0]):
    pad = INDENT * depth
    tname = type(seg).__name__
    length = getattr(seg, "length", None)
    if isinstance(seg, Sequence):
        print("%s%s len=%s (%d components)" % (pad, tname, length, len(list(seg.components))))
        for c in seg.components:
            limit[0] += 1
            if limit[0] > 400:
                print("%s  ...truncated" % pad)
                return
            walk(c, depth + 1, limit)
    elif isinstance(seg, OperationGroup):
        opname = getattr(seg.operation, "name", "?")
        print("%s%s op=%r len=%s" % (pad, tname, opname, length))
        for p in seg.parameters:
            describe_param(p, depth + 1)
        for c in seg.segments:
            walk(c, depth + 1, limit)
    elif isinstance(seg, SourceClip):
        mob = None
        try:
            mob = seg.mob
        except Exception:
            pass
        print("%s%s len=%s start=%s -> %s" % (
            pad, tname, length, getattr(seg, "start", "?"),
            getattr(mob, "name", None) if mob else "<unresolved>"))
    elif isinstance(seg, Transition):
        print("%s%s len=%s" % (pad, tname, length))
        try:
            og = seg["OperationGroup"].value
            walk(og, depth + 1, limit)
        except Exception:
            pass
    else:
        print("%s%s len=%s" % (pad, tname, length))


def main(path):
    with aaf2.open(path, "r") as f:
        print("=== MOBS ===")
        for mob in f.content.mobs:
            print("\n%s  name=%r  id=%s" % (type(mob).__name__, mob.name, mob.mob_id))
            for slot in mob.slots:
                st = getattr(slot, "segment", None)
                print("  SLOT id=%s name=%r media_kind=%s edit_rate=%s" % (
                    getattr(slot, "slot_id", "?"), getattr(slot, "name", None),
                    getattr(st, "media_kind", "?"), getattr(slot, "edit_rate", "?")))
                # slot-level user comments / attributes
                try:
                    for k, v in slot.items():
                        if k not in ("Segment",):
                            print("     attr %s = %r" % (k, v))
                except Exception:
                    pass
                if st is not None:
                    walk(st, 2)


if __name__ == "__main__":
    main(sys.argv[1])
