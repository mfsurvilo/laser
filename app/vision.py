"""Image handling: upload normalisation, perspective correction, outline detection."""
import cv2
import numpy as np
from PIL import Image, ImageOps

MAX_SIDE_PX = 4000   # cap on rectified image size
MAX_PPM = 10.0       # cap on rectified resolution (pixels per mm)


def imread(path):
    # np.fromfile + imdecode survives non-ASCII Windows paths; cv2.imread does not
    return cv2.imdecode(np.fromfile(str(path), dtype=np.uint8), cv2.IMREAD_COLOR)


def imwrite(path, img):
    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 92])
    if not ok:
        raise RuntimeError("could not encode image")
    buf.tofile(str(path))


def save_upload(stream, dest_path):
    """Apply phone EXIF rotation and save as RGB JPEG. Returns (w, h)."""
    img = ImageOps.exif_transpose(Image.open(stream)).convert("RGB")
    img.save(dest_path, "JPEG", quality=92)
    return img.size


def order_corners(pts):
    """Return corners as TL, TR, BR, BL (screen coordinates, y down)."""
    pts = np.asarray(pts, dtype=np.float32)
    c = pts.mean(axis=0)
    pts = pts[np.argsort(np.arctan2(pts[:, 1] - c[1], pts[:, 0] - c[0]))]  # clockwise on screen
    start = int(np.argmin(pts[:, 0] + pts[:, 1]))
    return np.roll(pts, -start, axis=0)


def rectify(src_path, dst_path, corners, width_mm, height_mm, margin_mm):
    """Warp the photo so the clicked rectangle becomes width_mm x height_mm.

    The output image has the rectangle's top-left at (margin, margin) mm.
    Returns pixels-per-mm of the output.
    """
    img = imread(src_path)
    src = order_corners(corners)
    top, right = np.linalg.norm(src[1] - src[0]), np.linalg.norm(src[2] - src[1])
    bottom, left = np.linalg.norm(src[2] - src[3]), np.linalg.norm(src[3] - src[0])
    ppm = (max(top, bottom) / width_mm + max(left, right) / height_mm) / 2
    total_w, total_h = width_mm + 2 * margin_mm, height_mm + 2 * margin_mm
    ppm = min(ppm, MAX_PPM, MAX_SIDE_PX / max(total_w, total_h))

    m = margin_mm
    dst = np.float32([[m, m], [m + width_mm, m], [m + width_mm, m + height_mm], [m, m + height_mm]]) * ppm
    H = cv2.getPerspectiveTransform(src, dst)
    size = (int(round(total_w * ppm)), int(round(total_h * ppm)))
    out = cv2.warpPerspective(img, H, size, flags=cv2.INTER_LINEAR, borderValue=(40, 40, 40))
    imwrite(dst_path, out)
    return float(ppm)


def _clean(mask, smooth_px):
    if smooth_px >= 1:
        k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * smooth_px + 1, 2 * smooth_px + 1))
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, k)
        mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, k)
    return mask


def _contour_to_mm(cnt, ppm, simplify_px):
    cnt = cv2.approxPolyDP(cnt, max(simplify_px, 0.5), True)
    return [[round(float(x) / ppm, 3), round(float(y) / ppm, 3)] for x, y in cnt.reshape(-1, 2)]


def crop(path, x, y, w, h):
    """Crop the photo in place. Returns the new (w, h)."""
    img = imread(path)
    H, W = img.shape[:2]
    x0, y0 = int(np.clip(x, 0, W - 1)), int(np.clip(y, 0, H - 1))
    x1, y1 = int(np.clip(x + w, x0 + 1, W)), int(np.clip(y + h, y0 + 1, H))
    imwrite(path, img[y0:y1, x0:x1])
    return x0, y0, x1 - x0, y1 - y0


def detect(img_path, ppm, mode, points, tolerance=20, threshold=128,
           smooth_mm=0.5, simplify_mm=0.3, min_area_mm2=200, region_mm=None):
    """Find outlines around clicked points.

    mode 'wand':       flood fill from each click on the object (colour tolerance);
                       clicks whose regions touch merge into one outline
    mode 'threshold':  brightness threshold (side picked by the first click), blobs under the clicks
    mode 'background': flood fill from a click on the background; every remaining
                       blob larger than min_area inside region_mm is returned
    mode 'colorkey':   like 'background', but every pixel near the clicked background colour(s)
                       counts as background, connected or not, judged mostly on colour rather
                       than brightness, so grey chrome reflections stand out from a coloured background
    Returns (list of polygons in mm, warning or None).
    """
    img = imread(img_path)
    h, w = img.shape[:2]
    seeds = [(int(np.clip(x * ppm, 0, w - 1)), int(np.clip(y * ppm, 0, h - 1))) for x, y in points]
    if not seeds:
        return [], "No click points."
    blur = cv2.GaussianBlur(img, (0, 0), sigmaX=max(0.6, 0.4 * ppm))
    smooth_px = int(round(smooth_mm * ppm))
    simplify_px = simplify_mm * ppm
    t = float(tolerance)

    if mode == "colorkey":
        lab = cv2.cvtColor(blur, cv2.COLOR_BGR2LAB).astype(np.float32)
        r = max(1, int(round(2 * ppm)))
        bg = np.zeros((h, w), bool)
        for x, y in seeds:
            patch = lab[max(y - r, 0):y + r + 1, max(x - r, 0):x + r + 1].reshape(-1, 3)
            d = lab - np.median(patch, axis=0)
            d[..., 0] *= 0.35  # brightness counts for little: shadows and glare shift it most
            bg |= np.sqrt((d * d).sum(axis=2)) < t
        obj = (~bg).astype(np.uint8) * 255
    elif mode == "threshold":
        gray = cv2.cvtColor(blur, cv2.COLOR_BGR2GRAY)
        dark = gray < threshold
        px, py = seeds[0]
        obj = (dark if dark[py, px] else ~dark).astype(np.uint8) * 255
    else:
        lab = cv2.cvtColor(blur, cv2.COLOR_BGR2LAB)
        obj = np.zeros((h, w), np.uint8)
        # fixed range (compare to the clicked colour); floating range leaks across soft edges
        flags = 8 | cv2.FLOODFILL_MASK_ONLY | cv2.FLOODFILL_FIXED_RANGE | (255 << 8)
        for seed in (seeds if mode == "wand" else seeds[:1]):
            ff = np.zeros((h + 2, w + 2), np.uint8)
            cv2.floodFill(lab, ff, seed, 0, (t, t, t), (t, t, t), flags)
            obj |= ff[1:-1, 1:-1]
        if mode == "background":
            obj = cv2.bitwise_not(obj)

    if mode in ("background", "colorkey") and region_mm:
        x, y, rw, rh = (int(round(v * ppm)) for v in region_mm)
        keep = np.zeros_like(obj)
        keep[max(y, 0):y + rh, max(x, 0):x + rw] = 255
        obj = cv2.bitwise_and(obj, keep)

    obj = _clean(obj, smooth_px)
    contours, _ = cv2.findContours(obj, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
    if not contours:
        return [], "Nothing found - try a higher tolerance."

    warning = None
    if mode in ("background", "colorkey"):
        min_px = min_area_mm2 * ppm * ppm
        picked = [c for c in contours if min_px <= cv2.contourArea(c) < 0.9 * w * h]
        if not picked:
            return [], "No objects found - lower min area or tolerance."
    else:
        picked = [c for c in contours if any(cv2.pointPolygonTest(c, sd, False) >= 0 for sd in seeds)]
        picked = picked or [max(contours, key=cv2.contourArea)]
        if sum(cv2.contourArea(c) for c in picked) > 0.6 * w * h:
            warning = "Selection covers most of the image - it probably leaked. Lower the tolerance."

    polys = [_contour_to_mm(c, ppm, simplify_px) for c in picked]
    return [p for p in polys if len(p) >= 3], warning


GC_MAX_SIDE = 900  # grabCut is slow; work on a downscaled copy of the box


def smart_select(img_path, ppm, rect_mm, fg_mm=(), bg_mm=(), smooth_mm=0.5, simplify_mm=0.3):
    """GrabCut: separate the object inside a box from its background using colour statistics.

    fg_mm / bg_mm are extra clicks that force spots to object / background.
    Returns (list of polygons in mm, warning or None).
    """
    img = imread(img_path)
    h, w = img.shape[:2]
    x0, y0, x1, y1 = (v * ppm for v in rect_mm)
    x0, x1 = sorted((x0, x1))
    y0, y1 = sorted((y0, y1))
    if x1 - x0 < 5 or y1 - y0 < 5:
        return [], "Drag a bigger box around the tool."
    pad = max(10 * ppm, 0.1 * max(x1 - x0, y1 - y0))  # some background around the box to learn from
    rx0, ry0 = int(max(x0 - pad, 0)), int(max(y0 - pad, 0))
    rx1, ry1 = int(min(x1 + pad, w)), int(min(y1 + pad, h))
    roi = img[ry0:ry1, rx0:rx1]
    k = min(1.0, GC_MAX_SIDE / max(roi.shape[:2]))
    small = cv2.resize(roi, None, fx=k, fy=k, interpolation=cv2.INTER_AREA) if k < 1 else roi.copy()

    mask = np.full(small.shape[:2], cv2.GC_BGD, np.uint8)
    bx0, by0 = int((x0 - rx0) * k), int((y0 - ry0) * k)
    bx1, by1 = int((x1 - rx0) * k), int((y1 - ry0) * k)
    mask[by0:by1, bx0:bx1] = cv2.GC_PR_FGD
    mark = max(2, int(round(2 * ppm * k)))
    for pts, val in ((bg_mm, cv2.GC_BGD), (fg_mm, cv2.GC_FGD)):
        for x, y in pts:
            centre = (int(round((x * ppm - rx0) * k)), int(round((y * ppm - ry0) * k)))
            cv2.circle(mask, centre, mark, int(val), -1)
    bgd, fgd = np.zeros((1, 65), np.float64), np.zeros((1, 65), np.float64)
    try:
        cv2.grabCut(small, mask, None, bgd, fgd, 5, cv2.GC_INIT_WITH_MASK)
    except cv2.error:
        return [], "Couldn't separate the tool - try a box with some background around it."
    obj = np.where((mask == cv2.GC_FGD) | (mask == cv2.GC_PR_FGD), 255, 0).astype(np.uint8)
    if k < 1:
        obj = cv2.resize(obj, (roi.shape[1], roi.shape[0]), interpolation=cv2.INTER_LINEAR)
        obj = np.where(obj > 127, 255, 0).astype(np.uint8)
    obj = _clean(obj, int(round(smooth_mm * ppm)))
    contours, _ = cv2.findContours(obj, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
    if not contours:
        return [], "Nothing found - click on the tool to mark it."
    # the biggest blob, plus any blob a foreground click landed in
    fg_px = [(float(x * ppm - rx0), float(y * ppm - ry0)) for x, y in fg_mm]
    big = max(contours, key=cv2.contourArea)
    picked = [big] + [c for c in contours if c is not big and any(cv2.pointPolygonTest(c, p, False) >= 0 for p in fg_px)]
    polys = [_contour_to_mm(c + np.array([rx0, ry0], dtype=c.dtype), ppm, simplify_mm * ppm) for c in picked]
    return [p for p in polys if len(p) >= 3], None
