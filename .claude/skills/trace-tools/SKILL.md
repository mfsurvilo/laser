---
name: trace-tools
description: Trace the tools in a Foam Insert Maker design (.foamproj) photo into cutouts and save a new .foamproj. Use when the user asks to trace, outline, or add cutouts for the tools in a design file or photo, e.g. "trace the tools in X.foamproj" or "add cutouts for the hex key and wrenches".
---

# Trace tools into cutouts

Goal: an accurate cutout per tool, in as few steps as possible. Work on an imported **copy**; never
overwrite the user's file. Accuracy matters more than speed, but look at as few images as needed:
each one costs tokens.

## Helper

All steps use one script. Run it with the project's venv from the project root:

```
PY=.venv/Scripts/python          # Windows venv; .venv/bin/python elsewhere
J=.claude/skills/trace-tools/scripts/foamjob.py
$PY $J -h                        # full command list
```

Lengths are mm in the rectified photo (origin top-left, y down). Outlines are saved by name under
`output/<pid>/trace/<name>.json`, and images are written to the same folder.

## Steps

1. **Find the file.** If the user says "most recent", search their Downloads for the newest
   `*.foamproj`. Then run `$PY $J open FILE` and note the PID, and look at `overview.jpg`: it has a
   10 mm grid with labels every 50 mm. Identify each tool and its rough bounding box. If the user
   named the tools, match those names; otherwise name them by what they are ("13 mm combination wrench").
2. **Outline each tool** with the right method (see below). Save one outline per tool.
3. **Verify** every outline with `zoom PID X0 Y0 X1 Y1 --show NAME --tag NAME_end`. Check the
   ends and any thin parts (jaws, tips, bends), not the straight middle. Two zooms per tool is usually
   enough. Fix what's wrong with `union` (add a polygon) or `union --erase` (cut one away). These are
   the equivalent of the app's brush.
4. **Replace the cutouts** only if the user asked. Run `clear PID`, then
   `add PID NAME --label "Readable name"` for each tool. Don't add finger grips unless asked
   (`--grip edge|center`). Keep the design's default buffer.
5. **Final check:** run `check PID` and look at `final.jpg` once. Sanity-check the sizes against real
   tools: a 13 mm combination wrench is about 170–180 mm long, an 8/10 open-end about 125 mm, and a
   6 mm hex key's long arm about 130–140 mm.
6. **Save** with `save PID "<Downloads>/<original name> - traced.foamproj"`, next to the original.
   Tell the user the path, list each cutout with its size and the method used, and give anything
   they should check. The imported copy also appears under *Open recent* in the app.

## Which method for which tool

- **Chrome wrenches, sockets, pliers (shiny, broad):**
  `smart PID NAME --rect X0 Y0 X1 Y1` with a box about 2–4 mm outside the tool. It follows the metal
  and ignores shadows. This is usually right first time.
  - **Open-end jaws:** the thin angled jaw faces are dimmer than the flat face, so the tips often get
    cut off. Zoom on each open end. If the tips are missing, `union` a polygon covering each jaw,
    reading the corners off the zoom's 1 mm ticks.
  - **Don't add `--bg` marks near a shadow.** GrabCut then tends to swallow the whole shadow. Fix
    small misses with `union` instead.
- **Thin tools whose faces look like the background** (hex keys, screwdriver shafts, dark or olive
  metal on dark or green backgrounds): smart select and colour key leak into shadows or drop faces.
  Trace these instead:
  1. `scan PID --rows Y0:Y1:6 --x X0:X1 --bg x,y` gives, for each row, where the pixels change class.
     Classes are B (bright), D (shadow), o (other, not background) and . (background).
  2. The tool is the B/o run. A D run right next to it is its **shadow**, not the tool. Check that
     the width stays constant: a hex key is its across-flats size, about 6 mm for a 6 mm key.
  3. Use `--cols` for parts that run horizontally (a hex key's short arm).
  4. Build the outline point by point with `poly PID NAME "x,y x,y ..."`: down one edge, round the
     end, and back up the other edge. Round a bend with 4–6 points.
- **Many tools on a strongly coloured background (green or blue felt):** try
  `colorkey PID --points "x,y;x,y"` first, clicking 2–3 background spots. It finds everything at once
  (saved as ck1, ck2, ... from left to right). If an outline's bbox spans several tools, or its area is
  far bigger than one tool, it leaked: add more background clicks or lower `--tol`, or switch to
  smart select. Keep the ones that zoom-check clean and redo the rest with the methods above.
- **Round or rectangular items** (sockets, bits cases): smart select, or `poly` with exact points.

## Notes

- Only the outer outline is cut, so the hole in a ring spanner gets cut out with the rest. That's
  expected.
- The design's buffer (usually +1 mm) is added to the traced outline, so a trace that's tight to the
  metal is right. Don't pad it.
- If the photo isn't calibrated (no rectified image), stop and tell the user to calibrate first.
