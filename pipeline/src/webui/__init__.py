"""
webui — the browser production UI (`kalinga.py make --ui` / `kalinga.py ui`).

Split out of the former monolithic webui.py into a package:
  context      shared session globals + job runner + artifact helpers
  state        the JSON snapshots the page renders from (build_state, _ss_*)
  actions      the per-stage action handlers + ACTIONS registry
  session      open a run / go home / run a capability tool
  server       the stdlib HTTP handler + main()
  assets/      the frontend as real files (page.html, styles.css, app.js)

Public API: main() (the entry point kalinga.py calls) and HOME (the launcher's
remembered channel, which kalinga.py pre-seeds before calling main()).
"""
from .server import main
from .context import HOME

__all__ = ["main", "HOME"]
