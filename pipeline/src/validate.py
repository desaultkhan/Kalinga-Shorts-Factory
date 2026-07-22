"""
validate.py — pre-publish gates, cheapest first:

  1. technical QC (free, instant): file exists, duration inside the
     template's window, audio present and at sane loudness, captions status
  2. virality gate: parse virality.md (Higgsfield brain_activity output) and
     compare against the template thresholds — kalinga.py uses this to decide
     the one hook retry
  3. SEO lint: seo.json against the channel's rules

`run(topic)` returns a dict kalinga.py acts on; `write_report(...)` writes
the human-readable report.md audit trail into the stock folder.

Usage:
    python3 validate.py AAPL
"""
from __future__ import annotations
import json
import re
import sys
from datetime import datetime
from pathlib import Path

import config
import templates
from make_video import sh, ffprobe_duration


# ---------- gate 1: technical QC ----------
def tech_qc(folder: Path, tpl: dict) -> list[str]:
    issues = []
    final = folder / "short.mp4"
    if not final.exists():
        return ["short.mp4 missing"]
    dur = ffprobe_duration(final)
    lo, hi = tpl["duration_range"]
    if not lo <= dur <= hi:
        issues.append(f"duration {dur:.1f}s outside [{lo}, {hi}]s "
                      "(viral sweet spot is 15-30s, hard drop past 45s)")
    streams = sh(["ffprobe", "-v", "quiet", "-show_entries",
                  "stream=codec_type", "-of", "csv=p=0", str(final)]).stdout
    if "audio" not in streams:
        issues.append("no audio stream")
    else:
        vol = sh(["ffmpeg", "-i", str(final), "-af", "volumedetect",
                  "-f", "null", "-"]).stderr
        m = re.search(r"mean_volume:\s*(-?[\d.]+) dB", vol)
        if m:
            mean = float(m.group(1))
            if mean < -35:
                issues.append(f"audio very quiet (mean {mean:.0f} dB)")
            elif mean > -8:
                issues.append(f"audio very hot (mean {mean:.0f} dB)")
    audio = folder / "audio.json"
    if audio.exists():
        a = json.loads(audio.read_text())
        if not any(s.get("words") for s in a["segments"]):
            issues.append("no word timings -> no karaoke captions "
                          "(~85% of Shorts start muted; TTS_ENGINE=edge "
                          "or ffmpeg-full would fix)")
    return issues


# ---------- gate 2: virality ----------
def parse_virality(folder: Path) -> dict | None:
    """Tolerant extraction of scores from virality.md (raw job JSON dump).
    Returns {"overall": int?, "hook": int?, "sustain": int?, "report": url?}
    or None when the file is missing/unparseable."""
    vp = folder / "virality.md"
    if not vp.exists():
        return None
    text = vp.read_text()
    out = {}
    m = re.search(r"https://[^\s\"')]+(?:virality|resultJobId)[^\s\"')]*", text)
    if m:
        out["report"] = m.group(0)
    # keys observed in brain_activity params; match generously
    patterns = {
        "overall": r'"(?:overall(?:_score)?|total_score|score)"\s*:\s*([\d.]+)',
        "hook": r'"(?:peak_)?hook(?:_score|_strength|_percent)?"\s*:\s*([\d.]+)',
        "sustain": r'"sustain(?:_score|_percent)?"\s*:\s*([\d.]+)',
        "attention": r'"attention(?:_score)?"\s*:\s*([\d.]+)',
    }
    for key, pat in patterns.items():
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            v = float(m.group(1))
            out[key] = round(v * 100) if v <= 1.0 else round(v)
    return out or None


def virality_gate(folder: Path, tpl: dict) -> dict:
    """{"status": "pass"|"weak"|"unknown", "scores": {...}, "why": str}"""
    scores = parse_virality(folder)
    if not scores or not any(k in scores for k in
                             ("overall", "hook", "attention")):
        return {"status": "unknown", "scores": scores or {},
                "why": "virality.md missing or unparseable — gate skipped"}
    weak = []
    if scores.get("overall", 100) < tpl["virality_min_overall"]:
        weak.append(f"overall {scores['overall']} < "
                    f"{tpl['virality_min_overall']}")
    if scores.get("hook", 100) < tpl["virality_min_hook"]:
        weak.append(f"hook {scores['hook']} < {tpl['virality_min_hook']}")
    if weak:
        return {"status": "weak", "scores": scores, "why": "; ".join(weak)}
    return {"status": "pass", "scores": scores,
            "why": "above template thresholds"}


# ---------- gate 3: SEO lint ----------
def seo_lint(folder: Path, topic: str) -> list[str]:
    p = folder / "seo.json"
    if not p.exists():
        return ["seo.json missing"]
    meta = json.loads(p.read_text())
    cfg = config.channel().seo
    issues = []
    # slug-tolerant match: a topic is a folder slug ("Steve-Jobs") but a natural
    # title reads "Steve Jobs", so treat - / _ as spaces (and ignore case) — else
    # title_must_include:["{topic}"] spuriously fails on every hyphenated topic
    def _norm(s):
        return re.sub(r"[-_]+", " ", s or "").lower()
    title = meta.get("title", "")
    if len(title) > 80:
        issues.append(f"title {len(title)} chars (>80 gets truncated)")
    for req in cfg.get("title_must_include", []):
        kw = req.replace("{topic}", topic)
        if _norm(kw) not in _norm(title):
            issues.append(f"title missing '{kw}'")
    desc = meta.get("description", "")
    for req in cfg.get("description_must_include", []):
        kw = req.replace("{topic}", topic)
        if _norm(kw) not in _norm(desc):
            issues.append(f"description missing '{kw}'")
    # plain text only: markdown pastes literally into YouTube and "looks like
    # tags" — flag bold/code/links/headings/bullets in the title or description
    md = (re.search(r"\*\*|`|\]\(", title + "\n" + desc)
          or re.search(r"(?m)^[ \t]{0,3}(#{1,6}[ \t]|[-*+][ \t]|>)", desc))
    if md:
        issues.append("title/description has markdown (plain text only)")
    if len(desc) < 120:
        issues.append(f"description thin ({len(desc)} chars — aim for 200+)")
    tags = meta.get("hashtags", [])
    if not 3 <= len(tags) <= 5:
        issues.append(f"{len(tags)} hashtags (want 3-5)")
    if len(meta.get("tags", [])) < 12:
        issues.append(f"{len(meta.get('tags', []))} search tags (want 15-20)")
    return issues


# ---------- combined ----------
def run(topic: str) -> dict:
    folder = config.topic_dir(topic, create=False)
    tpl = templates.load_pinned(folder)
    qc = tech_qc(folder, tpl)
    vir = virality_gate(folder, tpl)
    seo = seo_lint(folder, config.channel().normalize_topic(topic))
    ok = not qc and vir["status"] != "weak" and not seo
    return {"qc": qc, "virality": vir, "seo": seo, "ok": ok}


def write_report(topic: str, result: dict, meta: dict):
    """report.md — the audit trail for this run. `meta` carries run info
    kalinga.py knows (template, experiment, attempts, retries, credits)."""
    folder = config.topic_dir(topic, create=False)
    vir = result["virality"]
    lines = [
        f"# Run report — {folder.name}",
        f"Generated {datetime.now():%Y-%m-%d %H:%M}",
        "",
        f"- template: **{meta.get('template', '?')}**",
        f"- experiment: {meta.get('experiment') or 'none'}",
        f"- script: {meta.get('script_mode', '?')}, script judge "
        f"{meta.get('script_judge', meta.get('hook_judge', '?'))}/10",
        f"- hook retries used: {meta.get('hook_retries', 0)}",
        f"- credits: {meta.get('credits') or 'n/a'}",
        "",
        "## Gate 1 — technical QC",
        *([f"- ✗ {i}" for i in result["qc"]] or ["- ✓ clean"]),
        "",
        "## Gate 2 — virality "
        f"({vir['status'].upper()})",
        f"- {vir['why']}",
        *(f"- {k}: {v}" for k, v in vir["scores"].items()),
        "",
        "## Gate 3 — SEO lint",
        *([f"- ✗ {i}" for i in result["seo"]] or ["- ✓ clean"]),
        "",
        "## Verdict",
        "- **READY TO UPLOAD**" if result["ok"] else
        "- **REVIEW BEFORE UPLOAD** — see gate failures above",
        "",
        "After uploading:  `python3 comments.py register <VIDEO_ID> "
        f"{config.channel().normalize_topic(topic)}`",
    ]
    (folder / "report.md").write_text("\n".join(lines) + "\n")
    print(f"  report -> {folder.name}/report.md")


if __name__ == "__main__":
    tk = sys.argv[1] if len(sys.argv) > 1 else "AAPL"
    r = run(tk)
    print(json.dumps(r, indent=2))
    sys.exit(0 if r["ok"] else 1)
