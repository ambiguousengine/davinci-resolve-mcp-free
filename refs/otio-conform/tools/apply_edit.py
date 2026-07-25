"""apply_edit -- replay an intent ledger from a pinned base into a new Resolve version.

    from apply_edit import Pipeline
    p = Pipeline("Fall Prevention - TEST SEQUENCE")     # pins the base
    p.add("set_volume",   track=1, at=125,  db=-20)
    p.add("set_crop",     track=1, at=2878, top=0.25, bottom=0.25)
    print(p.apply(slug="mix_pass").report())

=============================================================================
WHY REPLAY-FROM-BASE, NOT READ-MODIFY-WRITE
=============================================================================
The base is a pinned duplicate of the locked offline and is NEVER mutated. The agent
holds an ordered ledger of intents; every apply() is `base + the WHOLE ledger -> a new
version`.

That buys three things read-modify-write cannot:
  * round-trip loss is bounded at exactly ONE trip forever, however many revisions;
  * revising means editing a ledger entry and replaying, not undoing;
  * undo is free -- switch back to the previous version and delete the bad one
    (Resolve exposes no scripted undo at all).

The round trip is also measured IDEMPOTENT -- it reaches a fixed point after two
passes with zero effect parameters lost or gained -- so replay is safe to repeat.

=============================================================================
THE ONE WAY A USER LOSES WORK -- SAY THIS IN THE SKILL
=============================================================================
Hand-tweaking inside a GENERATED version and then letting a replay run over it. The
replay rebuilds from base and the hand work is gone. If that happens, `rebaseline()`
pins the tweaked version as the new base and clears the ledger.

=============================================================================
SAFETY
=============================================================================
* The base timeline is opened READ-ONLY. Every write lands on a NEW timeline.
* Pre-flight runs first and RED refuses by default -- a silent failure on a lock is
  exactly the damage this pipeline exists to prevent. `force=True` names what it is
  overriding.
* Fusion titles are detected and refused rather than silently mangled. The
  placeholder+comp-restore workaround is proven on ONE static title only; wiring it in
  here without a multi-title test would be trusting an untested path with a lock.
"""
import json
import os
import re
import time as _time
import urllib.request

import otio_edit
import preflight

BASE_URL = "http://127.0.0.1:9876"


def _res(payload):
    """The HTTP bridge returns payloads UNWRAPPED; only the MCP layer adds "result".
    Accept either so this works whichever surface it is pointed at."""
    if isinstance(payload, dict) and "result" in payload and len(payload) == 1:
        return payload["result"]
    return payload


def call(path, body=None, timeout=180):
    req = (urllib.request.Request(BASE_URL + path) if body is None else
           urllib.request.Request(BASE_URL + path, data=json.dumps(body).encode(),
                                  headers={"Content-Type": "application/json"}))
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


class PipelineError(Exception):
    pass


class Pipeline:
    def __init__(self, base_name, work_dir=None, verbose=True):
        self.base_name = base_name
        self.work_dir = work_dir or os.path.join(os.path.expanduser("~"), ".otio_conform")
        os.makedirs(self.work_dir, exist_ok=True)
        self.ledger = []
        self.verbose = verbose
        self.versions = []
        self._last = None
        self.base_index = self._find_timeline(base_name)
        if self.base_index is None:
            raise PipelineError("no timeline named %r in the open project" % base_name)

    # ---- bridge helpers ---------------------------------------------------
    def _say(self, msg):
        if self.verbose:
            print(msg)

    def _find_timeline(self, name):
        """The bridge switches by INDEX only, so resolve a name to its index."""
        total = _res(call("/project")).get("timelineCount", 0)
        for i in range(1, total + 1):
            call("/timeline/switch", {"index": i})
            if _res(call("/timeline")).get("name") == name:
                return i
        return None

    def _export(self, dest):
        r = _res(call("/timeline/export", {"fileName": dest, "exportType": "OTIO"}))
        if not r.get("success"):
            raise PipelineError("export failed: %s" % r)
        return dest

    def _import(self, src, name):
        r = call("/mediapool/timeline/import",
                 {"filePath": src, "importOptions": {"timelineName": name}})
        r = _res(r)
        if not r.get("success"):
            raise PipelineError("import failed: %s" % r)
        # DO NOT trust the returned name. Observed returning the *base* timeline's name
        # after a successful import, which then made verify() export and check the WRONG
        # timeline and report a clean run as 8 failures. Confirm the new timeline exists
        # by finding it ourselves.
        if self._find_timeline(name) is None:
            raise PipelineError(
                "import reported success but no timeline named %r exists. Bridge said: %s"
                % (name, r))
        return name

    def _switch_to(self, name):
        """Make `name` current, and PROVE it. Never assume a switch landed."""
        if _res(call("/timeline")).get("name") != name:
            idx = self._find_timeline(name)
            if idx is None:
                raise PipelineError("no timeline named %r" % name)
            call("/timeline/switch", {"index": idx})
        actual = _res(call("/timeline")).get("name")
        if actual != name:
            raise PipelineError("wanted timeline %r but %r is current" % (name, actual))

    # ---- ledger -----------------------------------------------------------
    def add(self, verb, **kwargs):
        if not hasattr(otio_edit.Edit, verb):
            raise PipelineError("no such verb %r. Available: %s" % (
                verb, ", ".join(sorted(v for v in dir(otio_edit.Edit)
                                       if not v.startswith("_") and
                                       v not in ("save", "summary")))))
        self.ledger.append({"verb": verb, "args": kwargs})
        return self

    def revise(self, i, **kwargs):
        """Edit a ledger entry in place. The next apply() replays it from base."""
        self.ledger[i]["args"].update(kwargs)
        return self

    def drop(self, i):
        self.ledger.pop(i)
        return self

    # ---- the pipeline -----------------------------------------------------
    def apply(self, slug="edit", force=False):
        if not self.ledger:
            raise PipelineError("ledger is empty -- nothing to apply")
        # v{NNN}_{slug}_{HHMM}. The time suffix is NOT decoration: a fresh Pipeline
        # restarts numbering at 001, and importing onto an existing timeline name fails
        # outright. Bump further if the name is somehow still taken.
        n = len(self.versions) + 1
        stem = re.sub(r"[^A-Za-z0-9_]+", "_", slug)[:32]
        version = "v%03d_%s_%s" % (n, stem, _time.strftime("%H%M"))
        while self._find_timeline(version) is not None:
            n += 1
            version = "v%03d_%s_%s" % (n, stem, _time.strftime("%H%M"))
        base_otio = os.path.join(self.work_dir, "base.otio")
        out_otio = os.path.join(self.work_dir, "%s.otio" % version)

        # 1. export the pinned base, untouched
        call("/timeline/switch", {"index": self.base_index})
        self._export(base_otio)
        self._say("base exported: %s" % self.base_name)

        # 2. pre-flight -- refuse RED rather than damage a lock
        rc = preflight.scan(base_otio)
        if rc == 2 and not force:
            raise PipelineError(
                "PRE-FLIGHT RED -- refusing. See the report above. If a Fusion title is "
                "the blocker, the placeholder workaround is proven on ONE static title "
                "only and is not wired in here. force=True to override, naming the risk.")
        if rc == 3:
            raise PipelineError("pre-flight could not read the base export")

        # 3. replay the WHOLE ledger from base
        e = otio_edit.Edit(base_otio)
        applied, failed = [], []
        for i, entry in enumerate(self.ledger):
            try:
                getattr(e, entry["verb"])(**entry["args"])
                applied.append(i)
            except otio_edit.EditError as exc:
                failed.append((i, entry, str(exc)))
        if failed:
            lines = ["ledger entry %d (%s) -> %s" % (i, en["verb"], msg)
                     for i, en, msg in failed]
            raise PipelineError(
                "%d of %d ledger entries could not be applied; NOTHING was written.\n  %s"
                % (len(failed), len(self.ledger), "\n  ".join(lines)))
        e.save(out_otio)
        self._say("ledger replayed: %d intent(s)" % len(applied))

        # 4. import as a NEW version -- canonical call, defaults only
        made = self._import(out_otio, version)
        self.versions.append(made)
        self._last = {"version": made, "otio": out_otio, "base_otio": base_otio,
                      "intents": len(self.ledger), "preflight": rc}
        self._say("created version: %s" % made)
        return self

    def verify(self):
        """Re-export the new version and confirm every authored value actually landed.

        A write verb is NOT verified by its own return value -- the import reports
        success even when it silently discards parameters. This re-reads what Resolve
        actually holds, which is a different channel.
        """
        if not self._last:
            raise PipelineError("nothing applied yet")
        self._switch_to(self._last["version"])          # proves it, or raises
        rt = os.path.join(self.work_dir, "%s_verify.otio" % self._last["version"])
        self._export(rt)
        authored = _live_params(self._last["otio"])
        landed = _live_params(rt)
        missing = {k: v for k, v in authored.items() if not _same(landed.get(k), v)}
        self._last["verified"] = not missing
        self._last["missing"] = missing
        return self

    def rebaseline(self):
        """Pin the CURRENT version as the new base and clear the ledger.

        Use after hand-tweaking a generated version -- otherwise the next replay
        rebuilds from the old base and the hand work is lost.
        """
        if not self._last:
            raise PipelineError("nothing applied yet")
        self.base_name = self._last["version"]
        self.base_index = self._find_timeline(self.base_name)
        self.ledger = []
        self._say("re-baselined on %s; ledger cleared" % self.base_name)
        return self

    def report(self):
        if not self._last:
            return "nothing applied yet"
        L = self._last
        tag = {0: "GREEN", 1: "AMBER", 2: "RED (forced)"}.get(L["preflight"], "?")
        lines = ["", "=" * 64,
                 "RECEIPT  %s" % L["version"], "=" * 64,
                 "  base        : %s (never mutated)" % self.base_name,
                 "  pre-flight  : %s" % tag,
                 "  intents     : %d replayed from base" % L["intents"],
                 "  otio        : %s" % L["otio"]]
        if "verified" in L:
            lines.append("  verified    : %s" % ("ALL landed" if L["verified"]
                                                 else "%d MISSING" % len(L["missing"])))
            for k, v in list(L.get("missing", {}).items())[:8]:
                lines.append("      MISSING %s = %r" % (k, v))
        else:
            lines.append("  verified    : NOT RUN -- call .verify()")
        lines += ["", "  undo: switch to the previous version and delete this one.",
                  "  WARNING: hand edits made inside %s are LOST on the next apply()"
                  % L["version"], "           unless you call .rebaseline() first.", "=" * 64]
        return "\n".join(lines)


def _same(a, b, rel=1e-9):
    """Compare with a tolerance -- exact float equality is the WRONG test here.

    The round trip perturbs floats in the last bit or two: 16 `transformationTilt`
    values shifted by ~5e-17 on the first pass, converging to a fixed point by the
    third. That is float64 epsilon, not drift -- 5e-17 of a tilt is ~1e-13 of a pixel.
    An exact-equality verifier flags all of it and reports a perfect run as 13 failures,
    which is a checker that cries wolf. 1e-9 is still far tighter than any value an
    editor could set or perceive.
    """
    if a is None or b is None:
        return a == b
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        if isinstance(a, bool) or isinstance(b, bool):
            return a == b
        return abs(a - b) <= rel * max(1.0, abs(a), abs(b))
    return a == b


def _live_params(path):
    """{(kind, track, pos, effect_type, param_id): value} for every off-default value."""
    with open(path, encoding="utf-8") as fh:
        doc = json.load(fh)
    out, n = {}, 0
    for tr in doc["tracks"]["children"]:
        kind = tr.get("kind")
        if kind == "Video":
            n += 1
        t = n if kind == "Video" else 1
        pos = 0
        for ch in tr.get("children", []):
            schema = str(ch.get("OTIO_SCHEMA", "")).split(".")[0]
            dur = ((ch.get("source_range") or {}).get("duration") or {}).get("value") or 0
            if schema == "Transition":
                out[(kind, t, pos, "transition", "in")] = (ch.get("in_offset") or {}).get("value")
                out[(kind, t, pos, "transition", "out")] = (ch.get("out_offset") or {}).get("value")
                continue
            if schema != "Gap":
                for fx in ch.get("effects", []) or []:
                    em = (fx.get("metadata") or {}).get("Resolve_OTIO", {})
                    for q in em.get("Parameters", []) or []:
                        if ("Default Parameter Value" in q
                                and q.get("Parameter Value") != q["Default Parameter Value"]):
                            out[(kind, t, pos, em.get("Type"), q["Parameter ID"])] = \
                                q["Parameter Value"]
            pos += dur
    return out
