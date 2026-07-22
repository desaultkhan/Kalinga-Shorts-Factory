# Pipeline internals

The channel-agnostic engine lives in `pipeline/src/`. For setup, commands and
the big picture, see the repository [`README.md`](../README.md); for the module
graph and design, see [`ARCHITECTURE.md`](../ARCHITECTURE.md); for how to add a
channel/adapter/provider/engine, see [`docs/EXTENDING.md`](../docs/EXTENDING.md).

Run everything from the repo root via the launcher:

```bash
python3 kalinga.py ship "why is the sky blue"
python3 kalinga.py status
```

## Module map (`pipeline/src/`)

| module | role |
|---|---|
| `kalinga.py` | the CLI entry point (ship / make / run / redo / show / status / …) |
| `config.py` | channel resolution + run-folder paths (the keystone) |
| `research.py` | pluggable research adapters → `facts.json` (`ADAPTERS`: llm, manual) |
| `creative.py` | script writer + judges + the research-aware director (facts/verdict lock) |
| `llm.py` | LLM backends: `claude -p` (default) or Anthropic API; per-role models |
| `make_video.py` | generation via the `higgsfield` CLI: voice, keyframes, clips, assembly, thumbnail (`PROVIDERS`/`ENGINES` registries) |
| `validate.py` | gates: tech QC / virality / channel SEO lint |
| `seo.py` | title / description / hashtags / tags (channel rules) |
| `evaluator.py` | AI review & repair + pre-publish critique |
| `templates.py` | template loader (pins `template.json` into the run) |
| `taste.py` | aesthetic PIL primitives (thumbnail / brand cards) |
| `captions.py`, `voiceover.py`, `kenburns.py`, `recording.py`, `align.py` | assembly helpers (captions, edge-tts, ken-burns, mic capture, whisper alignment) |
| `brand.py`, `cast_setup.py` | optional channel brand kit + recurring cast |
| `elevenlabs.py` | optional ElevenLabs music/voice connector |
| `usage.py`, `daily.py` | spend tracking (FYI) + queue helpers |
| `interactive.py` | the terminal `make` session |
| `webui/` | the browser UI package (home + video workflow) |
| `prompts/` | the static LLM prompt templates (one `.md` each) |

## Dev

```bash
./build.sh     # byte-compile + import every module (run from repo root)
pytest -q      # offline smoke tests
```

Python is system **3.9** — every module starts with `from __future__ import
annotations`; no runtime `X | Y`, no `match`.
