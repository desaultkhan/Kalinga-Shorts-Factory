"""
Smoke tests — cheap, offline guarantees that keep the codebase safe to extend:

  * every pipeline module imports (no syntax / import errors),
  * the extension REGISTRIES are wired (adapters, providers, TTS engines),
  * the sample channel + its template load and validate.

No network, no `claude`/`higgsfield` CLI, no credits. Run with `pytest -q`.
These are the guard rails that let you refactor the big modules with
confidence; grow them as you add features.
"""
import importlib

import pytest

# The modules that make up the pipeline. Importing each one is itself a test:
# it catches syntax errors and dangling imports to removed modules.
MODULES = [
    "config", "llm", "templates", "taste", "captions", "kenburns", "align",
    "recording", "usage", "daily", "research", "creative", "make_video",
    "validate", "seo", "evaluator", "brand", "cast_setup", "elevenlabs",
    "voiceover", "interactive", "kalinga",
    "webui", "webui.state", "webui.actions", "webui.context",
    "webui.session", "webui.server",
]


@pytest.mark.parametrize("mod", MODULES)
def test_module_imports(mod):
    importlib.import_module(mod)


def test_research_adapters_registry():
    import research
    assert set(research.ADAPTERS) == {"llm", "manual"}


def test_generation_and_tts_registries():
    import make_video
    assert "higgsfield" in make_video.PROVIDERS      # Higgsfield stays wired
    assert "edge" in make_video.ENGINES              # free default TTS


def test_example_channels_and_templates_load():
    import config
    import templates
    available = config.available()
    assert {"daily-science", "history-shorts"} <= set(available)
    for name in ("daily-science", "history-shorts"):
        ch = config.set_channel(name)
        for t in templates.available():              # every look loads
            tpl = templates.load(t)
            assert tpl.get("world") and tpl.get("style") and tpl.get("motion")
        tpl = templates.load(ch.default_template)
        assert tpl.get("world") and tpl.get("style")  # REQUIRED template keys
        labels = {s["label"] for s in ch.segments}
        assert {"HOOK", "CTA"} <= labels


def test_no_references_to_removed_features():
    """Guard against a removed feature creeping back into a core module."""
    import pathlib
    src = pathlib.Path(__file__).resolve().parents[1] / "pipeline" / "src"
    banned = ("import carousel", "import charts", "import series",
              "import comments", "import optimizer", "import publish")
    offenders = []
    for py in src.glob("*.py"):
        text = py.read_text()
        for token in banned:
            if token in text:
                offenders.append(f"{py.name}: {token}")
    assert not offenders, offenders
