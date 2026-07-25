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
| `ref4_retime_50pct.otio` | **retime shape** — `LinearTimeWarp.1` / `time_scalar`, a **standard OTIO schema outside the `Resolve_OTIO` block**. Note `Retime and Scaling` (Type 22) stays `"Parameters": []` even here |
| `ref5_speedramp_stepped.otio` | **speed ramp** — `TimeEffect.1`, keyframes of `[timeline_s, source_s, smooth?, in_x, in_y, out_x, out_y]` |
| `ref6_speedramp_eased.otio` | **eased ramp** — the same, with cubic Bezier tangent handles populated. Slope `dy/dx` is the instantaneous speed |
| `ref7_clip_audio_params.otio` | **every Fairlight clip parameter** — pan, pitch, EQ band and an audio fade handle, all set off-default so their IDs serialise |

### Fairlight clip parameters — IDs, types and units

| effect | Type | Parameter ID | type | range | unit trap |
|---|---|---|---|---|---|
| Clip Volume and Fades | 62 | `volume` | Double | −100 … 30 | dB |
| | | `faderOut` | Double | 0 … 2^31 | **frames** — not seconds, not dB |
| Clip Pan | 72 | `pan` | Double | −100 … 100 | negative = left |
| Clip Pitch | 67 | `semiTones` | Int | −24 … 24 | |
| Equaliser Band | 63 | `eq band index` | Int | −1 … 5 | six slots; index is its own default |
| | | `eq frequency` | Int | 20 … 19000 | Hz |
| | | `eq dB gain` | Int | −700 … 240 | **tenths of a dB** — 79 = +7.9 dB |
| | | `eq qFactor` | Int | 300 … 100000 | **thousandths** — 750 = Q 0.75 |

Measured, not just read back: `pan: -50` gives **L−R = +9.50 dB** where the control
measures exactly 0.00; `faderOut: 427` gives tail decay of **−10.5 dB** then
**−17.6 dB**, beginning exactly where 427 frames at 50 fps predicts. `semiTones` and
the EQ values are read-back only so far. `faderIn` and a `cents` parameter are
inferred, not observed — set each once rather than guessing the spelling.

## worked-mutations/ — authored by hand, all verified applied

| file | mutation | verified by |
|---|---|---|
| `mut_vol20.otio` | volume −10 → −20 dB | render + `volumedetect`: exactly −10.0 dB delta |
| `mut_duck.otio` | keyframed duck 0 → −15 dB → 0 | 3/3 windows exact to 0.1 dB |
| `mut_dissolve20.otio` | dissolve 100 → 20 frames | Resolve reports duration 20; re-export `in=10 out=10` |
| `torture_placeholder.otio` | title → real-media placeholder | import succeeds where the original hard-fails |
| `torture_full_flow.otio` | placeholder + authored 50 f video fade | title pixel-identical AND fade applied (ratio 0.4842 vs 0.5 predicted) |
| `mut_retime25.otio` | speed 100% → 25%, added from scratch | frame at 20 s **byte-identical** (same MD5) to the unretimed timeline at 5 s, with a control proving the three comparison frames differ from each other |
| `mut_speedramp.otio` | 100% / 25% / 100% ramp, added from scratch | all three segments land byte-identical at their predicted source times |
| `mut_speedramp_eased.otio` | hand-edited Bezier tangents on an eased ramp | cubic Bezier predicts source 2.438 s at timeline 18 s; **measured 2.4 s**, next-nearest sample 15× worse |

**⚠️ Tangent handles are ignored on the first and last keyframes** — ease shaping only
takes on interior ones. A 2-keyframe ramp with handles on both endpoints renders
exactly linear (`mean|diff| = 0.000` vs the unretimed reference). Add an interior
keyframe if a ramp must ease at its very start or end.

## reference-exports/aaf/ — Resolve's own AAF export (added 25 Jul)

| file | what it shows |
|---|---|
| `T06_clipvol_minus20.aaf` | **constant clip gain** — `Audio Gain` → `ConstantValue` `Amplitude`, **linear** rational. −20 dB → `53687091/536870912` = 0.100000, exact |
| `T07_duck_keyframed.aaf` | **keyframed clip gain** — `Audio Gain` → `VaryingValue` `Amplitude` + `PointList`. Time is **normalised 0..1** over the segment; values are linear |
| `T20_AAFimport_duck.otio` | the same duck after `AAF → Resolve → OTIO`. Proves Resolve **imports** the automation. Note interior keyframes land **one frame early** (250/300/900/950 → 249/299/899/949), and `videoFaderOut` is **gone** |

Verified by measurement, not read-back: rendered and `ffmpeg volumedetect` per window
against the matched control — **+10.0 / −5.0 / +10.0 dB, 3/3 windows exact**.

## tools/

| file | what it does |
|---|---|
| `dump_aaf.py` | walks an AAF's mobs and slots, printing every `ConstantValue` / `VaryingValue` parameter and its control points. This is the reader the conform lane needs — `reel-forge/tools/aaf_audio.py` cannot open a Resolve AAF at all (it dies resolving *linked* essence long before it reaches `clip_gain()`) |
| `otio_effects.py` | prints every **non-default** effect parameter per clip. Effects live at `clip["effects"][n]["metadata"]["Resolve_OTIO"]` — **not** on the clip's own metadata block, which is the easy place to look and find nothing |
| `bridge.py` | 20-line HTTP client for the bridge. Use it rather than curl: Windows paths do not survive JSON quoting through a shell, and the MCP wrapper's argument names drift from the HTTP routes (`file_name` vs `fileName`) |

## The other two things to remember

**Resolve only serialises a parameter once it has been moved off its default in the
UI.** `Fairlight Clip Pan` (72), `Pitch` (67) and `Equaliser` (64) all export as
`"Parameters": []` on a clean clip — present, `Enabled`, and unauthorable.
`GetProperty()` on an audio item returns `{}`, so the live API is no help either.
Every new verb therefore costs exactly one UI gesture: set it once, export, read the
ID, author freely from then on.

**But an empty `Resolve_OTIO` slot does not mean the feature is absent.** Retime is
the counter-example: `Retime and Scaling` (Type 22) reports `"Parameters": []` on a
clip that is demonstrably retimed, because the speed lives in a standard
`LinearTimeWarp.1` effect outside the Resolve metadata block. Reading only the
`Resolve_OTIO` block is what produced the wrong "retime is UI-only" call. **Dump the
clip's whole JSON before declaring anything unauthorable.**

## Track state — an adapter limit, not a format limit

Say which layer a claim is about. **OTIO the format** gives every track an open
`metadata` dict and can carry arbitrary serialisable state. **Resolve's OTIO adapter**
is the actual constraint, and it is stricter than it looks:

- It writes exactly `{"Audio Type", "Locked", "SoloOn"}` on an audio track — including
  when `Locked`/`SoloOn` hold their default `false`. A fixed key set, with no volume in
  it. A track carrying a genuinely non-default **+0.2 dB** fader still exported those
  three keys and nothing more.
- It **discards unknown keys on import.** Eight spellings (`Volume`, `Fader`, `Level`,
  `Gain`, `Track Volume`, `Track Level`, `volume`, `fader`) injected at −20.0 came back
  from a re-export completely absent, and the render measured **+0.00 dB in 3/3
  windows** against the control.

Practical consequence: **a timeline carrying mix state cannot be round-tripped through
Resolve's OTIO without losing it** — and custom metadata is not a workaround, because
it does not survive either.

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
