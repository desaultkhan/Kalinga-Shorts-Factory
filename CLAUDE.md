# Kalinga — Multi-channel Shorts Factory

Channel-agnostic YouTube Shorts factory. Each **channel** is a folder under
`channels/<name>/` (premise, persona, beat structure, research adapter,
templates, queue, learnings, output); the pipeline in `pipeline/src/` reads it
and produces validated, upload-ready vertical videos. Example channels:
`daily-science` and `history-shorts` (with >1 channel, pick one per command
with `--channel <name>` / `KALINGA_CHANNEL`).

This is the open-source core — focused on the **video creation workflow**. It
does NOT include carousels, data-chart overlays, analytics/experiment loops,
publishing automation, or any channel-specific research (finance, etc.). Those
were intentionally removed; the registry seams (see below) make them easy to
add back.

Full design doc (module graph, ship flow, artifacts, gates, invariants — keep
it updated when the architecture changes):

@ARCHITECTURE.md

## Commands

- **Produce a video (headless):** `python3 kalinga.py ship [TOPIC]
  [--template X] [--review]` — research (channel adapter) → LLM script
  (whole-script-judged) → direction → Higgsfield visuals/TTS via the
  `higgsfield` CLI → assembly → validation gates with one automatic hook retry
  → report.md. Resumable: rerun after any failure. `--channel <name>` /
  `KALINGA_CHANNEL` picks the channel (the only channel wins by default).
  `python3 kalinga.py status` diagnoses.
- **Produce a video (interactive, terminal):** `python3 kalinga.py make
  [TOPIC] [--template X] [--at <stage>]` — stage-by-stage session
  (facts sign-off → concept → script co-writing → direction → per-segment voice
  → keyframes → clips → music → assemble → thumbnail → SEO → gates → wrap).
  Uniform menu `[a]/[r]/[e]/[A]uto/[q]uit`; typing prose at any menu is that
  stage's edit action. `[A]` finishes headless via ship's `run_to_done`.
  Design doc: `docs/interactive-make.md`.
- **Produce a video (interactive, browser):** `python3 kalinga.py make
  [TOPIC] --ui` — the `webui/` package serves a local single-page app (stdlib
  HTTP, no deps) with the same stage functions; CLI and UI resume each other.
- **Browser control center:** `python3 kalinga.py ui` — a launcher: pick
  channel → run folders (or a new run) → the video workflow or a capability
  (Status · Usage · Show · References · Cast · Condense-learnings).
- **Iterate stage-by-stage:** `kalinga.py run <stage> [TOPIC]`,
  `kalinga.py redo <stage> [TOPIC]` (supersedes the stage + downstream, reruns;
  `redo hook` is the surgical opener reset), `kalinga.py show [TOPIC]`. Stages:
  research script voice keyframes clips assemble seo thumbnail score validate
  critique.
- **Upload bundle:** `kalinga.py bundle [TOPIC]` — an `<run>/upload/` folder
  with two cuts (with/without music, watermarked) + thumbnail + paste-ready
  `seo.txt`. Built automatically at the end of a ship/wrap.
- **Manual generation:** `kalinga.py manual [TOPIC]` — export every keyframe/clip
  prompt + dependency to `<run>/manual/prompts.md` to hand-generate on the
  Higgsfield website, then drop files back + `kalinga.py run assemble [TOPIC]`.
- **Channel cast:** `kalinga.py cast` — a recurring roster (name → personality →
  gender → appearance → voice → AI avatar), saved to `cast.json` + `cast/`.
  When present, the writer uses only those characters and keyframes are
  conditioned on the right avatar.
- **Channel brand kit:** `kalinga.py brand` — design + render a channel's
  identity (logo, icon, watermark, banner, background) into
  `channels/<name>/brand/` (gitignored). AI marks + PIL-composited text.
- **Reference moodboard:** drop images into `<run>/concept/` (or give a
  TikTok/IG video URL) and `kalinga.py refs [TOPIC] [--url <link>]` extracts a
  style brief that steers the concept + director.
- **Topic ideation:** `kalinga.py ideas [SEED…]` (AI queue candidates);
  `kalinga.py queue-topic <TOPIC>` appends one.
- **Condense learnings:** `kalinga.py condense-learnings [--scope
  channel|global|both] [--dry-run]` — dedupe + merge the learnings without
  losing any distinct lesson (backs up first).
- **Usage FYI:** `kalinga.py usage [TOPIC]` — LLM tokens + Higgsfield
  generations/credits for a run (never a gate).
- **ElevenLabs (optional, `ELEVENLABS_API_KEY`):** royalty-free music
  (`music_source: elevenlabs`) and TTS voiceover (`tts_engine: elevenlabs`);
  `kalinga.py voices [SEARCH]` lists account voices. Degrades to free edge-tts
  without the key.
- **First-run setup:** `kalinga.py init` — a guided walkthrough: environment
  checks (ffmpeg, the two CLIs, python deps), the logins, then which channel to
  run and the first `make`/`ship` command. `kalinga.py status` re-diagnoses.
- **New channel:** `kalinga.py new-channel <name>` scaffolds
  `channels/<name>/`; `channels/daily-science/channel.yaml` is the commented
  reference.

## Architecture notes

- Channel content comes from `channel.yaml`; research is a pluggable adapter
  (`research.ADAPTERS`: `llm` | `manual`). Creative via `llm.py` — default
  backend is `claude -p` on the user's Claude subscription, with a per-ROLE
  model (`llm.ROLE_MODELS`; `KALINGA_MODEL_<ROLE>` / `KALINGA_MODEL` /
  `KALINGA_BACKEND` to change; Anthropic API fallback). Used by `creative.py`
  (script + hook/script/direction judges + the research-aware director under a
  code-enforced facts/verdict lock), `seo.py`, `evaluator.py`. Generation via
  the `higgsfield` CLI (`make_video.py`), gates (`validate.py`), templates
  (`templates.py` + `channels/<name>/templates/*.yaml`), orchestration
  (`kalinga.py`).
- **Extension seams are registries** (a new backend/source/look is one entry):
  `research.ADAPTERS`, `make_video.PROVIDERS` (generation), `make_video.ENGINES`
  (TTS), `webui.ACTIONS`/`STAGE_STATE`, plus channels/templates as folders. See
  `docs/EXTENDING.md`.
- **Plugins:** `plugins/remotion/` — an optional Remotion (React) Ken Burns
  animator for still segments. OFF by default (falls back to the ffmpeg
  zoompan); enable with template `kenburns: remotion` / `KALINGA_KENBURNS=remotion`
  after `cd plugins/remotion && npm install`.
- **Python is 3.9** — no `X | Y` unions at runtime; every module uses
  `from __future__ import annotations`.
- **Compile gate:** `./build.sh` clears `__pycache__`, byte-compiles + imports
  every module (missing OPTIONAL third-party deps are skipped). `pytest -q` runs
  the offline smoke tests (imports + registries + the sample channel). Run both
  after edits — CI runs the same.
- **Costs:** ~180–240 Higgsfield credits per video; `kalinga.py ship` checks the
  balance against the template's `budget_credits`. Clips >8s trip moderation —
  keep `max_clip_seconds: 8`.
- **Keys:** none required beyond one-time logins — `higgsfield auth login` and
  the `claude` CLI. Optional: `ANTHROPIC_API_KEY` (API fallback),
  `ELEVENLABS_API_KEY` (music/voice).
- **State (per channel):** `channels/<name>/queue.csv`, `learnings.md`,
  `output/<date>_<TOPIC>/` (gitignored). **Global:** `channels/learnings.md`
  (cross-channel craft every channel's prompts read).
