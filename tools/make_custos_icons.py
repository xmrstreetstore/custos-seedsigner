"""
Build src/seedsigner/resources/fonts/custos-icons.otf: icons that neither Font
Awesome nor the SeedSigner icon font has.

    python tools/make_custos_icons.py [--preview out.png]

Needs fonttools and skia-pathops (build-time only; nothing here ships to the
device except the .otf it writes). Codepoints must match CustosIconConstants in
src/seedsigner/gui/components.py.

The one icon so far is a smartcard contact chip. It is drawn as the gold area
of a chip: a rounded plate with the contact separators cut out of it. The
separators are written as centrelines and stroked, then subtracted, so the
pads come out with an even gap however the curves bend.
"""
import argparse
import os

import pathops
from fontTools.fontBuilder import FontBuilder
from fontTools.pens.t2CharStringPen import T2CharStringPen

OUT = os.path.join(os.path.dirname(__file__), "..", "src", "seedsigner",
                   "resources", "fonts", "custos-icons.otf")

UPM = 1000
SMARTCARD = 0xEC00

# Drawn in a 720 x 480 box, y down, like the reference picture it follows;
# _to_font() flips and scales it. Line weights are heavier than the picture's
# because this renders at 22px, where one of its hairlines would be a third of
# a pixel and vanish.
W, H = 720.0, 480.0
RADIUS = 46.0
LINE = 34.0                 # separator width
RING = 30.0                 # gold rim outside the inner outline
INSET = RING + LINE / 2     # centreline of the inner outline

# Glyph size: 24px wide at the 22px icon size buttons use, sitting on the
# baseline the way Font Awesome's glyphs do.
SCALE = 1090.0 / W
DESCENT = 60.0


def _to_font(x, y):
    return (x * SCALE, (H - y) * SCALE - DESCENT)


def _rrect(x0, y0, x1, y1, r):
    p = pathops.Path()
    k = 0.5523 * r          # cubic approximation of a quarter circle
    p.moveTo(x0 + r, y0)
    p.lineTo(x1 - r, y0)
    p.cubicTo(x1 - r + k, y0, x1, y0 + r - k, x1, y0 + r)
    p.lineTo(x1, y1 - r)
    p.cubicTo(x1, y1 - r + k, x1 - r + k, y1, x1 - r, y1)
    p.lineTo(x0 + r, y1)
    p.cubicTo(x0 + r - k, y1, x0, y1 - r + k, x0, y1 - r)
    p.lineTo(x0, y0 + r)
    p.cubicTo(x0, y0 + r - k, x0 + r - k, y0, x0 + r, y0)
    p.close()
    return p


def _stroke(path, width):
    path.stroke(width, pathops.LineCap.ROUND_CAP, pathops.LineJoin.ROUND_JOIN, 4)
    path.convertConicsToQuads()     # round caps come out as conics
    return path


def smartcard_path():
    plate = _rrect(0, 0, W, H, RADIUS)

    cuts = []
    # Inner outline.
    cuts.append(_stroke(_rrect(INSET, INSET, W - INSET, H - INSET, RADIUS - INSET / 2), LINE))

    top, bottom = INSET, H - INSET
    left, right = INSET, W - INSET
    xl, xr = 255.0, 465.0           # the centre column's straight edges
    y1, y2 = 180.0, 305.0           # the two pad rows on each side

    # Left edge of the centre column: an S down from the top, straight past the
    # pads, then out along the bottom-left pad's rounded corner and diagonal.
    p = pathops.Path()
    p.moveTo(200, top)
    p.lineTo(200, 55)
    p.cubicTo(200, 95, 275, 85, 275, 130)
    p.cubicTo(275, 160, xl, 160, xl, y1)
    p.lineTo(xl, y2)
    p.cubicTo(xl + 45, y2, xl + 50, y2 + 35, xl + 30, y2 + 70)
    p.lineTo(xl - 25, bottom)
    cuts.append(_stroke(p, LINE))

    # Right edge: a hook that stops short of the top, so the top-right pad and
    # the centre stay one piece, then the mirror of the left edge below.
    p = pathops.Path()
    p.moveTo(530, 85)
    p.lineTo(475, 85)
    p.cubicTo(440, 85, 430, 105, 430, 130)
    p.cubicTo(430, 160, xr, 160, xr, y1)
    p.lineTo(xr, y2)
    p.cubicTo(xr - 45, y2, xr - 50, y2 + 35, xr - 30, y2 + 70)
    p.lineTo(xr + 25, bottom)
    cuts.append(_stroke(p, LINE))

    # Rows: two lines across each side.
    for y in (y1, y2):
        for x0, x1 in ((left, xl), (xr, right)):
            p = pathops.Path()
            p.moveTo(x0, y)
            p.lineTo(x1, y)
            cuts.append(_stroke(p, LINE))

    holes = cuts[0]
    for c in cuts[1:]:
        holes = pathops.op(holes, c, pathops.PathOp.UNION)
    return pathops.op(plate, holes, pathops.PathOp.DIFFERENCE)


def _draw(path, pen):
    for verb, pts in path.segments:
        pts = [_to_font(*pt) for pt in pts]
        if verb == "moveTo":
            pen.moveTo(pts[0])
        elif verb == "lineTo":
            pen.lineTo(pts[0])
        elif verb == "curveTo":
            pen.curveTo(*pts)
        elif verb == "qCurveTo":
            pen.qCurveTo(*pts)
        elif verb == "closePath":
            pen.closePath()
        elif verb == "endPath":
            pen.endPath()


def build(out=OUT):
    advance = int(W * SCALE + 60)
    glyphs = {".notdef": None, "smartcard": smartcard_path()}
    charstrings = {}
    for name, path in glyphs.items():
        pen = T2CharStringPen(advance, None)
        if path is not None:
            _draw(path, pen)
        charstrings[name] = pen.getCharString()

    fb = FontBuilder(UPM, isTTF=False)
    fb.setupGlyphOrder(list(glyphs))
    fb.setupCharacterMap({SMARTCARD: "smartcard"})
    fb.setupCFF("CustosIcons", {"FullName": "Custos Icons"}, charstrings, {})
    fb.setupHorizontalMetrics({n: (advance, 0) for n in glyphs})
    fb.setupHorizontalHeader(ascent=900, descent=-100)
    fb.setupNameTable({"familyName": "Custos Icons", "styleName": "Regular"})
    fb.setupOS2(sTypoAscender=900, sTypoDescender=-100, usWinAscent=900, usWinDescent=100)
    fb.setupPost()
    # Fixed timestamps, so regenerating gives the identical file and the
    # shipped .otf can be checked against this script.
    fb.updateHead(created=0, modified=0)
    fb.save(out)
    return out


def preview(font_path, png):
    from PIL import Image, ImageDraw, ImageFont
    sizes = (22, 44, 160)
    img = Image.new("RGB", (520, 200), (40, 40, 40))
    d = ImageDraw.Draw(img)
    x = 10
    for s in sizes:
        f = ImageFont.truetype(font_path, s)
        d.text((x, 180), chr(SMARTCARD), font=f, fill=(247, 147, 26), anchor="ls")
        x += int(s * 1.25) + 20
    img.save(png)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--preview")
    ap.add_argument("--out", default=OUT)
    a = ap.parse_args()
    path = build(a.out)
    print(path)
    if a.preview:
        preview(path, a.preview)
        print(a.preview)
