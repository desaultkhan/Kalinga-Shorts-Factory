---
description: Make one viral Short end-to-end in-session (script + Higgsfield generation)
argument-hint: "[TOPIC]"
---

Make one finished Short for topic `$ARGUMENTS` (no argument → next `pending`
row in the channel's `queue.csv`). Run commands from the repo root via
`python3 kalinga.py …`. With one channel you never pass `--channel`; if several
exist, ask the user which one and add `--channel <name>` / `KALINGA_CHANNEL=<name>`.

1. **Research:** `python3 kalinga.py run research <TOPIC>` — the folder
   appears in `channels/<channel>/output/<date>_<TOPIC>/` with `facts.json`
   (the channel's research adapter: `llm` or `manual`).
   If it fails, mark the queue row `failed` and stop.

2. **Write the script yourself** (you are the LLM in the loop — never use
   the dry template). Read first:
   - `channels/<channel>/channel.yaml` — persona, premise, the exact
     segment labels + per-segment guidance, `voice_rules`, `visual_rules`
   - the pinned `template.json` in the topic folder (or the channel's
     default template yaml) — `world`, `target_words`
   - `channels/learnings.md` and `channels/<channel>/learnings.md` (if they
     exist), `channels/<channel>/overrides.json` (live experiment)
   - the folder's `facts.json`

   Then write the folder's `script.json`:
   `{"topic", "verdict" (optional), "mode": "claude-session", "segments": [
   {"label", "text"}]}` — one per channel segment, valid labels from
   channel.yaml, total words inside the template's `target_words`, comma-flow
   phrasing (sentence breaks cost ~0.6s each). The writer owns the spoken
   `text`; the director owns visuals/overlays, so don't hand-author those here.

3. **Generate everything:**

   ```bash
   python3 kalinga.py run keyframes <TOPIC> && python3 kalinga.py run clips <TOPIC> \
     && python3 kalinga.py run assemble <TOPIC>
   ```

   (Or just `python3 kalinga.py ship <TOPIC>` to run the whole tail.) These do
   TTS, keyframes, clips, assembly, SEO baseline, and the virality score, all
   resumable — rerun if anything fails; finished artifacts are cached. If the
   `higgsfield` CLI is unauthenticated, tell the user to run
   `higgsfield auth login` and stop.

4. **Polish + wrap:** rewrite the folder's `seo.md`/`seo.json` per the
   PROMPT spec in `seo.py` plus the channel.yaml `seo:` rules; read
   `virality.md` — if the hook scores weak, rewrite segment 0 in
   `script.json`, run `python3 kalinga.py redo hook <TOPIC>` (clears
   seg0/key0/clip0/short/virality), and rerun `make_video.py` once (cached
   artifacts make this cheap). Then mark the queue row `done`
   (`channels/<channel>/queue.csv`) and tell the user: file path, virality
   score, and what to paste from `seo.md`.
