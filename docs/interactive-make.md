# `kalinga.py make` — the interactive production session

`interactive.py` is a UI layer over the exact same stage functions that
`ship`/`run_to_done` call, so the terminal session, the browser UI (`--ui`,
`webui/`), and the headless pipeline all resume one another (same artifacts,
same `session.json`).

## Stage order

```
research  concept  script  direction  voice  keyframes  clips  music
assemble  thumbnail  seo  gates  wrap
```

The session enters at the first stage whose artifacts are missing
(`first_incomplete`), or at `--at <stage>` (jump into a finished video —
changed pieces invalidate downstream, the rest stays cached). An existing run
never resumes silently: the resume menu lists every stage numbered with ✓/·
progress; type a number to jump, Enter to continue, or start fresh.

## The uniform menu

Every checkpoint offers `[a]ccept [r]etry [e]dit … [A]uto [q]uit [?]`, plus:
Enter = accept, and **typing prose at any menu IS the edit action** for that
stage (script feedback, SEO notes, keyframe/clip prompt tweaks) — multi-line,
empty line sends. `[A]` hands the rest to `run_to_done` (the same code path as
headless `ship`); `[q]` keeps artifacts and rerunning resumes. Output is
colour-coded via `config.tint` (TTY-only, NO_COLOR-aware).

## What each stage adds over headless

- **research** — facts sign-off (verdict coloured when the channel has one).
- **concept** (`concept.json`, read by `creative.user_concept`) — the creator
  sets the video's HOOK idea + recurring motif, injected into BOTH the writer's
  opening beat and the director's world/motif. Set it three ways: `[e]` paste
  your own, `[r]` co-write with the AI from rough primers, or `[g]` let the AI
  suggest a fresh one (never a repeat). Also `[i]` (analyse reference images
  dropped in `concept/`), `[l]` (copy a TikTok/IG video's look by URL), and
  `[w]` (set this video's LENGTH — words or a time like `45s`/`1m30s`, pins
  `target_words` + a matching QC `duration_range`). Optional — skip for the
  default treatment.
- **script** — co-writing via `creative.revise_script` + a stateful `llm.Chat`
  (`draft_history` + `chat_session` persisted in `script.json`); a whole-script
  judge scores it (advisory). On accept, the session's feedback rounds append
  to the channel's learnings so future scripts pre-apply the critiques.
- **direction** — the research-aware director (`creative.direct_script`, $0)
  designs a bespoke visual world, tease/reveal arc, per-segment shots/motion and
  on-screen overlays under the facts/verdict lock; iterated with a stateful
  `llm.Chat` (`revise_direction`), and `[f]ill gaps` backfills only missing
  fields. A per-section judge (`judge_direction`) flags beats to fix.
- **voice** — per-segment re-read; the voice/engine is pinned into
  `template.json` (env `HF_VOICE`/`TTS_ENGINE` still wins). Multi-character
  scripts ask you to pick each speaker's voice; a `[d]ialogue` editor sets each
  line's independent start + expression (timing-only edits recompose for free).
  **`[R]`ecord your OWN voice** unit by unit — mic capture in the terminal
  (Enter to stop) with whisper word-alignment for karaoke when installed, else a
  held caption; recorded takes are kept across re-voicing.
- **keyframes / clips** — one-by-one with credit costs (first visit with none
  generated builds the whole set up front, then reviews each). A clip for a
  segment a character speaks can deliver the line on-camera (`clip_voice`).
- **music** — pick/generate the background track (`overrides.json` merge).
- **assemble** — `[g]` per-segment editor (text-reveal timing, clip fill,
  native/full/mix clip audio), overlay/subtitle/speed toggles, and an on-demand
  **AI review** (`[v]`, `evaluator.review_and_repair`) that shows a pass/fail
  verdict + fixes and asks before any re-roll. Assemble edits are STAGED and one
  confirmed Apply runs a single rebuild.
- **thumbnail / seo** — edit the cover headline (free recompose) or regenerate
  the bg; an SEO feedback loop (`seo.run(topic, feedback)`).
- **gates** — the three validation gates with a `redo hook` offer, then an
  accept decision.
- **wrap** (the LAST stage) — critique → `report.md` → queue marked done → a
  "what changed this session" review (the version trail + the script diff) →
  `extract_session_learnings` distils durable channel learnings from the actual
  edits → transferable craft promoted to global → the upload bundle is built →
  `session.json` deleted. Publishing is manual: upload the `<run>/upload/`
  bundle to your platform.

## State

`session.json` ({started, credits_start}) is the only session state file,
deleted at wrap. The invalidation matrix (what dies when a decision changes)
lives in `interactive._invalidate`'s docstring.
