# DaVinci Resolve MCP Bridge

Control DaVinci Resolve from AI coding assistants like Cursor using the [Model Context Protocol (MCP)](https://modelcontextprotocol.io/).

This bridge gives your AI assistant **full read and write access** to DaVinci Resolve — query timelines, manipulate clips, add markers, insert titles, control rendering, and even transcribe audio using local Whisper models. It works with **DaVinci Resolve Free** (no Studio license required).

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

### 44 MCP Tools

**Read (9 tools):** project info, current page, timeline details, clip lists, markers, render settings, media pool contents, render formats

**Write (33 tools):**
- **Navigation** — switch pages, move playhead
- **Markers** — add/delete timeline markers
- **Timeline** — create, rename, duplicate, switch timelines
- **Tracks** — add, delete, enable/disable, lock/unlock, rename
- **Media** — import files, append clips to timeline
- **Clips** — set color, enable/disable, transform properties (pan, tilt, zoom, rotation, opacity, crop, composite mode)
- **Titles & Generators** — insert Text+, generators, Fusion compositions
- **Rendering** — configure settings, set format/codec, manage render queue, start/stop renders
- **Project** — save, export current frame, modify project/timeline settings, auto-subtitles, scene detection

**Transcription (2 tools):**
- **transcribe_timeline** — transcribe the current timeline's audio using local Whisper (via [faster-whisper](https://github.com/SYSTRAN/faster-whisper))
- **transcribe_file** — transcribe any audio/video file

Models run locally on CPU with no cloud dependency. First run downloads the model (~483MB for `small`).

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
- *"Set up a render to MP4 H.265 and start rendering"*
- *"Import these files into the media pool: ..."*

## Architecture

The bridge exposes a clean HTTP API:

| Method | Endpoints | Purpose |
|--------|-----------|---------|
| `GET`  | `/status`, `/project`, `/page`, `/timeline`, `/timeline/clips`, `/timeline/markers`, `/render`, `/mediapool` | Read-only queries |
| `POST` | `/page`, `/playhead`, `/marker/*`, `/timeline/*`, `/track/*`, `/media/*`, `/clip/*`, `/title/*`, `/generator/*`, `/fusion/*`, `/render/*`, `/project/*` | Write/mutation operations |

All responses are JSON. The MCP server translates between MCP tool calls and these HTTP endpoints.

## Transcription

The MCP server includes built-in audio transcription powered by [faster-whisper](https://github.com/SYSTRAN/faster-whisper) (a CTranslate2 reimplementation of OpenAI's Whisper). Transcription runs entirely on your local CPU.

Available models: `tiny`, `base`, `small` (default), `medium`, `large-v3`

The model is automatically downloaded on first use and cached locally.

## Limitations

- **Keyframe animations** cannot be set via the scripting API — only static property values
- **Fusion node parameters** (text content, effects) require manual editing in the Fusion page
- **Transitions** must be added manually from the Effects Library
- **Color grading** adjustments are not exposed through the scripting API
- Some features like auto-subtitles and magic mask are **Studio-only**

## License

MIT
