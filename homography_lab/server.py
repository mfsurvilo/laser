"""Homography Lab: a small test bench for stitching several photos of one drawer into a
top-down, millimetre-scaled image.

    python homography_lab/server.py [photo_dir]     (default: homeography/)

Then open http://127.0.0.1:5001.

Workflow: label a rectangle of known size in one photo (the "frame"), align the other
photos to it (feature matching, or hand-placed point pairs), then build the mosaic.
A homography is exact only on one plane (the foam floor, or the drawer rim): anything
standing up off that plane will not line up perfectly between photos.
"""
import io
import json
import sys
import time
from pathlib import Path

import cv2
import numpy as np
from flask import Flask, abort, jsonify, request, send_file, send_from_directory
from PIL import Image, ImageOps

ROOT = Path(__file__).resolve().parent.parent
HERE = Path(__file__).resolve().parent
PHOTO_DIR = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else ROOT / "homeography"
CACHE = ROOT / "output" / "homography_lab"
SRC = CACHE / "src"
LABELS = PHOTO_DIR / "labels.json"
EXTS = {".jpg", ".jpeg", ".png"}
ALIGN_SIDE = 2400      # photos are downscaled to this long side for feature matching
MIN_INLIERS = 60       # automatic alignment needs this many agreeing matches...
MIN_RATIO = 0.15       # ...and this share of all matches (repeated wrenches give false sets)
MAX_CANVAS_SIDE = 6000

app = Flask(__name__)


# ---------- image io ----------

def imread(path):
    return cv2.imdecode(np.fromfile(str(path), dtype=np.uint8), cv2.IMREAD_COLOR)


def imwrite(path, img, quality=90):
    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, quality])
    buf.tofile(str(path))


def photos():
    return sorted(p.name for p in PHOTO_DIR.iterdir() if p.suffix.lower() in EXTS)


def src_path(name):
    """EXIF-rotated copy of a photo, so pixel coordinates match what the browser shows."""
    if name not in photos():
        abort(404)
    out = SRC / (Path(name).stem + ".jpg")
    orig = PHOTO_DIR / name
    if not out.exists() or out.stat().st_mtime < orig.stat().st_mtime:
        SRC.mkdir(parents=True, exist_ok=True)
        ImageOps.exif_transpose(Image.open(orig)).convert("RGB").save(out, "JPEG", quality=95)
    return out


def load_labels():
    if LABELS.exists():
        return json.loads(LABELS.read_text(encoding="utf-8"))
    return {"frame": None, "photos": {}}


def save_labels(d):
    LABELS.write_text(json.dumps(d, indent=1), encoding="utf-8")


def result_url(name, img):
    CACHE.mkdir(parents=True, exist_ok=True)
    imwrite(CACHE / name, img)
    return f"/out/{name}?t={time.time():.0f}"


# ---------- geometry ----------

def order_corners(pts):
    """TL, TR, BR, BL in image coordinates (y down). Same rule as the main app."""
    pts = np.asarray(pts, dtype=np.float64)
    c = pts.mean(axis=0)
    pts = pts[np.argsort(np.arctan2(pts[:, 1] - c[1], pts[:, 0] - c[0]))]
    return np.roll(pts, -int(np.argmin(pts[:, 0] + pts[:, 1])), axis=0)


def ref_homography(ref):
    """Photo pixels -> millimetres, with the reference's TL corner at (0, 0)."""
    src = order_corners(ref["pts"]).astype(np.float32)
    w, h = ref["w_mm"], ref["h_mm"]
    dst = np.float32([[0, 0], [w, 0], [w, h], [0, h]])
    return cv2.getPerspectiveTransform(src, dst)


def apply_h(H, pts):
    pts = np.asarray(pts, dtype=np.float64).reshape(-1, 1, 2)
    return cv2.perspectiveTransform(pts, H).reshape(-1, 2)


def mm_scale(ppm, margin):
    """mm -> output pixels, with `margin` mm of room left/top of the reference."""
    return np.array([[ppm, 0, margin * ppm], [0, ppm, margin * ppm], [0, 0, 1]], dtype=np.float64)


def frame_ref(labels):
    f = labels.get("frame")
    if not f:
        abort(400, "Pick a frame reference first (the circle button on a labelled rectangle).")
    for r in labels["photos"].get(f["photo"], {}).get("refs", []):
        if r["id"] == f["ref"]:
            if len(r["pts"]) != 4 or not r.get("w_mm") or not r.get("h_mm"):
                abort(400, "The frame reference needs 4 corners, a width and a height.")
            return f["photo"], r
    abort(400, "The frame reference no longer exists.")


def to_frame(labels, name, base):
    """Homography from this photo's pixels to frame-photo pixels, or None if not aligned."""
    if name == base:
        return np.eye(3)
    a = labels["photos"].get(name, {}).get("align")
    return np.array(a["H"]) if a and a.get("H") else None


def ref_check(H_mm, ref):
    """How far a labelled rectangle's measured size is from what was typed in."""
    c = apply_h(H_mm, order_corners(ref["pts"]))
    sides = [np.linalg.norm(c[(i + 1) % 4] - c[i]) for i in range(4)]
    w, h = (sides[0] + sides[2]) / 2, (sides[1] + sides[3]) / 2
    diag = [np.linalg.norm(c[2] - c[0]), np.linalg.norm(c[3] - c[1])]
    return {"name": ref.get("name") or "ref", "typed": [ref["w_mm"], ref["h_mm"]],
            "measured": [round(w, 1), round(h, 1)],
            "sides": [round(s, 1) for s in sides],
            "diag_diff": round(abs(diag[0] - diag[1]), 1)}


# ---------- feature matching ----------

_feat_cache = {}


def features(name):
    """RootSIFT keypoints (in full-res pixel coords) and descriptors, cached per photo."""
    path = src_path(name)
    key = (name, path.stat().st_mtime)
    if key not in _feat_cache:
        img = imread(path)
        s = min(1.0, ALIGN_SIDE / max(img.shape[:2]))
        small = cv2.resize(img, None, fx=s, fy=s, interpolation=cv2.INTER_AREA) if s < 1 else img
        gray = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8)).apply(cv2.cvtColor(small, cv2.COLOR_BGR2GRAY))
        kp, des = cv2.SIFT_create(nfeatures=0, contrastThreshold=0.02).detectAndCompute(gray, None)
        des = np.sqrt(des / (des.sum(axis=1, keepdims=True) + 1e-7)).astype(np.float32)
        _feat_cache[key] = (np.float32([k.pt for k in kp]) / s, des)
    return _feat_cache[key]


def match(a, b, H0=None, radius=60.0, ratio=0.8, thresh=8.0):
    """Homography a->b from feature matches. With a rough H0 (from hand-placed pairs), only
    matches landing within `radius` px of where H0 predicts are considered, which gets
    around the look-alike wrenches. Returns (H, inliers, candidates, rms_px, inlier_pts_in_b)."""
    (p1, d1), (p2, d2) = features(a), features(b)
    if H0 is None:
        knn = cv2.FlannBasedMatcher({"algorithm": 1, "trees": 5}, {"checks": 64}).knnMatch(d1, d2, k=2)
        good = [m for m, n in (x for x in knn if len(x) == 2) if m.distance < ratio * n.distance]
        src, dst = p1[[m.queryIdx for m in good]], p2[[m.trainIdx for m in good]]
    else:
        pred = apply_h(H0, p1)
        grid = {}
        for j, (x, y) in enumerate(p2):
            grid.setdefault((int(x // radius), int(y // radius)), []).append(j)
        src, dst = [], []
        for i, (px, py) in enumerate(pred):
            gx, gy = int(px // radius), int(py // radius)
            cand = [j for dx in (-1, 0, 1) for dy in (-1, 0, 1) for j in grid.get((gx + dx, gy + dy), ())]
            cand = [j for j in cand if np.hypot(p2[j, 0] - px, p2[j, 1] - py) < radius]
            if not cand:
                continue
            dist = np.linalg.norm(d2[cand] - d1[i], axis=1)
            o = np.argsort(dist)
            if len(o) == 1 or dist[o[0]] < 0.9 * dist[o[1]]:
                src.append(p1[i])
                dst.append(p2[cand[o[0]]])
        src, dst = np.float32(src).reshape(-1, 2), np.float32(dst).reshape(-1, 2)
    if len(src) < 8:
        return None
    H, mask = cv2.findHomography(src, dst, cv2.USAC_MAGSAC, thresh, maxIters=50000)
    if H is None:
        return None
    inl = mask.ravel().astype(bool)
    err = np.linalg.norm(apply_h(H, src[inl]) - dst[inl], axis=1)
    return H, int(inl.sum()), len(src), float(np.sqrt((err ** 2).mean())), dst[inl]


def sane(H, shape):
    """Reject fits that fold, mirror or wildly resize the photo."""
    h, w = shape[:2]
    q = apply_h(H, [[0, 0], [w, 0], [w, h], [0, h]])
    e = np.roll(q, -1, axis=0) - q
    turns = e[:, 0] * np.roll(e, -1, axis=0)[:, 1] - e[:, 1] * np.roll(e, -1, axis=0)[:, 0]
    area = cv2.contourArea(q.astype(np.float32))
    return all(t > 0 for t in turns) and 0.01 * w * h < area < 25 * w * h


# ---------- routes ----------

@app.get("/")
def index():
    return send_from_directory(HERE, "index.html")


@app.get("/photo/<name>")
def photo(name):
    return send_file(src_path(name))


@app.get("/out/<name>")
def out(name):
    return send_from_directory(CACHE, name)


@app.get("/api/state")
def state():
    items = []
    for n in photos():
        with Image.open(src_path(n)) as im:
            items.append({"name": n, "w": im.width, "h": im.height})
    return jsonify(photos=items, labels=load_labels(), photo_dir=str(PHOTO_DIR))


@app.post("/api/labels")
def put_labels():
    save_labels(request.get_json())
    return jsonify(ok=True)


@app.post("/api/rectify")
def rectify():
    """Warp one photo top-down using one of its own references."""
    a = request.get_json()
    labels = load_labels()
    ref = next(r for r in labels["photos"][a["photo"]]["refs"] if r["id"] == a["ref"])
    margin = float(a.get("margin", 20))
    c = order_corners(ref["pts"])
    native = max(np.linalg.norm(c[1] - c[0]) / ref["w_mm"], np.linalg.norm(c[2] - c[1]) / ref["h_mm"])
    ppm = min(pick_ppm(a.get("ppm"), native), MAX_CANVAS_SIDE / (max(ref["w_mm"], ref["h_mm"]) + 2 * margin))
    H = mm_scale(ppm, margin) @ ref_homography(ref)
    size = (int((ref["w_mm"] + 2 * margin) * ppm), int((ref["h_mm"] + 2 * margin) * ppm))
    img = cv2.warpPerspective(imread(src_path(a["photo"])), H, size, borderValue=(40, 40, 40))
    return jsonify(url=result_url("rectified.jpg", img), ppm=ppm, margin=margin)


@app.post("/api/align")
def align():
    """Align a photo to the frame photo. With 4+ hand-placed point pairs, those give the
    homography (refined by guided feature matching). Otherwise feature matching is tried
    against the frame photo and every photo already aligned, and the best one wins."""
    a = request.get_json()
    labels = load_labels()
    base, _ = frame_ref(labels)
    name = a["photo"]
    if name == base:
        abort(400, "That is the frame photo; it doesn't need aligning.")
    img = imread(src_path(name))
    rec = labels["photos"].setdefault(name, {"refs": []})
    pairs = rec.get("pairs") or {}
    complete = [pr for pr in pairs.get("pts", []) if pr[0] and pr[1]]
    info = None

    if len(complete) >= 4:
        target = pairs["target"]
        T = to_frame(labels, target, base)
        if T is None:
            abort(400, f"The pair target {target[:8]} isn't aligned itself yet.")
        pp = np.float32(complete)
        H0, _ = cv2.findHomography(pp[:, 0], pp[:, 1], 0)
        if H0 is None or not sane(H0, img.shape):
            abort(400, "The point pairs don't form a sensible homography. Check each pair is the same spot in both photos.")

        def pair_err(H):
            return float(np.sqrt((np.linalg.norm(apply_h(H, pp[:, 0]) - pp[:, 1], axis=1) ** 2).mean()))

        res = match(name, target, H0=H0)
        if res and res[1] >= 15 and sane(res[0], img.shape) and pair_err(res[0]) < 25:
            H, inl, n, rms, pts = res
            how = f"point pairs + {inl} guided feature matches"
        else:
            H, inl, n, rms, pts = H0, len(pp), len(pp), pair_err(H0), pp[:, 1]
            how = "point pairs only (feature refinement found nothing consistent)"
        info = {"H": (T @ H).tolist(), "via": target, "how": how, "inliers": inl, "matches": n,
                "rms_px": round(rms, 2), "pair_rms_px": round(pair_err(H), 1)}
    else:
        tried = []
        for t in [base] + [n for n in photos() if n not in (base, name) and to_frame(labels, n, base) is not None]:
            res = match(name, t)
            ok = res is not None and res[1] >= MIN_INLIERS and res[1] >= MIN_RATIO * res[2] and sane(res[0], img.shape)
            tried.append(f"{t[:4]} {res[1] if res else 0}/{res[2] if res else 0}{'' if ok else ' (rejected)'}")
            if ok and (info is None or res[1] > info["inliers"]):
                T = to_frame(labels, t, base)
                H, inl, n, rms, pts = res
                info = {"H": (T @ H).tolist(), "via": t, "how": "feature matching", "inliers": inl,
                        "matches": n, "rms_px": round(rms, 2)}
                best_pts, best_T = pts, T
        if info is None:
            abort(400, f"No reliable feature match ({', '.join(tried)}; need {MIN_INLIERS}+ and {MIN_RATIO:.0%}+ agreeing). "
                       "Place 4+ point pairs by hand.")
        pts, T = best_pts, best_T
        info["tried"] = ", ".join(tried)

    info["when"] = time.strftime("%H:%M:%S")
    rec["align"] = info
    save_labels(labels)

    # Overlay check: the aligned photo blended onto the frame photo, plus the inlier points.
    base_img = imread(src_path(base))
    Hb = np.array(info["H"])
    bs = (base_img.shape[1], base_img.shape[0])
    warped = cv2.warpPerspective(img, Hb, bs)
    cover = cv2.warpPerspective(np.full(img.shape[:2], 255, np.uint8), Hb, bs) > 0
    over = base_img.copy()
    over[cover] = (0.5 * base_img[cover] + 0.5 * warped[cover]).astype(np.uint8)
    h, w = img.shape[:2]
    cv2.polylines(over, [apply_h(Hb, [[0, 0], [w, 0], [w, h], [0, h]]).astype(np.int32)], True, (0, 255, 255), 8)
    pts = apply_h(T, pts)
    for x, y in pts[::max(1, len(pts) // 400)]:
        cv2.circle(over, (int(x), int(y)), 6, (0, 0, 255), -1)
    info["overlay"] = result_url(f"overlay_{Path(name).stem}.jpg", over)
    return jsonify(info)


@app.post("/api/unalign")
def unalign():
    labels = load_labels()
    labels["photos"].get(request.get_json()["photo"], {}).pop("align", None)
    save_labels(labels)
    return jsonify(ok=True)


COLORS = np.array([[230, 159, 0], [86, 180, 233], [0, 158, 115], [240, 228, 66],
                   [0, 114, 178], [213, 94, 0], [204, 121, 167], [150, 150, 150]], np.uint8)[:, ::-1]
MAX_EXPORT_PIXELS = 150e6      # ~450 MB of RGB; beyond this, lower the resolution
STRIP = 512                    # rows rendered at a time, to keep memory bounded
PDF_MAX_DPI = 300              # printers gain nothing past this


def layers(labels, skip=()):
    """The frame reference, and (name, photo px -> frame mm homography) per aligned photo."""
    base, ref = frame_ref(labels)
    Hf = ref_homography(ref)
    out = []
    for n in photos():
        T = to_frame(labels, n, base)
        if T is not None and n not in skip:
            out.append((n, Hf @ T))
    return ref, out


def native_ppm(ref, lay):
    """The most detail any photo holds on the reference (source pixels per mm)."""
    g = np.array([[x, y] for x in np.linspace(0, ref["w_mm"], 12) for y in np.linspace(0, ref["h_mm"], 12)])
    best = 0.0
    for n, Hm in lay:
        M = np.linalg.inv(Hm)
        with Image.open(src_path(n)) as im:
            w, h = im.size
        src = apply_h(M, g)
        ok = (src[:, 0] >= 0) & (src[:, 1] >= 0) & (src[:, 0] < w) & (src[:, 1] < h)
        if ok.any():
            d = M[2, 0] * g[ok, 0] + M[2, 1] * g[ok, 1] + M[2, 2]
            best = max(best, float(np.sqrt(abs(np.linalg.det(M)) / np.abs(d) ** 3).max()))
    return best


def render(ref, lay, ppm, margin, with_map=False):
    """Warp every photo into the frame's mm plane. At each output pixel keep the photo that
    saw that spot in the most detail (usually the closest close-up). Rendered in strips so
    full-resolution exports fit in memory."""
    size = (int(round((ref["w_mm"] + 2 * margin) * ppm)), int(round((ref["h_mm"] + 2 * margin) * ppm)))
    if size[0] * size[1] > MAX_EXPORT_PIXELS:
        abort(400, f"{size[0]}x{size[1]} px is too big; lower the resolution.")
    if min(size) < 1:
        abort(400, "The output is empty; check the frame size and margin.")
    S = mm_scale(ppm, margin)
    imgs = [(imread(src_path(n)), S @ Hm) for n, Hm in lay]
    canvas = np.full((size[1], size[0], 3), 40, np.uint8)
    srcmap = np.full((size[1], size[0]), -1, np.int16) if with_map else None
    xs = np.arange(size[0], dtype=np.float32)[None, :]
    for y0 in range(0, size[1], STRIP):
        rows = min(STRIP, size[1] - y0)
        shift = np.array([[1, 0, 0], [0, 1, -y0], [0, 0, 1]], np.float64)
        ys = np.arange(y0, y0 + rows, dtype=np.float32)[:, None]
        best = np.zeros((rows, size[0]), np.float32)
        for i, (img, H) in enumerate(imgs):
            Hs = shift @ H
            valid = cv2.warpPerspective(np.full(img.shape[:2], 255, np.uint8), Hs, (size[0], rows),
                                        flags=cv2.INTER_NEAREST)
            if not valid.any():
                continue
            valid = cv2.erode(valid, np.ones((5, 5), np.uint8)) > 0
            M = np.linalg.inv(H)   # source pixels per output pixel = sqrt|det J| of the inverse map
            w = M[2, 0] * xs + M[2, 1] * ys + M[2, 2]
            score = np.sqrt(abs(np.linalg.det(M)) / np.maximum(np.abs(w), 1e-9) ** 3).astype(np.float32)
            take = valid & (score > best)
            if not take.any():
                continue
            warped = cv2.warpPerspective(img, Hs, (size[0], rows), flags=cv2.INTER_LANCZOS4)
            canvas[y0:y0 + rows][take] = warped[take]
            best[take] = score[take]
            if with_map:
                srcmap[y0:y0 + rows][take] = i
    return canvas, srcmap


def pick_ppm(value, native):
    return native if value in (None, "", "auto") else float(value)


@app.post("/api/mosaic")
def mosaic():
    """Preview mosaic (JPEG, capped at MAX_CANVAS_SIDE) plus a map of which photo was used where."""
    a = request.get_json()
    labels = load_labels()
    ref, lay = layers(labels, set(a.get("skip", [])))
    margin = float(a.get("margin", 20))
    native = native_ppm(ref, lay)
    ppm = min(pick_ppm(a.get("ppm"), native), MAX_CANVAS_SIDE / (max(ref["w_mm"], ref["h_mm"]) + 2 * margin))
    canvas, srcmap = render(ref, lay, ppm, margin, with_map=True)
    checks = [{"photo": n, **ref_check(Hm, r)} for n, Hm in lay
              for r in labels["photos"].get(n, {}).get("refs", [])
              if len(r["pts"]) == 4 and r.get("w_mm") and r.get("h_mm")]
    smap = np.full_like(canvas, 40)
    smap[srcmap >= 0] = COLORS[srcmap[srcmap >= 0] % len(COLORS)]
    smap = cv2.addWeighted(canvas, 0.45, smap, 0.55, 0)
    return jsonify(url=result_url("mosaic.jpg", canvas), srcmap=result_url("mosaic_sources.jpg", smap),
                   ppm=ppm, native_ppm=round(native, 2), margin=margin, used=[n for n, _ in lay],
                   px=[canvas.shape[1], canvas.shape[0]],
                   colors=["#%02x%02x%02x" % tuple(int(v) for v in c[::-1]) for c in COLORS], checks=checks)


PAPER_MM = {"letter": (215.9, 279.4), "a4": (210.0, 297.0), "legal": (215.9, 355.6), "tabloid": (279.4, 431.8)}


def tile_pdf(canvas, ppm, paper, border_mm=8.0, overlap_mm=10.0):
    """Split the mosaic over pages at exact 1:1 scale. Each page gets a cut line, its grid
    label, its neighbours and a 50 mm scale bar to check the printer didn't rescale."""
    from PIL import ImageDraw
    pw, ph = PAPER_MM[paper]
    H, W = canvas.shape[:2]

    def px(mm):
        return int(round(mm * ppm))

    def count(total, tile):
        step = tile - px(overlap_mm)
        return max(1, int(np.ceil((total - px(overlap_mm)) / step)))

    def layout(pw, ph):
        tw, th = px(pw - 2 * border_mm), px(ph - 2 * border_mm - 8)   # 8 mm strip for labels
        return tw, th, count(W, tw), count(H, th)

    # portrait or landscape, whichever needs fewer sheets
    a, b = layout(pw, ph), layout(ph, pw)
    if b[2] * b[3] < a[2] * a[3]:
        pw, ph, (tw, th, cols, rows) = ph, pw, b
    else:
        tw, th, cols, rows = a
    step_x, step_y = tw - px(overlap_mm), th - px(overlap_mm)
    rgb = cv2.cvtColor(canvas, cv2.COLOR_BGR2RGB)

    def name(r, c):
        return f"{chr(65 + r)}{c + 1}"

    pages = []
    for r in range(rows):
        for c in range(cols):
            page = Image.new("RGB", (px(pw), px(ph)), "white")
            tile = rgb[r * step_y:r * step_y + th, c * step_x:c * step_x + tw]
            ox, oy = px(border_mm), px(border_mm + 8)
            page.paste(Image.fromarray(tile), (ox, oy))
            d = ImageDraw.Draw(page)
            d.rectangle([ox - 1, oy - 1, ox + tile.shape[1], oy + tile.shape[0]], outline="black", width=max(1, px(0.2)))
            nb = [f"{lbl} {name(rr, cc)}" for lbl, rr, cc in
                  (("above", r - 1, c), ("below", r + 1, c), ("left", r, c - 1), ("right", r, c + 1))
                  if 0 <= rr < rows and 0 <= cc < cols]
            d.text((ox, px(border_mm)), f"Page {name(r, c)}  ({rows} x {cols})   overlap {overlap_mm:.0f} mm   "
                   + "   ".join(nb), fill="black", font_size=px(3.2))
            bx, by = px(pw - border_mm - 50), px(border_mm + 4)
            d.rectangle([bx, by, bx + px(50), by + px(1.5)], fill="black")
            d.text((bx, px(border_mm)), "50 mm (check with a ruler)", fill="black", font_size=px(2.8))
            pages.append(page)
    buf = io.BytesIO()
    pages[0].save(buf, "PDF", save_all=True, append_images=pages[1:], resolution=ppm * 25.4)
    buf.seek(0)
    return buf, rows, cols


@app.get("/api/export")
def export():
    """Full-quality mosaic as a download: png or jpg with the DPI set so it prints at 1:1,
    or a pdf tiled over sheets of paper at exact scale."""
    a = request.args
    fmt = a.get("fmt", "png")
    if fmt not in ("png", "jpg", "pdf"):
        abort(400, "fmt must be png, jpg or pdf")
    paper = a.get("paper", "letter")
    if paper not in PAPER_MM:
        abort(400, "unknown paper size")
    labels = load_labels()
    ref, lay = layers(labels)
    margin = float(a.get("margin", 20))
    ppm = pick_ppm(a.get("ppm"), native_ppm(ref, lay))
    if fmt == "pdf":
        ppm = min(ppm, PDF_MAX_DPI / 25.4)
    canvas, _ = render(ref, lay, ppm, margin)
    dpi = ppm * 25.4
    stem = f"mosaic_{ref['w_mm'] + 2 * margin:.0f}x{ref['h_mm'] + 2 * margin:.0f}mm_{dpi:.0f}dpi"
    if fmt == "pdf":
        buf, rows, cols = tile_pdf(canvas, ppm, paper)
        return send_file(buf, "application/pdf", as_attachment=True,
                         download_name=f"{stem}_{paper}_{rows * cols}pages.pdf")
    img = Image.fromarray(cv2.cvtColor(canvas, cv2.COLOR_BGR2RGB))
    buf = io.BytesIO()
    if fmt == "jpg":
        img.save(buf, "JPEG", quality=97, subsampling=0, dpi=(dpi, dpi))
    else:
        img.save(buf, "PNG", dpi=(dpi, dpi), compress_level=3)
    buf.seek(0)
    return send_file(buf, "image/jpeg" if fmt == "jpg" else "image/png", as_attachment=True,
                     download_name=f"{stem}.{fmt}")


if __name__ == "__main__":
    print(f"Photos: {PHOTO_DIR}")
    app.run(debug=True, port=5001)
