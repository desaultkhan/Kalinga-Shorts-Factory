#!/usr/bin/env python3
"""Launcher — run the kalinga CLI from the repo root.

The real CLI lives in pipeline/src/kalinga.py; this puts pipeline/src on
sys.path (ahead of this file's directory) and imports it as the module
``kalinga`` so every later ``import kalinga`` (e.g. from interactive.py)
resolves to the same module instance.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "pipeline", "src"))
import kalinga  # noqa: E402

sys.exit(kalinga.main())
