"""
webui.context — shared server-side state for the browser UI.

MULTI-PROJECT (owner call 2026-07-04: "I want to work on multiple projects and
come back whenever the AI is done"): one server process holds SEVERAL project
sessions at once. Each open run folder is a `Box` in `SESSIONS`; the browser
looks at ONE box at a time (`FOCUS`), and every long operation is a `Job`
pinned to the box that started it — jobs on DIFFERENT run folders run in
parallel, so you can kick off a render, go Home, open another project, and
come back when the first is done.

`ctx.APP` / `ctx.MODE` are kept as the API every other webui module reads —
they are now module PROPERTIES resolving through the CURRENT box: the
thread-local box inside a job worker (a job keeps its own project even after
the browser switches away — its closures read ctx.APP at execution time), else
the browser's focused box. The module's __class__ is swapped for this;
rebinding goes through `bind_box`/`set_focus`, never `ctx.APP = …`.

What stays serialized (enforced in run_job):
  - one running job per RUN FOLDER — the folder isn't parallel-safe
  - one CHANNEL across all running jobs — config.channel() is process-global,
    so a second channel's job would poison the first (open as many projects
    as you like on the SAME channel)

stdout: with several jobs live at once, sys.stdout is replaced once by a
thread-aware router — each worker thread's prints stream to ITS job's log,
everything else falls through to the real stdout.
"""
from __future__ import annotations
import sys
import threading
import time
import traceback
import types
from pathlib import Path

import config
import interactive as iv
import kalinga


# ======================================================================
# per-project session state
# ======================================================================
class App:
    def __init__(self, s: iv.Session):
        self.s = s
        self.stage = iv.first_incomplete(s.folder)
        self.concept_suggestion = None  # last AI concept draft (concept stage)
        self.shutdown = False


class Box:
    """ONE project session: a run folder bound to a workflow. Several boxes
    live side by side in SESSIONS; the browser focuses one at a time."""
    def __init__(self, sid, mode, channel, topic, folder, app=None):
        self.sid = sid
        self.mode = mode               # video
        self.channel = channel
        self.topic = topic
        self.folder = folder
        self.app = app


SESSIONS: dict = {}           # sid -> Box, every project opened this server
FOCUS: str = None             # sid the browser is viewing (None = home)
JOBS: list = []               # every Job this server has run, oldest first
HOME = {"channel": None}      # remembered launcher selections
SETUP = {"warning": None}     # provider setup problem shown as a UI banner
_LOCK = threading.Lock()
_TLS = threading.local()      # .box / .job — set inside job worker threads


def sid_for(channel: str, folder: Path, mode: str) -> str:
    return f"{channel}::{Path(folder).name}::{mode}"


def _current_box():
    b = getattr(_TLS, "box", None)
    if b is not None:
        return b
    return SESSIONS.get(FOCUS) if FOCUS else None


def bind_box(mode, channel, topic, folder, app=None) -> Box:
    """Create-or-refresh the Box for a run folder + workflow and FOCUS it."""
    global FOCUS
    sid = sid_for(channel, folder, mode)
    box = SESSIONS.get(sid)
    if box is None:
        box = Box(sid, mode, channel, topic, folder, app=app)
        SESSIONS[sid] = box
    else:
        if app is not None:
            box.app = app
    FOCUS = sid
    return box


def set_focus(sid):
    global FOCUS
    FOCUS = sid


def _cur_folder():
    b = _current_box()
    if b is None:
        return None
    if b.app is not None:
        return b.app.s.folder
    return b.folder


# ---- ctx.APP / ctx.MODE as box-resolving module properties -----------------
class _CtxModule(types.ModuleType):
    @property
    def APP(self):
        b = _current_box()
        return b.app if b is not None else None

    @property
    def MODE(self):
        b = _current_box()
        return b.mode if b is not None else "home"

    @property
    def JOB(self):
        """The CURRENT box's active/most-recent job (legacy single-job API)."""
        return _job_for_current()


sys.modules[__name__].__class__ = _CtxModule


# ======================================================================
# jobs — several at once, one per run folder, streamed per-thread
# ======================================================================
class Job:
    """A background unit of work whose stdout streams to the browser."""
    _n = 0

    def __init__(self, label, box: Box = None, channel: str = None):
        Job._n += 1
        self.id = Job._n
        self.label = label
        self.lines = []
        self.status = "running"        # running | done | error
        self.error = None
        self.box = box
        self.channel = channel or (box.channel if box else None)
        self.topic = box.topic if box else None
        self.folder = box.folder if box else None
        self.mode = box.mode if box else "tool"
        self.started = time.time()

    # file-like: the stage functions print; we capture (no ANSI — not a TTY)
    def write(self, txt):
        if txt:
            self.lines.append(txt)
            sys.__stdout__.write(txt)

    def flush(self):
        sys.__stdout__.flush()

    # interactive helpers reused here color their output via config.tint,
    # which probes isatty — a job is never a TTY, so they print plain
    def isatty(self):
        return False

    def log(self) -> str:
        return "".join(self.lines)


class _StdoutRouter:
    """Thread-aware stdout: a job worker's prints go to ITS job, everything
    else falls through to the real stdout. Installed once — with several jobs
    running at once a plain sys.stdout swap would interleave their logs."""
    def write(self, txt):
        j = getattr(_TLS, "job", None)
        (j or sys.__stdout__).write(txt)

    def flush(self):
        j = getattr(_TLS, "job", None)
        if j is not None:
            j.flush()
        else:
            sys.__stdout__.flush()

    def isatty(self):
        if getattr(_TLS, "job", None) is not None:
            return False
        try:
            return sys.__stdout__.isatty()
        except Exception:                            # noqa: BLE001
            return False


def _install_router():
    if not isinstance(sys.stdout, _StdoutRouter):
        sys.stdout = _StdoutRouter()


def running_job_for(folder: Path):
    for j in JOBS:
        if j.status == "running" and j.folder is not None \
                and folder is not None and j.folder == folder:
            return j
    return None


def guard_channel(channel: str):
    """Raise if a RUNNING job belongs to a different channel — the pipeline's
    channel is process-global, so switching would poison that job."""
    for j in JOBS:
        if j.status == "running" and j.channel and channel \
                and j.channel != channel:
            raise RuntimeError(
                f"'{j.label}' is still running on channel {j.channel} — "
                f"parallel projects must stay on one channel (open another "
                f"{j.channel} project, or wait for it to finish)")


def run_job(label, fn):
    """Start fn() in a background thread pinned to the CURRENT box. Jobs on
    different run folders run in PARALLEL; raises with the reason when this
    folder (or a foreign channel, or the tool lane) is already busy."""
    box = _current_box()
    ch = box.channel if box else (HOME.get("channel") or None)
    with _LOCK:
        for j in JOBS:
            if j.status != "running":
                continue
            if box is not None and j.folder is not None \
                    and box.folder == j.folder:
                raise RuntimeError(
                    f"'{j.label}' is still running on this project — one job "
                    "per run folder (other projects can run in parallel)")
            if j.channel and ch and j.channel != ch:
                raise RuntimeError(
                    f"'{j.label}' is still running on channel {j.channel} — "
                    "parallel projects must stay on one channel for now")
            if box is None and j.folder is None:
                raise RuntimeError(f"tool '{j.label}' is still running")
        job = Job(label, box, ch)
        JOBS.append(job)
        if len(JOBS) > 60:                      # keep the tail; ids stay unique
            del JOBS[:len(JOBS) - 60]
    _install_router()

    def worker():
        _TLS.box = box
        _TLS.job = job
        try:
            fn()
            job.status = "done"
        except Exception as e:               # noqa: BLE001 — surfaced to UI
            traceback.print_exc(file=job)
            job.status = "error"
            job.error = str(e) or e.__class__.__name__
        finally:
            _TLS.job = None
            _TLS.box = None
    threading.Thread(target=worker, daemon=True).start()
    return job.id


def _job(label, fn):
    """run_job wrapped as an action result: ("job", id). Raises with a clear
    reason when it can't start."""
    return ("job", run_job(label, fn))


def find_job(jid):
    for j in JOBS:
        if str(j.id) == str(jid):
            return j
    return None


def _job_for_current():
    """The job the focused project should surface: its RUNNING job, else its
    most recent one."""
    b = _current_box()
    if b is None:
        # home: surface any running folder-less tool
        for j in reversed(JOBS):
            if j.folder is None and j.status == "running":
                return j
        return None
    mine = [j for j in JOBS if j.folder == b.folder]
    for j in mine:
        if j.status == "running":
            return j
    return mine[-1] if mine else None


def _jobinfo():
    j = _job_for_current()
    return ({"id": j.id, "label": j.label, "status": j.status}
            if j else None)


def jobs_summary() -> list:
    """Every recent job for the Home 'in flight' board — newest first."""
    out = []
    for j in JOBS[-14:]:
        out.append({
            "id": j.id, "label": j.label, "status": j.status,
            "channel": j.channel, "topic": j.topic, "mode": j.mode,
            "folder": j.folder.name if j.folder else None,
            "secs": int(time.time() - j.started),
        })
    return list(reversed(out))


# ======================================================================
# artifact helpers (both state builders need them)
# ======================================================================
def _mtime(p: Path):
    try:
        return int(p.stat().st_mtime)
    except OSError:
        return 0


def _art(folder: Path, name: str):
    """{name, mtime} for an artifact (name is the run-folder-relative path,
    routed into its subfolder), or None when absent — the page turns this into
    /artifact/<name>?v=<mtime> so a regen busts the cache. Callers pass the
    flat canonical name (e.g. "seg0.mp3"); config.art() resolves the layout."""
    p = config.art(folder, name)
    return {"name": config.art_rel(name), "mtime": _mtime(p)} if p.exists() else None
