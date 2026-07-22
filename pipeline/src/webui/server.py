"""
webui.server — the stdlib HTTP layer + entry point.

A ThreadingHTTPServer (no deps) serving the single-page app: GET / returns the
assembled PAGE (page.html + styles.css + app.js from assets/), GET /artifact/…
streams a run artifact, GET /api/state returns build_state(); POST routes to the
action handlers, the launcher (open/home/run), uploads, nav and quit. main()
resolves a topic (or the HOME launcher) and serves until Quit/Ctrl-C.
"""
from __future__ import annotations
import json
import socket
import threading
import webbrowser
import sys
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs, unquote

import config
import interactive as iv
import kalinga
import make_video
import templates

from . import context as ctx
from .context import App, _cur_folder
from .state import build_state
from .actions import act
from .session import open_session, go_home, run_tool, _set_channel

_ASSETS = Path(__file__).resolve().parent / "assets"


def _load_page() -> str:
    """Assemble the served page from the three asset files (edit those, not a
    Python string). str.replace — never .format — so the JS/CSS braces are safe."""
    tpl = (_ASSETS / "page.html").read_text()
    tpl = tpl.replace("__STYLES__", (_ASSETS / "styles.css").read_text())
    tpl = tpl.replace("__APP_JS__", (_ASSETS / "app.js").read_text())
    return tpl


PAGE = _load_page()


_CT = {".png": "image/png", ".jpg": "image/jpeg", ".mp4": "video/mp4",
       ".json": "application/json", ".md": "text/markdown; charset=utf-8"}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):           # quiet — job log is the story
        pass

    def _send(self, code, body, ctype="application/json"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
        elif isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urlparse(self.path)
        if u.path == "/":
            return self._send(200, PAGE, "text/html; charset=utf-8")
        if u.path == "/api/state":
            return self._send(200, build_state())
        if u.path.startswith("/api/job/"):
            jid = u.path.rsplit("/", 1)[-1]
            j = ctx.find_job(jid)
            if j is not None:
                return self._send(200, {"id": j.id, "label": j.label,
                                        "status": j.status, "log": j.log(),
                                        "error": j.error})
            return self._send(404, {"error": "no such job"})
        if u.path.startswith("/artifact/"):
            return self._serve_artifact(unquote(u.path[len("/artifact/"):]),
                                        versioned="v" in parse_qs(u.query))
        if u.path.startswith("/font/"):
            return self._serve_font(unquote(u.path[len("/font/"):]))
        if u.path.startswith("/castimg/"):
            return self._serve_castimg(unquote(u.path[len("/castimg/"):]),
                                       versioned="v" in parse_qs(u.query))
        if u.path.startswith("/sample/"):
            return self._serve_sample(unquote(u.path[len("/sample/"):]))
        return self._send(404, {"error": "not found"})

    def _serve_castimg(self, rel, versioned=False):
        """A cast avatar / reference image for the home-screen cast editor.
        `rel` is channel-dir-relative (cast/<name>.png, cast/<Name>/…); only
        files under the channel's cast dir are served."""
        try:
            ch = config.channel()
        except Exception:                            # noqa: BLE001
            return self._send(404, {"error": "no channel"})
        p = (ch.dir / rel).resolve()
        if ch.cast_dir.resolve() not in p.parents or not p.exists():
            return self._send(404, {"error": "not found"})
        data = p.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type",
                         {".png": "image/png", ".jpg": "image/jpeg",
                          ".webp": "image/webp"}.get(p.suffix,
                                                     "application/octet-stream"))
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control",
                         "private, max-age=604800, immutable" if versioned
                         else "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _serve_sample(self, name):
        """A synthesized voice-audition clip (cast editor ▶ sample). Bare
        filenames only, out of cast_setup's session temp dir."""
        import cast_setup
        d = cast_setup._SAMPLE_DIR
        if d is None or "/" in name or "\\" in name:
            return self._send(404, {"error": "not found"})
        p = Path(d) / name
        if not p.exists():
            return self._send(404, {"error": "not found"})
        data = p.read_bytes()
        ext = iv._true_audio_ext(p)
        self.send_response(200)
        self.send_header("Content-Type",
                         {".wav": "audio/wav", ".m4a": "audio/mp4",
                          ".mp3": "audio/mpeg"}.get(ext, "audio/mpeg"))
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _serve_font(self, name):
        """Serve a vendored brand font (pipeline/assets/fonts) so the page's
        @font-face can use the same type the rendered cards do. Bare filenames
        only — no path separators."""
        import config
        if "/" in name or "\\" in name or not name.endswith(".ttf"):
            return self._send(404, {"error": "not found"})
        p = config.FONTS / name
        if not p.exists():
            return self._send(404, {"error": "not found"})
        data = p.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "font/ttf")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "max-age=86400")
        self.end_headers()
        self.wfile.write(data)

    def _serve_artifact(self, name, versioned=False):
        base = _cur_folder()
        if base is None:
            return self._send(404, {"error": "no session"})
        p = (base / name).resolve()
        if base.resolve() not in p.parents or not p.exists():
            return self._send(404, {"error": "not found"})
        ctype = _CT.get(p.suffix, "application/octet-stream")
        if p.suffix == ".mp3":                  # bytes may be WAV/M4A
            ext = iv._true_audio_ext(p)
            ctype = {".wav": "audio/wav", ".m4a": "audio/mp4",
                     ".mp3": "audio/mpeg"}.get(ext, "audio/mpeg")
        data = p.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        # a ?v=<mtime> URL is content-addressed (a regen bumps the mtime → a
        # NEW URL), so it caches hard — re-renders reuse the browser cache
        # instead of re-downloading every image (the page-wide flicker)
        self.send_header("Cache-Control",
                         "private, max-age=604800, immutable" if versioned
                         else "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _handle_upload(self, u, length):
        """Receive a RAW file upload for one unit and ingest it. Query:
        kind=voice|keyframe|clip (default voice), index=<seg>, section=<k|blank>.
        voice → a recorded take (MediaRecorder blob) via apply_recording;
        keyframe → an image saved as the canonical key<i>[_k].png;
        clip → a video saved as the canonical clip<i>[_k].mp4. Returns fresh
        state."""
        if ctx.APP is None or ctx.APP.s is None:
            return self._send(400, {"error": "no video session"})
        q = parse_qs(u.query)
        kind = (q.get("kind", ["voice"])[0] or "voice")
        try:
            idx = int(q.get("index", ["0"])[0])
        except ValueError:
            return self._send(400, {"error": "bad index"})
        sraw = (q.get("section", [""])[0] or "").strip()
        sec = int(sraw) if sraw != "" else None
        data = self.rfile.read(length) if length else b""
        if not data:
            return self._send(400, {"error": "empty upload"})
        folder = ctx.APP.s.folder
        if kind == "keyframe":
            tmp = folder / "build"
            tmp.mkdir(exist_ok=True)
            tmp = tmp / f"_kfup{idx}_{sec if sec is not None else 0}.bin"
            tmp.write_bytes(data)
            try:
                res = make_video.import_keyframe(folder, idx, sec, tmp)
            except Exception as e:                # noqa: BLE001
                return self._send(400, {"error": str(e) or e.__class__.__name__})
            finally:
                tmp.unlink(missing_ok=True)
            if not res.get("ok"):
                return self._send(400, {"error": res.get("error", "import failed")})
            return self._send(200, {"ok": True, "result": res,
                                    "state": build_state()})
        if kind == "clip":
            tmp = folder / "build"
            tmp.mkdir(exist_ok=True)
            tmp = tmp / f"_clipup{idx}_{sec if sec is not None else 0}.bin"
            tmp.write_bytes(data)
            try:
                res = make_video.import_clip(folder, idx, sec, tmp)
            except Exception as e:                # noqa: BLE001
                return self._send(400, {"error": str(e) or e.__class__.__name__})
            finally:
                tmp.unlink(missing_ok=True)
            if not res.get("ok"):
                return self._send(400, {"error": res.get("error", "import failed")})
            return self._send(200, {"ok": True, "result": res,
                                    "state": build_state()})
        tmp = config.art(folder, f"seg{idx}"
                         + (f"_s{sec}" if sec is not None else "") + ".rec.webm")
        tmp.write_bytes(data)
        try:
            res = make_video.apply_recording(folder, idx, sec, tmp)
        except Exception as e:                    # noqa: BLE001
            return self._send(400, {"error": str(e) or e.__class__.__name__})
        finally:
            tmp.unlink(missing_ok=True)
        if not res.get("ok"):
            return self._send(400, {"error": res.get("error", "ingest failed")})
        return self._send(200, {"ok": True, "result": res,
                                "state": build_state()})

    def do_POST(self):
        u = urlparse(self.path)
        length = int(self.headers.get("Content-Length", 0))
        # /api/upload carries RAW audio bytes (a MediaRecorder blob), not JSON —
        # read + ingest it as a recorded take for one voice unit
        if u.path == "/api/upload":
            return self._handle_upload(u, length)
        body = self.rfile.read(length) if length else b"{}"
        try:
            p = json.loads(body or b"{}")
        except ValueError:
            return self._send(400, {"error": "bad JSON"})
        if u.path == "/api/home/channel":
            return self._send(200, _set_channel(p.get("name")))
        if u.path == "/api/home/open":
            try:
                return self._send(200, open_session(
                    p.get("channel"), p.get("topic"), p.get("mode", "video"),
                    p.get("template"), p.get("show")))
            except Exception as e:                # noqa: BLE001
                return self._send(400, {"error": str(e)
                                        or e.__class__.__name__})
        if u.path == "/api/home/run":
            try:
                res = run_tool(p.get("tool"), p.get("topic"),
                               p.get("args") or {})
                if isinstance(res, dict):          # an INSTANT tool's result
                    return self._send(200, res)
                j = ctx.find_job(res)
                return self._send(200, {"kind": "job", "jobId": res,
                                        "label": j.label if j else None})
            except Exception as e:                # noqa: BLE001
                return self._send(400, {"error": str(e)
                                        or e.__class__.__name__})
        if u.path == "/api/back":
            return self._send(200, go_home())
        if u.path == "/api/nav":
            if ctx.APP is None:
                return self._send(200, build_state())
            ctx.APP.stage = max(0, min(int(p.get("stage", ctx.APP.stage)),
                                   len(iv.STAGE_ORDER) - 1))
            return self._send(200, build_state())
        if u.path == "/api/quit":
            if ctx.APP is not None:
                ctx.APP.shutdown = True
            threading.Thread(target=self.server.shutdown, daemon=True).start()
            return self._send(200, {"ok": True})
        if u.path == "/api/action":
            try:
                kind, payload = act(p.get("stage"), p.get("action"), p)
                resp = {"kind": kind}
                if kind == "job":
                    resp["jobId"] = payload
                    j = ctx.find_job(payload)
                    if j is not None:      # the banner can name it instantly
                        resp["label"] = j.label
                else:
                    resp["result"] = payload
                    resp["state"] = build_state()
                return self._send(200, resp)
            except Exception as e:                # noqa: BLE001
                return self._send(400, {"error": str(e)
                                        or e.__class__.__name__})
        return self._send(404, {"error": "not found"})


def _free_port() -> int:
    sk = socket.socket()
    sk.bind(("127.0.0.1", 0))
    port = sk.getsockname()[1]
    sk.close()
    return port


def main(args) -> int:
    """Serve the UI. With a topic → straight into the VIDEO session (back-compat
    `make TOPIC --ui`). With no topic → the ctx.HOME launcher (pick channel → folder
    → workflow/capability), with no channel required up front."""
    # Setup problems (provider CLI missing / not logged in) must not block the
    # UI — remember the message and let the page show a banner; generation
    # stages will surface the same error if attempted before setup.
    try:
        make_video.ensure_cli()
    except make_video.StepFailed as e:
        ctx.SETUP["warning"] = str(e)
        print(f"! setup incomplete: {e}", file=sys.stderr)
        print("  starting the UI anyway — finish setup to generate media",
              file=sys.stderr)

    topic = getattr(args, "topic", None)
    if topic:
        ch = config.channel()
        t, in_queue = iv._resolve_topic(topic)
        if not t:
            return 1
        folder = config.topic_dir(t)
        if not (folder / "template.json").exists():
            templates.resolve(getattr(args, "template", None)
                              or ch.default_template, folder)
        tpl = templates.load_pinned(folder)
        s = iv.Session(t, folder, tpl, in_queue)
        credits = kalinga.hf_credits()
        sess = iv._read_json(folder, "session.json")
        if sess is None:
            sess = {"started": datetime.now().isoformat(timespec="seconds"),
                    "credits_start": credits}
            iv._write_json(folder, "session.json", sess)
        s.credits_start = sess.get("credits_start", credits)
        ctx.bind_box("video", ch.name, t, folder, app=App(s))
        if getattr(args, "at", None):
            ctx.APP.stage = iv.STAGE_ORDER.index(args.at)
        banner = f"channel={ch.name}, topic={t}"
    else:
        ctx.set_focus(None)
        if ctx.HOME.get("channel") is None and len(config.available()) == 1:
            ctx.HOME["channel"] = config.available()[0]
        banner = "launcher (pick a channel in the browser)"

    port = _free_port()
    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    url = f"http://127.0.0.1:{port}/"
    print(f"=== kalinga UI — {banner} ===")
    print(f"  open {url}")
    print("  (Ctrl-C here, or Quit/Home in the browser, stops the server)")
    try:
        webbrowser.open(url)
    except Exception:
        pass
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n  stopped — artifacts kept; resume any time")
    httpd.server_close()
    return 0
