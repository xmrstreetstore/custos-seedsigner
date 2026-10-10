"""
Read dice values from a photo of dice laid out in rows.

Pure numpy + Pillow: the device has neither OpenCV nor scipy, and a Pi Zero has
one slow core, so everything that touches pixels is a Pillow filter or a numpy
array operation, and the Python loops only ever run over pips or dice.

The approach, in order:

  1. Pips. A pip is a small round spot much darker (or, on red/black dice,
     much lighter) than the die face around it. Threshold against the brightest
     (darkest) pixel nearby, label the connected blobs, keep the round ones of a
     common size that sit on die face.
  2. The grid. Dice in rows sit on a lattice, and no pip ever lies on the
     boundary between two dice. Pitches that leave the boundaries empty are
     candidates; the one under which the most blobs make valid dice wins. That
     separates touching dice, where pip-to-pip distance alone cannot (a 6's two
     columns are as far apart as two neighbouring dice). A quadratic fit through
     the die centres then follows a camera not held square and the lens's bend.
  3. Each die. Its pips must form one of the six standard faces, allowing for
     the die sitting at an angle, at the same size as the other dice, and the
     picture must show nothing where its face has no pip. A missed pip or a
     stray blob fails one of these, and the die reads as unknown (None) instead
     of as the wrong number.

Tested against synthetic photos (white, ivory, red and black dice; touching,
in a tray, loose and crooked; tilted, in perspective, blurred, noisy, unevenly
lit): no die has read as a wrong number. Hard pictures give unknowns.

The caller decides what to do with unknowns; nothing here guesses.
"""
from dataclasses import dataclass, field
import math

import numpy as np
from PIL import Image, ImageFilter, ImageOps


#: The six faces as sets of positions on a 3x3 lattice, (column, row), -1..1.
#: Faces without 90-degree symmetry (2, 3, 6) are matched in both orientations.
_CORNERS = {(-1, -1), (1, -1), (-1, 1), (1, 1)}
FACES = {
    1: [{(0, 0)}],
    2: [{(-1, -1), (1, 1)}, {(1, -1), (-1, 1)}],
    3: [{(-1, -1), (0, 0), (1, 1)}, {(1, -1), (0, 0), (-1, 1)}],
    4: [_CORNERS],
    5: [_CORNERS | {(0, 0)}],
    6: [_CORNERS | {(-1, 0), (1, 0)}, _CORNERS | {(0, -1), (0, 1)}],
}

#: Longest side the photo is scaled to before looking for pips. A die in a
#: 10 x 10 layout is then ~55 px across, a pip ~10 px.
WORK_SIZE = 720

#: Half-width of the band around each cell boundary that must hold no pip
#: centre, as a fraction of the pitch. A pip centre on a die filling its cell
#: is 0.25 in from the edge; this leaves room for dice sitting at an angle.
BOUNDARY_BAND = 0.14


@dataclass
class DiceReading:
    values: list = field(default_factory=list)  # reading order; 1-6, or None if unreadable
    rows: int = 0
    cols: int = 0
    pips: int = 0
    detail: str = ""                            # one line, for when nothing was read

    @property
    def unknown(self) -> int:
        return sum(v is None for v in self.values)


def read_dice(image: Image.Image) -> DiceReading:
    """
    Read every die in the photo, left to right, top row first.

    Tries dark pips on light dice and light pips on dark dice, at two
    neighbourhood sizes, and keeps whichever reading explains the most dice.
    Stops at the first reading that is clean: a Pi Zero takes seconds a pass.
    """
    gray = ImageOps.grayscale(image)
    scale = WORK_SIZE / max(gray.size)
    if scale < 1:
        gray = gray.resize((round(gray.width * scale), round(gray.height * scale)),
                           Image.Resampling.BOX)
    g = np.asarray(gray, dtype=np.int16)
    short = min(g.shape)

    best, tried = None, []
    for window in (short // 18, short // 9):
        for dark in (True, False):
            reading = _read_once(g, dark, max(3, window))
            tried.append((reading.detail, reading.pips))
            if best is None or _score(reading) > _score(best):
                best = reading
            if len(best.values) >= 2 and best.unknown <= len(best.values) // 50:
                return best
    if not best.values:
        # The pass that found the most blobs says the most about why it failed.
        best.detail = max(tried, key=lambda t: t[1])[0] if tried else "no dice found"
    return best


def _score(r: DiceReading):
    known = len(r.values) - r.unknown
    return (known, -r.unknown)


# --- 1. pips -----------------------------------------------------------------

def _max_filter(a: np.ndarray, size: int) -> np.ndarray:
    """Maximum over a size x size square, by doubling: O(log size) array ops."""
    h, w = a.shape
    # Each pass looks forwards from a pixel; padding by half a window first
    # centres it, and edge padding keeps the borders honest.
    a = np.pad(a, size // 2, mode="edge")
    for axis in (0, 1):
        out, k = a, 1
        while 2 * k <= size:
            out = _shift_max(out, k, axis)
            k *= 2
        a = _shift_max(out, size - k, axis) if size > k else out
    return a[:h, :w]


def _shift_max(a, k, axis):
    if k <= 0:
        return a
    b = a.copy()
    if axis == 0:
        b[:-k] = np.maximum(a[:-k], a[k:])
    else:
        b[:, :-k] = np.maximum(a[:, :-k], a[:, k:])
    return b


def _read_once(g: np.ndarray, dark: bool, window: int) -> DiceReading:
    # Compare each pixel with the brightest (or darkest) one around it, which
    # for a pip is its die face. A local mean would not do: next to a dark table
    # it sinks, and the pips nearest a die's edge are lost.
    if dark:
        ref = _max_filter(g, window)
        mask = (g * 4 < ref * 3) & (ref - g > 30)
    else:
        ref = -_max_filter(-g, window)
        mask = (g * 3 > ref * 4) & (g - ref > 30)
    # A 3x3 opening removes speckle and one-pixel bridges between pips.
    img = Image.fromarray((mask * 255).astype(np.uint8))
    img = img.filter(ImageFilter.MinFilter(3)).filter(ImageFilter.MaxFilter(3))
    mask = np.asarray(img) > 0

    pts, diameter = _pips(mask)
    kind = "dark" if dark else "light"
    if len(pts) == 0:
        return DiceReading(detail=f"{kind}: no pips")
    on_face, levels = _on_face(g, pts, diameter)
    look = _Looker(g, levels, dark)
    reading = _read_grid(pts, diameter, on_face, look)
    reading.pips = len(pts)
    if not reading.values:
        reading.detail = f"{kind}: {len(pts)} pips, {reading.detail}"
    return reading


def _label(mask: np.ndarray):
    """
    Connected components (8-connected) of a boolean image, by runs.

    Returns per-run arrays (row, start, end, label). Works on runs rather than
    pixels so the only Python-level loop is the label propagation, which is
    vectorised and converges in about as many passes as a blob is tall.
    """
    h, w = mask.shape
    padded = np.zeros((h, w + 2), dtype=np.int8)
    padded[:, 1:-1] = mask
    d = np.diff(padded, axis=1)
    rows, starts = np.nonzero(d == 1)
    _r2, ends = np.nonzero(d == -1)            # exclusive; pairs up with starts
    n = len(rows)
    if n == 0:
        return rows, starts, ends, np.zeros(0, dtype=np.intp)

    stride = w + 2
    start_keys = rows * stride + starts
    end_keys = rows * stride + ends
    # Runs on the next row that overlap this one, 8-connected: start < end + 1
    # and end + 1 > start. Rows are in order and runs within a row are sorted,
    # so each run's partners are one contiguous slice.
    lo = np.searchsorted(end_keys, (rows + 1) * stride + starts - 1, side="right")
    hi = np.searchsorted(start_keys, (rows + 1) * stride + ends + 1, side="left")
    counts = np.maximum(hi - lo, 0)
    a = np.repeat(np.arange(n), counts)
    b = (np.arange(counts.sum()) - np.repeat(np.cumsum(counts) - counts, counts)
         + np.repeat(lo, counts))

    labels = np.arange(n)
    while True:
        m = np.minimum(labels[a], labels[b])
        new = labels.copy()
        np.minimum.at(new, a, m)
        np.minimum.at(new, b, m)
        new = new[new]                          # pointer jumping
        if np.array_equal(new, labels):
            break
        labels = new
    return rows, starts, ends, labels


def _pips(mask: np.ndarray):
    """Centres of the pip-like blobs, and their typical diameter."""
    rows, starts, ends, labels = _label(mask)
    if len(rows) == 0:
        return np.zeros((0, 2)), 0.0
    _, inv = np.unique(labels, return_inverse=True)
    k = inv.max() + 1
    length = (ends - starts).astype(np.float64)
    area = np.bincount(inv, weights=length, minlength=k)
    sx = np.bincount(inv, weights=length * (starts + ends - 1) / 2.0, minlength=k)
    sy = np.bincount(inv, weights=length * rows, minlength=k)
    x0 = np.full(k, 1 << 30); np.minimum.at(x0, inv, starts)
    x1 = np.zeros(k, dtype=np.intp); np.maximum.at(x1, inv, ends)
    y0 = np.full(k, 1 << 30); np.minimum.at(y0, inv, rows)
    y1 = np.zeros(k, dtype=np.intp); np.maximum.at(y1, inv, rows + 1)
    bw, bh = x1 - x0, y1 - y0

    # Round and solid: a filled circle covers pi/4 of its box.
    ok = ((area >= 10) & (np.maximum(bw, bh) <= 1.7 * np.minimum(bw, bh))
          & (area >= 0.55 * bw * bh))
    if not ok.any():
        return np.zeros((0, 2)), 0.0
    # Pips on one set of dice share a size; noise does not. Take the commonest
    # size (on a log scale) and keep what is near it. A big "1" pip is allowed.
    la = np.log(area[ok])
    hist, edges = np.histogram(la, bins=max(4, int((la.max() - la.min()) / 0.15) + 1))
    i = hist.argmax()
    typical = math.exp((edges[i] + edges[i + 1]) / 2)
    keep = ok & (area >= 0.35 * typical) & (area <= 4.5 * typical)
    pts = np.stack([sx[keep] / area[keep], sy[keep] / area[keep]], axis=1)
    return pts, 2 * math.sqrt(typical / math.pi)


def _sample(g: np.ndarray, x, y):
    h, w = g.shape
    return g[np.clip(np.round(y).astype(int), 0, h - 1), np.clip(np.round(x).astype(int), 0, w - 1)]


def _on_face(g: np.ndarray, pts: np.ndarray, diameter: float):
    """
    Per blob: is it surrounded by die face? Also returns (face, pip) brightness.

    The brightness a pip diameter out from each blob's centre is, for nearly all
    of them, the face of the die they are on, so the median over all blobs is
    the face's brightness. A speck on the table is surrounded by table instead.
    The ring's own median shrugs off the neighbouring pips it crosses.
    """
    ang = np.linspace(0, 2 * math.pi, 16, endpoint=False)
    ring = _sample(g, pts[:, 0:1] + diameter * np.cos(ang)[None, :],
                   pts[:, 1:2] + diameter * np.sin(ang)[None, :])
    ring = np.median(ring, axis=1)
    centre = _sample(g, pts[:, 0], pts[:, 1])
    face, pip = float(np.median(ring)), float(np.median(centre))
    return np.abs(ring - face) < 0.35 * max(8.0, abs(face - pip)), (face, pip)


class _Looker:
    """Is there a pip at these image points? Used to catch pips the mask missed."""

    def __init__(self, g, levels, dark):
        self.g, self.dark = g, dark
        face, pip = levels
        self.middle = (face + pip) / 2

    def pip_at(self, xy: np.ndarray) -> np.ndarray:
        # The darkest (or lightest) of a small cross, so a point a pixel off a
        # pip's centre still finds it.
        x, y = xy[:, 0], xy[:, 1]
        vals = np.stack([_sample(self.g, x + dx, y + dy)
                         for dx, dy in ((0, 0), (1.5, 0), (-1.5, 0), (0, 1.5), (0, -1.5))])
        if self.dark:
            return vals.min(axis=0) < self.middle
        return vals.max(axis=0) > self.middle


# --- 2. the grid -------------------------------------------------------------

#: Largest tilt of the rows searched for, and the step, in degrees.
MAX_TILT, TILT_STEP = 20, 0.5


def _rotation(pts: np.ndarray, diameter: float) -> float:
    """
    Tilt of the rows, in radians.

    At the right angle the pips' positions along each axis bunch into the
    columns and rows of dice, so the projections are sharpest: lowest entropy.
    The dice's own angles do not enter into it, which matters - loose dice in a
    tray each lie a little crooked, and pip-to-pip directions follow the die,
    not the row.
    """
    if len(pts) < 3:
        return 0.0
    c = pts - pts.mean(axis=0)
    width = max(1.0, 0.75 * diameter)
    best, best_h = 0.0, None
    for deg in np.arange(-MAX_TILT, MAX_TILT + TILT_STEP / 2, TILT_STEP):
        t = math.radians(deg)
        h = 0.0
        for axis in (c[:, 0] * math.cos(t) + c[:, 1] * math.sin(t),
                     -c[:, 0] * math.sin(t) + c[:, 1] * math.cos(t)):
            counts = np.bincount(np.floor((axis - axis.min()) / width).astype(int))
            prob = counts[counts > 0] / len(axis)
            h -= (prob * np.log(prob)).sum()
        if best_h is None or h < best_h:
            best, best_h = t, h
    return best


def _periods(c: np.ndarray, pmin: float, pmax: float, keep: int):
    """
    Candidate (pitch, offset) pairs along one axis: those that put the fewest
    pip centres on a cell boundary.

    Only candidates: pips sit on three evenly spaced lanes per die, so a pitch of
    three lanes keeps the boundaries just as empty as the true four, and the dark
    gaps where four dice corners meet sit right on the true boundaries. Which
    candidate is the grid is decided in 2D, by how many dice it explains.
    """
    pitches = np.geomspace(pmin, pmax, 100)
    phases = np.linspace(0, 1, 32, endpoint=False)
    bad = np.empty(len(pitches))
    for i, p in enumerate(pitches):
        f = (c[None, :] / p - phases[:, None]) % 1.0
        bad[i] = ((f < BOUNDARY_BAND) | (f > 1 - BOUNDARY_BAND)).sum(axis=1).min()
    # Local minima, best first, skipping near-duplicates of a pitch already kept.
    order = [i for i in range(len(bad))
             if bad[i] <= bad[max(0, i - 1)] and bad[i] <= bad[min(len(bad) - 1, i + 1)]]
    order.sort(key=lambda i: (bad[i], pitches[i]))
    out = []
    for i in order:
        p = pitches[i]
        if all(abs(p - q) > 0.04 * q for q, _ in out):
            out.append((p, _centre_phase(c, p)))
        if len(out) == keep:
            break
    return out


def _centre_phase(c: np.ndarray, p: float) -> float:
    """The offset in the middle of the run of best offsets, in pixels."""
    phases = np.linspace(0, 1, 200, endpoint=False)
    f = (c[None, :] / p - phases[:, None]) % 1.0
    bad = ((f < BOUNDARY_BAND) | (f > 1 - BOUNDARY_BAND)).sum(axis=1)
    good = bad == bad.min()
    # Longest circular run of good offsets; its middle is the safest boundary.
    doubled = np.concatenate([good, good])
    best_len, best_mid, run = 0, 0, 0
    for i, g in enumerate(doubled):
        run = run + 1 if g else 0
        if run > best_len and run <= len(good):
            best_len, best_mid = run, i - run / 2 + 0.5
    return (phases[int(best_mid) % len(phases)]) * p


#: Offsets tried per axis when placing a candidate grid.
OFFSET_STEPS = 12


def _fit_offsets(rot, pitch_x, pitch_y, off_x, off_y):
    """
    Where the grid lines go, given the pitch: the offset under which the most
    blobs sit in cells of at most six whose average position is the cell's
    centre. Every die face is symmetric about its centre, so the right grid
    centres them all; a grid that cuts dice leaves the pieces off-centre.

    All OFFSET_STEPS x OFFSET_STEPS offsets are scored in one batch of array
    operations rather than a Python loop.
    """
    k = OFFSET_STEPS
    st = np.arange(k) / k
    n = len(rot)
    gx = (rot[:, 0] - off_x) / pitch_x
    gy = (rot[:, 1] - off_y) / pitch_y
    GX = np.broadcast_to((gx[None, :] - st[:, None])[:, None, :], (k, k, n))
    GY = np.broadcast_to((gy[None, :] - st[:, None])[None, :, :], (k, k, n))
    cx, cy = np.floor(GX), np.floor(GY)
    fx, fy = GX - cx, GY - cy
    inside = (fx > EDGE_BLOB) & (fx < 1 - EDGE_BLOB) & (fy > EDGE_BLOB) & (fy < 1 - EDGE_BLOB)
    if not inside.any():
        return off_x, off_y
    cx = (cx - cx.min()).astype(np.intp)
    cy = (cy - cy.min()).astype(np.intp)
    w, h = int(cx.max()) + 1, int(cy.max()) + 1
    combo = np.broadcast_to(np.arange(k * k).reshape(k, k, 1), (k, k, n))
    ids = ((combo * h + cy) * w + cx)[inside]
    size = k * k * h * w
    count = np.bincount(ids, minlength=size)
    with np.errstate(invalid="ignore", divide="ignore"):
        mx = np.bincount(ids, weights=fx[inside], minlength=size) / count
        my = np.bincount(ids, weights=fy[inside], minlength=size) / count
    ok = (count > 0) & (count <= 6) & (np.abs(mx - 0.5) < 0.1) & (np.abs(my - 0.5) < 0.1)
    per = np.bincount(np.arange(size) // (h * w), weights=count * ok, minlength=k * k)
    i = int(per.argmax())
    return off_x + st[i // k] * pitch_x, off_y + st[i % k] * pitch_y


def _homography(src: np.ndarray, dst: np.ndarray):
    """Least-squares 3x3 H with dst ~ H @ src (DLT, normalised)."""
    def norm(p):
        m = p.mean(axis=0)
        s = math.sqrt(2) / max(1e-9, np.sqrt(((p - m) ** 2).sum(axis=1)).mean())
        return np.array([[s, 0, -s * m[0]], [0, s, -s * m[1]], [0, 0, 1]])
    ts, td = norm(src), norm(dst)
    s = (ts @ np.c_[src, np.ones(len(src))].T).T
    d = (td @ np.c_[dst, np.ones(len(dst))].T).T
    rows = []
    for (x, y, _), (u, v, _) in zip(s, d):
        rows.append([-x, -y, -1, 0, 0, 0, u * x, u * y, u])
        rows.append([0, 0, 0, -x, -y, -1, v * x, v * y, v])
    _, _, vt = np.linalg.svd(np.array(rows))
    h = vt[-1].reshape(3, 3)
    h = np.linalg.inv(td) @ h @ ts
    return h / h[2, 2]


def _apply(h: np.ndarray, p: np.ndarray) -> np.ndarray:
    q = np.c_[p, np.ones(len(p))] @ h.T
    return q[:, :2] / q[:, 2:3]


class _Quadratic:
    """
    A least-squares map from 2D points to 2D points, quadratic in each axis.

    Used in place of a homography so the grid can follow what a homography
    cannot: the camera's lens bends straight rows slightly, and a phone-style
    hold is rarely a clean tilt. It is fitted both ways (grid -> image and
    image -> grid) rather than inverted, which a quadratic cannot be exactly.
    """

    def __init__(self, src: np.ndarray, dst: np.ndarray):
        self.m = src.mean(axis=0)
        self.s = max(1e-9, float(np.abs(src - self.m).max()))
        self.coef, *_ = np.linalg.lstsq(self._terms(src), dst, rcond=None)

    def _terms(self, p):
        u = (p - self.m) / self.s
        x, y = u[:, 0], u[:, 1]
        return np.stack([np.ones_like(x), x, y, x * x, x * y, y * y], axis=1)

    def __call__(self, p: np.ndarray) -> np.ndarray:
        return self._terms(p) @ self.coef


def _read_grid(pts: np.ndarray, diameter: float, on_face: np.ndarray, look) -> DiceReading:
    if len(pts) < 1:
        return DiceReading(detail="no pips")
    theta = _rotation(pts, diameter)
    c, s = math.cos(-theta), math.sin(-theta)
    rot = pts @ np.array([[c, s], [-s, c]])

    # Grid coordinates: one unit per cell. Try each candidate grid and keep the
    # one under which the most blobs belong to a die showing a valid face.
    best, best_score = None, -1
    xs = _periods(rot[:, 0], 3.2 * diameter, 12 * diameter, keep=4)
    # The commonest wrong answer is three pip lanes instead of four, 3/4 of the
    # true pitch, and when it wins the search the true one can drop off the
    # list. So the 4/3 of the two best are always tried too.
    for p, _off in xs[:2]:
        q = p * 4 / 3
        if all(abs(q - r) > 0.04 * r for r, _ in xs):
            xs.append((q, _centre_phase(rot[:, 0], q)))
    for pitch_x, off_x in xs:
        if np.ptp(rot[:, 1]) < 1.5 * pitch_x:    # a single row
            ys = [(pitch_x, rot[:, 1].mean() - pitch_x / 2)]
        else:
            # Cells are square unless the camera is tilted, so always try that.
            ys = _periods(rot[:, 1], 0.8 * pitch_x, 1.25 * pitch_x, keep=2)
            ys.append((pitch_x, _centre_phase(rot[:, 1], pitch_x)))
        for pitch_y, off_y in ys:
            off_x, off_y = _fit_offsets(rot, pitch_x, pitch_y, off_x, off_y)
            grid = np.c_[(rot[:, 0] - off_x) / pitch_x, (rot[:, 1] - off_y) / pitch_y]
            score = _explained(grid, on_face)
            if score > best_score:
                best, best_score = grid, score
    if best is None or best_score <= 0:
        return DiceReading(detail="no grid")
    grid = best

    # Follow perspective and lens bend: fit cell centre -> pip centroid through
    # the dice that read cleanly, map the pips back through it, and reassign.
    for _ in range(3):
        good_src, good_dst = [], []
        for key, idx in _groups(grid).items():
            if _face(grid[idx], key, on_face[idx].all()) is not None:
                good_src.append((key[0] + 0.5, key[1] + 0.5))
                good_dst.append(rot[idx].mean(axis=0))
        src, dst = np.array(good_src), np.array(good_dst)
        if len(src) >= 12 and np.ptp(src[:, 0]) >= 2 and np.ptp(src[:, 1]) >= 2:
            refined = _Quadratic(dst, src)(rot)
        elif len(src) >= 8 and np.ptp(src[:, 0]) >= 1 and np.ptp(src[:, 1]) >= 1:
            try:
                refined = _apply(np.linalg.inv(_homography(src, dst)), rot)
            except np.linalg.LinAlgError:
                break
        else:
            break
        if _explained(refined, on_face) < _explained(grid, on_face):
            break
        grid = refined

    groups = _groups(grid)
    fits = {key: _fit(grid[idx], key, on_face[idx].all()) for key, idx in groups.items()}
    # The same dice again, in picture pixels: the check for missed pips looks at
    # the picture, so it works from where the pips really are, not through the
    # grid fit, which can be off where few dice read (a corner, say).
    shapes = {key: _shape(pts[groups[key]]) for key, fit in fits.items() if fit is not None}
    steps = [f[3] for f in fits.values() if f is not None and f[3] is not None]
    px_steps = [sh[3] for sh in shapes.values() if sh is not None and sh[3] is not None]
    step = float(np.median(steps)) if steps else 0.25
    px_step = float(np.median(px_steps)) if px_steps else 0.0
    faces = {key: _confirm(fit, shapes.get(key), key, step, px_step, look)
             for key, fit in fits.items()}
    # The layout is the box around the dice that read. A stray blob off to the
    # side reads as nothing and so cannot stretch it.
    valid = [k for k, v in faces.items() if v is not None]
    if not valid:
        return DiceReading(detail="no valid dice")
    c0, c1 = min(k[0] for k in valid), max(k[0] for k in valid)
    r0, r1 = min(k[1] for k in valid), max(k[1] for k in valid)
    values, occupied = [], []
    for r in range(r0, r1 + 1):
        for col in range(c0, c1 + 1):
            values.append(faces.get((col, r)))
            occupied.append((col, r) in groups)
    # Empty cells after the last die are not dice. An empty cell between dice is
    # a die whose pips were all missed, and stays as an unknown.
    while values and values[-1] is None and not occupied[len(values) - 1]:
        values.pop()
    return DiceReading(values=values, rows=r1 - r0 + 1, cols=c1 - c0 + 1)


#: Blobs this close to a cell edge (in cells) are not pips: no pip centre is
#: nearer the edge than ~0.18, while the dark gap where four dice corners meet
#: sits right on it.
EDGE_BLOB = 0.1


def _groups(grid: np.ndarray) -> dict:
    """{(col, row): indices of the blobs in that cell}, edge blobs left out."""
    frac = grid - np.floor(grid)
    inside = ((frac > EDGE_BLOB) & (frac < 1 - EDGE_BLOB)).all(axis=1)
    cells = np.floor(grid).astype(int)
    out = {}
    for i in np.nonzero(inside)[0]:
        out.setdefault((int(cells[i, 0]), int(cells[i, 1])), []).append(i)
    return {k: np.array(v) for k, v in out.items()}


def _explained(grid: np.ndarray, on_face: np.ndarray) -> int:
    """
    Blobs that belong to a die showing a valid face under this grid, less two
    for every cell whose blobs make no face.

    A grid finer than the true one chops dice into pieces, and the pieces read
    as 1s: a lone corner pip, or the gap where four dice corners meet. So a lone
    blob scores half. Real dice show a 1 one time in six; a wrong grid's
    "dice" are mostly 1s, and lose.
    """
    score = 0.0
    for key, idx in _groups(grid).items():
        if _face(grid[idx], key, on_face[idx].all()) is not None:
            score += len(idx) if len(idx) > 1 else 0.5
        else:
            score -= 2
    return score


# --- 3. each die -------------------------------------------------------------

def _face(p: np.ndarray, cell, on_face: bool = True):
    """The value of the die whose pips (in grid units) are p, or None."""
    fit = _fit(p, cell, on_face)
    return None if fit is None else fit[0]


def _fit(p: np.ndarray, cell, on_face: bool = True):
    """
    (value, centre, angle, lattice step, lattice spots) of the die whose
    pips (in grid units) are p, or None if they make no face.

    on_face says whether all of these blobs sit on die face (see _on_face):
    grain in a wooden table can make a pair of dots that pass for a 2.

    Lattice spacing comes from the die itself: its corner pips sit sqrt(2)
    lattice steps from its centre, on every face but the 1. The die's own angle
    comes from its pips too (corners at 45 degrees, a 6's middle pair at 0), so
    a die lying crooked in its cell still reads.
    """
    if not on_face:
        return None
    shape = _shape(p)
    if shape is None:
        return None
    n, centre, _alpha, q, _spots = shape
    if n == 1:
        cx, cy = cell[0] + 0.5, cell[1] + 0.5
        return shape if abs(centre[0] - cx) < 0.4 and abs(centre[1] - cy) < 0.4 else None
    # A corner pip sits a quarter of the die from its centre on each axis: 0.25
    # of a cell for touching dice, less with gaps between them. Much more and
    # the "cell" is smaller than a die - a wrong grid.
    return shape if 0.12 < q < 0.31 else None


def _shape(p: np.ndarray):
    """
    (value, centre, angle, lattice step, lattice spots) if the points p form a
    die face, in whatever units p is in; else None.
    """
    n = len(p)
    if n < 1 or n > 6:
        return None
    centre = p.mean(axis=0)
    if n == 1:
        return 1, centre, 0.0, None, {(0, 0)}
    o = p - centre
    r = np.hypot(o[:, 0], o[:, 1])
    q = r.max() / math.sqrt(2)
    if q <= 0:
        return None
    ratio = r / q
    ang = np.arctan2(o[:, 1], o[:, 0])
    corner = ratio > 1.2
    edge = (ratio > 0.5) & ~corner
    a = np.where(corner, ang - math.pi / 4, ang)
    use = corner | edge
    alpha = float(np.angle(np.exp(4j * a[use]).mean()) / 4) if use.any() else 0.0
    ca, sa = math.cos(-alpha), math.sin(-alpha)
    o = o @ np.array([[ca, sa], [-sa, ca]]) / q
    lattice = np.round(o)
    if np.abs(o - lattice).max() > 0.35 or np.abs(lattice).max() > 1:
        return None
    spots = {(int(x), int(y)) for x, y in lattice}
    if len(spots) != n:
        return None
    for face in FACES[n]:
        for k in range(4):                       # any quarter turn
            if _turn(spots, k) == face:
                return n, centre, alpha, q, spots
    return None


def _confirm(fit, shape, cell, step: float, px_step: float, look):
    """
    The die's value, if it survives two checks against the other dice and the
    picture itself; else None.

      - Its lattice step must match the other dice'. Losing a corner pip of a 3
        leaves a corner and the centre - a perfectly good 2, at half the size.
      - Every lattice point its face leaves empty must be empty in the picture.
        A pip the mask missed (blur, glare, a dark table right next to the edge)
        is still dark where it should not be, and the die becomes unknown
        instead of reading one lower.
    """
    if fit is None or shape is None or shape[0] != fit[0]:
        return None
    value, centre, _alpha, q, _spots = fit
    if value == 1:
        # A 1's pip is the die's centre; a lone corner pip of a 2 is not.
        if math.hypot(centre[0] - cell[0] - 0.5, centre[1] - cell[1] - 0.5) > 0.6 * step:
            return None
        if px_step <= 0:
            return None
        ang = np.linspace(0, 2 * math.pi, 8, endpoint=False)
        ring = np.r_[np.c_[np.cos(ang), np.sin(ang)] * px_step,
                     np.c_[np.cos(ang + math.pi / 8), np.sin(ang + math.pi / 8)] * px_step * math.sqrt(2)]
        empty = shape[1] + ring
    else:
        if not 0.75 * step < q < 1.33 * step:
            return None
        _v, px_centre, px_alpha, px_q, px_spots = shape
        empty = np.array([px_centre + _rotate((i, j), px_alpha) * px_q
                          for i in (-1, 0, 1) for j in (-1, 0, 1) if (i, j) not in px_spots])
    if len(empty) and look.pip_at(empty).any():
        return None
    return value


def _rotate(v, alpha):
    ca, sa = math.cos(alpha), math.sin(alpha)
    return np.array([v[0] * ca - v[1] * sa, v[0] * sa + v[1] * ca])


def _turn(spots, k):
    for _ in range(k):
        spots = {(-y, x) for x, y in spots}
    return spots
