"""Add snapshot/merge/restore, ripple-delete, trim, and keyboard rebind.

Each of these exists because a Resolve API is missing, not because the obvious
call was overlooked. Measured 2026-07-22 unless noted.

  merge/restore   Timeline.CreateCompoundClip works (49 clips -> 1, 70 ms) but
                  there is NO un-nest and NO scripted Undo -- checked on
                  Project, Timeline and TimelineItem, all absent. So the only
                  way to make a merge reversible is to record the full layout
                  BEFORE merging and rebuild it afterwards from that record.
  ripple-delete   No ripple API. Composite: delete, then re-place every later
                  clip shifted left. Done leftmost-first so a shifted clip
                  never lands on top of one not yet moved.
  trim            No trim API either. Changing a duration means delete +
                  re-place, which creates a NEW TimelineItem -- any Fusion
                  comp or grade on the old one is destroyed. This verb detects
                  that and refuses unless forced, because the earlier
                  "extend to 3x" silently threw away a fade the user had just
                  asked for.
  keyboard rebind The 4-byte key field is understood (Qt keycode | modifier
                  bits, verified against a live Numpad-6 assignment). BUT
                  Resolve rewrites keyboard.preset.xml from its in-memory copy
                  on preset operations -- an external edit made while Resolve
                  was running was silently reverted. So this refuses to write
                  while Resolve is running rather than appear to succeed.
"""
import io, sys

P = r"F:\AMBIGUITY\TOOLS\davinci-bridge\src\CursorBridge.py"
src = io.open(P, "r", encoding="utf-8").read()


def once(needle, label):
    n = src.count(needle)
    if n != 1:
        sys.exit("ANCHOR FAIL [%s]: %d occurrences, expected 1" % (label, n))
    print("anchor ok [%s]" % label)


A1 = "def action_timeline_read(body):"
B1 = r'''def _snapshot_tracks(tl, track_type="video"):
    """Record every clip on every track: enough to rebuild the layout exactly.

    Items with no media pool source (nested compounds, titles, generators)
    cannot be re-placed from a mediaId, so they are recorded but flagged --
    a restore that silently dropped them would look like it worked.
    """
    clips, unrestorable = [], []
    count = int(safe(lambda: tl.GetTrackCount(track_type)) or 0)
    for ti in range(1, count + 1):
        for it in (safe(lambda: tl.GetItemListInTrack(track_type, ti)) or []):
            mpi = safe(lambda it=it: it.GetMediaPoolItem())
            mid = safe(lambda: mpi.GetMediaId()) if mpi else None
            rec = {
                "trackType": track_type,
                "trackIndex": ti,
                "start": int(safe(lambda it=it: it.GetStart()) or 0),
                "duration": int(safe(lambda it=it: it.GetDuration()) or 0),
                "srcIn": int(safe(lambda it=it: it.GetLeftOffset()) or 0),
                "mediaId": mid,
                "name": safe(lambda it=it: it.GetName()),
            }
            (clips if mid else unrestorable).append(rec)
    return clips, unrestorable


def action_timeline_snapshot(body):
    """Read-only record of the current layout. Also the unit merge-undo uses."""
    _, _, tl, err = _timeline()
    if err:
        return err
    tt = body.get("trackType", "video")
    clips, unrestorable = _snapshot_tracks(tl, tt)
    return {
        "timeline": safe(lambda: tl.GetName()),
        "trackType": tt,
        "count": len(clips),
        "clips": clips,
        "unrestorable": unrestorable,
        "fullyRestorable": not unrestorable,
    }


def action_timeline_restore(body):
    """Rebuild a recorded layout. body: clips[], [clearFirst=true]

    Used as the undo for a merge. Clears the target tracks first so the
    compound clip left behind by the merge does not survive alongside the
    restored originals.
    """
    _, proj, tl, err = _timeline()
    if err:
        return err
    pool = proj.GetMediaPool()
    clips = body.get("clips", [])
    if not clips:
        return {"error": "clips array is required"}

    if body.get("clearFirst", True):
        tracks = sorted({(c.get("trackType", "video"), int(c["trackIndex"])) for c in clips})
        for tt, ti in tracks:
            existing = list(safe(lambda: tl.GetItemListInTrack(tt, ti)) or [])
            if existing:
                safe(lambda e=existing: tl.DeleteClips(e))

    by_id = _pool_items_by_ids(pool, [c["mediaId"] for c in clips if c.get("mediaId")])
    placed, failed = 0, []
    for c in sorted(clips, key=lambda x: (x["trackIndex"], x["start"])):
        item = by_id.get(c.get("mediaId"))
        if not item:
            failed.append({"name": c.get("name"), "why": "media pool item is gone"})
            continue
        landed, e = _place(pool, item, c.get("trackType", "video"),
                           int(c["trackIndex"]), int(c["start"]),
                           int(c.get("srcIn", 0)), int(c["duration"]))
        if e:
            failed.append({"name": c.get("name"), "start": c["start"],
                           "why": e.get("error")})
        else:
            placed += 1
    return {"success": not failed, "restored": placed,
            "requested": len(clips), "failed": failed}


def action_timeline_merge(body):
    """Collapse every video track into one compound clip -- reversibly.

    Resolve exposes no un-nest and no Undo, so the ONLY thing that makes this
    reversible is the snapshot taken here, before anything changes.
    """
    _, _, tl, err = _timeline()
    if err:
        return err
    name = body.get("name", "MERGED")
    clips, unrestorable = _snapshot_tracks(tl, "video")
    if unrestorable and not body.get("force"):
        return {"error": "%d item(s) on this timeline have no media pool source "
                         "(nested compound / title / generator). They could NOT "
                         "be rebuilt if you undo this merge. Pass force=true to "
                         "merge anyway and accept that." % len(unrestorable),
                "unrestorable": unrestorable}

    items = []
    for ti in range(1, int(safe(lambda: tl.GetTrackCount("video")) or 0) + 1):
        items.extend(safe(lambda: tl.GetItemListInTrack("video", ti)) or [])
    if not items:
        return {"error": "No video clips to merge"}

    result = safe(lambda: tl.CreateCompoundClip(items, {"name": name}))
    if not result:
        return {"error": "CreateCompoundClip failed; nothing changed"}

    if not body.get("_isUndo"):
        _push_undo(safe(lambda: tl.GetName()), "/timeline/restore",
                   {"clips": clips, "clearFirst": True})
    return {"success": True, "merged": len(items),
            "compound": safe(lambda: result.GetName()),
            "undoable": True, "snapshotClips": len(clips)}


def action_timeline_ripple_delete(body):
    """Delete the clip at a frame and close the gap it leaves.

    body: atFrame, trackIndex, [trackType=video]
    Shifts every later clip on that track left by the removed duration.
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
    removed = _describe(target)
    shift = removed["duration"]
    gap_start = removed["start"]

    # Record the followers BEFORE deleting anything -- their TimelineItem
    # handles do not survive the delete.
    later = []
    for it in (safe(lambda: tl.GetItemListInTrack(tt, ti)) or []):
        s = int(safe(lambda it=it: it.GetStart()) or 0)
        if s > gap_start:
            mpi = safe(lambda it=it: it.GetMediaPoolItem())
            mid = safe(lambda: mpi.GetMediaId()) if mpi else None
            if not mid:
                return {"error": "Clip '%s' at %d has no media pool source, so it "
                                 "cannot be shifted. Nothing was deleted."
                                 % (safe(lambda it=it: it.GetName()), s)}
            later.append({"mediaId": mid, "start": s,
                          "duration": int(safe(lambda it=it: it.GetDuration()) or 0),
                          "srcIn": int(safe(lambda it=it: it.GetLeftOffset()) or 0),
                          "name": safe(lambda it=it: it.GetName())})

    to_delete = [target] + [i for i in (safe(lambda: tl.GetItemListInTrack(tt, ti)) or [])
                            if int(safe(lambda i=i: i.GetStart()) or 0) > gap_start]
    if not safe(lambda: tl.DeleteClips(to_delete)):
        return {"error": "Delete failed; nothing changed"}

    by_id = _pool_items_by_ids(pool, [c["mediaId"] for c in later])
    moved, failed = 0, []
    for c in sorted(later, key=lambda x: x["start"]):      # leftmost first
        item = by_id.get(c["mediaId"])
        landed, e = _place(pool, item, tt, ti, c["start"] - shift,
                           c["srcIn"], c["duration"]) if item else (None, {"error": "pool item gone"})
        if e:
            failed.append({"name": c["name"], "why": e.get("error")})
        else:
            moved += 1

    if not body.get("_isUndo"):
        restore = [{"trackType": tt, "trackIndex": ti, "start": c["start"],
                    "duration": c["duration"], "srcIn": c["srcIn"],
                    "mediaId": c["mediaId"], "name": c["name"]} for c in later]
        src_mpi = safe(lambda: target.GetMediaPoolItem())
        src_mid = safe(lambda: src_mpi.GetMediaId()) if src_mpi else None
        if src_mid:
            restore.append({"trackType": tt, "trackIndex": ti,
                            "start": removed["start"], "duration": removed["duration"],
                            "srcIn": 0, "mediaId": src_mid, "name": removed["name"]})
            _push_undo(safe(lambda: tl.GetName()), "/timeline/restore",
                       {"clips": restore, "clearFirst": True})

    return {"success": not failed, "removed": removed, "closedGapOf": shift,
            "shifted": moved, "failed": failed}


def action_timeline_trim(body):
    """Change a clip's duration in place. body: atFrame, trackIndex, frames.

    Resolve has no trim API, so this is delete + re-place -- which produces a
    NEW TimelineItem. Any Fusion comp (a fade, a flash) or colour work on the
    old item is DESTROYED. This refuses when it detects that, rather than
    quietly discarding work, which is exactly what happened earlier today when
    an "extend" silently removed a fade that had just been requested.
    """
    _, proj, tl, err = _timeline()
    if err:
        return err
    pool = proj.GetMediaPool()
    tt = body.get("trackType", "video")
    ti = int(body.get("trackIndex", 1))
    at = int(body.get("atFrame", -1))
    frames = int(body.get("frames", 0))
    if at < 0 or frames <= 0:
        return {"error": "atFrame and a positive frames count are required"}

    target = _item_at_frame(tl, tt, ti, at)
    if not target:
        return {"error": "No clip at %s track %d frame %d" % (tt, ti, at)}
    before = _describe(target)

    n_comps = int(safe(lambda: target.GetFusionCompCount()) or 0)
    versions = safe(lambda: target.GetVersionNameList(0)) or []
    has_work = n_comps > 0 or len(versions) > 1
    if has_work and not body.get("force"):
        return {"error": "This clip carries %d Fusion comp(s) and %d colour "
                         "version(s). Resolve has no trim API, so changing its "
                         "length means rebuilding the clip and that work would "
                         "be LOST. Nothing was changed. Pass force=true to "
                         "proceed anyway." % (n_comps, len(versions)),
                "clip": before, "fusionComps": n_comps,
                "colorVersions": len(versions)}

    src = safe(lambda: target.GetMediaPoolItem())
    if not src:
        return {"error": "That item has no media pool source; cannot rebuild it"}
    src_in = int(safe(lambda: target.GetLeftOffset()) or 0)

    if not safe(lambda: tl.DeleteClips([target])):
        return {"error": "Delete failed; nothing changed"}
    landed, e = _place(pool, src, tt, ti, before["start"], src_in, frames)
    if e:
        return {"error": "Deleted the clip but could not re-place it: %s. It is "
                         "GONE from the timeline." % e.get("error"), "lost": before}

    after = _describe(landed)
    if not body.get("_isUndo"):
        _push_undo(safe(lambda: tl.GetName()), "/timeline/trim",
                   {"trackType": tt, "trackIndex": ti, "atFrame": before["start"],
                    "frames": before["duration"], "force": True})
    return {"success": True, "before": before, "after": after,
            "exact": after["duration"] == frames,
            "discardedFusionComps": n_comps if has_work else 0}


def _resolve_is_running():
    """True if any Resolve.exe is alive. Writing the keyboard preset while it
    is would be pointless: Resolve rewrites that file from memory."""
    import ctypes
    from ctypes import wintypes
    k32 = ctypes.windll.kernel32
    arr = (wintypes.DWORD * 4096)()
    got = wintypes.DWORD()
    if not ctypes.windll.psapi.EnumProcesses(ctypes.byref(arr),
                                             ctypes.sizeof(arr), ctypes.byref(got)):
        return None                       # unknown -- caller must not assume safe
    for i in range(got.value // ctypes.sizeof(wintypes.DWORD)):
        pid = arr[i]
        h = k32.OpenProcess(0x1000, False, pid)
        if not h:
            continue
        buf = ctypes.create_unicode_buffer(260)
        size = wintypes.DWORD(260)
        ok = k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size))
        k32.CloseHandle(h)
        if ok and buf.value.split("\\")[-1].lower() == "resolve.exe":
            return True
    return False


def action_keyboard_rebind(body):
    """Rebind (or clear) a command's keyboard shortcut.

    body: command, key (raw Qt value) | clear=true, [allowWhileRunning=false]

    Only the 4-byte key field is touched -- no records are resized, so the
    hash-map layout is untouched.
    """
    import re as _re
    import struct as _struct

    cmd = body.get("command", "")
    if not cmd:
        return {"error": "command is required"}
    if "key" not in body and not body.get("clear"):
        return {"error": "pass key (raw Qt value) or clear=true"}
    new_val = 0 if body.get("clear") else int(body["key"])

    running = _resolve_is_running()
    if running is not False and not body.get("allowWhileRunning"):
        return {"error": "DaVinci Resolve %s running. It rewrites "
                         "keyboard.preset.xml from its in-memory copy, so an "
                         "edit made now would be silently reverted (measured "
                         "2026-07-22). Quit Resolve first."
                         % ("appears to be" if running else "may be"),
                "resolveRunning": running}

    path = KEYBOARD_PRESET
    try:
        with open(path, "r", encoding="utf-8") as fh:
            txt = fh.read()
    except Exception as e:
        return {"error": "Could not read keyboard preset: %s" % e}
    m = _re.search(r"<PresetListBA>([0-9a-fA-F]+)</PresetListBA>", txt)
    if not m:
        return {"error": "No PresetListBA blob in keyboard.preset.xml"}
    blob = bytearray(bytes.fromhex(m.group(1)))

    off, i, n = None, 0, len(blob)
    while i + 4 <= n:
        L = _struct.unpack_from(">I", blob, i)[0]
        if 4 <= L <= 400 and L % 2 == 0 and i + 4 + L <= n:
            try:
                s = bytes(blob[i + 4:i + 4 + L]).decode("utf-16be", errors="strict")
            except UnicodeDecodeError:
                s = None
            if s == cmd:
                off = i + 4 + L + 8
                break
            if s and s.strip() and all(32 <= ord(c) < 0x2500 for c in s):
                i += 4 + L
                continue
        i += 1
    if off is None:
        return {"error": "No command named '%s' in the preset" % cmd}

    old_val = _struct.unpack_from(">I", blob, off)[0]
    backup = path + ".bak-ambiguity"
    try:
        with open(backup, "w", encoding="utf-8") as fh:
            fh.write(txt)
    except Exception as e:
        return {"error": "Refusing to write without a backup (%s)" % e}

    _struct.pack_into(">I", blob, off, new_val)
    out = txt[:m.start(1)] + bytes(blob).hex() + txt[m.end(1):]
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(out)

    with open(path, "r", encoding="utf-8") as fh:
        check = fh.read()
    cb = bytes.fromhex(_re.search(r"<PresetListBA>([0-9a-fA-F]+)</PresetListBA>", check).group(1))
    now = _struct.unpack_from(">I", cb, off)[0]
    _, _, old_label = _decode_binding(old_val)
    _, _, new_label = _decode_binding(new_val)
    return {"success": now == new_val, "command": cmd,
            "was": "0x%08x (%s)" % (old_val, old_label),
            "now": "0x%08x (%s)" % (now, new_label),
            "backup": backup,
            "note": "Resolve must be restarted to load this."}


def action_timeline_read(body):'''
once(A1, "verb insertion point")
src = src.replace(A1, B1)

A2 = '    "/shortcut/fire":               action_shortcut_fire,'
B2 = ('    "/shortcut/fire":               action_shortcut_fire,\n'
      '    "/timeline/snapshot":           action_timeline_snapshot,\n'
      '    "/timeline/restore":            action_timeline_restore,\n'
      '    "/timeline/merge":              action_timeline_merge,\n'
      '    "/timeline/ripple-delete":      action_timeline_ripple_delete,\n'
      '    "/timeline/trim":               action_timeline_trim,\n'
      '    "/keyboard/rebind":             action_keyboard_rebind,')
once(A2, "route table")
src = src.replace(A2, B2)

io.open(P, "w", encoding="utf-8").write(src)
print("WROTE", P)
