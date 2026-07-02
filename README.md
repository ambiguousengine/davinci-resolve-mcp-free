# davinci-resolve-mcp-free

An MCP server that lets any MCP-compatible AI assistant (Claude Desktop, Claude Code,
Cursor, Windsurf, …) drive **DaVinci Resolve** — **including the free edition** —
through an in-app bridge.

This is a **security-hardened fork** of
[`hiteshK03/davinci-resolve-mcp`](https://github.com/hiteshK03/davinci-resolve-mcp),
pinned at upstream commit `f812df7` **with upstream history preserved** in this
repository, plus a small security patch and documentation aimed at AI assistants.
See **Attribution & License** below.

## Why this fork exists

1. **It runs on the free edition.** Most Resolve MCP servers require the paid *Studio*
   version because they use *external scripting*, which is locked behind the paywall.
   This project (following upstream) runs a small bridge script **inside** Resolve via
   the Scripts menu — available to everyone — and the MCP server talks to it over
   `127.0.0.1`. No Studio required.
2. **Security hardening.** The upstream bridge served `Access-Control-Allow-Origin: *`
   with no auth. This fork rejects any request carrying an `Origin` header (i.e. a
   browser) or a non-loopback `Host`, closing the local CSRF / DNS-rebinding vector.
3. **AI-assistant docs.** A companion guide of operational knowledge — lifecycle
   gotchas, a validated tool set, and workarounds for API limitations — lives in
   [`docs/USING_WITH_AN_AI_ASSISTANT.md`](docs/USING_WITH_AN_AI_ASSISTANT.md). It will
   keep growing with real-world use.

## How it works

```
AI assistant  ──MCP───▶  resolve_mcp_bridge.py  ──HTTP 127.0.0.1:9876───▶  CursorBridge.py
(Claude, etc.)          (the MCP server)                                 (runs INSIDE Resolve,
                                                                          Workspace → Scripts)
                                                                                │
                                                                          Resolve scripting API
```

The MCP server has **no dependency on where Resolve is installed** — it only talks to
the bridge on localhost. The bridge is a script you launch from inside Resolve; it
exits when you stop it.

## Requirements

- DaVinci Resolve **18+** (Free or Studio).
- A **system Python 3** (3.10–3.12 recommended) that Resolve can detect, so the Scripts
  menu can run the Python bridge. Install from python.org with *Add to PATH*.
- An MCP-compatible client.

## Install

```sh
git clone https://github.com/EddieRivers/davinci-resolve-mcp-free
cd davinci-resolve-mcp-free
python -m venv venv
# Windows: venv\Scripts\activate     |     macOS/Linux: source venv/bin/activate
pip install -r requirements.txt      # core only — see "Core vs AI extras"
```

Copy the bridge into Resolve's Fusion scripts folder:

- **Windows:** `%APPDATA%\Blackmagic Design\DaVinci Resolve\Support\Fusion\Scripts\Utility\`
- **macOS:** `~/Library/Application Support/Blackmagic Design/DaVinci Resolve/Fusion/Scripts/Utility/`
- **Linux:** `~/.local/share/DaVinciResolve/Fusion/Scripts/`

Then, in Resolve: **Workspace → Scripts → CursorBridge**. The console should print
`Bridge is running`. (No need to touch *Preferences → External scripting* — that's only
for out-of-process MCPs that require Studio.)

Register the MCP server with your client, pointing at the venv Python + `resolve_mcp_bridge.py`:

```json
{
  "mcpServers": {
    "davinci-resolve": {
      "command": "<ABS_PATH>/venv/bin/python",
      "args": ["<ABS_PATH>/src/resolve_mcp_bridge.py"]
    }
  }
}
```

## Core vs AI extras

- **Core (`requirements.txt`):** timeline, media pool, color/CDL/LUT, Fusion comps
  (add/import/export/load), render queue, node graph, titles. This is all you need for
  editing/grading/rendering automation.
- **AI extras (`requirements-ai.txt`, optional):** voice isolation (Demucs), background
  removal (rembg), auto-subtitles (faster-whisper). These pull in PyTorch/onnxruntime
  and download model weights on first use. Install only if you need them:
  `pip install -r requirements-ai.txt` (+ `ffmpeg` on PATH).

## Hardening

Applied to `CursorBridge.py` on top of upstream `f812df7`:

- Removes the wide-open `Access-Control-Allow-Origin: *`.
- Adds a request guard: **reject if an `Origin` header is present** (a legitimate local
  MCP client never sends one; browsers do) **or if `Host` is not loopback** (anti
  DNS-rebinding). `do_OPTIONS` returns `403` (no CORS grant).

Rationale: the bridge is a local HTTP server with no auth. Binding to `127.0.0.1`
already keeps it off the network, but an open-CORS server can be poked by a malicious
web page's JavaScript. The guard closes that. Smoke-test it any time with
`python scripts/check_hardening.py`.

## Capabilities & limitations

**Works on the free edition:** the large majority of the exposed tools (150+). A handful
of Neural-Engine features are Studio-only; most ship a local CPU replacement (see AI
extras). The two without a free alternative are Studio's Smart Reframe and stabilization.

**Hard limitations of the Resolve scripting API (apply on Free and Studio):**

- **No keyframe animation.** The API exposes static property values only — animated
  transforms/opacity/etc. are not settable through it. Do animation in the Fusion/Edit
  UI, or upstream in your content.
- **No Fusion node parameters.** You can add/import/export/load Fusion compositions, but
  the *values inside* them (e.g. a `Text+` node's text) are not settable via the API —
  `Text+` defaults to `"Custom Title"`. **Validated workaround:** export the comp,
  edit `StyledText` in the `.comp` file, re-import it.
- **`insert_title` ripples on V1** and cannot target a specific track (it pushes
  existing clips). Plan around it.
- **Video background removal** is CPU-bound and slow.

## Operational notes

- Launching the bridge is one click (Workspace → Scripts) each time you open Resolve.
- **`fuscript` lifecycle:** the bridge runs in `fuscript`, a process that can
  **outlive Resolve** and keep holding the port. Such a zombie is inert (its Resolve
  handle is dead) and still hardened, but to release the port cleanly, shut it down:
  `POST http://127.0.0.1:9876/bridge/shutdown`. The bridge also self-shuts a previous
  instance on start (hot-reload).

## Roadmap

- **Auto-shutdown watchdog:** terminate the bridge when Resolve's handle dies (removes
  the `fuscript` zombie).
- **`set_fusion_input`:** set `Text+` text / node parameters directly, removing the
  `.comp` round-trip.
- **`insert_title` target track:** choose the destination track instead of rippling V1.
- Grow `docs/USING_WITH_AN_AI_ASSISTANT.md` with field-tested knowledge.

Contributions of these upstream (to `hiteshK03/davinci-resolve-mcp`) are welcome.

## Support

Community project, **low-maintenance, no warranty**. Issues/PRs are welcome but not
guaranteed a response.

## Disclaimer

This is an independent community project. It is **not affiliated with, endorsed by, or
sponsored by Blackmagic Design**. "DaVinci Resolve" is a trademark of Blackmagic Design
Pty Ltd. Nothing from DaVinci Resolve is redistributed here; the bridge uses the
scripting API that Resolve itself provides at runtime.

## Attribution & License

Forked from **[`hiteshK03/davinci-resolve-mcp`](https://github.com/hiteshK03/davinci-resolve-mcp)**
(MIT), pinned at commit `f812df7`, with the original commit history preserved in this
repository. Original copyright © the upstream author. This fork adds a security patch
and documentation and remains under the **MIT License** — see [`LICENSE`](LICENSE). The
in-app bridge approach and the tool implementations are upstream's work; credit to them.
