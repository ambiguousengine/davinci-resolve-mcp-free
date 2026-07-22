#!/usr/bin/env python3
"""
CursorBridge - HTTP bridge between DaVinci Resolve and Cursor MCP.

Launch from: Workspace > Scripts > CursorBridge
Exposes a JSON API on localhost:9876 that an external MCP server can query.
Works with DaVinci Resolve Free (no external scripting required).

GET  endpoints = read-only queries
POST endpoints = write / mutation operations
"""

import json
import os
import sys
import time
import socketserver
import traceback
import concurrent.futures
from datetime import datetime
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

# ThreadingHTTPServer only exists in Python 3.7+. Resolve's embedded Python can be
# 3.6, so build an equivalent from the mixin rather than importing it directly.
try:
    from http.server import ThreadingHTTPServer  # noqa: F401  (Python 3.7+)
except ImportError:
    class ThreadingHTTPServer(socketserver.ThreadingMixIn, HTTPServer):
        daemon_threads = True

HOST = "127.0.0.1"
PORT = 9876
BRIDGE_VERSION = "2.1.0"
RESOLVE_CALL_TIMEOUT = 25  # seconds — bound how long a single Resolve API call can block the bridge

# ---------------------------------------------------------------------------
# Resolve bootstrap — grab the object while Fusion globals are in scope
# ---------------------------------------------------------------------------
resolve_obj = None


def _init_resolve():
    global resolve_obj
    for attempt in (
        lambda: fu.GetResolve(),        # noqa: F821
        lambda: fusion.GetResolve(),     # noqa: F821
        lambda: bmd.scriptapp("Resolve"),  # noqa: F821
    ):
        try:
            resolve_obj = attempt()
            if resolve_obj:
                return
        except Exception:
            pass
    try:
        import DaVinciResolveScript as dvr
        resolve_obj = dvr.scriptapp("Resolve")
    except Exception:
        pass


_init_resolve()

if resolve_obj:
    print("[CursorBridge] Connected to Resolve: %s %s" % (
        resolve_obj.GetProductName(), resolve_obj.GetVersionString()))
else:
    print("[CursorBridge] WARNING: Could not obtain Resolve object.")


# ---------------------------------------------------------------------------
# Hardening: serialize all Resolve API calls through one worker + bounded timeout
#
# Resolve's scripting API is not safe to call concurrently from multiple threads,
# so every request still executes one-at-a-time — but routing through a
# ThreadPoolExecutor(max_workers=1) means a single call that blocks inside Resolve
# (slow render/export, a modal dialog, a UI repaint) can no longer wedge the whole
# HTTP server. It fails that one request cleanly after RESOLVE_CALL_TIMEOUT and
# leaves the bridge able to keep serving subsequent requests (queued behind the
# stuck one, each with its own bounded wait) instead of hanging forever and
# requiring a manual restart.
# ---------------------------------------------------------------------------
_executor = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="ResolveCall")

# Resolve's Workspace > Scripts console does NOT reliably define __file__, so
# deriving the logs dir from it crashes the whole script at load time. Resolve any
# base dir defensively, with a hardcoded fallback to the known install location and
# then the OS temp dir, so logging can never take the bridge down.
def _resolve_base_dir():
    try:
        return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    except Exception:
        pass
    known = r"F:\AMBIGUITY\TOOLS\davinci-bridge"
    if os.path.isdir(known):
        return known
    import tempfile
    return tempfile.gettempdir()


_BASE_DIR = _resolve_base_dir()
_LOG_PATH = os.path.join(_BASE_DIR, "logs", "cursorbridge_calls.log")
try:
    os.makedirs(os.path.dirname(_LOG_PATH), exist_ok=True)
except Exception:
    pass


def _log(msg):
    try:
        with open(_LOG_PATH, "a", encoding="utf-8") as f:
            f.write("%s %s\n" % (datetime.now().isoformat(timespec="seconds"), msg))
    except Exception:
        pass


def _call_with_timeout(path, handler, arg):
    """Run a route handler on the serialized Resolve worker, bounded by RESOLVE_CALL_TIMEOUT.
    Returns (result, error_dict). On timeout, error_dict is set and the underlying
    call is left running in the background (Python cannot forcibly kill a blocked
    native call) — but the HTTP server itself stays responsive for new requests."""
    start = time.monotonic()
    _log("START %s" % path)
    future = _executor.submit(handler, arg)
    try:
        result = future.result(timeout=RESOLVE_CALL_TIMEOUT)
        _log("OK    %s (%.2fs)" % (path, time.monotonic() - start))
        return result, None
    except concurrent.futures.TimeoutError:
        _log("STUCK %s (still running after %ss)" % (path, RESOLVE_CALL_TIMEOUT))
        return None, {
            "error": (
                "Resolve call to '%s' did not return within %ss. DaVinci Resolve's "
                "scripting API may be blocked (a slow render/export, a modal dialog, "
                "or a busy UI repaint). The call may still finish in the background — "
                "try again shortly. If every call keeps timing out, restart CursorBridge."
            ) % (path, RESOLVE_CALL_TIMEOUT)
        }
    except Exception:
        _log("ERROR %s\n%s" % (path, traceback.format_exc()))
        return None, {"error": traceback.format_exc()}


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def safe(fn):
    try:
        return fn()
    except Exception:
        return None


def _resolve():
    r = resolve_obj
    if not r:
        return None, {"error": "Not connected to Resolve"}
    return r, None


def _project():
    r, err = _resolve()
    if err:
        return None, None, err
    pm = r.GetProjectManager()
    if not pm:
        return None, None, {"error": "No project manager"}
    proj = pm.GetCurrentProject()
    if not proj:
        return None, None, {"error": "No project open"}
    return r, proj, None


def _timeline():
    r, proj, err = _project()
    if err:
        return None, None, None, err
    tl = proj.GetCurrentTimeline()
    if not tl:
        return None, None, None, {"error": "No timeline open"}
    return r, proj, tl, None


def _clip_at(body):
    """Locate a TimelineItem by trackType / trackIndex / clipIndex."""
    _, _, tl, err = _timeline()
    if err:
        return None, err
    tt = body.get("trackType", "video")
    ti = int(body.get("trackIndex", 1))
    ci = int(body.get("clipIndex", 0))
    items = safe(lambda: tl.GetItemListInTrack(tt, ti))
    if not items:
        return None, {"error": "No clips on %s track %d" % (tt, ti)}
    if ci < 0 or ci >= len(items):
        return None, {"error": "clipIndex %d out of range (0-%d)" % (ci, len(items) - 1)}
    return items[ci], None


def _find_pool_item(pool, name):
    """Recursively search media pool for an item by name."""
    def search(folder):
        clips = folder.GetClipList() or []
        for c in clips:
            if safe(lambda: c.GetName()) == name:
                return c
        for sf in (folder.GetSubFolderList() or []):
            hit = search(sf)
            if hit:
                return hit
        return None
    root = pool.GetRootFolder()
    return search(root) if root else None


def _find_pool_items_all(pool, name):
    """Every media pool item matching `name` --- not just the first.

    _find_pool_item() stops at the first hit, which makes every name-based verb
    non-deterministic when the same filename exists in several bins (RUSHES /
    _AUDIT_SCRATCH / TRASH). The first hit is routinely NOT the one the timeline
    references, so a clip can read as 'online' in the pool while the timeline
    instance stays offline. Callers that must not guess use this and fail loud.
    """
    hits = []

    def search(folder):
        for c in (folder.GetClipList() or []):
            if safe(lambda: c.GetName()) == name:
                hits.append(c)
        for sf in (folder.GetSubFolderList() or []):
            search(sf)

    root = pool.GetRootFolder()
    if root:
        search(root)
    return hits


def _find_folder_by_path(pool, path):
    """Find a media pool folder by slash-separated path (e.g. 'Footage/Day1')."""
    root = pool.GetRootFolder()
    if not root:
        return None
    if not path or path.lower() in ("root", "/", ""):
        return root
    parts = [p for p in path.split("/") if p and p.lower() not in ("root", "master")]
    current = root
    for part in parts:
        subfolders = current.GetSubFolderList() or []
        found = None
        for sf in subfolders:
            if safe(lambda sf=sf: sf.GetName()) == part:
                found = sf
                break
        if not found:
            return None
        current = found
    return current


def _resolve_clip_refs(tl, clip_refs):
    """Resolve a list of {trackType, trackIndex, clipIndex} dicts to TimelineItem objects."""
    items = []
    for ref in clip_refs:
        tt = ref.get("trackType", "video")
        ti = int(ref.get("trackIndex", 1))
        ci = int(ref.get("clipIndex", 0))
        track_items = safe(lambda tt=tt, ti=ti: tl.GetItemListInTrack(tt, ti))
        if track_items and 0 <= ci < len(track_items):
            items.append(track_items[ci])
    return items


def _get_album(gallery, body):
    """Get a gallery album from body params (albumIndex + albumType), or current album."""
    album_index = int(body.get("albumIndex", 0))
    album_type = body.get("albumType", "still")
    if album_index > 0:
        if album_type == "powergrade":
            albums = safe(lambda: gallery.GetGalleryPowerGradeAlbums()) or []
        else:
            albums = safe(lambda: gallery.GetGalleryStillAlbums()) or []
        if album_index < 1 or album_index > len(albums):
            return None, {"error": "Album index %d out of range (1-%d)" % (album_index, len(albums))}
        return albums[album_index - 1], None
    album = safe(lambda: gallery.GetCurrentStillAlbum())
    if not album:
        return None, {"error": "No album selected"}
    return album, None


# ---------------------------------------------------------------------------
# GET handlers  (read-only)
# ---------------------------------------------------------------------------

def gather_status():
    r, err = _resolve()
    if err:
        return {"connected": False, "bridgeVersion": BRIDGE_VERSION, **err}
    return {
        "connected": True,
        "bridgeVersion": BRIDGE_VERSION,
        "product": safe(lambda: r.GetProductName()),
        "version": safe(lambda: r.GetVersionString()),
    }


def gather_project():
    _, proj, err = _project()
    if err:
        return err
    keys = [
        "timelineResolutionWidth", "timelineResolutionHeight",
        "timelineFrameRate", "timelinePlaybackFrameRate",
        "colorScienceMode", "audioCaptureNumChannels", "superScale",
    ]
    settings = {}
    for k in keys:
        v = safe(lambda k=k: proj.GetSetting(k))
        if v not in (None, ""):
            settings[k] = v
    return {
        "name": safe(lambda: proj.GetName()),
        "timelineCount": safe(lambda: proj.GetTimelineCount()),
        "currentRenderFormatAndCodec": safe(lambda: proj.GetCurrentRenderFormatAndCodec()),
        "settings": settings,
    }


def gather_page():
    r, err = _resolve()
    if err:
        return err
    return {"page": safe(lambda: r.GetCurrentPage())}


def gather_timeline():
    _, proj, tl, err = _timeline()
    if err:
        return err
    vt = safe(lambda: tl.GetTrackCount("video")) or 0
    at = safe(lambda: tl.GetTrackCount("audio")) or 0
    st = safe(lambda: tl.GetTrackCount("subtitle")) or 0
    names = {}
    for i in range(1, vt + 1):
        n = safe(lambda i=i: tl.GetTrackName("video", i))
        if n:
            names["video_%d" % i] = n
    for i in range(1, at + 1):
        n = safe(lambda i=i: tl.GetTrackName("audio", i))
        if n:
            names["audio_%d" % i] = n
    tl_keys = [
        "timelineResolutionWidth", "timelineResolutionHeight",
        "timelineFrameRate", "timelineOutputResolutionWidth",
        "timelineOutputResolutionHeight",
    ]
    settings = {}
    for k in tl_keys:
        v = safe(lambda k=k: tl.GetSetting(k))
        if v not in (None, ""):
            settings[k] = v
    return {
        "name": safe(lambda: tl.GetName()),
        "startFrame": safe(lambda: tl.GetStartFrame()),
        "endFrame": safe(lambda: tl.GetEndFrame()),
        "startTimecode": safe(lambda: tl.GetStartTimecode()),
        "currentTimecode": safe(lambda: tl.GetCurrentTimecode()),
        "trackCount": {"video": vt, "audio": at, "subtitle": st},
        "trackNames": names,
        "markInOut": safe(lambda: tl.GetMarkInOut()),
        "settings": settings,
    }


def gather_clips(track_type, track_index):
    _, _, tl, err = _timeline()
    if err:
        return err
    mx = safe(lambda: tl.GetTrackCount(track_type)) or 0
    if track_index < 1 or track_index > mx:
        return {"error": "Track index %d out of range (1-%d) for %s" % (track_index, mx, track_type)}
    items = safe(lambda: tl.GetItemListInTrack(track_type, track_index))
    if not items:
        return {"trackType": track_type, "trackIndex": track_index, "clips": []}
    clips = []
    for item in items:
        cd = {
            "name": safe(lambda: item.GetName()),
            "duration": safe(lambda: item.GetDuration()),
            "start": safe(lambda: item.GetStart()),
            "end": safe(lambda: item.GetEnd()),
            "enabled": safe(lambda: item.GetClipEnabled()),
            "color": safe(lambda: item.GetClipColor()),
        }
        mp = safe(lambda: item.GetMediaPoolItem())
        if mp:
            props = safe(lambda: mp.GetClipProperty()) or {}
            for key in ("File Path", "Clip Name", "Resolution", "FPS", "Frames", "Duration", "Audio Ch"):
                if key in props:
                    cd[key] = props[key]
        clips.append(cd)
    return {"trackType": track_type, "trackIndex": track_index, "clips": clips}


def gather_markers():
    _, _, tl, err = _timeline()
    if err:
        return err
    raw = safe(lambda: tl.GetMarkers()) or {}
    return {"markers": [{"frameId": fid, **info} for fid, info in raw.items()]}


def gather_render():
    _, proj, err = _project()
    if err:
        return err
    return {
        "formatAndCodec": safe(lambda: proj.GetCurrentRenderFormatAndCodec()),
        "renderMode": safe(lambda: proj.GetCurrentRenderMode()),
        "renderJobList": safe(lambda: proj.GetRenderJobList()),
        "isRendering": safe(lambda: proj.IsRenderingInProgress()),
    }


def gather_media_pool():
    """List clips in current media pool folder."""
    _, proj, err = _project()
    if err:
        return err
    pool = proj.GetMediaPool()
    if not pool:
        return {"error": "No media pool"}
    folder = pool.GetCurrentFolder()
    if not folder:
        return {"error": "No current folder"}
    clips = folder.GetClipList() or []
    result = []
    for c in clips:
        result.append({
            "name": safe(lambda: c.GetName()),
            "clipColor": safe(lambda: c.GetClipColor()),
            "mediaId": safe(lambda: c.GetMediaId()),
        })
    subfolders = [safe(lambda sf=sf: sf.GetName()) for sf in (folder.GetSubFolderList() or [])]
    return {
        "folderName": safe(lambda: folder.GetName()),
        "clips": result,
        "subfolders": subfolders,
    }


def gather_media_pool_audit(qs):
    """Every media pool item with folder path, file path, mediaId and Usage.

    Exists because every other clip lookup in this bridge addresses items BY
    NAME, and a name is ambiguous exactly where it matters --- QMC-Storm holds
    35 repeated names across RUSHES / _AUDIT_SCRATCH / TRASH / REF. Worse, a
    repeated name is not necessarily a duplicate FILE: nike-ai.mp4 exists as
    .../Storm/ref/nike-ai.mp4 and .../Storm/1_source/video/nike-ai.mp4, which
    are different assets. So "de-duplicate by name" is unsafe by construction.

    Usage is Resolve's own count of how many timelines reference the item, so
    Usage == 0 is the only defensible basis for proposing a deletion. Items
    with no File Path are timelines/compounds living in the pool, flagged
    separately --- deleting one of those destroys an edit, not a reference.
    """
    _, proj, err = _project()
    if err:
        return err
    pool = proj.GetMediaPool()
    if not pool:
        return {"error": "No media pool"}

    items = []

    def walk(folder, path):
        name = safe(lambda: folder.GetName()) or "?"
        here = (path + "/" + name) if path else name
        for c in (safe(lambda: folder.GetClipList()) or []):
            props = safe(lambda c=c: c.GetClipProperty()) or {}
            if not isinstance(props, dict):
                props = {}
            usage_raw = props.get("Usage")
            try:
                usage = int(str(usage_raw).strip() or "0")
            except (TypeError, ValueError):
                usage = None
            file_path = props.get("File Path") or ""
            items.append({
                "folder": here,
                "name": safe(lambda c=c: c.GetName()),
                "mediaId": safe(lambda c=c: c.GetMediaId()),
                "filePath": file_path,
                "usage": usage,
                "usageRaw": usage_raw,
                "isTimelineOrGenerator": not file_path,
            })
        for sf in (safe(lambda: folder.GetSubFolderList()) or []):
            walk(sf, here)

    root = pool.GetRootFolder()
    if root:
        walk(root, "")

    unused = [i for i in items if i["usage"] == 0 and not i["isTimelineOrGenerator"]]
    return {
        "count": len(items),
        "unusedMediaCount": len(unused),
        "items": items,
    }


def gather_media_pool_structure(qs):
    """Get the media pool folder tree structure."""
    _, proj, err = _project()
    if err:
        return err
    pool = proj.GetMediaPool()
    if not pool:
        return {"error": "No media pool"}
    include_clips = qs.get("include_clips", ["false"])[0].lower() == "true"
    max_depth = int(qs.get("max_depth", ["10"])[0])

    def build_tree(folder, depth=0):
        if depth > max_depth:
            return {"name": safe(lambda: folder.GetName()), "truncated": True}
        clips = folder.GetClipList() or []
        subfolders = folder.GetSubFolderList() or []
        node = {
            "name": safe(lambda: folder.GetName()),
            "clipCount": len(clips),
            "subfolderCount": len(subfolders),
        }
        if include_clips:
            node["clips"] = [{"name": safe(lambda c=c: c.GetName()),
                              "mediaId": safe(lambda c=c: c.GetMediaId())} for c in clips]
        node["subfolders"] = [build_tree(sf, depth + 1) for sf in subfolders]
        return node

    root = pool.GetRootFolder()
    if not root:
        return {"error": "No root folder"}
    current = pool.GetCurrentFolder()
    return {
        "tree": build_tree(root),
        "currentFolder": safe(lambda: current.GetName()) if current else None,
    }


def gather_clip_metadata(qs):
    """Get metadata for a media pool clip by name."""
    _, proj, err = _project()
    if err:
        return err
    pool = proj.GetMediaPool()
    if not pool:
        return {"error": "No media pool"}
    clip_name = qs.get("clip_name", [""])[0]
    if not clip_name:
        return {"error": "clip_name parameter is required"}
    item = _find_pool_item(pool, clip_name)
    if not item:
        return {"error": "Clip '%s' not found in media pool" % clip_name}
    metadata = safe(lambda: item.GetMetadata()) or {}
    third_party = safe(lambda: item.GetThirdPartyMetadata()) or {}
    return {
        "name": safe(lambda: item.GetName()),
        "mediaId": safe(lambda: item.GetMediaId()),
        "metadata": metadata,
        "thirdPartyMetadata": third_party,
    }


def gather_clip_info(qs):
    """Get detailed clip properties for a media pool clip."""
    _, proj, err = _project()
    if err:
        return err
    pool = proj.GetMediaPool()
    if not pool:
        return {"error": "No media pool"}
    clip_name = qs.get("clip_name", [""])[0]
    if not clip_name:
        return {"error": "clip_name parameter is required"}
    item = _find_pool_item(pool, clip_name)
    if not item:
        return {"error": "Clip '%s' not found in media pool" % clip_name}
    props = safe(lambda: item.GetClipProperty()) or {}
    flags = safe(lambda: item.GetFlagList()) or []
    markers = safe(lambda: item.GetMarkers()) or {}
    mark_in_out = safe(lambda: item.GetMarkInOut()) or {}
    return {
        "name": safe(lambda: item.GetName()),
        "mediaId": safe(lambda: item.GetMediaId()),
        "clipColor": safe(lambda: item.GetClipColor()) or "",
        "flags": flags,
        "markers": {str(k): v for k, v in markers.items()},
        "markInOut": mark_in_out,
        "properties": props,
    }


def gather_clip_markers(qs):
    """Get markers on a specific timeline item."""
    _, _, tl, err = _timeline()
    if err:
        return err
    tt = qs.get("track_type", ["video"])[0]
    ti = int(qs.get("track_index", ["1"])[0])
    ci = int(qs.get("clip_index", ["0"])[0])
    items = safe(lambda: tl.GetItemListInTrack(tt, ti))
    if not items:
        return {"error": "No clips on %s track %d" % (tt, ti)}
    if ci < 0 or ci >= len(items):
        return {"error": "clipIndex %d out of range (0-%d)" % (ci, len(items) - 1)}
    item = items[ci]
    markers = safe(lambda: item.GetMarkers()) or {}
    return {
        "clipName": safe(lambda: item.GetName()),
        "markers": [{"frameId": fid, **info} for fid, info in markers.items()],
    }


def gather_clip_flags(qs):
    """Get flags on a specific timeline item."""
    _, _, tl, err = _timeline()
    if err:
        return err
    tt = qs.get("track_type", ["video"])[0]
    ti = int(qs.get("track_index", ["1"])[0])
    ci = int(qs.get("clip_index", ["0"])[0])
    items = safe(lambda: tl.GetItemListInTrack(tt, ti))
    if not items:
        return {"error": "No clips on %s track %d" % (tt, ti)}
    if ci < 0 or ci >= len(items):
        return {"error": "clipIndex %d out of range (0-%d)" % (ci, len(items) - 1)}
    item = items[ci]
    flags = safe(lambda: item.GetFlagList()) or []
    return {"clipName": safe(lambda: item.GetName()), "flags": flags}


def gather_clip_properties(qs):
    """Get transform/compositing properties of a timeline item.

    AMBIGUITY PATCH: the MCP layer's get_clip_properties GETs /clip/properties, but that
    path was only registered for POST (set_clip_properties), so the read side never
    existed and every call returned "Unknown GET endpoint". Without this there is no way
    to verify a transform independently of the tool that set it.
    """
    _, _, tl, err = _timeline()
    if err:
        return err
    tt = qs.get("track_type", ["video"])[0]
    ti = int(qs.get("track_index", ["1"])[0])
    ci = int(qs.get("clip_index", ["0"])[0])
    items = safe(lambda: tl.GetItemListInTrack(tt, ti))
    if not items:
        return {"error": "No clips on %s track %d" % (tt, ti)}
    if ci < 0 or ci >= len(items):
        return {"error": "clipIndex %d out of range (0-%d)" % (ci, len(items) - 1)}
    item = items[ci]
    props = safe(lambda: item.GetProperty())
    if props is None:
        return {"error": "Resolve returned no properties for this clip"}
    return {
        "clipName": safe(lambda: item.GetName()),
        "trackType": tt,
        "trackIndex": ti,
        "clipIndex": ci,
        "properties": props,
    }


def gather_current_video_item(qs):
    """Get the current video item at the playhead."""
    _, _, tl, err = _timeline()
    if err:
        return err
    item = safe(lambda: tl.GetCurrentVideoItem())
    if not item:
        return {"error": "No current video item at playhead"}
    mp = safe(lambda: item.GetMediaPoolItem())
    props = {}
    if mp:
        props = safe(lambda: mp.GetClipProperty()) or {}
    track_info = safe(lambda: item.GetTrackTypeAndIndex()) or []
    return {
        "name": safe(lambda: item.GetName()),
        "duration": safe(lambda: item.GetDuration()),
        "start": safe(lambda: item.GetStart()),
        "end": safe(lambda: item.GetEnd()),
        "enabled": safe(lambda: item.GetClipEnabled()),
        "color": safe(lambda: item.GetClipColor()),
        "trackType": track_info[0] if len(track_info) > 0 else None,
        "trackIndex": track_info[1] if len(track_info) > 1 else None,
        "properties": {k: props[k] for k in ("File Path", "Clip Name", "Resolution", "FPS", "Frames", "Duration") if k in props},
    }


def gather_clip_thumbnail(qs):
    """Get thumbnail for current clip (Color page only)."""
    _, _, tl, err = _timeline()
    if err:
        return err
    data = safe(lambda: tl.GetCurrentClipThumbnailImage())
    if not data:
        return {"error": "Failed to get thumbnail. Make sure you are on the Color page."}
    return data


def gather_gallery_albums(qs):
    """List gallery still albums and PowerGrade albums."""
    _, proj, err = _project()
    if err:
        return err
    gallery = safe(lambda: proj.GetGallery())
    if not gallery:
        return {"error": "Cannot access gallery"}
    still_albums = safe(lambda: gallery.GetGalleryStillAlbums()) or []
    pg_albums = safe(lambda: gallery.GetGalleryPowerGradeAlbums()) or []
    current = safe(lambda: gallery.GetCurrentStillAlbum())
    current_name = safe(lambda: gallery.GetAlbumName(current)) if current else None
    result = {"currentAlbum": current_name, "stillAlbums": [], "powerGradeAlbums": []}
    for i, album in enumerate(still_albums):
        name = safe(lambda a=album: gallery.GetAlbumName(a))
        stills = safe(lambda a=album: a.GetStills()) or []
        result["stillAlbums"].append({"index": i + 1, "name": name, "stillCount": len(stills)})
    for i, album in enumerate(pg_albums):
        name = safe(lambda a=album: gallery.GetAlbumName(a))
        stills = safe(lambda a=album: a.GetStills()) or []
        result["powerGradeAlbums"].append({"index": i + 1, "name": name, "stillCount": len(stills)})
    return result


def gather_album_stills(qs):
    """List stills in a gallery album."""
    _, proj, err = _project()
    if err:
        return err
    gallery = safe(lambda: proj.GetGallery())
    if not gallery:
        return {"error": "Cannot access gallery"}
    album_index = int(qs.get("album_index", ["0"])[0])
    album_type = qs.get("album_type", ["still"])[0]
    if album_index > 0:
        if album_type == "powergrade":
            albums = safe(lambda: gallery.GetGalleryPowerGradeAlbums()) or []
        else:
            albums = safe(lambda: gallery.GetGalleryStillAlbums()) or []
        if album_index < 1 or album_index > len(albums):
            return {"error": "Album index %d out of range (1-%d)" % (album_index, len(albums))}
        album = albums[album_index - 1]
    else:
        album = safe(lambda: gallery.GetCurrentStillAlbum())
    if not album:
        return {"error": "No album selected"}
    album_name = safe(lambda: gallery.GetAlbumName(album))
    stills = safe(lambda: album.GetStills()) or []
    result = []
    for i, still in enumerate(stills):
        label = safe(lambda s=still: album.GetLabel(s))
        result.append({"index": i + 1, "label": label or ""})
    return {"albumName": album_name, "stills": result}


# ---------------------------------------------------------------------------
# POST handlers  (write / mutate)
# ---------------------------------------------------------------------------

VALID_PAGES = ("media", "cut", "edit", "fusion", "color", "fairlight", "deliver")


def action_open_page(body):
    r, err = _resolve()
    if err:
        return err
    page = body.get("page", "")
    if page not in VALID_PAGES:
        return {"error": "Invalid page. Must be one of: %s" % ", ".join(VALID_PAGES)}
    return {"success": bool(r.OpenPage(page)), "page": page}


def action_set_timecode(body):
    _, _, tl, err = _timeline()
    if err:
        return err
    tc = body.get("timecode", "")
    if not tc:
        return {"error": "timecode is required (e.g. '01:00:05:00')"}
    return {"success": bool(tl.SetCurrentTimecode(tc)), "timecode": tc}


# -- Markers ---------------------------------------------------------------

def action_add_marker(body):
    _, _, tl, err = _timeline()
    if err:
        return err
    frame_id = body.get("frameId")
    if frame_id is None:
        return {"error": "frameId is required"}
    color = body.get("color", "Blue")
    name = body.get("name", "")
    note = body.get("note", "")
    duration = body.get("duration", 1)
    custom = body.get("customData", "")
    ok = tl.AddMarker(int(frame_id), color, name, note, int(duration), custom)
    return {"success": bool(ok), "frameId": frame_id, "color": color}


def action_delete_marker(body):
    _, _, tl, err = _timeline()
    if err:
        return err
    frame = body.get("frameId")
    color = body.get("color")
    if frame is not None:
        return {"success": bool(tl.DeleteMarkerAtFrame(int(frame))), "frameId": frame}
    if color:
        return {"success": bool(tl.DeleteMarkersByColor(color)), "color": color}
    return {"error": "Provide frameId or color (use 'All' to delete all)"}


# -- Timeline management ---------------------------------------------------

def action_switch_timeline(body):
    _, proj, err = _project()
    if err:
        return err
    idx = body.get("index")
    if idx is None:
        return {"error": "index is required (1-based)"}
    idx = int(idx)
    total = proj.GetTimelineCount() or 0
    if idx < 1 or idx > total:
        return {"error": "index %d out of range (1-%d)" % (idx, total)}
    tl = proj.GetTimelineByIndex(idx)
    if not tl:
        return {"error": "Could not get timeline at index %d" % idx}
    ok = proj.SetCurrentTimeline(tl)
    return {"success": bool(ok), "timeline": safe(lambda: tl.GetName())}


def action_create_timeline(body):
    _, proj, err = _project()
    if err:
        return err
    name = body.get("name", "")
    if not name:
        return {"error": "name is required"}
    pool = proj.GetMediaPool()
    if not pool:
        return {"error": "No media pool"}
    tl = pool.CreateEmptyTimeline(name)
    if not tl:
        return {"error": "Failed to create timeline '%s'" % name}
    return {"success": True, "timeline": name}


def action_rename_timeline(body):
    _, _, tl, err = _timeline()
    if err:
        return err
    name = body.get("name", "")
    if not name:
        return {"error": "name is required"}
    return {"success": bool(tl.SetName(name)), "name": name}


def action_duplicate_timeline(body):
    _, _, tl, err = _timeline()
    if err:
        return err
    name = body.get("name", "")
    new_tl = tl.DuplicateTimeline(name) if name else tl.DuplicateTimeline()
    if not new_tl:
        return {"error": "Failed to duplicate timeline"}
    return {"success": True, "timeline": safe(lambda: new_tl.GetName())}


# -- Track management ------------------------------------------------------

def action_add_track(body):
    _, _, tl, err = _timeline()
    if err:
        return err
    tt = body.get("trackType", "video")
    sub = body.get("subTrackType", "")
    if sub:
        ok = tl.AddTrack(tt, sub)
    else:
        ok = tl.AddTrack(tt)
    return {"success": bool(ok), "trackType": tt}


def action_delete_track(body):
    _, _, tl, err = _timeline()
    if err:
        return err
    tt = body.get("trackType", "video")
    ti = int(body.get("trackIndex", 0))
    if ti < 1:
        return {"error": "trackIndex must be >= 1"}
    return {"success": bool(tl.DeleteTrack(tt, ti)), "trackType": tt, "trackIndex": ti}


def action_set_track_enable(body):
    _, _, tl, err = _timeline()
    if err:
        return err
    tt = body.get("trackType", "video")
    ti = int(body.get("trackIndex", 1))
    en = bool(body.get("enabled", True))
    return {"success": bool(tl.SetTrackEnable(tt, ti, en)), "enabled": en}


def action_set_track_lock(body):
    _, _, tl, err = _timeline()
    if err:
        return err
    tt = body.get("trackType", "video")
    ti = int(body.get("trackIndex", 1))
    locked = bool(body.get("locked", True))
    return {"success": bool(tl.SetTrackLock(tt, ti, locked)), "locked": locked}


def action_set_track_name(body):
    _, _, tl, err = _timeline()
    if err:
        return err
    tt = body.get("trackType", "video")
    ti = int(body.get("trackIndex", 1))
    name = body.get("name", "")
    if not name:
        return {"error": "name is required"}
    return {"success": bool(tl.SetTrackName(tt, ti, name)), "name": name}


# -- Media management ------------------------------------------------------

def action_import_media(body):
    _, proj, err = _project()
    if err:
        return err
    paths = body.get("filePaths", [])
    if not paths:
        return {"error": "filePaths array is required"}
    pool = proj.GetMediaPool()
    if not pool:
        return {"error": "No media pool"}
    items = pool.ImportMedia(paths)
    if not items:
        return {"error": "Import failed — check file paths are valid Windows paths accessible from Resolve"}
    return {
        "success": True,
        "imported": [safe(lambda i=i: i.GetName()) for i in items],
    }


def action_import_media_from_storage(body):
    r, proj, err = _project()
    if err:
        return err
    paths = body.get("filePaths", [])
    if not paths:
        return {"error": "filePaths array is required"}
    ms = r.GetMediaStorage()
    if not ms:
        return {"error": "No media storage"}
    items = ms.AddItemListToMediaPool(paths)
    if not items:
        return {"error": "Import from storage failed"}
    return {
        "success": True,
        "imported": [safe(lambda i=i: i.GetName()) for i in items],
    }


def _pool_item_by_ref(pool, body, key_id="mediaId", key_name="clip"):
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


def _needs_markinout(item):
    """Stills and compound clips ignore startFrame/endFrame on append.

    A still reports Frames == 1 but Resolve gives it a virtual source starting
    at frame 108000, and appending one always yields the 5-second default (150
    frames @30) no matter what range is asked for. Compound clips behave the
    same way. For both, SetMarkInOut on the POOL item is what actually governs
    the placed length --- measured frame-exact at 20, 45 and 90 frames.
    """
    props = safe(lambda: item.GetClipProperty()) or {}
    if not isinstance(props, dict):
        return False
    if not props.get("File Path"):
        return True                       # compound clip / generator
    try:
        return int(props.get("Frames", 0)) <= 1
    except (TypeError, ValueError):
        return str(props.get("Type", "")).lower() == "still"


def _place(pool, item, track_type, track_index, record_frame, src_in, frames):
    """The one primitive. Returns (landed_item, error).

    Verifies by re-reading the track: the API returns objects even in cases
    where nothing committed, so the return value is not evidence.
    """
    if _needs_markinout(item):
        # A still ignores startFrame/endFrame entirely (always the 5s default),
        # so its length is governed by SetMarkInOut on the POOL item. But the
        # mark range is interpreted at the MEDIA's frame rate and then conformed
        # to the timeline's: on a 30fps timeline a 24fps still asked for 45
        # frames lands as 56 (45 * 30/24). Rather than hardcode a ratio that is
        # only right for one pairing, ask for N, measure what landed, and
        # correct by the observed factor. One correction converges because the
        # relationship is linear.
        base = 108000                     # Resolve's virtual origin for stills
        want = int(frames)

        def attempt(mark_len, retries=2):
            # Measured 2026-07-22: this same call sequence, on a fresh gap,
            # committed cleanly on the first try in isolation (0/0.1/0.3s
            # delay all landed fine) --- but failed once, silently, inside a
            # long-lived bridge process mid-session (delete succeeded, the
            # follow-up append did not commit, leaving a real gap). Root
            # cause not pinned down; this retry exists because the failure
            # mode is a GAP, not a wrong-but-safe value, so it is worth a
            # couple of cheap extra attempts rather than surfacing at once.
            for _ in range(retries + 1):
                safe(lambda: item.SetMarkInOut(base, base + max(1, int(mark_len)) - 1, "video"))
                safe(lambda: pool.AppendToTimeline([{
                    "mediaPoolItem": item, "trackIndex": int(track_index),
                    "recordFrame": int(record_frame), "mediaType": 1}]))
                _, _, t, e = _timeline()
                if e:
                    return None
                found = _item_at_frame(t, track_type, track_index, int(record_frame))
                if found is not None:
                    return found
            return None

        landed2 = attempt(want)
        # Compound clips do NOT commit via SetMarkInOut --- nothing lands at all.
        # They do commit, frame-exact, via the plain dict append, but ONLY when
        # startFrame/endFrame are present; omit those keys and the append
        # silently does nothing. (Measured: 60 frames asked, 60 landed.)
        if landed2 is None:
            for _ in range(3):
                safe(lambda: pool.AppendToTimeline([{
                    "mediaPoolItem": item, "startFrame": 0, "endFrame": int(frames),
                    "trackIndex": int(track_index), "recordFrame": int(record_frame)}]))
                _, _, t, e = _timeline()
                if e:
                    return None, e
                landed2 = _item_at_frame(t, track_type, track_index, int(record_frame))
                if landed2 is not None:
                    return landed2, None
            return None, {"error": "Nothing committed at %s track %d frame %d "
                                   "after 3 attempts" % (track_type, track_index, record_frame)}
        got = int(safe(lambda: landed2.GetDuration()) or 0)
        if got != want and got > 0:
            _, _, t, _e = _timeline()
            safe(lambda: t.DeleteClips([landed2]))
            corrected = attempt(int(round(want * want / float(got))))
            if corrected is not None:
                landed2 = corrected
        return landed2, None

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
    for _ in range(3):
        safe(lambda: pool.AppendToTimeline([spec]))
        _, _, tl, err = _timeline()
        if err:
            return None, err
        landed = _item_at_frame(tl, track_type, track_index, int(record_frame))
        if landed:
            return landed, None
    return None, {"error": "Nothing committed at %s track %d frame %d after 3 attempts"
                           % (track_type, track_index, record_frame)}


def _describe(it):
    return {
        "name": safe(lambda: it.GetName()),
        "start": int(safe(lambda: it.GetStart()) or 0),
        "duration": int(safe(lambda: it.GetDuration()) or 0),
    }


# --------------------------------------------------------------------------
# Undo log for the timeline verbs below.
#
# Resolve's own Undo (Ctrl+Z) is NOT exposed to scripting --- confirmed absent
# on Project, Timeline, and TimelineItem (2026-07-22, after a CreateCompoundClip
# test call left no scripted way back). So this is an application-level undo:
# each verb records what it would take to reverse itself, in-memory, on this
# bridge process. It does NOT see edits made through the Resolve UI or through
# calls that bypass these verbs (e.g. a hand-written script) --- only actions
# that went through place/swap/move/remove are undoable.
#
# Each entry is a (route, body) pair that reverses the operation when replayed
# through the SAME dispatch functions below. That is deliberate: undoing an
# undo just replays another forward op, which pushes its own reversing entry
# --- so pressing undo twice is equivalent to a redo, with no separate redo
# path to maintain.
# --------------------------------------------------------------------------
_UNDO_STACK = []
_UNDO_MAX = 200


def _push_undo(timeline_name, route, body):
    _UNDO_STACK.append({"timeline": timeline_name, "route": route, "body": body})
    del _UNDO_STACK[:-_UNDO_MAX]


# ---------------------------------------------------------------------------
# Shortcut firing: name -> keyboard binding -> real input.  See module notes.
# ---------------------------------------------------------------------------
KEYBOARD_PRESET = os.path.join(
    os.path.expanduser("~"), "AppData", "Roaming", "Blackmagic Design",
    "DaVinci Resolve", "Preferences", "keyboard.preset.xml")

# Qt modifier bits, as stored in the preset's 4-byte key field.
_QT_SHIFT, _QT_CTRL, _QT_ALT = 0x02000000, 0x04000000, 0x08000000
_QT_META, _QT_KEYPAD = 0x10000000, 0x20000000

# Windows virtual-key codes for the modifiers we can press.
_VK_SHIFT, _VK_CTRL, _VK_ALT, _VK_LWIN = 0x10, 0x11, 0x12, 0x5B

# Qt Key_* values that do NOT coincide with a Windows VK code. Letters and
# digits do coincide (Qt::Key_A == 'A' == VK_A == 0x41), so they need no entry.
_QT_TO_VK = {
    0x01000000: 0x1B,  # Escape
    0x01000001: 0x09,  # Tab
    0x01000003: 0x08,  # Backspace
    0x01000004: 0x0D,  # Return
    0x01000005: 0x0D,  # Enter (keypad) -> same VK
    0x01000006: 0x2D,  # Insert
    0x01000007: 0x2E,  # Delete
    0x01000010: 0x24,  # Home
    0x01000011: 0x23,  # End
    0x01000012: 0x25,  # Left
    0x01000013: 0x26,  # Up
    0x01000014: 0x27,  # Right
    0x01000015: 0x28,  # Down
    0x01000016: 0x21,  # PageUp
    0x01000017: 0x22,  # PageDown
    0x20: 0x20,        # Space
}
for _i in range(24):                      # F1..F24
    _QT_TO_VK[0x01000030 + _i] = 0x70 + _i


def _keyboard_bindings(path=None):
    """command name -> raw 4-byte key field, parsed from the preset file.

    The blob is a Qt hash-map serialization: big-endian length-prefixed UTF-16BE
    strings, each followed by two constant fields then the key field. Bucket
    ORDER changes on every save, so records are located by re-syncing on each
    length prefix -- never by absolute offset.
    """
    import re as _re
    import struct as _struct
    path = path or KEYBOARD_PRESET
    try:
        with open(path, "r", encoding="utf-8") as _fh:
            txt = _fh.read()
    except Exception as e:
        return None, {"error": "Could not read keyboard preset: %s" % e}
    m = _re.search(r"<PresetListBA>([0-9a-fA-F]+)</PresetListBA>", txt)
    if not m:
        return None, {"error": "keyboard.preset.xml has no PresetListBA blob"}
    b = bytes.fromhex(m.group(1))

    out, i, n = {}, 0, len(b)
    while i + 4 <= n:
        L = _struct.unpack_from(">I", b, i)[0]
        if 4 <= L <= 400 and L % 2 == 0 and i + 4 + L <= n:
            try:
                s = b[i + 4:i + 4 + L].decode("utf-16be", errors="strict")
            except UnicodeDecodeError:
                s = None
            if s and s.strip() and all(32 <= ord(c) < 0x2500 for c in s):
                fo = i + 4 + L + 8
                if fo + 4 <= n and s not in out:
                    out[s] = _struct.unpack_from(">I", b, fo)[0]
                i += 4 + L
                continue
        i += 1
    if not out:
        return None, {"error": "Parsed no commands out of the keyboard preset"}
    return out, None


def _decode_binding(raw):
    """4-byte key field -> (vk, [modifier vks], human label), or (None, ..., why)."""
    if not raw:
        return None, [], "unbound"
    mods, labels = [], []
    if raw & _QT_CTRL:
        mods.append(_VK_CTRL); labels.append("Ctrl")
    if raw & _QT_ALT:
        mods.append(_VK_ALT); labels.append("Alt")
    if raw & _QT_SHIFT:
        mods.append(_VK_SHIFT); labels.append("Shift")
    if raw & _QT_META:
        mods.append(_VK_LWIN); labels.append("Meta")

    base = raw & 0x00FFFFFF
    if raw & _QT_KEYPAD and 0x30 <= base <= 0x39:
        vk = 0x60 + (base - 0x30)          # VK_NUMPAD0..9
        labels.append("Numpad%c" % base)
    elif base in _QT_TO_VK:
        vk = _QT_TO_VK[base]
        labels.append("0x%x" % base)
    elif 0x30 <= base <= 0x39 or 0x41 <= base <= 0x5A:
        vk = base                          # digits and letters map 1:1
        labels.append(chr(base))
    else:
        return None, [], "unmappable key value 0x%08x" % raw
    return vk, mods, "+".join(labels)


def _resolve_hwnd():
    """Find Resolve's real main window. Never hardcode -- the handle changes
    every launch, and a stale handle would send real keystrokes nowhere (or,
    worse, to whatever inherited the number)."""
    import ctypes
    from ctypes import wintypes
    user32, kernel32 = ctypes.windll.user32, ctypes.windll.kernel32
    found = []

    def proc_name(pid):
        h = kernel32.OpenProcess(0x1000, False, pid)
        if not h:
            return ""
        buf = ctypes.create_unicode_buffer(260)
        size = wintypes.DWORD(260)
        ok = kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size))
        kernel32.CloseHandle(h)
        return buf.value.split("\\")[-1].lower() if ok else ""

    CB = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)

    def cb(hwnd, _):
        if user32.IsWindowVisible(hwnd):
            ln = user32.GetWindowTextLengthW(hwnd)
            if ln > 0:
                buf = ctypes.create_unicode_buffer(ln + 1)
                user32.GetWindowTextW(hwnd, buf, ln + 1)
                pid = wintypes.DWORD()
                user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                # Match the PROCESS, not the title: a title match would also hit
                # the bridge's own console window ("DaVinci Resolve MCP Bridge").
                if proc_name(pid.value) == "resolve.exe":
                    found.append((hwnd, buf.value))
        return True

    user32.EnumWindows(CB(cb), 0)
    if not found:
        return None, None
    # Prefer the real editing window over any transient splash/dialog.
    for hwnd, title in found:
        if "davinci resolve" in title.lower():
            return hwnd, title
    return found[0][0], found[0][1]


def _send_keystroke(hwnd, vk, mod_vks):
    """Foreground Resolve, then send REAL input. Returns (ok, detail)."""
    import ctypes
    user32, kernel32 = ctypes.windll.user32, ctypes.windll.kernel32
    PUL = ctypes.POINTER(ctypes.c_ulong)

    class _KB(ctypes.Structure):
        _fields_ = [("wVk", ctypes.c_ushort), ("wScan", ctypes.c_ushort),
                    ("dwFlags", ctypes.c_ulong), ("time", ctypes.c_ulong),
                    ("dwExtraInfo", PUL)]

    class _MI(ctypes.Structure):
        _fields_ = [("dx", ctypes.c_long), ("dy", ctypes.c_long),
                    ("mouseData", ctypes.c_ulong), ("dwFlags", ctypes.c_ulong),
                    ("time", ctypes.c_ulong), ("dwExtraInfo", PUL)]

    class _HI(ctypes.Structure):
        _fields_ = [("uMsg", ctypes.c_ulong), ("wParamL", ctypes.c_short),
                    ("wParamH", ctypes.c_ushort)]

    class _II(ctypes.Union):
        _fields_ = [("ki", _KB), ("mi", _MI), ("hi", _HI)]

    class _INPUT(ctypes.Structure):
        # All three variants must be declared: sizeof() must match the native
        # INPUT struct (40 bytes on x64) or SendInput rejects every event on the
        # size check. Declaring only the keyboard variant returned 0/4 accepted.
        _fields_ = [("type", ctypes.c_ulong), ("ii", _II)]

    def ev(v, up):
        return _INPUT(1, _II(ki=_KB(v, 0, 0x0002 if up else 0, 0, None)))

    fg_before = user32.GetForegroundWindow()
    fg_tid = user32.GetWindowThreadProcessId(fg_before, None)
    my_tid = kernel32.GetCurrentThreadId()
    tgt_tid = user32.GetWindowThreadProcessId(hwnd, None)

    user32.AttachThreadInput(my_tid, fg_tid, True)
    user32.AttachThreadInput(my_tid, tgt_tid, True)
    user32.ShowWindow(hwnd, 9)                       # SW_RESTORE if minimized
    user32.BringWindowToTop(hwnd)
    user32.SetForegroundWindow(hwnd)
    user32.AttachThreadInput(my_tid, fg_tid, False)
    user32.AttachThreadInput(my_tid, tgt_tid, False)
    time.sleep(0.05)

    if user32.GetForegroundWindow() != hwnd:
        # Refuse rather than fire blind: this is real input, and it would land
        # in whatever window IS frontmost.
        return False, {"error": "Could not bring Resolve to the foreground; "
                                "refused to send input so it cannot land in "
                                "the wrong window."}

    seq = [ev(m, False) for m in mod_vks] + [ev(vk, False), ev(vk, True)] \
        + [ev(m, True) for m in reversed(mod_vks)]
    arr = (_INPUT * len(seq))(*seq)
    sent = user32.SendInput(len(seq), arr, ctypes.sizeof(_INPUT))
    if sent != len(seq):
        return False, {"error": "SendInput accepted only %d of %d events"
                                % (sent, len(seq))}
    return True, {"events": sent}


def action_shortcut_fire(body):
    """Trigger a Resolve command by name, using its keyboard shortcut.

    body: command  (e.g. "viewActiveWindowSelectionEffects"), or
          key      (raw 4-byte Qt binding, for testing)
          [list=true] to return matching command names instead of firing.

    VISIBLE SIDE EFFECT: brings Resolve to the foreground and leaves it there.
    Unlike every other verb here, this one interrupts what the user is doing.
    """
    bindings, err = _keyboard_bindings()
    if err:
        return err

    cmd = body.get("command", "")
    if body.get("list"):
        q = cmd.lower()
        hits = sorted(k for k in bindings if q in k.lower())
        return {"query": cmd, "count": len(hits), "commands": hits[:200]}

    if "key" in body:
        raw, cmd = int(body["key"]), cmd or "(raw key)"
    else:
        if not cmd:
            return {"error": "command is required (or pass list=true to search)"}
        if cmd not in bindings:
            near = sorted(k for k in bindings if cmd.lower() in k.lower())[:10]
            return {"error": "No command named '%s'" % cmd, "didYouMean": near}
        raw = bindings[cmd]

    vk, mods, label = _decode_binding(raw)
    if vk is None:
        return {"error": "Command '%s' is %s -- nothing to fire. Assign it a "
                         "shortcut in Resolve first." % (cmd, label),
                "command": cmd, "binding": label}

    hwnd, title = _resolve_hwnd()
    if not hwnd:
        return {"error": "Could not find a visible DaVinci Resolve window"}

    t0 = time.time()
    ok, detail = _send_keystroke(hwnd, vk, mods)
    ms = (time.time() - t0) * 1000.0
    if not ok:
        detail.update({"command": cmd, "binding": label})
        return detail
    return {
        "success": True, "command": cmd, "binding": label,
        "window": title, "ms": round(ms), "events": detail.get("events"),
        "note": "Resolve is now in the foreground and will stay there; "
                "focus is not restorable (Windows limitation, tested).",
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
    if not body.get("_isUndo"):
        _push_undo(safe(lambda: tl.GetName()), "/timeline/remove",
                   {"trackType": tt, "trackIndex": ti, "atFrame": d["start"]})
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
    prior_src = safe(lambda: target.GetMediaPoolItem())
    prior_media_id = safe(lambda: prior_src.GetMediaId()) if prior_src else None
    prior_src_in = int(safe(lambda: target.GetLeftOffset()) or 0)
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
    if not body.get("_isUndo") and prior_media_id:
        _push_undo(safe(lambda: tl.GetName()), "/timeline/swap",
                   {"trackType": tt, "trackIndex": ti, "atFrame": before["start"],
                    "mediaId": prior_media_id, "srcIn": prior_src_in})
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
    if not body.get("_isUndo"):
        _push_undo(safe(lambda: tl.GetName()), "/timeline/move",
                   {"trackType": tt, "trackIndex": ti, "fromFrame": to, "toFrame": fr})
    return {"success": True, "from": before, "to": _describe(landed)}


def action_timeline_remove(body):
    """Remove the clip at a frame, leaving a gap. body: atFrame, trackIndex."""
    _, proj, tl, err = _timeline()
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
    prior_src = safe(lambda: target.GetMediaPoolItem())
    prior_media_id = safe(lambda: prior_src.GetMediaId()) if prior_src else None
    prior_src_in = int(safe(lambda: target.GetLeftOffset()) or 0)
    ok = safe(lambda: tl.DeleteClips([target]))
    gone = _item_at_frame(tl, tt, ti, at) is None
    if ok and gone and not body.get("_isUndo") and prior_media_id:
        _push_undo(safe(lambda: tl.GetName()), "/timeline/place",
                   {"trackType": tt, "trackIndex": ti, "recordFrame": d["start"],
                    "frames": d["duration"], "mediaId": prior_media_id,
                    "srcIn": prior_src_in})
    return {"success": bool(ok) and gone, "removed": d, "verifiedGone": gone}


def action_timeline_undo(body):
    """Reverse the last place/swap/move/remove call made through this bridge.

    Refuses if the current timeline differs from the one the entry was
    recorded on --- replaying a compensating edit on the wrong timeline would
    itself be a silent corruption, so this asks you to switch back rather
    than guess. The entry stays on the stack when refused, so a later retry
    on the right timeline still works.
    """
    if not _UNDO_STACK:
        return {"error": "Nothing to undo", "stackDepth": 0}
    entry = _UNDO_STACK[-1]
    _, _, tl, err = _timeline()
    if err:
        return err
    current = safe(lambda: tl.GetName())
    if entry["timeline"] != current:
        return {"error": "Last recorded action was on timeline '%s', but '%s' "
                         "is current. Switch back to undo it." % (entry["timeline"], current),
                "recordedOn": entry["timeline"], "current": current}
    _UNDO_STACK.pop()
    fn = POST_ROUTES.get(entry["route"])
    body2 = dict(entry["body"])
    body2["_isUndo"] = True
    result = fn(body2)
    return {"undid": entry["route"], "of": entry["body"], "result": result,
            "stackDepth": len(_UNDO_STACK)}


def _snapshot_tracks(tl, track_type="video"):
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
    # Capture the source id NOW: after DeleteClips the TimelineItem handle is
    # dead and GetMediaPoolItem() returns None, which previously meant the undo
    # entry was silently never pushed and undo popped an unrelated action.
    _tgt_mpi = safe(lambda: target.GetMediaPoolItem())
    removed_media_id = safe(lambda: _tgt_mpi.GetMediaId()) if _tgt_mpi else None
    removed_src_in = int(safe(lambda: target.GetLeftOffset()) or 0)
    # Snapshot the WHOLE track for undo, not just the clips this verb touches:
    # /timeline/restore clears the track before rebuilding, so a partial record
    # would wipe untouched clips and never put them back (measured: the clip
    # before the gap disappeared on undo).
    _undo_track_snapshot = []
    for _it in (safe(lambda: tl.GetItemListInTrack(tt, ti)) or []):
        _mpi = safe(lambda _it=_it: _it.GetMediaPoolItem())
        _mid = safe(lambda: _mpi.GetMediaId()) if _mpi else None
        if _mid:
            _undo_track_snapshot.append({
                "trackType": tt, "trackIndex": ti,
                "start": int(safe(lambda _it=_it: _it.GetStart()) or 0),
                "duration": int(safe(lambda _it=_it: _it.GetDuration()) or 0),
                "srcIn": int(safe(lambda _it=_it: _it.GetLeftOffset()) or 0),
                "mediaId": _mid, "name": safe(lambda _it=_it: _it.GetName())})

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
        if _undo_track_snapshot:
            _push_undo(safe(lambda: tl.GetName()), "/timeline/restore",
                       {"clips": _undo_track_snapshot, "clearFirst": True})

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


def action_append_to_timeline(body):
    _, proj, err = _project()
    if err:
        return err
    pool = proj.GetMediaPool()
    if not pool:
        return {"error": "No media pool"}
    clip_name = body.get("clipName", "")
    if not clip_name:
        return {"error": "clipName is required"}
    item = _find_pool_item(pool, clip_name)
    if not item:
        return {"error": "Clip '%s' not found in media pool" % clip_name}
    tl_items = pool.AppendToTimeline([item])
    if not tl_items:
        return {"error": "Failed to append clip to timeline"}
    return {"success": True, "appended": clip_name}


# -- Clip operations -------------------------------------------------------

def action_set_clip_color(body):
    item, err = _clip_at(body)
    if err:
        return err
    color = body.get("color", "")
    if not color:
        ok = item.ClearClipColor()
        return {"success": bool(ok), "action": "cleared"}
    return {"success": bool(item.SetClipColor(color)), "color": color}


def action_set_clip_enabled(body):
    item, err = _clip_at(body)
    if err:
        return err
    en = bool(body.get("enabled", True))
    return {"success": bool(item.SetClipEnabled(en)), "enabled": en}


def action_set_clip_properties(body):
    item, err = _clip_at(body)
    if err:
        return err
    props = body.get("properties", {})
    if not props:
        return {"error": "properties dict is required (e.g. {\"Pan\": 0, \"Opacity\": 80})"}
    results = {}
    for k, v in props.items():
        results[k] = bool(item.SetProperty(k, v))
    return {"success": all(results.values()), "results": results}


def action_add_clip_marker(body):
    item, err = _clip_at(body)
    if err:
        return err
    fid = body.get("frameId")
    if fid is None:
        return {"error": "frameId is required"}
    ok = item.AddMarker(
        int(fid),
        body.get("color", "Blue"),
        body.get("name", ""),
        body.get("note", ""),
        int(body.get("duration", 1)),
        body.get("customData", ""),
    )
    return {"success": bool(ok)}


# -- Titles / Generators ---------------------------------------------------

def action_insert_title(body):
    _, _, tl, err = _timeline()
    if err:
        return err
    name = body.get("titleName", "")
    if not name:
        return {"error": "titleName is required (e.g. 'Text+', 'Scroll')"}
    fusion_title = body.get("fusionTitle", False)
    if fusion_title:
        item = tl.InsertFusionTitleIntoTimeline(name)
    else:
        item = tl.InsertTitleIntoTimeline(name)
    if not item:
        return {"error": "Failed to insert title '%s'" % name}
    return {"success": True, "title": name, "clipName": safe(lambda: item.GetName())}


def action_insert_generator(body):
    _, _, tl, err = _timeline()
    if err:
        return err
    name = body.get("generatorName", "")
    if not name:
        return {"error": "generatorName is required (e.g. 'Solid Color', '10 Step')"}
    fusion_gen = body.get("fusionGenerator", False)
    if fusion_gen:
        item = tl.InsertFusionGeneratorIntoTimeline(name)
    else:
        item = tl.InsertGeneratorIntoTimeline(name)
    if not item:
        return {"error": "Failed to insert generator '%s'" % name}
    return {"success": True, "generator": name, "clipName": safe(lambda: item.GetName())}


def action_insert_fusion_comp(body):
    _, _, tl, err = _timeline()
    if err:
        return err
    item = tl.InsertFusionCompositionIntoTimeline()
    if not item:
        return {"error": "Failed to insert Fusion composition"}
    return {"success": True, "clipName": safe(lambda: item.GetName())}


# -- Render -----------------------------------------------------------------

def action_set_render_settings(body):
    _, proj, err = _project()
    if err:
        return err
    settings = body.get("settings", {})
    if not settings:
        return {"error": "settings dict is required"}
    return {"success": bool(proj.SetRenderSettings(settings))}


def action_set_render_format(body):
    _, proj, err = _project()
    if err:
        return err
    fmt = body.get("format", "")
    codec = body.get("codec", "")
    if not fmt or not codec:
        return {"error": "format and codec are required"}
    return {"success": bool(proj.SetCurrentRenderFormatAndCodec(fmt, codec))}


def action_add_render_job(body):
    _, proj, err = _project()
    if err:
        return err
    job_id = proj.AddRenderJob()
    if not job_id:
        return {"error": "Failed to add render job — check render settings are complete"}
    return {"success": True, "jobId": job_id}


def action_start_rendering(body):
    _, proj, err = _project()
    if err:
        return err
    job_ids = body.get("jobIds", [])
    if job_ids:
        ok = proj.StartRendering(job_ids)
    else:
        ok = proj.StartRendering()
    return {"success": bool(ok)}


def action_stop_rendering(body):
    _, proj, err = _project()
    if err:
        return err
    proj.StopRendering()
    return {"success": True}


def action_delete_render_job(body):
    _, proj, err = _project()
    if err:
        return err
    job_id = body.get("jobId", "")
    if job_id:
        return {"success": bool(proj.DeleteRenderJob(job_id))}
    if body.get("all", False):
        return {"success": bool(proj.DeleteAllRenderJobs())}
    return {"error": "Provide jobId or set all=true"}


def action_get_render_formats(body):
    _, proj, err = _project()
    if err:
        return err
    formats = safe(lambda: proj.GetRenderFormats()) or {}
    fmt = body.get("format", "")
    if fmt:
        codecs = safe(lambda: proj.GetRenderCodecs(fmt)) or {}
        return {"format": fmt, "codecs": codecs}
    return {"formats": formats}


# -- Project ----------------------------------------------------------------

def action_save_project(body):
    _, proj, err = _project()
    if err:
        return err
    return {"success": bool(proj.GetName() and resolve_obj.GetProjectManager().SaveProject())}


def action_set_project_setting(body):
    _, proj, err = _project()
    if err:
        return err
    key = body.get("key", "")
    value = body.get("value", "")
    if not key:
        return {"error": "key is required"}
    return {"success": bool(proj.SetSetting(key, str(value))), "key": key, "value": value}


def action_set_timeline_setting(body):
    _, _, tl, err = _timeline()
    if err:
        return err
    key = body.get("key", "")
    value = body.get("value", "")
    if not key:
        return {"error": "key is required"}
    return {"success": bool(tl.SetSetting(key, str(value))), "key": key, "value": value}


def action_export_frame(body):
    _, proj, err = _project()
    if err:
        return err
    path = body.get("filePath", "")
    if not path:
        return {"error": "filePath is required (e.g. 'C:\\\\output\\\\frame.png')"}
    return {"success": bool(proj.ExportCurrentFrameAsStill(path)), "filePath": path}


def action_create_subtitles(body):
    _, _, tl, err = _timeline()
    if err:
        return err
    ok = tl.CreateSubtitlesFromAudio(body.get("settings", {}))
    return {"success": bool(ok)}


def action_detect_scene_cuts(body):
    _, _, tl, err = _timeline()
    if err:
        return err
    return {"success": bool(tl.DetectSceneCuts())}


# -- Media Pool Deep Access -------------------------------------------------

def action_navigate_media_pool(body):
    _, proj, err = _project()
    if err:
        return err
    pool = proj.GetMediaPool()
    if not pool:
        return {"error": "No media pool"}
    path = body.get("path", "")
    if not path or path.lower() in ("root", "/"):
        folder = pool.GetRootFolder()
    else:
        folder = _find_folder_by_path(pool, path)
    if not folder:
        return {"error": "Folder not found: '%s'" % path}
    ok = pool.SetCurrentFolder(folder)
    return {"success": bool(ok), "folder": safe(lambda: folder.GetName())}


def action_create_media_pool_folder(body):
    _, proj, err = _project()
    if err:
        return err
    pool = proj.GetMediaPool()
    if not pool:
        return {"error": "No media pool"}
    name = body.get("name", "")
    if not name:
        return {"error": "name is required"}
    parent_path = body.get("parentPath", "")
    if parent_path:
        parent = _find_folder_by_path(pool, parent_path)
        if not parent:
            return {"error": "Parent folder not found: '%s'" % parent_path}
    else:
        parent = pool.GetCurrentFolder()
    if not parent:
        return {"error": "No parent folder"}
    new_folder = pool.AddSubFolder(parent, name)
    if not new_folder:
        return {"error": "Failed to create folder '%s'" % name}
    return {"success": True, "folder": name}


def action_set_clip_metadata(body):
    _, proj, err = _project()
    if err:
        return err
    pool = proj.GetMediaPool()
    if not pool:
        return {"error": "No media pool"}
    clip_name = body.get("clipName", "")
    if not clip_name:
        return {"error": "clipName is required"}
    item = _find_pool_item(pool, clip_name)
    if not item:
        return {"error": "Clip '%s' not found in media pool" % clip_name}
    metadata = body.get("metadata", {})
    if not metadata:
        return {"error": "metadata dict is required"}
    ok = item.SetMetadata(metadata)
    return {"success": bool(ok)}


def action_set_pool_clip_property(body):
    _, proj, err = _project()
    if err:
        return err
    pool = proj.GetMediaPool()
    if not pool:
        return {"error": "No media pool"}
    clip_name = body.get("clipName", "")
    if not clip_name:
        return {"error": "clipName is required"}
    item = _find_pool_item(pool, clip_name)
    if not item:
        return {"error": "Clip '%s' not found in media pool" % clip_name}
    prop_name = body.get("propertyName", "")
    prop_value = body.get("propertyValue", "")
    if not prop_name:
        return {"error": "propertyName is required"}
    ok = item.SetClipProperty(prop_name, prop_value)
    return {"success": bool(ok), "property": prop_name}


def action_delete_media_pool_clips(body):
    _, proj, err = _project()
    if err:
        return err
    pool = proj.GetMediaPool()
    if not pool:
        return {"error": "No media pool"}
    clip_names = body.get("clipNames", [])
    if not clip_names:
        return {"error": "clipNames array is required"}
    items, not_found = [], []
    for name in clip_names:
        item = _find_pool_item(pool, name)
        if item:
            items.append(item)
        else:
            not_found.append(name)
    if not items:
        return {"error": "No matching clips found", "notFound": not_found}
    ok = pool.DeleteClips(items)
    return {"success": bool(ok), "deleted": len(items), "notFound": not_found}


def action_move_media_pool_clips(body):
    _, proj, err = _project()
    if err:
        return err
    pool = proj.GetMediaPool()
    if not pool:
        return {"error": "No media pool"}
    clip_names = body.get("clipNames", [])
    target_path = body.get("targetFolder", "")
    if not clip_names:
        return {"error": "clipNames array is required"}
    if not target_path:
        return {"error": "targetFolder path is required"}
    target = _find_folder_by_path(pool, target_path)
    if not target:
        return {"error": "Target folder not found: '%s'" % target_path}
    items = [i for name in clip_names for i in [_find_pool_item(pool, name)] if i]
    if not items:
        return {"error": "No matching clips found"}
    ok = pool.MoveClips(items, target)
    return {"success": bool(ok), "moved": len(items)}


def _clip_file_path(item):
    """Current 'File Path' property of a media pool item, or '' if unreadable."""
    props = safe(lambda: item.GetClipProperty()) or {}
    if isinstance(props, dict):
        return props.get("File Path") or ""
    return safe(lambda: item.GetClipProperty("File Path")) or ""


def _folder_basenames(folder_path):
    """Lower-cased basenames of every file under folder_path (recursive)."""
    found = set()
    for root, _dirs, files in os.walk(folder_path):
        for f in files:
            found.add(f.lower())
    return found


def _pool_items_by_ids(pool, ids):
    """Resolve mediaIds to items in ONE pool walk. mediaId is unique; a name is not."""
    want = set(ids)
    found = {}

    def search(folder):
        for c in (safe(lambda: folder.GetClipList()) or []):
            mid = safe(lambda c=c: c.GetMediaId())
            if mid in want:
                found[mid] = c
        for sf in (safe(lambda: folder.GetSubFolderList()) or []):
            search(sf)

    root = pool.GetRootFolder()
    if root:
        search(root)
    return found


def _item_usage(item):
    props = safe(lambda: item.GetClipProperty()) or {}
    if not isinstance(props, dict):
        return None
    try:
        return int(str(props.get("Usage", "0")).strip() or "0")
    except (TypeError, ValueError):
        return None


def action_move_media_pool_clips_by_id(body):
    """Move clips addressed by mediaId. Verifies each landed in the target folder."""
    _, proj, err = _project()
    if err:
        return err
    pool = proj.GetMediaPool()
    if not pool:
        return {"error": "No media pool"}
    ids = body.get("mediaIds", [])
    target_path = body.get("targetFolder", "")
    if not ids:
        return {"error": "mediaIds array is required"}
    if not target_path:
        return {"error": "targetFolder path is required"}
    target = _find_folder_by_path(pool, target_path)
    if not target:
        return {"error": "Target folder not found: '%s'" % target_path}

    found = _pool_items_by_ids(pool, ids)
    missing = [i for i in ids if i not in found]
    if not found:
        return {"error": "No clips matched those mediaIds", "missing": missing, "moved": 0}

    ok = pool.MoveClips(list(found.values()), target)

    # Verify at the destination: re-read the target folder and confirm arrival.
    landed = set()
    for c in (safe(lambda: target.GetClipList()) or []):
        mid = safe(lambda c=c: c.GetMediaId())
        if mid in found:
            landed.add(mid)
    not_landed = [i for i in found if i not in landed]
    return {
        "success": bool(ok) and not not_landed,
        "moved": len(landed),
        "requested": len(ids),
        "notLanded": not_landed,
        "missing": missing,
        "apiReturned": bool(ok),
    }


def action_delete_media_pool_clips_by_id(body):
    """Delete clips addressed by mediaId. REFUSES anything Resolve reports as in use.

    Usage is Resolve's own count of timeline references. The guard is the whole
    point of this verb: a name-addressed delete in a pool with repeated names
    will eventually remove the copy an edit depends on. Pass force=true only
    with a deliberate reason.
    """
    _, proj, err = _project()
    if err:
        return err
    pool = proj.GetMediaPool()
    if not pool:
        return {"error": "No media pool"}
    ids = body.get("mediaIds", [])
    force = bool(body.get("force", False))
    if not ids:
        return {"error": "mediaIds array is required"}

    found = _pool_items_by_ids(pool, ids)
    missing = [i for i in ids if i not in found]
    if not found:
        return {"error": "No clips matched those mediaIds", "missing": missing, "deleted": 0}

    in_use = []
    for mid, item in found.items():
        u = _item_usage(item)
        if u is None or u > 0:
            in_use.append({
                "mediaId": mid,
                "name": safe(lambda item=item: item.GetName()),
                "usage": u,
            })
    if in_use and not force:
        return {
            "error": "Refusing to delete: %d of %d clips are reported in use by a "
                     "timeline (or their usage could not be read). Nothing was "
                     "deleted. Pass force=true only if you mean it." % (len(in_use), len(found)),
            "inUse": in_use,
            "deleted": 0,
        }

    targets = list(found.values())
    ok = pool.DeleteClips(targets)

    # Verify at the destination: the ids must no longer resolve anywhere.
    still = _pool_items_by_ids(pool, list(found.keys()))
    return {
        "success": bool(ok) and not still,
        "deleted": len(found) - len(still),
        "requested": len(ids),
        "stillPresent": list(still.keys()),
        "missing": missing,
        "forced": force,
        "apiReturned": bool(ok),
    }


def action_relink_media_pool_clips(body):
    """Relink pool clips to files in a folder, and report what ACTUALLY relinked.

    The previous version returned {"success": True, "relinked": len(items)} where
    len(items) was the number of clips *looked up*, not the number relinked --- and
    Resolve's RelinkClips() returns True even when the target folder contains none
    of the files. So it reported 26 relinked against a folder holding zero matches.
    Every relink in a whole session was reported as landing; most had not.

    This version refuses to guess:
      * names matching more than one pool item are AMBIGUOUS -> fail loud, because
        _find_pool_item silently takes the first hit and it is routinely not the
        one the timeline references (duplicate filenames across RUSHES/TRASH/etc).
      * if the folder contains no file matching any requested clip, that is an
        error, not a success.
      * the returned count is verified AFTER the fact by re-reading each item's
        File Path and confirming it now points at a file that exists on disk.
    """
    _, proj, err = _project()
    if err:
        return err
    pool = proj.GetMediaPool()
    if not pool:
        return {"error": "No media pool"}
    clip_names = body.get("clipNames", [])
    folder_path = body.get("folderPath", "")
    if not clip_names:
        return {"error": "clipNames array is required"}
    if not folder_path:
        return {"error": "folderPath is required (filesystem path)"}
    if not os.path.isdir(folder_path):
        return {"error": "folderPath is not a directory: '%s'" % folder_path}

    # Resolve every name, keeping ambiguity visible instead of taking the first hit.
    items, not_found, ambiguous = [], [], []
    for name in clip_names:
        matches = _find_pool_items_all(pool, name)
        if not matches:
            not_found.append(name)
        elif len(matches) > 1:
            ambiguous.append({"name": name, "count": len(matches)})
        else:
            items.append((name, matches[0]))

    if ambiguous:
        return {
            "error": "Ambiguous clip name(s) --- more than one media pool item shares "
                     "each of these names, so a name-based relink would pick an "
                     "arbitrary one. Relink these from the timeline clip via 'Find in "
                     "Media Pool' in the UI, or de-duplicate the pool first.",
            "ambiguous": ambiguous,
            "notFound": not_found,
            "relinked": 0,
        }
    if not items:
        return {"error": "No matching clips found", "notFound": not_found, "relinked": 0}

    # Decisive pre-check: does the folder actually hold any of these files? This is
    # the exact case the old version reported as a success.
    on_disk = _folder_basenames(folder_path)
    wanted = [n for n, _ in items]
    matchable = [n for n in wanted if n.lower() in on_disk]
    if not matchable:
        return {
            "error": "Folder '%s' contains none of the requested clips --- nothing to "
                     "relink. (%d file(s) scanned.)" % (folder_path, len(on_disk)),
            "requested": wanted,
            "notFound": not_found,
            "relinked": 0,
        }

    before = {n: _clip_file_path(it) for n, it in items}
    ok = pool.RelinkClips([it for _n, it in items], folder_path)

    # Verify at the destination: re-read each path and confirm it exists on disk.
    relinked, still_broken, unchanged = [], [], []
    for name, item in items:
        now = _clip_file_path(item)
        if now and os.path.isfile(now):
            if now != before.get(name):
                relinked.append(name)
            else:
                unchanged.append(name)      # already pointed at a real file
        else:
            still_broken.append(name)

    return {
        "success": bool(ok) and not still_broken,
        "relinked": len(relinked),
        "relinkedClips": relinked,
        "alreadyLinked": unchanged,
        "stillBroken": still_broken,
        "notFound": not_found,
        "requested": len(clip_names),
        "apiReturned": bool(ok),
    }


def action_unlink_media_pool_clips(body):
    _, proj, err = _project()
    if err:
        return err
    pool = proj.GetMediaPool()
    if not pool:
        return {"error": "No media pool"}
    clip_names = body.get("clipNames", [])
    if not clip_names:
        return {"error": "clipNames array is required"}
    items = [i for name in clip_names for i in [_find_pool_item(pool, name)] if i]
    if not items:
        return {"error": "No matching clips found"}
    ok = pool.UnlinkClips(items)
    return {"success": bool(ok), "unlinked": len(items)}


def action_auto_sync_audio(body):
    _, proj, err = _project()
    if err:
        return err
    pool = proj.GetMediaPool()
    if not pool:
        return {"error": "No media pool"}
    clip_names = body.get("clipNames", [])
    if len(clip_names) < 2:
        return {"error": "At least 2 clipNames required (video + audio)"}
    items = [i for name in clip_names for i in [_find_pool_item(pool, name)] if i]
    if len(items) < 2:
        return {"error": "Need at least 2 matching clips"}
    settings = body.get("settings", {})
    ok = pool.AutoSyncAudio(items, settings)
    return {"success": bool(ok)}


def action_import_timeline_from_file(body):
    _, proj, err = _project()
    if err:
        return err
    pool = proj.GetMediaPool()
    if not pool:
        return {"error": "No media pool"}
    file_path = body.get("filePath", "")
    if not file_path:
        return {"error": "filePath is required"}
    options = body.get("importOptions", {})
    tl = pool.ImportTimelineFromFile(file_path, options) if options else pool.ImportTimelineFromFile(file_path)
    if not tl:
        return {"error": "Failed to import timeline from '%s'" % file_path}
    return {"success": True, "timeline": safe(lambda: tl.GetName())}


def action_export_metadata(body):
    _, proj, err = _project()
    if err:
        return err
    pool = proj.GetMediaPool()
    if not pool:
        return {"error": "No media pool"}
    file_name = body.get("fileName", "")
    if not file_name:
        return {"error": "fileName is required (CSV path)"}
    clip_names = body.get("clipNames", [])
    items = [i for name in clip_names for i in [_find_pool_item(pool, name)] if i] if clip_names else []
    ok = pool.ExportMetadata(file_name, items) if items else pool.ExportMetadata(file_name)
    return {"success": bool(ok), "fileName": file_name}


def action_insert_to_timeline(body):
    """Insert a media pool clip at a specific track and record frame position."""
    _, proj, err = _project()
    if err:
        return err
    pool = proj.GetMediaPool()
    if not pool:
        return {"error": "No media pool"}
    clip_name = body.get("clipName", "")
    if not clip_name:
        return {"error": "clipName is required"}
    item = _find_pool_item(pool, clip_name)
    if not item:
        return {"error": "Clip '%s' not found in media pool" % clip_name}
    clip_info = {"mediaPoolItem": item}
    for key, body_key in [("trackIndex", "trackIndex"), ("recordFrame", "recordFrame"),
                          ("startFrame", "startFrame"), ("endFrame", "endFrame"),
                          ("mediaType", "mediaType")]:
        val = body.get(body_key)
        if val is not None:
            clip_info[key] = int(val)
    tl_items = pool.AppendToTimeline([clip_info])
    if not tl_items:
        return {"error": "Failed to insert clip to timeline"}
    return {"success": True, "inserted": clip_name, "count": len(tl_items)}


# -- Per-Clip Markers & Flags ----------------------------------------------

def action_delete_clip_marker(body):
    item, err = _clip_at(body)
    if err:
        return err
    frame = body.get("frameId")
    color = body.get("color")
    custom_data = body.get("customData")
    if frame is not None:
        return {"success": bool(item.DeleteMarkerAtFrame(int(frame)))}
    if color:
        return {"success": bool(item.DeleteMarkersByColor(color))}
    if custom_data:
        return {"success": bool(item.DeleteMarkerByCustomData(custom_data))}
    return {"error": "Provide frameId, color (use 'All' for all), or customData"}


def action_add_clip_flag(body):
    item, err = _clip_at(body)
    if err:
        return err
    color = body.get("color", "")
    if not color:
        return {"error": "color is required"}
    return {"success": bool(item.AddFlag(color)), "color": color}


def action_clear_clip_flags(body):
    item, err = _clip_at(body)
    if err:
        return err
    color = body.get("color", "All")
    return {"success": bool(item.ClearFlags(color)), "color": color}


# -- Timeline Clip Manipulation ---------------------------------------------

def action_delete_timeline_clips(body):
    _, _, tl, err = _timeline()
    if err:
        return err
    clip_refs = body.get("clips", [])
    ripple = body.get("ripple", False)
    if not clip_refs:
        return {"error": "clips array is required (each: {trackType, trackIndex, clipIndex})"}
    items = _resolve_clip_refs(tl, clip_refs)
    if not items:
        return {"error": "No matching clips found"}
    ok = tl.DeleteClips(items, ripple)
    return {"success": bool(ok), "deleted": len(items), "ripple": ripple}


def action_link_timeline_clips(body):
    _, _, tl, err = _timeline()
    if err:
        return err
    clip_refs = body.get("clips", [])
    linked = body.get("linked", True)
    if len(clip_refs) < 2:
        return {"error": "At least 2 clip references required"}
    items = _resolve_clip_refs(tl, clip_refs)
    if len(items) < 2:
        return {"error": "Need at least 2 matching clips"}
    ok = tl.SetClipsLinked(items, linked)
    return {"success": bool(ok), "linked": linked, "count": len(items)}


def action_create_compound_clip(body):
    _, _, tl, err = _timeline()
    if err:
        return err
    clip_refs = body.get("clips", [])
    if not clip_refs:
        return {"error": "clips array is required"}
    items = _resolve_clip_refs(tl, clip_refs)
    if not items:
        return {"error": "No matching clips found"}
    clip_info = {}
    if body.get("name"):
        clip_info["name"] = body["name"]
    if body.get("startTimecode"):
        clip_info["startTimecode"] = body["startTimecode"]
    result = tl.CreateCompoundClip(items, clip_info) if clip_info else tl.CreateCompoundClip(items)
    if not result:
        return {"error": "Failed to create compound clip"}
    return {"success": True, "name": safe(lambda: result.GetName())}


def action_create_fusion_clip(body):
    _, _, tl, err = _timeline()
    if err:
        return err
    clip_refs = body.get("clips", [])
    if not clip_refs:
        return {"error": "clips array is required"}
    items = _resolve_clip_refs(tl, clip_refs)
    if not items:
        return {"error": "No matching clips found"}
    result = tl.CreateFusionClip(items)
    if not result:
        return {"error": "Failed to create Fusion clip"}
    return {"success": True, "name": safe(lambda: result.GetName())}


def action_export_timeline(body):
    """Export timeline to AAF/EDL/FCPXML/OTIO etc."""
    r, _, tl, err = _timeline()
    if err:
        return err
    file_name = body.get("fileName", "")
    export_type = body.get("exportType", "")
    export_subtype = body.get("exportSubtype", "EXPORT_NONE")
    if not file_name:
        return {"error": "fileName is required"}
    if not export_type:
        return {"error": "exportType is required"}
    type_map = {
        "AAF": "EXPORT_AAF", "DRT": "EXPORT_DRT", "EDL": "EXPORT_EDL",
        "FCP_7_XML": "EXPORT_FCP_7_XML",
        "FCPXML_1_8": "EXPORT_FCPXML_1_8", "FCPXML_1_9": "EXPORT_FCPXML_1_9",
        "FCPXML_1_10": "EXPORT_FCPXML_1_10",
        "HDR_10_PROFILE_A": "EXPORT_HDR_10_PROFILE_A",
        "HDR_10_PROFILE_B": "EXPORT_HDR_10_PROFILE_B",
        "TEXT_CSV": "EXPORT_TEXT_CSV", "TEXT_TAB": "EXPORT_TEXT_TAB",
        "DOLBY_VISION_VER_2_9": "EXPORT_DOLBY_VISION_VER_2_9",
        "DOLBY_VISION_VER_4_0": "EXPORT_DOLBY_VISION_VER_4_0",
        "DOLBY_VISION_VER_5_1": "EXPORT_DOLBY_VISION_VER_5_1",
        "OTIO": "EXPORT_OTIO", "ALE": "EXPORT_ALE", "ALE_CDL": "EXPORT_ALE_CDL",
    }
    subtype_map = {
        "EXPORT_NONE": "EXPORT_NONE", "EXPORT_AAF_NEW": "EXPORT_AAF_NEW",
        "EXPORT_AAF_EXISTING": "EXPORT_AAF_EXISTING",
        "EXPORT_CDL": "EXPORT_CDL", "EXPORT_SDL": "EXPORT_SDL",
        "EXPORT_MISSING_CLIPS": "EXPORT_MISSING_CLIPS",
    }
    et_attr = type_map.get(export_type)
    if not et_attr:
        return {"error": "Unknown exportType '%s'. Valid: %s" % (export_type, ", ".join(type_map.keys()))}
    es_attr = subtype_map.get(export_subtype, "EXPORT_NONE")
    et = safe(lambda: getattr(r, et_attr))
    es = safe(lambda: getattr(r, es_attr))
    if et is None:
        return {"error": "Resolve does not support export constant '%s'" % et_attr}
    ok = tl.Export(file_name, et, es)
    return {"success": bool(ok), "fileName": file_name, "exportType": export_type}


# -- Gallery / Stills -------------------------------------------------------

def action_set_current_album(body):
    _, proj, err = _project()
    if err:
        return err
    gallery = safe(lambda: proj.GetGallery())
    if not gallery:
        return {"error": "Cannot access gallery"}
    album, aerr = _get_album(gallery, body)
    if aerr:
        return aerr
    ok = gallery.SetCurrentStillAlbum(album)
    name = safe(lambda: gallery.GetAlbumName(album))
    return {"success": bool(ok), "albumName": name}


def action_create_gallery_album(body):
    _, proj, err = _project()
    if err:
        return err
    gallery = safe(lambda: proj.GetGallery())
    if not gallery:
        return {"error": "Cannot access gallery"}
    album_type = body.get("albumType", "still")
    if album_type == "powergrade":
        album = gallery.CreateGalleryPowerGradeAlbum()
    else:
        album = gallery.CreateGalleryStillAlbum()
    if not album:
        return {"error": "Failed to create %s album" % album_type}
    name = body.get("name", "")
    if name:
        gallery.SetAlbumName(album, name)
    final_name = safe(lambda: gallery.GetAlbumName(album))
    return {"success": True, "albumName": final_name, "albumType": album_type}


def action_grab_still(body):
    _, _, tl, err = _timeline()
    if err:
        return err
    still = tl.GrabStill()
    if not still:
        return {"error": "Failed to grab still. Make sure you are on the Color page."}
    return {"success": True}


def action_grab_all_stills(body):
    _, _, tl, err = _timeline()
    if err:
        return err
    source = int(body.get("stillFrameSource", 2))
    stills = tl.GrabAllStills(source)
    if not stills:
        return {"error": "Failed to grab stills"}
    return {"success": True, "count": len(stills)}


def action_export_stills(body):
    _, proj, err = _project()
    if err:
        return err
    gallery = safe(lambda: proj.GetGallery())
    if not gallery:
        return {"error": "Cannot access gallery"}
    album, aerr = _get_album(gallery, body)
    if aerr:
        return aerr
    folder_path = body.get("folderPath", "")
    file_prefix = body.get("filePrefix", "still")
    fmt = body.get("format", "png")
    if not folder_path:
        return {"error": "folderPath is required"}
    stills = safe(lambda: album.GetStills()) or []
    still_indices = body.get("stillIndices", [])
    if still_indices:
        selected = [stills[i - 1] for i in still_indices if 1 <= i <= len(stills)]
    else:
        selected = stills
    if not selected:
        return {"error": "No stills to export"}
    ok = album.ExportStills(selected, folder_path, file_prefix, fmt)
    return {"success": bool(ok), "exported": len(selected), "folderPath": folder_path}


def action_import_stills(body):
    _, proj, err = _project()
    if err:
        return err
    gallery = safe(lambda: proj.GetGallery())
    if not gallery:
        return {"error": "Cannot access gallery"}
    album = safe(lambda: gallery.GetCurrentStillAlbum())
    if not album:
        return {"error": "No album selected"}
    file_paths = body.get("filePaths", [])
    if not file_paths:
        return {"error": "filePaths array is required"}
    ok = album.ImportStills(file_paths)
    return {"success": bool(ok)}


def action_delete_stills(body):
    _, proj, err = _project()
    if err:
        return err
    gallery = safe(lambda: proj.GetGallery())
    if not gallery:
        return {"error": "Cannot access gallery"}
    album, aerr = _get_album(gallery, body)
    if aerr:
        return aerr
    stills = safe(lambda: album.GetStills()) or []
    still_indices = body.get("stillIndices", [])
    if not still_indices:
        return {"error": "stillIndices array is required"}
    selected = [stills[i - 1] for i in still_indices if 1 <= i <= len(stills)]
    if not selected:
        return {"error": "No stills found at given indices"}
    ok = album.DeleteStills(selected)
    return {"success": bool(ok), "deleted": len(selected)}


def action_set_still_label(body):
    _, proj, err = _project()
    if err:
        return err
    gallery = safe(lambda: proj.GetGallery())
    if not gallery:
        return {"error": "Cannot access gallery"}
    album = safe(lambda: gallery.GetCurrentStillAlbum())
    if not album:
        return {"error": "No album selected"}
    still_index = int(body.get("stillIndex", 0))
    label = body.get("label", "")
    stills = safe(lambda: album.GetStills()) or []
    if still_index < 1 or still_index > len(stills):
        return {"error": "stillIndex %d out of range (1-%d)" % (still_index, len(stills))}
    ok = album.SetLabel(stills[still_index - 1], label)
    return {"success": bool(ok), "label": label}


# -- Color Grading / Node Graph / LUT / CDL --------------------------------

def gather_node_graph(qs):
    """Get the color node graph for a timeline item or the timeline itself."""
    scope = qs.get("scope", ["clip"])[0]
    if scope == "timeline":
        _, _, tl, err = _timeline()
        if err:
            return err
        graph = safe(lambda: tl.GetNodeGraph())
        if not graph:
            return {"error": "Cannot get timeline node graph"}
    else:
        item, err = _clip_at({
            "trackType": qs.get("track_type", ["video"])[0],
            "trackIndex": int(qs.get("track_index", ["1"])[0]),
            "clipIndex": int(qs.get("clip_index", ["0"])[0]),
        })
        if err:
            return err
        layer = int(qs.get("layer_index", ["1"])[0])
        graph = safe(lambda: item.GetNodeGraph(layer))
        if not graph:
            return {"error": "Cannot get clip node graph"}
    num = safe(lambda: graph.GetNumNodes()) or 0
    nodes = []
    for i in range(1, num + 1):
        nodes.append({
            "index": i,
            "label": safe(lambda i=i: graph.GetNodeLabel(i)) or "",
            "lut": safe(lambda i=i: graph.GetLUT(i)) or "",
            "tools": safe(lambda i=i: graph.GetToolsInNode(i)) or [],
            "cacheMode": safe(lambda i=i: graph.GetNodeCacheMode(i)),
        })
    return {"nodeCount": num, "nodes": nodes}


def action_set_lut(body):
    item, err = _clip_at(body)
    if err:
        return err
    layer = int(body.get("layerIndex", 1))
    graph = safe(lambda: item.GetNodeGraph(layer))
    if not graph:
        return {"error": "Cannot get node graph"}
    node_index = int(body.get("nodeIndex", 1))
    lut_path = body.get("lutPath", "")
    if not lut_path:
        return {"error": "lutPath is required"}
    ok = graph.SetLUT(node_index, lut_path)
    return {"success": bool(ok), "nodeIndex": node_index, "lutPath": lut_path}


def action_get_lut(body):
    item, err = _clip_at(body)
    if err:
        return err
    layer = int(body.get("layerIndex", 1))
    graph = safe(lambda: item.GetNodeGraph(layer))
    if not graph:
        return {"error": "Cannot get node graph"}
    node_index = int(body.get("nodeIndex", 1))
    lut = safe(lambda: graph.GetLUT(node_index))
    return {"nodeIndex": node_index, "lutPath": lut or ""}


def action_set_node_enabled(body):
    item, err = _clip_at(body)
    if err:
        return err
    layer = int(body.get("layerIndex", 1))
    graph = safe(lambda: item.GetNodeGraph(layer))
    if not graph:
        return {"error": "Cannot get node graph"}
    node_index = int(body.get("nodeIndex", 1))
    enabled = bool(body.get("enabled", True))
    ok = graph.SetNodeEnabled(node_index, enabled)
    return {"success": bool(ok), "nodeIndex": node_index, "enabled": enabled}


def action_apply_grade_from_drx(body):
    item, err = _clip_at(body)
    if err:
        return err
    layer = int(body.get("layerIndex", 1))
    graph = safe(lambda: item.GetNodeGraph(layer))
    if not graph:
        return {"error": "Cannot get node graph"}
    path = body.get("drxPath", "")
    grade_mode = int(body.get("gradeMode", 0))
    if not path:
        return {"error": "drxPath is required"}
    ok = graph.ApplyGradeFromDRX(path, grade_mode)
    return {"success": bool(ok), "drxPath": path, "gradeMode": grade_mode}


def action_reset_all_grades(body):
    item, err = _clip_at(body)
    if err:
        return err
    layer = int(body.get("layerIndex", 1))
    graph = safe(lambda: item.GetNodeGraph(layer))
    if not graph:
        return {"error": "Cannot get node graph"}
    ok = graph.ResetAllGrades()
    return {"success": bool(ok)}


def action_apply_arri_cdl_lut(body):
    item, err = _clip_at(body)
    if err:
        return err
    layer = int(body.get("layerIndex", 1))
    graph = safe(lambda: item.GetNodeGraph(layer))
    if not graph:
        return {"error": "Cannot get node graph"}
    ok = graph.ApplyArriCdlLut()
    return {"success": bool(ok)}


def action_set_cdl(body):
    item, err = _clip_at(body)
    if err:
        return err
    cdl_map = body.get("cdl", {})
    if not cdl_map:
        return {"error": "cdl dict is required with keys: NodeIndex, Slope, Offset, Power, Saturation"}
    ok = item.SetCDL(cdl_map)
    return {"success": bool(ok)}


def action_export_lut(body):
    r, _ = _resolve()
    item, err = _clip_at(body)
    if err:
        return err
    export_type_str = body.get("exportType", "33PTCUBE")
    path = body.get("path", "")
    if not path:
        return {"error": "path is required"}
    type_map = {
        "17PTCUBE": "EXPORT_LUT_17PTCUBE",
        "33PTCUBE": "EXPORT_LUT_33PTCUBE",
        "65PTCUBE": "EXPORT_LUT_65PTCUBE",
        "PANASONICVLUT": "EXPORT_LUT_PANASONICVLUT",
    }
    attr = type_map.get(export_type_str)
    if not attr:
        return {"error": "Unknown exportType. Valid: %s" % ", ".join(type_map.keys())}
    et = safe(lambda: getattr(r, attr))
    if et is None:
        return {"error": "Resolve does not support %s" % attr}
    ok = item.ExportLUT(et, path)
    return {"success": bool(ok), "path": path}


def action_copy_grades(body):
    _, _, tl, err = _timeline()
    if err:
        return err
    source_ref = body.get("source", {})
    target_refs = body.get("targets", [])
    if not source_ref or not target_refs:
        return {"error": "source and targets are required"}
    source_item, serr = _clip_at(source_ref)
    if serr:
        return serr
    target_items = _resolve_clip_refs(tl, target_refs)
    if not target_items:
        return {"error": "No matching target clips found"}
    ok = source_item.CopyGrades(target_items)
    return {"success": bool(ok), "copiedTo": len(target_items)}


def action_reset_node_colors(body):
    item, err = _clip_at(body)
    if err:
        return err
    ok = item.ResetAllNodeColors()
    return {"success": bool(ok)}


# -- Color Versions ---------------------------------------------------------

def gather_color_versions(qs):
    item, err = _clip_at({
        "trackType": qs.get("track_type", ["video"])[0],
        "trackIndex": int(qs.get("track_index", ["1"])[0]),
        "clipIndex": int(qs.get("clip_index", ["0"])[0]),
    })
    if err:
        return err
    current = safe(lambda: item.GetCurrentVersion()) or {}
    local_versions = safe(lambda: item.GetVersionNameList(0)) or []
    remote_versions = safe(lambda: item.GetVersionNameList(1)) or []
    return {
        "clipName": safe(lambda: item.GetName()),
        "currentVersion": current,
        "localVersions": local_versions,
        "remoteVersions": remote_versions,
    }


def action_add_color_version(body):
    item, err = _clip_at(body)
    if err:
        return err
    name = body.get("versionName", "")
    vtype = int(body.get("versionType", 0))
    if not name:
        return {"error": "versionName is required"}
    ok = item.AddVersion(name, vtype)
    return {"success": bool(ok), "versionName": name, "versionType": vtype}


def action_load_color_version(body):
    item, err = _clip_at(body)
    if err:
        return err
    name = body.get("versionName", "")
    vtype = int(body.get("versionType", 0))
    if not name:
        return {"error": "versionName is required"}
    ok = item.LoadVersionByName(name, vtype)
    return {"success": bool(ok), "versionName": name}


def action_delete_color_version(body):
    item, err = _clip_at(body)
    if err:
        return err
    name = body.get("versionName", "")
    vtype = int(body.get("versionType", 0))
    if not name:
        return {"error": "versionName is required"}
    ok = item.DeleteVersionByName(name, vtype)
    return {"success": bool(ok), "versionName": name}


def action_rename_color_version(body):
    item, err = _clip_at(body)
    if err:
        return err
    old_name = body.get("oldName", "")
    new_name = body.get("newName", "")
    vtype = int(body.get("versionType", 0))
    if not old_name or not new_name:
        return {"error": "oldName and newName are required"}
    ok = item.RenameVersionByName(old_name, new_name, vtype)
    return {"success": bool(ok)}


# -- Color Groups -----------------------------------------------------------

def gather_color_groups(qs):
    _, proj, err = _project()
    if err:
        return err
    groups = safe(lambda: proj.GetColorGroupsList()) or []
    result = []
    for i, g in enumerate(groups):
        name = safe(lambda g=g: g.GetName())
        result.append({"index": i + 1, "name": name})
    return {"colorGroups": result}


def action_add_color_group(body):
    _, proj, err = _project()
    if err:
        return err
    name = body.get("groupName", "")
    if not name:
        return {"error": "groupName is required"}
    group = proj.AddColorGroup(name)
    if not group:
        return {"error": "Failed to create color group '%s'" % name}
    return {"success": True, "groupName": name}


def action_delete_color_group(body):
    _, proj, err = _project()
    if err:
        return err
    name = body.get("groupName", "")
    if not name:
        return {"error": "groupName is required"}
    groups = safe(lambda: proj.GetColorGroupsList()) or []
    target = None
    for g in groups:
        if safe(lambda g=g: g.GetName()) == name:
            target = g
            break
    if not target:
        return {"error": "Color group '%s' not found" % name}
    ok = proj.DeleteColorGroup(target)
    return {"success": bool(ok)}


def action_assign_to_color_group(body):
    _, proj, err = _project()
    if err:
        return err
    item, ierr = _clip_at(body)
    if ierr:
        return ierr
    group_name = body.get("groupName", "")
    if not group_name:
        return {"error": "groupName is required"}
    groups = safe(lambda: proj.GetColorGroupsList()) or []
    target = None
    for g in groups:
        if safe(lambda g=g: g.GetName()) == group_name:
            target = g
            break
    if not target:
        return {"error": "Color group '%s' not found" % group_name}
    ok = item.AssignToColorGroup(target)
    return {"success": bool(ok), "groupName": group_name}


def action_remove_from_color_group(body):
    item, err = _clip_at(body)
    if err:
        return err
    ok = item.RemoveFromColorGroup()
    return {"success": bool(ok)}


# -- Fusion Composition Management ------------------------------------------

def gather_fusion_comps(qs):
    item, err = _clip_at({
        "trackType": qs.get("track_type", ["video"])[0],
        "trackIndex": int(qs.get("track_index", ["1"])[0]),
        "clipIndex": int(qs.get("clip_index", ["0"])[0]),
    })
    if err:
        return err
    count = safe(lambda: item.GetFusionCompCount()) or 0
    names = safe(lambda: item.GetFusionCompNameList()) or []
    return {"clipName": safe(lambda: item.GetName()), "fusionCompCount": count, "fusionCompNames": names}


def action_add_fusion_comp(body):
    item, err = _clip_at(body)
    if err:
        return err
    comp = item.AddFusionComp()
    if not comp:
        return {"error": "Failed to add Fusion composition"}
    return {"success": True}


def action_clip_fade(body):
    """Fusion-based fade from/to black on a clip via a keyframed BrightnessContrast Gain.
    body: trackType, trackIndex, clipIndex, direction ('in'|'out'), frames (int).
    Native keyframes/transitions aren't in the Resolve API; Fusion is the scriptable path."""
    item, err = _clip_at(body)
    if err:
        return err
    direction = str(body.get("direction", "in")).lower()
    frames = max(1, int(body.get("frames", 30)))
    comp = safe(lambda: item.GetFusionCompByIndex(1)) or safe(lambda: item.AddFusionComp())
    if not comp:
        return {"error": "Could not get or create a Fusion comp on this clip"}
    mi = safe(lambda: comp.FindTool("MediaIn1"))
    mo = safe(lambda: comp.FindTool("MediaOut1"))
    if not mi or not mo:
        return {"error": "MediaIn1/MediaOut1 not found in comp"}
    attrs = safe(lambda: comp.GetAttrs()) or {}
    gstart = int(attrs.get("COMPN_GlobalStart", 0))
    gend = int(attrs.get("COMPN_GlobalEnd", gstart + frames))
    comp.StartUndo("clip fade %s" % direction)
    bc = comp.AddTool("BrightnessContrast")
    bc.Input = mi            # MediaIn -> BC   (attribute-style; ConnectInput no-ops on this build)
    mo.Input = bc            # BC -> MediaOut
    bc.AddModifier("Gain", "BezierSpline")
    if direction == "out":
        f0, v0, f1, v1 = gend - frames + 1, 1.0, gend, 0.0
    else:
        f0, v0, f1, v1 = gstart, 0.0, gstart + frames - 1, 1.0
    bc.SetInput("Gain", v0, float(f0))
    bc.SetInput("Gain", v1, float(f1))
    comp.EndUndo(True)
    fm = (f0 + f1) // 2
    return {
        "success": True,
        "clip": safe(lambda: item.GetName()),
        "direction": direction, "frames": frames,
        "globalStart": gstart, "globalEnd": gend,
        "gainCurve": {
            str(f0): safe(lambda: bc.GetInput("Gain", float(f0))),
            str(fm): safe(lambda: bc.GetInput("Gain", float(fm))),
            str(f1): safe(lambda: bc.GetInput("Gain", float(f1))),
        },
    }


def action_import_fusion_comp(body):
    item, err = _clip_at(body)
    if err:
        return err
    path = body.get("path", "")
    if not path:
        return {"error": "path is required (.comp file)"}
    comp = item.ImportFusionComp(path)
    if not comp:
        return {"error": "Failed to import Fusion composition from '%s'" % path}
    return {"success": True, "path": path}


def action_export_fusion_comp(body):
    item, err = _clip_at(body)
    if err:
        return err
    path = body.get("path", "")
    comp_index = int(body.get("compIndex", 1))
    if not path:
        return {"error": "path is required"}
    ok = item.ExportFusionComp(path, comp_index)
    return {"success": bool(ok), "path": path, "compIndex": comp_index}


def action_delete_fusion_comp(body):
    item, err = _clip_at(body)
    if err:
        return err
    comp_name = body.get("compName", "")
    if not comp_name:
        return {"error": "compName is required"}
    ok = item.DeleteFusionCompByName(comp_name)
    return {"success": bool(ok), "compName": comp_name}


def action_load_fusion_comp(body):
    item, err = _clip_at(body)
    if err:
        return err
    comp_name = body.get("compName", "")
    if not comp_name:
        return {"error": "compName is required"}
    comp = item.LoadFusionCompByName(comp_name)
    if not comp:
        return {"error": "Failed to load Fusion composition '%s'" % comp_name}
    return {"success": True, "compName": comp_name}


def action_rename_fusion_comp(body):
    item, err = _clip_at(body)
    if err:
        return err
    old_name = body.get("oldName", "")
    new_name = body.get("newName", "")
    if not old_name or not new_name:
        return {"error": "oldName and newName are required"}
    ok = item.RenameFusionCompByName(old_name, new_name)
    return {"success": bool(ok)}


# -- Smart Features (Studio) ------------------------------------------------

def action_create_magic_mask(body):
    item, err = _clip_at(body)
    if err:
        return err
    mode = body.get("mode", "F")
    ok = item.CreateMagicMask(mode)
    return {"success": bool(ok), "mode": mode}


def action_regenerate_magic_mask(body):
    item, err = _clip_at(body)
    if err:
        return err
    ok = item.RegenerateMagicMask()
    return {"success": bool(ok)}


def action_stabilize(body):
    item, err = _clip_at(body)
    if err:
        return err
    ok = item.Stabilize()
    return {"success": bool(ok)}


def action_smart_reframe(body):
    item, err = _clip_at(body)
    if err:
        return err
    ok = item.SmartReframe()
    return {"success": bool(ok)}


# -- Audio / Fairlight -------------------------------------------------------

def gather_fairlight_presets(qs):
    r, err = _resolve()
    if err:
        return err
    presets = safe(lambda: r.GetFairlightPresets()) or []
    return {"presets": presets}


def action_apply_fairlight_preset(body):
    _, proj, err = _project()
    if err:
        return err
    name = body.get("presetName", "")
    if not name:
        return {"error": "presetName is required"}
    ok = proj.ApplyFairlightPresetToCurrentTimeline(name)
    return {"success": bool(ok), "presetName": name}


def action_insert_audio_at_playhead(body):
    _, proj, err = _project()
    if err:
        return err
    media_path = body.get("mediaPath", "")
    start_offset = int(body.get("startOffsetInSamples", 0))
    duration = int(body.get("durationInSamples", 0))
    if not media_path:
        return {"error": "mediaPath is required"}
    ok = proj.InsertAudioToCurrentTrackAtPlayhead(media_path, start_offset, duration)
    return {"success": bool(ok)}


def gather_voice_isolation_state(qs):
    scope = qs.get("scope", ["clip"])[0]
    if scope == "track":
        _, _, tl, err = _timeline()
        if err:
            return err
        ti = int(qs.get("track_index", ["1"])[0])
        state = safe(lambda: tl.GetVoiceIsolationState(ti))
        return {"scope": "track", "trackIndex": ti, "state": state or {}}
    else:
        item, err = _clip_at({
            "trackType": qs.get("track_type", ["video"])[0],
            "trackIndex": int(qs.get("track_index", ["1"])[0]),
            "clipIndex": int(qs.get("clip_index", ["0"])[0]),
        })
        if err:
            return err
        state = safe(lambda: item.GetVoiceIsolationState())
        return {"scope": "clip", "clipName": safe(lambda: item.GetName()), "state": state or {}}


def action_set_voice_isolation_state(body):
    scope = body.get("scope", "clip")
    state = body.get("state", {})
    if not state:
        return {"error": "state dict required: {isEnabled: bool, amount: int (0-100)}"}
    if scope == "track":
        _, _, tl, err = _timeline()
        if err:
            return err
        ti = int(body.get("trackIndex", 1))
        ok = tl.SetVoiceIsolationState(ti, state)
        return {"success": bool(ok), "scope": "track", "trackIndex": ti}
    else:
        item, err = _clip_at(body)
        if err:
            return err
        ok = item.SetVoiceIsolationState(state)
        return {"success": bool(ok), "scope": "clip"}


# -- Take Selector -----------------------------------------------------------

def gather_takes(qs):
    item, err = _clip_at({
        "trackType": qs.get("track_type", ["video"])[0],
        "trackIndex": int(qs.get("track_index", ["1"])[0]),
        "clipIndex": int(qs.get("clip_index", ["0"])[0]),
    })
    if err:
        return err
    count = safe(lambda: item.GetTakesCount()) or 0
    selected = safe(lambda: item.GetSelectedTakeIndex()) or 0
    takes = []
    for i in range(1, count + 1):
        info = safe(lambda i=i: item.GetTakeByIndex(i)) or {}
        takes.append({"index": i, "startFrame": info.get("startFrame"), "endFrame": info.get("endFrame")})
    return {"clipName": safe(lambda: item.GetName()), "takesCount": count, "selectedTakeIndex": selected, "takes": takes}


def action_add_take(body):
    _, proj, err = _project()
    if err:
        return err
    pool = proj.GetMediaPool()
    if not pool:
        return {"error": "No media pool"}
    item, ierr = _clip_at(body)
    if ierr:
        return ierr
    mp_clip_name = body.get("mediaPoolClipName", "")
    if not mp_clip_name:
        return {"error": "mediaPoolClipName is required"}
    mp_item = _find_pool_item(pool, mp_clip_name)
    if not mp_item:
        return {"error": "Clip '%s' not found in media pool" % mp_clip_name}
    start = body.get("startFrame")
    end = body.get("endFrame")
    if start is not None and end is not None:
        ok = item.AddTake(mp_item, int(start), int(end))
    else:
        ok = item.AddTake(mp_item)
    return {"success": bool(ok)}


def action_select_take(body):
    item, err = _clip_at(body)
    if err:
        return err
    idx = int(body.get("takeIndex", 1))
    ok = item.SelectTakeByIndex(idx)
    return {"success": bool(ok), "takeIndex": idx}


def action_delete_take(body):
    item, err = _clip_at(body)
    if err:
        return err
    idx = int(body.get("takeIndex", 1))
    ok = item.DeleteTakeByIndex(idx)
    return {"success": bool(ok), "takeIndex": idx}


def action_finalize_take(body):
    item, err = _clip_at(body)
    if err:
        return err
    ok = item.FinalizeTake()
    return {"success": bool(ok)}


# -- Proxy / Cache / Misc Clip Ops ------------------------------------------

def action_link_proxy_media(body):
    _, proj, err = _project()
    if err:
        return err
    pool = proj.GetMediaPool()
    if not pool:
        return {"error": "No media pool"}
    clip_name = body.get("clipName", "")
    proxy_path = body.get("proxyMediaFilePath", "")
    if not clip_name or not proxy_path:
        return {"error": "clipName and proxyMediaFilePath are required"}
    item = _find_pool_item(pool, clip_name)
    if not item:
        return {"error": "Clip '%s' not found" % clip_name}
    ok = item.LinkProxyMedia(proxy_path)
    return {"success": bool(ok)}


def action_unlink_proxy_media(body):
    _, proj, err = _project()
    if err:
        return err
    pool = proj.GetMediaPool()
    if not pool:
        return {"error": "No media pool"}
    clip_name = body.get("clipName", "")
    if not clip_name:
        return {"error": "clipName is required"}
    item = _find_pool_item(pool, clip_name)
    if not item:
        return {"error": "Clip '%s' not found" % clip_name}
    ok = item.UnlinkProxyMedia()
    return {"success": bool(ok)}


def action_replace_clip(body):
    _, proj, err = _project()
    if err:
        return err
    pool = proj.GetMediaPool()
    if not pool:
        return {"error": "No media pool"}
    clip_name = body.get("clipName", "")
    file_path = body.get("filePath", "")
    if not clip_name or not file_path:
        return {"error": "clipName and filePath are required"}
    item = _find_pool_item(pool, clip_name)
    if not item:
        return {"error": "Clip '%s' not found" % clip_name}
    ok = item.ReplaceClip(file_path)
    return {"success": bool(ok)}


def action_set_clip_cache(body):
    item, err = _clip_at(body)
    if err:
        return err
    cache_type = body.get("cacheType", "color")
    cache_value = int(body.get("cacheValue", 1))
    if cache_type == "fusion":
        ok = item.SetFusionOutputCache(cache_value)
    else:
        ok = item.SetColorOutputCache(cache_value)
    return {"success": bool(ok), "cacheType": cache_type, "cacheValue": cache_value}


def action_update_sidecar(body):
    item, err = _clip_at(body)
    if err:
        return err
    ok = item.UpdateSidecar()
    return {"success": bool(ok)}


def gather_linked_items(qs):
    item, err = _clip_at({
        "trackType": qs.get("track_type", ["video"])[0],
        "trackIndex": int(qs.get("track_index", ["1"])[0]),
        "clipIndex": int(qs.get("clip_index", ["0"])[0]),
    })
    if err:
        return err
    linked = safe(lambda: item.GetLinkedItems()) or []
    result = []
    for li in linked:
        ti = safe(lambda li=li: li.GetTrackTypeAndIndex()) or []
        result.append({
            "name": safe(lambda li=li: li.GetName()),
            "trackType": ti[0] if len(ti) > 0 else None,
            "trackIndex": ti[1] if len(ti) > 1 else None,
        })
    return {"clipName": safe(lambda: item.GetName()), "linkedItems": result}


def action_set_timeline_mark_in_out(body):
    _, _, tl, err = _timeline()
    if err:
        return err
    mark_in = body.get("markIn")
    mark_out = body.get("markOut")
    mark_type = body.get("type", "all")
    if mark_in is not None and mark_out is not None:
        ok = tl.SetMarkInOut(int(mark_in), int(mark_out), mark_type)
        return {"success": bool(ok)}
    return {"error": "markIn and markOut are required"}


def action_clear_timeline_mark_in_out(body):
    _, _, tl, err = _timeline()
    if err:
        return err
    mark_type = body.get("type", "all")
    ok = tl.ClearMarkInOut(mark_type)
    return {"success": bool(ok)}


# -- Project Manager ---------------------------------------------------------

def gather_project_list(qs):
    r, err = _resolve()
    if err:
        return err
    pm = r.GetProjectManager()
    if not pm:
        return {"error": "No project manager"}
    projects = safe(lambda: pm.GetProjectListInCurrentFolder()) or []
    folders = safe(lambda: pm.GetFolderListInCurrentFolder()) or []
    current_folder = safe(lambda: pm.GetCurrentFolder()) or ""
    current_db = safe(lambda: pm.GetCurrentDatabase()) or {}
    return {
        "currentFolder": current_folder,
        "currentDatabase": current_db,
        "projects": projects,
        "folders": folders,
    }


def gather_database_list(qs):
    r, err = _resolve()
    if err:
        return err
    pm = r.GetProjectManager()
    if not pm:
        return {"error": "No project manager"}
    dbs = safe(lambda: pm.GetDatabaseList()) or []
    current = safe(lambda: pm.GetCurrentDatabase()) or {}
    return {"currentDatabase": current, "databases": dbs}


def action_load_project(body):
    r, err = _resolve()
    if err:
        return err
    pm = r.GetProjectManager()
    if not pm:
        return {"error": "No project manager"}
    name = body.get("projectName", "")
    if not name:
        return {"error": "projectName is required"}
    proj = pm.LoadProject(name)
    if not proj:
        return {"error": "Failed to load project '%s'" % name}
    return {"success": True, "projectName": safe(lambda: proj.GetName())}


def action_create_project(body):
    r, err = _resolve()
    if err:
        return err
    pm = r.GetProjectManager()
    if not pm:
        return {"error": "No project manager"}
    name = body.get("projectName", "")
    if not name:
        return {"error": "projectName is required"}
    proj = pm.CreateProject(name)
    if not proj:
        return {"error": "Failed to create project '%s' (name may already exist)" % name}
    return {"success": True, "projectName": safe(lambda: proj.GetName())}


def action_delete_project(body):
    r, err = _resolve()
    if err:
        return err
    pm = r.GetProjectManager()
    name = body.get("projectName", "")
    if not name:
        return {"error": "projectName is required"}
    ok = pm.DeleteProject(name)
    return {"success": bool(ok)}


def action_archive_project(body):
    r, err = _resolve()
    if err:
        return err
    pm = r.GetProjectManager()
    name = body.get("projectName", "")
    file_path = body.get("filePath", "")
    if not name or not file_path:
        return {"error": "projectName and filePath are required"}
    src_media = body.get("archiveSrcMedia", True)
    render_cache = body.get("archiveRenderCache", True)
    proxy_media = body.get("archiveProxyMedia", False)
    ok = pm.ArchiveProject(name, file_path, src_media, render_cache, proxy_media)
    return {"success": bool(ok)}


def action_export_project(body):
    r, err = _resolve()
    if err:
        return err
    pm = r.GetProjectManager()
    name = body.get("projectName", "")
    file_path = body.get("filePath", "")
    if not name or not file_path:
        return {"error": "projectName and filePath are required"}
    with_stills = body.get("withStillsAndLUTs", True)
    ok = pm.ExportProject(name, file_path, with_stills)
    return {"success": bool(ok)}


def action_import_project(body):
    r, err = _resolve()
    if err:
        return err
    pm = r.GetProjectManager()
    file_path = body.get("filePath", "")
    if not file_path:
        return {"error": "filePath is required"}
    project_name = body.get("projectName")
    ok = pm.ImportProject(file_path, project_name) if project_name else pm.ImportProject(file_path)
    return {"success": bool(ok)}


def action_navigate_project_folder(body):
    r, err = _resolve()
    if err:
        return err
    pm = r.GetProjectManager()
    action = body.get("action", "")
    folder_name = body.get("folderName", "")
    if action == "root":
        ok = pm.GotoRootFolder()
    elif action == "parent":
        ok = pm.GotoParentFolder()
    elif action == "open" and folder_name:
        ok = pm.OpenFolder(folder_name)
    elif action == "create" and folder_name:
        ok = pm.CreateFolder(folder_name)
    elif action == "delete" and folder_name:
        ok = pm.DeleteFolder(folder_name)
    else:
        return {"error": "action required: 'root', 'parent', 'open', 'create', or 'delete'"}
    return {"success": bool(ok), "action": action}


def action_set_database(body):
    r, err = _resolve()
    if err:
        return err
    pm = r.GetProjectManager()
    db_info = body.get("dbInfo", {})
    if not db_info:
        return {"error": "dbInfo dict required with keys DbType, DbName, optional IpAddress"}
    ok = pm.SetCurrentDatabase(db_info)
    return {"success": bool(ok)}


# -- Resolve-level / Presets / MediaStorage ----------------------------------

def action_layout_preset(body):
    r, err = _resolve()
    if err:
        return err
    action = body.get("action", "")
    name = body.get("presetName", "")
    path = body.get("presetFilePath", "")
    if action == "load" and name:
        return {"success": bool(r.LoadLayoutPreset(name))}
    elif action == "save" and name:
        return {"success": bool(r.SaveLayoutPreset(name))}
    elif action == "update" and name:
        return {"success": bool(r.UpdateLayoutPreset(name))}
    elif action == "delete" and name:
        return {"success": bool(r.DeleteLayoutPreset(name))}
    elif action == "export" and name and path:
        return {"success": bool(r.ExportLayoutPreset(name, path))}
    elif action == "import" and path:
        return {"success": bool(r.ImportLayoutPreset(path, name))}
    return {"error": "action required: 'load', 'save', 'update', 'delete', 'export', 'import'"}


def action_render_preset(body):
    r, err = _resolve()
    if err:
        return err
    _, proj, perr = _project()
    if perr:
        return perr
    action = body.get("action", "")
    name = body.get("presetName", "")
    path = body.get("presetPath", "")
    if action == "import" and path:
        return {"success": bool(r.ImportRenderPreset(path))}
    elif action == "export" and name and path:
        return {"success": bool(r.ExportRenderPreset(name, path))}
    elif action == "load" and name:
        return {"success": bool(proj.LoadRenderPreset(name))}
    elif action == "saveAs" and name:
        return {"success": bool(proj.SaveAsNewRenderPreset(name))}
    elif action == "delete" and name:
        return {"success": bool(proj.DeleteRenderPreset(name))}
    elif action == "list":
        return {"presets": safe(lambda: proj.GetRenderPresetList()) or []}
    return {"error": "action required: 'load', 'saveAs', 'delete', 'list', 'import', 'export'"}


def action_burnin_preset(body):
    r, err = _resolve()
    if err:
        return err
    action = body.get("action", "")
    name = body.get("presetName", "")
    path = body.get("presetPath", "")
    if action == "import" and path:
        return {"success": bool(r.ImportBurnInPreset(path))}
    elif action == "export" and name and path:
        return {"success": bool(r.ExportBurnInPreset(name, path))}
    elif action == "load" and name:
        _, proj, perr = _project()
        if perr:
            return perr
        return {"success": bool(proj.LoadBurnInPreset(name))}
    return {"error": "action required: 'load', 'import', 'export'"}


def action_set_keyframe_mode(body):
    r, err = _resolve()
    if err:
        return err
    mode = int(body.get("mode", 0))
    ok = r.SetKeyframeMode(mode)
    return {"success": bool(ok), "mode": mode}


def gather_keyframe_mode(qs):
    r, err = _resolve()
    if err:
        return err
    mode = safe(lambda: r.GetKeyframeMode())
    labels = {0: "All", 1: "Color", 2: "Sizing"}
    return {"keyframeMode": mode, "label": labels.get(mode, "Unknown")}


def action_quick_export(body):
    _, proj, err = _project()
    if err:
        return err
    preset_name = body.get("presetName", "")
    if not preset_name:
        presets = safe(lambda: proj.GetQuickExportRenderPresets()) or []
        return {"error": "presetName is required", "availablePresets": presets}
    params = body.get("params", {})
    result = proj.RenderWithQuickExport(preset_name, params)
    return {"result": result}


def gather_quick_export_presets(qs):
    _, proj, err = _project()
    if err:
        return err
    presets = safe(lambda: proj.GetQuickExportRenderPresets()) or []
    return {"presets": presets}


def action_set_render_mode(body):
    _, proj, err = _project()
    if err:
        return err
    mode = int(body.get("renderMode", 0))
    ok = proj.SetCurrentRenderMode(mode)
    return {"success": bool(ok), "renderMode": mode}


def action_get_render_job_status(body):
    _, proj, err = _project()
    if err:
        return err
    job_id = body.get("jobId", "")
    if not job_id:
        return {"error": "jobId is required"}
    status = safe(lambda: proj.GetRenderJobStatus(job_id))
    return {"jobId": job_id, "status": status or {}}


def action_refresh_lut_list(body):
    _, proj, err = _project()
    if err:
        return err
    ok = proj.RefreshLUTList()
    return {"success": bool(ok)}


def gather_render_resolutions(qs):
    _, proj, err = _project()
    if err:
        return err
    fmt = qs.get("format", [""])[0]
    codec = qs.get("codec", [""])[0]
    if fmt and codec:
        res = safe(lambda: proj.GetRenderResolutions(fmt, codec)) or []
    else:
        res = safe(lambda: proj.GetRenderResolutions()) or []
    return {"resolutions": res}


def gather_media_storage(qs):
    r, err = _resolve()
    if err:
        return err
    ms = r.GetMediaStorage()
    if not ms:
        return {"error": "No media storage"}
    volumes = safe(lambda: ms.GetMountedVolumeList()) or []
    folder_path = qs.get("folder_path", [""])[0]
    result = {"volumes": volumes}
    if folder_path:
        subfolders = safe(lambda: ms.GetSubFolderList(folder_path)) or []
        files = safe(lambda: ms.GetFileList(folder_path)) or []
        result["subfolders"] = subfolders
        result["files"] = files
    return result


def action_reveal_in_storage(body):
    r, err = _resolve()
    if err:
        return err
    ms = r.GetMediaStorage()
    if not ms:
        return {"error": "No media storage"}
    path = body.get("path", "")
    if not path:
        return {"error": "path is required"}
    ok = ms.RevealInStorage(path)
    return {"success": bool(ok)}


# ---------------------------------------------------------------------------
# Route tables
# ---------------------------------------------------------------------------

GET_ROUTES = {
    "/status":                  lambda qs: gather_status(),
    "/project":                 lambda qs: gather_project(),
    "/page":                    lambda qs: gather_page(),
    "/timeline":                lambda qs: gather_timeline(),
    "/timeline/clips":          lambda qs: gather_clips(
                                    qs.get("track_type", ["video"])[0],
                                    int(qs.get("track_index", ["1"])[0])),
    "/timeline/markers":        lambda qs: gather_markers(),
    "/timeline/current-item":   lambda qs: gather_current_video_item(qs),
    "/timeline/thumbnail":      lambda qs: gather_clip_thumbnail(qs),
    "/render":                  lambda qs: gather_render(),
    "/render/resolutions":      gather_render_resolutions,
    "/render/quick-export-presets": gather_quick_export_presets,
    "/mediapool":               lambda qs: gather_media_pool(),
    "/mediapool/structure":     gather_media_pool_structure,
    "/mediapool/audit":         gather_media_pool_audit,
    "/mediapool/clip/metadata": gather_clip_metadata,
    "/mediapool/clip/info":     gather_clip_info,
    "/clip/markers":            gather_clip_markers,
    "/clip/flags":              gather_clip_flags,
    "/clip/properties":         gather_clip_properties,
    "/clip/node-graph":         gather_node_graph,
    "/clip/color-versions":     gather_color_versions,
    "/clip/fusion-comps":       gather_fusion_comps,
    "/clip/takes":              gather_takes,
    "/clip/linked-items":       gather_linked_items,
    "/color/groups":            gather_color_groups,
    "/audio/voice-isolation":   gather_voice_isolation_state,
    "/fairlight/presets":       gather_fairlight_presets,
    "/gallery/albums":          gather_gallery_albums,
    "/gallery/stills":          gather_album_stills,
    "/keyframe-mode":           gather_keyframe_mode,
    "/projects":                gather_project_list,
    "/databases":               gather_database_list,
    "/media-storage":           gather_media_storage,
}

def action_shutdown(body):
    """Gracefully stop the HTTP server (used for hot-reload)."""
    import threading
    threading.Thread(target=lambda: server.shutdown(), daemon=True).start()
    return {"success": True, "message": "Shutting down"}


POST_ROUTES = {
    "/bridge/shutdown":             action_shutdown,
    "/page":                        action_open_page,
    "/playhead":                    action_set_timecode,
    # markers
    "/marker/add":                  action_add_marker,
    "/marker/delete":               action_delete_marker,
    # timeline
    "/timeline/switch":             action_switch_timeline,
    "/timeline/create":             action_create_timeline,
    "/timeline/rename":             action_rename_timeline,
    "/timeline/duplicate":          action_duplicate_timeline,
    "/timeline/export":             action_export_timeline,
    "/timeline/mark-in-out":        action_set_timeline_mark_in_out,
    "/timeline/clear-mark-in-out":  action_clear_timeline_mark_in_out,
    # timeline clip manipulation
    "/timeline/clips/delete":       action_delete_timeline_clips,
    "/timeline/clips/link":         action_link_timeline_clips,
    "/timeline/compound-clip":      action_create_compound_clip,
    "/timeline/fusion-clip":        action_create_fusion_clip,
    # tracks
    "/track/add":                   action_add_track,
    "/track/delete":                action_delete_track,
    "/track/enable":                action_set_track_enable,
    "/track/lock":                  action_set_track_lock,
    "/track/rename":                action_set_track_name,
    # media
    "/media/import":                action_import_media,
    "/media/import-storage":        action_import_media_from_storage,
    "/media/append":                action_append_to_timeline,
    "/timeline/place":              action_timeline_place,
    "/timeline/swap":               action_timeline_swap,
    "/timeline/move":               action_timeline_move,
    "/timeline/remove":             action_timeline_remove,
    "/timeline/undo":               action_timeline_undo,
    "/timeline/read":               action_timeline_read,
    "/shortcut/fire":               action_shortcut_fire,
    "/timeline/snapshot":           action_timeline_snapshot,
    "/timeline/restore":            action_timeline_restore,
    "/timeline/merge":              action_timeline_merge,
    "/timeline/ripple-delete":      action_timeline_ripple_delete,
    "/timeline/trim":               action_timeline_trim,
    "/keyboard/rebind":             action_keyboard_rebind,
    "/media/insert":                action_insert_to_timeline,
    # media pool deep access
    "/mediapool/navigate":          action_navigate_media_pool,
    "/mediapool/folder/create":     action_create_media_pool_folder,
    "/mediapool/clip/metadata":     action_set_clip_metadata,
    "/mediapool/clip/property":     action_set_pool_clip_property,
    "/mediapool/clips/delete":      action_delete_media_pool_clips,
    "/mediapool/clips/move":        action_move_media_pool_clips,
    "/mediapool/clips/move_by_id":  action_move_media_pool_clips_by_id,
    "/mediapool/clips/delete_by_id": action_delete_media_pool_clips_by_id,
    "/mediapool/clips/relink":      action_relink_media_pool_clips,
    "/mediapool/clips/unlink":      action_unlink_media_pool_clips,
    "/mediapool/audio-sync":        action_auto_sync_audio,
    "/mediapool/timeline/import":   action_import_timeline_from_file,
    "/mediapool/metadata/export":   action_export_metadata,
    # clip operations
    "/clip/color":                  action_set_clip_color,
    "/clip/enabled":                action_set_clip_enabled,
    "/clip/properties":             action_set_clip_properties,
    "/clip/marker/add":             action_add_clip_marker,
    "/clip/marker/delete":          action_delete_clip_marker,
    "/clip/flag/add":               action_add_clip_flag,
    "/clip/flag/clear":             action_clear_clip_flags,
    "/clip/cache":                  action_set_clip_cache,
    "/clip/sidecar":                action_update_sidecar,
    # color grading / LUT / CDL
    "/color/set-lut":               action_set_lut,
    "/color/get-lut":               action_get_lut,
    "/color/set-node-enabled":      action_set_node_enabled,
    "/color/apply-drx":             action_apply_grade_from_drx,
    "/color/reset-grades":          action_reset_all_grades,
    "/color/apply-arri-cdl":        action_apply_arri_cdl_lut,
    "/color/set-cdl":               action_set_cdl,
    "/color/export-lut":            action_export_lut,
    "/color/copy-grades":           action_copy_grades,
    "/color/reset-node-colors":     action_reset_node_colors,
    # color versions
    "/color/version/add":           action_add_color_version,
    "/color/version/load":          action_load_color_version,
    "/color/version/delete":        action_delete_color_version,
    "/color/version/rename":        action_rename_color_version,
    # color groups
    "/color/group/add":             action_add_color_group,
    "/color/group/delete":          action_delete_color_group,
    "/color/group/assign":          action_assign_to_color_group,
    "/color/group/remove":          action_remove_from_color_group,
    # fusion comps per-clip
    "/clip/fusion/add":             action_add_fusion_comp,
    "/clip/fade":                   action_clip_fade,
    "/clip/fusion/import":          action_import_fusion_comp,
    "/clip/fusion/export":          action_export_fusion_comp,
    "/clip/fusion/delete":          action_delete_fusion_comp,
    "/clip/fusion/load":            action_load_fusion_comp,
    "/clip/fusion/rename":          action_rename_fusion_comp,
    # smart features
    "/clip/magic-mask":             action_create_magic_mask,
    "/clip/magic-mask/regenerate":  action_regenerate_magic_mask,
    "/clip/stabilize":              action_stabilize,
    "/clip/smart-reframe":          action_smart_reframe,
    # audio / fairlight
    "/audio/fairlight-preset":      action_apply_fairlight_preset,
    "/audio/insert-at-playhead":    action_insert_audio_at_playhead,
    "/audio/voice-isolation":       action_set_voice_isolation_state,
    # take selector
    "/clip/take/add":               action_add_take,
    "/clip/take/select":            action_select_take,
    "/clip/take/delete":            action_delete_take,
    "/clip/take/finalize":          action_finalize_take,
    # proxy
    "/mediapool/proxy/link":        action_link_proxy_media,
    "/mediapool/proxy/unlink":      action_unlink_proxy_media,
    "/mediapool/clip/replace":      action_replace_clip,
    # titles & generators
    "/title/insert":                action_insert_title,
    "/generator/insert":            action_insert_generator,
    "/fusion/insert":               action_insert_fusion_comp,
    # render
    "/render/settings":             action_set_render_settings,
    "/render/format":               action_set_render_format,
    "/render/formats":              action_get_render_formats,
    "/render/job/add":              action_add_render_job,
    "/render/job/status":           action_get_render_job_status,
    "/render/start":                action_start_rendering,
    "/render/stop":                 action_stop_rendering,
    "/render/job/delete":           action_delete_render_job,
    "/render/mode":                 action_set_render_mode,
    "/render/quick-export":         action_quick_export,
    "/render/refresh-luts":         action_refresh_lut_list,
    "/render/preset":               action_render_preset,
    # project
    "/project/save":                action_save_project,
    "/project/setting":             action_set_project_setting,
    "/timeline/setting":            action_set_timeline_setting,
    "/project/export-frame":        action_export_frame,
    "/timeline/subtitles":          action_create_subtitles,
    "/timeline/scene-cuts":         action_detect_scene_cuts,
    # project manager
    "/projects/load":               action_load_project,
    "/projects/create":             action_create_project,
    "/projects/delete":             action_delete_project,
    "/projects/archive":            action_archive_project,
    "/projects/export":             action_export_project,
    "/projects/import":             action_import_project,
    "/projects/folder":             action_navigate_project_folder,
    "/projects/database":           action_set_database,
    # resolve-level presets
    "/resolve/layout-preset":       action_layout_preset,
    "/resolve/burnin-preset":       action_burnin_preset,
    "/resolve/keyframe-mode":       action_set_keyframe_mode,
    # media storage
    "/media-storage/reveal":        action_reveal_in_storage,
    # gallery / stills
    "/gallery/album/set":           action_set_current_album,
    "/gallery/album/create":        action_create_gallery_album,
    "/gallery/grab":                action_grab_still,
    "/gallery/grab-all":            action_grab_all_stills,
    "/gallery/stills/export":       action_export_stills,
    "/gallery/stills/import":       action_import_stills,
    "/gallery/stills/delete":       action_delete_stills,
    "/gallery/stills/label":        action_set_still_label,
}


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------

class BridgeHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def _respond(self, data):
        body = json.dumps(data, default=str).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _error(self, code, msg):
        body = json.dumps({"error": msg}).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _blocked(self):
        # Hardening (this fork): anti-CSRF / anti-DNS-rebinding guard.
        # A legitimate local MCP client never sends an Origin header; a browser does.
        if self.headers.get("Origin") is not None:
            return True
        host = (self.headers.get("Host") or "").strip()
        if host not in ("127.0.0.1:%d" % PORT, "localhost:%d" % PORT, ""):
            return True
        return False

    def do_GET(self):
        if self._blocked():
            self._error(403, "Forbidden")
            return
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")
        qs = parse_qs(parsed.query)
        handler = GET_ROUTES.get(path)
        if handler:
            result, err = _call_with_timeout(path, handler, qs)
            if err:
                self._error(504, err["error"])
            else:
                self._respond(result)
        else:
            self._error(404, "Unknown GET endpoint: %s" % path)

    def do_POST(self):
        if self._blocked():
            self._error(403, "Forbidden")
            return
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")
        content_len = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(content_len) if content_len else b"{}"
        try:
            body = json.loads(raw)
        except Exception:
            self._error(400, "Invalid JSON body")
            return
        handler = POST_ROUTES.get(path)
        if handler:
            result, err = _call_with_timeout(path, handler, body)
            if err:
                self._error(504, err["error"])
            else:
                self._respond(result)
        else:
            self._error(404, "Unknown POST endpoint: %s" % path)

    def do_OPTIONS(self):
        # No permissive CORS (hardened): the browser gets no cross-origin grant.
        self.send_response(403)
        self.end_headers()


# ---------------------------------------------------------------------------
# Start server (with hot-reload: shut down any previous instance first)
# ---------------------------------------------------------------------------
import time
try:
    import urllib.request
    req = urllib.request.Request(
        "http://%s:%d/bridge/shutdown" % (HOST, PORT),
        data=b"{}",
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    urllib.request.urlopen(req, timeout=2)
    print("[CursorBridge] Sent shutdown to previous instance, waiting...")
    time.sleep(1.5)
except Exception:
    pass

def _log_startup_crash(exc_text):
    """Persist a startup traceback to disk. The Resolve console window vanishes on
    an unhandled crash, so without this the failure is invisible."""
    try:
        crash_path = os.path.join(_BASE_DIR, "logs", "cursorbridge_startup_error.log")
        os.makedirs(os.path.dirname(crash_path), exist_ok=True)
        with open(crash_path, "a", encoding="utf-8") as f:
            f.write("%s\n%s\n%s\n" % (
                datetime.now().isoformat(timespec="seconds"),
                "Python %s" % sys.version.replace("\n", " "),
                exc_text))
    except Exception:
        pass


print("[CursorBridge] Starting HTTP server on http://%s:%d ..." % (HOST, PORT))
server = None
try:
    server = ThreadingHTTPServer((HOST, PORT), BridgeHandler)
    server.daemon_threads = True  # threads die with the process; no hang-on-exit
    print("[CursorBridge] Bridge is running (read + write, hardened: threaded + %ss call timeout).  %d GET routes, %d POST routes." % (
        RESOLVE_CALL_TIMEOUT, len(GET_ROUTES), len(POST_ROUTES)))
    print("[CursorBridge] To stop: close DaVinci Resolve or re-run this script.")
    server.serve_forever()
except OSError as e:
    if "Address already in use" in str(e) or "Only one usage" in str(e) or getattr(e, "errno", 0) == 10048:
        print("[CursorBridge] Port %d already in use — could not replace old bridge." % PORT)
        print("[CursorBridge] Restart DaVinci Resolve and try again.")
    else:
        print("[CursorBridge] ERROR: %s" % e)
        _log_startup_crash(traceback.format_exc())
except KeyboardInterrupt:
    print("[CursorBridge] Shutting down.")
except Exception as e:
    print("[CursorBridge] ERROR: %s" % e)
    _log_startup_crash(traceback.format_exc())
