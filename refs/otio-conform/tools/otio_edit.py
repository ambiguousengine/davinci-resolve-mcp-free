"""otio_edit -- author edits into a Resolve OTIO export.

    from otio_edit import Edit
    e = Edit("base.otio")
    e.set_volume(track=1, at=125, db=-20)
    e.set_transform(track=1, at=3268, zoom=2.0)
    e.set_crop(track=1, at=2878, top=0.25, bottom=0.25)
    e.retime(track=1, at=5579, speed=0.5)
    e.save("edited.otio")

Then import with the CANONICAL call -- defaults, timelineName only:
    import_timeline_from_file(path, {"timelineName": "..."})

=============================================================================
THE THREE THINGS THAT WILL BITE YOU. All three cost real time on 2026-07-25.
=============================================================================

1. `Default Parameter Value` IS LOAD-BEARING.
   Resolve compares your value against the default YOU DECLARE and **discards the
   parameter when they are equal** -- it reads as "already at default, nothing to
   apply". Declaring a wrong default silently cancels your own edit, and can take the
   whole effect down with it including its `Enabled` flag. That produced a completely
   convincing (and completely false) "EQ needs a UI click" conclusion.
   -> `_param` below always prefers the default already in the file, and REFUSES to
      write a value equal to its default rather than emitting a silent no-op.

2. CLIP INDEX IS NOT TIMELINE POSITION.
   On a real cut `clip_index 11` was timeline frames 2878-3091, while position 3268 was
   `clip_index 13`. Grading one and measuring the other produced byte-identical frames
   that looked like a dead verb. Every verb here addresses clips by **timeline position
   in frames** (`at=`), counted with gaps consumed and transitions skipped.

3. A HIDDEN SAMPLE POINT LOOKS EXACTLY LIKE A BROKEN VERB.
   Measuring a frame that an upper video track covers gives control == edited and a
   ratio of exactly 1.0000. Use `preflight.py --clear` to find exposed frames BEFORE
   measuring anything.

Verbs are only added here once measured against a control -- never on a read-back.
"""
import json

# Resolve effect Type ids, as they appear in metadata.Resolve_OTIO["Type"].
# NOTE the key is "Type". "Effect Type" silently yields None and every lookup misses.
FX = {
    "composite": 1, "transform": 2, "crop": 3, "video_faders": 36,
    "volume": 62, "eq_band": 63, "eq_master": 64, "pitch": 67, "pan": 72,
}

# Defaults observed in real exports. ONLY used when the parameter is absent from the
# file; if it is present, that file's own declared default always wins (per trap 1).
# `eq frequency` is deliberately absent -- its default differs per band (band 2 = 239),
# so guessing one would be exactly the mistake this table exists to avoid.
DEFAULTS = {
    "volume": 0.0, "faderIn": 0.0, "faderOut": 0.0, "pan": 0.0, "semiTones": 0,
    "videoFaderIn": 0.0, "videoFaderOut": 0.0,
    "transformationZoomX": 1.0, "transformationZoomY": 1.0,
    "transformationPan": 0.0, "transformationTilt": 0.0,
    "transformationRotationAngle": 0.0,
    "cropLeft": 0.0, "cropRight": 0.0, "cropTop": 0.0, "cropBottom": 0.0,
    "cropSoftness": 0.0,
    "eq dB gain": 0, "eq qFactor": 1000,
}

INT_PARAMS = {"semiTones", "eq band index", "eq frequency", "eq dB gain", "eq qFactor"}

# Default centre frequency per EQ band. OBSERVED, not derived: read off the Fairlight
# Inspector (B1-B6 = 97 / 117 / 239 / 1.2K / 6.0K / 19.0K) and corroborated by
# ref7_clip_audio_params.otio, where band index 2 declares default 239 -- which pins the
# mapping as band_index = B - 1. A file's OWN declared default always wins over this.
EQ_BAND_DEFAULT_HZ = {0: 97, 1: 117, 2: 239, 3: 1200, 4: 6000, 5: 19000}


class EditError(Exception):
    pass


class Edit:
    def __init__(self, path):
        with open(path, encoding="utf-8") as fh:
            self.doc = json.load(fh)
        self.log = []

    # ---- addressing -------------------------------------------------------
    def _clips(self, kind, track):
        """Yield (timeline_position, clip) for one track. Gaps consumed, transitions skipped."""
        n = 0
        for tr in self.doc["tracks"]["children"]:
            if tr.get("kind") != kind:
                continue
            n += 1
            if n != track:
                continue
            pos = 0
            for ch in tr.get("children", []):
                schema = str(ch.get("OTIO_SCHEMA", "")).split(".")[0]
                dur = ((ch.get("source_range") or {}).get("duration") or {}).get("value") or 0
                if schema == "Transition":
                    continue
                if schema != "Gap":
                    yield pos, ch
                pos += dur
            return
        raise EditError("no %s track %d" % (kind, track))

    def _clip(self, kind, track, at):
        for pos, ch in self._clips(kind, track):
            if pos == at:
                return ch
        near = sorted(p for p, _ in self._clips(kind, track))
        raise EditError("no clip at %s frame %s on %s track %d. Clips start at: %s"
                        % (kind, at, kind, track, near[:14]))

    def _fx(self, clip, type_id, enable=True):
        for e in clip.get("effects", []) or []:
            em = (e.get("metadata") or {}).get("Resolve_OTIO", {})
            if em.get("Type") == type_id:
                if enable:
                    em["Enabled"] = True          # honoured on import; safe to set
                return em
        raise EditError("clip %r has no effect slot Type %s" % (clip.get("name"), type_id))

    def _param(self, em, pid, value, fallback_default=None):
        """Set one parameter, keeping the file's own declared default (trap 1)."""
        for p in em.setdefault("Parameters", []):
            if p.get("Parameter ID") == pid:
                default = p.get("Default Parameter Value")
                if value == default:
                    raise EditError(
                        "%s=%r equals its default -- Resolve would DISCARD this parameter "
                        "(and can drop the whole effect with it). Refusing to emit a "
                        "silent no-op." % (pid, value))
                p["Parameter Value"] = value
                return
        default = DEFAULTS.get(pid, fallback_default)
        if default is None:
            raise EditError(
                "no observed default for %r and it is absent from this file. Copy the "
                "true default from a clip that already carries it -- do NOT invent one "
                "(see trap 1)." % pid)
        if value == default:
            raise EditError("%s=%r equals its default -- would be discarded." % (pid, value))
        em["Parameters"].append({
            "Parameter ID": pid, "Parameter Value": value,
            "Default Parameter Value": default,
            "Variant Type": "Int" if pid in INT_PARAMS else "Double"})

    def _do(self, kind, track, at, type_id, note, **params):
        clip = self._clip(kind, track, at)
        em = self._fx(clip, type_id)
        for pid, val in params.items():
            if val is not None:
                self._param(em, pid, val)
        self.log.append("%s %s@%s (%s): %s" % (note, kind[0].upper() + str(track), at,
                                               clip.get("name"), params))
        return self

    # ---- AUDIO (all measured) --------------------------------------------
    def set_volume(self, track, at, db):
        """Clip volume in dB. Measured -20.00 exact in 4/4 windows on a real cut."""
        return self._do("Audio", track, at, FX["volume"], "volume", volume=float(db))

    def set_audio_fade(self, track, at, out_frames=None, in_frames=None):
        """Audio fade handles. UNITS ARE FRAMES -- not seconds, not dB."""
        return self._do("Audio", track, at, FX["volume"], "audio-fade",
                        faderOut=None if out_frames is None else float(out_frames),
                        faderIn=None if in_frames is None else float(in_frames))

    def set_pan(self, track, at, pan):
        """-100..100, negative = left. Measured L-R = +9.50 dB at pan -50."""
        return self._do("Audio", track, at, FX["pan"], "pan", pan=float(pan))

    def set_pitch(self, track, at, semitones):
        """-24..24. Measured: +12 shifts every 1/3-octave band to exactly 2f."""
        return self._do("Audio", track, at, FX["pitch"], "pitch", semiTones=int(semitones))

    def set_eq_band(self, track, at, band, freq_hz=None, gain_db=None, q=None):
        """EQ band 0-5. Measured: +23.08 dB peak at exactly the authored 1000 Hz.

        UNIT TRAPS: gain is TENTHS of a dB (+7.9 dB -> 79); q is THOUSANDTHS (Q 0.75 -> 750).
        This helper takes real-world units and converts. It also enables the Type 64
        master, which ships DISABLED -- band values alone do nothing without it.
        """
        clip = self._clip("Audio", track, at)
        self._fx(clip, FX["eq_master"])                     # master switch ON
        for e in clip.get("effects", []) or []:
            em = (e.get("metadata") or {}).get("Resolve_OTIO", {})
            if em.get("Type") != FX["eq_band"]:
                continue
            idx = [p for p in em.get("Parameters", []) or []
                   if p.get("Parameter ID") == "eq band index"]
            if idx and idx[0].get("Parameter Value") == band:
                em["Enabled"] = True
                if freq_hz is not None:
                    # This band's default centre frequency, used only if the file does
                    # not already declare one (which it does once the band is touched).
                    self._param(em, "eq frequency", int(freq_hz),
                                fallback_default=EQ_BAND_DEFAULT_HZ.get(band))
                if gain_db is not None:
                    self._param(em, "eq dB gain", int(round(gain_db * 10)))
                if q is not None:
                    self._param(em, "eq qFactor", int(round(q * 1000)))
                self.log.append("eq A%s@%s band %s: %sHz %sdB Q%s"
                                % (track, at, band, freq_hz, gain_db, q))
                return self
        raise EditError("no EQ band %s on that clip (bands are 0-5)" % band)

    # ---- VIDEO (all measured) --------------------------------------------
    def set_video_fade(self, track, at, out_frames=None, in_frames=None):
        """Video fade in FRAMES. Measured 0.4843 vs 0.5 predicted at the midpoint --
        consistent with the synthetic 0.4842, so ~0.484 is the curve's real shape."""
        return self._do("Video", track, at, FX["video_faders"], "video-fade",
                        videoFaderOut=None if out_frames is None else float(out_frames),
                        videoFaderIn=None if in_frames is None else float(in_frames))

    def set_transform(self, track, at, zoom=None, pan=None, tilt=None, rotation=None):
        """Zoom is a FACTOR (1.0 = 100%). Proven on a real cut.

        Read the clip's CURRENT value before predicting a result -- a clip already at
        zoom 1.58 is not at 1.0, and assuming otherwise invalidates the prediction.
        """
        return self._do("Video", track, at, FX["transform"], "transform",
                        transformationZoomX=zoom, transformationZoomY=zoom,
                        transformationPan=pan, transformationTilt=tilt,
                        transformationRotationAngle=rotation)

    def set_crop(self, track, at, left=None, right=None, top=None, bottom=None,
                 softness=None):
        """Crop as a 0..1 fraction of the frame. Measured 274 black rows vs 274 predicted.

        CROP IS APPLIED IN SOURCE SPACE, BEFORE the clip's Transform. A crop that shows
        no black band may still be correct -- a tilt can push it off frame.
        """
        return self._do("Video", track, at, FX["crop"], "crop",
                        cropLeft=left, cropRight=right, cropTop=top,
                        cropBottom=bottom, cropSoftness=softness)

    def retime(self, track, at, speed):
        """Linear retime. speed 0.5 = half speed. Does NOT change clip length.

        Proven BYTE-IDENTICAL at the predicted source frame, including on an off-rate
        clip (30 fps source in a 25 fps timeline).
        WARNING: retime does NOT carry the linked audio -- video ripples, audio does not.
        """
        clip = self._clip("Video", track, at)
        clip.setdefault("effects", []).append({
            "OTIO_SCHEMA": "LinearTimeWarp.1", "metadata": {},
            "name": "", "effect_name": "", "time_scalar": float(speed)})
        self.log.append("retime V%s@%s -> %sx" % (track, at, speed))
        return self

    def set_transition(self, track, at, in_frames=None, out_frames=None):
        """Change a dissolve's length. `at` is the transition's own timeline position.

        Measured: 12 -> 25 frames, still mid-dissolve where the short one had ended.
        """
        n = 0
        for tr in self.doc["tracks"]["children"]:
            if tr.get("kind") != "Video":
                continue
            n += 1
            if n != track:
                continue
            pos = 0
            for ch in tr.get("children", []):
                schema = str(ch.get("OTIO_SCHEMA", "")).split(".")[0]
                if schema == "Transition":
                    if pos == at:
                        if in_frames is not None:
                            ch["in_offset"]["value"] = float(in_frames)
                        if out_frames is not None:
                            ch["out_offset"]["value"] = float(out_frames)
                        self.log.append("transition V%s@%s in=%s out=%s"
                                        % (track, at, in_frames, out_frames))
                        return self
                    continue
                pos += ((ch.get("source_range") or {}).get("duration") or {}).get("value") or 0
        raise EditError("no transition at V%d frame %s" % (track, at))

    # ---- output -----------------------------------------------------------
    def save(self, path):
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(self.doc, fh, indent=2)
        return path

    def summary(self):
        return "\n".join("  * " + x for x in self.log) or "  (no edits)"
