"""Add code-native timeline editing verbs to CursorBridge.py.

Measured 2026-07-22 on Resolve Studio 21.0.2.4, all on a scratch timeline:

  AppendToTimeline          COMMITTED in 23 ms   <-- the notes said this
                                                     "silently commits NOTHING".
                                                     It does not. That single
                                                     wrong belief is why every
                                                     placement got routed
                                                     through FCPXML import.
  precise dict placement    frame-exact (V4 @ 300)          40 ms
  delete + re-append        position & duration held        78 ms

So the dict form of AppendToTimeline --- {mediaPoolItem, startFrame, endFrame,
trackIndex, recordFrame} --- is the primitive everything else is built from:

  place  = append at (track, recordFrame)
  swap   = delete + place at the same recordFrame with a different source
  move   = delete + place at a different recordFrame with the same source
  remove = DeleteClips

There is no ReplaceClip on a TimelineItem (it is a MediaPoolItem method and
changes the media everywhere it is used), so swap must be composite. Likewise
there is no move API; a move is a delete plus a re-place. Both verified above.

endFrame is EXCLUSIVE: endFrame=60 yields exactly 60 frames. endFrame=59
yields 59. Getting this backwards is a silent one-frame drift on every edit.

Not fixed here, because Resolve exposes no surface for them:
  * audio volume / pan / speed --- an audio TimelineItem has NO properties and
    NO methods at all on this build. Confirmed absent, not merely undocumented.
  * real transitions --- AddTransition / InsertTransition absent on Timeline.
  * InsertTitleIntoTimeline silently no-ops (InsertFusionGenerator works).
"""
import io, sys

P = r"F:\AMBIGUITY\TOOLS\davinci-bridge\src\CursorBridge.py"
src = io.open(P, "r", encoding="utf-8").read()


def once(needle, label):
    n = src.count(needle)
    if n != 1:
        sys.exit("ANCHOR FAIL [%s]: %d occurrences, expected 1" % (label, n))
    print("anchor ok [%s]" % label)


A1 = "def action_append_to_timeline(body):"
B1 = '''def _pool_item_by_ref(pool, body, key_id="mediaId", key_name="clip"):
    """Resolve a media pool item from either a mediaId or a name.

    mediaId is preferred and unambiguous. A name is a coin toss the moment the
    same filename exists in two bins, which in this project it routinely does,
    so a name that matches more than once is refused rather than guessed.
    """
    mid = body.get(key_id)
    if mid:
        found = _pool_items_by_ids(pool, [mid])
        if not found:
            return None, {"error": "No media pool item with mediaId %s" % mid}
        return list(found.values())[0], None
    name = body.get(key_name)
    if not name:
        return None, {"error": "Provide either %s or %s" % (key_id, key_name)}
    hits = _find_pool_items_all(pool, name)
    if not hits:
        return None, {"error": "No media pool item named '%s'" % name}
    if len(hits) > 1:
        return None, {
            "error": "'%s' matches %d pool items --- refusing to guess. "
                     "Pass mediaId instead." % (name, len(hits)),
            "candidates": [safe(lambda c=c: c.GetMediaId()) for c in hits],
        }
    return hits[0], None


def _item_at_frame(tl, track_type, track_index, frame):
    """The TimelineItem whose span contains `frame`, or None."""
    for it in (safe(lambda: tl.GetItemListInTrack(track_type, track_index)) or []):
        s = int(safe(lambda it=it: it.GetStart()) or 0)
        e = int(safe(lambda it=it: it.GetEnd()) or 0)
        if s <= frame < e:
            return it
    return None


def _place(pool, item, track_type, track_index, record_frame, src_in, frames):
    """The one primitive. Returns (landed_item, error).

    Verifies by re-reading the track: the API returns objects even in cases
    where nothing committed, so the return value is not evidence.
    """
    spec = {
        "mediaPoolItem": item,
        "startFrame": int(src_in),
        "endFrame": int(src_in) + int(frames),   # EXCLUSIVE --- see module docstring
        "trackIndex": int(track_index),
        "recordFrame": int(record_frame),
    }
    if track_type == "audio":
        spec["mediaType"] = 2
    elif track_type == "video":
        spec["mediaType"] = 1
    safe(lambda: pool.AppendToTimeline([spec]))
    _, _, tl, err = _timeline()
    if err:
        return None, err
    landed = _item_at_frame(tl, track_type, track_index, int(record_frame))
    if not landed:
        return None, {"error": "Nothing committed at %s track %d frame %d"
                               % (track_type, track_index, record_frame)}
    return landed, None


def _describe(it):
    return {
        "name": safe(lambda: it.GetName()),
        "start": int(safe(lambda: it.GetStart()) or 0),
        "duration": int(safe(lambda: it.GetDuration()) or 0),
    }


def action_timeline_place(body):
    """Place a pool clip at an exact track + frame.

    body: mediaId|clip, trackIndex, recordFrame, frames, [srcIn=0],
          [trackType=video]
    """
    _, proj, tl, err = _timeline()
    if err:
        return err
    pool = proj.GetMediaPool()
    item, err = _pool_item_by_ref(pool, body)
    if err:
        return err
    frames = int(body.get("frames", 0))
    if frames <= 0:
        return {"error": "frames must be a positive count"}
    tt = body.get("trackType", "video")
    ti = int(body.get("trackIndex", 1))
    rf = int(body.get("recordFrame", 0))
    landed, err = _place(pool, item, tt, ti, rf, int(body.get("srcIn", 0)), frames)
    if err:
        return err
    d = _describe(landed)
    return {
        "success": True, "placed": d, "timeline": safe(lambda: tl.GetName()),
        "frameExact": d["start"] == rf and d["duration"] == frames,
    }


def action_timeline_swap(body):
    """Swap the media under an existing edit, holding its position and length.

    There is no ReplaceClip on a TimelineItem, so this is delete + re-place at
    the identical recordFrame. body: atFrame, trackIndex, mediaId|clip,
    [trackType=video], [srcIn=0]
    """
    _, proj, tl, err = _timeline()
    if err:
        return err
    pool = proj.GetMediaPool()
    tt = body.get("trackType", "video")
    ti = int(body.get("trackIndex", 1))
    at = int(body.get("atFrame", -1))
    if at < 0:
        return {"error": "atFrame is required"}
    target = _item_at_frame(tl, tt, ti, at)
    if not target:
        return {"error": "No clip at %s track %d frame %d" % (tt, ti, at)}
    newsrc, err = _pool_item_by_ref(pool, body)
    if err:
        return err
    before = _describe(target)
    if not safe(lambda: tl.DeleteClips([target])):
        return {"error": "Could not delete the existing clip; nothing changed"}
    landed, err = _place(pool, newsrc, tt, ti, before["start"],
                         int(body.get("srcIn", 0)), before["duration"])
    if err:
        return {"error": "Deleted the old clip but the replacement did not "
                         "commit --- there is now a GAP at frame %d. %s"
                         % (before["start"], err.get("error")),
                "gapAt": before["start"], "lost": before}
    after = _describe(landed)
    return {
        "success": True, "before": before, "after": after,
        "positionHeld": after["start"] == before["start"],
        "durationHeld": after["duration"] == before["duration"],
    }


def action_timeline_move(body):
    """Move a clip along its track. Delete + re-place; there is no move API.

    body: fromFrame, toFrame, trackIndex, [trackType=video]
    """
    _, proj, tl, err = _timeline()
    if err:
        return err
    pool = proj.GetMediaPool()
    tt = body.get("trackType", "video")
    ti = int(body.get("trackIndex", 1))
    fr = int(body.get("fromFrame", -1))
    to = int(body.get("toFrame", -1))
    if fr < 0 or to < 0:
        return {"error": "fromFrame and toFrame are required"}
    target = _item_at_frame(tl, tt, ti, fr)
    if not target:
        return {"error": "No clip at %s track %d frame %d" % (tt, ti, fr)}
    before = _describe(target)
    src = safe(lambda: target.GetMediaPoolItem())
    if not src:
        return {"error": "That timeline item has no media pool source "
                         "(compound/Fusion/title?) --- cannot move it this way"}
    src_in = int(safe(lambda: target.GetLeftOffset()) or 0)
    if not safe(lambda: tl.DeleteClips([target])):
        return {"error": "Could not delete the clip; nothing changed"}
    landed, err = _place(pool, src, tt, ti, to, src_in, before["duration"])
    if err:
        return {"error": "Deleted the clip but could not re-place it at %d. "
                         "It is GONE from the timeline. %s" % (to, err.get("error")),
                "lost": before}
    return {"success": True, "from": before, "to": _describe(landed)}


def action_timeline_remove(body):
    """Remove the clip at a frame, leaving a gap. body: atFrame, trackIndex."""
    _, _, tl, err = _timeline()
    if err:
        return err
    tt = body.get("trackType", "video")
    ti = int(body.get("trackIndex", 1))
    at = int(body.get("atFrame", -1))
    if at < 0:
        return {"error": "atFrame is required"}
    target = _item_at_frame(tl, tt, ti, at)
    if not target:
        return {"error": "No clip at %s track %d frame %d" % (tt, ti, at)}
    d = _describe(target)
    ok = safe(lambda: tl.DeleteClips([target]))
    gone = _item_at_frame(tl, tt, ti, at) is None
    return {"success": bool(ok) and gone, "removed": d, "verifiedGone": gone}


def action_timeline_read(body):
    """Every clip on a track with its frame span --- the map you edit against."""
    _, _, tl, err = _timeline()
    if err:
        return err
    tt = body.get("trackType", "video")
    ti = int(body.get("trackIndex", 1))
    out = []
    for it in (safe(lambda: tl.GetItemListInTrack(tt, ti)) or []):
        d = _describe(it)
        mpi = safe(lambda it=it: it.GetMediaPoolItem())
        d["mediaId"] = safe(lambda: mpi.GetMediaId()) if mpi else None
        d["end"] = int(safe(lambda it=it: it.GetEnd()) or 0)
        out.append(d)
    return {"timeline": safe(lambda: tl.GetName()), "trackType": tt,
            "trackIndex": ti, "count": len(out), "clips": out}


def action_append_to_timeline(body):'''
once(A1, "verb insertion point")
src = src.replace(A1, B1)

A2 = '    "/media/append":                action_append_to_timeline,'
B2 = ('    "/media/append":                action_append_to_timeline,\n'
      '    "/timeline/place":              action_timeline_place,\n'
      '    "/timeline/swap":               action_timeline_swap,\n'
      '    "/timeline/move":               action_timeline_move,\n'
      '    "/timeline/remove":             action_timeline_remove,\n'
      '    "/timeline/read":               action_timeline_read,')
once(A2, "route table")
src = src.replace(A2, B2)

io.open(P, "w", encoding="utf-8").write(src)
print("WROTE", P)
