"""`python3 -m webui [TOPIC]` — the dev entry the old webui.py exposed."""
import argparse
import sys

import config
import interactive as iv

from . import main

if __name__ == "__main__":
    ap = argparse.ArgumentParser(prog="webui")
    ap.add_argument("topic", nargs="?", default=None)
    ap.add_argument("--template", default=None)
    ap.add_argument("--experiment", default=None)
    ap.add_argument("--at", default=None, choices=iv.STAGE_ORDER)
    ap.add_argument("--channel", default=None)
    a = ap.parse_args()
    config.set_channel(a.channel)
    sys.exit(main(a))
