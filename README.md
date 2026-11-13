# Foam Insert Maker

A small Flask app for turning a top-down photo of tools into a laser-cut SVG for
toolbox foam inserts.

1. **Calibrate.** Upload a photo (or drag it onto the page), crop it if you like, click the 4 corners of something rectangular whose size you
   know (the drawer, the foam sheet, or a sheet of paper) and type its width and height. The
   app removes the perspective and sets the scale.
2. **Trace.** Outline each tool with the magic wand, the threshold slider, auto-detect
   (click the background and it finds every object), or by drawing points yourself. Click the
   wand several times to grow a selection, and use *Tug* to push or pull an outline into shape.
   Each cutout goes into the table on the right with a number, its own colour and a finger grip.
3. **Layout.** Rearrange and rotate the cutouts, add engraved text, pull in the sheet corners, and set
   your laser's work area. If the sheet is bigger than the bed, it is split into pieces with jigsaw or
   dovetail joints. You can nudge each seam and its joints to keep them clear of the tools.
4. **Export.** Download the SVG files. They use real millimetre units, so LightBurn imports them
   at the correct size without any scaling.

## Setup (Windows)

Python 3.11 or newer is required.

```powershell
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python run.py
```

Then open http://127.0.0.1:5000.

Linux or macOS: use `source .venv/bin/activate` instead.

## LightBurn config

The LightBurn device profile and Ruida 644XS controller settings for the laser this was built for are in
[`lightburn/`](lightburn/). See [`lightburn/README.md`](lightburn/README.md) before loading them.

## Tips

- **Photo:** shoot as close to straight down as you can, in even light, with the tools on a
  background that contrasts with them. Dark foam and shiny chrome is the hardest combination;
  a sheet of white paper or card under the tools helps a lot.
- **Calibration rectangle:** measurements are only exact on the plane of the rectangle. Tall tools
  look slightly larger from above, so add some buffer.
  Use *Extra margin* if tools extend beyond the rectangle (for example when a sheet of paper is the reference).
- **Units:** mm by default. Any length box accepts a unit, so `4in`, `3 1/2in`, `3-1/2"`, `10cm` and `25mm`
  all work, and so do sums and differences like `15in + 3mm - 1/8in`. With no spaces, `3-1/2` is a
  mixed number (three and a half). Toggle to inches at the top right and bare numbers are read as inches.
- **Buffer:** each cutout has a buffer (0 mm by default; the laser kerf already adds a little
  clearance). Make it positive if a tool needs to drop in more easily. A negative buffer makes a tighter grip. New cutouts use the *default buffer* from
  the Layout tab. Use Edit (✎) to change a cutout's buffer, name, colour, smoothing, or to leave it
  out of the export.
- **Finger grips:** off by default. Add one from a cutout's Edit dialog (✎), or tick *Add a finger grip to
  new cutouts* on the Layout tab. *Place new grips at* chooses where they go (default 20 mm diameter):
  - **Edge:** on the edge nearest the cutout's centre. The yellow handle slides along the edge.
  - **Centre:** in the middle of the tool, so it cuts a finger slot across both sides. It only shows
    where it's wider than the tool, so use a diameter bigger than the tool's width. The orange handle
    moves freely.
  The Edit dialog has *+ Edge* and *+ Centre* buttons to add either kind. Alt+click a handle to remove it.
- **Adjust cutout position** (Layout tab): hides the photo. Drag cutouts to move them and drag the round
  handle to rotate (Shift = 15° steps). Click once on a straight edge to square it up. Arrow keys nudge
  (Shift = 10 mm), `[` and `]` rotate. Text can be dragged on the Layout tab at any time.
- **Shiny tools:** use **✨ Smart select**. Drag a box around one tool and it separates the tool from
  the background by colour and texture, so glare and reflections don't break the outline. If it misses
  part of the tool, click on that part; if it grabbed background, Alt+click there. **🎨 Colour key**
  works best on a coloured background (green or blue felt or card): click the background and every
  object is found by colour, not brightness. Click more spots to add shades of the background.
  The best photo fix is light from below: lay the tools on an LED tracing pad and you get a clean
  silhouette for *Auto*.
- **✎ Freehand:** drag to trace an outline. Let go whenever you like (to pan or zoom), then drag again:
  the line carries on from where you stopped. Letting go near the start, double-clicking, or pressing
  Enter closes it, and Backspace removes the last stroke.
- **🖌 Brush:** paint over a preview or a cutout to add to it. *Erase* (or holding Alt) paints it away,
  and painting on empty space starts a new outline. Set the size with the slider.
- **Tug:** press right on an outline (a yellow dot shows where it will grab) and drag. Near a corner it
  grabs that exact corner. Only the stretch highlighted in yellow moves, measured along the outline, so
  the far side of a thin tool stays put. *Reach* 0 moves just the one point.
- **〰 Smooth / straighten:** drag along part of an outline (preview or cutout) to pick a stretch; the ends
  snap to corners. *Smooth* evens out bumps (set how much with the slider, and watch the cyan preview);
  *Straight line* replaces the stretch with a straight edge. Enter applies, Esc cancels. Handy after
  Smart select to clean up a straight side or a ragged curve.
- **Circle and rectangle** (Trace tab): exact shapes. Type a radius (or width and height) and click to
  place one centred on the click, or drag: a circle from its centre, a rectangle from a corner as a
  square (hold Shift for any rectangle). Drag the placed shape to move it (it snaps if the grid snap is on).
  Typing a new size resizes it. Enter adds it.
  Rectangles keep square corners when buffered.
- **Text:** *Add text*, or double-click existing text (on the image or in the table) to edit it. Enter
  saves and Shift+Enter starts a new line. There are buttons for left, centre and right alignment, bold
  and italic (Ctrl+B / Ctrl+I), a Sans, Serif or Mono font, and line spacing.
- **Grid** (Layout tab): *Show grid* and *Snap to grid*, with any spacing (`1/4in` works). Snapping puts
  a cutout's or text's centre on grid points while you drag, and also snaps sheet corners and
  measurements. Alt+drag moves freely, and with snap on the arrow keys jump one grid step. Set the
  origin with *Origin at selection* (the selected item's centre) or *Origin at point…* (the next click,
  which pulls onto a nearby corner, outline point or measurement end).
- **Hide and output toggles** (cutouts table): the eye hides a cutout while you work, but it is still
  cut. The scissors turn a cutout's output off: it goes translucent grey and is left out of the SVG and
  ZIP. Export warns you and lists what's left out.
- **Sheet size:** drag the blue corner handles. *Calculate pieces based on initial crop* keeps the
  seams from the original sheet; unchecked, it re-plans for the smaller sheet, and if one piece fits
  the bed, there are no joints.
- **Measure** (📏 or M): click two points. Starting a new measurement clears the old one.
- **Undo:** Ctrl+Z (or the button) undoes cutout, text and layout changes. Cropping and calibrating
  change the image, so they can't be undone and they clear the undo history.
- **Keys:** Enter adds the preview, Esc clears it, Delete removes the selected cutout or text,
  Backspace removes the last point while drawing. Scroll to zoom. Right-drag, middle-drag or Space+drag pans.
  Double-click a name in the table to rename it.
- **Exports:**
  - *Export SVG*: the whole sheet, with the outline and every cutout.
  - *Export pieces*: a ZIP with one SVG per bed-sized piece, each placed at its own origin, plus
    `overview_all_tiles.svg` showing how the pieces fit together. Each piece's seam edge (joints
    included) is pushed out by half the *Laser kerf* setting (default 0.6 mm), so the joints fit tight and
    a cutout that spans a seam keeps its full length once the beam has burned the kerf away. The sheet
    edge and cutouts are not changed. If the joints are too tight to push together, lower the kerf; if
    they're loose, raise it. The on-screen preview shows the seams without kerf.
  - Cut lines are black and text is blue, so LightBurn puts them on separate layers (set the blue one to engrave/fill).

## Layout

```
run.py              start the dev server
app/
  __init__.py       Flask app factory
  routes.py         HTTP API and project storage (JSON per project)
  vision.py         perspective correction and outline detection (OpenCV)
  geometry.py       placement, buffers, finger grips, text outlines, sheet splitting / joints, SVG output (Shapely, matplotlib fonts)
  templates/        index.html
  static/           app.js, style.css
output/             uploads, rectified photos, exports (git-ignored)
```

## Tracing with Claude Code

The project includes a Claude Code skill, `.claude/skills/trace-tools`. In a fresh Claude Code session in
this folder, ask something like *"trace the tools in my latest .foamproj: hex key, 8/10 open-end, 13 mm
combination wrench"*. Claude opens a copy, outlines each tool (using smart select, or row-by-row edge
tracing for thin tools), checks the outlines at close zoom, and saves `<name> - traced.foamproj` next
to the original. The helper script can also be used on its own:
`.venv/Scripts/python .claude/skills/trace-tools/scripts/foamjob.py -h`.

## Saving and opening designs

*💾 Save design* downloads a single `.foamproj` file. It holds the uploaded photo (full and cropped),
the perspective-corrected image, and everything you did: calibration, cutouts with their buffers,
grips and positions, text, sheet size, bed and joint settings, and seam nudges. Use *Open design*
(or drop the file onto the page) to reopen it later, on this PC or another one, exactly where you left off.
Opening a design makes a new copy, so the original file never changes. A `.foamproj` is an ordinary
zip file if you want to look inside.

Each project is also saved as you work in `output/<project-id>/`. Use *Open recent* in the header to reopen one,
or go to `http://127.0.0.1:5000/?p=<project-id>`.

## Homography Lab (experimental)

A separate test bench for drawers too big or crowded for one photo: it stitches several photos into one
top-down, millimetre-scaled image.

```powershell
.venv\Scripts\python homography_lab\server.py            # photos from homeography\
.venv\Scripts\python homography_lab\server.py D:\photos  # or any folder
```

Then open http://127.0.0.1:5001. Label a rectangle of known size in one photo (the *frame*), use
*Auto-align all*, then place point pairs by hand for any photo it can't match. Finally, *Build mosaic*.
*Export full quality* downloads the mosaic at the sharpest photo's resolution. It comes as a PNG or JPG
with its DPI set, so printing at 100% gives true size, or as a PDF tiled over Letter/A4 sheets at 1:1 scale.
Labels are saved to `labels.json` in the photo folder.

Ideas for later go in [ToDo.md](ToDo.md).
