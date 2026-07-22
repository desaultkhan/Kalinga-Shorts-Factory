# Ken Burns — Remotion plugin (optional)

An **optional** animator for the still (non-`animate`) segments of a Kalinga
Short. The pipeline's built-in motion for stills is an ffmpeg `zoompan` pan/zoom
(`make_video._conform`). This plugin replaces it, per still segment, with a
richer **Remotion** (React) animated graphic:

- eased zoom + diagonal pan (no robotic constant-velocity drift),
- a slow light glow drifting across the frame,
- a soft vignette + subtle, moving film grain.

It is a **drop-in upgrade, never a dependency**: OFF by default, and when ON it
silently falls back to the ffmpeg Ken Burns if Node / Remotion / its `npm`
deps are missing or a render fails. The rest of the pipeline (clips, charts,
overlays, captions, music) is untouched — this only changes how a still moves.

## Turn it on

One-time install (only if you want it):

```bash
cd plugins/remotion
npm install
```

Then enable, either per template or via env (env wins):

```yaml
# channels/<name>/templates/<tpl>.yaml
kenburns: remotion        # default is "ffmpeg"
```

```bash
KALINGA_KENBURNS=remotion python3 kalinga.py ship AAPL
KALINGA_KENBURNS=ffmpeg   python3 kalinga.py ship AAPL   # force the built-in
```

Turn it off again by setting `kenburns: ffmpeg` (or `KALINGA_KENBURNS=off`),
or simply by not installing the deps.

## How it's wired

`pipeline/src/kenburns.py` is the Python adapter. For each still segment
`make_video._conform` calls `kenburns.render(...)`, which (when enabled +
available) runs:

```
npx remotion render src/index.ts KenBurns _kb<i>.mp4 \
    --props='{src, durationInFrames, fps, width, height, zoom..., pan..., glow, grain, vignette}' \
    --public-dir=<run folder>
```

`--public-dir` is the run folder, so the composition loads the keyframe via
`staticFile("key<i>.png")`. The rendered clip is fed through the same overlay
graph (charts, answer-text reveals) as everything else, then deleted as build
scratch. On any failure `render` returns `None` and `_conform` uses the
built-in zoompan instead.

Preview a composition in the Remotion studio while iterating:

```bash
cd plugins/remotion && npm run preview
```
