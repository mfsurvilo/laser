"use strict";

// ------------------------------------------------------------------ state
const S = {
  p: null,            // project from the server
  unit: "mm",
  mode: "calibrate",  // calibrate | trace | layout
  tool: "wand",
  corners: [],        // calibration clicks, original-image pixels
  cropping: false,
  cropRect: null,     // [x0, y0, x1, y1] original-image pixels
  preview: [],        // detected polygons awaiting "Add" (mm)
  seeds: [],          // wand/threshold clicks for the current preview (mm)
  draft: [],          // draw-tool points (mm)
  sel: null,          // {k: "c" | "t", id}
  tiles: null,
  hover: null,        // mouse position, mm
  measuring: false,
  measure: null,      // {a, b, done}
  arrange: false,
  live: null,         // uncommitted move/rotate: {k, id, ddx, ddy, drot, cx, cy}
  liveGrip: null,     // {num, idx, pt}
  tug: null,          // {kind: "preview"|"cut", i|num, base, grab, pts}
  pickOrigin: false,  // next click sets the grid origin
  shape: null,        // exact circle/rectangle preview: {kind, c, r | w, h, pts}
  hidden: new Set(),  // cutout numbers hidden from view while editing (still exported)
  smart: null,        // smart select: {rect: [x0, y0, x1, y1], fg: [], bg: []} mm
  stroke: null,       // brush stroke in progress: {pts, erase}
  brushErase: false,
  strokes: [],        // freehand: index in S.draft where each stroke starts (Backspace removes one)
};
const view = { scale: 1, ox: 0, oy: 0 };
const img = new Image();
let imgKind = null;   // "orig" | "rect"

const $ = (s) => document.querySelector(s);
const $$ = (s) => [...document.querySelectorAll(s)];
const cv = $("#cv");
const ctx = cv.getContext("2d");
const HANDLE = 8;     // screen px hit radius for handles

// ------------------------------------------------------------------ units
const TO_MM = { mm: 1, cm: 10, in: 25.4, inch: 25.4, inches: 25.4, '"': 25.4, ft: 304.8, "'": 304.8 };

// one term: "4in", "4 mm", "3 1/2in", "1/4", "10" (bare number = current unit)
function parseTerm(str) {
  let s = str.trim();
  let sign = 1;
  if (s[0] === "-") { sign = -1; s = s.slice(1).trim(); } else if (s[0] === "+") s = s.slice(1).trim();
  const um = s.match(/(mm|cm|inches|inch|in|ft|"|')$/);
  const unit = um ? um[1] : S.unit;
  if (um) s = s.slice(0, -um[1].length).trim();
  const m = s.match(/^(\d+(?:\.\d*)?|\.\d+)?(?:\s*(\d+)\/(\d+))?$/);
  if (!m || (m[1] === undefined && m[2] === undefined)) return null;
  let v = m[1] ? parseFloat(m[1]) : 0;
  if (m[2]) v += parseFloat(m[2]) / parseFloat(m[3]);
  return sign * v * TO_MM[unit];
}
// sums and differences: "15in + 3mm - 1/8in". "3-1/2in" (no spaces) is three and a half inches.
function parseLen(str) {
  const s = String(str).trim().toLowerCase().replace(/(\d)-(\d+\/\d+)/g, "$1 $2");
  if (!s) return null;
  const terms = s.match(/[+-]?[^+-]+/g);
  if (!terms || terms.join("") !== s) return null;
  let total = 0;
  for (const t of terms) {
    const v = parseTerm(t);
    if (v === null) return null;
    total += v;
  }
  return total;
}
function fmtLen(mm, withUnit = true) {
  if (mm === null || mm === undefined || mm === "" || isNaN(mm)) return "";
  const v = S.unit === "in" ? mm / 25.4 : mm;
  const txt = S.unit === "in" ? (+v.toFixed(3)).toString() : (+v.toFixed(2)).toString();
  return withUnit ? `${txt} ${S.unit}` : txt;
}
function lenInputs() { return $$("input.len"); }
function setLen(el, mm) { el.dataset.mm = mm; el.value = fmtLen(mm); }
function getLen(el) { return el.dataset.mm === "" ? null : parseFloat(el.dataset.mm); }
lenInputs().forEach((el) => {
  el.addEventListener("change", () => {
    const mm = parseLen(el.value);
    if (mm === null) { el.value = fmtLen(getLen(el)); return; }
    setLen(el, mm);
    el.dispatchEvent(new CustomEvent("len"));
  });
});
$("#unit").addEventListener("change", (e) => {
  S.unit = e.target.checked ? "in" : "mm";
  try { localStorage.setItem("unit", S.unit); } catch {}
  lenInputs().forEach((el) => (el.value = fmtLen(getLen(el))));
  renderTables(); renderSeams(); updateTileStatus(); draw();
});

// ------------------------------------------------------------------ api
async function api(method, url, body) {
  const r = await fetch(url, {
    method, headers: body ? { "Content-Type": "application/json" } : {},
    body: body ? JSON.stringify(body) : undefined,
  });
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(j.error || r.statusText);
  return j;
}
const P = (path) => `/api/p/${S.p.id}${path}`;

function setProject(p) {
  S.p = p;
  S.live = null; S.liveGrip = null; S.tug = null;
  if (S.sel && !findSel()) S.sel = null;
  $("#pname").textContent = p.name;
  $("#empty").style.display = "none";
  try { localStorage.setItem("last", p.id); } catch {}
  syncLayoutInputs();
  renderTables();
  refreshTiles();
  loadImage();  // no-op when the image has not changed
  updateButtons();
  draw();
}
async function change(method, path, body) {
  try { setProject(await api(method, P(path), body)); } catch (err) { alert(err.message); }
}

// ------------------------------------------------------------------ geometry helpers
const cutByNum = (n) => S.p.cutouts.find((c) => c.num === n);
const visibleCuts = () => S.p.cutouts.filter((c) => !S.hidden.has(c.num));
// suppressed (not exported) cutouts show as translucent grey
const cutColor = (c) => (c.include ? c.color : "rgba(160,160,160,.55)");
const textById = (id) => S.p.texts.find((t) => t.id === id);
function findSel() {
  if (!S.sel || !S.p) return null;
  return S.sel.k === "c" ? cutByNum(S.sel.id) : textById(S.sel.id);
}
function rotPts(pts, [cx, cy], deg) {
  const a = (deg * Math.PI) / 180, c = Math.cos(a), s = Math.sin(a);
  return pts.map(([x, y]) => [cx + (x - cx) * c - (y - cy) * s, cy + (x - cx) * s + (y - cy) * c]);
}
// the inverse of the server's placement (rotate about pivot, then translate)
const unplace = (pts, c) => rotPts(pts.map(([x, y]) => [x - c.dx, y - c.dy]), c.pivot, -c.rot);
function itemCenter(k, it) { return k === "c" ? [it.pivot[0] + it.dx, it.pivot[1] + it.dy] : [it.x, it.y]; }
// apply an in-progress drag/rotate to points belonging to item (k, id)
function live(pts, k, id) {
  const L = S.live;
  if (!L || L.k !== k || L.id !== id) return pts;
  return rotPts(pts, [L.cx, L.cy], L.drot).map(([x, y]) => [x + L.ddx, y + L.ddy]);
}
function polyArea(pts) {
  let a = 0;
  pts.forEach((p, i) => { const q = pts[(i + 1) % pts.length]; a += p[0] * q[1] - q[0] * p[1]; });
  return Math.abs(a) / 2;
}
function centroid(pts) {
  let x = 0, y = 0;
  pts.forEach((p) => { x += p[0]; y += p[1]; });
  return [x / pts.length, y / pts.length];
}
function bbox(pts) {
  const xs = pts.map((p) => p[0]), ys = pts.map((p) => p[1]);
  return [Math.min(...xs), Math.min(...ys), Math.max(...xs), Math.max(...ys)];
}
function pointInPoly([x, y], pts) {
  let inside = false;
  for (let i = 0, j = pts.length - 1; i < pts.length; j = i++) {
    const [xi, yi] = pts[i], [xj, yj] = pts[j];
    if ((yi > y) !== (yj > y) && x < ((xj - xi) * (y - yi)) / (yj - yi) + xi) inside = !inside;
  }
  return inside;
}
// nearest point on a closed polygon: {pt, d, i (segment start)}
function nearestOnPoly(p, pts) {
  let best = { d: Infinity };
  for (let i = 0; i < pts.length; i++) {
    const a = pts[i], b = pts[(i + 1) % pts.length];
    const vx = b[0] - a[0], vy = b[1] - a[1], L2 = vx * vx + vy * vy || 1e-9;
    const t = Math.max(0, Math.min(1, ((p[0] - a[0]) * vx + (p[1] - a[1]) * vy) / L2));
    const q = [a[0] + t * vx, a[1] + t * vy];
    const d = Math.hypot(q[0] - p[0], q[1] - p[1]);
    if (d < best.d) best = { pt: q, d, i };
  }
  return best;
}
function resample(pts, step) {
  const out = [];
  for (let i = 0; i < pts.length; i++) {
    const a = pts[i], b = pts[(i + 1) % pts.length];
    const n = Math.max(1, Math.ceil(Math.hypot(b[0] - a[0], b[1] - a[1]) / step));
    for (let k = 0; k < n; k++) out.push([a[0] + ((b[0] - a[0]) * k) / n, a[1] + ((b[1] - a[1]) * k) / n]);
  }
  return out;
}
// direction (degrees) of the outline near p: principal axis of nearby outline points
function edgeAngle(p, pts) {
  const near = nearestOnPoly(p, pts);
  const dense = resample(pts, 0.5).filter((q) => Math.hypot(q[0] - near.pt[0], q[1] - near.pt[1]) < 6);
  if (dense.length < 3) {
    const a = pts[near.i], b = pts[(near.i + 1) % pts.length];
    return (Math.atan2(b[1] - a[1], b[0] - a[0]) * 180) / Math.PI;
  }
  const [mx, my] = centroid(dense);
  let sxx = 0, syy = 0, sxy = 0;
  dense.forEach(([x, y]) => { sxx += (x - mx) ** 2; syy += (y - my) ** 2; sxy += (x - mx) * (y - my); });
  return (0.5 * Math.atan2(2 * sxy, sxx - syy) * 180) / Math.PI;
}

// ------------------------------------------------------------------ grid
const gridSet = () => S.p.settings;
function gridOrigin() {
  const s = gridSet(), sh = S.p.base_sheet || S.p.sheet;
  return s.grid_origin || (sh ? [sh.x, sh.y] : [0, 0]);
}
// Alt held = move freely
const snapOn = (e) => !!(S.p && S.p.rectified && gridSet().grid_snap && !(e && e.altKey));
function snapPt(p, e) {
  if (!snapOn(e)) return p;
  const g = gridSet().grid_mm, o = gridOrigin();
  return p.map((v, i) => o[i] + Math.round((v - o[i]) / g) * g);
}
// the point of an item that lands on grid points: its bounding-box centre
function snapAnchor(k, it) {
  if (k === "t") return [it.x, it.y];
  const b = bbox(it.placed);
  return [(b[0] + b[2]) / 2, (b[1] + b[3]) / 2];
}

// ------------------------------------------------------------------ image / view
function loadImage() {
  if (!S.p) return;
  const want = S.mode === "calibrate" || !S.p.rectified ? "orig" : "rect";
  const file = want === "orig" ? S.p.original.file : S.p.rectified.file;
  const src = `/files/${S.p.id}/${file}?v=${S.p.version}`;
  if (img.dataset.src === src) return;
  img.dataset.src = src;
  img.onload = () => { imgKind = want; fit(); draw(); };
  img.src = src;
}
function fit() {
  const r = cv.getBoundingClientRect();
  if (!img.naturalWidth) return;
  view.scale = Math.min(r.width / img.naturalWidth, r.height / img.naturalHeight) * 0.95;
  view.ox = (r.width - img.naturalWidth * view.scale) / 2;
  view.oy = (r.height - img.naturalHeight * view.scale) / 2;
}
function resize() {
  const r = cv.getBoundingClientRect();
  cv.width = r.width * devicePixelRatio;
  cv.height = r.height * devicePixelRatio;
  draw();
}
window.addEventListener("resize", resize);

const ppm = () => (S.p && S.p.rectified ? S.p.rectified.ppm : 1);
const mmToScreen = ([x, y]) => [x * ppm() * view.scale + view.ox, y * ppm() * view.scale + view.oy];
const pxToScreen = ([x, y]) => [x * view.scale + view.ox, y * view.scale + view.oy];
const screenToPx = (sx, sy) => [(sx - view.ox) / view.scale, (sy - view.oy) / view.scale];
const screenToMm = (sx, sy) => screenToPx(sx, sy).map((v) => v / ppm());
const mmPerScreenPx = () => 1 / (ppm() * view.scale);
const nearScreen = (a, b, r = HANDLE) => { const [ax, ay] = mmToScreen(a), [bx, by] = mmToScreen(b); return Math.hypot(ax - bx, ay - by) < r; };

// ------------------------------------------------------------------ drawing
function poly(pts, toScreen, close = true) {
  ctx.beginPath();
  pts.forEach((p, i) => { const [x, y] = toScreen(p); i ? ctx.lineTo(x, y) : ctx.moveTo(x, y); });
  if (close) ctx.closePath();
}
function orderCorners(pts) {
  if (pts.length < 4) return pts;
  const cx = pts.reduce((a, p) => a + p[0], 0) / 4, cy = pts.reduce((a, p) => a + p[1], 0) / 4;
  const s = [...pts].sort((a, b) => Math.atan2(a[1] - cy, a[0] - cx) - Math.atan2(b[1] - cy, b[0] - cx));
  let k = 0;
  s.forEach((p, i) => { if (p[0] + p[1] < s[k][0] + s[k][1]) k = i; });
  return [...s.slice(k), ...s.slice(0, k)];
}
function label(text, [x, y], color = "#fff", bg = "rgba(0,0,0,.65)") {
  ctx.font = "bold 13px system-ui";
  const w = ctx.measureText(text).width + 8;
  ctx.fillStyle = bg;
  ctx.fillRect(x - w / 2, y - 9, w, 18);
  ctx.fillStyle = color;
  ctx.textAlign = "center"; ctx.textBaseline = "middle";
  ctx.fillText(text, x, y);
}
function handle([x, y], fill = "#fff") {
  ctx.beginPath(); ctx.arc(x, y, 6, 0, Math.PI * 2);
  ctx.fillStyle = fill; ctx.fill();
  ctx.lineWidth = 2; ctx.strokeStyle = "#000"; ctx.stroke();
}
function sheetCorners(sh) {
  return [[sh.x, sh.y], [sh.x + sh.w, sh.y], [sh.x + sh.w, sh.y + sh.h], [sh.x, sh.y + sh.h]];
}
// screen position of the rotate handle for the selected item
function rotateHandle() {
  const it = findSel();
  if (!it || S.mode !== "layout" || !(S.arrange || S.sel.k === "t")) return null;
  const pts = S.sel.k === "c" ? live(it.final.length > 2 ? it.final : it.placed, "c", it.num) : live(it.rings.flat(), "t", it.id);
  if (!pts.length) return null;
  const b = bbox(pts.map(mmToScreen));
  return { top: [(b[0] + b[2]) / 2, b[1]], pos: [(b[0] + b[2]) / 2, b[1] - 28] };
}

function draw() {
  ctx.setTransform(devicePixelRatio, 0, 0, devicePixelRatio, 0, 0);
  ctx.clearRect(0, 0, cv.width, cv.height);
  if (!S.p || !img.complete || !img.naturalWidth) return;
  if (imgKind === "rect" && S.mode === "layout" && S.arrange) {
    ctx.fillStyle = "#000"; ctx.fillRect(0, 0, cv.width, cv.height);
  } else {
    ctx.imageSmoothingEnabled = view.scale < 2;
    ctx.drawImage(img, view.ox, view.oy, img.naturalWidth * view.scale, img.naturalHeight * view.scale);
  }
  if (imgKind === "orig") return drawCalibration();
  if (gridSet().grid_on) drawGrid();

  // sheet outline (+ corner handles on the layout tab)
  const sh = S.p.sheet;
  if (sh) {
    ctx.setLineDash([6, 4]); ctx.lineWidth = 1.5; ctx.strokeStyle = "#35d0ff";
    poly(sheetCorners(sh), mmToScreen); ctx.stroke(); ctx.setLineDash([]);
    if (S.mode === "layout") sheetCorners(sh).forEach((c) => handle(mmToScreen(c), "#35d0ff"));
  }

  // tile preview
  if (S.mode === "layout" && $("#show-tiles").checked && S.tiles) {
    S.tiles.tiles.forEach((t) => {
      ctx.lineWidth = 2; ctx.strokeStyle = t.fits ? "#ffb020" : "#ff3b3b";
      t.rings.forEach((r) => { poly(r, mmToScreen); ctx.stroke(); });
      if (S.tiles.tiles.length > 1 && !S.arrange) label(`piece ${t.row}-${t.col}`, mmToScreen(centroid(t.rings[0])), "#ffb020");
    });
  }

  // cutouts
  visibleCuts().forEach((c) => {
    const sel = S.sel && S.sel.k === "c" && S.sel.id === c.num;
    const tugging = S.tug && S.tug.kind === "cut" && S.tug.num === c.num;
    const final = live(c.final, "c", c.num), placed = tugging ? S.tug.pts : live(c.placed, "c", c.num);
    const color = cutColor(c);
    if (final.length > 2 && !tugging) {
      poly(final, mmToScreen);
      ctx.fillStyle = c.include ? c.color + (sel ? "88" : "55") : (sel ? "rgba(140,140,140,.4)" : "rgba(140,140,140,.22)"); ctx.fill();
      ctx.lineWidth = sel ? 3 : 2; ctx.strokeStyle = color; ctx.stroke();
    }
    ctx.setLineDash([3, 3]); ctx.lineWidth = tugging ? 2 : 1; ctx.strokeStyle = c.include ? "#fff" : "rgba(255,255,255,.4)";
    poly(placed, mmToScreen); ctx.stroke(); ctx.setLineDash([]);
    label(String(c.num), mmToScreen(centroid(final.length > 2 ? final : placed)), "#fff", c.include ? c.color : "#6b6e75");
    if (sel) {
      live(c.grip_pts, "c", c.num).forEach((g, i) => {
        const lg = S.liveGrip;
        handle(mmToScreen(lg && lg.num === c.num && lg.idx === i ? lg.pt : g), c.grips[i].at === "center" ? "#ff8a00" : "#ffd400");
      });
    }
  });

  // text
  S.p.texts.forEach((t) => {
    const sel = S.sel && S.sel.k === "t" && S.sel.id === t.id;
    ctx.beginPath();
    t.rings.forEach((r) => {
      live(r, "t", t.id).forEach((p, i) => { const [x, y] = mmToScreen(p); i ? ctx.lineTo(x, y) : ctx.moveTo(x, y); });
      ctx.closePath();
    });
    ctx.fillStyle = sel ? "#9cc2ff" : "#4c8dff"; ctx.fill("evenodd");
    if (sel && t.rings.length) {
      const b = bbox(live(t.rings.flat(), "t", t.id).map(mmToScreen));
      ctx.setLineDash([4, 3]); ctx.lineWidth = 1; ctx.strokeStyle = "#9cc2ff";
      ctx.strokeRect(b[0] - 4, b[1] - 4, b[2] - b[0] + 8, b[3] - b[1] + 8); ctx.setLineDash([]);
    }
  });

  // rotate handle
  const rh = rotateHandle();
  if (rh) {
    ctx.beginPath(); ctx.moveTo(...rh.top); ctx.lineTo(...rh.pos);
    ctx.lineWidth = 1.5; ctx.strokeStyle = "#fff"; ctx.stroke();
    handle(rh.pos, "#4c8dff");
  }

  // preview, wand clicks, draft
  S.preview.forEach((pp, i) => {
    const pts = S.tug && S.tug.kind === "preview" && S.tug.i === i ? S.tug.pts : pp;
    poly(pts, mmToScreen);
    ctx.fillStyle = "rgba(255,255,255,.25)"; ctx.fill();
    ctx.setLineDash([6, 4]); ctx.lineWidth = 2; ctx.strokeStyle = "#fff"; ctx.stroke(); ctx.setLineDash([]);
  });
  S.seeds.forEach((p) => {
    const [x, y] = mmToScreen(p);
    ctx.lineWidth = 2; ctx.strokeStyle = "#ff3b3b";
    ctx.beginPath(); ctx.moveTo(x - 5, y - 5); ctx.lineTo(x + 5, y + 5); ctx.moveTo(x + 5, y - 5); ctx.lineTo(x - 5, y + 5); ctx.stroke();
  });
  if (S.draft.length) {
    const pts = S.hover ? [...S.draft, S.hover] : S.draft;
    poly(pts, mmToScreen, false);
    ctx.lineWidth = 2; ctx.strokeStyle = "#fff"; ctx.stroke();
    // freehand strokes are dense: mark only the start and where each stroke began
    const marks = S.tool === "freehand" ? [0, ...S.strokes] : S.draft.map((_, i) => i);
    marks.forEach((i) => {
      const [x, y] = mmToScreen(S.draft[i]);
      ctx.fillStyle = i === 0 ? "#35d0ff" : "#fff";
      ctx.fillRect(x - 3, y - 3, 6, 6);
    });
  }
  // smart select box and marks
  if (S.mode === "trace" && S.tool === "smart") {
    const box = drag && drag.type === "smartBox" && drag.moved ? [...drag.start, ...drag.end] : S.smart && S.smart.rect;
    if (box) {
      const [ax, ay] = mmToScreen(box.slice(0, 2)), [bx, by] = mmToScreen(box.slice(2));
      ctx.setLineDash([5, 4]); ctx.lineWidth = 1.5; ctx.strokeStyle = "#ffd400";
      ctx.strokeRect(Math.min(ax, bx), Math.min(ay, by), Math.abs(bx - ax), Math.abs(by - ay)); ctx.setLineDash([]);
    }
    if (S.smart) {
      const mark = (p, col, plus) => {
        const [x, y] = mmToScreen(p);
        ctx.beginPath(); ctx.arc(x, y, 7, 0, Math.PI * 2); ctx.fillStyle = col; ctx.fill();
        ctx.strokeStyle = "#fff"; ctx.lineWidth = 2; ctx.beginPath(); ctx.moveTo(x - 4, y); ctx.lineTo(x + 4, y);
        if (plus) { ctx.moveTo(x, y - 4); ctx.lineTo(x, y + 4); }
        ctx.stroke();
      };
      S.smart.fg.forEach((p) => mark(p, "#1a9e4b", true));
      S.smart.bg.forEach((p) => mark(p, "#d23b3b", false));
    }
  }
  // brush stroke and cursor
  if (S.mode === "trace" && S.tool === "brush") {
    const r = +$("#s-brush").value / 2 / mmPerScreenPx();
    if (S.stroke) {
      poly(S.stroke.pts, mmToScreen, false);
      if (S.stroke.pts.length === 1) { const [x, y] = mmToScreen(S.stroke.pts[0]); ctx.lineTo(x + 0.01, y); }
      ctx.lineCap = ctx.lineJoin = "round"; ctx.lineWidth = 2 * r;
      ctx.strokeStyle = S.stroke.erase ? "rgba(255,59,59,.45)" : "rgba(60,220,120,.45)"; ctx.stroke();
      ctx.lineCap = ctx.lineJoin = "butt"; ctx.lineJoin = "miter";
    }
    if (S.hover && !S.measuring) {
      const [x, y] = mmToScreen(S.hover);
      ctx.beginPath(); ctx.arc(x, y, r, 0, Math.PI * 2);
      ctx.lineWidth = 1; ctx.strokeStyle = S.brushErase ? "rgba(255,120,120,.9)" : "rgba(255,255,255,.8)"; ctx.stroke();
    }
  }
  // tug radius
  if (S.mode === "trace" && S.tool === "tug" && S.hover && !S.measuring) {
    const [x, y] = mmToScreen(S.tug ? S.tug.grab : S.hover);
    ctx.beginPath(); ctx.arc(x, y, +$("#s-tug").value / mmPerScreenPx(), 0, Math.PI * 2);
    ctx.lineWidth = 1; ctx.strokeStyle = "rgba(255,255,255,.7)"; ctx.stroke();
  }

  // measurement
  if (S.measure) {
    const { a, b } = S.measure;
    const [ax, ay] = mmToScreen(a), [bx, by] = mmToScreen(b);
    ctx.lineWidth = 2; ctx.strokeStyle = "#ff2fd0";
    ctx.beginPath(); ctx.moveTo(ax, ay); ctx.lineTo(bx, by); ctx.stroke();
    [[ax, ay], [bx, by]].forEach(([x, y]) => { ctx.beginPath(); ctx.arc(x, y, 4, 0, Math.PI * 2); ctx.fillStyle = "#ff2fd0"; ctx.fill(); });
    label(fmtLen(Math.hypot(b[0] - a[0], b[1] - a[1])), [(ax + bx) / 2, (ay + by) / 2 - 14], "#fff", "#b0169a");
  }
}

function drawGrid() {
  const r = cv.getBoundingClientRect(), o = gridOrigin(), base = gridSet().grid_mm;
  let g = base;
  // keep lines at least 8 px apart when zoomed out
  for (const f of [2, 5, 10, 20, 50, 100]) { if (g / mmPerScreenPx() >= 8) break; g = base * f; }
  const [x0, y0] = screenToMm(0, 0), [x1, y1] = screenToMm(r.width, r.height);
  const lines = (lo, hi, org, fn) => {
    for (let i = Math.ceil((lo - org) / g); org + i * g <= hi; i++) {
      ctx.strokeStyle = i === 0 ? "rgba(255,212,0,.8)" : i % 10 === 0 ? "rgba(255,255,255,.35)" : "rgba(255,255,255,.14)";
      ctx.beginPath(); fn(org + i * g); ctx.stroke();
    }
  };
  ctx.lineWidth = 1;
  lines(x0, x1, o[0], (x) => { const [sx] = mmToScreen([x, 0]); ctx.moveTo(sx, 0); ctx.lineTo(sx, r.height); });
  lines(y0, y1, o[1], (y) => { const [, sy] = mmToScreen([0, y]); ctx.moveTo(0, sy); ctx.lineTo(r.width, sy); });
  const [ox, oy] = mmToScreen(o);
  ctx.beginPath(); ctx.arc(ox, oy, 5, 0, Math.PI * 2); ctx.strokeStyle = "#ffd400"; ctx.lineWidth = 2; ctx.stroke();
}

function drawCalibration() {
  if (S.cropping) {
    if (S.cropRect) {
      const [x0, y0, x1, y1] = S.cropRect;
      const [sx0, sy0] = pxToScreen([Math.min(x0, x1), Math.min(y0, y1)]);
      const [sx1, sy1] = pxToScreen([Math.max(x0, x1), Math.max(y0, y1)]);
      ctx.fillStyle = "rgba(0,0,0,.6)";
      ctx.beginPath(); ctx.rect(0, 0, cv.width, cv.height); ctx.rect(sx0, sy0, sx1 - sx0, sy1 - sy0); ctx.fill("evenodd");
      ctx.setLineDash([6, 4]); ctx.lineWidth = 2; ctx.strokeStyle = "#fff"; ctx.strokeRect(sx0, sy0, sx1 - sx0, sy1 - sy0); ctx.setLineDash([]);
    }
    return;
  }
  const pts = S.corners;
  if (!pts.length) return;
  const ord = orderCorners(pts);
  if (pts.length === 4) {
    poly(ord, pxToScreen);
    ctx.fillStyle = "rgba(53,208,255,.12)"; ctx.fill();
    ctx.lineWidth = 2; ctx.strokeStyle = "#35d0ff"; ctx.stroke();
    const mid = (a, b) => pxToScreen([(a[0] + b[0]) / 2, (a[1] + b[1]) / 2]);
    label("W " + fmtLen(getLen($("#cal-w"))), mid(ord[0], ord[1]), "#35d0ff");
    label("H " + fmtLen(getLen($("#cal-h"))), mid(ord[0], ord[3]), "#35d0ff");
  } else {
    poly(pts, pxToScreen, false); ctx.lineWidth = 2; ctx.strokeStyle = "#35d0ff"; ctx.stroke();
  }
  pts.forEach((p) => {
    const [x, y] = pxToScreen(p);
    ctx.beginPath(); ctx.arc(x, y, 7, 0, Math.PI * 2);
    ctx.lineWidth = 2; ctx.strokeStyle = "#ff3b3b"; ctx.stroke();
    ctx.beginPath(); ctx.moveTo(x - 11, y); ctx.lineTo(x + 11, y); ctx.moveTo(x, y - 11); ctx.lineTo(x, y + 11); ctx.stroke();
  });
}

// ------------------------------------------------------------------ mouse
let drag = null;
let spaceDown = false;

cv.addEventListener("contextmenu", (e) => e.preventDefault());
cv.addEventListener("wheel", (e) => {
  e.preventDefault();
  const [sx, sy] = screenPos(e);
  const f = Math.exp(-e.deltaY * 0.0015);
  view.ox = sx - (sx - view.ox) * f;
  view.oy = sy - (sy - view.oy) * f;
  view.scale *= f;
  draw();
}, { passive: false });

function screenPos(e) {
  const r = cv.getBoundingClientRect();
  return [e.clientX - r.left, e.clientY - r.top];
}

cv.addEventListener("mousedown", (e) => {
  if (!S.p) return;
  const [sx, sy] = screenPos(e);
  if (e.button === 1 || e.button === 2 || spaceDown) {
    drag = { type: "pan", sx, sy, ox: view.ox, oy: view.oy };
    return;
  }
  if (e.button !== 0) return;
  if (imgKind === "orig") return S.cropping ? cropDown(sx, sy) : calibrateClick(sx, sy);
  const mm = screenToMm(sx, sy);
  if (S.pickOrigin) return pickOrigin(mm, e);
  if (S.measuring) return measureClick(snapPt(mm, e));
  if (gripDown(mm, e)) return;
  if (S.mode === "layout") return layoutDown(mm, sx, sy);
  if (S.mode === "trace") traceDown(mm, sx, sy, e);
});

window.addEventListener("mousemove", (e) => {
  if (!S.p) return;
  const [sx, sy] = screenPos(e);
  if (drag && drag.type === "pan") {
    view.ox = drag.ox + sx - drag.sx; view.oy = drag.oy + sy - drag.sy; return draw();
  }
  if (imgKind === "orig") {
    if (drag && drag.type === "corner") { S.corners[drag.i] = screenToPx(sx, sy); draw(); }
    if (drag && drag.type === "crop") { const [x, y] = screenToPx(sx, sy); S.cropRect[2] = x; S.cropRect[3] = y; draw(); }
    return;
  }
  const mm = screenToMm(sx, sy);
  S.hover = e.target === cv ? mm : null;
  cv.style.cursor = (drag && drag.type === "shapeMove") || (!drag && S.hover && overShape(mm)) ? "move" : "";
  if (S.measure && !S.measure.done) S.measure.b = snapPt(mm, e);
  if (drag) dragMove(mm, sx, sy, e);
  if (S.measure || S.draft.length || S.tool === "tug" || S.tool === "brush" || drag) draw();
});

window.addEventListener("mouseup", async () => {
  const d = drag;
  drag = null;
  if (!d || d.type === "pan" || d.type === "corner") return;
  if (d.type === "crop") return updateCropButtons();
  await dragEnd(d);
});
cv.addEventListener("dblclick", (e) => {
  if (S.mode === "trace" && (S.tool === "draw" || S.tool === "freehand")) return finishDraft();
  // double-click text to edit it (layout tab, or the Select tool)
  if (!S.p || imgKind !== "rect" || S.measuring || S.pickOrigin) return;
  if (S.mode !== "layout" && !(S.mode === "trace" && S.tool === "select")) return;
  const hit = hitTest(screenToMm(...screenPos(e)));
  if (hit && hit.k === "t") { S.live = null; openTextEdit(textById(hit.id)); }
});

// ------------------------------------------------------------------ drag handlers (rectified view)
function gripDown(mm, e) {
  const c = S.sel && S.sel.k === "c" ? cutByNum(S.sel.id) : null;
  if (!c || S.mode === "calibrate") return false;
  const idx = c.grip_pts.findIndex((g) => nearScreen(g, mm));
  if (idx < 0) return false;
  if (e.altKey) {
    change("PATCH", `/cutouts/${c.num}`, { grips: c.grips.filter((_, i) => i !== idx) });
  } else {
    drag = { type: "grip", c, idx };
    S.liveGrip = { num: c.num, idx, pt: c.grip_pts[idx] };
  }
  return true;
}

function startLive(k, it) {
  const [cx, cy] = itemCenter(k, it);
  S.live = { k, id: k === "c" ? it.num : it.id, ddx: 0, ddy: 0, drot: 0, cx, cy, anchor: snapAnchor(k, it) };
}

function layoutDown(mm, sx, sy) {
  // sheet corner handles
  const sh = S.p.sheet;
  const corner = sh ? sheetCorners(sh).findIndex((c) => nearScreen(c, mm)) : -1;
  if (corner >= 0) { drag = { type: "sheet", corner, orig: { ...sh } }; return; }
  // rotate handle
  const rh = rotateHandle();
  if (rh && Math.hypot(rh.pos[0] - sx, rh.pos[1] - sy) < HANDLE + 2) {
    startLive(S.sel.k, findSel());
    const [cx, cy] = mmToScreen([S.live.cx, S.live.cy]);
    drag = { type: "rotate", cs: [cx, cy], a0: Math.atan2(sy - cy, sx - cx) };
    return;
  }
  const hit = hitTest(mm);
  S.sel = hit;
  markSelected();
  draw();
  if (hit && (hit.k === "t" || S.arrange)) {
    startLive(hit.k, findSel());
    drag = { type: "move", start: mm, sx, sy, moved: false };
  }
}

function hitTest(mm) {
  for (const t of [...S.p.texts].reverse()) {
    if (!t.rings.length) continue;
    const b = bbox(t.rings.flat());
    if (mm[0] >= b[0] - 1 && mm[0] <= b[2] + 1 && mm[1] >= b[1] - 1 && mm[1] <= b[3] + 1) return { k: "t", id: t.id };
  }
  const tol = HANDLE * mmPerScreenPx();
  for (const c of [...visibleCuts()].reverse()) {
    const shape = c.final.length > 2 ? c.final : c.placed;
    if (pointInPoly(mm, shape) || nearestOnPoly(mm, shape).d < tol) return { k: "c", id: c.num };
  }
  return null;
}

function dragMove(mm, sx, sy, e) {
  if (drag.type === "grip") {
    const c = drag.c;
    S.liveGrip.pt = c.grips[drag.idx].at === "center" ? mm : nearestOnPoly(mm, c.final.length > 2 ? c.final : c.placed).pt;
  } else if (drag.type === "sheet") {
    const o = drag.orig, fx = [o.x, o.x + o.w], fy = [o.y, o.y + o.h];
    const i = drag.corner;  // 0 TL, 1 TR, 2 BR, 3 BL
    const [px, py] = snapPt(mm, e);
    fx[i === 1 || i === 2 ? 1 : 0] = px;
    fy[i >= 2 ? 1 : 0] = py;
    S.p.sheet = { x: Math.min(...fx), y: Math.min(...fy), w: Math.abs(fx[1] - fx[0]), h: Math.abs(fy[1] - fy[0]) };
  } else if (drag.type === "rotate") {
    let a = ((Math.atan2(sy - drag.cs[1], sx - drag.cs[0]) - drag.a0) * 180) / Math.PI;
    if (e.shiftKey) a = Math.round(a / 15) * 15;
    S.live.drot = a;
  } else if (drag.type === "move") {
    if (Math.hypot(sx - drag.sx, sy - drag.sy) > 3) drag.moved = true;
    if (drag.moved) {
      const a = S.live.anchor;
      const [tx, ty] = snapPt([a[0] + mm[0] - drag.start[0], a[1] + mm[1] - drag.start[1]], e);
      S.live.ddx = tx - a[0]; S.live.ddy = ty - a[1];
    }
  } else if (drag.type === "shape") {
    if (Math.hypot(sx - drag.sx, sy - drag.sy) > 4) drag.moved = true;
    if (drag.moved) shapeDrag(drag.start, snapPt(mm, e), e.shiftKey);
  } else if (drag.type === "smartBox") {
    if (Math.hypot(sx - drag.sx, sy - drag.sy) > 4) drag.moved = true;
    if (drag.moved) drag.end = mm;
  } else if (drag.type === "free") {
    const last = mmToScreen(S.draft[S.draft.length - 1]);
    if (Math.hypot(sx - last[0], sy - last[1]) >= 3) S.draft.push(mm);
  } else if (drag.type === "brush") {
    const pts = S.stroke.pts, last = mmToScreen(pts[pts.length - 1]);
    if (Math.hypot(sx - last[0], sy - last[1]) >= 2) pts.push(mm);
  } else if (drag.type === "shapeMove") {
    const c = snapPt([drag.c0[0] + mm[0] - drag.start[0], drag.c0[1] + mm[1] - drag.start[1]], e);
    setShape({ ...S.shape, c });
  } else if (drag.type === "tug") {
    const T = S.tug, R = +$("#s-tug").value;
    const dx = mm[0] - T.grab[0], dy = mm[1] - T.grab[1];
    T.pts = T.base.map(([x, y]) => {
      const w = Math.max(0, 1 - (Math.hypot(x - T.grab[0], y - T.grab[1]) / R) ** 2) ** 2;
      return [x + w * dx, y + w * dy];
    });
  }
}

async function dragEnd(d) {
  if (d.type === "grip") {
    const c = d.c;
    const [raw] = unplace([S.liveGrip.pt], c);
    const grips = c.grips.map((g, i) => (i === d.idx ? { ...g, x: raw[0], y: raw[1] } : g));
    await change("PATCH", `/cutouts/${c.num}`, { grips });
  } else if (d.type === "sheet") {
    await change("PATCH", "/settings", { sheet: S.p.sheet });
  } else if (d.type === "rotate" || (d.type === "move" && d.moved)) {
    await commitLive();
  } else if (d.type === "move") {
    // a plain click on a cutout's straight edge squares it up
    S.live = null;
    const c = S.sel && S.sel.k === "c" && S.arrange ? cutByNum(S.sel.id) : null;
    if (c && nearestOnPoly(d.start, c.placed).d < HANDLE * mmPerScreenPx() * 1.5) {
      const ang = edgeAngle(d.start, c.placed);
      const drot = Math.round(ang / 90) * 90 - ang;
      if (Math.abs(drot) > 0.05) await change("PATCH", `/cutouts/${c.num}`, { rot: c.rot + drot });
    }
    draw();
  } else if (d.type === "shape") {
    if (!d.moved) placeShape(S.tool, d.start);
  } else if (d.type === "smartBox") {
    if (d.moved) {
      S.smart = { rect: [...d.start, ...d.end], fg: [], bg: [] };
    } else if (S.smart) {
      S.smart[d.alt ? "bg" : "fg"].push(d.start);
    } else return status("#trace-status", "Drag a box around the tool first.");
    runDetect();
  } else if (d.type === "free") {
    // letting go near the start closes the outline
    const [fx, fy] = mmToScreen(S.draft[0]), [lx, ly] = mmToScreen(S.draft[S.draft.length - 1]);
    if (S.draft.length > 10 && S.strokes.length && Math.hypot(fx - lx, fy - ly) < 10) finishDraft();
    else draw();
  } else if (d.type === "brush") {
    await applyBrush(S.stroke);
    S.stroke = null; draw();
  } else if (d.type === "tug") {
    const T = S.tug;
    if (T.kind === "preview") {
      S.preview[T.i] = T.pts; S.tug = null; draw();
    } else {
      const c = cutByNum(T.num);
      await change("PATCH", `/cutouts/${c.num}`, { raw: unplace(T.pts, c) });
    }
  }
}

async function commitLive() {
  const L = S.live;
  if (!L) return;
  if (L.k === "c") {
    // rotating about the placed pivot = adding to rot; moving = adding to dx/dy
    const c = cutByNum(L.id);
    await change("PATCH", `/cutouts/${L.id}`, { rot: c.rot + L.drot, dx: c.dx + L.ddx, dy: c.dy + L.ddy });
  } else {
    const t = textById(L.id);
    await change("PATCH", `/texts/${L.id}`, { rot: t.rot + L.drot, x: t.x + L.ddx, y: t.y + L.ddy });
  }
}

// keyboard/button nudges accumulate into S.live and are saved after a short pause
let nudgeTimer;
function nudge(ddx, ddy, drot) {
  const it = findSel();
  if (!it) return;
  if (!S.live || S.live.k !== S.sel.k || S.live.id !== S.sel.id) startLive(S.sel.k, it);
  S.live.ddx += ddx; S.live.ddy += ddy; S.live.drot += drot;
  draw();
  clearTimeout(nudgeTimer);
  nudgeTimer = setTimeout(commitLive, 400);
}
// with snap on, arrows jump the anchor to the next grid line in that direction
function gridNudge(sx, sy) {
  const it = findSel();
  if (!it) return;
  const L = S.live && S.live.k === S.sel.k && S.live.id === S.sel.id ? S.live : null;
  const a0 = snapAnchor(S.sel.k, it), a = [a0[0] + (L ? L.ddx : 0), a0[1] + (L ? L.ddy : 0)];
  const g = gridSet().grid_mm, o = gridOrigin();
  const step = (v, org, d) => {
    if (!d) return v;
    const n = (v - org) / g;
    return org + (d > 0 ? Math.floor(n + 1e-6) + 1 : Math.ceil(n - 1e-6) - 1) * g;
  };
  nudge(step(a[0], o[0], sx) - a[0], step(a[1], o[1], sy) - a[1], 0);
}
$("#rot-l").onclick = () => nudge(0, 0, -90);
$("#rot-r").onclick = () => nudge(0, 0, 90);

// ------------------------------------------------------------------ keyboard
window.addEventListener("keydown", (e) => {
  if (e.target.matches("input, select, textarea") || $("#edit").open || $("#tedit").open) return;
  if (e.code === "Space") { spaceDown = true; e.preventDefault(); }
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "z") { e.preventDefault(); return undo(); }
  if (e.key.toLowerCase() === "m" && !e.ctrlKey) return toggleMeasure();
  if (e.key === "Escape") {
    if (S.measuring || S.measure) { S.measure = null; setMeasuring(false); }
    if (S.cropping) endCrop();
    if (S.pickOrigin) setPickOrigin(false);
  }
  if ((e.key === "Delete" || (e.key === "Backspace" && !S.draft.length)) && S.sel && S.mode !== "calibrate") {
    e.preventDefault();
    return S.sel.k === "c" ? deleteCutout(S.sel.id) : deleteText(S.sel.id);
  }
  if (S.mode === "layout" && S.sel && (S.arrange || S.sel.k === "t")) {
    const step = e.shiftKey ? 10 : 1, rstep = e.shiftKey ? 15 : 1;
    const k = { ArrowLeft: [-step, 0, 0], ArrowRight: [step, 0, 0], ArrowUp: [0, -step, 0], ArrowDown: [0, step, 0],
      "[": [0, 0, -rstep], "]": [0, 0, rstep], "{": [0, 0, -rstep], "}": [0, 0, rstep] }[e.key];
    if (k && snapOn() && !k[2]) { e.preventDefault(); gridNudge(Math.sign(k[0]), Math.sign(k[1])); }
    else if (k) { e.preventDefault(); nudge(...k); }
  }
  if (S.mode !== "trace") return;
  if (e.key === "Enter") { if (S.draft.length >= 3) finishDraft(); addPreview(); }
  if (e.key === "Escape") clearPreview();
  if (e.key === "Backspace" && S.draft.length) {
    if (S.tool === "freehand" && S.strokes.length) S.draft.length = S.strokes.pop();
    else S.draft.pop();
    draw();
  }
});
window.addEventListener("keyup", (e) => { if (e.code === "Space") spaceDown = false; });

// ------------------------------------------------------------------ measure / undo
function setMeasuring(on) {
  S.measuring = on;
  $("#measure").classList.toggle("active", on);
  if (!on && S.measure && !S.measure.done) S.measure = null;
  draw();
}
function toggleMeasure() {
  if (!S.p || !S.p.rectified || S.mode === "calibrate") return;
  setMeasuring(!S.measuring);
}
$("#measure").onclick = toggleMeasure;
function measureClick(mm) {
  if (!S.measure || S.measure.done) S.measure = { a: mm, b: mm, done: false };  // a new one clears the old
  else { S.measure.b = mm; S.measure.done = true; }
  draw();
}
async function undo() {
  if (!S.p) return;
  try { setProject(await api("POST", P("/undo"))); } catch { /* nothing to undo */ }
}
$("#undo").onclick = undo;

// ------------------------------------------------------------------ calibrate + crop
function calibrateClick(sx, sy) {
  const i = S.corners.findIndex((p) => { const [x, y] = pxToScreen(p); return Math.hypot(x - sx, y - sy) < 12; });
  if (i >= 0) { drag = { type: "corner", i }; return; }
  if (S.corners.length < 4) S.corners.push(screenToPx(sx, sy));
  draw();
}
$("#cal-clear").onclick = () => { S.corners = []; draw(); };
["#cal-w", "#cal-h"].forEach((id) => $(id).addEventListener("len", draw));
$("#cal-go").onclick = async () => {
  const w = getLen($("#cal-w")), h = getLen($("#cal-h")), m = getLen($("#cal-m")) || 0;
  if (S.corners.length !== 4) return status("#cal-status", "Click all 4 corners first.");
  if (!(w > 0 && h > 0)) return status("#cal-status", "Enter the width and height.");
  if ((S.p.cutouts.length || S.p.texts.length) && !confirm("Re-calibrating removes the existing cutouts and text. Continue?")) return;
  status("#cal-status", "Working…");
  try {
    const p = await api("POST", P("/rectify"), { corners: S.corners, width_mm: w, height_mm: h, margin_mm: m });
    status("#cal-status", `Done – ${p.rectified.ppm.toFixed(2)} px/mm.`);
    setProject(p);
    setMode("trace");
  } catch (err) { status("#cal-status", err.message); }
};
function status(sel, msg) { $(sel).textContent = msg; }

function startCrop() {
  S.cropping = true; S.cropRect = null;
  updateCropButtons(); draw();
}
function endCrop() {
  S.cropping = false; S.cropRect = null;
  updateCropButtons(); draw();
}
function cropDown(sx, sy) {
  const [x, y] = screenToPx(sx, sy);
  S.cropRect = [x, y, x, y];
  drag = { type: "crop" };
}
function updateCropButtons() {
  $("#crop-start").hidden = S.cropping;
  $("#crop-apply").hidden = !S.cropping;
  $("#crop-cancel").hidden = !S.cropping;
  $("#crop-hint").hidden = !S.cropping;
  $("#crop-apply").disabled = !S.cropRect || Math.abs(S.cropRect[2] - S.cropRect[0]) < 10;
}
async function afterCrop() {
  S.corners = S.p.calib ? S.p.calib.corners.map((c) => [...c]) : [];
  draw();
}
$("#crop-start").onclick = startCrop;
$("#crop-cancel").onclick = endCrop;
$("#crop-apply").onclick = async () => {
  const [x0, y0, x1, y1] = S.cropRect;
  const r = { x: Math.min(x0, x1), y: Math.min(y0, y1), w: Math.abs(x1 - x0), h: Math.abs(y1 - y0) };
  endCrop();
  await change("POST", "/crop", r);
  afterCrop();
};
$("#crop-reset").onclick = async () => { await change("POST", "/crop", { reset: true }); afterCrop(); };

// ------------------------------------------------------------------ trace
const ICON = {
  eye: '<svg viewBox="0 0 16 16"><path d="M1 8s2.6-5 7-5 7 5 7 5-2.6 5-7 5-7-5-7-5z"/><circle cx="8" cy="8" r="2.2"/></svg>',
  eyeOff: '<svg viewBox="0 0 16 16"><path d="M1 8s2.6-5 7-5 7 5 7 5-2.6 5-7 5-7-5-7-5z"/><circle cx="8" cy="8" r="2.2"/><path d="M2 14 14 2"/></svg>',
  cut: '<svg viewBox="0 0 16 16"><circle cx="4" cy="12" r="2"/><circle cx="12" cy="12" r="2"/><path d="M5.3 10.5 12 2M10.7 10.5 4 2"/></svg>',
  cutOff: '<svg viewBox="0 0 16 16"><circle cx="4" cy="12" r="2"/><circle cx="12" cy="12" r="2"/><path d="M5.3 10.5 12 2M10.7 10.5 4 2"/><path d="M1 15 15 1" class="slash"/></svg>',
};
const TOOL_HINTS = {
  select: "Click a cutout to select it. Drag its grip handle (yellow = edge, orange = centre). Delete removes it.",
  wand: "Click on a tool. Click more spots to grow the selection (touching areas merge). Enter adds it.",
  threshold: "Click on a tool. Slide Threshold until the dark/light split matches. Extra clicks add more.",
  background: "Click on empty foam/background. Every object inside the sheet is found at once.",
  draw: "Click points around the tool. Click the first point or double-click to close. Backspace undoes.",
  tug: "Drag an outline (preview or cutout) to push or pull it. The circle shows how much moves.",
  smart: "Drag a box around one tool. Then click on missed bits of the tool, or Alt+click on background it grabbed. Enter adds it.",
  colorkey: "Click the background (best with a coloured background). Click more spots to add shades of it. Every object inside the sheet is found.",
  freehand: "Drag to trace the outline. Let go to pan or zoom, then drag again: it carries on from where you stopped. Enter or double-click closes it; Backspace removes the last stroke.",
  brush: "Paint over an outline (preview or cutout) to add to it; Erase (or hold Alt) paints it away. Painting on empty space starts a new outline.",
  circle: "Click to place a circle of this radius (centred on the click), or drag out from the centre. Drag the circle to move it. Enter adds it.",
  rect: "Click to place a rectangle of this size (centred on the click), or drag from a corner for a square (Shift = any rectangle). Drag the rectangle to move it. Enter adds it.",
};
function setTool(t) {
  S.tool = t;
  if (t !== "smart") S.smart = null;
  S.strokes = [];
  $$(".tools button").forEach((b) => b.classList.toggle("active", b.dataset.tool === t));
  $$("[data-for]").forEach((s) => (s.style.display = s.dataset.for.split(" ").includes(t) ? "" : "none"));
  $("#tool-hint").textContent = TOOL_HINTS[t];
  S.draft = [];
  draw();
}
$$(".tools button").forEach((b) => (b.onclick = () => setTool(b.dataset.tool)));

function traceDown(mm, sx, sy, e) {
  if (S.tool === "select") { S.sel = hitTest(mm); markSelected(); return draw(); }
  if (S.tool === "smart") { drag = { type: "smartBox", start: mm, sx, sy, moved: false, alt: e.altKey }; return; }
  if (S.tool === "freehand") {
    S.strokes.push(S.draft.length);
    S.draft.push(mm);  // joins on from the end of the last stroke
    drag = { type: "free", sx, sy };
    return draw();
  }
  if (S.tool === "brush") {
    S.stroke = { pts: [mm], erase: S.brushErase !== e.altKey };
    drag = { type: "brush" };
    return draw();
  }
  if (S.tool === "circle" || S.tool === "rect") {
    // grab the placed shape to move it; anywhere else places / drags out a new one
    if (overShape(mm)) { drag = { type: "shapeMove", start: mm, c0: S.shape.c }; return; }
    drag = { type: "shape", start: snapPt(mm, e), sx, sy, moved: false };
    return;
  }
  if (S.tool === "draw") {
    if (S.draft.length > 2) {
      const [fx, fy] = mmToScreen(S.draft[0]);
      if (Math.hypot(fx - sx, fy - sy) < 10) return finishDraft();
    }
    S.draft.push(mm); draw(); return;
  }
  if (S.tool === "tug") return tugDown(mm);
  if (S.tool === "background") S.seeds = [mm];  // colour key: every click adds a background shade
  else S.seeds.push(mm);  // wand/threshold: every click adds to the selection
  runDetect();
}
function tugDown(mm) {
  const R = +$("#s-tug").value;
  let best = null;
  S.preview.forEach((pp, i) => {
    const d = nearestOnPoly(mm, pp).d;
    if (d < R && (!best || d < best.d)) best = { d, kind: "preview", i, pts: pp };
  });
  visibleCuts().forEach((c) => {
    const d = nearestOnPoly(mm, c.placed).d;
    if (d < R && (!best || d < best.d)) best = { d, kind: "cut", num: c.num, pts: c.placed };
  });
  if (!best) return status("#trace-status", "Tug: start the drag closer to an outline.");
  const base = resample(best.pts, Math.min(1, R / 6));
  S.tug = { kind: best.kind, i: best.i, num: best.num, base, pts: base, grab: mm };
  drag = { type: "tug" };
}
// exact shapes
// the circle/rectangle waiting to be added, if the point is on it
const overShape = (mm) => S.mode === "trace" && ["circle", "rect"].includes(S.tool) && S.shape &&
  S.preview[0] === S.shape.pts && pointInPoly(mm, S.shape.pts);
function circlePts([cx, cy], r) {
  const n = Math.max(48, Math.min(360, Math.ceil(r * 4)));
  return Array.from({ length: n }, (_, i) => [cx + r * Math.cos((2 * Math.PI * i) / n), cy + r * Math.sin((2 * Math.PI * i) / n)]);
}
function setShape(sh) {
  sh.pts = sh.kind === "circle" ? circlePts(sh.c, sh.r)
    : [[sh.c[0] - sh.w / 2, sh.c[1] - sh.h / 2], [sh.c[0] + sh.w / 2, sh.c[1] - sh.h / 2],
       [sh.c[0] + sh.w / 2, sh.c[1] + sh.h / 2], [sh.c[0] - sh.w / 2, sh.c[1] + sh.h / 2]];
  S.shape = sh; S.preview = [sh.pts]; S.seeds = [];
  showShapeSize();
  status("#trace-status", "Enter to add it as a cutout, Esc to clear.");
  draw();
}
function placeShape(kind, c) {
  const r = getLen($("#sh-r")), w = getLen($("#sh-rw")), h = getLen($("#sh-rh"));
  if (kind === "circle" ? !(r > 0) : !(w > 0 && h > 0)) return status("#trace-status", "Enter a size first.");
  setShape({ kind, c, r, w, h });
}
function shapeDrag(a, b, free) {
  if (S.tool === "circle") {
    const r = Math.hypot(b[0] - a[0], b[1] - a[1]);
    if (r < 0.5) return;
    setLen($("#sh-r"), +r.toFixed(2));
    return setShape({ kind: "circle", c: a, r });
  }
  let dx = b[0] - a[0], dy = b[1] - a[1];
  if (!free) { const side = Math.max(Math.abs(dx), Math.abs(dy)); dx = Math.sign(dx || 1) * side; dy = Math.sign(dy || 1) * side; }
  const w = Math.abs(dx), h = Math.abs(dy);
  if (w < 0.5 || h < 0.5) return;
  setLen($("#sh-rw"), +w.toFixed(2)); setLen($("#sh-rh"), +h.toFixed(2));
  setShape({ kind: "rect", c: [a[0] + dx / 2, a[1] + dy / 2], w, h });
}
function showShapeSize() { $("#sh-d").textContent = "Diameter " + fmtLen(2 * (getLen($("#sh-r")) || 0)); }
// typing a new size resizes the shape already placed
["#sh-r", "#sh-rw", "#sh-rh"].forEach((id) => $(id).addEventListener("len", () => {
  showShapeSize();
  if (S.shape && S.preview[0] === S.shape.pts && S.mode === "trace") placeShape(S.shape.kind, S.shape.c);
}));
function shapeName(sh) {
  return sh.kind === "circle" ? `Circle ⌀${fmtLen(2 * sh.r)}` : `Rectangle ${fmtLen(sh.w, false)} × ${fmtLen(sh.h)}`;
}

// brush: paint into / out of the preview or cutout where the stroke starts
function setBrushErase(on) {
  S.brushErase = on;
  $("#br-add").classList.toggle("active", !on); $("#br-erase").classList.toggle("active", on);
  draw();
}
$("#br-add").onclick = () => setBrushErase(false);
$("#br-erase").onclick = () => setBrushErase(true);
async function applyBrush(st) {
  const r = +$("#s-brush").value / 2, start = st.pts[0];
  const near = (pts) => pointInPoly(start, pts) || nearestOnPoly(start, pts).d <= r;
  const pi = S.preview.findIndex(near);
  const cut = pi < 0 ? [...visibleCuts()].reverse().find((c) => near(c.placed)) : null;
  if (pi < 0 && !cut && st.erase) return;
  try {
    if (cut) {
      const res = await api("POST", P("/brush"), { polygons: [cut.raw], stroke: unplace(st.pts, cut), radius_mm: r, erase: st.erase });
      if (!res.polygons.length) return status("#trace-status", "That would erase the whole cutout - use the trash can instead.");
      const big = res.polygons.reduce((a, q) => (polyArea(q) > polyArea(a) ? q : a));
      await change("PATCH", `/cutouts/${cut.num}`, { raw: big });
    } else {
      const polys = pi >= 0 ? [S.preview[pi]] : [];
      const res = await api("POST", P("/brush"), { polygons: polys, stroke: st.pts, radius_mm: r, erase: st.erase });
      S.preview = pi >= 0 ? [...S.preview.slice(0, pi), ...res.polygons, ...S.preview.slice(pi + 1)] : [...S.preview, ...res.polygons];
      S.shape = null;
      status("#trace-status", `${S.preview.length} outline(s). Enter to add, Esc to start over.`);
    }
  } catch (err) { status("#trace-status", err.message); }
}

function finishDraft() {
  // a dblclick also fires two clicks; drop the duplicate points
  const d = S.draft.filter((p, i, a) => i === 0 || Math.hypot(p[0] - a[i - 1][0], p[1] - a[i - 1][1]) > 0.5);
  if (d.length >= 3) S.preview = [...S.preview, d];
  S.draft = []; S.strokes = [];
  draw();
}

let detectSeq = 0;
async function runDetect() {
  const smart = S.tool === "smart";
  if (smart ? !S.smart : !S.seeds.length || !["wand", "threshold", "background", "colorkey"].includes(S.tool)) return;
  const seq = ++detectSeq;
  status("#trace-status", "Detecting…");
  try {
    const r = await api("POST", P("/detect"), {
      mode: S.tool, points: S.seeds, ...(smart ? S.smart : {}),
      tolerance: +$("#s-tol").value, threshold: +$("#s-thr").value,
      smooth_mm: +$("#s-smooth").value, simplify_mm: +$("#s-simp").value,
      min_area_mm2: +$("#s-area").value * 100,
    });
    if (seq !== detectSeq) return;
    S.preview = r.polygons;
    status("#trace-status", r.warning || `${r.polygons.length} outline(s) found. Enter to add, Esc to start over.`);
    draw();
  } catch (err) { status("#trace-status", err.message); }
}
let detectTimer;
[["#s-tol", "#o-tol"], ["#s-thr", "#o-thr"], ["#s-area", "#o-area"], ["#s-smooth", "#o-smooth"], ["#s-simp", "#o-simp"], ["#s-tug", "#o-tug"], ["#s-brush", "#o-brush"]]
  .forEach(([s, o]) => {
    const upd = () => ($(o).textContent = $(s).value);
    upd();
    $(s).addEventListener("input", () => {
      upd();
      if (s === "#s-tug" || s === "#s-brush") return draw();
      clearTimeout(detectTimer); detectTimer = setTimeout(runDetect, 150);
    });
  });

function clearPreview() {
  S.preview = []; S.draft = []; S.strokes = []; S.seeds = []; S.shape = null; S.smart = null;
  status("#trace-status", ""); draw();
}
async function addPreview() {
  if (!S.preview.length) return;
  const polygons = S.preview;
  // an untouched circle/rectangle keeps its exact points, square corners and a descriptive name
  const sh = S.shape && polygons.length === 1 && polygons[0] === S.shape.pts ? S.shape : null;
  clearPreview();
  await change("POST", "/cutouts", sh ? { polygons, shape: sh.kind, name: shapeName(sh) } : { polygons });
  const last = S.p.cutouts[S.p.cutouts.length - 1];
  if (last) select("c", last.num);
  status("#trace-status", "Added.");
}
$("#prev-clear").onclick = clearPreview;
$("#prev-add").onclick = addPreview;

// ------------------------------------------------------------------ tables
function renderTables() {
  renderCutTable();
  renderTextTable();
}
function inlineEdit(td, value, save) {
  td.innerHTML = "";
  const inp = document.createElement("input");
  inp.value = value;
  td.appendChild(inp);
  inp.focus(); inp.select();
  let done = false;
  const finish = (ok) => {
    if (done) return;
    done = true;
    if (ok && inp.value.trim() && inp.value !== value) save(inp.value.trim());
    else renderTables();
  };
  inp.addEventListener("keydown", (e) => {
    if (e.key === "Enter") finish(true);
    if (e.key === "Escape") finish(false);
    e.stopPropagation();
  });
  inp.addEventListener("blur", () => finish(true));
  inp.addEventListener("click", (e) => e.stopPropagation());
}
function markSelected() {
  $$("#cut-table tbody tr").forEach((tr) => tr.classList.toggle("sel", !!S.sel && S.sel.k === "c" && +tr.dataset.id === S.sel.id));
  $$("#text-table tbody tr").forEach((tr) => tr.classList.toggle("sel", !!S.sel && S.sel.k === "t" && +tr.dataset.id === S.sel.id));
}
function select(k, id) { S.sel = { k, id }; markSelected(); draw(); }

function renderCutTable() {
  const tb = $("#cut-table tbody");
  tb.innerHTML = "";
  const cuts = S.p ? S.p.cutouts : [];
  $("#no-cuts").style.display = cuts.length ? "none" : "";
  cuts.forEach((c) => {
    const [x0, y0, x1, y1] = c.final.length > 2 ? bbox(c.final) : [0, 0, 0, 0];
    const tr = document.createElement("tr");
    tr.dataset.id = c.num;
    const hid = S.hidden.has(c.num);
    tr.className = (c.include ? "" : "off") + (hid ? " hid" : "");
    tr.innerHTML = `
      <td>${c.num}</td>
      <td><span class="swatch" style="background:${cutColor(c)}"></span></td>
      <td class="name" title="Double-click to rename"></td>
      <td>${fmtLen(x1 - x0, false)}×${fmtLen(y1 - y0)}</td>
      <td>${c.buffer_mm >= 0 ? "+" : ""}${fmtLen(c.buffer_mm)}</td>
      <td class="acts"><button class="icon eye${hid ? " on" : ""}" title="${hid ? "Hidden – click to show" : "Hide while editing (still exported)"}">${hid ? ICON.eyeOff : ICON.eye}</button><button class="icon out${c.include ? "" : " on"}" title="${c.include ? "Cut this – click to leave it out of the export" : "Left out of the export – click to cut it"}">${c.include ? ICON.cut : ICON.cutOff}</button><button class="icon edit" title="Edit">✎</button><button class="icon trash" title="Delete">🗑</button></td>`;
    const name = tr.querySelector(".name");
    name.textContent = c.name;
    tr.querySelector(".eye").onclick = (e) => { e.stopPropagation(); toggleHidden(c.num); };
    tr.querySelector(".out").onclick = (e) => { e.stopPropagation(); change("PATCH", `/cutouts/${c.num}`, { include: !c.include }); };
    name.ondblclick = (e) => { e.stopPropagation(); inlineEdit(name, c.name, (v) => change("PATCH", `/cutouts/${c.num}`, { name: v })); };
    tr.onclick = () => select("c", c.num);
    tr.querySelector(".edit").onclick = (e) => { e.stopPropagation(); openEdit(c); };
    tr.querySelector(".trash").onclick = (e) => { e.stopPropagation(); deleteCutout(c.num); };
    tb.appendChild(tr);
  });
  markSelected();
}
function renderTextTable() {
  const tb = $("#text-table tbody");
  tb.innerHTML = "";
  const texts = S.p ? S.p.texts : [];
  $("#no-texts").style.display = texts.length ? "none" : "";
  texts.forEach((t) => {
    const tr = document.createElement("tr");
    tr.dataset.id = t.id;
    tr.innerHTML = `<td>${t.id}</td><td class="name multi" title="Double-click to edit"></td><td>${fmtLen(t.size_mm)}</td>
      <td style="white-space:nowrap"><button class="icon edit" title="Edit">✎</button><button class="icon trash" title="Delete">🗑</button></td>`;
    const name = tr.querySelector(".name");
    name.textContent = t.text;
    name.ondblclick = (e) => { e.stopPropagation(); openTextEdit(t); };
    tr.onclick = () => select("t", t.id);
    tr.querySelector(".edit").onclick = (e) => { e.stopPropagation(); openTextEdit(t); };
    tr.querySelector(".trash").onclick = (e) => { e.stopPropagation(); deleteText(t.id); };
    tb.appendChild(tr);
  });
  markSelected();
}
function toggleHidden(num) {
  if (S.hidden.has(num)) S.hidden.delete(num);
  else {
    S.hidden.add(num);
    if (S.sel && S.sel.k === "c" && S.sel.id === num) S.sel = null;
  }
  renderTables(); draw();
}
async function deleteCutout(num) {
  const c = cutByNum(num);
  if (!c || !confirm(`Delete #${num} "${c.name}"?`)) return;
  S.sel = null;
  await change("DELETE", `/cutouts/${num}`);
}
async function deleteText(id) {
  const t = textById(id);
  if (!t || !confirm(`Delete text "${t.text}"?`)) return;
  S.sel = null;
  await change("DELETE", `/texts/${id}`);
}

// cutout edit dialog
let editing = null;
function fillGripCount() { const c = cutByNum(editing); $("#ed-grips").textContent = c ? c.grips.length : 0; }
function openEdit(c) {
  editing = c.num;
  select("c", c.num);
  $("#ed-num").textContent = "#" + c.num;
  $("#ed-name").value = c.name;
  setLen($("#ed-buf"), c.buffer_mm);
  setLen($("#ed-smooth"), c.smooth_mm || 0);
  $("#ed-rot").value = +c.rot.toFixed(2);
  $("#ed-color").value = c.color;
  $("#ed-include").checked = c.include;
  fillGripCount();
  $("#edit").showModal();
}
$("#ed-grip-add").onclick = async () => { await change("POST", `/cutouts/${editing}/grip`, { at: "edge" }); fillGripCount(); };
$("#ed-grip-add-c").onclick = async () => { await change("POST", `/cutouts/${editing}/grip`, { at: "center" }); fillGripCount(); };
$("#ed-grip-clear").onclick = async () => { await change("PATCH", `/cutouts/${editing}`, { grips: [] }); fillGripCount(); };
$("#edit").addEventListener("close", async () => {
  if ($("#edit").returnValue !== "ok" || editing === null) return;
  await change("PATCH", `/cutouts/${editing}`, {
    name: $("#ed-name").value, buffer_mm: getLen($("#ed-buf")) || 0, smooth_mm: getLen($("#ed-smooth")) || 0,
    rot: +$("#ed-rot").value || 0, color: $("#ed-color").value, include: $("#ed-include").checked,
  });
});

// text edit dialog (also used for new text: tediting = "new")
let tediting = null;
const tfmt = { align: "center", bold: true, italic: false };
function showTextFormat() {
  $$("#tedit [data-align]").forEach((b) => b.classList.toggle("active", b.dataset.align === tfmt.align));
  $("#te-bold").classList.toggle("active", tfmt.bold);
  $("#te-italic").classList.toggle("active", tfmt.italic);
  const ta = $("#te-text");
  ta.style.textAlign = tfmt.align;
  ta.style.fontWeight = tfmt.bold ? "bold" : "normal";
  ta.style.fontStyle = tfmt.italic ? "italic" : "normal";
  ta.className = $("#te-font").value;
}
function openTextDialog(t) {
  tediting = t ? t.id : "new";
  $("#te-title").textContent = t ? "Edit text" : "Add text";
  $("#te-num").textContent = t ? "#" + t.id : "";
  $("#te-text").value = t ? t.text : "";
  setLen($("#te-size"), t ? t.size_mm : 8);
  $("#te-rot").value = t ? +t.rot.toFixed(2) : 0;
  $("#te-spacing").value = t ? t.line_spacing : 1;
  $("#te-font").value = t ? t.font : "sans";
  Object.assign(tfmt, t ? { align: t.align, bold: t.bold, italic: t.italic } : { align: "center", bold: true, italic: false });
  showTextFormat();
  $("#tedit").showModal();
  $("#te-text").focus();
  $("#te-text").select();
}
function openTextEdit(t) { select("t", t.id); openTextDialog(t); }
$$("#tedit [data-align]").forEach((b) => (b.onclick = () => { tfmt.align = b.dataset.align; showTextFormat(); }));
$("#te-bold").onclick = () => { tfmt.bold = !tfmt.bold; showTextFormat(); };
$("#te-italic").onclick = () => { tfmt.italic = !tfmt.italic; showTextFormat(); };
$("#te-font").onchange = showTextFormat;
// Enter saves, Shift+Enter is a line break; Ctrl+B / Ctrl+I toggle bold / italic
$("#te-text").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); $("#tedit").close("ok"); }
  const k = e.key.toLowerCase();
  if ((e.ctrlKey || e.metaKey) && (k === "b" || k === "i")) { e.preventDefault(); $(k === "b" ? "#te-bold" : "#te-italic").click(); }
});
$("#tedit").addEventListener("close", async () => {
  const text = $("#te-text").value.replace(/\s+$/, "");
  if ($("#tedit").returnValue !== "ok" || tediting === null || !text.trim()) return;
  const body = {
    text, size_mm: getLen($("#te-size")) || 8, rot: +$("#te-rot").value || 0,
    line_spacing: +$("#te-spacing").value || 1, font: $("#te-font").value, ...tfmt,
  };
  if (tediting === "new") {
    await change("POST", "/texts", body);
    const t = S.p.texts[S.p.texts.length - 1];
    if (t) select("t", t.id);
  } else {
    await change("PATCH", `/texts/${tediting}`, body);
  }
});
$("#text-add").onclick = () => { if (S.p && S.p.sheet) openTextDialog(null); };

// ------------------------------------------------------------------ layout
function syncLayoutInputs() {
  const s = S.p.settings;
  if (S.p.sheet) { setLen($("#sh-w"), S.p.sheet.w); setLen($("#sh-h"), S.p.sheet.h); }
  setLen($("#def-buf"), s.default_buffer_mm);
  setLen($("#grip-d"), s.grip_d_mm);
  $("#grip-auto").checked = s.grip_auto;
  $("#grip-at").value = s.grip_at;
  setLen($("#bed-w"), s.bed_w); setLen($("#bed-h"), s.bed_h);
  setLen($("#tab"), s.tab_mm); setLen($("#tab-sp"), s.tab_spacing_mm);
  $("#joint").value = s.joint; $("#nx").value = s.nx; $("#ny").value = s.ny;
  $("#basis").checked = s.tile_basis === "initial";
  $("#incl-outline").checked = s.include_outline;
  $("#grid-on").checked = s.grid_on; $("#grid-snap").checked = s.grid_snap;
  setLen($("#grid-mm"), s.grid_mm);
  if (S.p.rectified) {
    const o = gridOrigin();
    $("#grid-origin-txt").textContent = `Origin: ${fmtLen(o[0], false)}, ${fmtLen(o[1])}` + (s.grid_origin ? "" : " (sheet corner)");
  }
  if (S.p.calib) {
    setLen($("#cal-w"), S.p.calib.width_mm); setLen($("#cal-h"), S.p.calib.height_mm); setLen($("#cal-m"), S.p.calib.margin_mm);
  }
  $("#save-design").classList.remove("disabled");
  $("#save-design").href = P("/package");
  const ready = !!S.p.sheet;
  $("#exp-svg").classList.toggle("disabled", !ready);
  $("#exp-zip").classList.toggle("disabled", !ready);
  $("#exp-svg").href = ready ? P("/export.svg") : "#";
  $("#exp-zip").href = ready ? P("/export_tiles.zip") : "#";
  const off = S.p.cutouts.filter((c) => !c.include);
  $("#exp-warn").hidden = !off.length;
  $("#exp-warn").textContent = off.length ? `⚠ Not exported: ${off.map((c) => "#" + c.num).join(", ")} (output turned off).` : "";
}
function updateButtons() {
  $("#measure").disabled = !(S.p && S.p.rectified) || S.mode === "calibrate";
  $("#undo").disabled = !S.p;
}
async function saveSettings() {
  if (!S.p) return;
  await change("PATCH", "/settings", {
    sheet_w: getLen($("#sh-w")), sheet_h: getLen($("#sh-h")), default_buffer_mm: getLen($("#def-buf")),
    grip_d_mm: getLen($("#grip-d")), grip_auto: $("#grip-auto").checked, grip_at: $("#grip-at").value,
    bed_w: getLen($("#bed-w")), bed_h: getLen($("#bed-h")), tab_mm: getLen($("#tab")), tab_spacing_mm: getLen($("#tab-sp")),
    joint: $("#joint").value, nx: +$("#nx").value || 0, ny: +$("#ny").value || 0,
    tile_basis: $("#basis").checked ? "initial" : "current", include_outline: $("#incl-outline").checked,
  });
}
["#sh-w", "#sh-h", "#def-buf", "#grip-d", "#bed-w", "#bed-h", "#tab", "#tab-sp"].forEach((id) => $(id).addEventListener("len", saveSettings));
["#joint", "#nx", "#ny", "#incl-outline", "#grip-auto", "#grip-at", "#basis"].forEach((id) => $(id).addEventListener("change", saveSettings));
$("#show-tiles").addEventListener("change", draw);
// exporting with suppressed cutouts asks first
["#exp-svg", "#exp-zip"].forEach((id) => $(id).addEventListener("click", (e) => {
  const off = S.p ? S.p.cutouts.filter((c) => !c.include) : [];
  if (!off.length) return;
  const list = off.map((c) => `  #${c.num} ${c.name}`).join("\n");
  if (!confirm(`${off.length} cutout(s) have output turned off and will NOT be in this export:\n\n${list}\n\nExport anyway?`)) e.preventDefault();
}));

// grid
const saveGrid = (body) => S.p && change("PATCH", "/settings", body);
$("#grid-on").addEventListener("change", (e) => saveGrid({ grid_on: e.target.checked }));
$("#grid-snap").addEventListener("change", (e) => saveGrid(e.target.checked ? { grid_snap: true, grid_on: true } : { grid_snap: false }));
$("#grid-mm").addEventListener("len", () => { if (getLen($("#grid-mm")) > 0) saveGrid({ grid_mm: getLen($("#grid-mm")) }); });
$("#grid-reset").onclick = () => { const sh = S.p && S.p.sheet; if (sh) saveGrid({ grid_origin: [sh.x, sh.y], grid_on: true }); };
$("#grid-from-sel").onclick = () => {
  const it = findSel();
  if (!it) return alert("Select a cutout or a piece of text first.");
  saveGrid({ grid_origin: snapAnchor(S.sel.k, it), grid_on: true });
};
function setPickOrigin(on) {
  S.pickOrigin = on;
  $("#grid-pick").classList.toggle("active", on);
  if (on && S.measuring) setMeasuring(false);
}
$("#grid-pick").onclick = () => { if (S.p && S.p.rectified) setPickOrigin(!S.pickOrigin); };
// the clicked point, pulled onto a nearby sheet corner, outline point, item centre or measurement end (Alt = exact)
function pickOrigin(mm, e) {
  setPickOrigin(false);
  let pt = mm;
  if (!e.altKey) {
    const cands = [...(S.p.sheet ? sheetCorners(S.p.sheet) : [])];
    S.p.cutouts.forEach((c) => cands.push(snapAnchor("c", c), ...(c.final.length > 2 ? c.final : c.placed)));
    S.p.texts.forEach((t) => cands.push([t.x, t.y]));
    if (S.measure) cands.push(S.measure.a, S.measure.b);
    let best = HANDLE;
    const [bx, by] = mmToScreen(mm);
    cands.forEach((c) => { const [ax, ay] = mmToScreen(c), d = Math.hypot(ax - bx, ay - by); if (d < best) { best = d; pt = c; } });
  }
  saveGrid({ grid_origin: [...pt], grid_on: true });
}
$("#sheet-reset").onclick = () => change("PATCH", "/settings", { reset_sheet: true });
$("#arrange").addEventListener("change", (e) => {
  S.arrange = e.target.checked;
  $("#arrange-box").hidden = !S.arrange;
  draw();
});

async function refreshTiles() {
  if (!S.p || !S.p.sheet) { S.tiles = null; renderSeams(); return; }
  S.tiles = await api("GET", P("/tiles"));
  renderSeams(); updateTileStatus(); draw();
}
function updateTileStatus() {
  if (!S.tiles) return status("#tile-status", "");
  const t = S.tiles, bad = t.tiles.filter((x) => !x.fits).length;
  const big = t.tiles.reduce((a, x) => [Math.max(a[0], x.w), Math.max(a[1], x.h)], [0, 0]);
  status("#tile-status", `${t.tiles.length} piece(s): ${t.nx} × ${t.ny}. Largest ${fmtLen(big[0], false)}×${fmtLen(big[1])}.` +
    (bad ? ` ⚠ ${bad} piece(s) exceed the bed – add columns/rows.` : ""));
}

// seam nudging: each seam moves sideways and its joints slide along it
function renderSeams() {
  const box = $("#seams");
  box.innerHTML = "";
  const t = S.tiles;
  if (!t || (t.nx < 2 && t.ny < 2)) return;
  const hint = document.createElement("p");
  hint.className = "hint";
  hint.textContent = "Nudge seams and joints to keep them clear of tools (5 mm per click, Shift = 1 mm).";
  box.appendChild(hint);
  const row = (name, dir, i, seamArrows, jointArrows) => {
    const el = document.createElement("div");
    el.className = "seam";
    el.innerHTML = `<span class="name">${name}</span>
      <span class="sub">Seam</span><span class="nudge"><button data-f="seam_off" data-s="-1">${seamArrows[0]}</button><span>${fmtLen(t.seam_off[dir][i])}</span><button data-f="seam_off" data-s="1">${seamArrows[1]}</button></span>
      <span class="sub">Joints</span><span class="nudge"><button data-f="tab_shift" data-s="-1">${jointArrows[0]}</button><span>${fmtLen(t.tab_shift[dir][i])}</span><button data-f="tab_shift" data-s="1">${jointArrows[1]}</button></span>`;
    el.querySelectorAll("button").forEach((b) => (b.onclick = (e) => nudgeSeam(b.dataset.f, dir, i, +b.dataset.s * (e.shiftKey ? 1 : 5))));
    box.appendChild(el);
  };
  t.seam_off.v.forEach((_, i) => row(`Vertical seam ${i + 1}`, "v", i, "◀▶", "▲▼"));
  t.seam_off.h.forEach((_, i) => row(`Horizontal seam ${i + 1}`, "h", i, "▲▼", "◀▶"));
  const reset = document.createElement("button");
  reset.className = "link"; reset.textContent = "Reset seams";
  reset.onclick = () => change("PATCH", "/settings", { seam_off: { v: [], h: [] }, tab_shift: { v: [], h: [] } });
  box.appendChild(reset);
}
function nudgeSeam(field, dir, i, delta) {
  const v = { v: [...S.tiles[field].v], h: [...S.tiles[field].h] };
  v[dir][i] += delta;
  change("PATCH", "/settings", { [field]: v });
}

// ------------------------------------------------------------------ modes / projects / upload
function setMode(m) {
  S.mode = m;
  $$(".tabs button").forEach((b) => b.classList.toggle("active", b.dataset.mode === m));
  $$("[data-panel]").forEach((s) => (s.style.display = s.dataset.panel === m ? "" : "none"));
  S.preview = []; S.draft = []; S.strokes = []; S.seeds = []; S.shape = null; S.smart = null;
  if (m === "calibrate") { S.measure = null; setMeasuring(false); }
  if (m !== "calibrate" && S.cropping) endCrop();
  updateButtons();
  loadImage(); draw();
}
$$(".tabs button").forEach((b) => (b.onclick = () => {
  if (b.dataset.mode !== "calibrate" && !(S.p && S.p.rectified)) return alert("Calibrate the photo first.");
  setMode(b.dataset.mode);
}));

async function openDesign(f) {
  const fd = new FormData();
  fd.append("package", f);
  const r = await fetch("/api/import", { method: "POST", body: fd });
  const j = await r.json();
  if (!r.ok) return alert(j.error);
  await openProject(j.id);
  loadProjectList();
}
$("#open-design").addEventListener("change", (e) => { openDesign(e.target.files[0]); e.target.value = ""; });

async function uploadFile(f) {
  if (f && f.name.toLowerCase().endsWith(".foamproj")) return openDesign(f);
  if (!f || !f.type.startsWith("image/")) return alert("Please drop an image or a .foamproj design file.");
  const fd = new FormData();
  fd.append("photo", f);
  const r = await fetch("/api/upload", { method: "POST", body: fd });
  const j = await r.json();
  if (!r.ok) return alert(j.error);
  S.corners = []; S.sel = null; S.measure = null; S.hidden.clear();
  setLen($("#cal-w"), ""); setLen($("#cal-h"), ""); setLen($("#cal-m"), 0);
  setMode("calibrate");
  setProject(j);
  startCrop();  // offer a crop straight away; "Skip" leaves the photo as is
  loadProjectList();
}
$("#upload").addEventListener("change", (e) => { uploadFile(e.target.files[0]); e.target.value = ""; });

// drag & drop a photo anywhere on the page
let dragDepth = 0;
window.addEventListener("dragenter", (e) => { if (e.dataTransfer.types.includes("Files")) { dragDepth++; document.body.classList.add("dragging"); } });
window.addEventListener("dragleave", () => { if (--dragDepth <= 0) { dragDepth = 0; document.body.classList.remove("dragging"); } });
window.addEventListener("dragover", (e) => e.preventDefault());
window.addEventListener("drop", (e) => {
  e.preventDefault();
  dragDepth = 0; document.body.classList.remove("dragging");
  if (e.dataTransfer.files.length) uploadFile(e.dataTransfer.files[0]);
});

async function openProject(id) {
  const p = await api("GET", `/api/p/${id}`);
  S.hidden.clear();
  S.corners = p.calib ? p.calib.corners.map((c) => [...c]) : [];
  S.sel = null; S.measure = null;
  setMode(p.rectified ? "trace" : "calibrate");
  setProject(p);
}
async function loadProjectList() {
  const list = await api("GET", "/api/projects");
  const sel = $("#projects");
  sel.innerHTML = '<option value="">Open recent…</option>';
  list.forEach((p) => sel.insertAdjacentHTML("beforeend", `<option value="${p.id}"></option>`));
  [...sel.options].slice(1).forEach((o, i) => (o.textContent = `${list[i].name} (${list[i].cutouts})`));
}
$("#projects").addEventListener("change", (e) => { if (e.target.value) openProject(e.target.value); e.target.value = ""; });

// ------------------------------------------------------------------ boot
(async function boot() {
  try { if (localStorage.getItem("unit") === "in") { $("#unit").checked = true; S.unit = "in"; } } catch {}
  lenInputs().forEach((el) => (el.value = fmtLen(getLen(el))));
  setTool("wand");
  setMode("calibrate");
  updateCropButtons();
  resize();
  await loadProjectList();
  const q = new URLSearchParams(location.search);
  let last = q.get("p");
  try { last = last || localStorage.getItem("last"); } catch {}
  if (last) {
    await openProject(last).catch(() => {});
    if (q.get("mode") && S.p && S.p.rectified) setMode(q.get("mode"));
    if (q.get("arrange")) $("#arrange").click();
  }
})();
