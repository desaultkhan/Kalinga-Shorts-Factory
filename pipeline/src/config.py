"""Channel resolution + shared paths.

Code in src/ is channel-agnostic. Each channel lives in channels/<name>/ with
its own channel.yaml (premise, persona, segments, research adapter, SEO
rules), templates/, queue.csv, state files, and output/. The current channel
is resolved once per process: --channel flag > KALINGA_CHANNEL env > the sole
channel folder. Cross-channel craft knowledge accumulates in
channels/learnings.md (GLOBAL_LEARNINGS).
"""
from __future__ import annotations
import json
import os
import re
import sys
from datetime import date
from pathlib import Path


def tint(s, *codes) -> str:
    """ANSI-wrap s (e.g. tint(x, "1", "36") = bold cyan) when stdout is a
    TTY and NO_COLOR is unset — safe to call from cron/pipes."""
    if not codes or not sys.stdout.isatty() or os.environ.get("NO_COLOR"):
        return str(s)
    return "\033[" + ";".join(codes) + f"m{s}\033[0m"


PIPELINE = Path(__file__).resolve().parent.parent       # pipeline/
REPO = PIPELINE.parent
CHANNELS_DIR = REPO / "channels"
FONTS = PIPELINE / "assets" / "fonts"                   # shared across channels
GLOBAL_LEARNINGS = CHANNELS_DIR / "learnings.md"        # cross-channel craft

# Higgsfield pricing + default generation models — the ONE home for these
# numbers/ids (they were duplicated across higgsfield.py / interactive.py /
# every tpl.get default). Pricing changes and model bumps edit here only.
CREDITS_PER_KEYFRAME = 2         # nano banana pro 1k
CREDITS_PER_CLIP_SEC = 3.5       # seedance fast 1080p (= 720p price)
DEFAULT_IMAGE_MODEL = "nano_banana_2"    # CLI job-type for "Nano Banana Pro"
DEFAULT_VIDEO_MODEL = "seedance_2_0"

# Optimizer dimensions any Shorts channel can vary (channel.yaml may override).
DEFAULT_DIMENSIONS = ["hook style", "CTA phrasing", "posting time",
                      "tags/SEO", "pacing/segment length",
                      "overlay text style", "content depth", "title style"]


class Channel:
    """One channel folder: its channel.yaml plus derived paths.

    SHOWS (channel.yaml `shows:`): one channel can carry several recurring
    FORMATS — each a named map of PARTIAL overrides laid over the channel base
    (premise, persona, segments, flexible_segments, research, default_template,
    topic_noun, seo, visual_rules, …). Top-level keys REPLACE the base (a show
    owns whatever it declares); everything the show doesn't declare — brand,
    cast, queue, learnings — stays the CHANNEL's. Activate with KALINGA_SHOW; a
    run pins its show into template.json so resumes reactivate it automatically
    (topic_dir), same precedent as the pinned template. (Optional — a channel
    with no `shows:` block just uses its base format.)"""

    def __init__(self, name: str):
        self.name = name
        self.dir = CHANNELS_DIR / name
        f = self.dir / "channel.yaml"
        if not f.exists():
            names = ", ".join(available()) or "none"
            raise FileNotFoundError(
                f"channels/{name}/channel.yaml missing (available: {names}; "
                "scaffold one with: python3 kalinga.py new-channel <name>)")
        import yaml
        self._base = yaml.safe_load(f.read_text()) or {}
        self._show = None
        if os.environ.get("KALINGA_SHOW"):
            self.set_show(os.environ["KALINGA_SHOW"])

    # ---- shows ----
    @property
    def cfg(self) -> dict:
        """The EFFECTIVE config: the channel base with the active show's
        overrides on top (top-level keys replace)."""
        if not self._show:
            return self._base
        merged = dict(self._base)
        merged.update(self.shows.get(self._show) or {})
        return merged

    @property
    def shows(self) -> dict:
        """{name: overrides} — the channel's declared shows ({} if none)."""
        return dict(self._base.get("shows") or {})

    @property
    def show(self):
        """The ACTIVE show name, or None (= the channel base format)."""
        return self._show

    def set_show(self, name) -> None:
        """Activate one of the channel's shows (None/'' = the base format).
        An unknown name is a loud error — a typo'd show must never silently
        produce the wrong format."""
        name = (name or "").strip() or None
        if name and name not in self.shows:
            known = ", ".join(self.shows) or "none defined in channel.yaml"
            raise ValueError(f"channel '{self.name}' has no show '{name}' — "
                             f"known: {known}")
        self._show = name

    # ---- paths ----
    @property
    def queue(self): return self.dir / "queue.csv"
    @property
    def learnings(self): return self.dir / "learnings.md"
    @property
    def videos(self): return self.dir / "videos.json"
    @property
    def overrides(self): return self.dir / "overrides.json"
    @property
    def experiments(self): return self.dir / "experiments.json"
    @property
    def output(self): return self.dir / "output"
    @property
    def templates_dir(self): return self.dir / "templates"
    @property
    def music_dir(self): return self.dir / "assets" / "music"
    @property
    def cast_dir(self): return self.dir / "cast"

    # ---- channel cast roster (channels/<name>/cast.json) ----
    @property
    def cast(self) -> dict:
        """The channel's persistent cast — {name: {voice, gender, personality,
        appearance, avatar}}. {} when the channel has none (single narrator)."""
        p = self.dir / "cast.json"
        try:
            return json.loads(p.read_text()) if p.exists() else {}
        except ValueError:
            return {}

    def cast_member(self, name: str) -> dict:
        return (self.cast or {}).get(name, {})

    def cast_avatar(self, name: str) -> Path:
        """Absolute path to a cast member's avatar PNG (may not exist)."""
        m = self.cast_member(name)
        rel = m.get("avatar") or f"cast/{name}.png"
        return self.dir / rel

    def cast_refs(self, name: str, **filters) -> list:
        """The cast member's EXISTING reference-library images matching the
        given filters (outfit / kind / angle / light), each as a metadata dict
        with an absolute `path`. `refs` in cast.json is a list of
        {file, outfit, kind, angle, light, when} written by `kalinga.py cast`
        — the director reads it to choose which reference fits a scene."""
        out = []
        for r in self.cast_member(name).get("refs") or []:
            if not isinstance(r, dict) or "file" not in r:
                continue
            if any(r.get(k) != v for k, v in filters.items()):
                continue
            p = self.dir / r["file"]
            if p.exists():
                d = dict(r)
                d["path"] = p
                out.append(d)
        return out

    def cast_ref(self, name: str, **filters):
        """Absolute Path to the FIRST reference image matching the filters
        (e.g. outfit='casual', kind='full', angle='front'), or None."""
        hits = self.cast_refs(name, **filters)
        return hits[0]["path"] if hits else None

    def cast_outfits(self, name: str) -> list:
        """Outfits this member has on-disk references for (e.g. formal,
        casual, ethnic)."""
        seen = []
        for r in self.cast_refs(name):
            if r["outfit"] not in seen:
                seen.append(r["outfit"])
        return seen

    # ---- channel.yaml accessors (safe defaults) ----
    @property
    def title(self): return self.cfg.get("title", self.name)
    @property
    def premise(self): return self.cfg.get("premise", "")
    @property
    def persona(self): return self.cfg.get("persona", "a sharp, warm narrator")
    @property
    def topic_noun(self): return self.cfg.get("topic_noun", "topic")
    @property
    def research(self): return self.cfg.get("research", "manual")
    @property
    def default_template(self): return self.cfg.get("default_template", "")
    @property
    def positive_verdict(self): return self.cfg.get("positive_verdict")

    @property
    def negative_verdict_markers(self) -> tuple:
        """Phrases that would FLIP this channel's verdict if a rewrite
        introduced one — the polarity vocabulary of the facts/verdict lock
        (channel.yaml `negative_verdict_markers:`). 'not <positive_verdict>'
        is always included when a positive_verdict exists; a channel with no
        verdict (a story channel) gets an empty tuple and the polarity check
        is inert. Channel content, never hardcoded in the engine."""
        out = [str(x).strip().lower()
               for x in self.cfg.get("negative_verdict_markers") or []
               if str(x).strip()]
        pv = self.positive_verdict
        if pv:
            neg = "not " + str(pv).strip().lower()
            if neg not in out:
                out.append(neg)
        return tuple(out)

    @property
    def allowed_constants(self) -> tuple:
        """Channel-known numeric constants (screening thresholds etc.,
        channel.yaml `allowed_constants:`) that may legitimately appear in
        generated copy even when they aren't in this video's facts — used by
        the invented-number guards."""
        return tuple(str(x) for x in self.cfg.get("allowed_constants") or [])

    @property
    def voice_rules(self): return self.cfg.get("voice_rules", [])
    @property
    def visual_rules(self): return self.cfg.get("visual_rules", "")
    @property
    def seo(self): return self.cfg.get("seo", {})
    @property
    def dimensions(self):
        return self.cfg.get("optimizer_dimensions", DEFAULT_DIMENSIONS)

    @property
    def segments(self) -> list:
        segs = self.cfg.get("segments")
        if not segs:
            raise ValueError(
                f"channels/{self.name}/channel.yaml defines no segments")
        return segs

    @property
    def labels(self) -> list:
        return [s["label"] for s in self.segments]

    @property
    def optional_labels(self) -> list:
        """Labels the writer MAY include but isn't required to (channel.yaml
        segment `optional: true`) — e.g. an OUTRO before the CTA."""
        return [s["label"] for s in self.segments if s.get("optional")]

    @property
    def flexible_segments(self) -> bool:
        """When true (channel.yaml `flexible_segments: true`), the segment list
        is a SUGGESTED beat menu, NOT a fixed contract: the writer may COMBINE
        beats, DROP one that doesn't earn its place, ADD a beat of its own, and
        choose the order. Only segments marked `required: true` are enforced.
        Default false (strict — every non-optional label exactly once)."""
        return bool(self.cfg.get("flexible_segments", False))

    @property
    def required_labels(self) -> list:
        """Labels the script MUST contain exactly once. In flexible mode that's
        ONLY the segments marked `required: true` (often none); otherwise it's
        every non-optional segment (the strict default)."""
        if self.flexible_segments:
            return [s["label"] for s in self.segments if s.get("required")]
        opt = set(self.optional_labels)
        return [s["label"] for s in self.segments if s["label"] not in opt]

    def segment(self, label: str) -> dict:
        for s in self.segments:
            if s["label"] == label:
                return s
        return {}

    def normalize_topic(self, topic: str) -> str:
        """Canonical topic id: keep the user's casing but make it
        filesystem-safe."""
        return topic.strip().replace("/", "-").replace(" ", "-")


def available() -> list:
    if not CHANNELS_DIR.exists():
        return []
    return sorted(p.parent.name for p in CHANNELS_DIR.glob("*/channel.yaml"))


_current = None


def set_channel(name=None) -> Channel:
    """Resolve and pin the process-wide channel: explicit name >
    KALINGA_CHANNEL > the only channel that exists."""
    global _current
    name = name or os.environ.get("KALINGA_CHANNEL")
    if not name:
        names = available()
        if len(names) == 1:
            name = names[0]
        elif not names:
            raise RuntimeError(
                "no channels yet — scaffold one: python3 kalinga.py "
                "new-channel <name>")
        else:
            raise RuntimeError(
                f"several channels exist ({', '.join(names)}) — pick one "
                "with --channel <name> or KALINGA_CHANNEL")
    _current = Channel(name)
    return _current


def channel() -> Channel:
    if _current is None:
        set_channel()
    return _current


def _activate_pinned_show(ch: Channel, folder: Path) -> None:
    """A resumed run REACTIVATES the show it was started under (stamped into
    template.json by templates.resolve) — so `run assemble TOPIC` gets the
    right premise/segments/researcher with no flags. The pin wins over a
    conflicting live --show/KALINGA_SHOW (same precedent as the pinned
    template beating --template on resume), with a warning. Best-effort."""
    try:
        tj = folder / "template.json"
        pinned = ((json.loads(tj.read_text()).get("show") or "").strip()
                  if tj.exists() else "")
    except (ValueError, OSError):
        return
    if not pinned or pinned == ch._show:
        return
    try:
        if ch._show:
            print(f"  ! this run is pinned to show '{pinned}' — overriding "
                  f"the requested '{ch._show}' (pins win on resume)",
                  file=sys.stderr)
        ch.set_show(pinned)
    except ValueError:
        print(f"  ! run pinned to unknown show '{pinned}' (removed from "
              f"channel.yaml?) — using the channel base format",
              file=sys.stderr)


def topic_dir(topic: str, create: bool = True) -> Path:
    """Folder for this topic's run inside the channel's output/. Reuses the
    newest existing *_TOPIC folder (so pipeline stages and resumes share it);
    otherwise creates <today>_TOPIC."""
    ch = channel()
    topic = ch.normalize_topic(topic)
    existing = sorted(ch.output.glob(f"*_{topic}"))
    if existing:
        migrate_layout(existing[-1])
        _activate_pinned_show(ch, existing[-1])
        return existing[-1]
    p = ch.output / f"{date.today():%Y-%m-%d}_{topic}"
    if create:
        p.mkdir(parents=True, exist_ok=True)
    return p


# ---------- per-run artifact layout (subfolders keep the run folder tidy) ----------
# A run folder keeps human-facing DELIVERABLES + state/manifest files at its
# root (short.mp4, thumbnail.png, *.json, *.md, music.txt, …); the many indexed
# media files and build scratch live in subfolders. Indices follow the chosen
# delivery order (index 0 = the opener). `.versions/` is handled separately by
# make_video.archive(). This routing is the SINGLE source of truth for the
# layout: stages build artifact paths through art()/art_glob() using flat
# CANONICAL names (e.g. "seg0.mp3", "key3.png") and never hardcode subfolders.
ART_SUBDIRS = ("audio", "keyframes", "clips", "charts", "overlays", "build")
_ART_ROUTES = (
    # seg<i>.mp3, dialogue lines seg<i>_l<k>.mp3, and SECTION audio seg<i>_s<k>.mp3
    # (section-native: a sectioned beat voices each section to its own file) +
    # an in-flight recording capture (seg<i>[_s<k>].rec.<ext>)
    ("audio",     re.compile(r"^seg\d+(_l\d+|_s\d+)?(\.rec)?\.(mp3|wav|m4a|webm|ogg)$")),
    # key<i>.png and the multi-shot sub-shots key<i>_<k>.png
    ("keyframes", re.compile(r"^key\d+(_\d+)?\.png$")),
    ("clips",     re.compile(r"^clip\d+(_\d+)?\.(mp4|webm)$")),
    ("charts",    re.compile(r"^chart\d+\.png$")),
    ("overlays",  re.compile(r"^text\d+(_\d+)?\.png$")),
    # build scratch — section-native adds part<i>_s<k>.mp4 and section-tagged
    # boomerang/freeze/stitch scratch (the tag may carry an _s<k> suffix), so the
    # name patterns accept a word tag rather than a bare \d+
    ("build",     re.compile(r"^(part\d+(_s\d+)?\.mp4|_bm\w+\.mp4|_kb\w+\.mp4|"
                             r"_hook\w*\.(mp4|txt)|concat\.txt|merged\.mp4|"
                             r"captioned\.mp4|nomusic\.mp4|subs\.ass|"
                             r"audio_caps\.json|audio_scaled\.json|"
                             r"sub\w+_\d+\.mp4|base\w+\.mp4|_subcat\w+\.txt|"
                             r"_fz\w+(_\w+)?\.(mp4|png|txt)|"
                             r"frame\d+\.jpg|rev\d+\.jpg)$")),
)


def art_subdir(name: str) -> str:
    """The subfolder (relative to a run folder) a canonical artifact name lives
    in; '' = the run-folder root (deliverables + state files)."""
    for sub, pat in _ART_ROUTES:
        if pat.match(name):
            return sub
    return ""


def art(folder, name: str) -> Path:
    """Absolute path to a per-run artifact by its canonical (flat) name, routed
    into its subfolder (created on demand so writers use the path directly)."""
    folder = Path(folder)
    sub = art_subdir(name)
    if sub:
        d = folder / sub
        d.mkdir(parents=True, exist_ok=True)
        return d / name
    return folder / name


def art_rel(name: str) -> str:
    """Run-folder-relative path string for a canonical name (e.g.
    'audio/seg0.mp3', or 'short.mp4' for a root deliverable)."""
    sub = art_subdir(name)
    return f"{sub}/{name}" if sub else name


def art_glob(folder, pattern: str) -> list:
    """Glob a canonical artifact pattern (e.g. 'key*.png', 'seg*.mp3',
    'short.mp4') in the subfolder it belongs to. Sorted Paths; [] if absent."""
    folder = Path(folder)
    sub = art_subdir(pattern.replace("*", "0"))
    base = folder / sub if sub else folder
    return sorted(base.glob(pattern)) if base.exists() else []


def migrate_layout(folder) -> int:
    """One-time, idempotent: move flat per-run artifacts left by older runs
    into their subfolders so presence-based caching/resume finds them (and paid
    artifacts aren't regenerated). New/empty folders and already-migrated ones
    are a no-op. Never descends into subfolders (incl. .versions/). Returns the
    count moved."""
    folder = Path(folder)
    if not folder.is_dir():
        return 0
    moved = 0
    for child in list(folder.iterdir()):
        if child.is_dir():
            continue
        sub = art_subdir(child.name)
        if not sub:
            continue
        dest = folder / sub / child.name
        if dest.exists():
            continue
        try:
            (folder / sub).mkdir(parents=True, exist_ok=True)
            child.rename(dest)
            moved += 1
        except OSError:
            pass
    return moved


# Legacy aliases — gen-1/2 modules (video.py renderer, voiceover.py,
# scriptgen.py, higgsfield.py, run_pipeline.py) still use the old names.
stock_dir = topic_dir
ROOT = PIPELINE

_LEGACY_PATHS = {"LEARNINGS": "learnings", "QUEUE": "queue",
                 "VIDEOS": "videos", "OUTPUT": "output"}


def __getattr__(name):
    """config.LEARNINGS / QUEUE / VIDEOS / OUTPUT now live on the channel;
    resolve them lazily so legacy modules keep working unmodified."""
    if name in _LEGACY_PATHS:
        return getattr(channel(), _LEGACY_PATHS[name])
    raise AttributeError(f"module 'config' has no attribute '{name}'")
