"""
creative.py — the headless creative brain, channel-agnostic.

Prompts are assembled from the channel definition
(channels/<name>/channel.yaml: persona, premise, segment structure, voice &
visual rules) plus the run's pinned template (world, pacing). Every LLM call
goes through llm.py — by default `claude -p` with claude-opus-4-8 on the
user's Claude subscription ($0 marginal), Anthropic API fallback:

  write_script(topic)         facts.json -> script.json  (segments judged by
                              an LLM hook-judge BEFORE any Higgsfield credits
                              are spent; best of up to `script_attempts`
                              tries is kept)
  rewrite_hook(topic, why)    surgical rewrite of segment 0 after a weak
                              virality score — only seg0/key0/clip0 need
                              regenerating (~15-25 cr)

Usage:
    python3 creative.py AAPL          # write (or re-judge) the script
"""
from __future__ import annotations
import prompts
import json
import os
import re
import sys
from pathlib import Path

import config
import llm
import templates


def _ask(user: str, system: str = "", max_tokens: int = 1500,
         image_paths=None, role: str = "script") -> str:
    """A creative-core LLM call. `role` picks the model (script | direction |
    judge — all Fable by default, tunable per role via KALINGA_MODEL_<ROLE>);
    the non-craft helpers (reference brief, topic ideas, learnings) call
    llm.ask directly and stay on the default model."""
    return llm.ask(user, system=system, max_tokens=max_tokens,
                   image_paths=image_paths, model=llm.model(role))


def _cast_images(script: dict) -> list:
    """The cast REFERENCE images to SHOW the director (so it composes around the
    real faces, not just text): each channel-cast member who appears in the
    script gets their avatar (headshot) + their front full-length ref. Capped so
    the prompt stays light. [] when the channel has no cast / no avatars."""
    ch = config.channel()
    names = []
    for s in script.get("segments", []):
        for n in ([s.get("speaker")] + [ln.get("speaker")
                                        for ln in (s.get("lines") or [])]
                  + list(s.get("cast_in_shot") or [])):
            if n and n not in names:
                names.append(n)
    for n in (script.get("cast") or {}):
        if n not in names:
            names.append(n)
    imgs = []
    for n in names:
        av = ch.cast_avatar(n)
        if av is not None and av.exists():
            imgs.append(av)
        full = ch.cast_ref(n, kind="full", angle="front")
        if full is not None:
            imgs.append(full)
    return imgs[:8]                              # keep the prompt light


def _json_block(text: str, opener: str = "[", closer: str = "]"):
    start, end = text.find(opener), text.rfind(closer)
    if start < 0 or end <= start:
        raise ValueError(f"no JSON {opener}...{closer} in model output: "
                         f"{text[:200]}")
    return json.loads(text[start:end + 1])


def _segments_and_meta(text: str):
    """Parse the writer's output into (segments, meta). Accepts a plain JSON
    ARRAY of segments (single narrator) OR an OBJECT
    {setup?, cast?, segments:[...]} (multi-character). meta carries
    setup/cast when present."""
    obj_at, arr_at = text.find("{"), text.find("[")
    if obj_at >= 0 and (arr_at < 0 or obj_at < arr_at):
        try:
            obj = _json_block(text, "{", "}")
        except ValueError:
            obj = None
        if isinstance(obj, dict) and isinstance(obj.get("segments"), list):
            return obj["segments"], {k: obj[k] for k in ("setup", "cast")
                                     if obj.get(k)}
    return _json_block(text, "[", "]"), {}


def _join_lines(seg: dict) -> dict:
    """A dialogue segment (`lines`) gets a joined `text` for the facts lock,
    captions fallback and thumbnail/hook reads."""
    if seg.get("lines") and not (seg.get("text") or "").strip():
        seg["text"] = " ".join((l.get("text") or "").strip()
                                for l in seg["lines"]
                                if (l.get("text") or "").strip())
    return seg


def _roster_cast_for(segs: list, ch) -> dict:
    """When the channel has a fixed cast, build the script's `cast` block from
    the ROSTER for the members the writer actually used (their decided voice +
    avatar + personality), so voices/avatars flow through and no character is
    invented. {} when the channel has no roster or none were used."""
    roster = ch.cast or {}
    if not roster:
        return {}
    used = []
    for s in segs:
        for n in ([s.get("speaker")]
                  + [l.get("speaker") for l in (s.get("lines") or [])]):
            if n and n in roster and n not in used:
                used.append(n)
    out = {}
    for n in used:
        m = roster[n]
        out[n] = {"voice": m.get("voice"), "desc": m.get("personality"),
                  "appearance": m.get("appearance"), "avatar": m.get("avatar")}
    return out


def learnings_tail(cap: int = 2500) -> str:
    """Global craft learnings + this channel's audience learnings, both
    tail-capped. Empty string when neither exists yet."""
    parts = []
    if config.GLOBAL_LEARNINGS.exists():
        parts.append("Cross-channel craft learnings:\n"
                     + config.GLOBAL_LEARNINGS.read_text()[-cap:])
    ch = config.channel()
    if ch.learnings.exists():
        parts.append("Learnings from this channel's audience:\n"
                     + ch.learnings.read_text()[-cap:])
    return "\n\n".join(parts)


def extract_session_learnings(topic: str, iterations: dict = None,
                              learned: list = None,
                              versioned: dict = None,
                              script_changes: str = "") -> str:
    """Distil concrete CHANNEL learnings from WHAT ACTUALLY CHANGED this
    session. The primary signal is `script_changes` — the real diff of the
    spoken words (segments added/removed/rewritten/reordered, was→now), not the
    creator's critique text — plus the VERSION TRAIL (`versioned`: canonical
    artifact -> times superseded, from versions.json; survives quit/resume) and
    which stages were retried. Appends the learnings to the channel learnings;
    returns the added text ('' if nothing / no backend)."""
    if not llm.available():
        return ""
    import datetime
    ch = config.channel()
    folder = config.topic_dir(topic)
    iters = ", ".join(f"{k}×{v}" for k, v in (iterations or {}).items())
    vtrail = ", ".join(f"{name}×{n}" if n > 1 else name
                       for name, n in sorted((versioned or {}).items()))
    script_changes = (script_changes or "").strip()
    crit = [i for i in (learned or []) if i and i.get("text")]
    if not (script_changes or vtrail or iters or crit):
        return ""
    body = ""
    if script_changes:
        body += "What changed in the SCRIPT (the actual words):\n" \
                + script_changes
    if crit:
        body += ("\n\nThe creator's explicit critiques/notes this session "
                 "(stage in parentheses) — these are the most direct signal of "
                 "what they want:\n"
                 + "\n".join(f"- ({i.get('tag', '?')}) {i['text']}" for i in crit))
    if vtrail:
        body += f"\n\nArtifacts regenerated (×N = how many times): {vtrail}"
    if iters:
        body += f"\nStages retried: {iters}"
    try:
        text = _ask(
            f'A creator just finished making one Short for "{ch.title}" '
            f'({ch.premise}). Here is what they did this session — the script '
            f'edits (BEFORE→AFTER) and their explicit critiques:\n\n'
            f'{body}\n\nDISTILL 2-5 concrete LEARNINGS for THIS channel — durable '
            "imperative guidance for the NEXT video (writer / director / SEO). "
            "Infer the PREFERENCE behind the edits and critiques, NOT the one-off "
            "wording: turn a raw note like \"don't use Dani here\" or \"apple "
            "isn't on google servers\" into a generalizable rule only if it "
            "transfers, and DROP pure one-off facts/per-video choices. Each "
            "learning must read as a standing instruction, never a paraphrase of "
            "a single edit. Reply as a markdown bullet list, or exactly NONE.",
            max_tokens=450).strip()
    except Exception:                       # noqa: BLE001
        return ""
    if not text or "NONE" in text.upper():
        return ""
    stamp = datetime.date.today().isoformat()
    with ch.learnings.open("a") as f:
        f.write(f"\n<!-- session {stamp} · {topic} · auto-extracted from "
                f"this session's edits -->\n{text}\n")
    return text


def globalize_critiques(items: list, topic: str) -> bool:
    """Distill a session's critiques into the GLOBAL learnings — the ones that
    are transferable Shorts craft (apply to any video/channel), rewritten as
    durable instructions a writer/director/critic should follow. Channel-
    specific ones are skipped (they already live in the channel learnings).
    One $0 LLM call; returns True if anything was added."""
    import datetime
    items = [i for i in items if i and i.get("text")]
    if not items or not llm.available():
        return False
    ch = config.channel()
    joined = "\n".join(f"- ({i.get('tag', '?')}) {i['text']}" for i in items)
    try:
        text = _ask(
            f'A creator gave these critiques while making one Short for the '
            f'channel "{ch.title}" ({ch.premise}):\n{joined}\n\n'
            "Pick ONLY the ones that are TRANSFERABLE craft — a durable "
            "lesson that would improve ANY future Short (hooks, pacing, "
            "visuals, data, captions, structure), not a one-off tweak to "
            "this video. Rewrite each as a single imperative instruction a "
            "writer/director/critic should follow. Reply as a markdown "
            "bullet list, or exactly NONE if none transfer.", max_tokens=400
        ).strip()
    except Exception:
        return False
    if not text or "NONE" in text.upper():
        return False
    stamp = datetime.date.today().isoformat()
    with config.GLOBAL_LEARNINGS.open("a") as f:
        f.write(f"\n<!-- distilled {stamp} from {ch.name}/{topic} -->\n"
                f"{text}\n")
    return True


def condense_learnings(path, dry_run: bool = False) -> dict:
    """Condense ONE learnings file: dedupe overlapping entries, MERGE repeated
    guidance into one sharper line, drop only one-off notes a later rule already
    supersedes, and reorganize into a tight set of durable imperative learnings
    — WITHOUT losing any distinct lesson. The learnings files only ever grow
    (every session appends) and the writers/critics read just the tail, so the
    most valuable lessons fall out of the window; condensing keeps them in it.
    The original is BACKED UP first (timestamped `.bak`, never deleted).
    Returns {ok, reason?, before, after, removed_pct, backup, text}."""
    import datetime
    path = Path(path)
    if not path.exists():
        return {"ok": False, "reason": "no learnings file yet"}
    original = path.read_text()
    if not llm.available():
        return {"ok": False, "reason": "no LLM backend (claude CLI / API key)"}
    if len(original.strip()) < 600:
        return {"ok": False, "reason": "already short — nothing to condense"}
    is_global = Path(config.GLOBAL_LEARNINGS) == path
    scope = ("CROSS-CHANNEL Shorts craft that applies to ANY channel or video"
             if is_global else
             f'audience + content learnings for the "{config.channel().title}" '
             f'channel ({config.channel().premise})')
    system = ("You are the editor of a living LEARNINGS file that AI "
              "writers, directors and SEO tools read before producing each "
              "short video. Your job is to keep it DENSE and NON-REPETITIVE so "
              "the important lessons stay near the end where they're read.")
    user = (
        f"This learnings file holds {scope}.\n\n"
        "Condense it. STRICT RULES:\n"
        "1. KEEP EVERY DISTINCT LESSON. Merge duplicates and near-duplicates "
        "into one sharper imperative line — never silently lose a unique rule.\n"
        "2. Drop ONLY true redundancy and one-off notes that a later, more "
        "general rule already supersedes. When unsure, keep it.\n"
        "3. Preserve all SPECIFICS verbatim: exact numbers, the order numbers "
        "are spoken, BANNED phrases, named tests/rules, dollar figures.\n"
        "4. Rewrite vague per-video gripes as durable, reusable guidance a "
        "writer/director/SEO can apply to the NEXT video.\n"
        "5. Group related rules under short markdown headings; lead with the "
        "highest-leverage rules. Keep the existing top title line if present.\n"
        "6. Output ONLY the new markdown file body — no preamble, no "
        "explanation of what you changed.\n\n"
        "--- CURRENT FILE ---\n" + original)
    try:
        text = _ask(user, system=system, max_tokens=3000).strip()
    except Exception as e:                       # noqa: BLE001
        return {"ok": False, "reason": f"LLM call failed: {str(e)[:120]}"}
    if not text or len(text) < 80:
        return {"ok": False, "reason": "model returned (almost) nothing — "
                "left the file untouched"}
    # never let condensing BALLOON the file (a misbehaving model) — guardrail
    if len(text) > len(original) * 1.1:
        return {"ok": False, "reason": "condensed output wasn't smaller — "
                "left the file untouched"}
    before, after = len(original), len(text)
    removed_pct = round((before - after) / before * 100) if before else 0
    if dry_run:
        return {"ok": True, "before": before, "after": after,
                "removed_pct": removed_pct, "backup": None, "text": text}
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = path.with_name(f"{path.stem}.{stamp}.bak{path.suffix}")
    backup.write_text(original)                  # never delete the prior state
    path.write_text(text.rstrip() + "\n")
    return {"ok": True, "before": before, "after": after,
            "removed_pct": removed_pct, "backup": backup, "text": text}


def _guardrails(tpl: dict, ch) -> str:
    """System prompt: channel persona + segment structure + voice rules,
    parameterized by the template's world and pacing."""
    lo, hi = tpl["target_words"]
    flexible = ch.flexible_segments
    seg_lines = []
    for s in ch.segments:
        line = f"- {s['label']}: {s.get('guidance', '').strip()}"
        if s.get("max_words"):
            line += f" Max {s['max_words']} words."
        if flexible and s.get("required"):
            line += " (REQUIRED — must appear.)"
        elif s.get("optional"):
            line += " (OPTIONAL — include only if it earns its place.)"
        seg_lines.append(line)
    structure = "\n".join(seg_lines)
    if flexible:
        req = ch.required_labels
        segments_intro = (
            "BEAT MENU — these are SUGGESTED beats for this channel, NOT a fixed "
            "checklist. YOU control how many segments there are and how big each "
            "is — and since each segment is ONE held shot, that IS your control "
            "over the video's length and pacing. Shape the story yourself: "
            "include the beats that serve it, COMBINE several small related beats "
            "into ONE fuller segment to keep the video tight and the storytelling "
            "coherent (don't fragment a single idea across many thin beats), DROP "
            "any that don't earn their place, ADD a beat of your own when the "
            "story needs it, and choose the ORDER. Prefer fewer, meatier segments "
            "over many tiny ones. When you use a listed beat keep its exact label "
            "(so it inherits its styling); for a new beat invent a short "
            "UPPERCASE label. Keep labels unique."
            + (f" These beat(s) MUST appear: {', '.join(req)}." if req else ""))
        optional_note = ""           # everything's discretionary in flexible mode
        output_beats = (
            "Design the beats yourself per the BEAT MENU above — include, "
            "combine, drop, add and ORDER them for the best story; give each "
            "segment a short unique label."
            + (f" The beat(s) {', '.join(req)} MUST appear." if req else ""))
    else:
        segments_intro = (
            "SEGMENTS TO COVER — your script must include EVERY one of these "
            "beats exactly once. The guidance is a SUGGESTION for what each beat "
            "covers; the RUNNING ORDER is YOURS to choose for the best story "
            "(see STORY & ORDER):")
        opt = ch.optional_labels
        optional_note = (
            "\nOPTIONAL beats (include each AT MOST ONCE, only when it genuinely "
            f"strengthens the piece — otherwise leave it out): {', '.join(opt)}."
            if opt else "")
        output_beats = (
            "Include EVERY required beat below exactly once, using these EXACT "
            "label strings, in the RUNNING ORDER YOU CHOOSE:\n"
            + ", ".join(ch.required_labels)
            + ".\n(That is the required SET of labels, NOT the order — you "
            "decide the sequence.)")
    voice = "\n".join(
        [f"- {v}" for v in ch.voice_rules]
        + ["- Use only numbers present in the data; speak them naturally.",
           "- Write the DELIVERY into the text — the TTS performs your "
           "punctuation: '…' is a half-beat pause before a reveal, an "
           "em dash — is a hard stop, a question lifts. Put at most ONE "
           "word per segment in CAPITALS where the stress earns it.",
           f"- TOTAL: {lo}-{hi} spoken words. Use the room when the "
           "substance needs it — cut filler, never compress a fact into "
           "vagueness to save words."])
    roster = ch.cast or {}
    if roster:
        members = "\n".join(
            f"- {name}: {(info or {}).get('personality', '').strip()}"
            for name, info in roster.items())
        cast_section = (
            "CHANNEL CAST — this channel has a FIXED cast. Use ONLY these "
            "characters; do NOT invent new ones, rename them, or add anyone "
            "else:\n" + members + "\n"
            "- Decide who carries the piece: one of them as a single narrator, "
            "OR stage a scene/dialogue between them. Mark each segment's "
            "\"speaker\": <name> (exactly one of the names above). For a "
            "back-and-forth INSIDE a segment use \"lines\": [{\"speaker\": "
            "<name>, \"text\": <words>}, …] instead of \"text\".\n"
            "- You do NOT choose voices — each character already has one; just "
            "pick who speaks.\n"
            "- Add a one-line \"setup\" naming the scene + who is present so "
            "the DIRECTOR can stage them. Use the cast where it earns it — the "
            "opener still has to stop the scroll.")
    else:
        cast_section = (
            "CAST & VOICE (OPTIONAL — a single narrator is the default and is "
            "perfectly fine; use characters only when more than one voice "
            "genuinely makes the piece better, e.g. a host vs a sceptic):\n"
            "- Declare characters in a \"cast\" map: name → {\"voice\": "
            "\"auto\", \"desc\": \"<who they are — for the DIRECTOR>\"}. Leave "
            "\"voice\" as \"auto\"; distinct voices are assigned automatically.\n"
            "- Mark WHO SPEAKS each segment with \"speaker\": <name>. For a "
            "back-and-forth INSIDE one segment use \"lines\": [{\"speaker\": "
            "<name>, \"text\": <words>, optional \"expression\": <how it's "
            "said>}, …] in place of \"text\".\n"
            "- Add a one-line \"setup\" naming the scene + cast for the "
            "DIRECTOR. Never add characters just to add them.")
    return f"""You write voiceover scripts for "{ch.title}", a cinematic
YouTube Shorts channel. {ch.premise}

PERSONA: {ch.persona}

{segments_intro}
{structure}

VOICE:
{voice}

STORY & ORDER — you are a SHORT-FORM STORYTELLER, not a list-reader. Order the
beats for maximum RETENTION using how real viral Shorts are built:
- THE OPEN IS EVERYTHING. The feed decides in ~1 second. Open on your single
  most scroll-stopping beat (first words = first frame) — normally the hook —
  and CLOSE on the CTA, which loops back to the open. Sequence the MIDDLE
  beats into the most gripping order; you may move them around freely.
- OPEN LOOPS that pay off EARLY: plant a question fast ("one of the four tests
  is where this nearly broke…"), then PAY IT OFF within a beat or two —
  opening the next loop as you close the last, so curiosity never flatlines.
  Reward the viewer quickly; don't make them wait the whole video for value.
- BUT / THEREFORE spine: connect beats with "but" (tension) and "so"
  (consequence), never a flat "and then… and then". Every line must earn the
  next; a beat that doesn't raise a question or pay one off gets cut or moved.
- PATTERN INTERRUPTS + a mid-roll RE-HOOK: change rhythm, scale or angle to
  re-grab attention at the point viewers usually drop (right after the setup).
- TEASE, don't dump: withhold the verdict and reveal it LATE; front-load
  intrigue, not conclusions.
- SPOKEN, not written, English: contractions, short clauses, second person,
  the odd rhetorical question. Write for the EAR — read every line aloud; if
  it sounds like an essay or a press release, rewrite it as talk.
- PACING — SEGMENT SIZE IS YOUR LENGTH DIAL. Each segment becomes exactly ONE
  shot held on screen while its lines are spoken, so the number and size of
  segments is what controls the video's runtime and rhythm. A string of tiny
  one-line segments makes a choppy, padded video that overstays its welcome.
  Group small RELATED points into ONE fuller segment (a single continuous shot)
  rather than spreading them thin; start a NEW segment only when the VISUAL
  genuinely should change or the story turns. Each segment should carry a
  complete beat — roughly a breath up to a few sentences — never a stray
  fragment. Fewer, meatier segments beat many thin ones: keep it tight.

WRITE WITH WIT — this is the difference between a video people finish and one
they swipe. Be QUIPPY and SMART, never a flat explainer:
- Every line needs a point of view, a turn of phrase, or a flash of wit —
  the surprising-but-true framing over the textbook one. If a sentence could
  open any corporate explainer, rewrite it.
- Land real punchlines: an unexpected comparison, a dry aside, a number made
  absurd by its scale, a setup that pays off a beat later. Aim for at least
  one line that earns a smirk or a "wait, what?".
- Talk like the sharpest person in the room who already knows the punchline —
  confident, specific, a little cheeky. Vary the rhythm so one line lands hard
  before the next (respect the channel's VOICE rules above on sentence flow).
- BANNED: "in this video", "let's dive in", "stay tuned", hedging ("might",
  "could be", "some say"), AI-explainer throat-clearing, and generic praise.
  Cut every word that isn't pulling weight.
- Smart ≠ smug: the wit serves the point, never buries the fact. Numbers and
  the verdict stay exactly true.

WRITE FOR THE COMMENT SECTION — a Short lives on comments, shares and saves,
so bias the script toward provoking a REACTION (honestly — never clickbait
that the facts don't back):
- The HOOK should either stake a defensible STANCE people will want to argue
  with (a judgment, a ranking, a "this is the real reason…") or drop a fact so
  surprising the viewer's first instinct is to send it to someone. A hook that
  merely informs gets scrolled; a hook that picks a side gets replies.
- Plant ONE deliberate debate-starter in the body: a line that splits the
  audience into camps ("genius or reckless?", "would you have taken that
  deal?") — an opinion the facts support but a viewer could push back on.
- Make at least one beat SCREENSHOT-WORTHY: a number, comparison or rule of
  thumb so crisp people save the video to keep it.
- The CTA may ASK THE AUDIENCE the open question the story raised (their
  verdict, their side, what they'd have done) instead of generic
  follow-boilerplate — a question beats an instruction for comments.
- Never manufacture outrage or misstate to provoke: the stance must be
  defensible from the facts on screen.

{cast_section}

OUTPUT — return EITHER a plain JSON ARRAY of segments (single narrator), OR a
JSON OBJECT {{"setup": <…>, "cast": {{…}}, "segments": [ … ]}} when you use
characters. {output_beats}{optional_note}
Each segment is {{"label": ..., "text": <the spoken words>}} plus, only if you
use a cast, an optional "speaker" (or "lines" for in-segment dialogue).
DELIVERY — any segment OR line may carry an optional "expression" naming HOW
it is said (e.g. "excited", "deadpan", "concerned", "whispering", "playful",
"warm"): it shifts the voice's pace, pitch and volume, so set it wherever the
delivery should change and leave it off for a neutral read. (Punctuation still
shapes delivery too: '…' = beat pause, em dash = hard stop, one stressed
CAPITAL per segment.) Write only the words/speakers/expression — the on-screen
overlay text and every visual are designed later by the DIRECTOR, not you. Do
not invent overlay or visual fields; pour all your craft into the spoken line."""


JUDGE_PROMPT = prompts.load("hook_judge")


REHOOK_PROMPT = prompts.load("hook_rewrite")


def _facts(d: dict, cap: int = 4000) -> str:
    return json.dumps(d, indent=1)[:cap]


def judge_hook(hook_seg: dict, d: dict) -> dict:
    """Score the spoken hook 0-10. NEVER raises — a malformed judge response is
    transient LLM formatting, not a reason to discard an otherwise-good script
    (it used to escape write_script's attempt loop and force the dry-template
    fallback). One retry, then a neutral score."""
    ch = config.channel()
    lt = learnings_tail(cap=1000)
    lt = (f"\nThe creator's learned standards — judge by THESE too:\n{lt}\n"
          if lt else "")
    prompt = JUDGE_PROMPT.format(noun=ch.topic_noun,
                                 hook=json.dumps(hook_seg, indent=1),
                                 facts=_facts(d), learnings=lt)
    last = None
    for _ in range(2):                  # one retry — judge JSON occasionally drifts
        try:
            j = _json_block(_ask(prompt, max_tokens=300, role="judge"),
                            "{", "}")
            j["score"] = int(j.get("score", 0))
            return j
        except (ValueError, TypeError) as e:
            last = e
    print(f"  ! hook judge unparseable ({last}) — scoring it neutral, keeping "
          f"the script", file=sys.stderr)
    return {"score": 0, "reason": "hook judge response unparseable",
            "improve": ""}


JUDGE_SCRIPT_PROMPT = prompts.load("script_judge")


def _script_view(segs: list) -> str:
    """The spoken script as a compact numbered, labelled transcript for the
    judge — dialogue `lines` joined, else the segment `text`."""
    out = []
    for i, s in enumerate(segs, 1):
        if s.get("lines"):
            txt = " ".join((l.get("text") or "") for l in s["lines"])
        else:
            txt = s.get("text", "")
        out.append(f'{i}. [{s.get("label", "?")}] {txt}')
    return "\n".join(out)


def judge_script(segs: list, d: dict, prev: dict = None,
                 min_score: int = 8) -> dict:
    """Score the WHOLE spoken script 0-10 across a rubric (hook/flow/specificity/
    payoff/voice) — not just the hook. Two behaviours the old hook-only judge
    lacked:
      • GOOD ENOUGH gate — `pass` is set authoritatively here (score >= the
        template's script_min_score); a pass clears `improve` so the UI/CLI stop
        nagging for changes on a script that's already strong.
      • CONVERGENCE — `prev` (the previous round's judge dict) is shown to the
        model so it CHECKS whether its last ask was applied and acknowledges it,
        instead of inventing a fresh equal-weight nitpick every round.
    NEVER raises — a malformed reply scores neutral and keeps the script."""
    ch = config.channel()
    lt = learnings_tail(cap=1000)
    lt = (f"\nThe creator's learned standards — judge by THESE too:\n{lt}\n"
          if lt else "")
    pv = ""
    if prev and (prev.get("improve") or prev.get("reason")):
        ps = prev.get("score")
        pv = ("\n<previous_round>\n"
              + (f"Last round you scored this {ps}/10. " if ps is not None else "")
              + (f'Your critique was: "{prev.get("reason")}". '
                 if prev.get("reason") else "")
              + (f'You asked: "{prev.get("improve")}". ' if prev.get("improve")
                 else "")
              + "The script below is the creator's NEW draft after that. FIRST "
              "check whether your ask was addressed; if it was, ACKNOWLEDGE it and "
              "raise the score — do NOT re-litigate a point you already made or "
              "swap in a brand-new nitpick of equal weight. Only hold back a pass "
              "for a genuinely material REMAINING weakness.\n</previous_round>\n")
    prompt = JUDGE_SCRIPT_PROMPT.format(
        noun=ch.topic_noun, script=_script_view(segs), facts=_facts(d),
        learnings=lt, min_score=min_score, prev=pv)
    last = None
    for _ in range(2):                  # one retry — judge JSON occasionally drifts
        try:
            j = _json_block(_ask(prompt, max_tokens=500, role="judge"),
                            "{", "}")
            j["score"] = int(j.get("score", 0))
            # pass is decided HERE, not by the model, so "good enough" is
            # deterministic and a pass never carries a leftover nitpick.
            j["pass"] = j["score"] >= min_score
            if j["pass"]:
                j["improve"] = ""
            return j
        except (ValueError, TypeError) as e:
            last = e
    print(f"  ! script judge unparseable ({last}) — scoring it neutral, keeping "
          f"the script", file=sys.stderr)
    return {"score": 0, "pass": False, "reason": "script judge unparseable",
            "improve": "", "scores": {}}


JUDGE_DIRECTION_PROMPT = prompts.load("direction_judge")


def _direction_summary(script: dict) -> str:
    """A compact JSON view of the direction PLAN for the judge — the top-level
    bible + each beat's visual/motion/overlay/shots (long fields truncated)."""
    d = script.get("direction") or {}
    def clip(v, n=200):
        return (v or "")[:n]
    segs = []
    for s in script.get("segments", []):
        item = {"label": s.get("label"),
                "visual": clip(s.get("visual")),
                "motion": clip(s.get("motion"), 120),
                "clip_motion": clip(s.get("clip_motion")),
                "overlay": s.get("overlay") or [
                    o.get("text") for o in (s.get("overlays") or [])],
                "animate": s.get("animate", True)}
        shots = s.get("shots")
        if isinstance(shots, list) and len(shots) >= 2:
            item["shots"] = [{"visual": clip(sp.get("visual"), 110),
                              "clip_motion": clip(sp.get("clip_motion"), 110),
                              "animate": sp.get("animate", True)}
                             for sp in shots]
        segs.append(item)
    return json.dumps({"visual_concept": clip(d.get("visual_concept"), 400),
                       "anchor_motif": clip(d.get("anchor_motif"), 200),
                       "reveal_arc": clip(d.get("reveal_arc"), 200),
                       "style": clip(d.get("style"), 200),
                       "segments": segs}, indent=1)


def judge_direction(script: dict, facts: dict, concept: str = "") -> dict:
    """Score the DIRECTOR'S shot plan 0-10 across physics / theme / freshness /
    kinetics / audience / clarity — the visual analogue of judge_hook, run before any
    keyframe credits are spent. NEVER raises (a malformed judge reply is
    transient LLM formatting, not a reason to discard a good plan): one retry,
    then a neutral score. ADVISORY only — the creator decides whether to act on
    it (like the hook judge)."""
    ch = config.channel()
    lt = learnings_tail(cap=1000)
    lt = (f"\nThe creator's learned standards — judge by THESE too:\n{lt}\n"
          if lt else "")
    prompt = JUDGE_DIRECTION_PROMPT.format(
        channel=ch.title, premise=ch.premise,
        concept=(concept or "(no fixed concept — judge the world it invents)"),
        plan=_direction_summary(script), facts=_facts(facts), learnings=lt)
    last = None
    for _ in range(2):
        try:
            j = _json_block(_ask(prompt, max_tokens=500, role="judge"),
                            "{", "}")
            scores = {k: int(j.get("scores", {}).get(k, 0))
                      for k in ("physics", "theme", "freshness", "kinetics",
                                "audience", "clarity")}
            j["scores"] = scores
            j["overall"] = int(j.get("overall")
                               or round(sum(scores.values()) / len(scores)))
            j["segments"] = _clean_judge_segments(j.get("segments"))
            _fill_judge_improve(j)
            return j
        except (ValueError, TypeError) as e:
            last = e
    print(f"  ! director judge unparseable ({last}) — scoring it neutral",
          file=sys.stderr)
    return {"scores": {}, "overall": 0, "weakest": "", "segments": [],
            "reason": "director judge response unparseable", "improve": ""}


def _clean_judge_segments(raw) -> list:
    """Normalize the director judge's PER-SECTION critique to a clean list of
    {label, issue, improve} — one per beat that needs work, each with an
    actionable per-beat fix the director agent can apply verbatim. Anything
    without a label + a usable note is dropped."""
    out = []
    if isinstance(raw, list):
        for it in raw:
            if not isinstance(it, dict):
                continue
            label = str(it.get("label") or "").strip()
            issue = str(it.get("issue") or "").strip()
            improve = str(it.get("improve") or issue).strip()
            if label and improve:
                out.append({"label": label, "issue": issue, "improve": improve})
    return out


def _fill_judge_improve(j: dict) -> dict:
    """GUARANTEE an actionable `improve` note on a judge dict — the model
    sometimes omits it, which left the UI with no 'apply the judge's fix'
    option. Synthesize one from the weakest dimension + the reason, and normalize
    the per-section critique. In place; returns the dict."""
    j["segments"] = _clean_judge_segments(j.get("segments"))
    if not (j.get("improve") or "").strip():
        weakest = (j.get("weakest") or "").strip()
        reason = (j.get("reason") or "").strip()
        if reason or weakest:
            j["improve"] = (
                (f"Strengthen {weakest}: " if weakest else "Fix this: ")
                + (reason or "address the weakest dimension")
                + " — keep the facts and verdict locked.")
    return j


def ensure_direction_judge(topic: str, regen: bool = False,
                           compute: bool = True) -> dict:
    """Make sure a DIRECTED script carries a director judge WITH an actionable
    improve note. Lets a run directed before the judge existed (or one whose
    judge lacks `improve`) surface a usable fix without a re-plan.
      - an existing judge is NORMALIZED (improve backfilled, no LLM) and saved.
      - a missing judge is computed (compute=True + an LLM backend), else {}.
    `compute=False` is the cheap normalize-only path the webui state builder uses
    (it must never block on an LLM call). Advisory; never raises fatally."""
    folder = config.topic_dir(topic)
    p = folder / "script.json"
    if not p.exists():
        return {}
    script = json.loads(p.read_text())
    if not script.get("directed"):
        return {}
    d = script.setdefault("direction", {})
    existing = d.get("judge")
    if existing and not regen:
        before = dict(existing)
        _fill_judge_improve(existing)
        if existing != before:                  # backfilled improve → persist
            p.write_text(json.dumps(script, indent=2))
        return existing
    if not compute or not llm.available():
        return existing or {}
    facts = json.loads((folder / "facts.json").read_text())
    j = judge_direction(script, facts, user_concept(folder))
    d["judge"] = j
    p.write_text(json.dumps(script, indent=2))
    return j


def _words(text: str) -> int:
    """Spoken-word count — delivery punctuation ('…', a lone em dash)
    doesn't inflate it."""
    return len([w for w in text.split() if any(c.isalnum() for c in w)])


def _validate(segs: list, tpl: dict, ch) -> list[str]:
    """End validation + soft problems. The writer chooses the ORDER, but the
    script must contain EXACTLY the channel's required label set — every label
    once, none missing, no extras, no duplicates (order-independent). A bad set
    raises (the attempt is rejected with explicit feedback); soft problems are
    just reported back for the next try. Per-segment caps are looked up BY LABEL
    (not position) since the order is now the writer's."""
    got = [s.get("label") for s in segs]
    required = ch.required_labels
    dupes = sorted({l for l in got if got.count(l) > 1})
    if ch.flexible_segments:
        # the segment list is a SUGGESTED menu — the writer may combine, drop,
        # add and reorder beats. Only enforce: each `required: true` beat present,
        # labels unique (so artifact/label↔index maps stay clean), at least 2
        # beats, and every label a non-empty UPPERCASE-ish token.
        missing = [l for l in required if l not in got]
        blank = [i for i, l in enumerate(segs) if not (l.get("label") or "").strip()]
        if missing or dupes or blank or len(segs) < 2:
            raise ValueError(
                "flexible segments — shape the beats freely, but: keep each "
                "label UNIQUE, give every segment a short label, write at least "
                "2 beats"
                + (f", and INCLUDE the required beat(s) {required}" if required
                   else "")
                + f". got={got}"
                + (f"; missing_required={missing}" if missing else "")
                + (f"; duplicated={dupes}" if dupes else "")
                + ("; some segments have no label" if blank else ""))
    else:
        optional = set(ch.optional_labels)
        known = set(ch.labels)
        missing = [l for l in required if l not in got]
        extra = [l for l in got if l not in known]
        if missing or extra or dupes:
            raise ValueError(
                "must include every required label exactly once, in any order "
                + (f"(optional, include at most once: {sorted(optional)}) "
                   if optional else "")
                + f"— required={required}; got={got}"
                + (f"; missing={missing}" if missing else "")
                + (f"; unexpected={extra}" if extra else "")
                + (f"; duplicated={dupes}" if dupes else ""))
    problems = []
    words = sum(_words(s["text"]) for s in segs)
    lo, hi = tpl["target_words"]
    if not lo <= words <= hi + 10:
        problems.append(f"total {words} words, target {lo}-{hi}")
    for s in segs:
        cap = ch.segment(s["label"]).get("max_words")
        n = _words(s["text"])
        if cap and n > cap:
            problems.append(f"{s['label']} {n} words, max {cap}")
        if not (s.get("text") or "").strip():
            problems.append(f"{s['label']} empty text")
    # overlay + visual are the DIRECTOR's job now — not validated at write time
    return problems


def user_concept(folder) -> str:
    """The creator's per-video CONCEPT (hook idea + theme/motif) set in the
    interactive `concept` stage — drives the writer's hook and the director's
    visual through-line. '' when none/skipped."""
    p = Path(folder) / "concept.json"
    if not p.exists():
        return ""
    try:
        return (json.loads(p.read_text()).get("concept") or "").strip()
    except (ValueError, OSError):
        return ""


# ---- dropped reference moodboard (concept/ folder) --------------------------
# The creator drops images (videos coming later) into <run>/concept/ to define
# a LOOK they want to recreate. A vision pass extracts the transferable style
# into concept_refs.json, which then steers the concept + both directors.
_REF_IMG_EXT = (".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp")
_REF_VID_EXT = (".mp4", ".mov", ".webm", ".m4v", ".avi", ".mkv")

REF_SYS = prompts.load("reference_brief.system")

REF_USER = prompts.load("reference_brief.user")


def _reference_files(folder):
    rdir = Path(folder) / "concept"
    if not rdir.exists():
        return [], []
    imgs = sorted(p for p in rdir.iterdir()
                  if p.is_file() and p.suffix.lower() in _REF_IMG_EXT)
    vids = sorted(p for p in rdir.iterdir()
                  if p.is_file() and p.suffix.lower() in _REF_VID_EXT)
    return imgs, vids


def extract_reference_brief(topic_or_folder, regen: bool = False) -> dict:
    """Vision pass over the creator's dropped reference IMAGES in <run>/concept/
    → a structured STYLE brief written to concept_refs.json (used to recreate
    the look). Cached on the newest reference's mtime; $0 on the subscription.
    No refs / no LLM backend → {}. Video refs are detected but not yet sampled
    (images first; video analysis lands next)."""
    folder = (topic_or_folder if isinstance(topic_or_folder, Path)
              else config.topic_dir(topic_or_folder))
    imgs, vids = _reference_files(folder)
    out = folder / "concept_refs.json"
    if not imgs and not vids:
        return {}
    if vids:
        print(f"  ({len(vids)} video reference(s) found — video analysis is "
              "coming next; using the images for now)")
    if not imgs:
        return json.loads(out.read_text()) if out.exists() else {}
    newest = max(p.stat().st_mtime for p in imgs)
    if out.exists() and not regen:
        try:
            prev = json.loads(out.read_text())
            if prev.get("ts", 0) >= newest:
                return prev
        except ValueError:
            pass
    if not llm.available():
        return json.loads(out.read_text()) if out.exists() else {}
    # cap the frames sent to the vision pass, but EVENLY across the set (not the
    # first N) so a long clip's whole arc is covered, not just its opening
    cap = _ref_max_frames()
    if len(imgs) > cap:
        step = len(imgs) / cap
        sel = [imgs[min(int(k * step), len(imgs) - 1)] for k in range(cap)]
    else:
        sel = imgs
    paths = [str(p) for p in sel]
    # video-link sources (reference_from_url) carry a caption + a per-link FOCUS
    # = exactly what the creator wants to pull; fold both into the prompt so the
    # extraction is steered by the creator, not hardcoded.
    srcs = _ref_sources(folder)
    caps = [s.get("caption", "") for s in srcs if s.get("caption")]
    focuses = [s.get("focus", "") for s in srcs if s.get("focus")]
    ctx = ""
    if srcs:
        ctx += ("\n\nSome frames are stills sampled IN TIME ORDER across short "
                "VIDEOS the creator wants to emulate — read the progression to "
                "infer the editing rhythm/cuts and motion, and recreate the "
                "visual CONCEPT (look, framing, pacing, on-screen text style), "
                "never the specific subject/person/brand.")
        if caps:
            ctx += "\nVideo caption(s): " + " | ".join(c[:200] for c in caps)
    if focuses:
        ctx += ("\n\nThe creator specifically wants to PULL THIS from the "
                "reference(s) — center the brief on it: "
                + " | ".join(f[:300] for f in focuses))
    print(f"  reading {len(paths)} reference image(s) from concept/…")
    try:
        txt = llm.ask(REF_USER + ctx, system=REF_SYS, max_tokens=1200,
                      image_paths=paths)
        brief = _json_block(txt, "{", "}")
    except (ValueError, RuntimeError) as e:
        print(f"  ! reference extraction failed: {str(e)[:120]}",
              file=sys.stderr)
        return json.loads(out.read_text()) if out.exists() else {}
    rec = {"brief": brief, "sources": [p.name for p in imgs], "ts": newest}
    if srcs:
        rec["links"] = [{"url": s.get("url"), "focus": s.get("focus", "")}
                        for s in srcs if s.get("url")]
    out.write_text(json.dumps(rec, indent=2))
    print(f"  ✓ reference brief: {(brief.get('summary') or '')[:90]}")
    return rec


def reference_brief(folder) -> str:
    """The dropped-reference style brief as a compact prompt block, or ''."""
    p = Path(folder) / "concept_refs.json"
    if not p.exists():
        return ""
    try:
        rec = json.loads(p.read_text())
    except ValueError:
        return ""
    b = rec.get("brief") or {}
    if not b:
        return ""
    lines = []
    if b.get("summary"):
        lines.append(str(b["summary"]))
    for k in ("themes", "palette", "lighting", "composition", "mood",
              "motifs", "typography", "recreate"):
        v = b.get(k)
        if not v:
            continue
        if isinstance(v, list):
            v = ", ".join(str(x) for x in v)
        lines.append(f"{k}: {v}")
    n = len(rec.get("sources") or [])
    links = rec.get("links") or []
    src = (f"The creator referenced {len(links)} short VIDEO link(s)"
           + (f" + {n - len(links)} image(s)" if n > len(links) else "")
           if links else f"The creator dropped {n} reference image(s)")
    focuses = [l.get("focus") for l in links if l.get("focus")]
    head = (f"{src} to define the LOOK they want — RECREATE this style/vibe; do "
            "NOT copy any specific subject, person, brand or logo from them"
            + (". They specifically want to pull: " + "; ".join(focuses)
               if focuses else "") + ":")
    return head + "\n" + "\n".join(lines)


def _ensure_reference_brief(folder) -> str:
    """Extract (cached) then return the brief text — so the directors and the
    concept tools pick up dropped refs automatically. Never fatal."""
    try:
        extract_reference_brief(Path(folder))
    except Exception as e:                        # noqa: BLE001
        print(f"  ! reference brief skipped: {str(e)[:100]}", file=sys.stderr)
    return reference_brief(folder)


# ---- reference from a TikTok/Instagram VIDEO LINK --------------------------
# Give a link to a short video and pull frames + caption into concept/ so the
# look STEERS the concept + directors like dropped images. The creator says what
# to PULL (focus) — it's never hardcoded.
_REF_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
           "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


def _ref_frame_interval() -> float:
    """Seconds between sampled reference frames — one frame every N seconds of the
    source video. Configurable via KALINGA_REF_FRAME_SECS (default 10)."""
    try:
        v = float(os.environ.get("KALINGA_REF_FRAME_SECS", "10") or 10)
        return v if v >= 1 else 10.0
    except ValueError:
        return 10.0


def _ref_max_frames() -> int:
    """Cap on how many frames are sent to the vision pass (keeps the prompt + cost
    bounded). Configurable via KALINGA_REF_MAX_FRAMES (default 8)."""
    try:
        v = int(os.environ.get("KALINGA_REF_MAX_FRAMES", "8") or 8)
        return max(2, min(v, 30))
    except ValueError:
        return 8


def _ref_sources(folder) -> list:
    """The recorded video-link sources for this run (concept/_sources.json)."""
    sp = Path(folder) / "concept" / "_sources.json"
    if not sp.exists():
        return []
    try:
        return json.loads(sp.read_text())
    except (ValueError, OSError):
        return []


def _ref_http_get(url: str, timeout: int = 25) -> bytes:
    import urllib.request
    req = urllib.request.Request(url, headers={"User-Agent": _REF_UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def _oembed_poster(url: str):
    """(thumbnail_url, caption) via a provider oEmbed endpoint, else (None,'')."""
    import urllib.parse
    host = urllib.parse.urlparse(url).netloc.lower()
    if "tiktok" in host:
        ep = "https://www.tiktok.com/oembed?url=" + urllib.parse.quote(url, safe="")
    else:
        return None, ""                          # IG public oEmbed needs a token
    try:
        j = json.loads(_ref_http_get(ep))
        return j.get("thumbnail_url"), (j.get("title") or "")
    except Exception:                            # noqa: BLE001
        return None, ""


def _og_poster(url: str):
    """(image_url, caption) scraped from the page's OpenGraph meta tags."""
    import html as _html
    import re as _re
    try:
        page = _ref_http_get(url).decode("utf-8", "ignore")
    except Exception:                            # noqa: BLE001
        return None, ""

    def meta(prop):
        for pat in (r'<meta[^>]+(?:property|name)=["\']%s["\'][^>]+content=["\']([^"\']+)',
                    r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+(?:property|name)=["\']%s["\']'):
            m = _re.search(pat % _re.escape(prop), page, _re.I)
            if m:
                return _html.unescape(m.group(1))
        return None
    img = meta("og:image") or meta("og:image:secure_url")
    cap = meta("og:description") or meta("og:title") or ""
    return img, cap


def _vtt_text(path: Path) -> str:
    """Plain transcript text from a .vtt subtitle file — strips the WEBVTT header,
    cue numbers, timestamps and inline tags, and dedupes the consecutive repeats
    auto-captions emit. '' on any trouble."""
    import re as _re
    try:
        raw = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return ""
    out, prev = [], None
    for ln in raw.splitlines():
        s = ln.strip()
        if (not s or s == "WEBVTT" or "-->" in s or s.isdigit()
                or s.startswith(("NOTE", "Kind:", "Language:"))):
            continue
        s = _re.sub(r"<[^>]+>", "", s)          # <c>, <00:00:01.000> inline tags
        s = _re.sub(r"\s+", " ", s).strip()
        if s and s != prev:
            out.append(s)
            prev = s
    return " ".join(out).strip()


def _ytdlp_cmd():
    """How to invoke yt-dlp: the PATH binary, else `python -m yt_dlp` (pip
    installs the module without always putting the script on PATH), else None."""
    import importlib.util
    import shutil as _sh
    import sys as _sys
    if _sh.which("yt-dlp"):
        return ["yt-dlp"]
    if importlib.util.find_spec("yt_dlp"):
        return [_sys.executable, "-m", "yt_dlp"]
    return None


def _ytdlp_frames(url: str, rdir: Path, slug: str, interval: float = None):
    """(saved_frame_paths, caption, audio_path|None, transcript) — download the
    video with yt-dlp (covers TikTok + Instagram + most sites), ffmpeg-sample
    frames in TIME ORDER (one every `interval` seconds, default
    KALINGA_REF_FRAME_SECS / 10) into concept/ so the vision pass sees the actual
    video (not just a poster), save a copy of the AUDIO track
    (concept/ref_<slug>_audio.mp3) for reuse as SFX, AND pull the spoken/on-screen
    TEXT (subtitles → transcript) so the SCRIPT WRITER can use the wording.
    ([], '', None, '') if yt-dlp/ffmpeg aren't available or it fails."""
    import shutil as _sh
    import subprocess
    import tempfile
    ydl = _ytdlp_cmd()
    if not (ydl and _sh.which("ffmpeg")):
        return [], "", None, ""
    td = Path(tempfile.mkdtemp())
    try:
        r = subprocess.run(
            ydl + ["--no-playlist", "-q", "--write-info-json",
                   "--write-auto-subs", "--write-subs", "--sub-langs", "en.*",
                   "--sub-format", "vtt", "-o", str(td / "v.%(ext)s"), url],
            capture_output=True, text=True, timeout=300)
        caption = ""
        ij = list(td.glob("*.info.json"))
        if ij:
            try:
                j = json.loads(ij[0].read_text())
                caption = (j.get("description") or j.get("title") or "")
            except ValueError:
                pass
        # spoken / on-screen text from any subtitle track yt-dlp fetched
        transcript = ""
        for vtt in sorted(td.glob("*.vtt")):
            transcript = _vtt_text(vtt)
            if transcript:
                break
        vids = [p for p in td.glob("v.*")
                if p.suffix.lower() in _REF_VID_EXT and "." not in p.stem]
        if r.returncode != 0 or not vids:
            return [], caption, None, transcript
        v = vids[0]
        dur = 0.0
        try:
            dur = float(subprocess.run(
                ["ffprobe", "-v", "error", "-show_entries", "format=duration",
                 "-of", "default=nw=1:nk=1", str(v)],
                capture_output=True, text=True, timeout=30).stdout.strip() or 0)
        except Exception:                        # noqa: BLE001
            pass
        # one frame every `interval` seconds (configurable), bounded so a very
        # short clip still gets a few and a very long one doesn't explode
        interval = interval or _ref_frame_interval()
        if dur > 1:
            n = max(3, min(int(round(dur / interval)), 60))
            fps = n / dur
        else:
            n, fps = 3, 1.0
        subprocess.run(
            ["ffmpeg", "-y", "-i", str(v), "-vf", f"fps={fps:.5f},scale=720:-2",
             "-frames:v", str(n), str(rdir / f"ref_{slug}_%02d.jpg")],
            capture_output=True, text=True, timeout=120)
        print(f"  sampled {n} frame(s) (~1 every {interval:.0f}s of {dur:.0f}s)")
        # save the AUDIO track too — reusable as SFX (best-effort; a silent clip
        # has no audio stream → ffmpeg fails → no file, fine)
        audio = rdir / f"ref_{slug}_audio.mp3"
        ar = subprocess.run(
            ["ffmpeg", "-y", "-i", str(v), "-vn", "-c:a", "libmp3lame",
             "-q:a", "2", str(audio)],
            capture_output=True, text=True, timeout=120)
        if not (ar.returncode == 0 and audio.exists() and audio.stat().st_size):
            audio = None
        if transcript:
            print(f"  pulled transcript ({len(transcript)} chars) for the writer")
        return sorted(rdir.glob(f"ref_{slug}_*.jpg")), caption, audio, transcript
    except Exception as e:                        # noqa: BLE001
        print(f"  ! yt-dlp frame sampling failed: {str(e)[:100]}", file=sys.stderr)
        return [], "", None, ""
    finally:
        import shutil as _sh2
        _sh2.rmtree(td, ignore_errors=True)


def _fetch_url_reference(url: str, rdir: Path, interval: float = None) -> dict:
    """Pull frame(s) + caption for a TikTok/IG link into concept/. Prefers
    yt-dlp frame sampling (one frame every `interval` seconds); falls back to a
    single oEmbed/og:image poster frame. {saved:[Path], caption:str, via:str}."""
    import re as _re
    import urllib.parse
    slug = (_re.sub(r"[^a-z0-9]+", "", urllib.parse.urlparse(url).path.lower())[:24]
            or "link")
    saved, caption, audio, transcript = _ytdlp_frames(
        url, rdir, slug, interval=interval)
    via = "yt-dlp frames" if saved else ""
    if not saved:
        thumb, cap = _oembed_poster(url)
        caption = caption or cap
        if not thumb:
            thumb, cap2 = _og_poster(url)
            caption = caption or cap2
        if thumb:
            try:
                (rdir / f"ref_{slug}.jpg").write_bytes(_ref_http_get(thumb))
                saved = [rdir / f"ref_{slug}.jpg"]
                via = "poster frame"
            except Exception as e:               # noqa: BLE001
                print(f"  ! poster download failed: {str(e)[:90]}", file=sys.stderr)
    return {"saved": saved, "caption": (caption or "").strip(), "via": via,
            "audio": audio, "transcript": (transcript or "").strip()}


def reference_from_url(topic_or_folder, url: str, focus: str = "",
                       interval: float = None, regen: bool = True) -> dict:
    """Pull a TikTok/Instagram VIDEO LINK into the reference moodboard so it
    STEERS the concept + both directors like dropped images ("recreate this look,
    never copy it"). Fetches frame(s) + the caption into <run>/concept/ (one frame
    every `interval` seconds, default 10), records the source + the creator's
    FOCUS (what they want to pull — never hardcoded), then runs the style-brief
    vision pass. Best-effort (yt-dlp optional, else a poster frame); returns the
    brief record, or {} if nothing could be pulled."""
    folder = (topic_or_folder if isinstance(topic_or_folder, Path)
              else config.topic_dir(topic_or_folder))
    rdir = folder / "concept"
    rdir.mkdir(exist_ok=True)
    info = _fetch_url_reference(url, rdir, interval=interval)
    if not info.get("saved"):
        print(f"  ! couldn't pull a frame from {url}. Tip: install yt-dlp for "
              "full video frames (pip install yt-dlp), or screenshot the video "
              f"into {rdir}/ and press [i].", file=sys.stderr)
        return {}
    audio = info.get("audio")
    srcs = _ref_sources(folder)
    srcs.append({"url": url, "focus": (focus or "").strip(),
                 "caption": info.get("caption", ""),
                 "transcript": info.get("transcript", ""),
                 "frames": [p.name for p in info["saved"]],
                 "audio": audio.name if audio else "",
                 "via": info.get("via", "")})
    (rdir / "_sources.json").write_text(json.dumps(srcs, indent=2))
    print(f"  pulled {len(info['saved'])} frame(s) via {info['via']}"
          + (f" · caption: {info['caption'][:70]}" if info.get("caption") else "")
          + (f"\n  focus: {focus}" if focus else ""))
    if audio:
        print(f"  saved audio for SFX → concept/{audio.name}")
    return extract_reference_brief(folder, regen=regen)


def reference_text(folder) -> str:
    """The TEXT pulled from any video-link reference(s) — the creator's focus +
    the caption + the spoken/on-screen TRANSCRIPT — as a block for the SCRIPT
    WRITER (inspiration for the hook, structure and phrasing energy). '' when no
    link source carries text."""
    out = []
    for s in _ref_sources(folder):
        bits = []
        if s.get("focus"):
            bits.append(f"emulate: {s['focus']}")
        if s.get("caption"):
            bits.append(f"caption: {s['caption'][:300]}")
        if s.get("transcript"):
            bits.append(f"transcript: {s['transcript'][:900]}")
        if bits:
            out.append("• " + "\n  ".join(bits))
    if not out:
        return ""
    return ("The creator referenced these video(s) to emulate — use the "
            "caption/transcript for the HOOK angle, STRUCTURE and phrasing "
            "ENERGY, but write ORIGINAL words and stay 100% true to <facts>; "
            "never copy a reference verbatim:\n" + "\n".join(out))


def music_brief(topic: str) -> str:
    """A ONE-LINE instrumental background-music prompt for ElevenLabs Music ($0),
    designed from the episode concept + the script's mood so the track fits the
    video. Instrumental by construction (it sits under the voiceover). '' with no
    LLM backend — the caller then uses a sensible default."""
    if not llm.available():
        return ""
    folder = config.topic_dir(topic)
    concept = user_concept(folder)
    sp = folder / "script.json"
    script = json.loads(sp.read_text()) if sp.exists() else {}
    arc = (script.get("direction") or {}).get("reveal_arc") or ""
    ch = config.channel()
    system = ("You write ONE LINE: an instrumental background-music prompt for a "
              "short vertical video. NO vocals, NO lyrics. It must sit UNDER a "
              "voiceover — atmospheric and restrained, never busy or distracting. "
              "Name the genre, a couple of instruments, the mood, and the energy "
              "arc in a single sentence.")
    user = (f"Channel: {ch.title}. {ch.premise}\n"
            + (f"Episode concept: {concept}\n" if concept else "")
            + (f"Reveal arc: {arc}\n" if arc else "")
            + "Write ONE instrumental music prompt (no vocals). Return ONLY the "
            "prompt text.")
    try:
        return llm.ask(user, system=system, max_tokens=120).strip()
    except Exception:                       # noqa: BLE001 — best-effort
        return ""


def suggest_topics(n: int = 8, seed: str = "") -> list:
    """TOPIC IDEATION for the active channel/SHOW — fresh subjects for the
    queue, not concepts for one video. Reads the show's premise + everything
    already queued or produced (never repeats), and returns
    [{topic, why}] — `topic` in the queue's hyphenated style, `why` one
    editorial line on the angle. With a `seed` (the creator's rough idea) it
    REFINES that idea into concrete topic options instead of ideating from
    scratch. [] without an LLM backend. $0 on the subscription."""
    if not llm.available():
        return []
    ch = config.channel()
    import daily
    try:
        taken = [r[0].strip() for r in daily.load_queue() if r and r[0].strip()]
    except FileNotFoundError:
        taken = []
    show_line = (f' (the "{ch.show}" show)' if ch.show else "")
    seed_block = (
        f"\n\nThe creator's ROUGH IDEA — refine THIS into the options (stay "
        f"true to its intent; make it concrete, researchable and specific):\n"
        f"{seed}\n" if (seed or "").strip() else "")
    text = llm.ask(
        f'You pick topics for "{ch.title}"{show_line} — {ch.premise}\n'
        f"Persona: {ch.persona}\n\n"
        "Topics ALREADY taken (queued or produced) — never repeat or "
        "near-duplicate any:\n"
        + ("\n".join(f"- {t}" for t in taken) or "(none yet)")
        + seed_block
        + f"\n\nPropose {n} NEW topics this audience would stop for. Each "
        "must be a real, researchable subject with verifiable facts — not a "
        "listicle, not a vague theme. Vary era, geography and fame (a couple "
        "of well-known bankers + several fresher picks beat all-obvious "
        "choices).\n"
        'Return JSON only: [{"topic": "Hyphenated-Name-Like-The-Queue", '
        '"why": "<one line: the angle that makes it a great episode>"}] '
        f"with exactly {n} items.", max_tokens=1200)
    try:
        j = json.loads(text[text.find("["):text.rfind("]") + 1])
    except ValueError:
        return []
    low_taken = {t.lower() for t in taken}
    out = []
    for it in j if isinstance(j, list) else []:
        t = re.sub(r"\s+", "-", str((it or {}).get("topic") or "").strip())
        t = re.sub(r"[^A-Za-z0-9-]", "", t).strip("-")
        if t and t.lower() not in low_taken:
            out.append({"topic": t,
                        "why": str((it or {}).get("why") or "").strip()})
    return out[:n]


def suggest_concept(topic: str) -> str:
    """Suggest a FRESH creative concept for this video — same spirit as the
    past ones (a short, character-driven skit + a recurring visual motif tied
    to the company) but NOT a repeat. Reads what's already been done from the
    queue so it varies the scenario/motif each time. '' if no LLM backend."""
    if not llm.available():
        return ""
    ch = config.channel()
    folder = config.topic_dir(topic)
    facts = json.loads((folder / "facts.json").read_text())
    import daily
    past = daily.concepts(exclude=topic)
    done = "\n".join(f"- {t}: {c}" for t, c in past) or "(none yet)"
    system = (
        f'You are the creative director for "{ch.title}". {ch.premise} Each '
        "video OPENS on a short, character-driven SKIT built around a recurring "
        "visual MOTIF tied to the company, which leads into the question the "
        "episode answers. You invent that concept.")
    refs = _ensure_reference_brief(folder)
    refblock = (("\n\nREFERENCE LOOK the creator dropped — root the concept's "
                 "WORLD, setting and motif in THIS style:\n" + refs)
                if refs else "")
    user = (
        "Concepts ALREADY USED — do NOT repeat any (need a new scenario, a new "
        "motif, a fresh setting):\n" + done
        + "\n\nCompany facts for the NEW video:\n" + _facts(facts)
        + refblock
        + "\n\nPropose ONE fresh concept: a 2-3 sentence skit idea + the "
        "recurring motif, rooted in THIS company's own world (its product, "
        "sector or imagery)"
        + (" AND in the reference look above" if refs else "")
        + " — same spirit as the past ones but clearly "
        "different. Keep it producible (1-3 characters, one location) and true "
        "to the facts/verdict. Return ONLY the concept text.")
    try:
        return llm.ask(user, system=system, max_tokens=400).strip()
    except Exception:                       # noqa: BLE001 — suggestion is best-effort
        return ""


def refine_concept(topic: str, primers: str, prior: str = "") -> str:
    """Take the creator's ROUGH primers (a half-formed idea, a few notes) and
    write a polished, producible CONCEPT from them — the creator's intent
    sharpened, not replaced. `prior` is the previous refined draft when the
    creator is iterating ("tweak it like this"), so the loop builds on it.
    Stays rooted in this company's world + true to the facts/verdict. '' if no
    LLM backend / empty primers."""
    if not llm.available() or not (primers or "").strip():
        return ""
    ch = config.channel()
    folder = config.topic_dir(topic)
    facts = json.loads((folder / "facts.json").read_text())
    system = (
        f'You are the creative director for "{ch.title}". {ch.premise} Each '
        "video OPENS on a short, character-driven SKIT built around a recurring "
        "visual MOTIF tied to the company, leading into the question the episode "
        "answers. The creator gives you their rough idea; you SHARPEN it into a "
        "crisp, producible concept — their intent, made better, NOT a different "
        "idea.")
    refs = _ensure_reference_brief(folder)
    refblock = (("\n\nREFERENCE LOOK the creator dropped — root the concept's "
                 "world/motif in THIS style:\n" + refs) if refs else "")
    user = (
        "Company facts for this video:\n" + _facts(facts)
        + refblock
        + (("\n\nThe current refined concept (improve THIS per the new "
            "notes — keep what works):\n" + prior) if prior.strip() else "")
        + "\n\nThe creator's notes / rough idea:\n" + primers.strip()
        + "\n\nWrite the refined concept: a 2-3 sentence skit idea + the "
        "recurring motif, rooted in THIS company's own world (product, sector "
        "or imagery)"
        + (" and the reference look above" if refs else "")
        + ", faithful to the creator's notes and to the facts/verdict, "
        "and producible (1-3 characters, one location). Return ONLY the concept "
        "text — no preamble.")
    try:
        return llm.ask(user, system=system, max_tokens=400).strip()
    except Exception:                       # noqa: BLE001 — best-effort
        return ""


def refine_prompt(kind: str, current: str, notes: str,
                  context: str = "") -> str:
    """AI-refine an IMAGE or VIDEO generation prompt. The CURRENT prompt is
    passed in as the BASE to work from (so the model knows exactly what it is
    revising) and `notes` are the creator's change requests. Sections are
    XML-tagged so the model never confuses the base prompt, the notes and the
    rules. Returns ONLY the revised prompt text (it is sent verbatim to the
    image/video model). '' on no backend / empty inputs / failure — the caller
    then keeps the current prompt."""
    if not llm.available() or not (current or "").strip() \
            or not (notes or "").strip():
        return ""
    is_img = kind == "image"
    medium = "image (keyframe)" if is_img else "video (clip)"
    engine = "Nano Banana / nano_banana_2" if is_img else "Seedance / seedance_2_0"
    if is_img:
        rules = (
            "- It is a single still 9:16 vertical keyframe. KEEP the prompt "
            "ending with the 9:16 / clean-bottom-third requirement.\n"
            "- Keep the scene CLEAN and WORDLESS: do NOT introduce captions, "
            "titles, labels, logos, signage, or extra rubber-stamps/seals/brand "
            "marks (at most the one corner badge the base prompt already names "
            "stays; never add more).\n"
            "- Commit to specifics — lens/framing, motivated light, "
            "composition, texture, one focal point. Kill generic AI-slop.")
    else:
        rules = (
            "- It is a DYNAMIC video brief: describe real ACTION/movement and "
            "camera, never just a zoom or drift.\n"
            "- Do NOT write any spoken dialogue words into the prompt — the "
            "exact lines are appended separately and attributed to each "
            "speaker; you write only the ACTION, blocking and staging.\n"
            "- Keep any character roster / who-is-in-the-scene detail the base "
            "prompt already carries.")
    system = (
        f"You are an expert prompt engineer for the {engine} {medium} model. "
        "The creator hands you the CURRENT prompt plus change notes. Revise the "
        "prompt: apply the notes, keep everything they don't touch, and respect "
        "the technical rules. Output is fed VERBATIM to the model.\n" + rules
        + "\nReturn ONLY the revised prompt text — no preamble, no quotes, no "
        "explanation.")
    user = (
        (f"<context>\n{context.strip()}\n</context>\n\n" if context.strip()
         else "")
        + "<current_prompt>\n" + current.strip() + "\n</current_prompt>\n\n"
        + "<creator_notes>\n" + notes.strip() + "\n</creator_notes>\n\n"
        + "<instructions>\nRewrite <current_prompt> applying <creator_notes>. "
        "Keep every part not implicated by the notes unchanged. Return ONLY the "
        "full revised prompt text.\n</instructions>")
    try:
        return llm.ask(user, system=system, max_tokens=1200).strip()
    except Exception:                       # noqa: BLE001 — best-effort
        return ""


def write_script(topic: str) -> dict:
    """facts.json -> script.json, gated by the hook judge. Returns the
    script dict (with judge metadata under "judge")."""
    ch = config.channel()
    folder = config.topic_dir(topic)
    d = json.loads((folder / "facts.json").read_text())
    tpl = templates.load_pinned(folder)
    learnings = learnings_tail()
    overrides = (json.loads(ch.overrides.read_text())
                 if ch.overrides.exists() else {})
    exp = overrides.get("script_instruction")
    concept = user_concept(folder)
    ref_text = reference_text(folder)

    base_user = (
        (f"<learnings>\n{learnings}\n— apply those learnings.\n</learnings>\n\n"
         if learnings else "")
        + (f"<concept>\nThe creator's agreed creative idea for THIS video. Build "
           f"the HOOK (the opening beat) and the whole piece's THEME around it; "
           f"make it the through-line, weave the running motif in, and let it "
           f"shape the framing/characters. Stay 100% true to the facts and the "
           f"verdict:\n{concept}\n</concept>\n\n" if concept else "")
        + (f"<reference_video>\n{ref_text}\n</reference_video>\n\n"
           if ref_text else "")
        + (f"<experiment>\nApply this one change today: {exp}\n</experiment>\n\n"
           if exp else "")
        + "<facts>\nTopic data — the ground truth; every number and the verdict "
          "are locked:\n" + _facts(d)
        + "\n</facts>\n\n<instructions>\nReturn ONLY the JSON array.\n"
          "</instructions>")

    system = _guardrails(tpl, ch)
    best, best_judge, best_meta, feedback = None, {"score": 0}, {}, ""
    # owner call 2026-06-15: do NOT auto-reroll on a low hook-JUDGE score —
    # keep the FIRST valid script and let the creator iterate (the judge score
    # is advisory, surfaced for info/learnings). We still retry up to
    # script_attempts ONLY when the model returns invalid JSON / a bad label
    # set — a hard failure we genuinely cannot proceed from.
    for attempt in range(1, tpl["script_attempts"] + 1):
        user = base_user + (
            f"\n\nYour previous reply was invalid ({feedback}). Return ONLY "
            f"valid JSON (array, or object with a segments array), labels "
            f"exactly: {', '.join(ch.labels)}." if feedback else "")
        try:
            segs, smeta = _segments_and_meta(_ask(user, system=system))
            for s in segs:
                _join_lines(s)
            problems = _validate(segs, tpl, ch)
        except ValueError as e:
            feedback = str(e)
            print(f"  script attempt {attempt}: invalid ({e}) — regenerating",
                  file=sys.stderr)
            continue
        judge = judge_script(segs, d, min_score=tpl["script_min_score"])
        words = sum(_words(s["text"]) for s in segs)
        cast_n = len(smeta.get("cast") or {})
        print(f"  script attempt {attempt}: script {judge['score']}/10 "
              f"({words}w{', ' + str(cast_n) + ' voices' if cast_n else ''})"
              f"{' — ' + '; '.join(problems) if problems else ''}"
              + ("  ✓ good enough" if judge.get("pass")
                 else " (below the bar — kept anyway, iterate to improve)"))
        best, best_judge, best_meta = segs, judge, smeta
        break       # keep the first valid script — never reroll on the judge

    if best is None:
        raise RuntimeError("script generation failed — the model returned "
                           "invalid output on every attempt")
    out = {"topic": d.get("topic", topic), "ticker": d.get("ticker"),
           "verdict": d.get("verdict"), "channel": ch.name, "mode": "llm",
           "model": llm.model(), "template": tpl["name"],
           "judge": best_judge, "segments": best}
    roster_cast = _roster_cast_for(best, ch)
    if roster_cast:                 # channel has a fixed cast — use the roster
        out["cast"] = roster_cast
    elif best_meta.get("cast"):     # else the writer's own ad-hoc cast
        out["cast"] = best_meta["cast"]
    if best_meta.get("setup"):
        out["setup"] = best_meta["setup"]
    (folder / "script.json").write_text(json.dumps(out, indent=2))
    print(f"  script.json ({llm.describe()}, hook {best_judge['score']}/10) "
          f"-> {folder.name}/")
    return out


REVISE_SEED = prompts.load("script_revise.seed")


def make_chat(topic: str):
    """A llm.Chat seeded for revise_script rounds: system prompt = the same
    guardrails write_script uses; resumes the script's recorded session when
    one exists (chat_session in script.json)."""
    folder = config.topic_dir(topic)
    tpl = templates.load_pinned(folder)
    p = folder / "script.json"
    script = json.loads(p.read_text()) if p.exists() else {}
    return llm.Chat(system=_guardrails(tpl, config.channel()),
                    session_id=script.get("chat_session"),
                    model=llm.model("script"))


def revise_script(topic: str, feedback: str, chat=None) -> dict:
    """One co-writing revision round. With a llm.Chat the model remembers
    every earlier round (the first round seeds facts + the CURRENT draft, so
    a resumed session always revises what's actually on disk); without one
    (fresh process, expired session) a single stateless call carries the
    draft + the prior feedback notes from draft_history. Same validation and
    a fresh, memoryless hook judge either way."""
    ch = config.channel()
    folder = config.topic_dir(topic)
    d = json.loads((folder / "facts.json").read_text())
    tpl = templates.load_pinned(folder)
    script = json.loads((folder / "script.json").read_text())
    draft = json.dumps(script["segments"], indent=1)

    def _round(fb: str) -> str:
        if chat is not None:
            if not chat.messages:
                return chat.ask(REVISE_SEED.format(
                    facts=_facts(d), draft=draft, feedback=fb))
            return chat.ask(f"{fb}\n\nReturn ONLY the full revised "
                            f"JSON array.")
        notes = [h["feedback"] for h in script.get("draft_history", [])
                 if h.get("feedback")]
        prior = ("\nEarlier revision notes, already applied:\n- "
                 + "\n- ".join(notes) + "\n") if notes else ""
        return _ask(
            "Topic data:\n" + _facts(d)
            + "\n\nCurrent script draft (JSON array of segments):\n" + draft
            + prior
            + "\nNew revision notes — apply them; keep everything not "
              "implicated unchanged; same labels, same order:\n" + fb
            + "\n\nReturn ONLY the full revised JSON array.",
            system=_guardrails(tpl, ch))

    try:
        segs, smeta = _segments_and_meta(_round(feedback))
        for s in segs:
            _join_lines(s)
        problems = _validate(segs, tpl, ch)
    except ValueError as e:
        segs, smeta = _segments_and_meta(_round(
            f"{feedback}\n\n(Your previous reply was invalid: {e}. Return "
            f"ONLY the corrected full JSON array, labels exactly: "
            f"{', '.join(ch.labels)}.)"))
        for s in segs:
            _join_lines(s)
        problems = _validate(segs, tpl, ch)
    for p in problems:
        print(f"  ! {p}")
    # pass the PRE-revision judge so the re-score CONVERGES — it checks whether
    # the last ask was applied instead of inventing a fresh nitpick each round.
    judge = judge_script(segs, d, prev=script.get("judge"),
                         min_score=tpl["script_min_score"])
    hist = script.setdefault("draft_history", [])
    hist.append({"feedback": feedback, "judge": script.get("judge"),
                 "segments": [dict(s) for s in script["segments"]]})
    del hist[:-10]   # cap: revisions can pile up in a long session
    # update only TEXT + overlay in place — keep each segment's director
    # fields (visual/motion/tease/ref_idx/animate) and the `directed`
    # flag, so a script tweak doesn't re-plan or re-render the whole video;
    # downstream only the segments whose text actually changed are redone.
    old = script["segments"]
    prev_texts = [s.get("text") for s in old]
    for i, ns in enumerate(segs):
        if i < len(old):
            old[i]["text"] = ns.get("text", old[i]["text"])
            old[i]["overlay"] = ns.get("overlay", old[i].get("overlay"))
            if "speaker" in ns:
                old[i]["speaker"] = ns["speaker"]
            if "lines" in ns:
                old[i]["lines"] = ns["lines"]
        else:
            old.append(ns)            # count grew (labels validated, rare)
            script.pop("directed", None)
    if len(segs) < len(old):
        del old[len(segs):]
        script.pop("directed", None)
    # a SECTION-NATIVE beat derives each section's `say` from the locked text —
    # re-split every beat whose spoken text changed so the sections voice and
    # caption the NEW sentences instead of the old draft's (the shot visuals are
    # kept where they still line up; _sectionize_beats reuses the shot list)
    resplit = [s for i, s in enumerate(old)
               if _has_section_say(s)
               and (i >= len(prev_texts) or s.get("text") != prev_texts[i])]
    if resplit:
        _sectionize_beats(resplit)
    # cast/setup: a channel roster always wins (rebuilt from who's used now);
    # else keep what exists, updating only if the revision restated them
    roster_cast = _roster_cast_for(old, ch)
    if roster_cast:
        script["cast"] = roster_cast
    elif smeta.get("cast"):
        script["cast"] = smeta["cast"]
    if smeta.get("setup"):
        script["setup"] = smeta["setup"]
    script["judge"] = judge
    script["mode"] = "llm-revised"
    if chat is not None:
        script["chat_session"] = chat.session_id
    import make_video
    make_video.archive(folder, "script.json")    # keep the pre-revision draft
    (folder / "script.json").write_text(json.dumps(script, indent=2))
    print(f"  script revised (hook {judge['score']}/10) — previous kept in "
          f".versions/")
    return script


# the facts/verdict lock — the director may rephrase but never alter numbers
# or flip the decision. Number-bearing tokens we hold constant per segment:
_NUM_WORDS = {
    "zero", "one", "two", "three", "four", "five", "six", "seven", "eight",
    "nine", "ten", "eleven", "twelve", "thirteen", "fourteen", "fifteen",
    "sixteen", "seventeen", "eighteen", "nineteen", "twenty", "thirty",
    "forty", "fifty", "sixty", "seventy", "eighty", "ninety", "hundred",
    "thousand", "million", "billion", "trillion", "point", "percent",
    "half", "quarter"}
def _neg_verdict() -> tuple:
    """The CHANNEL's verdict-flipping phrases (channel.yaml
    `negative_verdict_markers` + an automatic 'not <positive_verdict>').
    Channel vocabulary — the engine defines none of its own; a channel with
    no verdict returns () and the polarity checks are inert."""
    return config.channel().negative_verdict_markers


def _num_tokens(text: str):
    import re
    from collections import Counter
    toks = re.findall(r"\d[\d,\.]*|[a-z]+", text.lower())
    return Counter(t.strip(",.") for t in toks
                   if t and (t[0].isdigit() or t in _NUM_WORDS))


def _lock_text(primer: str, revised: str, positive: bool) -> bool:
    """True if `revised` keeps every number from `primer` and doesn't flip
    the verdict polarity — i.e. it's a safe rephrase, not a fact change."""
    if not revised or revised == primer:
        return True
    if _num_tokens(revised) != _num_tokens(primer):
        return False
    low = revised.lower()
    neg = _neg_verdict()
    if positive and any(p in low for p in neg) \
            and not any(p in primer.lower() for p in neg):
        return False
    return True


DIRECT_SYS = prompts.load("director.system")


def _direct_sys(ch, tpl: dict) -> str:
    """The director system prompt, filled in once (so the brand layer + visual
    rules reach every call site identically)."""
    brand = tpl.get("brand") or {}
    return DIRECT_SYS.format(
        title=ch.title, premise=ch.premise, persona=ch.persona,
        world=tpl["world"], kinds="",
        visrules=ch.visual_rules or "design freely",
        brand_palette=brand.get("palette", "the channel's signature colors"),
        brand_marks=brand.get(
            "marks", "the channel's brand marks where they fit the scene"))


def _direction_experiment() -> str:
    """The live experiment's instruction for the director (visual/pacing),
    from overrides.json — '' when none."""
    ch = config.channel()
    if not ch.overrides.exists():
        return ""
    ov = json.loads(ch.overrides.read_text())
    return (ov.get("direction_instruction") or "").strip()


_WPS = 2.6           # spoken words per second (matches interactive._WPS / the docs)
_CLIP_WINDOW = 8     # one generated clip covers ~this many seconds (max_clip_seconds)


def _seg_words(s: dict) -> int:
    """Spoken word count of a segment — joined dialogue `lines`, else `text`."""
    if s.get("lines"):
        txt = " ".join((l.get("text") or "") for l in s["lines"])
    else:
        txt = s.get("text", "")
    return len(txt.split())


def _seg_seconds(s: dict) -> float:
    """Approx spoken seconds for a beat (word count ÷ WPS)."""
    return round(_seg_words(s) / _WPS, 1)


def _suggested_shots(seconds: float) -> int:
    """How many distinct shots a beat of this length should be split into so no
    single image holds the screen too long — ceil(seconds / clip window), capped
    2-4. 1 (no split) for short beats."""
    import math
    if seconds <= _CLIP_WINDOW + 1:
        return 1
    return max(2, min(4, math.ceil(seconds / _CLIP_WINDOW)))


def _primer(segs: list) -> list:
    out = []
    for s in segs:
        secs = _seg_seconds(s)
        item = {"label": s["label"], "text": s.get("text", ""),
                "approx_seconds": secs,
                "shots_needed": _suggested_shots(secs)}
        if s.get("speaker"):
            item["speaker"] = s["speaker"]
        if s.get("lines"):
            item["lines"] = s["lines"]
        out.append(item)
    return out


def _director_context(facts: dict, cast: dict, setup: str, concept: str,
                      have_imgs: bool, references: str = "") -> str:
    """The shared HEAD of every director prompt: concept, references, experiment,
    facts, cast — XML-tagged so the model never confuses the sections (Anthropic
    prompting best practice). The per-call primer + instructions follow."""
    exp = _direction_experiment()
    exp = (f"<experiment>\nApply this ONE visual change today: {exp}\n"
           "</experiment>\n\n" if exp else "")
    refs = (f"<references>\n{references}\n</references>\n\n"
            if references else "")
    con = (f"<concept>\nThe creator's agreed creative idea + visual motif for "
           f"this episode. THIS defines the WORLD, setting and staging — build "
           f"the opening shot, the bespoke world and the anchor motif INSIDE it "
           f"and carry it through every beat. The channel adds only its brand "
           f"colours and marks; do not pull the world back toward a channel "
           f"theme.\n{concept}\n</concept>\n\n" if concept else "")
    castblock = ""
    if cast:
        portraits = ("Reference PORTRAITS of these characters are attached as "
                     "images — STUDY them and match each character's face, hair/"
                     "hijab, build and outfit to their portrait. " if have_imgs
                     else "")
        castblock = ("<cast>\n" + portraits + "Who speaks — the visuals MUST "
                     "show the right character on screen for each segment's "
                     "speaker:\n" + json.dumps(cast, indent=1) + "\n")
        ch = config.channel()
        wardrobe = {}
        for nm in cast:
            if ch.cast_outfits(nm):
                wardrobe[nm] = {r["outfit"]: r.get("when", "")
                                for r in ch.cast_refs(nm)}
        if wardrobe:
            castblock += (
                "\nWARDROBE — each character has reference images in these "
                "outfits (with when each fits). You MAY set a per-segment "
                "\"outfit\" (one of the listed names) to dress that shot for "
                "its context; omit it to keep the default formal look:\n"
                + json.dumps(wardrobe, indent=1) + "\n")
        if setup:
            castblock += f"\nScene setup: {setup}\n"
        castblock += "</cast>\n\n"
    elif setup:
        castblock = f"<cast>\nScene setup: {setup}\n</cast>\n\n"
    return (con + refs + exp
            + "<facts>\nGROUND TRUTH — every figure is locked; never change, add "
            "or drop a number, and never change the verdict:\n"
            + _facts(facts) + "\n</facts>\n\n" + castblock)


def _director_user(facts: dict, segs: list, labels: list,
                   note: str = "", cast: dict = None, setup: str = "",
                   concept: str = "", have_imgs: bool = False,
                   references: str = "") -> str:
    return (_director_context(facts, cast, setup, concept, have_imgs, references)
            + "<primer>\nThe screenwriter's script, in running order:\n"
            + json.dumps(_primer(segs), indent=1) + "\n</primer>\n\n"
            + f"<instructions>\n{note}Design the direction now. Use the labels "
            "exactly, in this order: " + ", ".join(labels)
            + ".\n</instructions>")


def _director_bible_user(facts, segs, labels, cast=None, setup="",
                         concept="", have_imgs=False, references="") -> str:
    """LONG-script split, pass 1: the GLOBAL direction bible only (no per-shot
    design yet) — small response that anchors every later batch."""
    return (_director_context(facts, cast, setup, concept, have_imgs, references)
            + "<primer>\nThe screenwriter's full script, in running order:\n"
            + json.dumps(_primer(segs), indent=1) + "\n</primer>\n\n"
            + "<instructions>\nThis script is long, so we design it in passes. "
            "RIGHT NOW return ONLY the top-level direction BIBLE as a JSON "
            "object — {\"visual_concept\":…, \"anchor_motif\":…, "
            "\"reveal_arc\":…, \"style\":…, \"thumbnail\":{…}} — and do NOT "
            "design per-segment shots yet (ignore the per-segment shape in the "
            "system prompt for this pass).\n</instructions>")


def _design_digest(done: list) -> str:
    """A COMPACT view of the beats already designed this pass, fed to the next
    beat's designer so the agent BUILDS on what came before — varies framing
    from the previous shot, progresses the anchor motif, and sets `refs`
    accurately — instead of designing each beat blind. Only the fields that
    drive continuity (label, the keyframe scene, overlay, the sub-shot scenes)."""
    out = []
    for s in done:
        item = {"label": s.get("label"), "visual": s.get("visual", "")}
        if s.get("overlay"):
            item["overlay"] = s["overlay"]
        if isinstance(s.get("shots"), list) and s["shots"]:
            item["shots"] = [{"visual": sh.get("visual", "")} for sh in s["shots"]]
        out.append(item)
    return json.dumps(out, indent=1)


def _seg_to_plan(seg: dict) -> dict:
    """Reproduce a CURRENT script segment's design as a director-plan entry, so a
    beat the judge marked GOOD is passed through UNCHANGED (no LLM call, no
    redesign) while still giving the flagged beats continuity context. `refs` is
    omitted deliberately so _apply_direction keeps the existing `ref_idx`."""
    keys = ("label", "text", "overlay", "overlays", "visual", "motion",
            "clip_motion", "tease", "animate", "clip_voice",
            "cast_in_shot", "angle", "shots")
    return {k: seg[k] for k in keys if k in seg}


def _director_one_user(facts, segs, label, idx, bible, done, cs, note=None) -> str:
    """Agentic director, per-BEAT pass: design the per-segment direction for ONE
    label, given the locked bible AND the beats already designed (so the agent
    works piece by piece, each beat building on the last, instead of dumping the
    whole video in one muddled response). `note` = the director judge's fix for
    THIS beat, applied specifically to it."""
    prior = (("<designed_so_far>\nThe beats already designed, IN ORDER — keep "
              "visual continuity with them: vary the framing from the previous "
              "shot, and point this beat's \"refs\" at the earlier label(s) it "
              "should visually match. Don't overuse the anchor motif — stamped "
              "on every beat it makes the video dull; a subtle echo (or "
              "nothing) is usually right here, and the beat's own best image "
              "wins.\n"
              + _design_digest(done) + "\n</designed_so_far>\n\n")
             if done else "")
    fix = (f"<judge_note>\nThe director judge reviewed this beat and flagged it. "
           f"Redesign it to APPLY this fix specifically — keep the facts and "
           f"verdict locked:\n{note}\n</judge_note>\n\n" if note else "")
    return (_director_context(facts, cs.get("cast"), cs.get("setup", ""),
                              cs.get("concept", ""), cs.get("have_imgs", False),
                              cs.get("references", ""))
            + "<direction>\nThe agreed top-level direction for this episode "
            "(locked — honour it):\n" + json.dumps(bible, indent=1)
            + "\n</direction>\n\n"
            + "<primer>\nThe screenwriter's full script, in running order (for "
            "context — design ONLY the one beat named below):\n"
            + json.dumps(_primer(segs), indent=1) + "\n</primer>\n\n"
            + prior + fix
            + f"<instructions>\nDesign the direction for EXACTLY ONE beat now: "
            f"\"{label}\" (beat {idx + 1} of {len(segs)}). Return ONLY a single "
            "JSON object for THIS beat — the full per-segment shape from the "
            "system prompt (label, text, overlay, overlays, visual, motion, "
            "clip_motion, tease, refs, animate, cast_in_shot, shots "
            "with per-shot \"say\", …) — and nothing else. Set \"label\" to "
            f"\"{label}\". Do NOT return the top-level bible fields or any other "
            "beat.\n</instructions>")


def _direct_agentic(ch, tpl, facts, script, labels, cs, imgs,
                    notes=None, bible=None) -> dict:
    """The DIRECTOR as an AGENT (owner call 2026-06-28): instead of one LLM call
    dumping the whole video (which muddies every field), design it in FOCUSED
    pieces — first the top-level BIBLE, then ONE beat at a time, each pass seeing
    the bible + the beats already designed so continuity, refs and motif build
    coherently. Returns a merged plan dict in the same shape `_apply_direction`
    expects; RAISES on any malformed piece so the caller falls back to one call.

    `notes` = {label: fix} the director judge's PER-SECTION critique — each beat's
    designer gets ITS OWN fix. `bible` = an existing top-level direction to REUSE
    (when applying judge notes, so the concept/motif/style hold and only the beats
    change); when None the bible is designed fresh."""
    segs = script["segments"]
    sysp = _direct_sys(ch, tpl)
    notes = notes or {}
    reuse = bible is not None            # judge-fix mode: keep the good beats
    if not reuse:
        bible = _json_block(
            _ask(_director_bible_user(facts, segs, labels, **cs),
                 system=sysp, max_tokens=900, image_paths=imgs,
                 role="direction"), "{", "}")
        print(f"  director (agent): bible set — designing {len(labels)} beats one by one")
    else:
        flagged = sum(1 for l in labels if notes.get(l))
        print(f"  director (agent): reusing the locked bible — redesigning "
              f"{flagged}/{len(labels)} flagged beat(s), keeping the rest as-is")
    done = []
    for i, label in enumerate(labels):
        note = notes.get(label)
        if reuse and not note:
            # the judge left this beat alone → keep it verbatim, no LLM, no change
            done.append(_seg_to_plan(segs[i]))
            print(f"    · beat {i + 1}/{len(labels)} {label} — kept (judge: good)")
            continue
        out = _ask(_director_one_user(facts, segs, label, i, bible, done, cs, note),
                   system=sysp, max_tokens=1800, image_paths=imgs,
                   role="direction")
        try:
            one = _json_block(out, "{", "}")
        except ValueError as e:
            out = _ask(_director_one_user(facts, segs, label, i, bible, done, cs,
                                          note)
                       + f"\n\n(Your previous reply was invalid: {e}. Return ONLY "
                       "the one JSON object.)", system=sysp, max_tokens=1800,
                       image_paths=imgs, role="direction")
            one = _json_block(out, "{", "}")
        if one.get("label") != label:
            # tolerate a missing/blank label, but never the WRONG beat
            if one.get("label"):
                raise ValueError(f"beat designer returned {one.get('label')!r} "
                                 f"for {label!r}")
            one["label"] = label
        if not one.get("visual"):
            raise ValueError(f"beat {label} missing visual")
        done.append(one)
        print(f"    · beat {i + 1}/{len(labels)} {label} ✓")
    plan = {k: bible.get(k, "") for k in
            ("visual_concept", "anchor_motif", "reveal_arc", "style")}
    if bible.get("thumbnail"):
        plan["thumbnail"] = bible["thumbnail"]
    plan["segments"] = done
    return plan


def make_direction_chat(topic: str):
    """A llm.Chat seeded for the director / visual co-design rounds."""
    ch = config.channel()
    folder = config.topic_dir(topic)
    tpl = templates.load_pinned(folder)
    p = folder / "script.json"
    script = json.loads(p.read_text()) if p.exists() else {}
    sys = _direct_sys(ch, tpl)
    return llm.Chat(system=sys,
                    session_id=script.get("direction", {}).get("chat_session"),
                    model=llm.model("direction"))


def _validate_plan(plan: dict, labels: list) -> list:
    segs = plan.get("segments")
    if not isinstance(segs, list) or [s.get("label") for s in segs] != labels:
        raise ValueError(f"director labels {[s.get('label') for s in segs] if isinstance(segs, list) else segs} != {labels}")
    problems = []
    for s in segs:
        if not s.get("visual"):
            problems.append(f"{s['label']} missing visual")
    return problems


def _clean_shots(raw) -> list:
    """Sanitize a director `shots` plan: 2-4 distinct sub-shots, each a small
    visual object (no spoken text — the segment's VO/overlays/chart stay
    segment-level). Returns [] when it isn't a real multi-shot plan (so the
    segment stays single-shot)."""
    if not isinstance(raw, list) or len(raw) < 2:
        return []
    out = []
    for sp in raw[:4]:                       # cap distinct shots per beat
        if not isinstance(sp, dict):
            continue
        s = {}
        for f in ("visual", "motion", "clip_motion", "tease", "outfit", "say"):
            v = sp.get(f)
            if isinstance(v, str) and v.strip():
                s[f] = v.strip()
        if "animate" in sp:
            s["animate"] = bool(sp["animate"])
        a = str(sp.get("angle") or "").strip().lower()
        if a in ("front", "side", "back"):
            s["angle"] = a
        cis = sp.get("cast_in_shot")
        if isinstance(cis, list):
            s["cast_in_shot"] = [str(x).strip() for x in cis if str(x).strip()]
        try:
            w = float(sp.get("weight"))
            if w > 0:
                s["weight"] = w
        except (TypeError, ValueError):
            pass
        if s.get("visual") or s.get("clip_motion"):
            out.append(s)
    return out if len(out) >= 2 else []


# framing variations for the deterministic long-beat backstop — each yields a
# genuinely DIFFERENT frame on the same scene so a stitched beat isn't a repeat
_FRAMINGS = [
    "wide establishing shot, full scene, lots of negative space",
    "tight close-up on the key detail, shallow depth of field",
    "low reverse angle looking back at the subject",
    "over-the-shoulder medium shot from the side",
]


def _sentences(text: str) -> list:
    """Split spoken text into sentences (on . ! ? — NOT the '…' beat-pause or the
    em-dash hard-stop the writer uses for delivery, so a pause never starts a new
    section). Returns [] for empty text."""
    text = (text or "").strip()
    if not text:
        return []
    parts = re.split(r'(?<=[.!?])\s+', text)
    return [p.strip() for p in parts if p.strip()]


def _balance_sizes(sents: list, n: int) -> list:
    """Partition `sents` into n contiguous groups balanced by spoken LENGTH
    (characters), not sentence count — the writer deliberately mixes 3-word
    punches with long lines, so count-splitting made some sections tiny and
    others huge. Cut points land on the sentence boundaries closest to the
    ideal even-length fractions, always leaving ≥1 sentence per group.
    Returns the group SIZES (sentence counts), summing to len(sents)."""
    n = max(1, min(n, len(sents)))
    if n == 1:
        return [len(sents)]
    lens = [len(x) for x in sents]
    cums, c = [], 0
    for ln in lens:
        c += ln
        cums.append(c)
    total = cums[-1]
    cuts, last = [], 0
    for g in range(n - 1):
        ideal = total * (g + 1) / n
        lo, hi = last + 1, len(sents) - (n - 1 - g)
        best = min(range(lo, hi + 1), key=lambda i: abs(cums[i - 1] - ideal))
        cuts.append(best)
        last = best
    sizes, prev = [], 0
    for cpos in cuts + [len(sents)]:
        sizes.append(cpos - prev)
        prev = cpos
    return sizes


def _groups_from_sizes(sents: list, sizes: list) -> list:
    """Join `sents` into the contiguous groups described by `sizes`."""
    groups, idx = [], 0
    for z in sizes:
        groups.append(" ".join(sents[idx:idx + z]).strip())
        idx += z
    return groups


def _balance_groups(sents: list, n: int) -> list:
    """Distribute `sents` into n contiguous groups, balanced by spoken length
    (see _balance_sizes). Each group is the joined text of its sentences."""
    return _groups_from_sizes(sents, _balance_sizes(sents, n))


def _valid_breaks(sizes, n_sents: int) -> bool:
    """A creator section_breaks list is honoured only while it still fits the
    text: positive ints summing to the beat's CURRENT sentence count (a text
    edit that changes the sentence count silently reverts to auto)."""
    return (isinstance(sizes, list) and len(sizes) >= 1
            and all(isinstance(z, int) and z >= 1 for z in sizes)
            and sum(sizes) == n_sents)


def section_sizes(seg: dict) -> list:
    """The EFFECTIVE section grouping for a beat — group sizes (sentence
    counts) summing to its sentence count. THE single source of truth shared
    by _sectionize_beats (production) and the script-stage preview (UI), so
    what the creator sees is exactly what will voice. Priority: the creator's
    `section_breaks` (when still valid for the text) > the director's shot
    count > the suggested split by length; [] for empty text, [len] = no
    split."""
    sents = _sentences(seg.get("text", ""))
    if not sents:
        return []
    breaks = seg.get("section_breaks")
    if _valid_breaks(breaks, len(sents)):
        return list(breaks)
    existing = seg.get("shots")
    if isinstance(existing, list) and len(existing) >= 2:
        n = len(existing)
    else:
        n = _suggested_shots(_seg_seconds(seg))
    n = min(n, len(sents))
    return _balance_sizes(sents, n)


def _has_section_say(seg: dict) -> bool:
    """True when the beat is SECTION-NATIVE — its shots carry a derived `say`
    (which must be re-derived whenever the beat's locked text changes)."""
    return any((sp.get("say") or "").strip()
               for sp in (seg.get("shots") or []) if isinstance(sp, dict))


def _sectionize_beats(segs: list) -> None:
    """SECTION-NATIVE beats (owner call 2026-06-27): when a beat plays as several
    shots, make each shot a SECTION that voices its OWN sentence(s) — so caption,
    audio and video are produced section by section and stay in sync (no more one
    VO stretched over arbitrary visual slices). Section count is the director's
    shot count when it planned one, else the suggested split (`_suggested_shots`),
    capped at the number of SENTENCES (a section needs its own line). The spoken
    `say` per section is DERIVED from the locked script text (split by sentence)
    — never the director's words — so the facts/verdict lock is untouched. The
    director's per-shot visuals fill the sections; a framing variant
    (wide/close/reverse/OTS) fills any extra. A single-sentence or short beat
    stays single-shot (nothing to sync). Dialogue (`lines`) beats are left to the
    dialogue path. Runs on a full direction (not a gap-fill)."""
    made = 0
    for s in segs:
        if s.get("lines"):                       # dialogue → its own timeline
            continue
        sents = _sentences(s.get("text", ""))
        existing = s.get("shots")
        # the grouping decision lives in section_sizes (creator breaks >
        # director shots > length-balanced suggestion) — shared with the
        # script-stage preview so the UI shows exactly what will voice
        sizes = section_sizes(s)
        n = len(sizes)
        if n < 2:
            # one sentence / short → no split. If this is a RE-split (the text
            # changed under existing sections), strip the stale per-shot `say`
            # so the old sentences are never voiced again — the beat degrades
            # to a plain/montage beat voiced from its own text.
            if isinstance(existing, list):
                for sp in existing:
                    if isinstance(sp, dict):
                        sp.pop("say", None)
            continue
        groups = _groups_from_sizes(sents, sizes)
        base_vis = (s.get("visual") or "").strip()
        base_motion = (s.get("clip_motion") or s.get("motion") or "").strip()
        vis = existing if isinstance(existing, list) else []
        animate0 = bool(s.get("animate", True))
        shots = []
        for k in range(n):
            if k < len(vis) and isinstance(vis[k], dict):
                shot = dict(vis[k])
            else:                                # framing-varied backstop shot
                framing = _FRAMINGS[k % len(_FRAMINGS)]
                shot = {"visual": (f"{base_vis} — {framing}" if base_vis
                                   else framing),
                        "clip_motion": base_motion}
            shot["say"] = groups[k]              # locked text, partitioned
            if "animate" not in shot:
                shot["animate"] = animate0 if k == 0 else False
            shot.setdefault("weight", 1.0)
            shots.append(shot)
        s["shots"] = shots
        made += 1
    if made:
        print(f"  section-native: {made} beat(s) split into sections (each shot "
              f"voices + captions its own sentence — synced section by section)")


def _apply_direction(folder, script, plan, ch, facts, fill: bool = False) -> dict:
    """Merge the director's plan into script.json under the facts lock.
    fill=True only fills fields that are MISSING — it never overwrites a
    visual, motion, chart, ref or animate flag you've already settled, so it
    backfills (e.g. an `animate` flag a pre-existing direction lacked)
    without disturbing the rest."""
    segs = script["segments"]
    primer = script.setdefault(
        "primer", [{"label": s["label"], "text": s["text"]} for s in segs])
    positive = facts.get("verdict") == ch.positive_verdict
    # ref labels resolve to indices in the DELIVERY order (the writer's order)
    labels = [s["label"] for s in segs]
    reverts = 0

    def want(seg, key):                  # in fill mode, only touch gaps
        return not fill or seg.get(key) in (None, "", [])

    for i, (seg, pl, pr) in enumerate(zip(segs, plan["segments"], primer)):
        if not fill:                     # fill never rewrites the script text
            if _lock_text(pr["text"], pl.get("text", ""), positive):
                if pl.get("text"):
                    seg["text"] = pl["text"]
            else:
                seg["text"] = pr["text"]   # unsafe rephrase → primer wins
                reverts += 1
        if want(seg, "overlay") and pl.get("overlay") is not None:
            seg["overlay"] = pl["overlay"]
        # multiple timed overlays (each {text,pos,start,end}) — optional
        if "overlays" in pl and want(seg, "overlays"):
            ovl = pl.get("overlays")
            seg["overlays"] = ovl if isinstance(ovl, list) else []
        if want(seg, "visual") and pl.get("visual"):
            seg["visual"] = pl["visual"]
        elif not seg.get("visual"):
            seg["visual"] = pl.get("visual") or ""
        if want(seg, "motion") and pl.get("motion"):
            seg["motion"] = pl["motion"]
        if want(seg, "clip_motion") and pl.get("clip_motion"):
            seg["clip_motion"] = pl["clip_motion"]
        if want(seg, "tease") and pl.get("tease"):
            seg["tease"] = pl["tease"]
        # the director's continuity graph: which earlier keyframes condition
        # this one (image refs). Labels → earlier-only indices, deduped,
        # order preserved (most important last for a last-wins CLI), cap 3.
        if "refs" in pl and ("ref_idx" not in seg or not fill):
            idxs = []
            for r in (pl.get("refs") or []):
                j = (r if isinstance(r, int)
                     else labels.index(r) if r in labels else None)
                if j is not None and 0 <= j < i and j not in idxs:
                    idxs.append(j)
            seg["ref_idx"] = idxs[-3:]
        if "animate" in pl and ("animate" not in seg or not fill):
            seg["animate"] = bool(pl["animate"])
        if "clip_voice" in pl and ("clip_voice" not in seg or not fill):
            seg["clip_voice"] = bool(pl["clip_voice"])
        # WHICH cast appears in this shot (the director's call — not every frame
        # needs the cast; [] = a scene with no people). Only kept when it's a
        # list; an omitted field leaves the default (whoever speaks).
        if "cast_in_shot" in pl and ("cast_in_shot" not in seg or not fill):
            cis = pl.get("cast_in_shot")
            if isinstance(cis, list):
                seg["cast_in_shot"] = [str(x).strip() for x in cis if str(x).strip()]
        # camera angle on the character(s) → picks the angle-matched face ref
        if "angle" in pl and ("angle" not in seg or not fill):
            a = str(pl.get("angle") or "").strip().lower()
            if a in ("front", "side", "back"):
                seg["angle"] = a
        # MULTI-SHOT: the beat split into several distinct shots, stitched (§3).
        # fill never overwrites a shots plan you've already settled.
        if "shots" in pl and ("shots" not in seg or not fill):
            shots = _clean_shots(pl.get("shots"))
            if shots:
                seg["shots"] = shots
            else:
                seg.pop("shots", None)
    if not fill:
        _sectionize_beats(segs)
    d = script.setdefault("direction", {})
    for k in ("visual_concept", "anchor_motif", "reveal_arc", "style"):
        if not fill or not d.get(k):
            d[k] = plan.get(k, d.get(k, ""))
    if plan.get("thumbnail") and (not fill or not d.get("thumbnail")):
        d["thumbnail"] = plan["thumbnail"]
    d["model"] = llm.model()
    script["directed"] = llm.model()
    if not fill:                            # fill keeps the unchanged thumb
        import make_video
        make_video.archive(folder, "thumb_bg.png", "thumbnail.png")
    if reverts:
        print(f"  facts lock: kept the primer wording on {reverts} segment(s)"
              f" (a revision would have changed a number or the verdict)")
    print(f"  director: bespoke visuals + reveal arc for {len(segs)} segments")
    # ADVISORY judge of the shot plan (physics/theme/freshness/audience/clarity)
    # — the visual analogue of the hook judge, $0, before any keyframe spend. A
    # gap-fill (fill=True) doesn't re-judge (it didn't re-plan).
    if not fill:
        try:
            d["judge"] = judge_direction(script, facts, user_concept(folder))
            jd = d["judge"]
            if jd.get("overall"):
                sc = jd.get("scores", {})
                print(f"  director judge: {jd['overall']}/10  ("
                      + " ".join(f"{k[:4]} {v}" for k, v in sc.items()) + ")"
                      + (f" — weakest: {jd.get('weakest')}"
                         if jd.get("weakest") else ""))
        except Exception as e:                       # noqa: BLE001 (advisory)
            print(f"  ! director judge skipped: {e}", file=sys.stderr)
    return script


def direct_script(topic: str, chat=None, fill: bool = False) -> dict:
    """Research-aware director ($0): designs a bespoke per-company visual
    world, a tease/reveal arc, and per-segment shots + motion — and may
    sharpen the spoken script, under a hard facts/verdict lock (numbers and
    the decision can never change). Writes `direction` +
    per-segment visual/motion/tease into script.json. No-op when
    already directed or no LLM backend; a creator's visual_extra/motion_extra
    still win at prompt time. fill=True backfills ONLY missing fields (e.g.
    a newly-added `animate` flag) and leaves everything else untouched.

    The director runs as an AGENT (`_direct_agentic`): a BIBLE pass then ONE
    beat at a time, each pass seeing the bible + the beats already designed — so
    the output is built in focused pieces rather than one muddled dump, and
    continuity/refs cohere. Any failure falls back to a single call. The chat
    co-design loop + the fill backfill stay single-call by design."""
    ch = config.channel()
    folder = config.topic_dir(topic)
    p = folder / "script.json"
    script = json.loads(p.read_text())
    if not llm.available() or (script.get("directed") and not fill):
        return script
    facts = json.loads((folder / "facts.json").read_text())
    tpl = templates.load_pinned(folder)
    # the writer chose the running order — the director follows IT, not the
    # channel's declared order
    labels = [s["label"] for s in script["segments"]]
    _cs = {"cast": script.get("cast"), "setup": script.get("setup"),
           "concept": user_concept(folder),
           "references": _ensure_reference_brief(folder)}
    imgs = _cast_images(script)               # SHOW the director the cast faces
    _cs["have_imgs"] = bool(imgs)
    plan, problems = None, []
    # DEFAULT: the director works as an AGENT — bible first, then ONE beat at a
    # time, each building on the last (focused pieces beat the single muddled
    # dump; owner call 2026-06-28). Falls back to one call on any failure. The
    # chat co-design loop + fill backfill stay single-call by design.
    if chat is None and not fill:
        try:
            plan = _direct_agentic(ch, tpl, facts, script, labels, _cs, imgs)
            problems = _validate_plan(plan, labels)
        except (ValueError, KeyError) as e:
            print(f"  ! director agent pass failed ({e}) — designing in one call")
            plan = None
    if plan is None:
        if chat is not None:
            out = chat.ask(_director_user(facts, script["segments"], labels,
                                          **_cs))
        else:
            out = _ask(_director_user(facts, script["segments"], labels, **_cs),
                       system=_direct_sys(ch, tpl), max_tokens=3000,
                       image_paths=imgs, role="direction")
        try:
            plan = _json_block(out, "{", "}")
            problems = _validate_plan(plan, labels)
        except ValueError as e:
            retry = (chat.ask(f"That failed to parse ({e}). Return ONLY the JSON "
                              f"object, labels exactly: {', '.join(labels)}.")
                     if chat is not None else
                     _ask(_director_user(facts, script["segments"], labels,
                          f"(Your previous reply was invalid: {e}.) ", **_cs),
                          system=_direct_sys(ch, tpl), max_tokens=3000,
                          image_paths=imgs, role="direction"))
            plan = _json_block(retry, "{", "}")
            problems = _validate_plan(plan, labels)
    for pb in problems:
        print(f"  ! {pb}")
    script = _apply_direction(folder, script, plan, ch, facts, fill=fill)
    if chat is not None:
        script["direction"]["chat_session"] = chat.session_id
    p.write_text(json.dumps(script, indent=2))
    return script


def revise_direction(topic: str, feedback: str, chat=None) -> dict:
    """One visual-design iteration round — re-plan the direction applying the
    creator's notes (same facts lock). With a chat the director remembers the
    earlier rounds; without one it re-plans from the current script."""
    folder = config.topic_dir(topic)
    p = folder / "script.json"
    script = json.loads(p.read_text())
    ch = config.channel()
    facts = json.loads((folder / "facts.json").read_text())
    tpl = templates.load_pinned(folder)
    labels = [s["label"] for s in script["segments"]]
    _cs = {"cast": script.get("cast"), "setup": script.get("setup"),
           "concept": user_concept(folder),
           "references": _ensure_reference_brief(folder)}
    note = f"Revision notes from the creator — apply them:\n{feedback}\n\n"
    if chat is not None and chat.messages:
        out = chat.ask(f"{feedback}\n\nReturn ONLY the full revised JSON "
                       f"object, same shape, labels: {', '.join(labels)}.")
    elif chat is not None:
        out = chat.ask(_director_user(facts, script["segments"], labels, note,
                                      **_cs))
    else:
        out = _ask(_director_user(facts, script["segments"], labels, note,
                                  **_cs),
                   system=_direct_sys(ch, tpl), max_tokens=3000,
                   role="direction")
    plan = _json_block(out, "{", "}")
    _validate_plan(plan, labels)
    script["directed"] = None            # force re-merge below
    script = _apply_direction(folder, script, plan, ch, facts)
    if chat is not None:
        script["direction"]["chat_session"] = chat.session_id
    p.write_text(json.dumps(script, indent=2))
    return script


def revise_direction_from_judge(topic: str) -> dict:
    """Apply the director judge's PER-SECTION notes: re-run the per-beat director
    AGENT, feeding each flagged beat ITS OWN fix, and REUSE the locked bible so
    the concept/motif/style hold and only the beats change. This is what "apply
    the judge's fix" runs — the judge critiques section by section, and the
    director takes each section's note back into that section's designer. Falls
    back to a whole-plan revise when the judge left only an overall note."""
    folder = config.topic_dir(topic)
    p = folder / "script.json"
    script = json.loads(p.read_text())
    d = script.get("direction") or {}
    judge = d.get("judge") or {}
    seg_notes = {n["label"]: n["improve"]
                 for n in (judge.get("segments") or [])
                 if n.get("label") and n.get("improve")}
    if not seg_notes:                    # no per-section notes → old whole-plan path
        return revise_direction(topic, judge.get("improve")
                                or "Strengthen the weakest beats.", None)
    ch = config.channel()
    facts = json.loads((folder / "facts.json").read_text())
    tpl = templates.load_pinned(folder)
    labels = [s["label"] for s in script["segments"]]
    _cs = {"cast": script.get("cast"), "setup": script.get("setup"),
           "concept": user_concept(folder),
           "references": _ensure_reference_brief(folder)}
    imgs = _cast_images(script)
    _cs["have_imgs"] = bool(imgs)
    bible = {k: d.get(k, "") for k in
             ("visual_concept", "anchor_motif", "reveal_arc", "style")}
    if d.get("thumbnail"):
        bible["thumbnail"] = d["thumbnail"]
    print(f"  director: applying the judge's notes to {len(seg_notes)} beat(s): "
          + ", ".join(seg_notes))
    try:
        plan = _direct_agentic(ch, tpl, facts, script, labels, _cs, imgs,
                               notes=seg_notes, bible=bible)
        _validate_plan(plan, labels)
    except (ValueError, KeyError) as e:
        print(f"  ! agentic judge-fix failed ({e}) — one-call whole-plan revise")
        note = ("Apply these per-beat fixes from the director judge:\n"
                + "\n".join(f"- {k}: {v}" for k, v in seg_notes.items()))
        return revise_direction(topic, note, None)
    script["directed"] = None
    script = _apply_direction(folder, script, plan, ch, facts)
    p.write_text(json.dumps(script, indent=2))
    return script


# ---- brand kit -------------------------------------------------------------
BRAND_SYS = prompts.load("brand.system")


def _brand_user(ch, tpl: dict, note: str = "", cast_desc: str = "") -> str:
    brand = tpl.get("brand") or {}
    pal = brand.get("palette", "")
    cast = (f"<cast>\nThe channel's recurring host(s) — the brand should feel "
            f"like them:\n{cast_desc}\n</cast>\n\n" if cast_desc else "")
    palette = (f"<palette>\nThe channel's current signature colours to build "
               f"from (refine into hex):\n{pal}\n</palette>\n\n" if pal else "")
    return (cast + palette
            + f"<instructions>\n{note}Design the brand kit for \"{ch.title}\" "
            f"now — a channel about: {ch.premise}\nReturn ONLY the JSON object.\n"
            "</instructions>")


def _cast_desc(ch) -> str:
    """One-line look of each cast member, to ground the brand in the host(s)."""
    out = []
    for name, m in (ch.cast or {}).items():
        look = (m.get("appearance") or m.get("personality") or "").strip()
        out.append(f"- {name}: {look}" if look else f"- {name}")
    return "\n".join(out)


def design_channel(seed: str) -> dict:
    """CHANNEL DESIGNER ($0): turn the creator's rough idea into a complete
    channel definition — refined premise, persona, topic_noun, the beat menu
    (segments with per-beat guidance), voice_rules, visual_rules and starter
    SEO — ready to write into a new channel.yaml. No channel needs to be
    selected (it designs channels, it doesn't read one). {} without an LLM
    backend or on an unparseable reply."""
    seed = (seed or "").strip()
    if not seed or not llm.available():
        return {}
    text = _ask(prompts.load("channel_design")
                + f"\n\nTHE CREATOR'S ROUGH IDEA:\n{seed}\n",
                max_tokens=2200)
    j = _json_block(text, "{", "}")
    if not isinstance(j, dict) or not j.get("premise"):
        return {}
    segs = []
    for sg in (j.get("segments") or []):
        if not isinstance(sg, dict):
            continue
        lbl = str(sg.get("label") or "").strip().upper()
        gd = " ".join(str(sg.get("guidance") or "").split())
        if not lbl or not gd:
            continue
        one = {"label": lbl, "guidance": gd}
        try:
            one["max_words"] = int(sg["max_words"])
        except (KeyError, TypeError, ValueError):
            pass
        segs.append(one)
    j["segments"] = segs
    for k in ("voice_rules", "visual_rules"):
        j[k] = [" ".join(str(r).split()) for r in (j.get(k) or [])
                if str(r).strip()]
    return j


def design_brand(notes: str = "", chat=None, regen: bool = False) -> dict:
    """Design (or revise) the channel BRAND KIT spec ($0) → channels/<name>/
    brand/brand.json {name, tagline, palette, logo, wordmark, banner,
    background, motifs}. Cached: a designed spec is kept unless `regen`/a chat
    revision. No-op without an LLM backend (returns whatever brand.json holds)."""
    ch = config.channel()
    bdir = ch.dir / "brand"
    bdir.mkdir(parents=True, exist_ok=True)
    bp = bdir / "brand.json"
    existing = {}
    if bp.exists():
        try:
            existing = json.loads(bp.read_text())
        except ValueError:
            existing = {}
    if existing.get("logo") and not regen and chat is None:
        return existing
    if not llm.available():
        if not existing:
            print("  ! no LLM backend — cannot design the brand", file=sys.stderr)
        return existing
    tpl = templates.load(ch.default_template)
    cast_desc = _cast_desc(ch)
    note = (f"Revision notes from the creator — apply them, keep what works:\n"
            f"{notes}\n\n" if notes else "")
    spec = _design_round(
        chat,
        lambda extra="": _brand_user(ch, tpl, extra + note, cast_desc),
        BRAND_SYS.format(title=ch.title, premise=ch.premise, persona=ch.persona),
        "Return ONLY the full revised brand JSON object, same shape.")
    spec["designed"] = True
    if chat is not None:
        spec["chat_session"] = chat.session_id
    bp.write_text(json.dumps(spec, indent=2))
    print(f"  ✓ brand designed: {spec.get('name')} — {spec.get('tagline')}")
    return spec


def make_brand_chat():
    """A llm.Chat seeded for the brand co-design / revision rounds."""
    ch = config.channel()
    tpl = templates.load(ch.default_template)
    bp = ch.dir / "brand" / "brand.json"
    spec = {}
    if bp.exists():
        try:
            spec = json.loads(bp.read_text())
        except ValueError:
            spec = {}
    return llm.Chat(
        system=BRAND_SYS.format(title=ch.title, premise=ch.premise,
                                persona=ch.persona),
        session_id=spec.get("chat_session"))


def rewrite_hook(topic: str, feedback: str) -> dict:
    """Rewrite segment 0 in place after a weak virality score. Caller is
    responsible for make_video.reset_hook() + rerun."""
    ch = config.channel()
    folder = config.topic_dir(topic)
    d = json.loads((folder / "facts.json").read_text())
    tpl = templates.load_pinned(folder)
    script = json.loads((folder / "script.json").read_text())
    seg0 = script["segments"][0]          # the opener — whatever label leads
    prev_text, prev_overlay = seg0.get("text"), seg0.get("overlay")
    hook_cfg = ch.segment(seg0.get("label")) or (ch.segments[0])
    new = _json_block(_ask(REHOOK_PROMPT.format(
        feedback=feedback or "low hook / attention score",
        hook=json.dumps(seg0, indent=1),
        guidance=hook_cfg.get("guidance", ""),
        rest=json.dumps(script["segments"][1:], indent=1),
        facts=_facts(d), max_words=hook_cfg.get("max_words", 12),
        noun=ch.topic_noun, world=tpl["world"]), max_tokens=500), "{", "}")
    # update seg0 in place — keep its director fields (visual/motion/tease/
    # ref_idx/animate) and `directed`; only key0/clip0 (reset by
    # reset_hook) regenerate, not the whole video. Visual stays the director's;
    # we only fix the spoken line and its overlay.
    seg0.update({"text": new["text"],
                 "overlay": new.get("overlay", prev_overlay)})
    if _has_section_say(seg0):
        # a section-native opener re-derives each section's `say` from the
        # rewritten line so the sections voice the NEW hook, not the old one
        _sectionize_beats([seg0])
    script.setdefault("hook_history", []).append(
        {"text": prev_text, "overlay": prev_overlay,
         "reason": feedback[:300]})
    script["mode"] = "llm-hookfix"
    import make_video
    make_video.archive(folder, "script.json")    # keep the pre-rewrite draft
    (folder / "script.json").write_text(json.dumps(script, indent=2))
    print(f"  HOOK rewritten: \"{new['text']}\"")
    return script


if __name__ == "__main__":
    write_script(sys.argv[1] if len(sys.argv) > 1 else "AAPL")
