"""
Karaoke captions — builds an ASS subtitle file from word timings in the audio
manifest. Words show in groups of 3; the spoken word is highlighted gold and
slightly enlarged (CapCut style). Burned into video by video.py via libass.
"""
import json
import os
from pathlib import Path

SEG_PAD = 0.25  # must match video.SEG_PAD

# Words shown per caption phrase. Few words (the old 3) means a NEW caption every
# ~1s — the text jumps constantly. A larger phrase holds more words on screen so
# the karaoke highlight travels across a STABLE block and the caption changes far
# less often. CapCut/viral style is ~5-6. Override with KALINGA_CAPTION_WORDS.
try:
    WORDS_PER_CAPTION = max(2, int(os.environ.get("KALINGA_CAPTION_WORDS", "6")))
except ValueError:
    WORDS_PER_CAPTION = 6
# wrap width scales with the phrase so a 6-word line breaks to ~2 tidy lines
# instead of running off-screen (held captions are \pos'd; WrapStyle 2 = no auto)
CAPTION_WRAP = 30

HEADER = """[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920
WrapStyle: 2

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Karaoke,Fredoka One,58,{primary},{primary},{outline},&H00000000,0,0,0,0,100,100,0,0,1,5,{shadow},2,60,60,200,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""

# paper: dark ink + paper-white outline (local sketch renderer)
# viral: white + black outline/shadow, gold active word (cinematic footage)
STYLES = {
    "paper": {"primary": "&H00262A2C", "outline": "&H00E9F4F8", "shadow": 0,
              "active": r"&H303CCA&"},   # BGR marker red
    "viral": {"primary": "&H00FFFFFF", "outline": "&H00141414", "shadow": 2,
              "active": r"&H4AD2FF&"},   # BGR gold (255,210,74)
}


def ts(t: float) -> str:
    h, rem = divmod(max(t, 0), 3600)
    m, s = divmod(rem, 60)
    return f"{int(h)}:{int(m):02d}:{s:05.2f}"


def _wrap(text: str, max_chars: int = CAPTION_WRAP) -> str:
    """Hard-wrap a caption into short lines with ASS `\\N` breaks so it always
    fits the screen — the header is WrapStyle 2 (NO auto-wrap), and a \\pos'd
    line never wraps on margins, so a long held caption would otherwise run off
    both edges. Greedy by words; a single over-long word is left on its own line."""
    words, lines, cur = text.split(), [], ""
    for w in words:
        if cur and len(cur) + 1 + len(w) > max_chars:
            lines.append(cur)
            cur = w
        else:
            cur = f"{cur} {w}".strip()
    if cur:
        lines.append(cur)
    return r"\N".join(lines)


def _chunk_tokens(text: str, n: int = WORDS_PER_CAPTION):
    """Split a held section line into <=n-word groups (breaking early at sentence
    enders), so a long section is PACED as a few short captions instead of one
    wall of text. Mirrors group_words but for plain tokens."""
    groups, cur = [], []
    for w in text.split():
        cur.append(w)
        if len(cur) >= n or w[-1:] in ".!?,":
            groups.append(cur)
            cur = []
    if cur:
        if len(cur) == 1 and groups:
            groups[-1].extend(cur)
        else:
            groups.append(cur)
    return [" ".join(g) for g in groups]


def _group_breaks(grp, max_chars: int = CAPTION_WRAP) -> set:
    """Indices in a karaoke group where a NEW LINE starts (greedy by chars) —
    computed ONCE per group from the plain words, so the \\N breaks stay in the
    same place while the gold highlight moves (no re-flow), and a 6-word phrase
    at Fontsize 58 never runs past the 1080px frame (the events are \\pos'd and
    WrapStyle 2 never auto-wraps them)."""
    breaks, cur = set(), ""
    for j, g in enumerate(grp):
        w = g["word"].strip()
        if cur and len(cur) + 1 + len(w) > max_chars:
            breaks.add(j)
            cur = w
        else:
            cur = f"{cur} {w}".strip()
    return breaks


def group_words(words, n=WORDS_PER_CAPTION):
    """Split into groups of <=n words. Break early only at sentence enders;
    merge trailing singletons into the previous group so captions never
    flash a lone word."""
    groups, cur = [], []
    for w in words:
        cur.append(w)
        if len(cur) >= n or w["word"].rstrip()[-1:] in ".!?":
            groups.append(cur)
            cur = []
    if cur:
        if len(cur) == 1 and groups:
            groups[-1].extend(cur)
        else:
            groups.append(cur)
    return groups


def build_ass(manifest_path: str, out_path: str, style: str = "paper") -> Path:
    st = STYLES[style]
    m = json.loads(Path(manifest_path).read_text())
    lines = [HEADER.format(primary=st["primary"], outline=st["outline"],
                           shadow=st["shadow"])]
    gold = st["active"]
    # Pin every caption to ONE fixed spot (\an2 bottom-centre, \pos, \q2 no
    # auto-wrap — long phrases carry explicit \N breaks from _group_breaks and
    # grow UPWARD from the anchor) so it never shifts vertically. With a fixed
    # position, two captions that overlap in TIME would draw on top of each
    # other — so flatten every word-event with an absolute start and TILE them
    # (each ends exactly when the next begins), guaranteeing no two are ever on
    # screen together. x=PlayResX/2, y=PlayResY-MarginV (matches MarginV 200).
    pin = r"{\q2\an2\pos(540,1720)}"
    # a HELD block (a section with no word timings — Inworld/recorded without
    # whisper) has no per-word timing, so it's PACED into a few short captions
    # (_chunk_tokens) and each is hard-wrapped to fit (_wrap), since WrapStyle 2
    # never auto-wraps a \pos'd line. \q2 dropped (we wrap manually with \N).
    pin_wrap = r"{\an2\pos(540,1700)}"
    events = []          # (t0_abs, word_end_abs, rendered_text, wrap)
    seg_start = 0.0
    for seg in m["segments"]:
        dur = seg.get("duration", 0.0)
        words = seg.get("words")
        if words:
            for grp in group_words(words):
                breaks = _group_breaks(grp)
                for i, w in enumerate(grp):
                    txt = ""
                    for j, g in enumerate(grp):
                        word = g["word"].strip()
                        # active word highlighted by COLOUR ONLY (no scale → no
                        # horizontal re-flow)
                        tok = ((r"{\c" + gold + "}" + word + r"{\r}")
                               if j == i else word)
                        txt += ("" if not txt
                                else r"\N" if j in breaks else " ") + tok
                    events.append((seg_start + w["start"], seg_start + w["end"],
                                   txt, False))
        elif (seg.get("caption") or "").strip():
            # held section text (no per-word timing): PACE it into a few short,
            # hard-wrapped captions across the section so it reads like the
            # karaoke ones and never overflows the screen as one long line
            txt = " ".join(seg["caption"].split())
            chunks = _chunk_tokens(txt) or [txt]
            span = max(dur - 0.1, 0.3)
            per = span / len(chunks)
            for ci, ch in enumerate(chunks):
                cs = seg_start + ci * per
                ce = seg_start + min((ci + 1) * per, span)
                events.append((cs, ce, _wrap(ch), True))
        seg_start += dur + SEG_PAD
    events.sort(key=lambda e: e[0])
    HOLD = 0.6           # max a caption lingers past its word (else a gap)
    for k, (t0, wend, text, wrap) in enumerate(events):
        t1 = wend + (0.0 if wrap else HOLD)
        if k + 1 < len(events):
            t1 = min(t1, events[k + 1][0])    # never overlap the NEXT caption
        t1 = max(t1, t0 + 0.05)               # but always show for a moment
        lines.append(f"Dialogue: 0,{ts(t0)},{ts(t1)},Karaoke,,0,0,0,,"
                     + (pin_wrap if wrap else pin) + text)
    Path(out_path).write_text("\n".join(lines) + "\n")
    return Path(out_path)


if __name__ == "__main__":
    import sys
    build_ass(sys.argv[1], sys.argv[2])
