"""Shape maths: placing/buffering cutouts, finger grips, text outlines, sheet splitting, SVG output.

All coordinates are millimetres in the rectified-photo frame (origin top-left, y down).
Rotations are in degrees, clockwise on screen.
"""
import io
import math
import zipfile

import numpy as np
from shapely.affinity import translate
from shapely.geometry import LinearRing, Point, Polygon, box
from shapely.ops import unary_union

BIG = 1e5
CUT_COLOR = "#000000"
TEXT_COLOR = "#0000ff"


def _largest(geom):
    if geom is None or geom.is_empty:
        return None
    if isinstance(geom, Polygon):
        return geom
    polys = [g for g in getattr(geom, "geoms", []) if isinstance(g, Polygon)]
    return max(polys, key=lambda p: p.area) if polys else None


def to_polygon(pts):
    p = Polygon(pts)
    if not p.is_valid:
        p = _largest(p.buffer(0))
    return p


def centroid(pts):
    p = to_polygon(pts)
    if p is None or p.is_empty:
        a = np.asarray(pts, float)
        return [float(a[:, 0].mean()), float(a[:, 1].mean())]
    return [round(p.centroid.x, 3), round(p.centroid.y, 3)]


def rnd(pts):
    return [[round(float(x), 3), round(float(y), 3)] for x, y in pts]


def place(pts, pivot, rot, dx, dy):
    """Rotate pts about pivot, then translate."""
    a = math.radians(rot)
    c, s = math.cos(a), math.sin(a)
    px, py = pivot
    return [[round(px + (x - px) * c - (y - py) * s + dx, 3),
             round(py + (x - px) * s + (y - py) * c + dy, 3)] for x, y in pts]


def simplify_raw(pts, tol=0.1):
    p = to_polygon(pts)
    if p is None:
        return pts
    p = _largest(p.simplify(tol)) or p
    return rnd(p.exterior.coords[:-1])


# ---------------------------------------------------------------- cutouts

def default_grip(raw, existing, r, at="edge"):
    """A new finger grip.

    at='edge':   the point on the outline nearest the shape centre, away from existing grips
                 (for long tools such as wrenches this lands mid-way along a long side)
    at='center': the middle of the tool; the circle only shows where it's wider than the tool,
                 giving a finger slot across it
    """
    p = to_polygon(raw)
    if p is None:
        return None
    c = p.centroid
    if at == "center":
        if not p.contains(c):  # curved tools: the centroid can fall outside
            c = p.point_on_surface()
        return {"x": round(c.x, 3), "y": round(c.y, 3), "r": r, "at": "center"}
    ring = p.exterior
    n = max(int(ring.length / 2), 8)
    cand = [ring.interpolate(i / n, normalized=True) for i in range(n)]
    taken = [Point(g["x"], g["y"]) for g in existing]
    free = [q for q in cand if all(q.distance(t) > 2 * r for t in taken)] or cand
    best = min(free, key=lambda q: q.distance(c))
    return {"x": round(best.x, 3), "y": round(best.y, 3), "r": r, "at": "edge"}


def outline(c):
    """Compute placed outline, final cut line and snapped grip centres for a cutout dict."""
    placed = place(c["raw"], c["pivot"], c["rot"], c["dx"], c["dy"])
    c["placed"] = placed
    c["final"], c["grip_pts"] = [], []
    p = to_polygon(placed)
    if p is None or p.is_empty:
        return c
    smooth, buf = c.get("smooth_mm", 0.0), c.get("buffer_mm", 0.0)
    if smooth > 0:
        p = _largest(p.buffer(smooth, join_style="round").buffer(-smooth, join_style="round")) or p
    if buf:
        # a rectangle keeps square corners; everything else gets rounded ones
        join = "mitre" if c.get("shape") == "rect" else "round"
        p = _largest(p.buffer(buf, quad_segs=8, join_style=join))
        if p is None:
            return c
    ring = p.exterior
    circles = []
    for g in c.get("grips", []):
        gx, gy = place([[g["x"], g["y"]]], c["pivot"], c["rot"], c["dx"], c["dy"])[0]
        # edge grips snap to the (buffered) outline; centre grips stay where they are put
        q = Point(gx, gy) if g.get("at") == "center" else ring.interpolate(ring.project(Point(gx, gy)))
        c["grip_pts"].append([round(q.x, 3), round(q.y, 3)])
        circles.append(q.buffer(g["r"], quad_segs=12))
    if circles:
        p = _largest(unary_union([p] + circles)) or p
    p = _largest(p.simplify(0.05)) or p
    c["final"] = rnd(p.exterior.coords[:-1])
    return c


# ---------------------------------------------------------------- text

FONTS = {"sans": "DejaVu Sans", "serif": "DejaVu Serif", "mono": "DejaVu Sans Mono"}
ALIGNS = ("left", "center", "right")


def text_rings(text, x, y, height_mm, rot, align="center", font="sans", bold=True, italic=False, spacing=1.0):
    """Outline polygons for (multi-line) text whose block is centred on (x, y).

    height_mm is the capital-letter height; lines are aligned left/center/right within the block
    and spaced `spacing` times the normal line height.
    """
    from matplotlib.font_manager import FontProperties
    from matplotlib.path import Path
    from matplotlib.textpath import TextPath, text_to_path

    lines = (text or "").replace("\r", "").split("\n")
    if not "".join(lines).strip():
        return []
    fp = FontProperties(family=FONTS.get(font, FONTS["sans"]), weight="bold" if bold else "normal",
                        style="italic" if italic else "normal")
    cap = TextPath((0, 0), "H", size=100, prop=fp).get_extents().height
    fp.set_size(100 * height_mm / cap)
    pitch = 1.2 * fp.get_size() * max(spacing, 0.1)
    rings = []
    for i, line in enumerate(lines):
        if not line.strip():
            continue
        width = text_to_path.get_text_width_height_descent(line, fp, False)[0]
        x0 = {"left": 0, "right": -width}.get(align, -width / 2)
        cur = []
        for seg, code in TextPath((x0, -i * pitch), line, prop=fp).iter_bezier():
            if code == Path.MOVETO:
                if len(cur) > 2:
                    rings.append(cur)
                cur = [tuple(seg.control_points[0])]
            elif code == Path.CLOSEPOLY:
                if len(cur) > 2:
                    rings.append(cur)
                cur = []
            else:
                steps = 1 if code == Path.LINETO else 8
                cur += [tuple(seg.point_at_t(t)) for t in np.linspace(0, 1, steps + 1)[1:]]
        if len(cur) > 2:
            rings.append(cur)
    if not rings:
        return []
    pts = np.array([p for r in rings for p in r])
    cx, cy = (pts[:, 0].min() + pts[:, 0].max()) / 2, (pts[:, 1].min() + pts[:, 1].max()) / 2
    # font space is y-up: flip, centre the block on (x, y), rotate about it
    return [place([[px - cx + x, cy - py + y] for px, py in r], (x, y), rot, 0, 0) for r in rings]


# ---------------------------------------------------------------- tiling

def joint_protrusion(joint, tab):
    if joint == "jigsaw":
        r = tab / 2
        w = r * 0.55
        return tab * 0.2 + math.sqrt(r * r - w * w) + r
    if joint == "dovetail":
        return tab * 0.8
    return 0.0


def _knob(c, d, joint, tab):
    """Points (along, offset) for one joint centred at `c`, bulging to side d (+1/-1)."""
    if joint == "jigsaw":
        r = tab / 2
        w = r * 0.55
        dx = math.sqrt(r * r - w * w)
        neck = tab * 0.2
        centre = neck + dx
        a0 = math.pi - math.acos(dx / r)
        pts = [(c - w, 0), (c - w, d * neck)]
        steps = 24
        for i in range(1, steps):
            t = -a0 + 2 * a0 * i / steps
            pts.append((c + r * math.sin(t), d * (centre + r * math.cos(t))))
        pts += [(c + w, d * neck), (c + w, 0)]
        return pts
    if joint == "dovetail":
        a, b, depth = tab * 0.35, tab * 0.6, tab * 0.8
        return [(c - a, 0), (c - b, d * depth), (c + b, d * depth), (c + a, 0)]
    return []


def _seam_curve(a0, a1, cross, joint, tab, spacing, seam_idx, shift):
    """Seam running from a0 to a1 (along-seam coords), joints between the crossing seams."""
    pts = [(a0 - 1.0, 0.0)]
    if joint != "straight":
        stops = [a0] + sorted(cross) + [a1]
        n = 0
        for s0, s1 in zip(stops, stops[1:]):
            seg = s1 - s0
            if seg < tab * 2.5:
                continue
            k = max(1, round(seg / spacing))
            lo, hi = s0 + tab * 1.2, s1 - tab * 1.2
            for m in range(k):
                c = min(max(s0 + seg * (m + 0.5) / k + shift, lo), hi)
                d = 1 if (n + seam_idx) % 2 == 0 else -1
                pts += _knob(c, d, joint, tab)
                n += 1
    pts.append((a1 + 1.0, 0.0))
    return pts


def _before_seam(pos, curve, vertical):
    """Polygon covering everything on the low side of a seam (left of / above)."""
    if vertical:
        pts = [(pos + o, a) for a, o in curve] + [(-BIG, curve[-1][0]), (-BIG, curve[0][0])]
    else:
        pts = [(a, pos + o) for a, o in curve] + [(curve[-1][0], -BIG), (curve[0][0], -BIG)]
    return Polygon(pts)


def _pad(vals, n):
    vals = [float(v) for v in (vals or [])][:n]
    return vals + [0.0] * (n - len(vals))


def grid_size(W, H, s):
    prot = joint_protrusion(s["joint"], s["tab_mm"])

    def count(length, bed, override):
        if override and override > 0:
            return int(override)
        if length <= bed:
            return 1
        return max(1, math.ceil(length / max(bed - 2 * prot - 2, 1)))

    return count(W, s["bed_w"], s.get("nx")), count(H, s["bed_h"], s.get("ny"))


def seam_layout(base, s):
    """Seam positions for a base rectangle, including the user's nudges."""
    nx, ny = grid_size(base["w"], base["h"], s)
    off, shift = s.get("seam_off", {}), s.get("tab_shift", {})
    ov, oh = _pad(off.get("v"), nx - 1), _pad(off.get("h"), ny - 1)
    return {
        "nx": nx, "ny": ny,
        "xs": [base["x"] + base["w"] * i / nx + ov[i - 1] for i in range(1, nx)],
        "ys": [base["y"] + base["h"] * j / ny + oh[j - 1] for j in range(1, ny)],
        "seam_off": {"v": ov, "h": oh},
        "tab_shift": {"v": _pad(shift.get("v"), nx - 1), "h": _pad(shift.get("h"), ny - 1)},
    }


def build_tiles(base, clip, holes, s):
    """Split the clip rectangle into pieces using seams laid out over `base`.

    Returns (layout, list of dicts: row, col, geom, fits).
    """
    L = seam_layout(base, s)
    xs, ys = L["xs"], L["ys"]
    joint, tab, spacing = s["joint"], s["tab_mm"], s["tab_spacing_mm"]
    y0, y1 = base["y"], base["y"] + base["h"]
    x0, x1 = base["x"], base["x"] + base["w"]
    left = [_before_seam(x, _seam_curve(y0, y1, ys, joint, tab, spacing, i, L["tab_shift"]["v"][i]), True)
            for i, x in enumerate(xs)]
    above = [_before_seam(y, _seam_curve(x0, x1, xs, joint, tab, spacing, j + 1, L["tab_shift"]["h"][j]), False)
             for j, y in enumerate(ys)]
    sheet = box(clip["x"], clip["y"], clip["x"] + clip["w"], clip["y"] + clip["h"])
    if holes is not None:
        sheet = sheet.difference(holes)

    tiles = []
    for r in range(L["ny"]):
        for c in range(L["nx"]):
            g = sheet
            if c < L["nx"] - 1:
                g = g.intersection(left[c])
            if c > 0:
                g = g.difference(left[c - 1])
            if r < L["ny"] - 1:
                g = g.intersection(above[r])
            if r > 0:
                g = g.difference(above[r - 1])
            if g.is_empty or g.area < 1:
                continue
            minx, miny, maxx, maxy = g.bounds
            fits = (maxx - minx) <= s["bed_w"] + 1e-6 and (maxy - miny) <= s["bed_h"] + 1e-6
            tiles.append({"row": r + 1, "col": c + 1, "geom": g, "fits": fits})
    return L, tiles


# ---------------------------------------------------------------- output

def polygons_of(geom):
    if geom is None or geom.is_empty:
        return []
    if isinstance(geom, Polygon):
        return [geom]
    return [g for g in getattr(geom, "geoms", []) if isinstance(g, Polygon)]


def rings_of(geom):
    rings = []
    for p in polygons_of(geom):
        rings.append([list(c) for c in p.exterior.coords[:-1]])
        rings += [[list(c) for c in i.coords[:-1]] for i in p.interiors]
    return rings


def lines_of(geom):
    if geom is None or geom.is_empty:
        return []
    parts = getattr(geom, "geoms", [geom])
    return [[list(c) for c in g.coords] for g in parts if g.geom_type in ("LineString", "LinearRing")]


def clip_text(rings, area):
    """Text outlines cut to an area. Returns (closed rings, open polylines)."""
    closed, open_ = [], []
    for r in rings:
        ring = LinearRing(r)
        if area.contains(ring):
            closed.append(r)
        elif area.intersects(ring):
            open_ += lines_of(ring.intersection(area))
    return closed, open_


def _shift(paths, dx, dy):
    return [[[x + dx, y + dy] for x, y in p] for p in paths]


def _path(pts, close=True):
    return "M" + " L".join(f"{x:.3f},{y:.3f}" for x, y in pts) + (" Z" if close else "")


def svg_document(width, height, cut_rings, text_closed=(), text_open=()):
    """SVG with real mm units so LightBurn/Inkscape import it at true size.

    Cut lines are black, text is blue, so they land on separate LightBurn layers.
    """
    def group(color, body):
        return f'<g fill="none" stroke="{color}" stroke-width="0.1">\n{body}\n</g>\n' if body else ""

    cuts = "\n".join(f'  <path d="{_path(r)}"/>' for r in cut_rings if len(r) >= 3)
    text = "\n".join([f'  <path d="{_path(r)}"/>' for r in text_closed if len(r) >= 3] +
                     [f'  <path d="{_path(r, False)}"/>' for r in text_open if len(r) >= 2])
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width:.3f}mm" height="{height:.3f}mm" '
        f'viewBox="0 0 {width:.3f} {height:.3f}">\n'
        + group(CUT_COLOR, cuts) + group(TEXT_COLOR, text) + "</svg>\n"
    )


def holes_union(cutouts):
    polys = []
    for c in cutouts:
        if c.get("include", True) and len(c.get("final", [])) >= 3:
            p = to_polygon(c["final"])
            if p is not None:
                polys.append(p)
    return unary_union(polys) if polys else None


def all_text_rings(texts):
    return [r for t in texts for r in t.get("rings", [])]


def export_sheet_svg(cutouts, texts, sheet, include_outline):
    x, y, W, H = sheet["x"], sheet["y"], sheet["w"], sheet["h"]
    area = box(x, y, x + W, y + H)
    holes = holes_union(cutouts)
    if include_outline:
        cuts = rings_of(area.difference(holes) if holes is not None else area)
    else:
        cuts = rings_of(holes)
    closed, open_ = clip_text(all_text_rings(texts), area)
    return svg_document(W, H, _shift(cuts, -x, -y), _shift(closed, -x, -y), _shift(open_, -x, -y))


def export_tiles_zip(cutouts, texts, sheet, base, settings):
    _, tiles = build_tiles(base, sheet, holes_union(cutouts), settings)
    trings = all_text_rings(texts)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        all_cuts, all_closed, all_open = [], [], []
        for t in tiles:
            g = t["geom"]
            cuts = rings_of(g)
            closed, open_ = clip_text(trings, g)
            all_cuts += cuts
            all_closed += closed
            all_open += open_
            minx, miny, maxx, maxy = g.bounds
            z.writestr(f"tile_r{t['row']}_c{t['col']}.svg",
                       svg_document(maxx - minx, maxy - miny, _shift(cuts, -minx, -miny),
                                    _shift(closed, -minx, -miny), _shift(open_, -minx, -miny)))
        sx, sy = -sheet["x"], -sheet["y"]
        z.writestr("overview_all_tiles.svg", svg_document(
            sheet["w"], sheet["h"], _shift(all_cuts, sx, sy), _shift(all_closed, sx, sy), _shift(all_open, sx, sy)))
    return buf.getvalue(), tiles


# ---------------------------------------------------------------- brush

def brush(polygons, stroke, radius, erase=False):
    """Paint a round brush stroke into (or out of) outlines. Returns the resulting outlines.

    Adding merges the stroke with the outlines it touches (or makes a new one); erasing cuts it away
    and may split an outline in two. Only outer boundaries are kept, like every cut line.
    """
    from shapely.geometry import LineString
    if not stroke:
        return polygons
    pen = (Point(stroke[0]) if len(stroke) == 1 else LineString(stroke)).buffer(max(radius, 0.05), quad_segs=8)
    shapes = [p for p in (to_polygon(q) for q in polygons if len(q) >= 3) if p is not None and not p.is_empty]
    if erase:
        out = [p.difference(pen) for p in shapes]
    else:
        touching = [p for p in shapes if p.intersects(pen)]
        out = [p for p in shapes if not p.intersects(pen)] + [unary_union(touching + [pen])]
    polys = []
    for g in out:
        parts = [g] if isinstance(g, Polygon) else [q for q in getattr(g, "geoms", []) if isinstance(q, Polygon)]
        polys += [rnd(q.exterior.coords[:-1]) for q in parts if q.area > 1.0]
    return polys
