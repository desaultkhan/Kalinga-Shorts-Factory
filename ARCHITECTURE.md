# Kalinga — System Design

Session bootstrap doc (auto-imported by CLAUDE.md). If code and this doc
disagree, the code wins — fix this doc in the same change.

## 1. What this is

A channel-agnostic Shorts factory: a **channel** is a folder under
`channels/<name>/` defining content (premise, persona, beat structure, research
adapter, SEO rules) and look (templates); the pipeline in `pipeline/src/` turns
a topic into a validated, upload-ready vertical video:

```
research → LLM script (whole-script-judged) → direction → Higgsfield
keyframes/clips/voiceover → local ffmpeg assembly → SEO → validation gates
```

Marginal cost: $0 creative (Claude subscription via the `claude` CLI) +
Higgsfield credits for generation (~180–240/video). First/only channel shipped:
`daily-science` (a science-explainer demo on the `llm` research adapter).

## 2. Channel anatomy

```
channels/
├── learnings.md            GLOBAL cross-channel craft (read by writers/critics)
└── <name>/
    ├── channel.yaml        the content contract (below)
    ├── templates/*.yaml    visual worlds (world, style, per-beat motion)
    ├── queue.csv           TOPIC,status,date,concept  (status pending|done|failed)
    ├── learnings.md        channel memory (grows; condense-learnings tightens)
    ├── cast.json           optional recurring character roster
    ├── brand/              optional generated brand kit (gitignored)
    └── output/<date>_<TOPIC>/   per-run artifacts (gitignored)
```

`channel.yaml` keys: `title`, `premise`, `persona`, `topic_noun`, `research`
(adapter: `llm` | `manual`), `default_template`, `segments` (ordered beat menu —
each `label`, `guidance`, optional `max_words`/`ink`/`badge`/`required`),
`flexible_segments` (let the writer combine/drop/add/reorder beats — only
`required: true` beats are enforced), `positive_verdict` +
`negative_verdict_markers` (optional verdict-ink lock), `voice_rules`,
`visual_rules`, `seo` (`title_must_include`, `description_must_include`,
`base_hashtags`, `base_tags`, `prompt_rules`). See
`channels/daily-science/channel.yaml` (the commented reference).

`config.py` is the keystone: the `Channel` class (paths + yaml accessors, an
optional `shows:` partial-override mechanism), `topic_dir()` (newest
`*_<TOPIC>` folder reuse — the resume mechanism), channel selection
(`--channel` > `KALINGA_CHANNEL` > the only channel), and the `art()` layout
helpers.

## 3. CLI (`kalinga.py`)

```
ship [TOPIC] [--template T] [--review]     produce one validated Short (headless)
make [TOPIC] [--template T] [--at STAGE]    interactive stage-by-stage (terminal)
make [TOPIC] --ui   |   ui                   browser UI / launcher (§6)
run  <stage> [TOPIC] | redo <stage> [TOPIC] | show [TOPIC]
bundle [TOPIC] | manual [TOPIC]              upload bundle / hand-generation prompts
cast | brand | refs [TOPIC] | voices         cast roster / brand kit / style refs
ideas [SEED…] | queue-topic <T>              topic ideation / queue append
usage [TOPIC] | condense-learnings           spend FYI / tighten learnings
init | status | templates | channels | new-channel <name>
```

`init` is a guided first-run walkthrough (env checks + logins + the first
command); it and `channels`/`new-channel`/`ui` skip channel resolution so they
work before a channel is chosen. Example channels shipped: `daily-science`
(science explainers) and `history-shorts` (historical moments).

Stages: `research script voice keyframes clips assemble seo thumbnail score
validate critique`, plus pseudo-stage `redo hook` (surgical opener reset).
Caching is presence-based, so `redo` cascades: it archives the stage's
artifacts and every later stage's, then reruns.

**Versioned artifacts — nothing is deleted.** Every supersede (`redo`, a regen,
a hook reset, the AI-review repair loop) MOVES the old file into the run's
`.versions/` dir (indexed in `versions.json`) and frees the canonical name so
the stage regenerates fresh; `make_video.restore` swaps one back.

**`ship`** (resumable — any `StepFailed` → "rerun to resume"): preflight
(`ensure_cli`, `llm.available` warn) → topic from arg/queue → template pinned to
`template.json` (once) → budget gate → `run_to_done`: research → script →
direction → produce (voice → keyframes → clips → assemble → SEO → score) →
optional AI review/repair → gates (one automatic hook rewrite on a weak
virality score) → critique → report → queue mark. `ship` and the interactive
auto-handoff share `run_to_done` (one code path).

### 3.1 `make` — interactive session (`interactive.py`)

A UI over the same stage functions. Stage order: `research concept script
direction voice keyframes clips music assemble thumbnail seo gates wrap`, entered
at the first incomplete stage (or `--at`). Uniform menu at every checkpoint
(`[a]ccept [r]etry [e]dit … [A]uto [q]uit`); typing prose IS the edit action.
`[A]` hands off to `run_to_done`; `[q]` keeps artifacts and resumes. Adds over
headless: facts sign-off, a **concept** stage (the video's hook idea + motif,
fed to writer + director; `[i]` reference images, `[l]` copy a video's look by
URL, `[w]` set length), script co-writing with a stateful `llm.Chat`, a
**direction** co-design stage, a per-segment/character **voice** stage
(multi-character cast, per-line timing/expression, **record your own voice**),
keyframes/clips one-by-one with credit costs, music, SEO, gates, then **wrap**
(critique → report → distill session learnings → upload bundle). Design doc:
`docs/interactive-make.md`.

## 4. Artifacts — `channels/<name>/output/<date>_<TOPIC>/`

| artifact | producer |
|---|---|
| `template.json` | `templates.resolve` — pinned config |
| `facts.json` | `research.ensure` — adapter output; `verdict` drives ink |
| `script.json` | `creative.write_script` / `revise_script`; director adds `direction` + per-segment `visual`/`motion`/`overlay`/`tease`/`ref_idx`/`animate`/`clip_voice`; optional multi-char `cast`/`lines`/`speaker` |
| `audio.json`, `seg*.mp3` | `step_voice` — edge-tts (word timings) / Inworld / ElevenLabs; per-line + per-section files; recorded takes kept |
| `key*.png` | `step_keyframes` — director `visual` + episode `style` + baked overlay; cast avatar conditioning |
| `text*.png` | `_ensure_overlays` — one PNG per timed on-screen overlay |
| `clip*.mp4` | `step_clips` — seedance; absent → ken-burns fallback |
| `short.mp4` | `step_assemble` — 1080×1920@30, captions iff libass+timings, music iff configured |
| `seo.json/md`, `virality.md` | `seo.run` / `step_score` |
| `upload/` | `make_video.export_upload` — both cuts + thumbnail + `seo.txt` |
| `thumbnail.png` | `step_thumbnail` — AI bg + PIL text |
| `report.md`, `critique.md`, `run_state.json` | validate / evaluator / retry counter |
| `versions.json` + `.versions/` | `make_video.archive` — the version index |

## 5. Creative layer (`llm.py`, `creative.py`)

Prompt templates live in `pipeline/src/prompts/` (one `.md` per prompt;
`prompts.load("name")` returns the text, callers `.format()` it).

`llm.ask(user, system, max_tokens, image_paths, tools, model)` is the single LLM
entry. Backends: **cli** (default) = `claude -p --model <id>`; **api** =
Anthropic SDK (`ANTHROPIC_API_KEY`). Force with `KALINGA_BACKEND`/`KALINGA_MODEL`.
Per-ROLE models (`llm.ROLE_MODELS`, override `KALINGA_MODEL_<ROLE>`): **research**
on Sonnet/Opus, **script/direction/judges** on the creative model.

- `write_script` — guardrails assembled from channel.yaml; the writer produces
  spoken `text` per beat (+ label), chooses the running order, may introduce a
  multi-character cast. A whole-script judge (`judge_script`) scores it
  (advisory — keeps the first valid script; retries only on invalid JSON/labels).
- `direct_script` — the research-aware DIRECTOR ($0): a bespoke visual world,
  tease/reveal arc, per-segment `visual`/`motion`/`overlay`, keyframe continuity
  graph (`ref_idx`), and `animate` (paid clip vs free ken-burns). Runs as an
  agent (bible pass → one beat at a time). Under a **facts/verdict lock**
  (`_lock_text`): the director may rephrase but every NUMBER and the DECISION are
  frozen. `judge_direction` scores the plan per-beat; `revise_direction_from_judge`
  redesigns only flagged beats.
- Reference moodboard (`extract_reference_brief`) — dropped `concept/` images (or
  a video URL via `reference_from_url`) → a style brief steering concept +
  director. Topic ideation (`suggest_topics`), brand design (`design_brand`),
  learnings (`condense_learnings`, `extract_session_learnings`,
  `globalize_critiques`).

## 6. Browser UI (`webui/`)

`kalinga.py ui` (launcher: pick channel → run folders or new run → the video
workflow or a tool) and `make --ui` (jump into the video workflow). Multi-mode
`ctx.MODE ∈ {home, video}`; multi-project (`ctx.SESSIONS`, one job per run
folder, parallel across folders on one channel). Modules: `context` (session
state + job runner), `state` (`build_state` + `_ss_<stage>` snapshots +
`STAGE_STATE`), `actions` (`a_<stage>_<action>` + `ACTIONS`), `session`
(open/home/tools), `server` (stdlib HTTP), `assets/` (page.html/styles.css/app.js).
Adding a stage/action = one `_ss_`+`STAGE_STATE` entry and one `a_`+`ACTIONS`
entry. Capabilities: status, usage, show, refs, cast, condense-learnings.
**Cast is a full editor** (home sub-view): roster CRUD + voice audition are
INSTANT `run_tool` calls (`cast_save/cast_delete/cast_sample`), avatar +
reference-library generation stream as Jobs (`cast_avatar/cast_refs`) — the
same `cast.json` the `kalinga.py cast` terminal wizard edits; images/samples
served via `/castimg/` + `/sample/`. Versioned artifact URLs (`?v=mtime`)
are served long-cache so re-renders don't refetch media (the UI-flicker fix),
and the page skips re-rendering entirely when a state poll is unchanged.
A failed provider setup check (`ensure_cli`) never blocks startup: the message
lands in `ctx.SETUP["warning"]` → `setupWarning` in every state snapshot → a
banner on the page; generation stages still fail with the same error until
setup is done.

## 7. Generation (`make_video.py` — via the `higgsfield` CLI)

- **Provider registry** (`PROVIDERS`, `$KALINGA_PROVIDER`, default `higgsfield`):
  `generate()`/`ensure_cli()` are provider-agnostic — a second provider is one entry.
- **TTS engine registry** (`ENGINES`): `edge` (free, karaoke timings, default) ·
  `higgsfield` (Inworld) · `elevenlabs` (needs `ELEVENLABS_API_KEY`). A new engine
  is one entry.
- **Voice** — per-beat / per-line / per-section synthesis; multi-character cast;
  per-line timing + `expression` (edge prosody); RECORD-your-own-voice (mic in
  terminal / MediaRecorder in browser, whisper alignment for karaoke via
  `align.py`).
- **Keyframes** — `nano_banana_2`, 9:16; ONE reference image chosen by priority
  (cast face > recurring-character anchor > continuity ref > style ref); a
  brightness backstop lifts dark frames.
- **Clips** — `seedance_2_0`, `--start-image`, ≤8s (moderation cap); the director's
  dynamic `clip_motion` brief; `animate` decides clip vs free ken-burns;
  `clip_voice` lets a clip deliver the line on-camera (lip-synced to the TTS
  reference).
- **Assembly** — each part's length = where the voice actually ends; section-native
  beats emit one part per section; text overlays fade in at their window; ken-burns
  for stills (ffmpeg `zoompan`, or the optional Remotion plugin); captions iff
  libass + timings; music mixed under when configured.
- **AI review & repair** (`evaluator.review_and_repair`, $0 to review) — an LLM
  looks at one frame per segment + the script and returns pass/fail + layer-attributed
  fixes (keyframe / overlay / reassemble); gated by a confirm callback (auto in
  headless, prompt in `make`).
- **Thumbnail** (`step_thumbnail`) — AI 9:16 bg + PIL ticker/headline, or the
  browser cover editor's custom text `elements` (each {text, pos 0-1, size,
  color} — recomposed FREE on the same bg; `thumb_bg_prompt` exposes the exact
  bg prompt to the UI). Display text renders via `_display_font`: the brand
  font, with a system-font fallback when the text needs glyphs it lacks
  (Greek/superscripts — 1/λ⁴); uppercasing is ASCII-only so symbols survive.
  Karaoke captions hard-wrap per phrase (`captions._group_breaks`) so they
  never run off the 1080px frame.
- Credits ≈ 2/keyframe + 3.5/s of clip + 2/thumbnail; `budget_credits` gated up front.

Optional **brand kit** (`brand.py`, `kalinga.py brand`) — `creative.design_brand`
writes `brand.json`, then renders logo/icon/watermark/banner/background (AI marks
+ PIL text via `taste.py`); `brand/` is gitignored.

## 8. Gates (`validate.py`)

1. **Tech QC** (free): `short.mp4` exists; duration in `duration_range`; audio
   stream; mean volume in range; warns when captions absent.
2. **Virality**: regex over `virality.md` → `weak` (< thresholds) triggers the one
   hook rewrite; `unknown` never blocks.
3. **SEO lint**: title ≤80; channel must-includes; plain-text only; description
   ≥120 chars; 3–5 hashtags; ≥12 tags.

`ok` = QC clean AND not weak AND SEO clean → `report.md`.

## 9. Feedback — learnings (two tiers)

`channels/<name>/learnings.md` (channel memory) + `channels/learnings.md`
(GLOBAL craft). Read by the script writer, SEO, and the critics
(`creative.learnings_tail`, tail-capped). `condense_learnings` ($0 LLM) dedupes +
merges without dropping distinct lessons (backs up first). In `make`, every
critique is remembered and, at wrap, `extract_session_learnings` distils the
session's edits into channel learnings and `globalize_critiques` promotes
transferable craft to global.

## 10. Research adapters (`research.py`)

`research.ensure(topic, folder)` → cached `facts.json` or dispatch on
`channel().research` via the `ADAPTERS` registry: **llm** (Claude via `llm.ask`
with WebSearch+WebFetch on the cli backend; knowledge + uncertainty flags on the
api backend) · **manual** (drop `facts.json`/`facts.md`). A new adapter is one
`ADAPTERS` entry returning at least `{topic, title}` (+ optional `verdict`).
`research_brief` (a run-folder `brief.md` + the concept) scopes what an adapter
researches. `web_context` adds a live-news block on the cli backend
(`web_research: false` opts out).

## 11. Templates (`templates.py`)

Live in `channels/<name>/templates/`. `DEFAULTS` carries mechanical knobs
(models, `video_params`, `max_clip_seconds` 8, voice/tts/captions, `target_words`,
gate thresholds, `budget_credits`, `kenburns`); `world` + `style` are REQUIRED
from the yaml (loader raises), `motion` maps label→camera move. `load()`
deep-MERGES any knob whose default is a dict, REPLACES everything else.

## 12. Extension points (registries)

The engine stays channel-agnostic; the variable parts are one-entry registries:
`research.ADAPTERS`, `make_video.PROVIDERS`, `make_video.ENGINES`,
`webui.ACTIONS`/`STAGE_STATE`, plus channels/templates as folders. See
`docs/EXTENDING.md`.

## 13. Environment

| var | effect |
|---|---|
| `KALINGA_CHANNEL` / `--channel` | channel selection (sole channel wins) |
| `KALINGA_BACKEND` / `KALINGA_MODEL` | LLM backend (`cli`\|`api`) / global model pin |
| `KALINGA_MODEL_<ROLE>` | per-role model (RESEARCH/SCRIPT/DIRECTION/JUDGE) |
| `ANTHROPIC_API_KEY` | api backend |
| `TTS_ENGINE` | `edge` (default, karaoke) \| `elevenlabs` |
| `ELEVENLABS_API_KEY` / `ELEVENLABS_VOICE_ID` | ElevenLabs voice/music |
| `HF_VOICE` | override the Inworld voice |
| `FFMPEG_FULL` | path to a libass ffmpeg for caption burn |
| `KALINGA_KENBURNS` | still animator: `remotion`\|`ffmpeg`\|`off` |
| `KALINGA_PROVIDER` | generation provider (default `higgsfield`) |

Python is **system 3.9**: every module uses `from __future__ import
annotations`; no runtime `X | Y`, no `match`. `./build.sh` byte-compiles +
imports every module (the compile gate); `pytest -q` runs the smoke tests.

## 14. Invariants & gotchas

- Clips >8s trip Higgsfield moderation false-positives — keep `max_clip_seconds: 8`.
- Channel content lives in the channel: verdict-polarity words from
  `negative_verdict_markers`, overlay/badge aesthetics from template tokens — never
  hardcode a channel's vocabulary in the engine. Extension seams are REGISTRIES.
- `topic_dir` reuses the newest `*_<TOPIC>` folder across days; pinned
  `template.json` beats `--template` on resume.
- `redo` cascades downstream by design; supersede = ARCHIVE to `.versions/`, never
  delete. Artifact paths route through `config.art`/`art_glob` — never hardcode a
  subfolder.
- The director OWNS per-segment `overlay`/`visual`/`motion`; the WRITER owns spoken
  `text` (+ label, optional cast). The facts/verdict lock (`_lock_text`) is the only
  thing between the director and a changed number — never weaken it.
- Karaoke captions need BOTH edge-tts timings AND a libass ffmpeg.
- The root `kalinga.py` launcher imports `pipeline/src/kalinga.py` as the module
  `kalinga`, so `interactive.py`'s `import kalinga` is the SAME instance.
