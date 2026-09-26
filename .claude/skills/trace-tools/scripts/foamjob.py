"""Command-line helper for tracing tools in a Foam Insert Maker design.

Runs the app in-process (no server needed). Every length is in mm, in the rectified-photo frame
(origin top-left, y down). Working files go to output/<pid>/trace/.

  open FILE.foamproj                   import a copy, print its details, write overview.jpg (10 mm grid)
  zoom PID X0 Y0 X1 Y1 [--show N ...]  close-up with 1 mm ticks; overlays saved outlines / cutouts
  smart PID NAME --rect X0 Y0 X1 Y1 [--fg "x,y;..."] [--bg "x,y;..."]
                                       GrabCut inside the box -> outline NAME
  colorkey PID --points "x,y;..." [--tol 20]
                                       everything not the background colour -> outlines ck1, ck2, ...
  scan PID (--rows A:B:STEP --x X0:X1 | --cols A:B:STEP --y Y0:Y1) [--bg x,y] [--bright 120] [--dark 30]
                                       per row/column, where the pixels change class:
                                       B bright, D dark/shadow, o other (not background), . background-like
  poly PID NAME "x,y x,y ..."          outline NAME from typed points (a traced outline)
  union PID NAME "x,y x,y ..." [--erase]
                                       add (or cut away) a polygon to outline NAME, like the brush
  clear PID                            delete every cutout
  add PID NAME --label "Hex key" [--grip edge|center]
                                       outline NAME becomes a cutout (no finger grip unless asked)
  check PID                            final.jpg: every cutout's cut line over the photo, plus sizes
  save PID OUT.foamproj                write the design package
"""
import argparse
import json
import sys
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT))
from app import create_app  # noqa: E402

APP = create_app()
C = APP.test_client()
OUT = Path(APP.config["OUTPUT_DIR"])


def call(method, url, **kw):
    r = getattr(C, method)(url, **kw)
    if r.status_code != 200:
        sys.exit(f"{method.upper()} {url} -> {r.status_code}: {r.get_data(as_text=True)[:300]}")
    return r


def project(pid):
    return call("get", f"/api/p/{pid}").get_json()


def work(pid):
    d = OUT / pid / "trace"
    d.mkdir(exist_ok=True)
    return d


def image(pid):
    return cv2.imdecode(np.fromfile(str(OUT / pid / "rectified.jpg"), np.uint8), cv2.IMREAD_COLOR)


def pts_arg(s):
    """ "x,y;x,y" or "x,y x,y" -> [[x, y], ...] """
    if not s:
        return []
    return [[float(v) for v in p.split(",")] for p in s.replace(";", " ").split()]


def save_poly(pid, name, poly):
    (work(pid) / f"{name}.json").write_text(json.dumps(poly))


def load_poly(pid, name):
    f = work(pid) / f"{name}.json"
    if not f.exists():
        sys.exit(f"no outline named {name!r} (have: {', '.join(p.stem for p in work(pid).glob('*.json')) or 'none'})")
    return json.loads(f.read_text())


def area(poly):
    a = np.asarray(poly)
    return 0.5 * abs(np.dot(a[:, 0], np.roll(a[:, 1], 1)) - np.dot(a[:, 1], np.roll(a[:, 0], 1)))


def describe(name, poly):
    a = np.asarray(poly)
    lo, hi = a.min(0), a.max(0)
    print(f"  {name}: {len(poly)} pts, area {area(poly):.0f} mm², bbox x {lo[0]:.1f}-{hi[0]:.1f}, y {lo[1]:.1f}-{hi[1]:.1f} "
          f"({hi[0] - lo[0]:.1f} x {hi[1] - lo[1]:.1f} mm)")


def ticks(img, x0, y0, s, step_small, step_big, w_mm, h_mm, color=(0, 0, 255)):
    """Ruler ticks along the top and left edges; s = px per mm in img."""
    for mm in np.arange(np.ceil(x0 / step_small) * step_small, x0 + w_mm, step_small):
        x = int((mm - x0) * s)
        big = abs(mm / step_big - round(mm / step_big)) < 1e-6
        cv2.line(img, (x, 0), (x, 22 if big else 9), color, 1)
        if big:
            cv2.putText(img, f"{mm:g}", (x + 2, 34), cv2.FONT_HERSHEY_SIMPLEX, 0.45, color, 1)
    for mm in np.arange(np.ceil(y0 / step_small) * step_small, y0 + h_mm, step_small):
        y = int((mm - y0) * s)
        big = abs(mm / step_big - round(mm / step_big)) < 1e-6
        cv2.line(img, (0, y), (22 if big else 9, y), color, 1)
        if big:
            cv2.putText(img, f"{mm:g}", (25, y + 4), cv2.FONT_HERSHEY_SIMPLEX, 0.45, color, 1)


# ---------------------------------------------------------------- commands

def cmd_open(a):
    src = Path(a.file)
    with open(src, "rb") as f:
        pid = call("post", "/api/import", data={"package": (f, src.name)}).get_json()["id"]
    p = project(pid)
    ppm = p["rectified"]["ppm"]
    img = image(pid)
    h, w = img.shape[:2]
    print(f"PID {pid}")
    print(f"photo {w / ppm:.1f} x {h / ppm:.1f} mm at {ppm:.2f} px/mm; sheet {json.dumps({k: round(v, 1) for k, v in p['sheet'].items()})}")
    print(f"cutouts: {[(c['num'], c['name']) for c in p['cutouts']] or 'none'}")
    print(f"text: {[t['text'] for t in p['texts']] or 'none'}; default buffer {p['settings']['default_buffer_mm']} mm")
    k = min(1.0, 1100 / max(w, h))
    o = cv2.resize(img, None, fx=k, fy=k, interpolation=cv2.INTER_AREA)
    s = ppm * k
    for mm in range(0, int(w / ppm) + 1, 10):
        cv2.line(o, (int(mm * s), 0), (int(mm * s), o.shape[0]), (0, 0, 255) if mm % 50 == 0 else (0, 190, 255), 2 if mm % 50 == 0 else 1)
        if mm % 50 == 0:
            cv2.putText(o, str(mm), (int(mm * s) + 3, 16), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0, 0, 255), 2)
    for mm in range(0, int(h / ppm) + 1, 10):
        cv2.line(o, (0, int(mm * s)), (o.shape[1], int(mm * s)), (0, 0, 255) if mm % 50 == 0 else (0, 190, 255), 2 if mm % 50 == 0 else 1)
        if mm % 50 == 0:
            cv2.putText(o, str(mm), (3, int(mm * s) - 4), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0, 0, 255), 2)
    out = work(pid) / "overview.jpg"
    cv2.imwrite(str(out), o)
    print(f"overview (10 mm grid, labels every 50): {out}")


def cmd_zoom(a):
    p = project(a.pid)
    ppm = p["rectified"]["ppm"]
    img = image(a.pid)
    x0, y0, x1, y1 = a.box
    for name in a.show or []:
        if name == "cutouts":
            for c in p["cutouts"]:
                cv2.polylines(img, [(np.array(c["final"]) * ppm).astype(np.int32)], True, (255, 0, 255), 2)
        else:
            cv2.polylines(img, [(np.array(load_poly(a.pid, name)) * ppm).astype(np.int32)], True, (0, 0, 255), 2)
    crop = img[int(y0 * ppm):int(y1 * ppm), int(x0 * ppm):int(x1 * ppm)]
    k = min(a.max_px / max(crop.shape[:2]), 3.0)
    crop = cv2.resize(crop, None, fx=k, fy=k, interpolation=cv2.INTER_AREA if k < 1 else cv2.INTER_NEAREST)
    ticks(crop, x0, y0, ppm * k, 1, 5, x1 - x0, y1 - y0)
    out = work(a.pid) / f"zoom_{a.tag or 'z'}.jpg"
    cv2.imwrite(str(out), crop)
    print(f"{out}  ({crop.shape[1]}x{crop.shape[0]} px, {ppm * k:.1f} px/mm; red = saved outlines, magenta = cutouts)")


def cmd_smart(a):
    r = call("post", f"/api/p/{a.pid}/detect", json={
        "mode": "smart", "rect": a.rect, "fg": pts_arg(a.fg), "bg": pts_arg(a.bg),
        "smooth_mm": a.smooth, "simplify_mm": a.simplify}).get_json()
    if not r["polygons"]:
        sys.exit(f"nothing found: {r['warning']}")
    poly = max(r["polygons"], key=area)
    save_poly(a.pid, a.name, poly)
    describe(a.name, poly)
    if len(r["polygons"]) > 1:
        print(f"  ({len(r['polygons']) - 1} smaller extra blob(s) ignored)")


def cmd_colorkey(a):
    r = call("post", f"/api/p/{a.pid}/detect", json={
        "mode": "colorkey", "points": pts_arg(a.points), "tolerance": a.tol, "min_area_mm2": a.min_area,
        "smooth_mm": a.smooth, "simplify_mm": a.simplify}).get_json()
    if not r["polygons"]:
        sys.exit(f"nothing found: {r['warning']}")
    for i, poly in enumerate(sorted(r["polygons"], key=lambda q: np.asarray(q)[:, 0].min()), 1):
        save_poly(a.pid, f"ck{i}", poly)
        describe(f"ck{i}", poly)


def cmd_scan(a):
    p = project(a.pid)
    ppm = p["rectified"]["ppm"]
    img = cv2.GaussianBlur(image(a.pid), (0, 0), 1.5)
    lab = cv2.cvtColor(img, cv2.COLOR_BGR2LAB).astype(np.float32)
    ref = None
    if a.bg:
        bx, by = pts_arg(a.bg)[0]
        r = max(1, int(2 * ppm))
        ref = np.median(lab[int(by * ppm) - r:int(by * ppm) + r, int(bx * ppm) - r:int(bx * ppm) + r].reshape(-1, 3), 0)

    def classify(v):
        if v[0] > a.bright:
            return "B"
        if v[0] < a.dark:
            return "D"
        if ref is not None:
            d = v - ref
            d[0] *= 0.35
            if np.sqrt((d * d).sum()) < a.tol:
                return "."
        return "o"

    rows = a.rows is not None
    lo, hi, step = (float(v) for v in (a.rows or a.cols).split(":"))
    s0, s1 = (float(v) for v in (a.x if rows else a.y).split(":"))
    print(("row y" if rows else "col x") + ": class changes as  position(class)  " +
          "[B bright, D dark/shadow, o other, . background-like]")
    for pos in np.arange(lo, hi + 1e-9, step):
        along = np.arange(s0, s1, 0.1)
        if rows:
            vals = [lab[int(pos * ppm), int(q * ppm)] for q in along]
        else:
            vals = [lab[int(q * ppm), int(pos * ppm)] for q in along]
        cls = [classify(v.copy()) for v in vals]
        runs, start = [], 0
        for i in range(1, len(cls) + 1):
            if i == len(cls) or cls[i] != cls[start]:
                if (i - start) * 0.1 >= a.min_run or not runs:
                    runs.append(f"{along[start]:.1f}({cls[start]})")
                start = i
        print(f"{pos:6.1f}: " + " ".join(runs) + f"  ..{s1:.1f}")


def cmd_poly(a):
    poly = pts_arg(a.points)
    if len(poly) < 3:
        sys.exit("need at least 3 points")
    save_poly(a.pid, a.name, poly)
    describe(a.name, poly)


def cmd_union(a):
    from shapely.geometry import Polygon
    base = Polygon(load_poly(a.pid, a.name)).buffer(0)
    piece = Polygon(pts_arg(a.points)).buffer(0)
    g = base.difference(piece) if a.erase else base.union(piece)
    parts = [g] if g.geom_type == "Polygon" else list(g.geoms)
    big = max(parts, key=lambda q: q.area)
    poly = [[round(x, 3), round(y, 3)] for x, y in big.exterior.coords[:-1]]
    save_poly(a.pid, a.name, poly)
    describe(a.name, poly)
    if len(parts) > 1:
        print(f"  ({len(parts) - 1} separate piece(s) dropped)")


def cmd_clear(a):
    for c in project(a.pid)["cutouts"]:
        call("delete", f"/api/p/{a.pid}/cutouts/{c['num']}")
        print(f"deleted #{c['num']} {c['name']}")


def cmd_add(a):
    p = call("post", f"/api/p/{a.pid}/cutouts", json={"polygons": [load_poly(a.pid, a.name)]}).get_json()
    n = p["cutouts"][-1]["num"]
    call("patch", f"/api/p/{a.pid}/cutouts/{n}", json={"name": a.label, "grips": []})
    if a.grip:
        call("post", f"/api/p/{a.pid}/cutouts/{n}/grip", json={"at": a.grip})
    c = next(c for c in project(a.pid)["cutouts"] if c["num"] == n)
    f = np.asarray(c["final"])
    print(f"added #{n} {a.label}: buffer {c['buffer_mm']} mm, grips {len(c['grips'])}, cut size "
          f"{np.ptp(f[:, 0]):.1f} x {np.ptp(f[:, 1]):.1f} mm")


def cmd_check(a):
    p = project(a.pid)
    ppm = p["rectified"]["ppm"]
    img = image(a.pid)
    for c in p["cutouts"]:
        cv2.polylines(img, [(np.array(c["final"]) * ppm).astype(np.int32)], True, (0, 0, 255), 3)
        cv2.polylines(img, [(np.array(c["placed"]) * ppm).astype(np.int32)], True, (255, 255, 255), 1)
        f = np.asarray(c["final"])
        print(f"#{c['num']} {c['name']}: buffer {c['buffer_mm']} mm, grips {len(c['grips'])}, "
              f"cut {np.ptp(f[:, 0]):.1f} x {np.ptp(f[:, 1]):.1f} mm, output {'on' if c['include'] else 'OFF'}")
    k = min(1.0, 1100 / max(img.shape[:2]))
    out = work(a.pid) / "final.jpg"
    cv2.imwrite(str(out), cv2.resize(img, None, fx=k, fy=k, interpolation=cv2.INTER_AREA))
    print(f"{out}  (red = cut line incl. buffer, white = traced tool)")


def cmd_save(a):
    data = call("get", f"/api/p/{a.pid}/package").data
    out = Path(a.out)
    if out.exists() and not a.force:
        sys.exit(f"{out} exists - pick another name or pass --force")
    out.write_bytes(data)
    print(f"saved {out} ({len(data) / 1e6:.1f} MB)")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("open"); s.add_argument("file")
    s = sub.add_parser("zoom"); s.add_argument("pid"); s.add_argument("box", type=float, nargs=4)
    s.add_argument("--show", nargs="*"); s.add_argument("--tag"); s.add_argument("--max-px", type=int, default=700)
    s = sub.add_parser("smart"); s.add_argument("pid"); s.add_argument("name")
    s.add_argument("--rect", type=float, nargs=4, required=True); s.add_argument("--fg"); s.add_argument("--bg")
    s.add_argument("--smooth", type=float, default=0.3); s.add_argument("--simplify", type=float, default=0.15)
    s = sub.add_parser("colorkey"); s.add_argument("pid"); s.add_argument("--points", required=True)
    s.add_argument("--tol", type=float, default=20); s.add_argument("--min-area", type=float, default=300)
    s.add_argument("--smooth", type=float, default=0.3); s.add_argument("--simplify", type=float, default=0.15)
    s = sub.add_parser("scan"); s.add_argument("pid"); s.add_argument("--rows"); s.add_argument("--cols")
    s.add_argument("--x"); s.add_argument("--y"); s.add_argument("--bg"); s.add_argument("--bright", type=float, default=120)
    s.add_argument("--dark", type=float, default=30); s.add_argument("--tol", type=float, default=20)
    s.add_argument("--min-run", type=float, default=0.4)
    s = sub.add_parser("poly"); s.add_argument("pid"); s.add_argument("name"); s.add_argument("points")
    s = sub.add_parser("union"); s.add_argument("pid"); s.add_argument("name"); s.add_argument("points")
    s.add_argument("--erase", action="store_true")
    s = sub.add_parser("clear"); s.add_argument("pid")
    s = sub.add_parser("add"); s.add_argument("pid"); s.add_argument("name"); s.add_argument("--label", required=True)
    s.add_argument("--grip", choices=["edge", "center"])
    s = sub.add_parser("check"); s.add_argument("pid")
    s = sub.add_parser("save"); s.add_argument("pid"); s.add_argument("out"); s.add_argument("--force", action="store_true")
    a = ap.parse_args()
    if a.cmd == "scan" and not ((a.rows and a.x) or (a.cols and a.y)):
        sys.exit("scan needs --rows A:B:STEP --x X0:X1, or --cols A:B:STEP --y Y0:Y1")
    globals()["cmd_" + a.cmd](a)


if __name__ == "__main__":
    main()
