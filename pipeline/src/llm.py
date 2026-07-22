"""
llm.py — every creative call (script, hook judge, SEO, critique, experiment
proposals) goes through ask(). Two interchangeable backends:

  cli   `claude -p` — Claude Code headless, runs on the user's Claude
        SUBSCRIPTION ($0 marginal cost). Default model: claude-fable-5
        (back as the default 2026-07-02 — access verified on the CLI; it was
        claude-opus-4-8 from 2026-06-13 while Fable access was gated).
        THIS IS THE DEFAULT.
  api   Anthropic API — needs ANTHROPIC_API_KEY (pay-as-you-go, ~$0.05/video).
        Default model: claude-sonnet-4-6.

Auto-detect: claude CLI present -> cli; else ANTHROPIC_API_KEY -> api; else
unavailable (callers fall back to dry templates). Override with
KALINGA_BACKEND=cli|api; override the model with KALINGA_MODEL.

Cost control: when the CLI (subscription) is the active backend, a transient
CLI failure does NOT silently fall back to the paid API — it raises and the
resumable pipeline retries the subscription for free on rerun. Set
KALINGA_API_FALLBACK=1 (with ANTHROPIC_API_KEY) to opt into the paid fallback.

One-time setup for the cli backend:
    curl -fsSL https://claude.ai/install.sh | bash
    claude        # log in once if it asks
"""
from __future__ import annotations
import base64
import os
import shutil
import subprocess
from pathlib import Path

import usage as _usage

CLI_DEFAULT_MODEL = "claude-fable-5"    # KALINGA_MODEL overrides (e.g.
                                        # claude-opus-4-8 to pin the old one)
API_DEFAULT_MODEL = "claude-sonnet-4-6"
CLI_TIMEOUT = 900   # the model thinks; give long scripts room

# Per-ROLE model choice (cli/subscription backend only — these are subscription
# model ids). Research is heavy web synthesis, so it runs on Sonnet/Opus and
# saves Fable's craft budget for the writing; script + direction + the judges
# run on Fable (the creative core). Override any role with
# KALINGA_MODEL_<ROLE> (e.g. KALINGA_MODEL_RESEARCH=claude-opus-4-8); the
# global KALINGA_MODEL still pins every role at once (the escape hatch).
ROLE_MODELS = {
    "research":  "claude-sonnet-5",
    "script":    "claude-fable-5",
    "direction": "claude-fable-5",
    "judge":     "claude-fable-5",
}


def _cli_path():
    """The claude binary — PATH first, then the native-install location
    (cron's PATH usually lacks ~/.local/bin)."""
    found = shutil.which("claude")
    if found:
        return found
    native = Path.home() / ".local" / "bin" / "claude"
    return str(native) if native.exists() else None


def backend():
    """'cli' | 'api' | None."""
    forced = os.environ.get("KALINGA_BACKEND")
    if forced == "cli":
        return "cli" if _cli_path() else None
    if forced == "api":
        return "api" if os.environ.get("ANTHROPIC_API_KEY") else None
    if _cli_path():
        return "cli"
    if os.environ.get("ANTHROPIC_API_KEY"):
        return "api"
    return None


def model(role: str = None) -> str:
    """The model id for a call. Precedence: KALINGA_MODEL_<ROLE> (per-role
    override) > KALINGA_MODEL (global pin) > the role's default on the cli
    backend > the backend default. `role` in ROLE_MODELS (research | script |
    direction | judge) picks a role model on cli; None / an unknown role / the
    api backend fall through to the backend default."""
    if role:
        rv = os.environ.get("KALINGA_MODEL_" + role.upper())
        if rv:
            return rv
    explicit = os.environ.get("KALINGA_MODEL")
    if explicit:
        return explicit
    if backend() == "cli" and role in ROLE_MODELS:
        return ROLE_MODELS[role]
    return CLI_DEFAULT_MODEL if backend() == "cli" else API_DEFAULT_MODEL


_resolve_model = model      # a stable alias — callers whose param is named
                            # `model` (ask/Chat) shadow the function name


def available() -> bool:
    return backend() is not None


def describe() -> str:
    b = backend()
    return f"{b} / {model()}" if b else "none"


def ask(user: str, system: str = "", max_tokens: int = 1500,
        image_paths=None, tools=None, model: str = None) -> str:
    """One LLM call -> response text. image_paths: optional list of local
    image files the model should look at (e.g. sampled video frames).
    tools: optional Claude Code tool names (e.g. ["WebSearch"]) — cli
    backend only; the api backend ignores them. `model` overrides which model
    runs this ONE call (e.g. llm.model("research")); None uses the default."""
    mdl = model or _resolve_model()
    b = backend()
    if b == "cli":
        try:
            return _ask_cli(user, system, image_paths, tools, mdl)
        except (RuntimeError, OSError, subprocess.TimeoutExpired) as e:
            # The CLI runs on the claude.ai SUBSCRIPTION ($0). On a transient
            # CLI failure we DON'T silently bill the pay-as-you-go API — the
            # pipeline is resumable, so a rerun retries the subscription for
            # free, and the owner wants costs on the subscription. Opt into the
            # paid fallback explicitly with KALINGA_API_FALLBACK=1 (needs
            # ANTHROPIC_API_KEY). (`_cli_env` strips the key for the CLI call so
            # the old "key present → CLI refuses" failure no longer happens.)
            if (os.environ.get("KALINGA_API_FALLBACK")
                    and os.environ.get("ANTHROPIC_API_KEY")):
                print(f"  ! claude CLI failed ({str(e)[:90]}) — falling back to "
                      "the Anthropic API (KALINGA_API_FALLBACK is set)")
                # the role model is a SUBSCRIPTION id (e.g. claude-fable-5) the
                # API may not accept — let the API pick its own default
                return _ask_api(user, system, max_tokens, image_paths)
            raise
    if b == "api":
        return _ask_api(user, system, max_tokens, image_paths, mdl)
    raise RuntimeError(
        "no LLM backend — install the claude CLI "
        "(curl -fsSL https://claude.ai/install.sh | bash; uses your Claude "
        "subscription) or export ANTHROPIC_API_KEY")


def _cli_env():
    """Environment for the claude CLI subprocess: drop ANTHROPIC_API_KEY so the
    CLI uses the claude.ai SUBSCRIPTION login (its $0 default) instead of
    refusing — when the key is set it takes precedence over the login and
    disables it. So Kalinga runs on the subscription with NO key needed, even if
    one happens to be exported for other tools. (The `api` backend, selected by
    KALINGA_BACKEND=api, still reads the key directly — it doesn't use this.)"""
    env = dict(os.environ)
    env.pop("ANTHROPIC_API_KEY", None)
    return env


def _ask_cli(user, system, image_paths, tools=None, mdl=None):
    mdl = mdl or _resolve_model()
    prompt = (system + "\n\n====\n\n" if system else "") + user
    cmd = [_cli_path(), "-p", "--model", mdl, "--output-format", "text"]
    allowed = list(tools or [])
    if image_paths:
        prompt += ("\n\nBefore answering, Read these image files (reference "
                   "images, in order) and use what you see:\n"
                   + "\n".join(str(p) for p in image_paths))
        if "Read" not in allowed:
            allowed.append("Read")
    if allowed:
        cmd += ["--allowedTools", ",".join(allowed)]
    r = subprocess.run(cmd, input=prompt, capture_output=True, text=True,
                       timeout=CLI_TIMEOUT, env=_cli_env())
    if r.returncode != 0:
        raise RuntimeError(
            f"claude CLI failed: {(r.stderr or r.stdout).strip()[:300]}")
    out = r.stdout.strip()
    if not out:
        raise RuntimeError("claude CLI returned empty output")
    # FYI token tracking — cli text mode reports no usage, so estimate (≈char/4)
    _usage.record_llm(mdl, len(prompt) // 4, len(out) // 4, approx=True)
    return out


def _ask_api(user, system, max_tokens, image_paths, mdl=None):
    import anthropic
    mdl = mdl or API_DEFAULT_MODEL
    if image_paths:
        content = [{"type": "image",
                    "source": {"type": "base64", "media_type": "image/jpeg",
                               "data": base64.b64encode(
                                   Path(p).read_bytes()).decode()}}
                   for p in image_paths]
        content.append({"type": "text", "text": user})
    else:
        content = user
    kw = {"model": mdl, "max_tokens": max_tokens,
          "messages": [{"role": "user", "content": content}]}
    if system:
        kw["system"] = system
    msg = anthropic.Anthropic().messages.create(**kw)
    u = getattr(msg, "usage", None)
    _usage.record_llm(mdl, getattr(u, "input_tokens", 0),
                      getattr(u, "output_tokens", 0))
    return msg.content[0].text


class Chat:
    """Multi-turn conversation — used ONLY by the interactive script
    co-writing loop (every other call site stays one-shot ask() by design:
    a hook judge with memory anchors on prior drafts).

    Keeps an in-memory transcript as the source of truth. The cli backend
    additionally rides claude's on-disk sessions (-p --session-id /
    -p --resume, verified on 2.1.175) so each round sends only the new
    turn; ANY session failure falls back to replaying the transcript in
    one stateless call — context is never silently lost. The api backend
    replays the transcript through the SDK each call."""

    def __init__(self, system: str = "", session_id: str = None,
                 model: str = None):
        import uuid
        self.system = system
        self.session_id = session_id or str(uuid.uuid4())
        self.messages = []   # [{"role": "user"|"assistant", "content": str}]
        # a caller-supplied id refers to an existing on-disk session
        self._started = session_id is not None
        self._broken = False
        # which model this conversation runs on (e.g. llm.model("direction"));
        # None → the default. Resolved once so every turn is consistent.
        self.model = model or _resolve_model()

    def ask(self, user: str, max_tokens: int = 4000) -> str:
        self.messages.append({"role": "user", "content": user})
        out = None
        if backend() == "cli" and not self._broken:
            try:
                out = self._cli_turn(user)
            except (RuntimeError, OSError, subprocess.TimeoutExpired) as e:
                self._broken = True
                print(f"  ! chat session unavailable ({str(e)[:120]}) — "
                      f"replaying conversation in a fresh call")
        if out is None:
            out = self._replay(max_tokens)
        self.messages.append({"role": "assistant", "content": out})
        return out

    def _cli_turn(self, user: str) -> str:
        cmd = [_cli_path(), "-p", "--model", self.model,
               "--output-format", "text"]
        if self._started:
            cmd += ["--resume", self.session_id]
        else:
            cmd += ["--session-id", self.session_id]
            if self.system:
                cmd += ["--append-system-prompt", self.system]
        r = subprocess.run(cmd, input=user, capture_output=True, text=True,
                           timeout=CLI_TIMEOUT, env=_cli_env())
        if r.returncode != 0:
            raise RuntimeError((r.stderr or r.stdout).strip()[:300])
        out = r.stdout.strip()
        if not out:
            raise RuntimeError("claude CLI returned empty output")
        self._started = True
        _usage.record_llm(self.model, len(user) // 4, len(out) // 4, approx=True)
        return out

    def _replay(self, max_tokens: int) -> str:
        """The whole transcript in one stateless call — the api transport
        and the universal cli fallback."""
        if backend() == "api":
            import anthropic
            # this conversation's model is a SUBSCRIPTION id chosen for the cli
            # backend (e.g. claude-fable-5); the API may not accept it, so the
            # replay transport uses the API default
            amdl = API_DEFAULT_MODEL
            kw = {"model": amdl, "max_tokens": max_tokens,
                  "messages": self.messages}
            if self.system:
                kw["system"] = self.system
            msg = anthropic.Anthropic().messages.create(**kw)
            u = getattr(msg, "usage", None)
            _usage.record_llm(amdl, getattr(u, "input_tokens", 0),
                              getattr(u, "output_tokens", 0))
            return msg.content[0].text
        # cli replay fallback: one stateless ask() on this chat's model
        turns = []
        for m in self.messages[:-1]:
            who = "User" if m["role"] == "user" else "You replied"
            turns.append(f"{who}:\n{m['content']}")
        last = self.messages[-1]["content"]
        prompt = (("Earlier conversation:\n\n" + "\n\n".join(turns)
                   + "\n\nUser:\n" + last) if turns else last)
        return ask(prompt, system=self.system, max_tokens=max_tokens,
                   model=self.model)


if __name__ == "__main__":
    print(f"backend: {describe()}")
    if available():
        print(ask("Reply with exactly: backend OK"))
