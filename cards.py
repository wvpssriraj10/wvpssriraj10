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

W = 880
GAP = 12  # vertical breathing room baked into every card, so README needs no spacing
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
        self.style = spec.get("style", "normal")
        CACHE.mkdir(exist_ok=True)
        raw = CACHE / (hashlib.sha1(spec["url"].encode()).hexdigest()[:12] + ".ttf")
        if not raw.exists():
            print(f"  downloading {spec['family']} {self.style} …")
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


GENERIC = {"display": "Georgia,serif", "displayi": "Georgia,serif", "body": "system-ui,sans-serif",
           "bodyb": "system-ui,sans-serif", "mono": "ui-monospace,Menlo,Consolas,monospace"}


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
@keyframes rise{from{opacity:0;transform:translateY(12px)}}
@keyframes fade{from{opacity:0}}
@keyframes grow{from{transform:scaleY(0)}}
@keyframes pop{from{opacity:0;transform:scale(.4)}}
@keyframes draw{from{stroke-dashoffset:var(--len)}}
.rise,.fade,.grow,.pop,.draw{animation-duration:.8s;animation-timing-function:cubic-bezier(.2,.7,.2,1);animation-fill-mode:both;animation-delay:var(--d,0s)}
.rise{animation-name:rise}
.fade{animation-name:fade;animation-duration:.5s}
.grow{animation-name:grow;transform-box:fill-box;transform-origin:50% 100%}
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

    def text_m(self, x, y, s, face, size, fill, anchor="start", track=0.0, kind=None, t=0.0, opacity=None):
        for ch in s:
            if not FACES[face].has(ch):
                print(f"  warn: {face} font lacks {ch!r} in card {self.name}")
        self.chars[face] |= set(s)
        a = f' text-anchor="{anchor}"' if anchor != "start" else ""
        tr = f' letter-spacing="{track}"' if track else ""
        op = f' fill-opacity="{opacity}"' if opacity is not None else ""
        at = self.anim(kind, t, face) if kind else f' class="{face}"'
        return (f'<text x="{x:g}" y="{y:g}" font-size="{size:g}" fill="{fill}"{a}{tr}{op}{at}>'
                f'{html.escape(s, quote=False)}</text>')

    def text(self, *a, **k):
        self.add(self.text_m(*a, **k))

    def runs(self, x, y, parts, kind=None, t=0.0, track=0.0):
        """Several faces/sizes on one baseline, as tspans inside ONE <text> so the browser
        does the spacing: parts = [(text, face, size, fill), …]."""
        spans = []
        for n, (s, face, size, fill) in enumerate(parts):
            self.chars[face] |= set(s)
            dx = f' dx="{size * 0.22:g}"' if n else ""
            spans.append(f'<tspan class="{face}" font-size="{size:g}" fill="{fill}"{dx}>'
                         f'{html.escape(s.strip(), quote=False)}</tspan>')
        tr = f' letter-spacing="{track}"' if track else ""
        self.add(f'<text x="{x:g}" y="{y:g}"{tr}{self.anim(kind, t) if kind else ""}>{"".join(spans)}</text>')

    def live(self, animated, still=""):
        """SMIL can't be turned off by CSS, so the animated copy is hidden under
        prefers-reduced-motion and a static twin is shown instead."""
        if STATIC:
            return still
        return f'<g class="smil">{animated}</g><g class="still">{still}</g>'

    def panel(self, x=0, w=None, h=None, fill=None, rx=14):
        w = w if w is not None else self.w - 2 * x
        h = h if h is not None else self.h - GAP
        self.add(f'<rect x="{x}" y="0" width="{w}" height="{h}" rx="{rx}" fill="{fill or T["card"]}"/>')

    def svg(self):
        css = "text{font-kerning:normal}"
        for key, face in FACES.items():
            if self.chars[key]:
                b64 = face.woff_b64("".join(self.chars[key]))
                css += (f"@font-face{{font-family:'SR {key}';font-weight:{face.weight};font-style:{face.style};"
                        f"src:url(data:font/woff;base64,{b64}) format('woff')}}")
            css += (f".{key}{{font-family:'SR {key}',{GENERIC[key]};font-weight:{face.weight};"
                    f"font-style:{face.style}}}")
        if not STATIC:
            css += KEYFRAMES
        title = html.escape(self.label, quote=True)
        return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{self.w}" height="{self.h}" '
                f'viewBox="0 0 {self.w} {self.h}" role="img" aria-label="{title}"><title>{title}</title>'
                f'<style>{css}</style><defs>{"".join(self.defs)}</defs>{"".join(self.body)}</svg>')

    def save(self):
        OUT.mkdir(parents=True, exist_ok=True)
        data = self.svg()
        (OUT / f"{self.name}.svg").write_text(data, encoding="utf-8")
        print(f"  {self.name}.svg  {len(data) / 1024:.1f} KB")


# Cards play top to bottom: each card starts a little after the one above it.
START = {"hero": 0.0, "tile": 0.6, "chart": 1.2, "title": 1.9, "repo": 2.1, "graph": 3.4}


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


def arrow(x, y, color, size=1.0):
    return (f'<path transform="translate({x:g},{y:g}) scale({size:g})" d="M0 11L11 0M3 0h8v8" fill="none" '
            f'stroke="{color}" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/>')


STAR = "M12 2.5l2.9 6 6.6.9-4.8 4.6 1.2 6.5L12 17.4 6.1 20.5l1.2-6.5L2.5 9.4l6.6-.9z"


def kicker(c, x, y, s, t=0.0, dot=False):
    """Small uppercase mono caption; optional live accent square before it."""
    if dot:
        sq = f'<rect x="{x}" y="{y - 8}" width="8" height="8" rx="2" fill="{T["brand"]}"/>'
        pulse = (f'<rect x="{x}" y="{y - 8}" width="8" height="8" rx="2" fill="{T["brand"]}">'
                 f'<animate attributeName="opacity" values="1;.25;1" dur="2.2s" repeatCount="indefinite"/></rect>')
        c.add(f'<g{c.anim("fade", t)}>{c.live(pulse, sq)}</g>')
        x += 18
    c.text(x, y, s, "mono", 12, T["muted"], track=1.0, kind="fade", t=t)


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


def ball(cx, cy, rotate):
    """A cricket ball: white disc, two rows of accent stitches. Optionally spinning (SMIL)."""
    spin = ('<animateTransform attributeName="transform" type="rotate" from="0" to="360" dur="90s" '
            'repeatCount="indefinite"/>' if rotate else "")
    return (f'<g transform="translate({cx},{cy})"><g>{spin}'
            f'<circle r="140" fill="#ffffff"/><circle r="140" fill="none" stroke="{T["faint"]}"/>'
            f'<path d="M-62 -124Q-24 0 -62 124M62 -124Q24 0 62 124" fill="none" stroke="{T["brand"]}" '
            f'stroke-width="3.5" stroke-linecap="round" stroke-dasharray="1 9"/>'
            f'<path d="M-44 -132Q-8 0 -44 132M44 -132Q8 0 44 132" fill="none" stroke="{T["faint"]}" '
            f'stroke-width="1.5"/></g></g>')


def card_hero():
    H = 440
    c = Card("hero", W, H, START["hero"], f'{CFG["name"]}. {CFG["tagline"]}')
    brand, ink = T["brand"], T["ink"]
    c.defs.append(
        f'<linearGradient id="shine" gradientUnits="userSpaceOnUse" x1="0" y1="0" x2="600" y2="0">'
        f'<stop offset="0" stop-color="{ink}"/><stop offset=".42" stop-color="{ink}"/>'
        f'<stop offset=".5" stop-color="{brand}"/><stop offset=".58" stop-color="{ink}"/>'
        f'<stop offset="1" stop-color="{ink}"/>'
        f'<animateTransform attributeName="gradientTransform" type="translate" values="-600 0;600 0;600 0" '
        f'keyTimes="0;.45;1" dur="7s" begin="1.6s" repeatCount="indefinite"/></linearGradient>'
        f'<clipPath id="pc"><circle cx="700" cy="{(H - GAP) // 2}" r="128"/></clipPath>')
    c.panel()
    kicker(c, 48, 57, CFG["kicker"], t=0.0, dot=True)

    pic = portrait_uri()
    cy = (H - GAP) // 2
    if pic:
        c.add(f'<g{c.anim("pop", 0.3)}><image href="{pic}" x="572" y="{cy - 128}" width="256" height="256" '
              f'clip-path="url(#pc)" preserveAspectRatio="xMidYMid slice"/></g>')
        ln = 2 * 3.14159 * 138
        ring = "" if STATIC else f' class="draw" style="--len:{ln:.0f}px;--d:{START["hero"] + .2:.2f}s"'
        c.add(f'<g transform="rotate(-90 700 {cy})"><circle cx="700" cy="{cy}" r="138" fill="none" stroke="{brand}" '
              f'stroke-width="2"{ring}/></g>')
    else:
        c.add(f'<g{c.anim("fade", 0.3)}>{c.live(ball(700, cy, True), ball(700, cy, False))}</g>')

    parts = CFG["name"].split()
    first, last = (" ".join(parts[:-1]), parts[-1]) if len(parts) > 1 else ("", parts[0])
    size = 116
    for face, s in (("display", first), ("displayi", last)):
        if s:
            size = min(size, fit(face, s, size, 500, 60, -2.5)[1])
    y1 = 168
    if first:
        c.text(44, y1, first, "display", size, ink, track=-2.5, kind="rise", t=0.15)
    y2 = y1 + (104 if first else 0)
    still = c.text_m(44, y2, last, "displayi", size, ink, track=-2.5, kind="rise", t=0.3)
    anim = c.text_m(44, y2, last, "displayi", size, "url(#shine)", track=-2.5, kind="rise", t=0.3)
    c.add(c.live(anim, still) if not STATIC else still)

    ty = y2 + 50
    for i, line in enumerate(wrap("body", CFG["tagline"], 18, 500, 2)):
        c.text(48, ty + i * 26, line, "body", 18, T["body"], kind="rise", t=0.55 + i * 0.12)

    py = H - GAP - 56
    st = CFG["status"]
    sw = FACES["mono"].width(st, 12) + 48
    dot = f'<circle cx="68" cy="{py + 17}" r="4" fill="{brand}"/>'
    pulse = (f'<circle cx="68" cy="{py + 17}" r="4" fill="none" stroke="{brand}"><animate attributeName="r" '
             f'values="4;11" dur="2.4s" repeatCount="indefinite"/><animate attributeName="opacity" values=".7;0" '
             f'dur="2.4s" repeatCount="indefinite"/></circle>')
    c.add(f'<g{c.anim("rise", 0.85)}><rect x="48" y="{py}" width="{sw:.0f}" height="34" rx="17" fill="#ffffff"/>'
          f'{dot}{c.live(pulse)}</g>')
    c.text(82, py + 21.5, st, "mono", 12, ink, kind="rise", t=0.85)
    x = 48 + sw + 10
    for i, chip in enumerate(CFG["chips"]):
        cw = FACES["mono"].width(chip, 12) + 28
        c.add(f'<g{c.anim("rise", 1.0 + i * 0.08)}><rect x="{x:.0f}" y="{py}" width="{cw:.0f}" height="34" rx="17" '
              f'fill="none" stroke="{T["faint"]}"/></g>')
        c.text(x + 14, py + 21.5, chip, "mono", 12, T["muted"], kind="rise", t=1.0 + i * 0.08)
        x += cw + 8
    return c


def card_tile(i, tile):
    w, h = 286, 234
    R = h - GAP
    c = Card(f"tile-{i + 1}", w, h, START["tile"] + i * 0.15, f'{tile["label"].title()}: {tile["sub"]}')
    fill = {"brand": T["brand"], "ink": T["ink"], "amber": T["amber"]}.get(tile["color"], tile["color"])
    fg = tile.get("fg", "#ffffff")
    c.defs.append(f'<clipPath id="card"><rect x="6" y="0" width="{w - 12}" height="{R}" rx="14"/></clipPath>'
                  f'<linearGradient id="gl" x1="0" x2="1"><stop offset="0" stop-color="#fff" stop-opacity="0"/>'
                  f'<stop offset=".5" stop-color="#fff" stop-opacity=".3"/><stop offset="1" stop-color="#fff" '
                  f'stop-opacity="0"/></linearGradient>')
    shine = (f'<rect x="-120" y="-20" width="90" height="260" fill="url(#gl)" transform="skewX(-14)">'
             f'<animate attributeName="x" values="-120;-120;380" keyTimes="0;.6;1" dur="9s" begin="{3.5 + i}s" '
             f'repeatCount="indefinite"/></rect>')
    c.add(f'<g{c.anim("rise", 0)}><g clip-path="url(#card)"><rect x="6" width="{w - 12}" height="{R}" fill="{fill}"/>'
          f'<circle cx="236" cy="172" r="60" fill="none" stroke="{fg}" stroke-opacity=".3" stroke-width="2"/>'
          f'{c.live(shine)}</g></g>')
    c.text(30, 42, tile["label"], "mono", 12, fg, track=1.2, kind="fade", t=0.2, opacity=0.85)
    c.add(f'<g{c.anim("rise", 0.25)}><path transform="translate({w - 44},30)" d="M0 6H14M9 1l5 5-5 5" fill="none" '
          f'stroke="{fg}" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/></g>')
    c.text(28, 112, tile["line1"], "display", 42, fg, track=-0.5, kind="rise", t=0.3)
    c.text(28, 158, tile["line2"], "displayi", 42, fg, track=-0.5, kind="rise", t=0.4)
    sub, ss = fit("body", tile["sub"], 14, w - 12 - 48, 10)
    c.text(30, 198, sub, "body", ss, fg, kind="rise", t=0.5, opacity=0.85)
    return c


def card_chart(days, st):
    H = 372
    c = Card("contributions", W, H, START["chart"], f'{st["total"]} contributions in the last 12 months, by month, '
             f'with current streak, active days and busiest day')
    ink, muted, brand = T["ink"], T["muted"], T["brand"]
    c.panel()
    kicker(c, 40, 58, "THE LAST 12 MONTHS ON GITHUB", t=0.0)
    c.runs(38, 114, [(f'{st["total"]:,} ', "display", 52, ink), ("contributions", "displayi", 52, ink)],
           kind="rise", t=0.1)

    months = monthly(days)
    top = max(v for _, v in months) or 1
    base, maxh, x0, area = 312, 140, 40, 540
    step = area / 12
    barw = step * 0.7
    peak = max(range(12), key=lambda i: months[i][1])
    for i, ((y, m), v) in enumerate(months):
        cx = x0 + step * i + step / 2
        h = max(maxh * v / top, 4)
        col = brand if i == peak else (ink if v else T["faint"])
        t0 = 0.4 + i * 0.07
        c.add(f'<rect x="{cx - barw / 2:.1f}" y="{base - h:.1f}" width="{barw:.1f}" height="{h:.1f}" rx="4" '
              f'fill="{col}"{c.anim("grow", t0)}><title>{MONTHS[m - 1]} {y}: {v}</title></rect>')
        c.text(cx, base + 24, MONTHS[m - 1], "mono", 11, muted, anchor="middle", kind="fade", t=t0)
    if months[peak][1]:
        cx = x0 + step * peak + step / 2
        h = max(maxh * months[peak][1] / top, 4)
        c.text(cx, base - h - 8, f"{months[peak][1]:,}", "mono", 12, ink, anchor="middle", kind="fade", t=1.4)

    cur = st["current"]
    rows = [
        (str(cur), " day" + ("" if cur == 1 else "s"), f'CURRENT STREAK · LONGEST {st["longest"]}'),
        (str(st["active"]), " active days", f'OF {st["span"]} · {round(100 * st["active"] / st["span"])}% OF THE YEAR'),
        (str(st["busiest"][1]), " in a day", f'BUSIEST DAY · {fdate(st["busiest"][0]).upper()}'),
    ]
    sx = 632
    for i, (num, unit, cap) in enumerate(rows):
        top_y = 70 + i * 88
        t0 = 0.5 + i * 0.18
        c.runs(sx, top_y + 50, [(num, "display", 52, ink), (unit, "displayi", 24, muted)], kind="rise", t=t0)
        cap, cs = fit("mono", cap, 11, W - 40 - sx, 8, 0.8)
        c.text(sx + 2, top_y + 72, cap, "mono", cs, muted, track=0.8, kind="fade", t=t0 + 0.1)
        if i:
            c.add(f'<line x1="{sx}" x2="{W - 40}" y1="{top_y - 12}" y2="{top_y - 12}" stroke="{T["faint"]}"'
                  f'{c.anim("fade", t0)}/>')
    return c


def card_title(light):
    name = "title-work-light" if light else "title-work-dark"
    c = Card(name, W, 84, START["title"], "Featured work")
    t = CFG["section_titles"]["work"]
    plain = T["ink"] if light else T["card"]
    c.runs(40, 62, [(t["plain"], "display", 44, plain), (t["italic"], "displayi", 44, T["brand"])],
           kind="rise", t=0)
    return c


def card_repo(i, item, meta):
    w, h = 440, 210
    R = h - GAP
    name = item["repo"]
    c = Card(f"repo-{i + 1}", w, h, START["repo"] + i * 0.2, f'{name}: {item["description"]}')
    brand = T["brand"]
    c.panel(x=6, w=w - 12, h=R)
    c.add(f'<g{c.anim("rise", 0.05)}><rect x="30" y="34" width="8" height="8" rx="2" fill="{brand}"/></g>')
    nm, ns = fit("bodyb", name, 18, w - 12 - 60 - 30, 12)
    c.text(46, 43, nm, "bodyb", ns, T["ink"], kind="rise", t=0.1)
    c.add(f'<g{c.anim("rise", 0.15)}>{arrow(w - 44, 30, T["muted"], 0.95)}</g>')
    desc = item.get("description") or meta.get("description") or ""
    for k, line in enumerate(wrap("body", desc, 14, w - 12 - 60, 3)):
        c.text(30, 80 + k * 22, line, "body", 14, T["body"], kind="rise", t=0.25 + k * 0.1)

    x, my = 30, R - 22
    lang = meta.get("language")
    if lang:
        c.add(f'<circle cx="{x + 5}" cy="{my - 4}" r="5" fill="{LANG_COLORS.get(lang, brand)}"{c.anim("pop", 0.5)}/>')
        c.text(x + 17, my, lang, "mono", 12, T["muted"], kind="rise", t=0.5)
        x += 17 + FACES["mono"].width(lang, 12) + 22
    if "stars" in meta:
        c.add(f'<g{c.anim("rise", 0.55)}><path d="{STAR}" transform="translate({x},{my - 13}) scale(.6)" fill="none" '
              f'stroke="{T["muted"]}" stroke-width="2" stroke-linejoin="round"/></g>')
        c.text(x + 19, my, str(meta["stars"]), "mono", 12, T["muted"], kind="rise", t=0.55)
    if meta.get("pushed_at"):
        d = dt.date.fromisoformat(meta["pushed_at"][:10])
        c.text(w - 30, my, f"UPDATED {fdate(d).upper()}", "mono", 11, T["muted"], anchor="end", track=0.6,
               kind="rise", t=0.6, opacity=0.8)
    return c


def card_graph(days, st):
    H = 286
    c = Card("graph", W, H, START["graph"], f'Contribution graph: {st["total"]} contributions in the last year')
    brand = T["brand"]
    c.panel()
    c.runs(42, 62, [("Every day, ", "display", 34, T["ink"]), ("in green", "displayi", 34, brand)], kind="rise", t=0)
    c.text(W - 42, 60, "REFRESHED DAILY", "mono", 12, T["muted"], anchor="end", track=1.0, kind="fade", t=0.1)

    first = days[0][0]
    sunday = first - dt.timedelta(days=(first.weekday() + 1) % 7)
    nz = sorted(v for _, v in days if v > 0)
    q = [nz[int(len(nz) * f)] for f in (0.25, 0.5, 0.75)] if nz else [1, 2, 3]
    levels = [T["empty"]] + [mix(T["empty"], brand, t) for t in (0.3, 0.55, 0.8, 1.0)]

    def level(v):
        return 0 if v <= 0 else 1 + sum(v > t for t in q)

    pitch, cell, gx, gy = 15, 12, 44, 112
    last_col = (days[-1][0] - sunday).days // 7
    last_label_col = -10
    for d, v in days:
        col, row = (d - sunday).days // 7, (d.weekday() + 1) % 7
        x, y = gx + col * pitch, gy + row * pitch
        c.add(f'<rect x="{x}" y="{y}" width="{cell}" height="{cell}" rx="3" fill="{levels[level(v)]}"'
              f'{c.anim("fade", 0.15 + col * 0.022 + row * 0.01)}><title>{v} on {fdate(d)}</title></rect>')
        if d.day <= 7 and row == 0 and col - last_label_col >= 3 and col <= last_col - 2:
            c.text(x, gy - 12, MONTHS[d.month - 1], "mono", 11, T["muted"], kind="fade", t=0.1 + col * 0.022)
            last_label_col = col

    d = days[-1][0]
    tx, ty = gx + ((d - sunday).days // 7) * pitch, gy + ((d.weekday() + 1) % 7) * pitch
    c.add(c.live(f'<rect x="{tx - 2}" y="{ty - 2}" width="{cell + 4}" height="{cell + 4}" rx="4.5" fill="none" '
                 f'stroke="{T["ink"]}"><animate attributeName="opacity" values="0;1;0" dur="2.8s" begin="3.8s" '
                 f'repeatCount="indefinite"/></rect>'))

    ly = H - GAP - 26
    lx = W - 42
    c.text(lx, ly + 1, "more", "mono", 11, T["muted"], anchor="end", kind="fade", t=0.2)
    lx -= FACES["mono"].width("more", 11) + 10
    for lv in (4, 3, 2, 1, 0):
        c.add(f'<rect x="{lx - cell}" y="{ly - 10}" width="{cell}" height="{cell}" rx="3" fill="{levels[lv]}"'
              f'{c.anim("fade", 0.2)}/>')
        lx -= cell + 4
    c.text(lx - 6, ly + 1, "less", "mono", 11, T["muted"], anchor="end", kind="fade", t=0.2)
    return c


# ───────────────────────── preview page (STATIC=1) ─────────────────────────
def write_preview(files):
    img = lambda f, w: f'<img src="cards/{f}" style="width:{w}%">'
    row = lambda pre, w: "".join(img(f, w) for f in files if f.startswith(pre))
    page = f"""<!doctype html><meta charset="utf-8"><title>Profile cards: final frame</title>
<style>body{{margin:0;background:{T["page"]};display:flex;justify-content:center}}
main{{width:{W}px;padding:24px 0;line-height:0;font-size:0}}img{{display:inline-block;vertical-align:top}}</style>
<main>{row("hero", 100)}<br>{row("tile-", 32.5)}<br>{row("contributions", 100)}<br>{row("title-work-light", 100)}<br>
{row("repo-", 50)}<br>{row("graph", 100)}</main>"""
    (ROOT / "preview" / "index.html").write_text(page, encoding="utf-8")


def main():
    print("STATIC (final frame) mode" if STATIC else "animated mode")
    load_fonts()
    days = load_days()
    st = compute(days)
    repos = load_repos()
    cards = [card_hero()]
    cards += [card_tile(i, t) for i, t in enumerate(CFG["tiles"])]
    cards += [card_chart(days, st), card_title(True), card_title(False)]
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
