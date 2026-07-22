"""
SEO metadata — YouTube title, description, hashtags, and tags per video.
LLM-first (channel-aware, learnings-aware, applies live experiment
overrides); minimal generic fallback.

Strategy (search-intent-first, Shorts-tuned): the prompt is grounded in the
ACTUAL video (`_seo_context` — the seg0 hook + on-screen headline alongside the
research facts/numbers), and asks for a keyword-front-loaded 45-65-char title, a
description whose first line is the phrase people SEARCH, niche-first hashtags
(the first 3 show above the title), and 15-20 intent-covering tags. All output is
PLAIN TEXT — `_strip_markdown`/`_clean_meta` strip any markdown (YouTube renders
it literally and it "looks like tags") while keeping real #hashtags, and `seo.md`
is a plain-text copy-paste sheet (no `#`/`##` headers). validate.seo_lint
enforces the plain-text + length + count rules.

Channel rules come from channels/<name>/channel.yaml `seo:` block
(prompt_rules, base_hashtags, base_tags, must-include lists — the latter are
enforced by validate.seo_lint).

Usage:
    python3 seo.py AAPL
Writes seo.json + seo.md (copy-paste ready) into the topic folder.
"""
import json
import prompts
import re
import sys

import config

PROMPT = prompts.load("seo")

# markdown that YouTube renders LITERALLY (and that "looks like tags") — stripped
# from the title/description while real #hashtags (a # glued to a word) survive
_MD_LINK = re.compile(r"\[([^\]]+)\]\([^)]*\)")        # [text](url) -> text
_MD_BOLD = re.compile(r"(\*\*|__)(.+?)\1", re.S)       # **x** / __x__ -> x
_MD_CODE = re.compile(r"`+([^`]*)`+")                  # `x` -> x
_MD_HEADING = re.compile(r"(?m)^[ \t]{0,3}#{1,6}[ \t]+")  # '## ' line start
_MD_BULLET = re.compile(r"(?m)^[ \t]{0,3}[-*+][ \t]+")    # '- ' bullets
_MD_QUOTE = re.compile(r"(?m)^[ \t]{0,3}>[ \t]?")         # '> ' blockquote
_MD_STARS = re.compile(r"\*+")                         # any leftover * emphasis


def _strip_markdown(text: str) -> str:
    """Plain-text a string: drop markdown YouTube won't render — links, bold,
    code, headings, bullets, blockquotes, stray emphasis stars — but KEEP real
    #hashtags (only '# ' heading markers are removed, never '#word')."""
    if not text:
        return text or ""
    t = _MD_LINK.sub(r"\1", text)
    t = _MD_BOLD.sub(r"\2", t)
    t = _MD_CODE.sub(r"\1", t)
    t = _MD_HEADING.sub("", t)
    t = _MD_BULLET.sub("", t)
    t = _MD_QUOTE.sub("", t)
    t = _MD_STARS.sub("", t)
    t = re.sub(r"[ \t]{2,}", " ", t)
    return t.strip()


def _clean_meta(meta: dict) -> dict:
    """Sanitize generated metadata: plain-text the title + description (no
    markdown), normalize hashtags (leading #, no spaces, deduped), and dedupe
    the tag list. Idempotent — safe to run on any meta dict."""
    meta["title"] = _strip_markdown(meta.get("title", "")).replace("\n", " ")
    meta["description"] = _strip_markdown(meta.get("description", ""))
    seen, tags = set(), []
    for h in meta.get("hashtags") or []:
        h = "#" + re.sub(r"[^0-9A-Za-z]", "", str(h))
        if len(h) > 1 and h.lower() not in seen:
            seen.add(h.lower())
            tags.append(h)
    meta["hashtags"] = tags[:5]
    seen2, out = set(), []
    for t in meta.get("tags") or []:
        t = _strip_markdown(str(t)).lstrip("#").strip()
        if t and t.lower() not in seen2:
            seen2.add(t.lower())
            out.append(t)
    meta["tags"] = out
    return meta


def _seo_context(folder, facts: dict) -> dict:
    """A FOCUSED, SEO-relevant snapshot of THIS video — the research facts most
    useful for search intent PLUS the actual video opening (seg0 hook) and the
    on-screen headline, so the metadata targets the real story, not generic
    facts. Keeps the LLM payload tight and on-point."""
    ctx = {}
    for k in ("topic", "title", "summary", "one_liner",
              "hook_angles", "numbers", "timeline"):
        v = facts.get(k)
        if v:
            ctx[k] = v
    try:
        sc = json.loads((folder / "script.json").read_text())
        seg0 = (sc.get("segments") or [{}])[0]
        hook = (seg0.get("text") or "").strip()
        if hook:
            ctx["video_hook"] = hook[:280]
        head = ((sc.get("direction") or {}).get("thumbnail") or {}).get("text")
        if head:
            ctx["on_screen_headline"] = head
    except Exception:                            # noqa: BLE001 (facts are enough)
        pass
    return ctx


def claude_seo(d: dict, feedback: str = None, prev: dict = None) -> dict:
    """LLM metadata; feedback + prev (the previous seo.json dict) turn it
    into a revision call — used by the interactive session. Output is
    plain-text sanitized (no markdown) and normalized."""
    import creative
    import llm
    ch = config.channel()
    overrides = (json.loads(ch.overrides.read_text())
                 if ch.overrides.exists() else {})
    exp = ""
    if overrides.get("seo_instruction"):
        exp = f"LIVE EXPERIMENT, apply it: {overrides['seo_instruction']}\n"
    learnings = creative.learnings_tail(cap=1200)
    prompt = PROMPT.format(
        channel=ch.title, premise=ch.premise, noun=ch.topic_noun,
        rules=ch.seo.get("prompt_rules", "(none)"),
        learnings=learnings + "\n" if learnings else "",
        experiment=exp,
        data=json.dumps(d, indent=1)[:3500])
    if feedback:
        prompt += (f"\n\nPrevious metadata:\n{json.dumps(prev or {}, indent=1)}"
                   f"\nCreator feedback on it — apply:\n{feedback}")
    text = llm.ask(prompt, max_tokens=1100)
    meta = json.loads(text[text.find("{"):text.rfind("}") + 1])
    return _clean_meta(meta)


def template_seo(d: dict) -> dict:
    """No-LLM fallback: functional, lint-passing where the channel config
    allows, never creative."""
    ch = config.channel()
    cfg = ch.seo
    topic, title_txt = d.get("topic", ""), d.get("title", "")
    must = [m.replace("{topic}", topic)
            for m in cfg.get("description_must_include", [])]
    hashtags = list(cfg.get("base_hashtags", [])) + [f"#{topic}"]
    return _clean_meta({
        "title": f"{title_txt} ({topic}) — {ch.title}"[:80],
        "description": (f"{title_txt}: {ch.premise}\n"
                        + (" · ".join(must) + "\n" if must else "")
                        + " ".join(hashtags[:3])),
        "hashtags": hashtags[:5],
        "tags": list(cfg.get("base_tags", [])) + [topic, title_txt],
    })


def _write_md(folder, meta: dict):
    """seo.json + copy-paste seo.md (incl. the music credit) from one meta
    dict — also used to re-sync after manual seo.json edits."""
    music_file = folder / "music.txt"
    credit = ""
    if music_file.exists():
        credit = ("\nMUSIC CREDIT (add to the description)\n"
                  f"Background track: {music_file.read_text().strip()} — "
                  "check the source's attribution requirement.\n")
    (folder / "seo.json").write_text(json.dumps(meta, indent=2))
    # PLAIN TEXT copy-paste sheet — no markdown (# / ## headers read as tags and
    # YouTube renders none of it); just labelled blocks to copy straight across
    (folder / "seo.md").write_text(
        f"TITLE\n{meta['title']}\n\n"
        f"DESCRIPTION\n{meta['description']}\n\n"
        f"HASHTAGS\n{' '.join(meta['hashtags'])}\n\n"
        f"TAGS (comma-separated)\n{', '.join(meta['tags'])}\n" + credit)


def run(topic: str, feedback: str = None):
    import llm
    folder = config.topic_dir(topic)
    facts = json.loads((folder / "facts.json").read_text())
    d = _seo_context(folder, facts)              # focused, video-grounded payload
    prev = None
    if feedback and (folder / "seo.json").exists():
        prev = json.loads((folder / "seo.json").read_text())
    if llm.available():
        try:
            meta, mode = claude_seo(d, feedback, prev), llm.describe()
        except Exception as e:
            print(f"  ! LLM SEO failed ({e}); using fallback", file=sys.stderr)
            meta, mode = template_seo(d), "template"
    else:
        meta, mode = template_seo(d), "template"

    _write_md(folder, meta)
    print(f"  SEO ({mode}) -> {folder.name}/seo.md")
    return meta


if __name__ == "__main__":
    run(sys.argv[1] if len(sys.argv) > 1 else "why-is-the-sky-blue")
