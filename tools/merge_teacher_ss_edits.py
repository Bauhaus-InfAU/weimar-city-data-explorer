"""Merge the user's edits of the teacher's Space Syntax model into weimar_ss_{car,pedestrian}_city2.geojson.

Inputs : data/raw/teacher_ss_model/{car,pedastrian}.geojson  (her edited cut of the centre, WGS84)
         data/processed/ss_city2_teacher_original/ (unedited teacher import, written on first run)
Rules  : * segments with a matching ssId and unchanged geometry stay as in the teacher's model;
         * her new segments (ssId missing/unknown) are added;
         * teacher segments absent from her file are removed only if they lie inside her area
           (closing of her features by CLOSE_M) - elsewhere her cut simply did not include them;
         * only her NEW segments are snapped: ends -> nearest node / line of the same layer within
           SNAP_TOL (target line is split there), pedestrian ends -> car likewise (car is split);
           crossings of a new segment with existing lines get a shared node (lines split).
Writes : web/layers/data/weimar_ss_{car,pedestrian}_city2.geojson, ss_city2_removed_review.geojson,
         ss_city2_added_review.geojson. Rerun-safe (always starts from the saved original).
"""
import json, shutil
from pathlib import Path
from pyproj import Transformer
from shapely.geometry import shape, LineString, Point, mapping
from shapely.ops import transform, unary_union, substring, nearest_points
from shapely.strtree import STRtree

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "web/layers/data"
RAW = ROOT / "data/raw/teacher_ss_model"
ORIG = ROOT / "data/processed/ss_city2_teacher_original"
TO = Transformer.from_crs(4326, 25832, always_xy=True).transform
FR = Transformer.from_crs(25832, 4326, always_xy=True).transform
SNAP_TOL = 2.0
CLOSE_M = 150.0
NAMES = {"car": "car", "pedestrian": "pedastrian"}

ORIG.mkdir(parents=True, exist_ok=True)
for n in NAMES:
    o = ORIG / f"weimar_ss_{n}_city2.geojson"
    if not o.exists():
        shutil.copy(DATA / f"weimar_ss_{n}_city2.geojson", o)

def load(p):
    return [(f["properties"], transform(TO, shape(f["geometry"]))) for f in json.load(open(p, encoding="utf8"))["features"]]

old = {n: load(ORIG / f"weimar_ss_{n}_city2.geojson") for n in NAMES}
her = {n: load(RAW / f"{f}.geojson") for n, f in NAMES.items()}

result = {}; removed = []; added = []
for n in NAMES:
    oldids = {p["ssId"]: g for p, g in old[n]}
    hid = {p.get("ssId") for p, g in her[n]}
    area = unary_union([g for p, g in her[n]]).buffer(CLOSE_M).buffer(-CLOSE_M)
    keep, gone = [], []
    for p, g in old[n]:
        if p["ssId"] in hid or not area.contains(g): keep.append([dict(p), g])
        else: gone.append((p, g))
    new = [(p, g) for p, g in her[n] if p.get("ssId") not in oldids]
    result[n] = (keep, new)
    removed += [{"type": "Feature", "properties": {"network": n, "ssId": p["ssId"]}, "geometry": mapping(transform(FR, g))} for p, g in gone]
    print(n, "teacher", len(old[n]), "kept", len(keep), "removed", len(gone), "new", len(new))

# ---- snap only her new segments, split target lines
def split_at(lines, idx, pt, tol=0.05):
    p, g = lines[idx]
    d = g.project(pt)
    if d < tol or g.length - d < tol: return
    lines[idx] = [p, substring(g, 0, d)]
    q = dict(p); q["ssId"] = p["ssId"] + "s"
    lines.append([q, substring(g, d, g.length)])

def snap_end(pt, own, other):
    """-> (point, (layer_list, idx) or None)"""
    best = None
    for lst in (own, other):
        for i, (p, g) in enumerate(lst):
            for e in (Point(g.coords[0]), Point(g.coords[-1])):
                d = pt.distance(e)
                if d <= SNAP_TOL and (best is None or d < best[0]): best = (d, e, None)
        if best: return best[1], None
    for lst in (own, other):
        near = [(g.distance(pt), i) for i, (p, g) in enumerate(lst)]
        near = [x for x in near if x[0] <= SNAP_TOL]
        if near:
            d, i = min(near)
            q = lst[i][1].interpolate(lst[i][1].project(pt)); return q, (lst, i)
    return pt, None

for n, other_n in (("car", "pedestrian"), ("pedestrian", "car")):
    pass
lines = {n: result[n][0] for n in NAMES}
for n in ("car", "pedestrian"):
    other = lines["pedestrian" if n == "car" else "car"]
    if n == "car": other_order = other
    for p, g in result[n][1]:
        c = list(g.coords)
        for k in (0, -1):
            q, tgt = snap_end(Point(c[k]), lines[n], other)
            c[k] = (q.x, q.y)
            if tgt: split_at(tgt[0], tgt[1], q)
        g2 = LineString(c)
        # crossings with existing lines -> shared node
        for lst in (lines[n], other):
            for i in range(len(lst)):
                h = lst[i][1]
                if h.crosses(g2):
                    x = h.intersection(g2)
                    if x.geom_type == "Point":
                        split_at(lst, i, x)
                        gp = g2.project(x); 
                        c2 = list(g2.coords); 
                        if all(Point(v).distance(x) > 0.05 for v in c2):
                            g2 = LineString(list(substring(g2, 0, gp).coords) + list(substring(g2, gp, g2.length).coords)[0:])
        q = dict(p); q["ssId"] = q.get("ssId") or f"{n[:3]}new{len(lines[n]) + 1}"; q["network"] = n; q["source"] = "user_edit"; q["length"] = round(g2.length, 1)
        lines[n].append([q, g2]); added.append({"type": "Feature", "properties": {"network": n, "ssId": q["ssId"]}, "geometry": mapping(transform(FR, g2))})

def dump(path, feats): json.dump({"type": "FeatureCollection", "features": feats}, open(path, "w"), separators=(",", ":"))
for n in NAMES:
    out = []
    for p, g in lines[n]:
        p = dict(p); p["length"] = round(g.length, 1)
        out.append({"type": "Feature", "properties": p, "geometry": {"type": "LineString", "coordinates": [[round(x, 7), round(y, 7)] for x, y in (FR(*c) for c in g.coords)]}})
    dump(DATA / f"weimar_ss_{n}_city2.geojson", out); print(n, "written", len(out))
dump(DATA / "ss_city2_removed_review.geojson", removed); dump(DATA / "ss_city2_added_review.geojson", added)
