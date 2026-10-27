"""UAS_SpaceSyntax_Weimar.3dm (teacher's detailed centre model) -> weimar_ss_{car,pedestrian}_city2.geojson.

The new model is the truth inside its own footprint: every line of it is taken unchanged (translated by SHIFT,
fitted to city2: 91 % of vertices coincide to 1 cm). Footprint = closing (CLOSE_M) of the model's own lines, per network,
purely geometric. Lines of the previous city2 inside the footprint are dropped (the user's edits and auto lines included);
outside the footprint city2 is kept. Dropped from the new model: single straight lines longer than LONG_M that touch
nothing of the old network ("artistic" diagonals) and the stray 5x5 m Brep on the pedestrian layer.
Seam: ends of kept old lines within SNAP_TOL of the new network are moved onto it (target split there).
Source of the "old" side is data/processed/ss_city2_before_uas/ (saved on first run), so reruns are idempotent.
Writes weimar_ss_*_city2.geojson, ss_city2_uas_removed_review.geojson, ss_city2_uas_dropped_review.geojson.
"""
import json
from pathlib import Path
import rhino3dm
from pyproj import Transformer
from shapely.geometry import shape, LineString, Point, mapping
from shapely.ops import transform, unary_union, substring

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "web/layers/data"
SRC = ROOT / "data/raw/teacher_ss_model/UAS_SpaceSyntax_Weimar.3dm"
OLD = ROOT / "data/processed/ss_city2_before_uas"
SHIFT = (663212.7575, 5649063.6655)
CLOSE_M, MATCH_M, LONG_M, SNAP_TOL = 200.0, 1.0, 400.0, 2.0
LAYERS = {"car": "Car", "pedestrian": "Pedestrian Paths"}
TO = Transformer.from_crs(4326, 25832, always_xy=True).transform
FR = Transformer.from_crs(25832, 4326, always_xy=True).transform

def curve_pts(g):
    if isinstance(g, rhino3dm.LineCurve): return [g.PointAtStart, g.PointAtEnd]
    if isinstance(g, rhino3dm.PolylineCurve): return [g.Point(i) for i in range(g.PointCount)]
    if isinstance(g, rhino3dm.NurbsCurve):
        d = g.Domain; return [g.PointAt(d.T0 + (d.T1 - d.T0) * k / 50) for k in range(51)]
    return None

m = rhino3dm.File3dm.Read(str(SRC))
new = {n: [] for n in LAYERS}; skipped = []
for o in m.Objects:
    ln = m.Layers.FindIndex(o.Attributes.LayerIndex).Name
    n = next((k for k, v in LAYERS.items() if v == ln), None)
    pts = curve_pts(o.Geometry)
    if n is None or pts is None: skipped.append((ln, type(o.Geometry).__name__)); continue
    new[n].append(LineString([(p.X + SHIFT[0], p.Y + SHIFT[1]) for p in pts]))
print("skipped non-curves:", skipped)

def load(n):
    return [(f["properties"], transform(TO, shape(f["geometry"]))) for f in json.load(open(OLD / f"weimar_ss_{n}_city2.geojson", encoding="utf8"))["features"]]

def split_at(lines, idx, pt, tol=0.05):
    p, g = lines[idx]; d = g.project(pt)
    if d < tol or g.length - d < tol: return
    lines[idx] = [p, substring(g, 0, d)]
    q = dict(p); q["ssId"] = p["ssId"] + "s"; lines.append([q, substring(g, d, g.length)])

feat = lambda n, g, **p: {"type": "Feature", "properties": {"network": n, **p}, "geometry": mapping(transform(FR, g))}
added, removed, dropped = [], [], []
final = {}
for n in LAYERS:
    old = load(n)
    ou = unary_union([g for p, g in old]); ob = ou.buffer(MATCH_M)
    zone = unary_union(new[n]).buffer(CLOSE_M).buffer(-CLOSE_M)
    keepnew = []
    for g in new[n]:
        if len(g.coords) == 2 and g.length > LONG_M and g.intersection(ob).length < 0.05 * g.length:
            dropped.append(feat(n, g, length=round(g.length))); continue
        keepnew.append(g)
    nb = unary_union(keepnew).buffer(MATCH_M)
    lines = []; nrem = 0
    for p, g in old:
        inzone = g.intersection(zone).length > 0.5 * g.length
        if inzone:
            nrem += 1
            if g.intersection(nb).length < 0.95 * g.length: removed.append(feat(n, g, ssId=p["ssId"]))
            continue
        # straddling line: cut away the part duplicated by the new model
        rest = g.difference(nb)
        parts = [rest] if rest.geom_type == "LineString" else list(getattr(rest, "geoms", []))
        for i, h in enumerate(x for x in parts if x.geom_type == "LineString" and x.length > 0.5):
            q = dict(p); 
            if i: q["ssId"] += f"p{i}"
            lines.append([q, h])
    ostart = len(lines)
    nl = [[{"ssId": f"{n[:3]}u{i + 1}", "network": n, "source": "teacher_uas"}, g] for i, g in enumerate(keepnew)]
    # seam: kept old ends close to the new network are moved onto it
    nu = unary_union(keepnew); moved = 0
    for L in lines[:ostart]:
        c = list(L[1].coords)
        for k in (0, -1):
            pt = Point(c[k]); d = pt.distance(nu)
            if 0.02 < d <= SNAP_TOL:
                tgt = min(range(len(nl)), key=lambda i: nl[i][1].distance(pt))
                q = nl[tgt][1].interpolate(nl[tgt][1].project(pt)); c[k] = (q.x, q.y); split_at(nl, tgt, q); moved += 1
        L[1] = LineString(c)
    final[n] = lines + nl
    print(n, "new kept", len(keepnew), "of", len(new[n]), "| old dropped in zone", nrem, "of", len(old), "| old kept", ostart, "| seam ends moved", moved)
    added += [feat(n, g, ssId=p["ssId"]) for p, g in nl]

def dump(path, feats): json.dump({"type": "FeatureCollection", "features": feats}, open(path, "w"), separators=(",", ":"))
for n in LAYERS:
    out = []
    for p, g in final[n]:
        p = dict(p); p["length"] = round(g.length, 1)
        out.append({"type": "Feature", "properties": p, "geometry": {"type": "LineString", "coordinates": [[round(x, 7), round(y, 7)] for x, y in (FR(*c) for c in g.coords)]}})
    dump(DATA / f"weimar_ss_{n}_city2.geojson", out); print(n, "written", len(out))
dump(DATA / "ss_city2_uas_removed_review.geojson", removed); dump(DATA / "ss_city2_uas_dropped_review.geojson", dropped)
