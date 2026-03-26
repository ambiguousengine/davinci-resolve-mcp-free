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
import sys
import traceback
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

HOST = "127.0.0.1"
PORT = 9876
BRIDGE_VERSION = "2.0.0"

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


# ---------------------------------------------------------------------------
# Route tables
# ---------------------------------------------------------------------------

GET_ROUTES = {
    "/status":           lambda qs: gather_status(),
    "/project":          lambda qs: gather_project(),
    "/page":             lambda qs: gather_page(),
    "/timeline":         lambda qs: gather_timeline(),
    "/timeline/clips":   lambda qs: gather_clips(
                             qs.get("track_type", ["video"])[0],
                             int(qs.get("track_index", ["1"])[0])),
    "/timeline/markers": lambda qs: gather_markers(),
    "/render":           lambda qs: gather_render(),
    "/mediapool":        lambda qs: gather_media_pool(),
}

def action_shutdown(body):
    """Gracefully stop the HTTP server (used for hot-reload)."""
    import threading
    threading.Thread(target=lambda: server.shutdown(), daemon=True).start()
    return {"success": True, "message": "Shutting down"}


POST_ROUTES = {
    "/bridge/shutdown":         action_shutdown,
    "/page":                    action_open_page,
    "/playhead":                action_set_timecode,
    # markers
    "/marker/add":              action_add_marker,
    "/marker/delete":           action_delete_marker,
    # timeline
    "/timeline/switch":         action_switch_timeline,
    "/timeline/create":         action_create_timeline,
    "/timeline/rename":         action_rename_timeline,
    "/timeline/duplicate":      action_duplicate_timeline,
    # tracks
    "/track/add":               action_add_track,
    "/track/delete":            action_delete_track,
    "/track/enable":            action_set_track_enable,
    "/track/lock":              action_set_track_lock,
    "/track/rename":            action_set_track_name,
    # media
    "/media/import":            action_import_media,
    "/media/import-storage":    action_import_media_from_storage,
    "/media/append":            action_append_to_timeline,
    # clip operations
    "/clip/color":              action_set_clip_color,
    "/clip/enabled":            action_set_clip_enabled,
    "/clip/properties":         action_set_clip_properties,
    "/clip/marker":             action_add_clip_marker,
    # titles & generators
    "/title/insert":            action_insert_title,
    "/generator/insert":        action_insert_generator,
    "/fusion/insert":           action_insert_fusion_comp,
    # render
    "/render/settings":         action_set_render_settings,
    "/render/format":           action_set_render_format,
    "/render/formats":          action_get_render_formats,
    "/render/job/add":          action_add_render_job,
    "/render/start":            action_start_rendering,
    "/render/stop":             action_stop_rendering,
    "/render/job/delete":       action_delete_render_job,
    # project
    "/project/save":            action_save_project,
    "/project/setting":         action_set_project_setting,
    "/timeline/setting":        action_set_timeline_setting,
    "/project/export-frame":    action_export_frame,
    "/timeline/subtitles":      action_create_subtitles,
    "/timeline/scene-cuts":     action_detect_scene_cuts,
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
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _error(self, code, msg):
        body = json.dumps({"error": msg}).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")
        qs = parse_qs(parsed.query)
        handler = GET_ROUTES.get(path)
        if handler:
            try:
                self._respond(handler(qs))
            except Exception:
                self._error(500, traceback.format_exc())
        else:
            self._error(404, "Unknown GET endpoint: %s" % path)

    def do_POST(self):
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
            try:
                self._respond(handler(body))
            except Exception:
                self._error(500, traceback.format_exc())
        else:
            self._error(404, "Unknown POST endpoint: %s" % path)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
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

print("[CursorBridge] Starting HTTP server on http://%s:%d ..." % (HOST, PORT))
server = None
try:
    server = HTTPServer((HOST, PORT), BridgeHandler)
    print("[CursorBridge] Bridge is running (read + write).  %d GET routes, %d POST routes." % (
        len(GET_ROUTES), len(POST_ROUTES)))
    print("[CursorBridge] To stop: close DaVinci Resolve or re-run this script.")
    server.serve_forever()
except OSError as e:
    if "Address already in use" in str(e) or "Only one usage" in str(e) or getattr(e, "errno", 0) == 10048:
        print("[CursorBridge] Port %d already in use — could not replace old bridge." % PORT)
        print("[CursorBridge] Restart DaVinci Resolve and try again.")
    else:
        print("[CursorBridge] ERROR: %s" % e)
except KeyboardInterrupt:
    print("[CursorBridge] Shutting down.")
except Exception as e:
    print("[CursorBridge] ERROR: %s" % e)
