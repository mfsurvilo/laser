import colorsys
import io
import json
import re
import shutil
import time
import uuid
import zipfile
from datetime import datetime
from pathlib import Path

from flask import Blueprint, Response, abort, current_app, jsonify, render_template, request, send_from_directory

from . import geometry, vision

bp = Blueprint("main", __name__)

PID_RE = re.compile(r"^[a-z0-9-]+$")
HISTORY_LIMIT = 50
PACKAGE_EXT = ".foamproj"
PACKAGE_FORMAT = "foam-insert-maker"
PACKAGE_FILES = ("original_full.jpg", "original.jpg", "rectified.jpg")  # plus project.json

DEFAULT_SETTINGS = {
    "default_buffer_mm": 0.0,
    "grip_auto": False,       # give new cutouts a finger grip
    "grip_d_mm": 20.0,
    "grip_at": "edge",        # where new grips go: edge | center
    "bed_w": 400.0,
    "bed_h": 400.0,
    "joint": "jigsaw",        # jigsaw | dovetail | straight
    "tab_mm": 12.0,
    "tab_spacing_mm": 100.0,
    "kerf_mm": 0.6,           # laser beam width; seams are widened by half of it on each piece
    "nx": 0,                  # 0 = automatic
    "ny": 0,
    "tile_basis": "initial",  # initial = seams laid out over the calibrated sheet; current = the resized sheet
    "seam_off": {"v": [], "h": []},
    "tab_shift": {"v": [], "h": []},
    "include_outline": True,
    "grid_on": False,         # layout grid
    "grid_snap": False,
    "grid_mm": 10.0,
    "grid_origin": None,      # [x, y] mm; None = sheet's top-left corner
}
GRID_KEYS = {"grid_on", "grid_snap", "grid_mm", "grid_origin"}
TEXT_DEFAULTS = {"align": "center", "font": "sans", "bold": True, "italic": False, "line_spacing": 1.0}
TEXT_MAX = 1000


# ---------------------------------------------------------------- storage

def project_dir(pid):
    if not PID_RE.match(pid):
        abort(404)
    d = Path(current_app.config["OUTPUT_DIR"]) / pid
    if not d.is_dir():
        abort(404)
    return d


def load(pid):
    p = json.loads((project_dir(pid) / "project.json").read_text())
    p["settings"] = {**DEFAULT_SETTINGS, **p.get("settings", {})}
    p.setdefault("texts", [])
    p.setdefault("next_text", 1)
    for t in p["texts"]:  # projects saved before text formatting existed
        for k, v in TEXT_DEFAULTS.items():
            t.setdefault(k, v)
    if p.get("sheet") and not p.get("base_sheet"):
        p["base_sheet"] = dict(p["sheet"])
    for c in p["cutouts"]:  # projects saved before move/rotate/grips existed
        if "pivot" not in c:
            c.update(pivot=geometry.centroid(c["raw"]), dx=0.0, dy=0.0, rot=0.0, grips=[])
            geometry.outline(c)
    return p


def edit(pid):
    """Load a project and snapshot it so the change can be undone."""
    p = load(pid)
    hist = project_dir(pid) / "history.json"
    stack = json.loads(hist.read_text()) if hist.exists() else []
    stack = (stack + [p])[-HISTORY_LIMIT:]
    hist.write_text(json.dumps(stack))
    return json.loads(json.dumps(p))


def clear_history(pid):
    (project_dir(pid) / "history.json").unlink(missing_ok=True)


def save(p):
    (project_dir(p["id"]) / "project.json").write_text(json.dumps(p, indent=1))
    return p


def unique_color(n):
    """Distinct colours by stepping the hue around the wheel by the golden angle."""
    h = (n * 0.618033988749895) % 1.0
    l = 0.5 if n % 2 else 0.6
    r, g, b = colorsys.hls_to_rgb(h, l, 0.85)
    return "#{:02x}{:02x}{:02x}".format(int(r * 255), int(g * 255), int(b * 255))


def num(v, default=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def find(items, key, val):
    item = next((i for i in items if i[key] == val), None)
    if item is None:
        abort(404)
    return item


def refresh_text(t):
    t["rings"] = geometry.text_rings(t["text"], t["x"], t["y"], t["size_mm"], t["rot"], t["align"],
                                     t["font"], t["bold"], t["italic"], t["line_spacing"])


def text_format(t, a):
    """Copy formatting fields from request `a` onto text `t`."""
    if a.get("align") in geometry.ALIGNS:
        t["align"] = a["align"]
    if a.get("font") in geometry.FONTS:
        t["font"] = a["font"]
    for k in ("bold", "italic"):
        if k in a:
            t[k] = bool(a[k])
    if "line_spacing" in a:
        t["line_spacing"] = min(max(num(a["line_spacing"], 1.0), 0.5), 5.0)


# ---------------------------------------------------------------- pages / files

@bp.get("/")
def index():
    return render_template("index.html")


@bp.get("/files/<pid>/<name>")
def files(pid, name):
    return send_from_directory(project_dir(pid), name)


# ---------------------------------------------------------------- projects

@bp.get("/api/projects")
def projects():
    out = []
    for f in sorted(Path(current_app.config["OUTPUT_DIR"]).glob("*/project.json"), reverse=True):
        p = json.loads(f.read_text())
        out.append({"id": p["id"], "name": p["name"], "cutouts": len(p["cutouts"])})
    return jsonify(out)


def new_pid():
    return datetime.now().strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:4]


@bp.post("/api/upload")
def upload():
    f = request.files.get("photo")
    if not f:
        return jsonify(error="no file"), 400
    pid = new_pid()
    d = Path(current_app.config["OUTPUT_DIR"]) / pid
    d.mkdir(parents=True)
    try:
        w, h = vision.save_upload(f.stream, d / "original.jpg")
    except Exception as e:  # not an image
        shutil.rmtree(d, ignore_errors=True)
        return jsonify(error=f"could not read image: {e}"), 400
    shutil.copy(d / "original.jpg", d / "original_full.jpg")
    p = {
        "id": pid,
        "name": f.filename or pid,
        "original": {"file": "original.jpg", "w": w, "h": h},
        "calib": None,
        "rectified": None,
        "sheet": None,
        "base_sheet": None,
        "settings": dict(DEFAULT_SETTINGS),
        "cutouts": [],
        "next_num": 1,
        "texts": [],
        "next_text": 1,
        "version": int(time.time() * 1000),
    }
    return jsonify(save(p))


@bp.get("/api/p/<pid>")
def get_project(pid):
    return jsonify(load(pid))


@bp.post("/api/p/<pid>/undo")
def undo(pid):
    hist = project_dir(pid) / "history.json"
    stack = json.loads(hist.read_text()) if hist.exists() else []
    if not stack:
        return jsonify(error="nothing to undo"), 400
    p = stack.pop()
    hist.write_text(json.dumps(stack))
    return jsonify(save(p))


@bp.post("/api/p/<pid>/crop")
def crop(pid):
    p = load(pid)
    clear_history(pid)  # the image file changes, so older snapshots no longer match it
    d = project_dir(pid)
    a = request.get_json()
    old = p.get("crop_origin") or [0, 0]
    if a.get("reset"):
        shutil.copy(d / "original_full.jpg", d / "original.jpg")
        w, h = vision.imread(d / "original.jpg").shape[1::-1]
        new = [0, 0]
    else:
        x, y, w, h = vision.crop(d / "original.jpg", num(a.get("x")), num(a.get("y")), num(a.get("w")), num(a.get("h")))
        new = [old[0] + x, old[1] + y]
    if p["calib"]:  # keep calibration corners on the same spot of the photo
        p["calib"]["corners"] = [[cx + old[0] - new[0], cy + old[1] - new[1]] for cx, cy in p["calib"]["corners"]]
    p["crop_origin"] = new
    p["original"].update(w=int(w), h=int(h))
    p["version"] = int(time.time() * 1000)
    return jsonify(save(p))


@bp.post("/api/p/<pid>/rectify")
def rectify(pid):
    p = load(pid)
    clear_history(pid)
    data = request.get_json()
    corners = data.get("corners") or []
    w, h, m = num(data.get("width_mm")), num(data.get("height_mm")), max(num(data.get("margin_mm")), 0.0)
    if len(corners) != 4 or w <= 0 or h <= 0:
        return jsonify(error="need 4 corners and a positive width and height"), 400
    d = project_dir(pid)
    ppm = vision.rectify(d / "original.jpg", d / "rectified.jpg", corners, w, h, m)
    p["calib"] = {"corners": corners, "width_mm": w, "height_mm": h, "margin_mm": m}
    p["rectified"] = {"file": "rectified.jpg", "ppm": ppm, "width_mm": w + 2 * m, "height_mm": h + 2 * m}
    p["sheet"] = {"x": m, "y": m, "w": w, "h": h}
    p["base_sheet"] = dict(p["sheet"])
    p["cutouts"], p["texts"] = [], []  # old coordinates no longer line up with the new image
    p["next_num"] = p["next_text"] = 1
    p["settings"].update(seam_off={"v": [], "h": []}, tab_shift={"v": [], "h": []})
    p["version"] = int(time.time() * 1000)
    return jsonify(save(p))


@bp.post("/api/p/<pid>/detect")
def detect(pid):
    p = load(pid)
    if not p["rectified"]:
        return jsonify(error="calibrate first"), 400
    a = request.get_json()
    s = p["sheet"]
    if a.get("mode") == "smart":
        pts = lambda k: [(num(x), num(y)) for x, y in a.get(k, [])]
        polys, warning = vision.smart_select(
            project_dir(pid) / "rectified.jpg", p["rectified"]["ppm"], [num(v) for v in a.get("rect", [0, 0, 0, 0])],
            pts("fg"), pts("bg"), smooth_mm=num(a.get("smooth_mm"), 0.5), simplify_mm=num(a.get("simplify_mm"), 0.3))
        return jsonify(polygons=polys, warning=warning)
    polys, warning = vision.detect(
        project_dir(pid) / "rectified.jpg", p["rectified"]["ppm"], a.get("mode", "wand"),
        [(num(x), num(y)) for x, y in a.get("points", [])],
        tolerance=num(a.get("tolerance"), 20), threshold=num(a.get("threshold"), 128),
        smooth_mm=num(a.get("smooth_mm"), 0.5), simplify_mm=num(a.get("simplify_mm"), 0.3),
        min_area_mm2=num(a.get("min_area_mm2"), 200), region_mm=(s["x"], s["y"], s["w"], s["h"]),
    )
    return jsonify(polygons=polys, warning=warning)


@bp.post("/api/p/<pid>/brush")
def brush(pid):
    """Stateless: paint a stroke into / out of the given outlines and return them."""
    load(pid)
    a = request.get_json()
    polys = geometry.brush(a.get("polygons", []), [(num(x), num(y)) for x, y in a.get("stroke", [])],
                           num(a.get("radius_mm"), 3), bool(a.get("erase")))
    return jsonify(polygons=polys)


# ---------------------------------------------------------------- cutouts

@bp.post("/api/p/<pid>/cutouts")
def add_cutouts(pid):
    p = edit(pid)
    s = p["settings"]
    a = request.get_json()
    shape = a.get("shape") if a.get("shape") in ("rect", "circle") else None
    for raw in a.get("polygons", []):
        if len(raw) < 3:
            continue
        if not shape:  # exact shapes keep their points
            raw = geometry.simplify_raw(raw)
        n = p["next_num"]
        p["next_num"] += 1
        c = {
            "num": n,
            "name": str(a.get("name") or f"Cutout {n}")[:80],
            "color": unique_color(n),
            "raw": raw,
            "pivot": geometry.centroid(raw),
            "dx": 0.0, "dy": 0.0, "rot": 0.0,
            "buffer_mm": s["default_buffer_mm"],
            "smooth_mm": 0.0,
            "include": True,
            "grips": [],
        }
        if shape:
            c["shape"] = shape
        if s["grip_auto"]:
            g = geometry.default_grip(raw, [], s["grip_d_mm"] / 2, s["grip_at"])
            if g:
                c["grips"].append(g)
        geometry.outline(c)
        p["cutouts"].append(c)
    return jsonify(save(p))


@bp.patch("/api/p/<pid>/cutouts/<int:n>")
def edit_cutout(pid, n):
    p = edit(pid)
    c = find(p["cutouts"], "num", n)
    a = request.get_json()
    if "name" in a:
        c["name"] = str(a["name"])[:80]
    if "color" in a and re.match(r"^#[0-9a-fA-F]{6}$", str(a["color"])):
        c["color"] = a["color"]
    if "include" in a:
        c["include"] = bool(a["include"])
    for k in ("buffer_mm", "smooth_mm", "dx", "dy", "rot"):
        if k in a:
            c[k] = num(a[k])
    c["smooth_mm"] = max(c["smooth_mm"], 0.0)
    c["rot"] = ((c["rot"] + 180) % 360) - 180
    if "raw" in a and len(a["raw"]) >= 3:
        c["raw"] = geometry.simplify_raw(a["raw"])
    if "grips" in a:
        c["grips"] = [{"x": num(g["x"]), "y": num(g["y"]), "r": max(num(g.get("r"), 10), 1),
                       "at": "center" if g.get("at") == "center" else "edge"} for g in a["grips"]]
    geometry.outline(c)
    return jsonify(save(p))


@bp.post("/api/p/<pid>/cutouts/<int:n>/grip")
def add_grip(pid, n):
    p = edit(pid)
    c = find(p["cutouts"], "num", n)
    a = request.get_json(silent=True) or {}
    at = a.get("at") if a.get("at") in ("edge", "center") else p["settings"]["grip_at"]
    g = geometry.default_grip(c["raw"], c["grips"], p["settings"]["grip_d_mm"] / 2, at)
    if g:
        c["grips"].append(g)
    geometry.outline(c)
    return jsonify(save(p))


@bp.delete("/api/p/<pid>/cutouts/<int:n>")
def delete_cutout(pid, n):
    p = edit(pid)
    p["cutouts"] = [c for c in p["cutouts"] if c["num"] != n]
    return jsonify(save(p))


# ---------------------------------------------------------------- text

@bp.post("/api/p/<pid>/texts")
def add_text(pid):
    p = edit(pid)
    if not p["sheet"]:
        abort(400)
    a = request.get_json()
    sh = p["sheet"]
    t = {
        "id": p["next_text"],
        "text": str(a.get("text", "Text"))[:TEXT_MAX],
        "x": num(a.get("x"), sh["x"] + sh["w"] / 2),
        "y": num(a.get("y"), sh["y"] + sh["h"] / 2),
        "size_mm": max(num(a.get("size_mm"), 8), 1),
        "rot": 0.0,
        **TEXT_DEFAULTS,
    }
    text_format(t, a)
    p["next_text"] += 1
    refresh_text(t)
    p["texts"].append(t)
    return jsonify(save(p))


@bp.patch("/api/p/<pid>/texts/<int:tid>")
def edit_text(pid, tid):
    p = edit(pid)
    t = find(p["texts"], "id", tid)
    a = request.get_json()
    if "text" in a:
        t["text"] = str(a["text"])[:TEXT_MAX]
    text_format(t, a)
    for k in ("x", "y", "rot"):
        if k in a:
            t[k] = num(a[k])
    if "size_mm" in a:
        t["size_mm"] = max(num(a["size_mm"], t["size_mm"]), 1)
    t["rot"] = ((t["rot"] + 180) % 360) - 180
    refresh_text(t)
    return jsonify(save(p))


@bp.delete("/api/p/<pid>/texts/<int:tid>")
def delete_text(pid, tid):
    p = edit(pid)
    p["texts"] = [t for t in p["texts"] if t["id"] != tid]
    return jsonify(save(p))


# ---------------------------------------------------------------- layout / export

@bp.patch("/api/p/<pid>/settings")
def settings(pid):
    a = request.get_json()
    # grid tweaks are view settings: saved with the design but not worth an undo step
    p = load(pid) if a and set(a) <= GRID_KEYS else edit(pid)
    s = p["settings"]
    for k in ("grid_on", "grid_snap"):
        if k in a:
            s[k] = bool(a[k])
    if "grid_mm" in a:
        s["grid_mm"] = max(num(a["grid_mm"], s["grid_mm"]), 0.1)
    if "grid_origin" in a:
        o = a["grid_origin"]
        s["grid_origin"] = [num(o[0]), num(o[1])] if isinstance(o, list) and len(o) == 2 else None
    for k in ("default_buffer_mm", "grip_d_mm", "bed_w", "bed_h", "tab_mm", "tab_spacing_mm", "kerf_mm"):
        if k in a and a[k] is not None:
            s[k] = num(a[k], s[k])
    for k in ("nx", "ny"):
        if k in a:
            s[k] = max(int(num(a[k])), 0)
    if a.get("grip_at") in ("edge", "center"):
        s["grip_at"] = a["grip_at"]
    if a.get("joint") in ("jigsaw", "dovetail", "straight"):
        s["joint"] = a["joint"]
    if a.get("tile_basis") in ("initial", "current"):
        s["tile_basis"] = a["tile_basis"]
    for k in ("include_outline", "grip_auto"):
        if k in a:
            s[k] = bool(a[k])
    for k in ("seam_off", "tab_shift"):
        if isinstance(a.get(k), dict):
            s[k] = {d: [num(v) for v in a[k].get(d, [])] for d in ("v", "h")}
    s["bed_w"], s["bed_h"] = max(s["bed_w"], 10), max(s["bed_h"], 10)
    s["tab_mm"], s["tab_spacing_mm"] = max(s["tab_mm"], 2), max(s["tab_spacing_mm"], 10)
    s["grip_d_mm"] = max(s["grip_d_mm"], 2)
    s["kerf_mm"] = min(max(s["kerf_mm"], 0), 10)

    if p["sheet"]:
        sh = p["sheet"]
        if a.get("reset_sheet"):
            p["sheet"] = dict(p["base_sheet"])
        elif isinstance(a.get("sheet"), dict):
            for k in ("x", "y", "w", "h"):
                sh[k] = num(a["sheet"].get(k), sh[k])
        else:
            if a.get("sheet_w") is not None:
                sh["w"] = num(a["sheet_w"], sh["w"])
            if a.get("sheet_h") is not None:
                sh["h"] = num(a["sheet_h"], sh["h"])
        p["sheet"]["w"], p["sheet"]["h"] = max(p["sheet"]["w"], 1), max(p["sheet"]["h"], 1)
    return jsonify(save(p))


def tile_base(p):
    return p["base_sheet"] if p["settings"]["tile_basis"] == "initial" else p["sheet"]


@bp.get("/api/p/<pid>/tiles")
def tiles(pid):
    p = load(pid)
    if not p["sheet"]:
        return jsonify(tiles=[], nx=0, ny=0)
    layout, ts = geometry.build_tiles(tile_base(p), p["sheet"], None, p["settings"], kerf=False)
    out = []
    for t in ts:
        minx, miny, maxx, maxy = t["geom"].bounds
        out.append({"row": t["row"], "col": t["col"], "rings": geometry.rings_of(t["geom"]), "fits": t["fits"],
                    "w": maxx - minx, "h": maxy - miny})
    return jsonify(tiles=out, nx=layout["nx"], ny=layout["ny"], xs=layout["xs"], ys=layout["ys"],
                   seam_off=layout["seam_off"], tab_shift=layout["tab_shift"])


def _stem(p):
    return re.sub(r"[^A-Za-z0-9_-]+", "_", Path(p["name"]).stem) or "insert"


@bp.get("/api/p/<pid>/export.svg")
def export_svg(pid):
    p = load(pid)
    if not p["sheet"]:
        abort(400)
    svg = geometry.export_sheet_svg(p["cutouts"], p["texts"], p["sheet"], p["settings"]["include_outline"])
    (project_dir(pid) / "export.svg").write_text(svg)
    return Response(svg, mimetype="image/svg+xml",
                    headers={"Content-Disposition": f'attachment; filename="{_stem(p)}.svg"'})


@bp.get("/api/p/<pid>/export_tiles.zip")
def export_tiles(pid):
    p = load(pid)
    if not p["sheet"]:
        abort(400)
    data, _ = geometry.export_tiles_zip(p["cutouts"], p["texts"], p["sheet"], tile_base(p), p["settings"])
    (project_dir(pid) / "export_tiles.zip").write_bytes(data)
    return Response(data, mimetype="application/zip",
                    headers={"Content-Disposition": f'attachment; filename="{_stem(p)}_tiles.zip"'})


# ---------------------------------------------------------------- design packages

@bp.get("/api/p/<pid>/package")
def package(pid):
    """One file with the photo, every derived image and all settings/cutouts/text."""
    p = load(pid)
    d = project_dir(pid)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("manifest.json", json.dumps({"format": PACKAGE_FORMAT, "version": 1, "saved": datetime.now().isoformat()}))
        z.writestr("project.json", json.dumps(p, indent=1))
        for name in PACKAGE_FILES:
            if (d / name).exists():
                z.write(d / name, name)
    return Response(buf.getvalue(), mimetype="application/zip",
                    headers={"Content-Disposition": f'attachment; filename="{_stem(p)}{PACKAGE_EXT}"'})


@bp.post("/api/import")
def import_package():
    f = request.files.get("package")
    if not f:
        return jsonify(error="no file"), 400
    try:
        z = zipfile.ZipFile(io.BytesIO(f.read()))
        names = set(z.namelist())
        manifest = json.loads(z.read("manifest.json"))
        if manifest.get("format") != PACKAGE_FORMAT or "project.json" not in names or "original.jpg" not in names:
            raise ValueError("not a Foam Insert Maker design")
        p = json.loads(z.read("project.json"))
    except (zipfile.BadZipFile, KeyError, ValueError) as e:
        return jsonify(error=f"could not open design: {e}"), 400

    pid = new_pid()
    d = Path(current_app.config["OUTPUT_DIR"]) / pid
    d.mkdir(parents=True)
    for name in PACKAGE_FILES:  # fixed names only - never trust paths inside the zip
        if name in names:
            (d / name).write_bytes(z.read(name))
    if not (d / "original_full.jpg").exists():
        shutil.copy(d / "original.jpg", d / "original_full.jpg")
    if p.get("rectified") and not (d / "rectified.jpg").exists():
        p["rectified"] = None
    p["id"] = pid
    p["version"] = int(time.time() * 1000)
    save(p)
    return jsonify(load(pid))
