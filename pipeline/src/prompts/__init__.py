"""
LLM prompt templates, kept as .md files in this folder so they're easy to READ
and MAINTAIN without hunting through code. Each file is one prompt (a system
prompt, a judge rubric, a user-prompt seed, or a reusable fragment).

Usage (in code):
    import prompts
    DIRECT_SYS = prompts.load("director.system")
    user = prompts.load("hook_judge").format(**ctx)

`load(name)` returns the file's text VERBATIM (byte-for-byte, cached) — the files
were generated from the original in-code constants, so behaviour is unchanged;
callers still `.format(...)` templates exactly as before. Any `{placeholder}` in a
template is a str.format field; literal braces must be doubled `{{`/`}}` (same as
when the prompt lived in a triple-quoted string).

Only fully-STATIC prompts live here. Prompts that are assembled dynamically in
code (loops/conditionals over facts, cast, segments — e.g. the director's
per-beat user prompts, the writer guardrails builder, the research adapters) stay
in their modules; some of them still pull a static chunk from here via load().
"""
from functools import lru_cache
from pathlib import Path

_DIR = Path(__file__).resolve().parent


@lru_cache(maxsize=None)
def load(name: str) -> str:
    """Return the prompt template `<name>.md` from this folder, verbatim and
    cached. Raises FileNotFoundError with the resolved path if it's missing."""
    p = _DIR / f"{name}.md"
    if not p.exists():
        raise FileNotFoundError(f"prompt template not found: {p}")
    return p.read_text()
