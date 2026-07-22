from __future__ import annotations
"""
brand.py — the channel BRAND-KIT workflow.

A channel's identity as real assets, designed once and reused everywhere:

  brand/brand.json    the design spec (creative.design_brand, $0 LLM)
  brand/mark.png      the AI logo SYMBOL (image-only, no text)
  brand/icon.png      1:1 profile icon — the mark on a brand tile
  brand/logo.png      the full lockup — mark + wordmark (PIL text, crisp)
  brand/watermark.png the corner watermark the pipeline already uses
                      (transparent wordmark) → channels/<name>/brand/watermark.png
  brand/banner.png    16:9 channel banner (AI hero bg + wordmark + tagline)
  brand/background.png a reusable 9:16 backdrop (AI, image-only)

Same split as the rest of Kalinga: the IMAGE model draws only WORDLESS marks /
backgrounds; every word is PIL-composited (via taste.py) so it stays sharp and
legible. AI generations are ~2 credits each (mark + banner + background ≈ 6);
the icon / logo / watermark are pure PIL ($0). Degrades gracefully — a missing
LLM backend or image model just skips the AI parts.

CLI: kalinga.py brand [--channel X] [--auto] [--regen] [--only mark,logo,…]
"""
import json
import sys
from pathlib import Path

import config
import make_video
import taste

# canonical asset sizes
ICON = 1080                              # 1:1 profile
BANNER_W, BANNER_H = 2560, 1440          # YouTube banner
BG_W, BG_H = 1080, 1920                  # reusable 9:16 backdrop
ASSETS = ("mark", "icon", "logo", "watermark", "banner", "background")


def _bdir() -> Path:
    d = config.channel().dir / "brand"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _spec() -> dict:
    p = _bdir() / "brand.json"
    try:
        return json.loads(p.read_text()) if p.exists() else {}
    except (OSError, ValueError):
        return {}


def _pal(spec: dict) -> dict:
    """Brand palette with a legibility guarantee (taste.ensure_contrast) so the
    wordmark/tagline are always readable on the tile/banner."""
    p = dict(spec.get("palette") or {})
    bg = taste.valid_hex(p.get("bg")) or "#0d1020"
    ink = taste.valid_hex(p.get("ink")) or "#f4f6ff"
    accent = taste.valid_hex(p.get("accent")) or "#36d0e6"
    accent2 = taste.valid_hex(p.get("accent2")) or accent
    return {
        "bg": bg,
        "ink": taste.ensure_contrast(ink, bg, 5.0),
        "accent": taste.ensure_contrast(accent, bg, 3.2),
        "accent2": accent2,
    }


def _font(name, size):
    return taste.font(name or "FredokaOne-Regular.ttf", size)


def _fit_font(draw, text: str, name: str, maxw: int, start: int, floor: int = 40):
    """Largest font (≤ start, ≥ floor) whose `text` fits within `maxw`."""
    size = start
    while size > floor:
        f = _font(name, size)
        if draw.textlength(text, font=f) <= maxw:
            return f
        size -= 6
    return _font(name, floor)


# ---- AI assets (image-only) ------------------------------------------------
def gen_mark(spec: dict, tpl: dict, regen: bool = False) -> Path:
    """The logo SYMBOL → brand/mark.png (1:1, image only). ~2 credits."""
    out = _bdir() / "mark.png"
    if out.exists() and not regen:
        print("  brand/mark.png (cached)")
        return out
    if out.exists():
        make_video.archive(out.parent, out.name)
    logo = spec.get("logo") or {}
    pal = _pal(spec)
    prompt = (
        f"{logo.get('mark_concept', spec.get('name', 'a clean abstract mark'))}. "
        f"{logo.get('style', 'flat vector')}. A single ICONIC LOGO SYMBOL, bold "
        f"and simple, instantly readable at small size, centred with generous "
        f"margin on a plain flat {pal['bg']} background, brand colours "
        f"{pal['accent']} and {pal['accent2']}. ABSOLUTELY NO text, letters, "
        "numbers, words or lettering anywhere — symbol only, vector-clean, "
        "high contrast, no photographic detail.")
    make_video.generate(
        tpl.get("image_model", config.DEFAULT_IMAGE_MODEL), prompt, out,
        (".png", ".jpg", ".webp"), ["--aspect_ratio", "1:1"], "brand mark",
        alt_prompts=[f"{prompt} {make_video.SAFE_TAIL}"],
        validate=make_video._healthy_image)
    return out if out.exists() else None


def gen_banner_bg(spec: dict, tpl: dict, regen: bool = False) -> Path:
    out = _bdir() / "_banner_bg.png"
    if out.exists() and not regen:
        return out
    if out.exists():
        make_video.archive(out.parent, out.name)
    pal = _pal(spec)
    concept = (spec.get("banner") or {}).get("concept") or \
        f"an atmospheric on-brand hero backdrop for {spec.get('name')}"
    prompt = (f"{concept}. A premium wide 16:9 channel-banner BACKGROUND in the "
              f"palette {pal['bg']}, {pal['accent']}, {pal['accent2']} — "
              "atmospheric, depth, with calm NEGATIVE SPACE across the centre "
              "for a title to sit. ABSOLUTELY NO text, letters, numbers, logos "
              "or watermarks anywhere — image only.")
    make_video.generate(
        tpl.get("image_model", config.DEFAULT_IMAGE_MODEL), prompt, out,
        (".png", ".jpg", ".webp"), ["--aspect_ratio", "16:9"], "brand banner bg",
        alt_prompts=[f"{prompt} {make_video.SAFE_TAIL}"],
        validate=make_video._healthy_image)
    return out if out.exists() else None


def gen_background(spec: dict, tpl: dict, regen: bool = False) -> Path:
    out = _bdir() / "background.png"
    if out.exists() and not regen:
        print("  brand/background.png (cached)")
        return out
    if out.exists():
        make_video.archive(out.parent, out.name)
    pal = _pal(spec)
    concept = (spec.get("background") or {}).get("concept") or \
        f"a calm reusable backdrop for {spec.get('name')}"
    prompt = (f"{concept}. A reusable vertical 9:16 BACKDROP in the brand "
              f"palette {pal['bg']}, {pal['accent']} — subtle texture/gradient/"
              "motif, calm enough to sit text on top. ABSOLUTELY NO text, "
              "letters, numbers, logos or watermarks — image only.")
    make_video.generate(
        tpl.get("image_model", config.DEFAULT_IMAGE_MODEL), prompt, out,
        (".png", ".jpg", ".webp"), ["--aspect_ratio", "9:16"], "brand background",
        alt_prompts=[f"{prompt} {make_video.SAFE_TAIL}"],
        validate=make_video._healthy_image)
    return out if out.exists() else None


# ---- PIL composites --------------------------------------------------------
def _cover(img, w, h):
    from PIL import Image
    scale = max(w / img.width, h / img.height)
    img = img.resize((int(img.width * scale) + 1, int(img.height * scale) + 1))
    ox, oy = (img.width - w) // 2, (img.height - h) // 2
    return img.crop((ox, oy, ox + w, oy + h))


def _rrect(d, box, r, **kw):
    try:
        d.rounded_rectangle(box, radius=r, **kw)
    except (AttributeError, TypeError):
        d.rectangle(box, **kw)


def build_icon(spec: dict) -> Path:
    """1:1 profile icon — the mark on a rounded brand tile."""
    from PIL import Image, ImageDraw
    pal = _pal(spec)
    out = _bdir() / "icon.png"
    img = Image.new("RGBA", (ICON, ICON), taste.hex_rgb(pal["bg"]) + (255,))
    img = taste.grain(taste.mesh_bg((ICON, ICON), pal["bg"],
                                    taste.to_hex(taste.darken(pal["bg"], 0.2)),
                                    pal["accent"]), alpha=12)
    mark = _bdir() / "mark.png"
    if mark.exists():
        m = _cover(Image.open(str(mark)).convert("RGBA"), int(ICON * 0.72),
                   int(ICON * 0.72))
        img.alpha_composite(m, ((ICON - m.width) // 2, (ICON - m.height) // 2))
    img.convert("RGB").save(str(out), quality=95)
    print(f"  brand/icon.png")
    return out


def build_logo(spec: dict) -> Path:
    """Full lockup — the mark badge + the wordmark (crisp PIL text)."""
    from PIL import Image, ImageDraw
    pal = _pal(spec)
    wm = spec.get("wordmark") or {}
    text = wm.get("text") or spec.get("name") or config.channel().title
    stacked = (wm.get("lockup") or "stacked") != "horizontal"
    W, H = (1200, 1500) if stacked else (1800, 900)
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    mark = _bdir() / "mark.png"
    badge = int(min(W, H) * (0.5 if stacked else 0.7))
    ty = int(H * 0.30)                    # default text y (when there's no mark)
    if mark.exists():
        m = _cover(Image.open(str(mark)).convert("RGBA"), badge, badge)
        # rounded-tile mask the mark so it reads as a logo badge
        tile = Image.new("RGBA", (badge, badge), taste.hex_rgb(pal["bg"]) + (255,))
        tile.alpha_composite(m)
        mask = Image.new("L", (badge, badge), 0)
        _rrect(ImageDraw.Draw(mask), [0, 0, badge, badge], int(badge * 0.22),
               fill=255)
        if stacked:
            img.paste(tile, ((W - badge) // 2, int(H * 0.08)), mask)
            ty = int(H * 0.08) + badge + 60
        else:
            img.paste(tile, (40, (H - badge) // 2), mask)
    maxw = (W - 80) if stacked else (W - badge - 140)
    f = _fit_font(d, text, wm.get("font"), maxw, 150 if stacked else 130, 48)
    tw = d.textlength(text, font=f)
    if stacked:
        d.text(((W - tw) / 2, ty), text, font=f, fill=taste.hex_rgb(pal["ink"]))
    else:
        d.text((40 + badge + 60, (H - f.size) / 2 - 20), text, font=f,
               fill=taste.hex_rgb(pal["ink"]))
    out = _bdir() / "logo.png"
    img.save(str(out))
    print(f"  brand/logo.png")
    return out


def build_watermark(spec: dict) -> Path:
    """The corner watermark the pipeline composites — the logo ICON (a rounded
    badge of the mark) with the channel NAME in lowercase BELOW it, crisp and
    TRANSPARENT. Lands at brand/watermark.png, the path
    make_video._watermark_compose prefers."""
    from PIL import Image, ImageDraw
    pal = _pal(spec)
    # the channel ALIAS (the slug, e.g. "daily-science") — owner prefers
    # the handle-style alias over the capitalised display name
    name = config.channel().name
    f = _font((spec.get("wordmark") or {}).get("font"), 60)
    tmp = ImageDraw.Draw(Image.new("RGBA", (10, 10)))
    tw = tmp.textlength(name, font=f)
    asc, desc = f.getmetrics()
    name_h = asc + desc
    icon_sz, gap, pad = 200, 26, 16
    mark = _bdir() / "mark.png"
    has_icon = mark.exists()
    W = int(max(icon_sz if has_icon else 0, tw) + pad * 2)
    H = int((icon_sz + gap if has_icon else 0) + name_h + pad * 2)
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    y = pad
    if has_icon:
        # the icon as a clean ROUNDED BADGE (the mark is on a brand tile already)
        m = _cover(Image.open(str(mark)).convert("RGBA"), icon_sz, icon_sz)
        mask = Image.new("L", (icon_sz, icon_sz), 0)
        _rrect(ImageDraw.Draw(mask), [0, 0, icon_sz, icon_sz],
               int(icon_sz * 0.24), fill=255)
        img.paste(m, ((W - icon_sz) // 2, y), mask)
        y += icon_sz + gap
    d = ImageDraw.Draw(img)
    d.text(((W - tw) / 2, y), name, font=f,
           fill=taste.hex_rgb(pal["ink"]) + (255,))
    out = _bdir() / "watermark.png"
    img.save(str(out))
    print("  brand/watermark.png (icon + name — the pipeline corner watermark)")
    return out


def build_banner(spec: dict, tpl: dict, regen: bool = False) -> Path:
    """16:9 channel banner — AI hero bg + wordmark + tagline in the safe area."""
    from PIL import Image, ImageDraw
    pal = _pal(spec)
    bg_p = gen_banner_bg(spec, tpl, regen=regen)
    if bg_p and bg_p.exists():
        img = _cover(Image.open(str(bg_p)).convert("RGBA"), BANNER_W, BANNER_H)
    else:
        img = taste.mesh_bg((BANNER_W, BANNER_H), pal["bg"],
                            taste.to_hex(taste.darken(pal["bg"], 0.2)),
                            pal["accent"])
    # centre scrim so the wordmark always reads
    sc = Image.new("RGBA", (BANNER_W, BANNER_H), (0, 0, 0, 0))
    ImageDraw.Draw(sc).ellipse(
        [BANNER_W * 0.18, BANNER_H * 0.18, BANNER_W * 0.82, BANNER_H * 0.82],
        fill=taste.hex_rgb(taste.darken(pal["bg"], 0.1)) + (150,))
    try:
        from PIL import ImageFilter
        sc = sc.filter(ImageFilter.GaussianBlur(120))
    except Exception:        # noqa: BLE001
        pass
    img = Image.alpha_composite(img, sc)
    d = ImageDraw.Draw(img)
    text = (spec.get("wordmark") or {}).get("text") or config.channel().title
    tag = spec.get("tagline") or ""
    cx, cy = BANNER_W // 2, BANNER_H // 2
    hf = _fit_font(d, text, (spec.get("wordmark") or {}).get("font"),
                   int(BANNER_W * 0.7), 200, 90)
    tw = d.textlength(text, font=hf)
    d.text((cx - tw / 2, cy - 150), text, font=hf, fill=taste.hex_rgb(pal["ink"]),
           stroke_width=4, stroke_fill=(0, 0, 0, 180))
    if tag:
        tf = _font("FredokaOne-Regular.ttf", 78)
        tgw = d.textlength(tag.upper(), font=tf)
        d.text((cx - tgw / 2, cy + 90), tag.upper(), font=tf,
               fill=taste.hex_rgb(pal["accent"]))
    out = _bdir() / "banner.png"
    img.convert("RGB").save(str(out), quality=95)
    print(f"  brand/banner.png")
    return out


# ---- orchestration ---------------------------------------------------------
def _credits_note(only) -> int:
    ai = sum(1 for a in ("mark", "banner", "background") if a in only)
    return ai * 2


def run(only=None, regen: bool = False, auto: bool = False,
        notes: str = "") -> int:
    """Design the spec (if needed) then render the selected assets ($0 LLM +
    ~2 cr per AI asset). `only` = subset of ASSETS; default all."""
    import creative
    ch = config.channel()
    only = list(only) if only else list(ASSETS)
    spec = creative.design_brand(notes=notes, regen=regen)
    if not spec:
        print("  ! no brand spec (need an LLM backend to design one, or drop a "
              "brand/brand.json)", file=sys.stderr)
        return 1
    tpl = templates_load(ch)
    print(f"\n  brand: {spec.get('name')} — \"{spec.get('tagline')}\"")
    pal = _pal(spec)
    print(f"  palette: bg {pal['bg']} · ink {pal['ink']} · accent {pal['accent']}")
    if not auto:
        cost = _credits_note(only)
        ans = input(f"  render {', '.join(only)} (~{cost} credits)? "
                    "[Y]es / [e]dit spec / [q]uit: ").strip().lower()
        if ans in ("q", "quit"):
            return 0
        if ans in ("e", "edit"):
            note = _multiline("  describe the changes (empty line to send):")
            if note:
                spec = creative.design_brand(notes=note, regen=True)
    # mark first (icon/logo depend on it)
    if "mark" in only:
        gen_mark(spec, tpl, regen=regen)
    if "icon" in only:
        build_icon(spec)
    if "logo" in only:
        build_logo(spec)
    if "watermark" in only:
        build_watermark(spec)
    if "background" in only:
        gen_background(spec, tpl, regen=regen)
    if "banner" in only:
        build_banner(spec, tpl, regen=regen)
    print(f"\n  ✓ brand kit → {ch.dir / 'brand'}/")
    return 0


def templates_load(ch):
    import templates
    return templates.load(ch.default_template)


def _multiline(prompt: str) -> str:
    print(prompt)
    lines = []
    while True:
        try:
            ln = input()
        except EOFError:
            break
        if not ln.strip():
            break
        lines.append(ln)
    return "\n".join(lines)


def main(args) -> int:
    only = None
    if getattr(args, "only", None):
        only = [a.strip() for a in args.only.split(",") if a.strip() in ASSETS]
    return run(only=only, regen=getattr(args, "regen", False),
               auto=getattr(args, "auto", False))
