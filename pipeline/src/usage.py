"""
usage.py — FYI usage tracker: per-run totals AND a GLOBAL append-only ledger.

PURELY INFORMATIONAL — it never gates or blocks anything; if anything here
fails it is swallowed.

TWO sinks, written together:

1. The run folder's `usage.json` — running TOTALS for one run (survives resumes;
   one run folder = one "session"). What `kalinga.py usage TOPIC` reads.

2. A GLOBAL append-only EVENT LEDGER (`~/.kalinga/usage.jsonl`, override with
   $KALINGA_USAGE_LOG) — ONE JSON line PER EVENT (every LLM call, every image/
   video/audio generation), stamped with time + project + channel + run. This is
   the cross-project spend log: multiple projects/channels generating at the same
   time all append to the SAME file, so total token + credit spend is visible in
   one place instead of scattered across per-run usage.json files. Read it with
   `kalinga.py usage --global` (or tail the jsonl directly).

LLM tokens are EXACT on the api backend (from the SDK) and ESTIMATED on the cli
backend (char/4, flagged `approx`, since `claude -p` text mode reports no usage
and we keep that proven call untouched). Higgsfield credits = the account-balance
delta (credits_start → now); generation count is exact.

Dependency-free (json + pathlib + os + datetime) so any module can import it
without cycles.
"""
from __future__ import annotations
import json
import os
from datetime import datetime
from pathlib import Path

_state = {"llm": {}, "hf": {"generations": 0, "credits_start": None}}
_path = None
_ctx = {"project": None, "channel": None, "run": None}    # global-ledger context


# ---- global append-only event ledger ---------------------------------------
def ledger_path() -> Path:
    """The cross-project event log. $KALINGA_USAGE_LOG wins, else
    ~/.kalinga/usage.jsonl."""
    p = os.environ.get("KALINGA_USAGE_LOG")
    return Path(p).expanduser() if p else Path.home() / ".kalinga" / "usage.jsonl"


def _set_ctx(folder) -> None:
    """Derive project/channel/run from a run-folder path:
    .../<project>/channels/<channel>/output/<run>/. Best-effort; any part may
    stay None for a non-standard path."""
    try:
        folder = Path(folder)
        _ctx["run"] = folder.name
        parts = folder.parts
        if "channels" in parts:
            i = parts.index("channels")
            _ctx["channel"] = parts[i + 1] if i + 1 < len(parts) else None
            _ctx["project"] = parts[i - 1] if i >= 1 else None
        else:
            _ctx["channel"] = folder.parent.parent.name
            _ctx["project"] = folder.parent.parent.parent.name
        if os.environ.get("KALINGA_PROJECT"):
            _ctx["project"] = os.environ["KALINGA_PROJECT"]
    except Exception:        # noqa: BLE001
        pass


def _log_event(event: str, **fields) -> None:
    """Append ONE event line to the global ledger. Never raises."""
    try:
        rec = {"ts": datetime.now().isoformat(timespec="seconds"),
               "event": event, "project": _ctx.get("project"),
               "channel": _ctx.get("channel"), "run": _ctx.get("run")}
        rec.update({k: v for k, v in fields.items() if v is not None})
        p = ledger_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a") as f:
            f.write(json.dumps(rec) + "\n")
    except Exception:        # noqa: BLE001 — FYI only, never break a real call
        pass


def log_event(event: str, **fields) -> None:
    """Public hook so any module can drop a custom spend event into the ledger
    (e.g. a third-party API call) without touching usage internals."""
    _log_event(event, **fields)


def _save() -> None:
    if _path is None:
        return
    try:
        _path.write_text(json.dumps(_state, indent=2))
    except OSError:
        pass


def bind(folder, credits_start=None) -> None:
    """Point the tracker at a run folder's usage.json (loading prior totals so
    resumes accumulate). Call once, early, before any LLM/generation work."""
    global _path, _state
    _path = Path(folder) / "usage.json"
    _set_ctx(folder)
    if _path.exists():
        try:
            loaded = json.loads(_path.read_text())
            if isinstance(loaded, dict):
                _state = loaded
        except ValueError:
            pass
    _state.setdefault("llm", {})
    _state.setdefault("hf", {"generations": 0, "credits_start": None})
    if credits_start is not None and _state["hf"].get("credits_start") is None:
        _state["hf"]["credits_start"] = credits_start
    _save()


def set_credits_start(c) -> None:
    if c is not None and _state["hf"].get("credits_start") is None:
        _state["hf"]["credits_start"] = c
        _save()


def record_llm(model: str, input_tokens=0, output_tokens=0,
               approx: bool = False) -> None:
    try:
        m = _state["llm"].setdefault(
            model or "?", {"input": 0, "output": 0, "calls": 0, "approx": False})
        m["input"] += int(input_tokens or 0)
        m["output"] += int(output_tokens or 0)
        m["calls"] += 1
        m["approx"] = bool(m["approx"] or approx)
        _save()
    except Exception:        # noqa: BLE001 — FYI only, never break a real call
        pass
    _log_event("llm", model=model or "?", input=int(input_tokens or 0),
               output=int(output_tokens or 0), approx=bool(approx) or None)


def _gen_kind(model) -> str:
    """Best-effort image|video|audio|generation from the HF model id."""
    m = (model or "").lower()
    if any(k in m for k in ("seedance", "video", "kling", "veo", "runway")):
        return "video"
    if any(k in m for k in ("banana", "image", "flux", "sdxl", "imagen")):
        return "image"
    if any(k in m for k in ("inworld", "tts", "speech", "voice", "audio")):
        return "audio"
    return "generation"


def record_generation(n: int = 1, model=None, label=None, credits=None) -> None:
    """Count a billed Higgsfield generation in the run totals AND log it to the
    global ledger (image/video/audio inferred from the model id)."""
    try:
        _state["hf"]["generations"] = _state["hf"].get("generations", 0) + n
        _save()
    except Exception:        # noqa: BLE001
        pass
    _log_event("generate", media=_gen_kind(model), model=model, label=label,
               n=n if n != 1 else None, credits=credits)


def totals() -> dict:
    tin = sum(m.get("input", 0) for m in _state["llm"].values())
    tout = sum(m.get("output", 0) for m in _state["llm"].values())
    return {"llm_input": tin, "llm_output": tout, "llm_total": tin + tout,
            "generations": _state["hf"].get("generations", 0),
            "credits_start": _state["hf"].get("credits_start")}


def summary_lines(credits_now=None) -> list:
    """Human-readable FYI lines (empty when nothing tracked yet)."""
    lines = []
    llm = _state.get("llm", {})
    if llm:
        tin = sum(m.get("input", 0) for m in llm.values())
        tout = sum(m.get("output", 0) for m in llm.values())
        lines.append(f"LLM tokens: {tin + tout:,} "
                     f"({tin:,} in / {tout:,} out)")
        for mdl, m in llm.items():
            ap = " ~est" if m.get("approx") else ""
            lines.append(f"    {mdl}: {m.get('input', 0) + m.get('output', 0):,}"
                         f" tok · {m.get('calls', 0)} calls{ap}")
    hf = _state.get("hf", {})
    gens, cs = hf.get("generations", 0), hf.get("credits_start")
    if gens or cs is not None:
        line = f"Higgsfield: {gens} generation(s)"
        if cs is not None and credits_now is not None:
            line += (f"  ·  ~{cs - credits_now} credits burned "
                     f"({cs} → {credits_now})")
        elif cs is not None:
            line += f"  ·  start balance {cs}"
        lines.append(line)
    return lines


def load(folder) -> dict:
    """Read a run folder's usage.json without binding (for `kalinga.py usage` /
    `show`)."""
    p = Path(folder) / "usage.json"
    try:
        return json.loads(p.read_text()) if p.exists() else {}
    except ValueError:
        return {}


# ---- global ledger reading / aggregation -----------------------------------
def read_ledger(since: str = None) -> list:
    """All events from the global ledger (optionally only ts >= `since`, an ISO
    date/datetime prefix). Bad lines are skipped."""
    p = ledger_path()
    if not p.exists():
        return []
    out = []
    try:
        for ln in p.read_text().splitlines():
            ln = ln.strip()
            if not ln:
                continue
            try:
                rec = json.loads(ln)
            except ValueError:
                continue
            if since and str(rec.get("ts", "")) < since:
                continue
            out.append(rec)
    except OSError:
        pass
    return out


def ledger_summary(since: str = None) -> dict:
    """Aggregate the global ledger by project/channel: LLM tokens (+calls),
    generations by media kind, and any logged credits. The cross-project view."""
    rows = read_ledger(since)
    agg = {}
    for r in rows:
        key = (r.get("project") or "?", r.get("channel") or "?")
        a = agg.setdefault(key, {
            "llm_in": 0, "llm_out": 0, "llm_calls": 0,
            "image": 0, "video": 0, "audio": 0, "generation": 0,
            "credits": 0, "runs": set()})
        if r.get("run"):
            a["runs"].add(r["run"])
        if r.get("event") == "llm":
            a["llm_in"] += int(r.get("input", 0) or 0)
            a["llm_out"] += int(r.get("output", 0) or 0)
            a["llm_calls"] += 1
        elif r.get("event") == "generate":
            a[r.get("media", "generation")] = a.get(r.get("media", "generation"),
                                                     0) + int(r.get("n", 1) or 1)
            a["credits"] += int(r.get("credits", 0) or 0)
    return {"events": len(rows), "by": agg}


def ledger_lines(since: str = None) -> list:
    """Human-readable summary of the global ledger (for `kalinga.py usage
    --global`)."""
    s = ledger_summary(since)
    if not s["events"]:
        return ["(global ledger empty — nothing logged yet at %s)"
                % ledger_path()]
    lines = ["global ledger · %d events · %s"
             % (s["events"], ledger_path())]
    if since:
        lines[0] += "  (since %s)" % since
    g_in = g_out = g_calls = g_gen = 0
    for (proj, chan), a in sorted(s["by"].items()):
        tok = a["llm_in"] + a["llm_out"]
        g_in += a["llm_in"]; g_out += a["llm_out"]; g_calls += a["llm_calls"]
        gens = a["image"] + a["video"] + a["audio"] + a["generation"]
        g_gen += gens
        lines.append("  %s / %s — %d run(s)" % (proj, chan, len(a["runs"])))
        lines.append("      LLM: %s tok (%s in / %s out) · %d calls"
                     % (f"{tok:,}", f"{a['llm_in']:,}", f"{a['llm_out']:,}",
                        a["llm_calls"]))
        gp = ", ".join("%d %s" % (a[k], k) for k in
                       ("image", "video", "audio", "generation") if a[k])
        if gp:
            line = "      gen: %s (%d total)" % (gp, gens)
            if a["credits"]:
                line += " · ~%d credits" % a["credits"]
            lines.append(line)
    lines.append("  ── totals: %s LLM tok · %d calls · %d generations"
                 % (f"{g_in + g_out:,}", g_calls, g_gen))
    return lines
