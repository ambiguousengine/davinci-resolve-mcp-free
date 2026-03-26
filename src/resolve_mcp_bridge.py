#!/usr/bin/env python3
"""
DaVinci Resolve MCP Bridge Server

Connects to the CursorBridge HTTP server running inside DaVinci Resolve.
The bridge script must be started first: Workspace > Scripts > CursorBridge.

Exposes read AND write tools so Cursor can both query and manipulate Resolve.
"""

import json
import logging
import os
import sys
import urllib.request
import urllib.error
from typing import Dict, Any, List, Optional

log_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs")
os.makedirs(log_dir, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[logging.FileHandler(os.path.join(log_dir, "bridge_mcp.log"))],
)
logger = logging.getLogger("resolve-bridge-mcp")

from mcp.server.fastmcp import FastMCP

BRIDGE_URL = "http://127.0.0.1:9876"

CONN_ERROR = (
    "Cannot reach the CursorBridge inside DaVinci Resolve. "
    "Make sure DaVinci Resolve is open and you have started the bridge "
    "via Workspace > Scripts > CursorBridge."
)

mcp = FastMCP(
    "DaVinciResolveBridge",
    instructions=(
        "DaVinci Resolve MCP Bridge — provides full read AND write access to "
        "DaVinci Resolve via an internal HTTP bridge.\n"
        "Before using these tools, the user must start the CursorBridge script "
        "inside DaVinci Resolve (Workspace > Scripts > CursorBridge).\n"
        "If tools return connection errors, remind the user to start the bridge script.\n\n"
        "WRITE OPERATIONS: This bridge can modify the Resolve project — add markers, "
        "import media, insert titles, change clip properties, start renders, and more. "
        "Always confirm destructive operations with the user before executing."
    ),
)


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def _get(endpoint: str, params: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    url = f"{BRIDGE_URL}{endpoint}"
    if params:
        qs = "&".join(f"{k}={v}" for k, v in params.items())
        url = f"{url}?{qs}"
    try:
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            body = json.loads(e.read().decode("utf-8"))
            if e.code == 404:
                body["hint"] = "The CursorBridge may be outdated. Restart DaVinci Resolve and re-run CursorBridge."
            return body
        except Exception:
            return {"error": f"Bridge returned HTTP {e.code}"}
    except urllib.error.URLError:
        return {"error": CONN_ERROR}
    except Exception as e:
        return {"error": f"Bridge request failed: {e}"}


def _post(endpoint: str, body: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    url = f"{BRIDGE_URL}{endpoint}"
    data = json.dumps(body or {}).encode("utf-8")
    try:
        req = urllib.request.Request(
            url, data=data,
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            body = json.loads(e.read().decode("utf-8"))
            if e.code in (404, 501):
                body["hint"] = "The CursorBridge may be outdated. Restart DaVinci Resolve and re-run CursorBridge."
            return body
        except Exception:
            return {"error": f"Bridge returned HTTP {e.code}"}
    except urllib.error.URLError:
        return {"error": CONN_ERROR}
    except Exception as e:
        return {"error": f"Bridge request failed: {e}"}


# ═══════════════════════════════════════════════════════════════════════════
# READ TOOLS
# ═══════════════════════════════════════════════════════════════════════════

@mcp.tool()
def get_resolve_status() -> Dict[str, Any]:
    """Check whether the CursorBridge is running and DaVinci Resolve is connected.
    Call this first to verify the bridge is active before using other tools."""
    return _get("/status")


@mcp.tool()
def get_project_info() -> Dict[str, Any]:
    """Get information about the currently open DaVinci Resolve project.
    Returns the project name, resolution, frame rate, color science, and timeline count."""
    return _get("/project")


@mcp.tool()
def get_current_page() -> Dict[str, Any]:
    """Get which page the user is currently viewing in DaVinci Resolve.
    Returns one of: media, cut, edit, fusion, color, fairlight, deliver."""
    return _get("/page")


@mcp.tool()
def get_timeline_info() -> Dict[str, Any]:
    """Get detailed information about the current timeline.
    Returns the timeline name, duration, frame rate, track counts,
    current playhead timecode, track names, and in/out mark positions."""
    return _get("/timeline")


@mcp.tool()
def get_timeline_clips(track_type: str = "video", track_index: int = 1) -> Dict[str, Any]:
    """Get the list of clips on a specific track in the current timeline.
    Args:
        track_type: 'video', 'audio', or 'subtitle'. Defaults to 'video'.
        track_index: 1-based track index. Defaults to 1.
    Returns clip names, durations, positions, file paths, colors, and enabled state."""
    return _get("/timeline/clips", {"track_type": track_type, "track_index": str(track_index)})


@mcp.tool()
def get_timeline_markers() -> Dict[str, Any]:
    """Get all markers on the current timeline.
    Returns marker positions, colors, names, notes, and durations."""
    return _get("/timeline/markers")


@mcp.tool()
def get_render_settings() -> Dict[str, Any]:
    """Get the current render configuration for the project.
    Returns render format, codec, render mode, job list, and rendering status."""
    return _get("/render")


@mcp.tool()
def get_media_pool() -> Dict[str, Any]:
    """List clips and subfolders in the current media pool folder.
    Returns clip names, colors, and media IDs."""
    return _get("/mediapool")


# ═══════════════════════════════════════════════════════════════════════════
# WRITE TOOLS — Navigation
# ═══════════════════════════════════════════════════════════════════════════

@mcp.tool()
def open_page(page: str) -> Dict[str, Any]:
    """Switch DaVinci Resolve to a different page.
    Args:
        page: One of 'media', 'cut', 'edit', 'fusion', 'color', 'fairlight', 'deliver'."""
    return _post("/page", {"page": page})


@mcp.tool()
def set_playhead(timecode: str) -> Dict[str, Any]:
    """Move the playhead to a specific timecode in the current timeline.
    Args:
        timecode: Timecode string, e.g. '01:00:05:00' or '00:00:30:00'."""
    return _post("/playhead", {"timecode": timecode})


# ═══════════════════════════════════════════════════════════════════════════
# WRITE TOOLS — Timeline Markers
# ═══════════════════════════════════════════════════════════════════════════

@mcp.tool()
def add_marker(
    frameId: int,
    color: str = "Blue",
    name: str = "",
    note: str = "",
    duration: int = 1,
    customData: str = "",
) -> Dict[str, Any]:
    """Add a marker to the current timeline.
    Args:
        frameId: Frame number (relative to timeline start) where the marker is placed.
        color: Marker color — 'Blue', 'Cyan', 'Green', 'Yellow', 'Red', 'Pink',
               'Purple', 'Fuchsia', 'Rose', 'Lavender', 'Sky', 'Mint', 'Lemon',
               'Sand', 'Cocoa', 'Cream'.
        name: Marker name/title.
        note: Marker note/description.
        duration: Duration in frames (default 1).
        customData: Optional custom data string for scripting use."""
    return _post("/marker/add", {
        "frameId": frameId, "color": color, "name": name,
        "note": note, "duration": duration, "customData": customData,
    })


@mcp.tool()
def delete_markers(frameId: Optional[int] = None, color: Optional[str] = None) -> Dict[str, Any]:
    """Delete timeline markers by frame position or by color.
    Args:
        frameId: Delete the specific marker at this frame number.
        color: Delete all markers of this color. Use 'All' to delete every marker."""
    body: Dict[str, Any] = {}
    if frameId is not None:
        body["frameId"] = frameId
    if color is not None:
        body["color"] = color
    return _post("/marker/delete", body)


# ═══════════════════════════════════════════════════════════════════════════
# WRITE TOOLS — Timeline Management
# ═══════════════════════════════════════════════════════════════════════════

@mcp.tool()
def switch_timeline(index: int) -> Dict[str, Any]:
    """Switch to a different timeline in the project.
    Args:
        index: 1-based timeline index. Use get_project_info() to see timelineCount."""
    return _post("/timeline/switch", {"index": index})


@mcp.tool()
def create_timeline(name: str) -> Dict[str, Any]:
    """Create a new empty timeline in the media pool.
    Args:
        name: Name for the new timeline."""
    return _post("/timeline/create", {"name": name})


@mcp.tool()
def rename_timeline(name: str) -> Dict[str, Any]:
    """Rename the current timeline.
    Args:
        name: New name for the timeline."""
    return _post("/timeline/rename", {"name": name})


@mcp.tool()
def duplicate_timeline(name: str = "") -> Dict[str, Any]:
    """Duplicate the current timeline.
    Args:
        name: Optional name for the duplicated timeline."""
    return _post("/timeline/duplicate", {"name": name})


# ═══════════════════════════════════════════════════════════════════════════
# WRITE TOOLS — Track Management
# ═══════════════════════════════════════════════════════════════════════════

@mcp.tool()
def add_track(track_type: str, sub_track_type: str = "") -> Dict[str, Any]:
    """Add a new track to the current timeline.
    Args:
        track_type: 'video', 'audio', or 'subtitle'.
        sub_track_type: For audio tracks: 'mono', 'stereo', '5.1', '7.1', etc. Defaults to 'mono'."""
    return _post("/track/add", {"trackType": track_type, "subTrackType": sub_track_type})


@mcp.tool()
def delete_track(track_type: str, track_index: int) -> Dict[str, Any]:
    """Delete a track from the current timeline.
    Args:
        track_type: 'video', 'audio', or 'subtitle'.
        track_index: 1-based index of the track to delete."""
    return _post("/track/delete", {"trackType": track_type, "trackIndex": track_index})


@mcp.tool()
def set_track_enable(track_type: str, track_index: int, enabled: bool) -> Dict[str, Any]:
    """Enable or disable a track in the current timeline.
    Args:
        track_type: 'video', 'audio', or 'subtitle'.
        track_index: 1-based track index.
        enabled: True to enable, False to disable."""
    return _post("/track/enable", {"trackType": track_type, "trackIndex": track_index, "enabled": enabled})


@mcp.tool()
def set_track_lock(track_type: str, track_index: int, locked: bool) -> Dict[str, Any]:
    """Lock or unlock a track in the current timeline.
    Args:
        track_type: 'video', 'audio', or 'subtitle'.
        track_index: 1-based track index.
        locked: True to lock, False to unlock."""
    return _post("/track/lock", {"trackType": track_type, "trackIndex": track_index, "locked": locked})


@mcp.tool()
def set_track_name(track_type: str, track_index: int, name: str) -> Dict[str, Any]:
    """Rename a track in the current timeline.
    Args:
        track_type: 'video', 'audio', or 'subtitle'.
        track_index: 1-based track index.
        name: New name for the track."""
    return _post("/track/rename", {"trackType": track_type, "trackIndex": track_index, "name": name})


# ═══════════════════════════════════════════════════════════════════════════
# WRITE TOOLS — Media Management
# ═══════════════════════════════════════════════════════════════════════════

@mcp.tool()
def import_media(file_paths: List[str]) -> Dict[str, Any]:
    """Import media files into the current media pool folder.
    Args:
        file_paths: List of absolute file paths (Windows paths as seen by Resolve,
                    e.g. ['C:\\\\Users\\\\user\\\\Videos\\\\clip.mp4'])."""
    return _post("/media/import", {"filePaths": file_paths})


@mcp.tool()
def append_to_timeline(clip_name: str) -> Dict[str, Any]:
    """Append a media pool clip to the end of the current timeline.
    Args:
        clip_name: Name of the clip in the media pool (as returned by get_media_pool)."""
    return _post("/media/append", {"clipName": clip_name})


# ═══════════════════════════════════════════════════════════════════════════
# WRITE TOOLS — Clip Operations
# ═══════════════════════════════════════════════════════════════════════════

@mcp.tool()
def set_clip_color(
    track_type: str, track_index: int, clip_index: int, color: str = ""
) -> Dict[str, Any]:
    """Set the color label of a clip on the timeline.
    Args:
        track_type: 'video', 'audio', or 'subtitle'.
        track_index: 1-based track index.
        clip_index: 0-based clip position on that track.
        color: Color name (e.g. 'Orange', 'Teal', 'Lime'). Empty string clears the color."""
    return _post("/clip/color", {
        "trackType": track_type, "trackIndex": track_index,
        "clipIndex": clip_index, "color": color,
    })


@mcp.tool()
def set_clip_enabled(
    track_type: str, track_index: int, clip_index: int, enabled: bool
) -> Dict[str, Any]:
    """Enable or disable a clip on the timeline.
    Args:
        track_type: 'video', 'audio', or 'subtitle'.
        track_index: 1-based track index.
        clip_index: 0-based clip position on that track.
        enabled: True to enable, False to disable."""
    return _post("/clip/enabled", {
        "trackType": track_type, "trackIndex": track_index,
        "clipIndex": clip_index, "enabled": enabled,
    })


@mcp.tool()
def set_clip_properties(
    track_type: str, track_index: int, clip_index: int,
    properties: Dict[str, Any] = {},
) -> Dict[str, Any]:
    """Set transform and compositing properties on a timeline clip.
    Args:
        track_type: 'video', 'audio', or 'subtitle'.
        track_index: 1-based track index.
        clip_index: 0-based clip position on that track.
        properties: Dict of property key-value pairs. Supported keys include:
            'Pan', 'Tilt' (float), 'ZoomX', 'ZoomY' (0-100),
            'RotationAngle' (-360 to 360), 'Opacity' (0-100),
            'CropLeft', 'CropRight', 'CropTop', 'CropBottom' (float),
            'FlipX', 'FlipY' (bool), 'Distortion' (-1 to 1),
            'AnchorPointX', 'AnchorPointY' (float),
            'CompositeMode' (int: 0=Normal, 4=Multiply, 5=Screen, 6=Overlay, etc.),
            'Scaling' (int: 0=Project, 1=Crop, 2=Fit, 3=Fill, 4=Stretch).
    Example: {'ZoomX': 50, 'ZoomY': 50, 'Pan': -200, 'Opacity': 80}"""
    return _post("/clip/properties", {
        "trackType": track_type, "trackIndex": track_index,
        "clipIndex": clip_index, "properties": properties,
    })


# ═══════════════════════════════════════════════════════════════════════════
# WRITE TOOLS — Titles & Generators
# ═══════════════════════════════════════════════════════════════════════════

@mcp.tool()
def insert_title(title_name: str, fusion_title: bool = False) -> Dict[str, Any]:
    """Insert a title at the playhead in the current timeline.
    Args:
        title_name: Name of the title template (e.g. 'Text+', 'Scroll', 'Lower Third').
        fusion_title: If True, inserts a Fusion title instead of a standard title."""
    return _post("/title/insert", {"titleName": title_name, "fusionTitle": fusion_title})


@mcp.tool()
def insert_generator(generator_name: str, fusion_generator: bool = False) -> Dict[str, Any]:
    """Insert a generator at the playhead in the current timeline.
    Args:
        generator_name: Name of the generator (e.g. 'Solid Color', '10 Step', 'Grey Scale').
        fusion_generator: If True, inserts a Fusion generator instead of a standard one."""
    return _post("/generator/insert", {"generatorName": generator_name, "fusionGenerator": fusion_generator})


@mcp.tool()
def insert_fusion_composition() -> Dict[str, Any]:
    """Insert an empty Fusion composition at the playhead in the current timeline.
    Opens a blank Fusion comp that can be edited in the Fusion page."""
    return _post("/fusion/insert", {})


# ═══════════════════════════════════════════════════════════════════════════
# WRITE TOOLS — Rendering
# ═══════════════════════════════════════════════════════════════════════════

@mcp.tool()
def set_render_settings(settings: Dict[str, Any]) -> Dict[str, Any]:
    """Configure render settings for the project.
    Args:
        settings: Dict of render settings. Supported keys include:
            'TargetDir' (str), 'CustomName' (str), 'SelectAllFrames' (bool),
            'MarkIn' (int), 'MarkOut' (int), 'ExportVideo' (bool),
            'ExportAudio' (bool), 'FormatWidth' (int), 'FormatHeight' (int),
            'FrameRate' (float), 'VideoQuality' (int or str like 'Best'),
            'AudioCodec' (str), 'AudioBitDepth' (int), 'AudioSampleRate' (int),
            'ExportAlpha' (bool), 'NetworkOptimization' (bool).
    Example: {'TargetDir': 'C:\\\\output', 'CustomName': 'final', 'SelectAllFrames': True}"""
    return _post("/render/settings", {"settings": settings})


@mcp.tool()
def set_render_format(format: str, codec: str) -> Dict[str, Any]:
    """Set the render output format and codec.
    Args:
        format: Render format (e.g. 'mp4', 'mov', 'mxf'). Use get_render_formats() to see options.
        codec: Codec name (e.g. 'H264', 'H265'). Use get_render_formats(format) to see codecs."""
    return _post("/render/format", {"format": format, "codec": codec})


@mcp.tool()
def get_render_formats(format: str = "") -> Dict[str, Any]:
    """List available render formats, or codecs for a specific format.
    Args:
        format: If provided, returns available codecs for this format. Otherwise lists all formats."""
    return _post("/render/formats", {"format": format})


@mcp.tool()
def add_render_job() -> Dict[str, Any]:
    """Add a render job to the queue based on current render settings.
    Returns the job ID if successful. Configure settings first with set_render_settings()."""
    return _post("/render/job/add", {})


@mcp.tool()
def start_rendering(job_ids: List[str] = []) -> Dict[str, Any]:
    """Start rendering queued jobs.
    Args:
        job_ids: Optional list of specific job IDs to render. Empty = render all queued jobs."""
    return _post("/render/start", {"jobIds": job_ids})


@mcp.tool()
def stop_rendering() -> Dict[str, Any]:
    """Stop any currently active render process."""
    return _post("/render/stop", {})


@mcp.tool()
def delete_render_job(job_id: str = "", all: bool = False) -> Dict[str, Any]:
    """Delete render job(s) from the queue.
    Args:
        job_id: ID of a specific job to delete.
        all: If True, deletes all render jobs in the queue."""
    return _post("/render/job/delete", {"jobId": job_id, "all": all})


# ═══════════════════════════════════════════════════════════════════════════
# WRITE TOOLS — Project & Settings
# ═══════════════════════════════════════════════════════════════════════════

@mcp.tool()
def save_project() -> Dict[str, Any]:
    """Save the currently open DaVinci Resolve project."""
    return _post("/project/save", {})


@mcp.tool()
def set_project_setting(key: str, value: str) -> Dict[str, Any]:
    """Set a project-level setting.
    Args:
        key: Setting name (e.g. 'timelineResolutionWidth', 'timelineFrameRate', 'superScale').
        value: Setting value as string."""
    return _post("/project/setting", {"key": key, "value": value})


@mcp.tool()
def set_timeline_setting(key: str, value: str) -> Dict[str, Any]:
    """Set a timeline-level setting on the current timeline.
    Args:
        key: Setting name (e.g. 'timelineResolutionWidth', 'timelineResolutionHeight', 'timelineFrameRate').
        value: Setting value as string."""
    return _post("/timeline/setting", {"key": key, "value": value})


@mcp.tool()
def export_current_frame(file_path: str) -> Dict[str, Any]:
    """Export the current frame (at playhead) as a still image.
    Args:
        file_path: Absolute Windows path with extension (e.g. 'C:\\\\output\\\\frame.png').
                   Supported formats: .dpx, .cin, .tif, .jpg, .png, .ppm, .bmp, .xpm."""
    return _post("/project/export-frame", {"filePath": file_path})


@mcp.tool()
def create_subtitles_from_audio() -> Dict[str, Any]:
    """Auto-generate subtitles from the audio in the current timeline using DaVinci Resolve's built-in speech-to-text."""
    return _post("/timeline/subtitles", {})


@mcp.tool()
def detect_scene_cuts() -> Dict[str, Any]:
    """Automatically detect and create scene cuts along the current timeline."""
    return _post("/timeline/scene-cuts", {})


# ═══════════════════════════════════════════════════════════════════════════
# AI: VOICE ISOLATION (local Demucs — replaces Studio Voice Isolation)
# ═══════════════════════════════════════════════════════════════════════════

_demucs_model = None
_demucs_model_name = None


def _load_demucs(model_name: str = "htdemucs"):
    global _demucs_model, _demucs_model_name
    if _demucs_model and _demucs_model_name == model_name:
        return _demucs_model
    try:
        from demucs.pretrained import get_model
    except ImportError:
        return None
    logger.info("Loading Demucs model '%s' (first load downloads ~80MB)...", model_name)
    _demucs_model = get_model(model_name)
    _demucs_model.eval()
    _demucs_model_name = model_name
    logger.info("Demucs model '%s' loaded.", model_name)
    return _demucs_model


def _run_voice_isolation(
    file_path: str,
    model_name: str = "htdemucs",
    two_stems: str = "vocals",
    output_dir: str = "",
) -> Dict[str, Any]:
    try:
        import torch
        import numpy as np
        import soundfile as sf
        from demucs.apply import apply_model
    except ImportError as e:
        return {"error": f"Missing dependency: {e}. Run: pip install demucs soundfile"}

    if not os.path.isfile(file_path):
        return {"error": f"File not found: {file_path}"}

    model = _load_demucs(model_name)
    if model is None:
        return {"error": "demucs is not installed. Run: pip install demucs"}

    if not output_dir:
        output_dir = os.path.join(os.path.dirname(file_path), "davinci-mcp-output", "voice-isolation")
    os.makedirs(output_dir, exist_ok=True)

    logger.info("Voice isolation starting: %s (model=%s, stems=%s)", file_path, model_name, two_stems)

    try:
        wav_np, sr = sf.read(file_path, dtype="float32")
        if wav_np.ndim == 1:
            wav_np = np.stack([wav_np, wav_np], axis=-1)

        wav_tensor = torch.from_numpy(wav_np.T).float()

        target_sr = model.samplerate
        if sr != target_sr:
            import torchaudio.functional as F
            wav_tensor = F.resample(wav_tensor, sr, target_sr)

        ref = wav_tensor.mean(0)
        wav_tensor = (wav_tensor - ref.mean()) / ref.std()
        sources = apply_model(model, wav_tensor[None], device="cpu")[0]
        sources = sources * ref.std() + ref.mean()

        stem_idx = model.sources.index(two_stems) if two_stems in model.sources else 0
        stem_audio = sources[stem_idx].detach().cpu().numpy()

        other_indices = [i for i in range(len(model.sources)) if i != stem_idx]
        nostem_audio = sources[other_indices].sum(0).detach().cpu().numpy()

        basename = os.path.splitext(os.path.basename(file_path))[0]
        stem_dir = os.path.join(output_dir, basename)
        os.makedirs(stem_dir, exist_ok=True)

        stem_path = os.path.join(stem_dir, f"{two_stems}.wav")
        nostem_path = os.path.join(stem_dir, f"no_{two_stems}.wav")

        sf.write(stem_path, stem_audio.T, target_sr)
        sf.write(nostem_path, nostem_audio.T, target_sr)

        logger.info("Voice isolation complete: %s, %s", stem_path, nostem_path)
        return {
            "success": True,
            "model": model_name,
            "stems": {two_stems: stem_path, f"no_{two_stems}": nostem_path},
            "output_dir": stem_dir,
        }
    except Exception as e:
        logger.exception("Voice isolation failed")
        return {"error": f"Demucs separation failed: {e}"}


@mcp.tool()
def voice_isolate(
    file_path: str,
    model: str = "htdemucs",
    stems: str = "vocals",
    output_dir: str = "",
) -> Dict[str, Any]:
    """Separate vocals from background audio using Demucs (replaces Studio Voice Isolation).
    Downloads the model on first use (~150MB). Runs on CPU (~1.5x real-time).
    Args:
        file_path: Absolute path to audio/video file.
        model: Demucs model — 'htdemucs' (default, best quality), 'htdemucs_ft' (slower, slightly better),
               'mdx_extra' (good alternative). First run downloads the model.
        stems: Which stem to isolate — 'vocals' (default, outputs vocals + no_vocals),
               'drums', or 'bass'.
        output_dir: Optional output directory. Defaults to a folder next to the source file.
    Returns paths to the separated audio stems (e.g. vocals.wav, no_vocals.wav)."""
    return _run_voice_isolation(file_path, model, stems, output_dir)


@mcp.tool()
def voice_isolate_timeline(
    model: str = "htdemucs",
    stems: str = "vocals",
    output_dir: str = "",
) -> Dict[str, Any]:
    """Isolate vocals from the current timeline's audio track using Demucs.
    Automatically detects the audio file from audio track 1.
    Args:
        model: Demucs model — 'htdemucs' (default), 'htdemucs_ft', 'mdx_extra'.
        stems: Which stem to isolate — 'vocals' (default), 'drums', 'bass'.
        output_dir: Optional output directory.
    Returns paths to the separated audio stems."""
    clips = _get("/timeline/clips", {"track_type": "audio", "track_index": "1"})
    if "error" in clips:
        return clips
    clip_list = clips.get("clips", [])
    if not clip_list:
        return {"error": "No audio clips found on audio track 1"}
    file_path = clip_list[0].get("File Path", "")
    if not file_path:
        return {"error": "Could not determine audio file path from the timeline clip"}
    return _run_voice_isolation(file_path, model, stems, output_dir)


# ═══════════════════════════════════════════════════════════════════════════
# AI: BACKGROUND REMOVAL (local rembg — replaces Studio Magic Mask)
# ═══════════════════════════════════════════════════════════════════════════

_rembg_session = None
_rembg_model_name = None


def _load_rembg(model_name: str = "birefnet-general"):
    global _rembg_session, _rembg_model_name
    if _rembg_session and _rembg_model_name == model_name:
        return _rembg_session
    try:
        from rembg import new_session
    except ImportError:
        return None
    logger.info("Loading rembg model '%s' (first load downloads the model)...", model_name)
    _rembg_session = new_session(model_name)
    _rembg_model_name = model_name
    logger.info("rembg model '%s' loaded.", model_name)
    return _rembg_session


def _remove_bg_single(input_path: str, output_path: str, model_name: str, alpha_matte: bool) -> bool:
    session = _load_rembg(model_name)
    if session is None:
        return False
    from rembg import remove
    from PIL import Image

    img = Image.open(input_path)
    result = remove(img, session=session, alpha_matting=alpha_matte)
    result.save(output_path)
    return True


@mcp.tool()
def remove_background(
    file_path: str,
    model: str = "birefnet-general",
    output_path: str = "",
    alpha_matting: bool = False,
) -> Dict[str, Any]:
    """Remove background from a single image (replaces Studio Magic Mask for stills).
    Downloads the model on first use (~170MB). Runs on CPU.
    Args:
        file_path: Absolute path to the image file.
        model: Segmentation model — 'birefnet-general' (default, best quality),
               'birefnet-general-lite' (faster), 'u2net' (classic), 'u2net_human_seg' (people only),
               'isnet-general-use', 'silueta' (smallest).
        output_path: Where to save the result. Defaults to <input>_nobg.png.
        alpha_matting: If True, applies alpha matting for smoother edges (slower).
    Returns the path to the output image with transparent background."""
    try:
        from rembg import remove
        from PIL import Image
    except ImportError:
        return {"error": "rembg is not installed. Run: pip install 'rembg[cpu]'"}

    if not os.path.isfile(file_path):
        return {"error": f"File not found: {file_path}"}

    if not output_path:
        base, _ = os.path.splitext(file_path)
        output_path = f"{base}_nobg.png"

    session = _load_rembg(model)
    if session is None:
        return {"error": "Failed to load rembg model"}

    logger.info("Removing background: %s", file_path)
    try:
        img = Image.open(file_path)
        result = remove(img, session=session, alpha_matting=alpha_matting)
        result.save(output_path)
        logger.info("Background removed: %s", output_path)
        return {"success": True, "output_path": output_path}
    except Exception as e:
        return {"error": f"Background removal failed: {e}"}


@mcp.tool()
def remove_background_video(
    file_path: str,
    model: str = "birefnet-general",
    output_dir: str = "",
    output_format: str = "png_sequence",
    alpha_matting: bool = False,
) -> Dict[str, Any]:
    """Remove background from every frame of a video file (replaces Studio Magic Mask for video).
    Extracts frames with ffmpeg, processes each with AI, reassembles the result.
    Args:
        file_path: Absolute path to the video file.
        model: Segmentation model — 'birefnet-general' (default), 'birefnet-general-lite' (faster),
               'u2net', 'u2net_human_seg'.
        output_dir: Where to save output frames. Defaults to a folder next to the source file.
        output_format: 'png_sequence' (default, PNG frames with alpha) or 'matte_video'
                       (grayscale matte as MP4, white=foreground).
        alpha_matting: If True, applies alpha matting per frame (slower, smoother edges).
    Returns the output directory path and frame count. Processing is ~0.5-2s per frame on CPU."""
    try:
        from rembg import remove
        from PIL import Image
    except ImportError:
        return {"error": "rembg is not installed. Run: pip install 'rembg[cpu]'"}

    if not os.path.isfile(file_path):
        return {"error": f"File not found: {file_path}"}

    import subprocess
    import shutil

    basename = os.path.splitext(os.path.basename(file_path))[0]
    if not output_dir:
        output_dir = os.path.join(os.path.dirname(file_path), "davinci-mcp-output", "background-removal", basename)
    os.makedirs(output_dir, exist_ok=True)

    frames_dir = os.path.join(output_dir, "_frames")
    os.makedirs(frames_dir, exist_ok=True)

    logger.info("Extracting frames from: %s", file_path)
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-i", file_path, "-qscale:v", "2", os.path.join(frames_dir, "frame_%06d.png")],
            capture_output=True, text=True, check=True,
        )
    except FileNotFoundError:
        return {"error": "ffmpeg not found. Install ffmpeg to process video files."}
    except subprocess.CalledProcessError as e:
        return {"error": f"ffmpeg frame extraction failed: {e.stderr[:500]}"}

    frame_files = sorted(f for f in os.listdir(frames_dir) if f.endswith(".png"))
    if not frame_files:
        return {"error": "No frames extracted from video"}

    total = len(frame_files)
    logger.info("Processing %d frames with model '%s'...", total, model)

    session = _load_rembg(model)
    if session is None:
        return {"error": "Failed to load rembg model"}

    output_frames_dir = os.path.join(output_dir, "frames")
    os.makedirs(output_frames_dir, exist_ok=True)

    for i, fname in enumerate(frame_files):
        if (i + 1) % 50 == 0 or i == 0:
            logger.info("  Frame %d / %d", i + 1, total)
        try:
            img = Image.open(os.path.join(frames_dir, fname))
            result = remove(img, session=session, alpha_matting=alpha_matting)

            if output_format == "matte_video":
                alpha = result.split()[-1] if result.mode == "RGBA" else result.convert("L")
                alpha.save(os.path.join(output_frames_dir, fname))
            else:
                result.save(os.path.join(output_frames_dir, fname))
        except Exception as e:
            logger.warning("  Frame %d failed: %s", i + 1, e)

    shutil.rmtree(frames_dir, ignore_errors=True)

    response: Dict[str, Any] = {
        "success": True,
        "model": model,
        "total_frames": total,
        "output_dir": output_frames_dir,
        "output_format": output_format,
    }

    if output_format == "matte_video":
        matte_path = os.path.join(output_dir, f"{basename}_matte.mp4")
        try:
            probe = subprocess.run(
                ["ffprobe", "-v", "error", "-select_streams", "v:0",
                 "-show_entries", "stream=r_frame_rate", "-of", "csv=p=0", file_path],
                capture_output=True, text=True,
            )
            fps = probe.stdout.strip() or "30"

            subprocess.run(
                ["ffmpeg", "-y", "-framerate", fps,
                 "-i", os.path.join(output_frames_dir, "frame_%06d.png"),
                 "-c:v", "libx264", "-pix_fmt", "yuv420p", matte_path],
                capture_output=True, text=True, check=True,
            )
            response["matte_video"] = matte_path
        except Exception as e:
            logger.warning("Matte video assembly failed: %s", e)
            response["matte_video_error"] = str(e)

    logger.info("Background removal complete: %d frames processed", total)
    return response


@mcp.tool()
def remove_background_clip(
    track_type: str = "video",
    track_index: int = 1,
    clip_index: int = 0,
    model: str = "birefnet-general",
    output_format: str = "png_sequence",
) -> Dict[str, Any]:
    """Remove background from a specific timeline clip's source video.
    Finds the clip's source file from the timeline, processes it, returns output paths.
    Args:
        track_type: 'video' or 'audio'. Defaults to 'video'.
        track_index: 1-based track index. Defaults to 1.
        clip_index: 0-based clip position on the track. Defaults to 0.
        model: Segmentation model — 'birefnet-general' (default), 'birefnet-general-lite' (faster).
        output_format: 'png_sequence' (PNG with alpha) or 'matte_video' (B/W matte MP4).
    Returns the output directory and frame count."""
    clips = _get("/timeline/clips", {"track_type": track_type, "track_index": str(track_index)})
    if "error" in clips:
        return clips
    clip_list = clips.get("clips", [])
    if not clip_list:
        return {"error": f"No clips on {track_type} track {track_index}"}
    if clip_index < 0 or clip_index >= len(clip_list):
        return {"error": f"clip_index {clip_index} out of range (0-{len(clip_list) - 1})"}
    file_path = clip_list[clip_index].get("File Path", "")
    if not file_path:
        return {"error": "Could not determine file path for this clip"}
    return remove_background_video(file_path, model=model, output_format=output_format)


# ═══════════════════════════════════════════════════════════════════════════
# TRANSCRIPTION (local Whisper via faster-whisper)
# ═══════════════════════════════════════════════════════════════════════════

_whisper_model = None
_whisper_model_size = None


def _load_whisper(model_size: str = "small"):
    global _whisper_model, _whisper_model_size
    if _whisper_model and _whisper_model_size == model_size:
        return _whisper_model
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        return None
    logger.info("Loading Whisper model '%s' (first load downloads ~483MB)...", model_size)
    _whisper_model = WhisperModel(model_size, device="cpu", compute_type="int8", cpu_threads=4)
    _whisper_model_size = model_size
    logger.info("Whisper model '%s' loaded.", model_size)
    return _whisper_model


def _run_transcription(file_path: str, model_size: str = "small", language: Optional[str] = None) -> Dict[str, Any]:
    model = _load_whisper(model_size)
    if model is None:
        return {"error": "faster-whisper is not installed. Run: pip install faster-whisper"}

    try:
        kwargs: Dict[str, Any] = {"beam_size": 5, "word_timestamps": True}
        if language:
            kwargs["language"] = language

        segments_gen, info = model.transcribe(file_path, **kwargs)

        segments = []
        full_text_parts = []
        for seg in segments_gen:
            words = []
            if seg.words:
                words = [{"word": w.word.strip(), "start": round(w.start, 2), "end": round(w.end, 2),
                          "probability": round(w.probability, 2)} for w in seg.words]
            segments.append({
                "start": round(seg.start, 2),
                "end": round(seg.end, 2),
                "text": seg.text.strip(),
                "words": words,
            })
            full_text_parts.append(seg.text.strip())

        return {
            "language": info.language,
            "language_probability": round(info.language_probability, 2),
            "duration": round(info.duration, 2),
            "full_text": " ".join(full_text_parts),
            "segments": segments,
        }
    except Exception as e:
        return {"error": f"Transcription failed: {e}"}


@mcp.tool()
def transcribe_timeline(model_size: str = "small", language: str = "") -> Dict[str, Any]:
    """Transcribe the audio from the current timeline's audio track using local Whisper.
    Downloads the model on first use (~483MB for 'small'). Runs entirely on CPU.
    Args:
        model_size: Whisper model size - 'tiny', 'base', 'small', 'medium', or 'large-v3'.
                    'small' is the default (good accuracy/speed balance).
        language: Optional language code (e.g. 'en', 'es', 'fr'). Auto-detected if empty.
    Returns segments with timestamps and the full transcript text."""
    clips = _get("/timeline/clips", {"track_type": "audio", "track_index": "1"})
    if "error" in clips:
        return clips
    clip_list = clips.get("clips", [])
    if not clip_list:
        return {"error": "No audio clips found on audio track 1"}
    file_path = clip_list[0].get("File Path", "")
    if not file_path:
        return {"error": "Could not determine audio file path"}
    return _run_transcription(file_path, model_size, language or None)


@mcp.tool()
def transcribe_file(file_path: str, model_size: str = "small", language: str = "") -> Dict[str, Any]:
    """Transcribe any audio or video file using local Whisper.
    Args:
        file_path: Absolute path to the audio/video file (Windows path).
        model_size: Whisper model size - 'tiny', 'base', 'small', 'medium', or 'large-v3'.
        language: Optional language code. Auto-detected if empty.
    Returns segments with timestamps and the full transcript text."""
    return _run_transcription(file_path, model_size, language or None)


# ---------------------------------------------------------------------------
if __name__ == "__main__":
    logger.info("Starting DaVinci Resolve MCP Bridge Server (read + write + AI tools)")
    mcp.run()
