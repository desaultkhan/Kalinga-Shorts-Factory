# Extending Kalinga

Kalinga is designed so the common ways you'd grow it are **one entry in a
registry**, never a rewrite. This is the scalability contract: the pipeline
orchestration stays channel- and backend-agnostic, and the variable parts plug
in through small maps. Each section below is a complete recipe.

---

## Add a channel

A channel is a folder under `channels/<name>/`. The fastest path:

```bash
python3 kalinga.py new-channel my-channel
```

Then edit `channels/my-channel/channel.yaml`. The keys that matter:

- `title`, `premise`, `persona`, `topic_noun` — who the channel is.
- `research` — which research adapter to use (`llm` or `manual`, or your own).
- `default_template` — the visual look (a file in `templates/`).
- `segments` — the ordered beat menu. Mark the ones you require with
  `required: true`; set `flexible_segments: true` to let the writer combine,
  drop, add and reorder beats.
- `seo` — title/description must-includes, base hashtags/tags, prompt rules.
- `positive_verdict` (+ `negative_verdict_markers`) — only if your videos render
  a verdict (WORTH-IT/SKIP, MYTH/CONFIRMED, …); drives the verdict-ink lock.

`channels/daily-science/channel.yaml` is the fully-commented reference.

## Add a visual look (template)

Drop a `channels/<name>/templates/<look>.yaml`. Only `world` and `style` are
required; everything else inherits mechanical defaults from
`templates.DEFAULTS`. Add a `motion:` map (label → camera move) to art-direct
each beat. Point a channel at it with `default_template:` or `--template`.

## Add a research adapter

A research adapter turns a topic into `facts.json` (the channel-agnostic
contract read by the writer, director and SEO). Register it in
`research.ADAPTERS`:

```python
# research.py
def _my_source(topic, folder, ch):
    ...                                    # fetch / compute
    return {"topic": topic, "title": ...,  # minimum contract
            "facts": [...],                # whatever the writer needs
            "verdict": ...}                # optional (drives verdict ink)

ADAPTERS = {"manual": _manual, "llm": _llm, "my_source": _my_source}
```

Then set `research: my_source` in a channel's `channel.yaml`. If your adapter
doesn't already search the web, `web_context()` will add a live-news block
automatically (opt out with `web_research: false`).

## Add a generation provider

Generation (images/video) is dispatched through a `GenProvider` selected by
`$KALINGA_PROVIDER` (default `higgsfield`). Add one entry to
`make_video.PROVIDERS` implementing submit-and-wait, asset-URL extraction, and
an installed/authenticated probe. Models stay template tokens
(`image_model`, `video_model`), so nothing else changes.

## Add a TTS engine

Each voice backend is a self-contained `TTSEngine` in `make_video.ENGINES`
(voice pool, free/timings/expression flags, availability probe, synth fn). The
resolver, the fallback, and both UIs' engine toggle all derive from the
registry — a new engine is one entry. The built-ins are `edge` (free, karaoke
timings), `higgsfield` (Inworld), and `elevenlabs`.

## Add a browser-UI stage or action

The webui follows a strict pattern (see `ARCHITECTURE.md` §3.2):

- a read-only snapshot builder `_ss_<stage>` + a `STAGE_STATE` entry in
  `webui/state.py`,
- a handler `a_<stage>_<action>` + an `ACTIONS` entry in `webui/actions.py`.

No other module changes.

---

## Guard rails

Before you commit a change:

```bash
./build.sh     # byte-compiles + imports every module
pytest -q      # smoke tests: imports, the registries above, the sample channel
```

Both run in CI. The smoke test in `tests/test_smoke.py` asserts the registries
are wired and that no core module re-imports a removed feature — extend it as
you add capabilities so the next contributor can refactor safely.

## Python compatibility

Target is **system Python 3.9**: every module starts with
`from __future__ import annotations`; do not use runtime `X | Y` unions or
`match` statements.
