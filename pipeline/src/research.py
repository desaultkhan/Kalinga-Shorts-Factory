"""
research.py — pluggable per-channel research: topic in, facts.json out.

The adapter is picked by `research:` in channel.yaml:

  llm            Claude gathers the facts through llm.py — the claude -p
                 backend uses WebSearch + WebFetch; the API backend answers
                 from knowledge and is told to flag uncertainty. (The default
                 for the sample channel — no API keys or data feeds needed.)
  manual         you drop facts.json (or facts.md) into the topic folder
                 before the run.

Adapters are a REGISTRY (`ADAPTERS`) — add your own with one entry (e.g. a
scraper, a database lookup, a domain API). The contract: return at least
{"topic", "title"}, optionally "verdict" (drives `ink: verdict` overlay
coloring) plus whatever the adapter knows.

Optional **Wikipedia grounding** (channel.yaml `wiki_grounding: true`): the
`llm` adapter first resolves the topic to its Wikipedia page (`wiki.py`) and
injects a `<wikipedia>` ground-truth block so dates/numbers/mechanisms anchor to
a citable extract instead of being hallucinated; `wiki_image: true` also pulls
the page's lead image into the run folder as a keyframe reference.

The `manual` adapter (and any adapter except `llm`, which already searches) is
then enriched with `web_context()` — a live WebSearch/WebFetch pass that adds a
`context` block of current-news bullets (+ sources) for topical hooks (cli
backend only; channel.yaml `web_research: false` opts out).

WHAT to research is steerable per RUN, not just per channel:
`research_brief(topic, folder)` assembles a directive from the run folder's
`brief.md` (creator-editable) plus the creator's concept when set. The
LLM-driven adapter injects it as a <research_brief> block that OWNS the scope.

facts.json is the channel-agnostic contract read by creative.py, seo.py and
make_video.py.

Usage:
    python3 research.py why-is-the-sky-blue
"""
from __future__ import annotations
import json
import sys

import config
import llm


def _ask(prompt, **kw):
    """Every research LLM call runs on the RESEARCH model (Sonnet/Opus by
    default — heavy web synthesis, saving the creative model for the writing).
    Override with KALINGA_MODEL_RESEARCH; KALINGA_MODEL still pins everything."""
    kw.setdefault("model", llm.model("research"))
    return llm.ask(prompt, **kw)


def research_brief(topic: str, folder) -> str:
    """What THIS run should research — '' when nothing scopes it (the adapter
    then researches the subject generally). Sources, in order:
      • <run>/brief.md — a creator-editable directive dropped in the run folder
        to point the researcher at one slice of the subject.
      • plus the creator's CONCEPT (concept.json), when already set."""
    from pathlib import Path
    lines = []
    bp = Path(folder) / "brief.md"
    if bp.exists():
        txt = bp.read_text().strip()
        if txt:
            lines.append(txt)
    try:
        import creative
        c = creative.user_concept(folder)
        if c:
            lines.append(f"The creator's concept for this video: {c}")
    except Exception:                            # noqa: BLE001
        pass
    return "\n".join(lines)


def _brief_block(topic: str, folder) -> str:
    """The <research_brief> XML block for an LLM adapter prompt ('' if no
    brief). The brief OWNS the scope: research that slice deeply."""
    brief = research_brief(topic, folder)
    if not brief:
        return ""
    return ("\n<research_brief>\nWhat THIS video must cover — the brief OWNS "
            "the scope. Research THIS slice deeply (its events, people, "
            "numbers, quotes); do not spread across the whole subject, and "
            "skip depth on ground the brief assigns elsewhere:\n"
            f"{brief}\n</research_brief>\n")


def ensure(topic: str, folder) -> dict:
    """Cached facts.json, or run the channel's adapter and write it."""
    p = folder / "facts.json"
    if p.exists():
        print("  facts.json (cached)")
        return json.loads(p.read_text())
    ch = config.channel()
    fn = ADAPTERS.get(ch.research)
    if fn is None:
        raise RuntimeError(
            f"unknown research adapter '{ch.research}' in "
            f"channels/{ch.name}/channel.yaml (known: "
            + ", ".join(sorted(ADAPTERS)) + ")")
    facts = fn(topic, folder, ch)
    if not facts:
        raise RuntimeError(f"research found nothing for {topic}")
    facts.setdefault("topic", topic)
    facts.setdefault("title", facts.get("name", topic))
    # enrich with live web context (current news → topical hooks) unless the
    # adapter already searched (llm) or the channel opts out
    if (ch.cfg.get("web_research", True) and ch.research != "llm"
            and "context" not in facts):
        ctx = web_context(topic, ch, facts)
        if ctx:
            facts["context"] = ctx
            print(f"  + {len(ctx)} current-context note(s) from the web")
    p.write_text(json.dumps(facts, indent=2))
    print(f"  facts.json ({ch.research}) -> {folder.name}/")
    return facts


def web_context(topic: str, ch, facts: dict):
    """Search the web, read a few sources, and return factual current-events
    bullets a scriptwriter can use for a TOPICAL hook — only what was actually
    found, each with its source. cli backend only (real WebSearch/WebFetch);
    the api backend can't search, so it's skipped to avoid invented 'news'.
    Never fatal — returns None on any trouble."""
    if llm.backend() != "cli":
        return None
    title = facts.get("title") or facts.get("name") or topic
    prompt = (
        f'Find the CURRENT real-world context for a YouTube Short about '
        f'"{title}" ({topic}) on the channel "{ch.title}" — {ch.premise}\n\n'
        "Search the web for what this is in the NEWS for right now and any "
        "recent developments (the last few months) a viewer would recognize. "
        "Read 2-3 of the most relevant, credible sources. Return concise, "
        "FACTUAL bullets a scriptwriter could turn into a topical hook — only "
        "things you actually found in the sources, no speculation.\n\n"
        'Return ONLY JSON: {"current_context": [{"point": <one factual '
        'sentence>, "source": <url>}], "as_of": <approx date>}')
    try:
        text = _ask(prompt, max_tokens=1500,
                       tools=["WebSearch", "WebFetch"])
        data = json.loads(text[text.find("{"):text.rfind("}") + 1])
    except Exception as e:
        print(f"  ! web context skipped: {e}", file=sys.stderr)
        return None
    pts = [c for c in (data.get("current_context") or [])
           if isinstance(c, dict) and c.get("point")]
    return pts or None


def dry_script(facts: dict):
    """Adapter-provided fallback script when no LLM backend is available. The
    LLM-driven channels have none, so the LLM backend is required to write."""
    return None


# ---------- adapters ----------
def _manual(topic, folder, ch):
    md = folder / "facts.md"
    if md.exists():
        return {"topic": topic, "title": topic, "notes": md.read_text()}
    fj = folder / "facts.json"                    # (handled by ensure() cache,
    if fj.exists():                               # but explicit here too)
        return json.loads(fj.read_text())
    raise RuntimeError(
        f"channel '{ch.name}' uses manual research — drop facts.json or "
        f"facts.md into {folder}/ and rerun")


def _summarise_grounding(title, extract):
    """Condense a long Wikipedia extract into a tight factual brief for a ~60s
    script — preserving every concrete date, number and name — instead of
    truncating it. Returns '' on any failure (caller falls back to the lead)."""
    prompt = (
        f'Condense this Wikipedia article on "{title}" into a tight factual '
        "brief for a 60-second video script. Keep EVERY concrete date, number, "
        "name and place; capture the setup, the turning point, the immediate "
        "consequences and why it still matters. No fluff, no invention — only "
        "what the text supports. ~350-600 words, plain prose or bullets.\n\n"
        f"{extract}")
    try:
        out = _ask(prompt, max_tokens=1400).strip()
        return out or ""
    except Exception as e:                        # noqa: BLE001 — never fatal
        print(f"  ! grounding summary skipped: {e}", file=sys.stderr)
        return ""


def _wiki_grounding(topic, folder, ch):
    """Optional Wikipedia grounding for any adapter, gated by channel.yaml
    `wiki_grounding: true`. Resolves the topic to its Wikipedia page and returns
    a (<wikipedia> prompt block, grounding dict) pair so the LLM anchors dates/
    numbers to a citable extract instead of hallucinating. With `wiki_image:
    true` it also saves the page's lead image into the run folder as a keyframe
    reference. Never fatal — returns ("", {}) on any miss."""
    if not ch.cfg.get("wiki_grounding", False):
        return "", {}
    from pathlib import Path
    try:
        import wiki
        g = wiki.page(topic)                      # the FULL extract
    except Exception as e:                        # noqa: BLE001 — never fatal
        print(f"  ! wikipedia grounding skipped: {e}", file=sys.stderr)
        return "", {}
    extract = g.get("extract") or ""
    if not extract:
        return "", {}
    # STORE the full extract in the run folder — never lost, citable, reusable.
    try:
        (Path(folder) / "wikipedia.md").write_text(
            f"# {g.get('title')}\n{g.get('url', '')}\n\n{extract}")
        g["grounding_file"] = "wikipedia.md"
    except Exception:                             # noqa: BLE001
        pass
    # A very long article bloats the research prompt, so SUMMARISE it (rather
    # than truncate) into a compact factual brief; short ones inject as-is.
    thresh = int(ch.cfg.get("wiki_grounding_chars", 8000))
    grounding = extract
    if len(extract) > thresh:
        summ = _summarise_grounding(g.get("title"), extract)
        if summ:
            grounding = summ
            print(f"  wikipedia: {g['title']} ({len(extract)} chars → "
                  f"summarised to {len(summ)}; full text in wikipedia.md)")
        else:
            grounding = extract[:thresh]          # summary failed → lead only
            print(f"  wikipedia: {g['title']} ({len(extract)} chars, lead "
                  f"injected; full text in wikipedia.md)")
    else:
        print(f"  wikipedia: {g['title']} ({len(extract)} chars grounding)")
    g["extract"] = grounding
    if ch.cfg.get("wiki_image", False) and g.get("image"):
        try:
            import wiki as _w
            saved = _w.fetch_image(g["image"], folder / "reference")
            if saved:
                g["reference_image"] = saved.name
                print(f"  reference image: {saved.name} (from Wikipedia)")
        except Exception:                         # noqa: BLE001
            pass
    block = (f'\n<wikipedia title="{g["title"]}" url="{g.get("url")}">\n'
             f'GROUND TRUTH — anchor dates, numbers and the mechanism to this; '
             f'verify the rest with a web search:\n{g["extract"]}\n</wikipedia>\n')
    return block, g


def _llm(topic, folder, ch):
    wiki_block, grounding = _wiki_grounding(topic, folder, ch)
    prompt = (
        f'Research for a YouTube Short on the channel "{ch.title}" — '
        f"{ch.premise}\n\n{ch.topic_noun.capitalize()}: {topic}\n"
        f"{_brief_block(topic, folder)}{wiki_block}\n"
        "Gather the concrete facts a scriptwriter needs: what it is, why "
        "people care, its most famous/specific details, real numbers, "
        "common misconceptions, anything startling. Use web search if "
        "available; otherwise rely only on well-established knowledge and "
        'mark anything uncertain with "confidence": "low".\n\n'
        'Return ONLY a JSON object: {"topic": ..., "title": ..., '
        '"summary": ..., "facts": [{"fact": ..., "detail": ..., '
        '"confidence": "high|medium|low"}], "hook_angles": [3-5 strings]}')
    text = _ask(prompt, max_tokens=1800, tools=["WebSearch", "WebFetch"])
    data = json.loads(text[text.find("{"):text.rfind("}") + 1])
    if grounding:
        src = list(data.get("sources") or []) + list(grounding.get("sources") or [])
        data["sources"] = list(dict.fromkeys(s for s in src if s))
        if grounding.get("reference_image"):
            data["reference_image"] = grounding["reference_image"]
    return data


ADAPTERS = {"manual": _manual, "llm": _llm}


if __name__ == "__main__":
    t = sys.argv[1] if len(sys.argv) > 1 else "why-is-the-sky-blue"
    ensure(t, config.topic_dir(t))
