# Using this MCP with an AI assistant

Operational knowledge for an AI assistant driving DaVinci Resolve through this server.
Read this before you start issuing tools — it will save you from dead ends the API
simply does not support. This guide grows with real-world use; PRs with field-tested
findings are welcome.

## Mental model

- A bridge script runs **inside Resolve** and exposes the Resolve scripting API over
  `http://127.0.0.1:9876`. Your MCP tools call that bridge.
- The scripting API is **state-setting, not timeline-animating**: you can set *static*
  property values, create/modify timeline items, apply grades, and queue renders — but
  you **cannot** set keyframes or edit the internals of a Fusion composition.
- If the bridge is not running (the human hasn't started it, or it crashed), your tools
  will fail to connect. Ask the human to start **Workspace → Scripts → CursorBridge**.

## Safe operation (do this)

- **Confirm destructive actions with the human, per action:** deleting media, timelines
  or projects; emptying bins; **starting a render**. Queuing a render job is safe;
  *starting* it is not — never call the render-start tool without an explicit go-ahead.
- **Experiment on a throwaway, empty project**, never on the human's real project.
- **Report what you did and what came back**; don't assume success — verify with a
  read-back tool (e.g. after creating a timeline, read its info).

## Tool catalog (by area)

Over 150 tools are exposed. The ones below were validated end-to-end on the **free**
edition and are a reliable starting set:

- **Status / project:** `get_resolve_status`, `get_project_info`.
- **Timeline:** `create_timeline`, `get_timeline_info`, `get_timeline_clips`,
  `insert_to_timeline`, `set_playhead`.
- **Media:** `import_media`, `get_media_pool`.
- **Titles / Fusion comps:** `insert_title`, `get_fusion_comps`,
  `export_fusion_comp_from_clip`, `import_fusion_comp_to_clip`.
- **Color:** `set_cdl` (slope/offset/power + saturation), `get_node_graph`.
- **Render:** `get_render_formats`, `set_render_format`, `set_render_settings`,
  `add_render_job`, `get_render_job_status`. (Queue only — do not start without consent.)

## Hard limitations and the validated workarounds

1. **Keyframe animation — not available.** The API takes static values only. Anything
   animated (moving elements, push-ins, opacity ramps) must be done in the Fusion/Edit
   UI by a human, or produced upstream. Don't promise animated results through the API.
2. **Fusion node parameters — not settable.** You can manage compositions as objects
   (add/import/export/load/delete/rename) but not the values inside them. A `Text+`
   title you insert will read `"Custom Title"`. **Workaround (validated):**
   `export_fusion_comp_from_clip` → edit the `StyledText` field in the exported `.comp`
   file → `import_fusion_comp_to_clip`.
3. **`insert_title` ripples V1** and cannot pick a track — it pushes existing clips
   right. Insert titles *before* laying clips, or account for the shift afterwards.
4. **Video background removal** is CPU-bound and slow; only available with the AI extras
   installed.
5. **Gallery stills** require the timeline to be on the **Color** page.

## Bridge lifecycle (a real gotcha)

The bridge runs in `fuscript`, which **can outlive Resolve** and keep the port `9876`
open. A leftover instance is inert — its Resolve handle is dead, so it can't read or
touch projects — and it's still hardened. But if a fresh bridge won't bind or behaves
oddly, suspect a zombie. Clean shutdown:

```
POST http://127.0.0.1:9876/bridge/shutdown
```

Starting the bridge also self-shuts a previous instance (hot-reload), so re-launching
from the Scripts menu usually clears a stuck one.

## Security you should know about

This server rejects requests that carry an `Origin` header or a non-loopback `Host`
(returns `403`). A local MCP client (like you, via the server process) is unaffected; a
browser cannot drive the bridge. Don't try to work around this — it's intentional. You
can verify the guard any time with `python scripts/check_hardening.py`.
