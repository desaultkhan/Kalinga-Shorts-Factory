from __future__ import annotations
"""
taste.py — a small library of aesthetic design PRIMITIVES for composed cards.

Card composers (the thumbnail, the brand kit) draw every word with PIL so it
stays crisp; the problem was that "crisp" was also FLAT — a near-black gradient
with centred text and dead space. This module is the art-direction layer that
makes a card look designed instead of defaulted:

  • mesh_bg      rich multi-stop background with accent GLOW BLOOMS + vignette
                 (never a flat near-black)
  • grain        fine film grain for texture (overlay blend, no wash-out)
  • duotone      cohesive darken/tint pass over an AI photo so text pops
  • panel        rounded card with a soft drop shadow (text legibility block)
  • pill         a rounded kicker/label CHIP
  • num_chip     a numbered token for list rows (01 / 02 / 03)
  • ghost        an oversized faint glyph behind a hero stat (depth + fills space)
  • rule         an accent divider with an end dot
  • chrome       the uniform editorial frame: hairline border + corner ticks,
                 a slide index, a progress rail and a brand footer

Pure Pillow, no new deps. Every effect degrades gracefully if a filter or a
font is unavailable (it simply skips), and every colour argument accepts a hex
string OR an (r,g,b) tuple.
"""
import config


# ---- colour ----------------------------------------------------------------
def hex_rgb(s) -> tuple:
    if isinstance(s, (tuple, list)):
        return tuple(int(x) for x in s[:3])
    s = str(s).lstrip("#")
    if len(s) == 3:
        s = "".join(c * 2 for c in s)
    try:
        return (int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16))
    except (ValueError, IndexError):
        return (16, 24, 20)


def mix(a, b, t: float) -> tuple:
    a, b = hex_rgb(a), hex_rgb(b)
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))


def lighten(c, t: float) -> tuple:
    return mix(c, (255, 255, 255), t)


def darken(c, t: float) -> tuple:
    return mix(c, (0, 0, 0), t)


def saturate(c, f: float) -> tuple:
    """Scale a colour's HSV saturation by `f` (jewel-tone instead of grey)."""
    import colorsys
    r, g, b = hex_rgb(c)
    h, s, v = colorsys.rgb_to_hsv(r / 255.0, g / 255.0, b / 255.0)
    r2, g2, b2 = colorsys.hsv_to_rgb(h, max(0.0, min(1.0, s * f)), v)
    return (int(r2 * 255), int(g2 * 255), int(b2 * 255))


def _rel_lum(c) -> float:
    """WCAG relative luminance of an (r,g,b)/hex colour."""
    def ch(v):
        v = v / 255.0
        return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4
    r, g, b = hex_rgb(c)
    return 0.2126 * ch(r) + 0.7152 * ch(g) + 0.0722 * ch(b)


def contrast(c1, c2) -> float:
    """WCAG contrast ratio between two colours (1.0 … 21.0). The legibility QC:
    body text wants ≥ ~3.5, a quieter accent/subtext ≥ ~2.2."""
    a, b = _rel_lum(c1), _rel_lum(c2)
    hi, lo = max(a, b), min(a, b)
    return (hi + 0.05) / (lo + 0.05)


def to_hex(c) -> str:
    r, g, b = hex_rgb(c)
    return "#%02x%02x%02x" % (r, g, b)


def ensure_contrast(color, bg, floor: float, steps: int = 14) -> str:
    """Return `color` nudged just enough to clear a contrast `floor` against
    `bg` — blended toward white or black (whichever the bg needs), stopping the
    moment it's legible so the hue is preserved as much as possible. The
    READABILITY guarantee: whatever palette the director picks, the text that
    lands on the card is comfortable to read. Returns a hex string."""
    if contrast(color, bg) >= floor:
        return to_hex(color)
    target = (255, 255, 255) if _rel_lum(bg) < 0.5 else (0, 0, 0)
    c = hex_rgb(color)
    for i in range(1, steps + 1):
        cand = mix(c, target, i / steps)
        if contrast(cand, bg) >= floor:
            return to_hex(cand)
    return to_hex(target)


def valid_hex(s):
    """Normalise to '#rrggbb' if `s` is a parseable hex colour, else None."""
    if not isinstance(s, str):
        return None
    t = s.strip().lstrip("#")
    if len(t) == 3:
        t = "".join(c * 2 for c in t)
    if len(t) != 6:
        return None
    try:
        int(t, 16)
    except ValueError:
        return None
    return "#" + t.lower()


# ---- fonts -----------------------------------------------------------------
def font(name: str, size: int):
    from PIL import ImageFont
    try:
        return ImageFont.truetype(str(config.FONTS / name), size)
    except OSError:
        try:
            return ImageFont.truetype(
                str(config.FONTS / "FredokaOne-Regular.ttf"), size)
        except OSError:
            return ImageFont.load_default()


def _rrect(draw, box, radius, **kw) -> None:
    """rounded_rectangle with a hard-rectangle fallback for old Pillow."""
    try:
        draw.rounded_rectangle(box, radius=radius, **kw)
    except (AttributeError, TypeError):
        draw.rectangle(box, **kw)


# ---- backgrounds -----------------------------------------------------------
def _linear(size, c1, c2):
    """Top→bottom linear gradient as an RGBA image."""
    from PIL import Image, ImageDraw
    W, H = size
    c1, c2 = hex_rgb(c1), hex_rgb(c2)
    img = Image.new("RGBA", size)
    d = ImageDraw.Draw(img)
    for y in range(H):
        t = y / max(1, H - 1)
        c = tuple(int(c1[i] + (c2[i] - c1[i]) * t) for i in range(3))
        d.line([(0, y), (W, y)], fill=c + (255,))
    return img


def radial_glow(size, center, radius, color, max_alpha: int = 110):
    """A soft circular bloom — a blurred filled ellipse on a clear layer."""
    from PIL import Image, ImageDraw, ImageFilter
    g = Image.new("RGBA", size, (0, 0, 0, 0))
    d = ImageDraw.Draw(g)
    cx, cy = center
    d.ellipse([cx - radius, cy - radius, cx + radius, cy + radius],
              fill=hex_rgb(color) + (max_alpha,))
    try:
        g = g.filter(ImageFilter.GaussianBlur(radius * 0.5))
    except Exception:                                # noqa: BLE001
        pass
    return g


def vignette(size, strength: int = 150):
    """Darken the top and (more) the bottom edge for depth + footer legibility."""
    from PIL import Image, ImageDraw
    W, H = size
    v = Image.new("RGBA", size, (0, 0, 0, 0))
    d = ImageDraw.Draw(v)
    for y in range(H):
        bottom = max(0.0, (y - H * 0.66) / (H * 0.34))
        top = max(0.0, (H * 0.14 - y) / (H * 0.14))
        a = int(strength * max(bottom, top * 0.6))
        if a:
            d.line([(0, y), (W, y)], fill=(0, 0, 0, min(255, a)))
    return v


def mesh_bg(size, base, base2, accent, accent2=None, blooms=None):
    """A premium multi-stop background: a rich diagonal base wash + accent
    GLOW BLOOMS + a gentle vignette. The fix for the flat near-black card.
    Tuned 2026-07-03 (owner: cards looked dull and worn out): the base is no
    longer pre-darkened a quarter, the blooms glow noticeably brighter, and
    the vignette eases off — depth from LIGHT, not from mud."""
    from PIL import Image
    W, H = size
    accent2 = accent2 or base2
    # jewel-tone canvas: keep the hue SATURATED so the ramp reads as a rich
    # colour, not grey haze (2026-07-04, owner: "still dusky and dirty")
    top = saturate(lighten(base2, 0.10), 1.35)
    bot = saturate(darken(base, 0.12), 1.30)
    img = _linear(size, top, bot)
    if blooms is None:
        # depth comes from LUMINOSITY blooms in the canvas's OWN hue — a big
        # complementary accent wash (amber over indigo) mixes to brown mud.
        # The accent appears only as one small corner kiss of light.
        blooms = [
            ((W * 0.20, H * 0.10), W * 0.62, saturate(lighten(base2, 0.42), 1.3), 110),
            ((W * 0.92, H * 0.86), W * 0.50, saturate(lighten(base2, 0.22), 1.3), 70),
            ((W * 0.06, H * 0.04), W * 0.24, lighten(accent, 0.12), 80),
        ]
    for (c, r, col, a) in blooms:
        img = Image.alpha_composite(img, radial_glow(size, c, r, col, a))
    img = Image.alpha_composite(img, vignette(size, 90))
    return img


def paper_bg(size, base, base2=None):
    """A QUIET textured card — the easiest background to read on (owner
    2026-07-04): a barely-there tonal ramp in the base hue + a coarse paper
    mottle + fine grain + a soft vignette. No blooms, no gradient drama —
    just calm, tactile paper for text-dense slides."""
    from PIL import Image, ImageFilter
    W, H = size
    base2 = base2 or base
    img = _linear(size, saturate(lighten(base, 0.07), 1.15),
                  saturate(darken(base2, 0.05), 1.15))
    # coarse mottle: low-res noise blown up smooth = soft paper blotches
    try:
        mottle = Image.effect_noise((max(2, W // 9), max(2, H // 9)), 34)
        mottle = mottle.resize(size).filter(ImageFilter.GaussianBlur(3))
        mottle = mottle.convert("RGB")
        from PIL import ImageChops
        base_rgb = img.convert("RGB")
        img = Image.blend(base_rgb, ImageChops.overlay(base_rgb, mottle),
                          0.10).convert("RGBA")
    except Exception:                                # noqa: BLE001
        pass
    img = grain(img, amount=8, alpha=7)
    return Image.alpha_composite(img, vignette(size, 60))


def grain(img, amount: int = 11, alpha: int = 16):
    """Fine film grain via an OVERLAY blend — texture without washing the darks."""
    from PIL import Image, ImageChops
    try:
        n = Image.effect_noise(img.size, amount).convert("RGB")
    except Exception:                                # noqa: BLE001
        return img
    base = img.convert("RGB")
    blended = ImageChops.overlay(base, n)
    out = Image.blend(base, blended, max(0.0, min(alpha / 100.0, 1.0)))
    return out.convert("RGBA")


def duotone(img, shadow, tint_alpha: int = 0):
    """Cohesion pass over an AI photo: a bottom-weighted darken so composited
    text pops, plus an optional faint brand tint. Keeps the photo legible."""
    from PIL import Image, ImageDraw
    W, H = img.size
    sh = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(sh)
    sc = hex_rgb(shadow)
    # eased 150/210 → 115/170 (2026-07-03): the text-band scrim already
    # guarantees legibility, and the two together washed AI photos grey
    for y in range(H):
        a = int(115 * max(0.0, (y - H * 0.30) / (H * 0.70)) ** 1.3)
        d.line([(0, y), (W, y)], fill=sc + (min(170, a),))
    out = Image.alpha_composite(img.convert("RGBA"), sh)
    if tint_alpha:
        out = Image.alpha_composite(
            out, Image.new("RGBA", (W, H), sc + (tint_alpha,)))
    return out


# ---- panels & chips --------------------------------------------------------
def panel(size, box, radius, fill, shadow: bool = True, shadow_alpha: int = 150):
    """A rounded card layer (full-frame RGBA) with an optional soft drop shadow."""
    from PIL import Image, ImageDraw, ImageFilter
    layer = Image.new("RGBA", size, (0, 0, 0, 0))
    x0, y0, x1, y1 = box
    if shadow:
        sh = Image.new("RGBA", size, (0, 0, 0, 0))
        sd = ImageDraw.Draw(sh)
        _rrect(sd, [x0, y0 + 16, x1, y1 + 16], radius, fill=(0, 0, 0, shadow_alpha))
        try:
            sh = sh.filter(ImageFilter.GaussianBlur(30))
        except Exception:                            # noqa: BLE001
            pass
        layer = Image.alpha_composite(layer, sh)
    d = ImageDraw.Draw(layer)
    _rrect(d, box, radius, fill=fill)
    return layer


def pill(draw, x, y, text, fnt, fill=None, text_fill=(255, 255, 255, 255),
         pad_x: int = 32, pad_y: int = 16, outline=None, outline_w: int = 0):
    """A rounded kicker/label CHIP drawn with its top-left at (x, y). Returns
    its bounding box so callers can flow the next element below it."""
    tw = draw.textlength(text, font=fnt)
    asc, desc = fnt.getmetrics()
    box = [x, y, x + tw + pad_x * 2, y + asc + desc + pad_y * 2]
    r = int((box[3] - box[1]) / 2)
    if fill:
        _rrect(draw, box, r, fill=fill)
    if outline:
        _rrect(draw, box, r, outline=outline, width=outline_w)
    draw.text((x + pad_x, y + pad_y - desc * 0.15), text, font=fnt, fill=text_fill)
    return box


def pill_centered(draw, cx, y, text, fnt, fill=None,
                  text_fill=(255, 255, 255, 255), **kw):
    """A pill centred horizontally on cx. Returns its bounding box."""
    tw = draw.textlength(text, font=fnt)
    pad_x = kw.get("pad_x", 32)
    x = cx - (tw + pad_x * 2) / 2
    return pill(draw, x, y, text, fnt, fill, text_fill, **kw)


def num_chip(draw, x, y, label, fnt, fill, text_fill, size: int):
    """A numbered token (a filled rounded square) for list rows."""
    box = [x, y, x + size, y + size]
    _rrect(draw, box, int(size * 0.30), fill=fill)
    tw = draw.textlength(label, font=fnt)
    asc, desc = fnt.getmetrics()
    draw.text((x + (size - tw) / 2, y + (size - (asc + desc)) / 2 + desc * 0.1),
              label, font=fnt, fill=text_fill)
    return box


def rule(draw, cx, y, half_w: int, color, width: int = 5, dot: bool = True):
    """An accent divider centred on cx, with an optional end dot."""
    draw.line([(cx - half_w, y), (cx + half_w, y)], fill=color, width=width)
    if dot:
        r = width + 3
        draw.ellipse([cx + half_w - r, y - r, cx + half_w + r, y + r], fill=color)


def ghost(size, text, fnt, color, alpha: int = 26, center=None):
    """An oversized, faint glyph behind a hero element — depth + fills space."""
    from PIL import Image, ImageDraw
    W, H = size
    layer = Image.new("RGBA", size, (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    tw = d.textlength(text, font=fnt)
    asc, desc = fnt.getmetrics()
    cx, cy = center or (W / 2, H / 2)
    d.text((cx - tw / 2, cy - (asc + desc) / 2), text, font=fnt,
           fill=hex_rgb(color) + (alpha,))
    return layer


# ---- the uniform editorial chrome -----------------------------------------
def chrome(img, *, idx: int, total: int, handle: str, accent, ink, muted,
           margin: int = 46, logo=None):
    """Draw the per-slide editorial frame onto `img` (in place): a hairline
    inset border with corner ticks, the slide index top-right, a progress rail
    and a brand footer. Shared by stills AND motion slides (drawn on the overlay
    layer so it survives the clip overlay). `handle` is the footer TEXT (the
    channel name, or a social handle); `logo` (a PIL image, e.g. the brand
    mark) is circle-badged in place of the accent dot when given."""
    from PIL import ImageDraw
    W, H = img.size
    d = ImageDraw.Draw(img)
    acc = hex_rgb(accent)
    mut = hex_rgb(muted)
    ink3 = hex_rgb(ink)

    # hairline frame + corner ticks
    m, tick = margin, 30
    d.rectangle([m, m, W - m, H - m], outline=acc + (60,), width=2)
    for (cx, cy, dx, dy) in [(m, m, 1, 1), (W - m, m, -1, 1),
                             (m, H - m, 1, -1), (W - m, H - m, -1, -1)]:
        d.line([(cx, cy), (cx + dx * tick, cy)], fill=acc + (220,), width=4)
        d.line([(cx, cy), (cx, cy + dy * tick)], fill=acc + (220,), width=4)

    # slide index — top right, small mono caps
    if total:
        f = font("Poppins-SemiBold.ttf", 28)
        label = "%02d / %02d" % (idx, total)
        tw = d.textlength(label, font=f)
        d.text((W - m - 18 - tw, m + 16), label, font=f, fill=mut + (235,))

    # brand footer — the logo as a small round badge (else an accent dot)
    # + the footer text, baseline-left
    foot_end = 0
    if handle:
        f = font("Poppins-SemiBold.ttf", 28)
        fy = H - m - 50
        x = m + 16
        badge = None
        if logo is not None:
            try:
                from PIL import Image
                size = 46
                badge = logo.convert("RGB").resize((size, size),
                                                   Image.LANCZOS)
                mask = Image.new("L", (size * 4, size * 4), 0)
                ImageDraw.Draw(mask).ellipse([0, 0, size * 4, size * 4],
                                             fill=255)
                mask = mask.resize((size, size), Image.LANCZOS)
                by = fy + 15 - size // 2          # centred on the text line
                img.paste(badge, (x, by), mask)
                x += size + 16
            except Exception:                     # noqa: BLE001 — dot fallback
                badge = None
        if badge is None:
            d.ellipse([x, fy + 6, x + 18, fy + 24], fill=acc + (255,))
            x += 30
        d.text((x, fy), handle, font=f, fill=ink3 + (210,))
        foot_end = x + d.textlength(handle, font=f)

    # progress rail — bottom centre dashes, the active one accent + wide;
    # a long handle shares the band, so the rail dodges right of it (staying
    # inside the frame) instead of striking through the text
    if total and total > 1:
        seg, gap = 26, 12
        span = total * seg + (total - 1) * gap
        x0 = (W - span) / 2
        if foot_end and x0 < foot_end + 28:
            x0 = min(foot_end + 28, W - m - 18 - span)
        ry = H - m - 30
        for i in range(total):
            on = (i + 1) == idx
            x = x0 + i * (seg + gap)
            w = seg + (14 if on else 0)
            col = acc + (255,) if on else mut + (90,)
            _rrect(d, [x, ry, x + w, ry + 7], 4, fill=col)
