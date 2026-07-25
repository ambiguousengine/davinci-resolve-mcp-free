# OTIO conform — reference exports & worked mutations

Evidence and authoring templates from the 2026-07-25 Phase A gate tests.
**Full write-up and method:** `F:\AMBIGUITY\TOOLS\_docs\FCPXML-CONFORM-RUNBOOK.md`

Captured from project `FCPXML test` (Resolve Studio 21.0.2.4, bridge 2.1.0),
1080p **50 fps**, source `E:\SHOWREEL\NBC UNIVERSAL\more 13th st\1128797.mxf`.

## reference-exports/ — ground truth, exported from Resolve

| file | what it shows |
|---|---|
| `base.otio` | clean single-clip baseline; effect slots present with empty `Parameters` |
| `ref2.otio` | **transition shape** — `Transition.1`, `SMPTE_Dissolve`, `in_offset`+`out_offset` |
| `ref3_clipvol.otio` | **clip volume shape** — `Fairlight Clip Volume and Fades` → `Parameter ID: "volume"`, dB, range −100..+30 |
| `torture_ref.otio` | **the title blocker** — Fusion title as `Clip.2` with `MissingReference.1` |

## worked-mutations/ — authored by hand, all verified applied

| file | mutation | verified by |
|---|---|---|
| `mut_vol20.otio` | volume −10 → −20 dB | render + `volumedetect`: exactly −10.0 dB delta |
| `mut_duck.otio` | keyframed duck 0 → −15 dB → 0 | 3/3 windows exact to 0.1 dB |
| `mut_dissolve20.otio` | dissolve 100 → 20 frames | Resolve reports duration 20; re-export `in=10 out=10` |
| `torture_placeholder.otio` | title → real-media placeholder | import succeeds where the original hard-fails |
| `torture_full_flow.otio` | placeholder + authored 50 f video fade | title pixel-identical AND fade applied (ratio 0.4842 vs 0.5 predicted) |

## Other files

- `title_saved.comp` — Fusion title comp extracted via `export_fusion_comp_from_clip`.
  `TextPlus` → `MediaOut`, **no `MediaIn`** — which is why it renders correctly when
  re-attached to any placeholder clip. This is the artifact the title workaround
  depends on.
- `measure_duck.py` — the audio measurement harness (ffmpeg `volumedetect` per window
  against a matched control render). Reusable for any level verification.

## The one thing to remember

Import with the **defaults** — `import_timeline_from_file(path, {"timelineName": ...})`.
Setting `importSourceClips: false` is the intuitive choice for a round-trip and it is
exactly what produces a **Media Offline** timeline.
