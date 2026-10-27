#!/usr/bin/env python3
"""TASK_31/32: before/after pictures for two places -> output/task32_<place>_<preset>.png
left = v1 (TASK_31) network, right = v2 (TASK_32) network + segments removed by the rules in red."""
import json, sys
from pathlib import Path
import matplotlib; matplotlib.use("Agg")
import matplotlib.pyplot as plt
from pyproj import Transformer
from shapely.geometry import shape, box, Point
from shapely.ops import transform
ROOT = Path(__file__).resolve().parents[1]
TO = Transformer.from_crs(4326, 25832, always_xy=True).transform
PLACES = {"centre": ("Historic centre (Markt / Frauenplan)", 11.3265, 50.9795, 420),
          "west": ("Courtyard passages, Weimar-West", 11.3045, 50.9895, 420)}
def load(p): return [transform(TO, shape(f["geometry"])) for f in json.load(open(p, encoding="utf-8"))["features"]]
def main(preset="medium"):
    bld = load(ROOT / "web/layers/data/weimar_building_footprints.geojson")
    for pl, (title, lng, lat, half) in PLACES.items():
        x, y = TO(lng, lat); win = box(x - half, y - half, x + half, y + half)
        fig, axes = plt.subplots(2, 2, figsize=(16, 16))
        for r, net in enumerate(("car", "pedestrian")):
            v1 = load(ROOT / f"data/processed/space_syntax_v1_task31/weimar_ss_{net}_{preset}.geojson")
            v2 = load(ROOT / f"web/layers/data/weimar_ss_{net}_{preset}.geojson")
            rem = load(ROOT / f"web/layers/data/weimar_ss_{net}_{preset}_removed.geojson")
            for c, (lines, lab) in enumerate(((v1, "v1 (TASK_31)"), (v2, "v2 (TASK_32)"))):
                ax = axes[r][c]
                for b in bld:
                    if b.intersects(win):
                        xs, ys = b.exterior.xy if b.geom_type == "Polygon" else b.geoms[0].exterior.xy
                        ax.fill(xs, ys, color="#e4e4e4", lw=0)
                nodes = {}
                for l in lines:
                    if l.intersects(win):
                        xs, ys = l.xy; ax.plot(xs, ys, color="#7b2cbf" if net == "car" else "#0a9396", lw=1.6)
                        for p in (l.coords[0], l.coords[-1]): nodes[(round(p[0], 1), round(p[1], 1))] = p
                if c == 1:
                    for l in rem:
                        if l.intersects(win):
                            xs, ys = l.xy; ax.plot(xs, ys, color="#e5171f", lw=2.4, zorder=6)
                nx_ = [p for p in nodes.values() if win.contains(Point(p))]
                ax.scatter([p[0] for p in nx_], [p[1] for p in nx_], s=12, color="#222", zorder=5)
                ax.set_xlim(x - half, x + half); ax.set_ylim(y - half, y + half); ax.set_aspect("equal"); ax.set_xticks([]); ax.set_yticks([])
                ax.set_title(f"{net} {preset}: {lab}" + (" — removed segments in red" if c == 1 else "") + f" — {len(nx_)} nodes in view")
        fig.suptitle(f"TASK 32 — {title}  (black = nodes, grey = footprints)")
        fig.tight_layout(); out = ROOT / "output" / f"task32_{pl}_{preset}.png"; fig.savefig(out, dpi=70); print(out)
if __name__ == "__main__":
    main(*sys.argv[1:])
