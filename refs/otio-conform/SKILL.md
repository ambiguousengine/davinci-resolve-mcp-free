# SKILL — Edit a locked DaVinci Resolve timeline by intent

**Trigger this when Navigator asks for a change to an existing Resolve cut** — however
phrased: *"drop the music under the VO"*, *"make that dissolve longer"*, *"punch in on
that shot"*, *"fade the end card"*, *"take 6 dB off the bed"*, *"crop the top off shot 4"*,
*"halve the speed on that clip"*.

**Do NOT use this to build a timeline from nothing** — that is assembly, and
`AppendToTimeline` or a fresh OTIO import does it directly.

---

## The one-paragraph model

Resolve's scripting API cannot set transitions, audio levels, fades or keyframes. So we
don't ask it to. Instead: **export the timeline to OpenTimelineIO, edit the file, import
it back as a new version.** The base timeline is pinned and never touched; the agent
holds an ordered **ledger of intents**; every run replays the whole ledger from base into
a new version. Loss is bounded at exactly one round trip no matter how many revisions,
and undo is switching version and deleting.

---

## Before you touch anything

1. **Bridge up.** `get_resolve_status` → `connected: true`. If not, ask Navigator to run
   the CursorBridge script inside Resolve (Workspace ▸ Scripts ▸ CursorBridge).
2. **Right project open.** `get_project_list` / `load_project`.
3. **Read the base's name exactly.** Timeline names are matched literally.

## The workflow

```python
import sys; sys.path.insert(0, r"F:\AMBIGUITY\TOOLS\davinci-bridge\refs\otio-conform\tools")
from apply_edit import Pipeline

p = Pipeline("Fall Prevention - TEST SEQUENCE")      # pins the base, read-only
p.add("set_volume",     track=1, at=125,  db=-12)    # `at` = TIMELINE FRAME
p.add("set_crop",       track=1, at=2878, top=0.2, bottom=0.2)
p.add("set_video_fade", track=1, at=5579, out_frames=40)
p.apply(slug="mix_pass").verify()
print(p.report())                                    # the receipt — always show this
```

Revising is **not** undo — edit the intent and replay:

```python
p.revise(0, db=-6).apply(slug="quieter").verify()
```

### Always

- **Show Navigator the receipt.** It names the base, the pre-flight verdict, the intent
  count, and whether every value actually landed.
- **`.verify()` before reporting success.** The import returns success even when it
  silently discards parameters. `verify()` re-exports and re-reads what Resolve actually
  holds, which is a different channel from the write's own return value.

---

## Verbs — all proven by measurement, never by read-back

| verb | units | evidence |
|---|---|---|
| `set_volume(track, at, db)` | dB | −20.00 dB exact in 4/4 windows on a real cut |
| `set_audio_fade(track, at, out_frames, in_frames)` | **frames** | tail decay measured at the predicted frame |
| `set_pan(track, at, pan)` | −100…100, −ve = left | L−R = +9.50 dB at pan −50 |
| `set_pitch(track, at, semitones)` | −24…24 | +12 shifts every ⅓-octave band to exactly 2f |
| `set_eq_band(track, at, band, freq_hz, gain_db, q)` | real units, converted | +23.08 dB peak at exactly the authored 1000 Hz |
| `set_video_fade(track, at, out_frames, in_frames)` | **frames** | 0.4843 vs 0.5 predicted (synthetic gave 0.4842) |
| `set_transform(track, at, zoom, pan, tilt, rotation)` | zoom is a **factor** | proven on a real cut |
| `set_crop(track, at, left, right, top, bottom, softness)` | 0…1 fraction | 274 black rows measured vs 274 predicted |
| `retime(track, at, speed)` | 0.5 = half | **byte-identical** at the predicted source frame |
| `set_transition(track, at, in_frames, out_frames)` | frames | 12→25 f, confirmed mid-dissolve by pixel |

**Colour is a different lane.** `set_cdl`, `set_lut`, `apply_grade_from_drx`,
`copy_grades` are **live-API** verbs — call them directly on the clip, not through this
pipeline. The OTIO round trip **destroys grades**, so grade *after* the conform, or move
grades with `.drx` (which is byte-identical; a LUT is an interpolated approximation).

---

## The four traps. Every one of these cost real time.

**1. `Default Parameter Value` is load-bearing.**
Resolve discards a parameter whose value equals the default *you declare*. Declaring a
wrong default silently cancels your own edit and can drop the whole effect with it. This
produced a completely convincing, completely false *"EQ needs a UI click"* conclusion.
`otio_edit` guards it — it prefers the file's own default and refuses a no-op. **Never
hand-write a default; copy it from `reference-exports/ref10_clean_audio_base.otio`.**

**2. Clip index is NOT timeline position.** On a real cut, `clip_index 11` was frames
2878–3091 while position 3268 was `clip_index 13`. Every verb here takes `at=` in
**timeline frames**. Only the live-API colour verbs take a clip index — do not mix them up.

**3. A hidden sample point looks exactly like a broken verb.** Measuring a frame that an
upper video track covers gives control == edited and a ratio of exactly 1.0000. **Before
measuring anything, run:**
```
python tools/preflight.py --clear <file.otio> [track] [--tail]
```

**4. A render job stuck at `Ready` 0% means no mark in/out.** `SelectAllFrames: false`
renders the in/out range — and **importing a timeline clears the marks**, so on any
round-tripped timeline that range is empty. Use `SelectAllFrames: true`. A stuck job can
be neither started nor deleted and poisons the queue for everything after it.

---

## What this refuses, and why

- **Fusion titles → RED, refused.** The placeholder-substitution workaround is proven on
  **exactly one static title**. Multiple and animated titles are untested. Wiring an
  unproven path to a real lock is the damage this pipeline exists to prevent.
- **Compound clips are fine** — proven to round-trip with contents intact.
- `force=True` overrides a RED. Only use it after telling Navigator precisely what is
  being overridden and getting a yes.

## What the round trip costs, every time

- **Custom clip names revert to filenames** (20 clips on a real cut). Cosmetic, but it is
  an editor's work.
- **Timeline mark in/out is cleared.**
- **Grades are destroyed** — see the colour note above.

## The one way Navigator loses work

**Hand-tweaking inside a generated version, then letting a replay run over it.** The
replay rebuilds from base and the hand work is gone. If Navigator has touched a generated
version, call `p.rebaseline()` first — it pins that version as the new base and clears the
ledger. **Say this out loud when handing back a version.**

---

## Still untested — do not claim these work

Adjustment clips · Fusion *clips* (as opposed to titles) · multiple or animated titles ·
**speed ramps on off-rate clips** (ramps keyframe in *seconds*; only the linear retime is
proven off-rate) · colour versions, colour groups, node-graph authoring · `faderIn`
spelling · track/bus Fairlight processing (known NOT to survive OTIO — a limitation, not
a bug).

`clip_fade` in the bridge returns success while changing no pixels. **Do not use it** —
use `set_video_fade` here instead.

## Where the depth lives

- **Method, evidence, every measurement:** `F:\AMBIGUITY\TOOLS\_docs\FCPXML-CONFORM-RUNBOOK.md`
- **Authoring shapes and worked examples:** `refs/otio-conform/README.md`
- **Anything AAF:** `F:\AMBIGUITY\TOOLS\_docs\AAF-IMPORT.md`
- **Current status and what's next:** the project's Notion **STATE**

**Standing rule for this lane: a write verb is not verified by its own return value.**
Export the frame, or render and measure the decibels. Two bridge verbs have returned
success and a plausible read-back while changing nothing.
