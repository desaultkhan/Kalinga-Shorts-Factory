# Contributing

Thanks for helping improve Kalinga!

## Setup

```bash
python3 -m pip install -r requirements.txt pytest
brew install ffmpeg          # or your platform's package manager
```

The creative and generation steps drive the `claude` and `higgsfield` CLIs
(see the README) — but you don't need either to run the checks below.

## Before you open a PR

```bash
./build.sh     # byte-compiles + imports every module (the compile gate)
pytest -q      # offline smoke tests
```

Both must pass; CI runs the same two commands.

## Conventions

- **Python 3.9.** Start every module with `from __future__ import
  annotations`; no runtime `X | Y` unions, no `match`.
- **Keep the seams clean.** New backends/sources/looks go through the
  registries in [`docs/EXTENDING.md`](docs/EXTENDING.md) — avoid if/elif
  chains and channel-specific logic in the engine.
- **Grow the smoke tests.** When you add a capability, add an assertion in
  `tests/test_smoke.py` so it stays safe to refactor.
- **Match the surrounding style** — comment density, naming and idiom.

## Scope

This repo is the focused, open-source **video creation** core. Larger
features (publishing automation, analytics/experiment loops, alternate output
formats) are great as clearly-scoped additions layered on the extension
points, rather than woven into the core pipeline.
