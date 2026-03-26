# DaVinci Resolve MCP Bridge

Control DaVinci Resolve from AI coding assistants like Cursor using the [Model Context Protocol (MCP)](https://modelcontextprotocol.io/).

This bridge gives your AI assistant **full read and write access** to DaVinci Resolve — query timelines, manipulate clips, add markers, insert titles, control rendering, transcribe audio, isolate vocals, and remove video backgrounds. All AI features use **open-source models running locally** as free replacements for Studio-only features. Works with **DaVinci Resolve Free** (no Studio license required).

## How It Works

The bridge uses a two-part architecture to work around the Free version's lack of external scripting:

```
Cursor (AI Assistant)
    │
    │  MCP Protocol (stdio)
    ▼
resolve_mcp_bridge.py        ← MCP server (runs on your machine)
    │
    │  HTTP (localhost:9876)
    ▼
CursorBridge.py              ← Runs INSIDE DaVinci Resolve
    │                           (Workspace > Scripts > CursorBridge)
    ▼
DaVinci Resolve API
```

**CursorBridge.py** runs as an internal Fusion script inside Resolve, exposing an HTTP API on `localhost:9876`. **resolve_mcp_bridge.py** is the MCP server that Cursor talks to — it translates MCP tool calls into HTTP requests to the bridge.

## Features

### 162 MCP Tools

**Read (31 endpoints):** project info, current page, timeline details, clip lists, markers, render settings & resolutions, media pool contents & folder structure, clip metadata & properties, per-clip markers & flags, node graph & LUT info, color versions, color groups, Fusion compositions, takes, linked items, voice isolation state, Fairlight presets, current video item, clip thumbnail, gallery albums & stills, keyframe mode, project list, database list, media storage, quick export presets

**Write (124 endpoints):**
- **Navigation** — switch pages, move playhead
- **Markers** — add/delete timeline markers
- **Per-Clip Markers & Flags** — add/get/delete markers on individual clips, add/get/clear flags
- **Timeline** — create, rename, duplicate, switch, export (AAF/EDL/FCPXML/OTIO), set/clear mark in/out
- **Timeline Clip Manipulation** — delete clips (with ripple), link/unlink clips, create compound clips, create Fusion clips
- **Tracks** — add, delete, enable/disable, lock/unlock, rename
- **Media Pool Deep Access** — navigate folders, create subfolders, get/set clip metadata & properties, move/delete/relink/unlink clips, auto-sync audio, import timelines from file, export metadata to CSV, replace clips
- **Media** — import files, import from storage, append/insert clips to timeline
- **Media Storage** — browse volumes, list files/folders, reveal in storage panel
- **Clips** — set color, enable/disable, transform properties, render cache, update sidecar (BRAW/R3D)
- **Color Grading** — set/get LUT on nodes, enable/disable nodes, apply grade from DRX, set CDL values, export LUT, copy grades between clips, reset grades, reset node colors, ARRI CDL/LUT
- **Color Versions** — add, load, delete, rename local/remote versions
- **Color Groups** — create, delete, assign clips to groups, remove from groups
- **Fusion Compositions** — list, add, import, export, delete, load, rename per-clip Fusion comps
- **Smart Features (Studio)** — Magic Mask (create/regenerate), Stabilize, Smart Reframe
- **Audio/Fairlight** — apply Fairlight presets, insert audio at playhead, get/set voice isolation state (per-track and per-clip)
- **Take Selector** — add takes, select, delete, finalize
- **Proxy Management** — link/unlink proxy media, replace clip source
- **Titles & Generators** — insert Text+, generators, Fusion compositions
- **Rendering** — configure settings, set format/codec, manage render queue, start/stop, job status, render mode, quick export, render presets, LUT refresh, render resolutions
- **Gallery & Stills** — list/create albums, grab stills (single or all clips), export/import/delete stills, set labels
- **Project** — save, export current frame, modify project/timeline settings, auto-subtitles, scene detection
- **Project Manager** — list/load/create/delete projects, archive/export/import projects, navigate project folders, switch databases
- **Presets** — layout presets (save/load/export/import), render presets, burn-in presets, keyframe mode

**AI Tools (7 tools) — open-source replacements for Studio features:**

| Studio Feature | Open-Source Replacement | MCP Tools |
|---|---|---|
| Voice Isolation (Neural Engine) | [Demucs v4](https://github.com/facebookresearch/demucs) by Meta | `voice_isolate`, `voice_isolate_timeline` |
| Magic Mask (AI segmentation) | [rembg](https://github.com/danielgatis/rembg) with BiRefNet | `remove_background`, `remove_background_video`, `remove_background_clip` |
| Subtitle Generation | [faster-whisper](https://github.com/SYSTRAN/faster-whisper) (OpenAI Whisper) | `transcribe_timeline`, `transcribe_file` |

All AI models run locally on CPU with no cloud dependency. Models are downloaded automatically on first use.

## Setup

### Prerequisites

- DaVinci Resolve 18+ (Free or Studio)
- Python 3.9+ on the same machine as Resolve
- An MCP-compatible AI assistant (Cursor, Claude Desktop, etc.)

### 1. Install the CursorBridge script

Copy `src/CursorBridge.py` to your DaVinci Resolve scripts folder:

**Windows:**
```
%APPDATA%\Blackmagic Design\DaVinci Resolve\Support\Fusion\Scripts\Utility\
```

**macOS:**
```
~/Library/Application Support/Blackmagic Design/DaVinci Resolve/Fusion/Scripts/Utility/
```

**Linux:**
```
~/.local/share/DaVinciResolve/Fusion/Scripts/
```

### 2. Set up the MCP server

```bash
# Clone the repo
git clone https://github.com/hiteshK03/davinci-resolve-mcp.git
cd davinci-resolve-mcp

# Create a virtual environment and install dependencies
python -m venv venv
source venv/bin/activate  # or venv\Scripts\activate on Windows
pip install -r requirements.txt
```

### 3. Configure your AI assistant

Add to your MCP configuration (e.g., `.cursor/mcp.json`):

```json
{
  "mcpServers": {
    "davinci-resolve": {
      "command": "python",
      "args": ["path/to/davinci-resolve-mcp/src/resolve_mcp_bridge.py"]
    }
  }
}
```

On **Windows with WSL** (Cursor running in WSL, Resolve on Windows):
```json
{
  "mcpServers": {
    "davinci-resolve": {
      "command": "cmd.exe",
      "args": ["/c", "C:\\path\\to\\venv\\Scripts\\python.exe", "C:\\path\\to\\src\\resolve_mcp_bridge.py"]
    }
  }
}
```

### 4. Start the bridge

1. Open DaVinci Resolve
2. Go to **Workspace > Scripts > CursorBridge**
3. The console should show: `Bridge is running (read + write). 8 GET routes, 37 POST routes.`
4. Your AI assistant can now control Resolve

The bridge supports **hot-reload** — re-running the script from the menu will gracefully replace the previous instance.

## Usage Examples

Once the bridge is running, you can ask your AI assistant things like:

- *"What's on my timeline right now?"*
- *"Add a green marker at the 5 second mark called 'intro ends'"*
- *"Insert a Text+ title at the playhead"*
- *"Set the opacity of the first clip on video track 2 to 70%"*
- *"Zoom in the first clip to 120%"*
- *"Transcribe my timeline audio"*
- *"Isolate the vocals from my timeline audio"*
- *"Remove the background from the first clip on video track 1"*
- *"Set up a render to MP4 H.265 and start rendering"*
- *"Import these files into the media pool: ..."*

## Architecture

The bridge exposes a clean HTTP API (31 GET + 124 POST = 155 endpoints):

| Method | Endpoints | Purpose |
|--------|-----------|---------|
| `GET`  | `/status`, `/project`, `/page`, `/timeline`, `/timeline/clips`, `/timeline/markers`, `/timeline/current-item`, `/timeline/thumbnail`, `/render`, `/mediapool`, `/mediapool/structure`, `/mediapool/clip/metadata`, `/mediapool/clip/info`, `/clip/markers`, `/clip/flags`, `/gallery/albums`, `/gallery/stills` | Read-only queries |
| `POST` | `/page`, `/playhead`, `/marker/*`, `/timeline/*`, `/track/*`, `/media/*`, `/mediapool/*`, `/clip/*`, `/title/*`, `/generator/*`, `/fusion/*`, `/render/*`, `/project/*`, `/gallery/*` | Write/mutation operations |

All responses are JSON. The MCP server translates between MCP tool calls and these HTTP endpoints.

## AI Features — Studio Replacements

These features use open-source models to replicate capabilities that normally require DaVinci Resolve Studio ($295). All processing runs locally on your CPU.

### Voice Isolation (replaces Studio Voice Isolation)

Powered by [Demucs v4](https://github.com/facebookresearch/demucs) (Hybrid Transformer) by Meta Research. Separates audio into stems: **vocals**, **drums**, **bass**, and **other**. Use two-stem mode to get clean `vocals.wav` and `no_vocals.wav` files.

- **Models:** `htdemucs` (default, best quality), `htdemucs_ft` (4x slower, slightly better), `mdx_extra`
- **Speed:** ~1.5x real-time on CPU (60s audio takes ~90s)
- **First run:** Downloads the model (~150MB)

### Background Removal (replaces Studio Magic Mask)

Powered by [rembg](https://github.com/danielgatis/rembg) with BiRefNet, U2-Net, and other segmentation models. Removes backgrounds from images and video frame-by-frame, producing either transparent PNGs or black/white matte videos.

- **Models:** `birefnet-general` (default, best quality), `birefnet-general-lite` (faster), `u2net`, `u2net_human_seg` (people only), `isnet-general-use`
- **Speed:** ~0.5-2s per frame on CPU depending on resolution
- **Output formats:** PNG sequence with alpha channel, or grayscale matte video (MP4)
- **Requires:** ffmpeg for video frame extraction/reassembly

### Transcription (replaces Studio Subtitle Generation)

Powered by [faster-whisper](https://github.com/SYSTRAN/faster-whisper) (CTranslate2 reimplementation of OpenAI's Whisper). Generates word-level timestamps.

- **Models:** `tiny`, `base`, `small` (default), `medium`, `large-v3`
- **First run:** Downloads the model (~483MB for `small`)

## Free vs Studio Compatibility

**155 of 162 tools work on DaVinci Resolve Free.** The remaining 7 require Studio (DaVinci Neural Engine). For each Studio-only feature, a local open-source AI alternative is provided that works on Free.

| Feature | Studio Tool | Free Alternative (Local AI) |
|---|---|---|
| Voice Isolation | `get_voice_isolation_state`, `set_voice_isolation_state` | `voice_isolate`, `voice_isolate_timeline` (Demucs v4) |
| Magic Mask / BG Removal | `create_magic_mask`, `regenerate_magic_mask` | `remove_background`, `remove_background_video`, `remove_background_clip` (rembg/BiRefNet) |
| Speech-to-Text | `create_subtitles_from_audio` | `transcribe_timeline`, `transcribe_file` (faster-whisper) |
| Smart Reframe | `smart_reframe_clip` | — |
| Stabilization | `stabilize_clip` | — |

All tool docstrings are tagged: `[STUDIO ONLY]` for Studio-required tools, `[FREE + STUDIO · LOCAL AI]` for local AI alternatives.

## Limitations

- **Keyframe animations** cannot be set via the scripting API — only static property values
- **Fusion node parameters** inside compositions (text content, effect values) require the Fusion page UI
- **Transitions** must be added manually from the Effects Library
- **Video background removal** (local AI) is CPU-bound and can be slow for long clips — consider processing short segments
- **Gallery stills** require being on the Color page for grab operations

## License

MIT
