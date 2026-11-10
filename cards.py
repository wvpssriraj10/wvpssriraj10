#!/usr/bin/env python3
"""Render the GitHub profile cards as animated SVGs (safe for <img> tags).

    python cards.py            -> cards/*.svg            (animated, what you commit)
    STATIC=1 python cards.py   -> preview/cards/*.svg    (final frame, no animation)
                                  preview/index.html     (README-like page to screenshot)

Everything that must work inside <img>: inline <style> with CSS keyframes, SMIL
<animate>, fonts subset with pyftsubset and embedded as base64 WOFF. No scripts,
no external requests. All animation is switched off under prefers-reduced-motion.
Inputs: config.json, data/contributions.json, data/repos.json.
"""
import base64
import datetime as dt
import hashlib
import html
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

from fontTools.ttLib import TTFont
from fontTools.varLib import instancer

ROOT = Path(__file__).resolve().parent
CFG = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
STATIC = os.environ.get("STATIC") == "1"
OUT = ROOT / "preview" / "cards" if STATIC else ROOT / "cards"
CACHE = ROOT / ".font-cache"

W = 830
T = CFG["theme"]
MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
LANG_COLORS = {
    "Python": "#3572A5", "JavaScript": "#f1e05a", "TypeScript": "#3178c6", "HTML": "#e34c26",
    "CSS": "#563d7c", "Jupyter Notebook": "#DA5B0B", "R": "#198CE7", "Java": "#b07219",
    "C": "#555555", "C++": "#f34b7d", "SQL": "#e38c00", "Shell": "#89e051",
}


# ───────────────────────── colour helpers ─────────────────────────
def _rgb(h):
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def mix(a, b, t):
    ra, rb = _rgb(a), _rgb(b)
    return "#%02x%02x%02x" % tuple(round(x + (y - x) * t) for x, y in zip(ra, rb))


# ───────────────────────── fonts ─────────────────────────
class Face:
    """One font face: download -> pin weight (if variable) -> measure -> subset to WOFF."""

    def __init__(self, key, spec):
        self.key = key
        self.family = spec["family"]
        self.weight = spec["weight"]
        CACHE.mkdir(exist_ok=True)
        raw = CACHE / (hashlib.sha1(spec["url"].encode()).hexdigest()[:12] + ".ttf")
        if not raw.exists():
            print(f"  downloading {spec['family']} …")
            req = urllib.request.Request(spec["url"], headers={"User-Agent": "profile-cards"})
            raw.write_bytes(urllib.request.urlopen(req, timeout=60).read())
        font = TTFont(raw)
        if "fvar" in font:  # pin every axis so we get a small static instance
            loc = {}
            for ax in font["fvar"].axes:
                if ax.axisTag == "wght":
                    loc[ax.axisTag] = min(max(self.weight, ax.minValue), ax.maxValue)
                else:
                    loc[ax.axisTag] = ax.defaultValue
            font = instancer.instantiateVariableFont(font, loc)
        self.path = CACHE / f"{key}-{self.weight}.ttf"
        font.save(self.path)
        self.font = TTFont(self.path)
        self.cmap = self.font.getBestCmap()
        self.hmtx = self.font["hmtx"]
        self.upm = self.font["head"].unitsPerEm
        self._woff = {}

    def has(self, ch):
        return ord(ch) in self.cmap

    def width(self, s, size, track=0.0):
        total = 0
        for ch in s:
            g = self.cmap.get(ord(ch))
            total += self.hmtx[g][0] if g else self.upm * 0.55
        return total / self.upm * size + track * len(s)

    def woff_b64(self, chars):
        text = "".join(sorted(set(chars) | {" "}))
        if text in self._woff:
            return self._woff[text]
        with tempfile.TemporaryDirectory() as td:
            txt, out = Path(td) / "t.txt", Path(td) / "o.woff"
            txt.write_text(text, encoding="utf-8")
            exe = shutil.which("pyftsubset")
            cmd = [exe] if exe else [sys.executable, "-m", "fontTools.subset"]
            subprocess.run(
                cmd + [str(self.path), f"--text-file={txt}", "--flavor=woff", "--layout-features=kern,liga",
                       "--no-hinting", f"--output-file={out}"],
                check=True, capture_output=True)
            data = base64.b64encode(out.read_bytes()).decode()
        self._woff[text] = data
        return data


FACES = {}


def load_fonts():
    for key, spec in CFG["fonts"].items():
        if not key.startswith("_"):
            FACES[key] = Face(key, spec)


GENERIC = {"display": "system-ui,sans-serif", "body": "system-ui,sans-serif",
           "mono": "ui-monospace,Menlo,Consolas,monospace"}


def fit(face, text, size, maxw, minsize=10, track=0.0):
    """Shrink the font size until text fits; ellipsise as a last resort."""
    f = FACES[face]
    while size > minsize and f.width(text, size, track) > maxw:
        size -= 0.5
    ell = "…" if f.has("…") else "..."
    while f.width(text, size, track) > maxw and len(text) > 1:
        text = text[:-2].rstrip() + ell
    return text, size


def wrap(face, text, size, maxw, max_lines):
    f = FACES[face]
    lines, cur = [], ""
    for w in text.split():
        t = (cur + " " + w).strip()
        if f.width(t, size) <= maxw:
            cur = t
        else:
            lines.append(cur)
            cur = w
    lines.append(cur)
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        ell = "…" if f.has("…") else "..."
        last = lines[-1]
        while f.width(last + ell, size) > maxw and len(last) > 1:
            last = last[:-1]
        lines[-1] = last.rstrip() + ell
    return lines


# ───────────────────────── card scaffold ─────────────────────────
KEYFRAMES = """
@keyframes rise{from{opacity:0;transform:translateY(10px)}}
@keyframes fade{from{opacity:0}}
@keyframes grow{from{transform:scaleY(0)}}
@keyframes sweep{from{transform:scaleX(0)}}
@keyframes pop{from{opacity:0;transform:scale(.4)}}
@keyframes draw{from{stroke-dashoffset:var(--len)}}
.rise,.fade,.grow,.sweep,.pop,.draw{animation-duration:.8s;animation-timing-function:cubic-bezier(.2,.7,.2,1);animation-fill-mode:both;animation-delay:var(--d,0s)}
.rise{animation-name:rise}
.fade{animation-name:fade;animation-duration:.5s}
.grow{animation-name:grow;transform-box:fill-box;transform-origin:50% 100%}
.sweep{animation-name:sweep;transform-box:fill-box;transform-origin:0 50%}
.pop{animation-name:pop;transform-box:fill-box;transform-origin:50% 50%}
.draw{animation-name:draw;animation-duration:1.4s;stroke-dasharray:var(--len)}
.still{display:none}
@media (prefers-reduced-motion:reduce){
*{animation:none!important;transition:none!important}
.smil{display:none}
.still{display:inline}
}
"""


class Card:
    def __init__(self, name, w, h, start, label):
        self.name, self.w, self.h, self.start, self.label = name, w, h, start, label
        self.body, self.defs = [], []
        self.chars = {k: set() for k in FACES}

    # animation attributes: class + stagger delay (card start offset + local offset)
    def anim(self, kind, t, extra_cls=""):
        cls = " ".join(x for x in (extra_cls, "" if STATIC else kind) if x)
        sty = "" if STATIC else f' style="--d:{self.start + t:.2f}s"'
        return (f' class="{cls}"' if cls else "") + sty

    def add(self, s):
        self.body.append(s)

    def text(self, x, y, s, face, size, fill, anchor="start", track=0.0, kind=None, t=0.0, opacity=None):
        for ch in s:
            if not FACES[face].has(ch):
                print(f"  warn: {face} font lacks {ch!r} in card {self.name}")
        self.chars[face] |= set(s)
        a = f' text-anchor="{anchor}"' if anchor != "start" else ""
        tr = f' letter-spacing="{track}"' if track else ""
        op = f' fill-opacity="{opacity}"' if opacity is not None else ""
        at = self.anim(kind, t, face) if kind else f' class="{face}"'
        self.add(f'<text x="{x:g}" y="{y:g}" font-size="{size:g}" fill="{fill}"{a}{tr}{op}{at}>'
                 f'{html.escape(s, quote=False)}</text>')

    def live(self, animated, still=""):
        """SMIL can't be turned off by CSS, so the animated copy is hidden under
        prefers-reduced-motion and a static twin is shown instead."""
        if STATIC:
            return still
        return f'<g class="smil">{animated}</g><g class="still">{still}</g>'

    def panel(self, rx=18):
        self.add(f'<rect x=".5" y=".5" width="{self.w - 1}" height="{self.h - 1}" rx="{rx}" '
                 f'fill="{T["surface"]}" stroke="{T["border"]}"/>')

    def svg(self):
        css = "text{font-kerning:normal}"
        for key, face in FACES.items():
            if self.chars[key]:
                b64 = face.woff_b64("".join(self.chars[key]))
                css += (f"@font-face{{font-family:'SR {key}';font-weight:{face.weight};"
                        f"src:url(data:font/woff;base64,{b64}) format('woff')}}")
            css += f".{key}{{font-family:'SR {key}',{GENERIC[key]};font-weight:{face.weight}}}"
        if not STATIC:
            css += KEYFRAMES
        title = html.escape(self.label, quote=True)
        return (f'<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" '
                f'width="{self.w}" height="{self.h}" viewBox="0 0 {self.w} {self.h}" role="img" '
                f'aria-label="{title}"><title>{title}</title><style>{css}</style>'
                f'<defs>{"".join(self.defs)}</defs>{"".join(self.body)}</svg>')

    def save(self):
        OUT.mkdir(parents=True, exist_ok=True)
        data = self.svg()
        (OUT / f"{self.name}.svg").write_text(data, encoding="utf-8")
        print(f"  {self.name}.svg  {len(data) / 1024:.1f} KB")


# Cards play top to bottom: each card starts a little after the one above it.
START = {"hero": 0.0, "tile": 0.55, "chart": 1.15, "repo": 1.9, "graph": 3.2}

# ───────────────────────── data ─────────────────────────
def fdate(d):
    return f"{MONTHS[d.month - 1]} {d.day}, {d.year}"


def load_days():
    raw = json.loads((ROOT / "data" / "contributions.json").read_text(encoding="utf-8"))
    return sorted((dt.date.fromisoformat(x["date"]), int(x["count"])) for x in raw["days"])


def load_repos():
    p = ROOT / "data" / "repos.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


def compute(days):
    total = sum(c for _, c in days)
    active = sum(1 for _, c in days if c > 0)
    busiest = max(days, key=lambda x: (x[1], x[0]))
    longest = run = 0
    for _, c in days:
        run = run + 1 if c > 0 else 0
        longest = max(longest, run)
    i = len(days) - 1
    if days[i][1] == 0:  # today may not have a contribution yet: don't break the streak
        i -= 1
    cur = 0
    while i >= 0 and days[i][1] > 0:
        cur += 1
        i -= 1
    return dict(total=total, active=active, busiest=busiest, longest=longest, current=cur, span=len(days))


def monthly(days):
    y, m = days[-1][0].year, days[-1][0].month
    keys = []
    for _ in range(12):
        keys.append((y, m))
        m -= 1
        if m == 0:
            m, y = 12, y - 1
    keys.reverse()
    sums = {k: 0 for k in keys}
    for d, c in days:
        if (d.year, d.month) in sums:
            sums[(d.year, d.month)] += c
    return [(k, sums[k]) for k in keys]


# ───────────────────────── icons (24×24 grid, stroked) ─────────────────────────
ICONS = {
    "briefcase": '<rect x="3" y="7" width="18" height="13" rx="2"/><path d="M9 7V5a2 2 0 0 1 2-2h2a2 2 0 0 1 2 2v2M3 13h18"/>',
    "person": '<circle cx="12" cy="8" r="4"/><path d="M4 21c0-4.4 3.6-7 8-7s8 2.6 8 7"/>',
    "mail": '<rect x="3" y="5" width="18" height="14" rx="2"/><path d="M3 7l9 6 9-6"/>',
}
STAR = "M12 2.5l2.9 6 6.6.9-4.8 4.6 1.2 6.5L12 17.4 6.1 20.5l1.2-6.5L2.5 9.4l6.6-.9z"


def icon(name, x, y, scale, color):
    return (f'<g transform="translate({x:g},{y:g}) scale({scale:g})" fill="none" stroke="{color}" '
            f'stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">{ICONS[name]}</g>')


def arrow(x, y, color):
    return (f'<path transform="translate({x:g},{y:g})" d="M0 11L11 0M3 0h8v8" fill="none" stroke="{color}" '
            f'stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/>')


# ───────────────────────── cards ─────────────────────────
def portrait_uri():
    p = ROOT / CFG.get("portrait", "")
    if not CFG.get("portrait") or not p.exists():
        return None
    try:
        from PIL import Image
        im = Image.open(p).convert("RGB")
        s = min(im.size)
        l, t = (im.width - s) // 2, (im.height - s) // 2
        im = im.crop((l, t, l + s, t + s)).resize((320, 320))
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=84, optimize=True)
        return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()
    except ImportError:
        mime = "image/png" if p.suffix.lower() == ".png" else "image/jpeg"
        return f"data:{mime};base64," + base64.b64encode(p.read_bytes()).decode()


def card_hero():
    H = 282
    c = Card("hero", W, H, START["hero"], f'{CFG["name"]}: {CFG["tagline"]}')
    brand = T["brand"]
    c.defs.append(
        f'<linearGradient id="bg" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="{T["bg"]}"/>'
        f'<stop offset="1" stop-color="{T["surface"]}"/></linearGradient>'
        f'<radialGradient id="glow"><stop offset="0" stop-color="{brand}" stop-opacity=".30"/>'
        f'<stop offset="1" stop-color="{brand}" stop-opacity="0"/></radialGradient>'
        f'<linearGradient id="shine" x1="0" y1="0" x2="1" y2="0"><stop offset="0" stop-color="#fff" stop-opacity="0"/>'
        f'<stop offset=".5" stop-color="#fff" stop-opacity=".06"/><stop offset="1" stop-color="#fff" stop-opacity="0"/></linearGradient>'
        f'<clipPath id="clip"><rect width="{W}" height="{H}" rx="20"/></clipPath>'
        f'<clipPath id="pc"><circle cx="{W - 124}" cy="{H // 2}" r="76"/></clipPath>')
    c.add(f'<rect x=".5" y=".5" width="{W - 1}" height="{H - 1}" rx="20" fill="url(#bg)" stroke="{T["border"]}"/>')
    glow_still = f'<circle cx="{W - 120}" cy="30" r="260" fill="url(#glow)" opacity=".85"/>'
    glow_anim = (f'<circle cx="{W - 120}" cy="30" r="260" fill="url(#glow)"><animate attributeName="opacity" '
                 f'values=".6;1;.6" dur="7s" repeatCount="indefinite"/></circle>')
    c.add(f'<g clip-path="url(#clip)">{c.live(glow_anim, glow_still)}')
    c.add(c.live(f'<rect x="-200" y="0" width="160" height="{H}" fill="url(#shine)" transform="skewX(-18)">'
                 f'<animate attributeName="x" values="-200;-200;1000" keyTimes="0;.7;1" dur="10s" begin="1.8s" '
                 f'repeatCount="indefinite"/></rect>'))
    c.add("</g>")

    pic = portrait_uri()
    text_w = W - 88 - (200 if pic else 0)
    if pic:
        cx, cy = W - 124, H // 2
        c.add(f'<g{c.anim("pop", 0.3)}><image href="{pic}" x="{cx - 76}" y="{cy - 76}" width="152" height="152" '
              f'clip-path="url(#pc)" preserveAspectRatio="xMidYMid slice"/></g>')
        ln = 2 * 3.14159 * 84
        ring = "" if STATIC else f' class="draw" style="--len:{ln:.0f}px;--d:{START["hero"] + .2:.2f}s"'
        c.add(f'<g transform="rotate(-90 {cx} {cy})"><circle cx="{cx}" cy="{cy}" r="84" fill="none" stroke="{brand}" '
              f'stroke-width="2"{ring}/></g>')

    c.text(44, 64, CFG["kicker"], "mono", 12, brand, track=2.4, kind="rise", t=0.1)
    name, nsize = fit("display", CFG["name"], 60, text_w, 34)
    c.text(44, 64 + 14 + nsize * 0.8, name, "display", nsize, T["text"], kind="rise", t=0.25)
    y = 64 + 14 + nsize * 0.8 + 38
    for i, line in enumerate(wrap("body", CFG["tagline"], 19, text_w, 2)):
        c.text(44, y + i * 27, line, "body", 19, T["muted"], kind="rise", t=0.5 + i * 0.12)

    # status pill with a pulsing dot, then skill chips
    py = H - 62
    st = CFG["status"]
    sw = FACES["mono"].width(st, 12) + 52
    c.add(f'<g{c.anim("rise", 0.85)}><rect x="44" y="{py}" width="{sw:.0f}" height="30" rx="15" fill="{brand}" '
          f'fill-opacity=".12" stroke="{brand}" stroke-opacity=".45"/><circle cx="62" cy="{py + 15}" r="4" fill="{brand}"/>'
          + c.live(f'<circle cx="62" cy="{py + 15}" r="4" fill="none" stroke="{brand}"><animate attributeName="r" '
                   f'values="4;11" dur="2.4s" repeatCount="indefinite"/><animate attributeName="opacity" '
                   f'values=".7;0" dur="2.4s" repeatCount="indefinite"/></circle>')
          + "</g>")
    c.text(76, py + 19.5, st, "mono", 12, T["text"], kind="rise", t=0.85)
    x = 44 + sw + 12
    for i, chip in enumerate(CFG["chips"]):
        cw = FACES["mono"].width(chip, 12) + 24
        c.add(f'<g{c.anim("rise", 1.0 + i * 0.1)}><rect x="{x:.0f}" y="{py}" width="{cw:.0f}" height="30" rx="9" '
              f'fill="{T["bg"]}" stroke="{T["border"]}"/></g>')
        c.text(x + 12, py + 19.5, chip, "mono", 12, T["muted"], kind="rise", t=1.0 + i * 0.1)
        x += cw + 8
    return c


def card_tile(i, tile):
    w, h = 270, 112
    c = Card(f"tile-{i + 1}", w, h, START["tile"] + i * 0.15, f'{tile["label"]}: {tile["sub"]}')
    brand = T["brand"]
    c.add(f'<g{c.anim("rise", 0)}><rect x=".5" y=".5" width="{w - 1}" height="{h - 1}" rx="16" fill="{T["surface"]}" '
          f'stroke="{T["border"]}"/><circle cx="46" cy="56" r="26" fill="{brand}" fill-opacity=".12"/>'
          f'{icon(tile["icon"], 28, 38, 1.5, brand)}{arrow(w - 36, 24, T["muted"])}</g>')
    label, ls = fit("display", tile["label"], 21, 150, 14)
    c.text(88, 54, label, "display", ls, T["text"], kind="rise", t=0.1)
    sub, ss = fit("body", tile["sub"], 14, 162, 10)
    c.text(88, 78, sub, "body", ss, T["muted"], kind="rise", t=0.2)
    return c


def card_chart(days, st):
    H = 360
    c = Card("contributions", W, H, START["chart"], "Contributions per month, current streak, active days and busiest day")
    brand = T["brand"]
    c.panel()
    c.text(30, 50, "Contributions per month", "display", 22, T["text"], kind="rise", t=0)
    c.text(W - 30, 50, f'{st["total"]:,} in the last year', "mono", 12, T["muted"], anchor="end", kind="rise", t=0.05)

    bw, gap = (W - 60 - 28) / 3, 14
    boxes = [
        ("CURRENT STREAK", f'{st["current"]} day' + ("" if st["current"] == 1 else "s"), f'longest {st["longest"]} days'),
        ("ACTIVE DAYS", f'{st["active"]}', f'of {st["span"]} days · {round(100 * st["active"] / st["span"])}%'),
        ("BUSIEST DAY", f'{st["busiest"][1]} contributions', fdate(st["busiest"][0])),
    ]
    for i, (lab, val, sub) in enumerate(boxes):
        x = 30 + i * (bw + gap)
        t0 = 0.15 + i * 0.12
        c.add(f'<g{c.anim("rise", t0)}><rect x="{x:.1f}" y="72" width="{bw:.1f}" height="82" rx="12" fill="{T["bg"]}" '
              f'stroke="{T["border"]}"/><rect x="{x:.1f}" y="72" width="3" height="82" rx="1.5" fill="{brand}"/></g>')
        c.text(x + 18, 94, lab, "mono", 10.5, T["muted"], track=1.4, kind="rise", t=t0 + 0.05)
        v, vs = fit("display", val, 26, bw - 36, 16)
        c.text(x + 18, 126, v, "display", vs, T["text"], kind="rise", t=t0 + 0.1)
        s, ss = fit("mono", sub, 11.5, bw - 36, 9)
        c.text(x + 18, 144, s, "mono", ss, brand, kind="rise", t=t0 + 0.15)

    months = monthly(days)
    top = max(v for _, v in months) or 1
    base, maxh = 312, 118
    x0, span = 30, W - 60
    step = span / 12
    barw = step * 0.56
    for frac in (0.5, 1.0):
        gy = base - maxh * frac
        c.add(f'<line x1="{x0}" x2="{W - 30}" y1="{gy:.1f}" y2="{gy:.1f}" stroke="{T["border"]}" stroke-dasharray="3 5" '
              f'stroke-opacity=".7"/>')
    c.add(f'<line x1="{x0}" x2="{W - 30}" y1="{base}" y2="{base}" stroke="{T["border"]}"/>')
    peak = max(range(12), key=lambda i: months[i][1])
    for i, ((y, m), v) in enumerate(months):
        cx = x0 + step * i + step / 2
        h = max(maxh * v / top, 2.5)
        col = brand if i == peak else mix(T["surface"], brand, 0.5)
        t0 = 0.45 + i * 0.06
        c.add(f'<rect x="{cx - barw / 2:.1f}" y="{base - h:.1f}" width="{barw:.1f}" height="{h:.1f}" rx="4" fill="{col}"'
              f'{c.anim("grow", t0)}/>')
        if v:
            c.text(cx, base - h - 7, str(v), "mono", 11, T["text"] if i == peak else T["muted"], anchor="middle",
                   kind="fade", t=t0 + 0.4)
        c.text(cx, base + 20, MONTHS[m - 1], "mono", 11, T["muted"], anchor="middle", kind="fade", t=t0)
        if i == 0 or m == 1:
            c.text(cx, base + 35, str(y), "mono", 10, T["muted"], anchor="middle", kind="fade", t=t0, opacity=0.6)
    return c


def card_repo(i, item, meta):
    H = 148
    name = item["repo"]
    c = Card(f"repo-{i + 1}", W, H, START["repo"] + i * 0.25, f'{name}: {item["description"]}')
    brand = T["brand"]
    c.panel()
    c.add(f'<rect x="0" y="22" width="4" height="{H - 44}" rx="2" fill="{brand}"{c.anim("grow", 0.1)}/>')
    c.add(f'<g{c.anim("rise", 0.1)}><path d="M5 4.5A1.5 1.5 0 0 1 6.5 3H18v15H6.5a1.5 1.5 0 0 0 0 3H18M5 4.5v15" '
          f'transform="translate(30,26) scale(1.15)" fill="none" stroke="{brand}" stroke-width="1.8" '
          f'stroke-linecap="round" stroke-linejoin="round"/></g>')
    c.text(64, 46, name, "display", 22, T["text"], kind="rise", t=0.15)
    c.add(f'<g{c.anim("rise", 0.2)}>{arrow(W - 52, 28, T["muted"])}</g>')
    desc = item.get("description") or meta.get("description") or ""
    for k, line in enumerate(wrap("body", desc, 15, W - 64 - 44, 2)):
        c.text(64, 76 + k * 22, line, "body", 15, T["muted"], kind="rise", t=0.3 + k * 0.1)

    x, my = 64, H - 24
    lang = meta.get("language")
    if lang:
        col = LANG_COLORS.get(lang, brand)
        c.add(f'<circle cx="{x + 5}" cy="{my - 4}" r="5" fill="{col}"{c.anim("pop", 0.5)}/>')
        c.text(x + 17, my, lang, "mono", 12, T["muted"], kind="rise", t=0.5)
        x += 17 + FACES["mono"].width(lang, 12) + 26
    if "stars" in meta:
        c.add(f'<g{c.anim("rise", 0.55)}><path d="{STAR}" transform="translate({x},{my - 13}) scale(.62)" fill="none" '
              f'stroke="{T["muted"]}" stroke-width="2" stroke-linejoin="round"/></g>')
        c.text(x + 20, my, str(meta["stars"]), "mono", 12, T["muted"], kind="rise", t=0.55)
        x += 20 + FACES["mono"].width(str(meta["stars"]), 12) + 24
    if "forks" in meta:
        c.add(f'<g transform="translate({x},{my - 13}) scale(.62)" fill="none" stroke="{T["muted"]}" stroke-width="2" '
              f'stroke-linecap="round"><g{c.anim("rise", 0.6)}><circle cx="6" cy="5" r="2.6"/><circle cx="18" cy="5" r="2.6"/>'
              f'<circle cx="12" cy="20" r="2.6"/><path d="M6 8v2a3 3 0 0 0 3 3h6a3 3 0 0 0 3-3V8M12 13v4.4"/></g></g>')
        c.text(x + 20, my, str(meta["forks"]), "mono", 12, T["muted"], kind="rise", t=0.6)
    if meta.get("pushed_at"):
        d = dt.date.fromisoformat(meta["pushed_at"][:10])
        c.text(W - 30, my, f"updated {fdate(d)}", "mono", 12, T["muted"], anchor="end", kind="rise", t=0.65, opacity=0.8)
    return c


def card_graph(days, st):
    H = 218
    c = Card("graph", W, H, START["graph"], f'Contribution graph: {st["total"]} contributions in the last year')
    brand = T["brand"]
    c.panel()
    c.text(30, 46, "Contribution graph", "display", 22, T["text"], kind="rise", t=0)
    c.text(W - 30, 46, f'{st["total"]:,} contributions in the last year', "mono", 12, T["muted"], anchor="end",
           kind="rise", t=0.05)

    first = days[0][0]
    sunday = first - dt.timedelta(days=(first.weekday() + 1) % 7)
    nz = sorted(v for _, v in days if v > 0)
    q = [nz[int(len(nz) * f)] for f in (0.25, 0.5, 0.75)] if nz else [1, 2, 3]
    levels = [mix(T["surface"], T["border"], 0.55)] + [mix(T["surface"], brand, t) for t in (0.3, 0.52, 0.76, 1.0)]

    def level(v):
        if v <= 0:
            return 0
        return 1 + sum(v > t for t in q)

    pitch, cell, gx, gy = 14, 11, 62, 82
    last_col = (days[-1][0] - sunday).days // 7
    last_label_col = -10
    for d, v in days:
        col, row = (d - sunday).days // 7, (d.weekday() + 1) % 7
        x, y = gx + col * pitch, gy + row * pitch
        c.add(f'<rect x="{x}" y="{y}" width="{cell}" height="{cell}" rx="2.5" fill="{levels[level(v)]}"'
              f'{c.anim("fade", 0.15 + col * 0.022 + row * 0.01)}><title>{v} on {fdate(d)}</title></rect>')
        if d.day <= 7 and row == 0 and col - last_label_col >= 3 and col <= last_col - 2:
            c.text(x, gy - 12, MONTHS[d.month - 1], "mono", 10.5, T["muted"], kind="fade", t=0.1 + col * 0.022)
            last_label_col = col
    for row, lab in ((1, "Mon"), (3, "Wed"), (5, "Fri")):
        c.text(30, gy + row * pitch + 9, lab, "mono", 10, T["muted"], kind="fade", t=0.1)

    # today's cell gets a soft pulse (SMIL, replaced by nothing under reduced motion)
    d = days[-1][0]
    tx, ty = gx + ((d - sunday).days // 7) * pitch, gy + ((d.weekday() + 1) % 7) * pitch
    c.add(c.live(f'<rect x="{tx - 1.5}" y="{ty - 1.5}" width="{cell + 3}" height="{cell + 3}" rx="4" fill="none" '
                 f'stroke="{brand}"><animate attributeName="opacity" values="0;1;0" dur="2.8s" begin="3.5s" '
                 f'repeatCount="indefinite"/></rect>'))

    ly = H - 22
    lx = W - 30
    c.text(lx, ly + 0.5, "More", "mono", 10.5, T["muted"], anchor="end", kind="fade", t=0.2)
    lx -= FACES["mono"].width("More", 10.5) + 8
    for lv in (4, 3, 2, 1, 0):
        c.add(f'<rect x="{lx - cell}" y="{ly - 9}" width="{cell}" height="{cell}" rx="2.5" fill="{levels[lv]}"'
              f'{c.anim("fade", 0.2)}/>')
        lx -= cell + 4
    c.text(lx - 4, ly + 0.5, "Less", "mono", 10.5, T["muted"], anchor="end", kind="fade", t=0.2)
    return c


# ───────────────────────── preview page (STATIC=1) ─────────────────────────
def write_preview(files):
    tiles = "\n".join(f'<img src="cards/{f}" width="32.5%">' for f in files if f.startswith("tile-"))
    others = lambda pre: "\n".join(f'<img src="cards/{f}" width="100%">' for f in files if f.startswith(pre))
    page = f"""<!doctype html><meta charset="utf-8"><title>Profile cards: final frame</title>
<style>body{{margin:0;background:#0d1117;display:flex;justify-content:center}}
main{{width:{W}px;padding:24px 0;line-height:0}}img{{display:block;margin:0 0 14px}}
p{{margin:0 0 14px;display:flex;gap:1.25%;line-height:0}}p img{{margin:0}}</style>
<main>{others("hero")}<p>{tiles}</p>{others("contributions")}{others("repo-")}{others("graph")}</main>"""
    (ROOT / "preview" / "index.html").write_text(page, encoding="utf-8")


def main():
    print("STATIC (final frame) mode" if STATIC else "animated mode")
    load_fonts()
    days = load_days()
    st = compute(days)
    repos = load_repos()
    cards = [card_hero()]
    cards += [card_tile(i, t) for i, t in enumerate(CFG["tiles"])]
    cards += [card_chart(days, st)]
    cards += [card_repo(i, f, repos.get(f["repo"], {})) for i, f in enumerate(CFG["featured"])]
    cards += [card_graph(days, st)]
    if OUT.exists():
        for old in OUT.glob("*.svg"):
            old.unlink()
    for card in cards:
        card.save()
    if STATIC:
        write_preview([f"{c.name}.svg" for c in cards])
        print("open preview/index.html and screenshot it")


if __name__ == "__main__":
    main()
