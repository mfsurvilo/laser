# LightBurn config — Ruida 644XS

Settings for the laser these foam inserts are cut on (Ruida 644XS controller, 350 × 200 mm bed).

| File | What it is | How to load |
|---|---|---|
| `Ruida644XS.lbdev` | LightBurn device profile (bed size, origin, connection type, camera) | LightBurn → **Devices** → **Import** |
| `Ruida644XS-machine-settings.lbset` | Ruida controller settings backup (108 values: speeds, accelerations, axis config, etc.), saved 2026-09-23 | **Edit → Machine Settings → Load from file**, then **Write** |

**Careful with the `.lbset`:** writing it overwrites the controller's own settings (steps/mm, axis limits,
acceleration). Only load it onto the same model of machine, and back up your own first
(Machine Settings → **Save to file**). If your machine is different, use these values as a reference only.
