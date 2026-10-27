#!/usr/bin/env python3
"""
diagnose_space_syntax.py — TASK_33 part A: why the TASK_32 pedestrian network is bad.

Read-only. Compares the source network (data/processed/space_syntax_v1_task31/raw_pedestrian.geojson — the
TASK_31/32 source, snapped + noded, nothing removed) with a built preset and answers, with numbers:

  1 plaza diagonals    segments that come from the TASK_31 "Plaza -> star of entry lines" step
                       (srcClass AX_Platz:star): how many, how many run inside a plaza, how many cross each
                       other away from their endpoints, and which real lines the star replaced
  2 lost main routes   per protected street (ss_protected.py): length in the source vs in the built network,
                       and which removal rule took the missing pieces
  3 lines in buildings segments overlapping weimar_building_footprints by > 1 m, split by cause
                       (already in the source / created by straightening / by Douglas-Peucker)
  4 new gaps           free-end pairs within 8 m that sit in different components

Run:  python tools/diagnose_space_syntax.py [preset] [dir]
      default: medium, data/processed/space_syntax_v1_task31 (the TASK_32 v2 files)
      writes output/task33_diagnosis.json
"""
import json, sys
from collections import defaultdict
from pathlib import Path

import networkx as nx
from pyproj import Transformer
from shapely.geometry import LineString, Point, shape
from shapely.ops import transform, unary_union
from shapely.strtree import STRtree

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from ss_protected import protected_street_geoms, protected_groups, plaza_polygons, working_area  # noqa: E402
import ss_rules33 as R33  # noqa: E402

DATA = ROOT / "web" / "layers" / "data"
V1 = ROOT / "data" / "processed" / "space_syntax_v1_task31"
TO = Transformer.from_crs(4326, 25832, always_xy=True).transform
OVERLAP_MIN = 1.0


def load(p):
    return [(transform(TO, shape(f["geometry"])), f["properties"])
            for f in json.load(open(p, encoding="utf-8"))["features"]]


def comp_map(lines):
    k = lambda xy: (round(xy[0], 2), round(xy[1], 2))
    G = nx.Graph()
    for l in lines:
        G.add_edge(k(l.coords[0]), k(l.coords[-1]))
    cm = {}
    for i, c in enumerate(nx.connected_components(G)):
        for n in c:
            cm[n] = i
    return cm, k, G


def diag_plazas(raw, v2, pzs, out):
    star = [g for g, p in v2 if p.get("srcClass") == "AX_Platz:star"]
    pzu = unary_union([p for p, _ in pzs])
    inside = [g for g in star if g.intersection(pzu).length > 0.5 * g.length]
    tree = STRtree(star) if star else None
    cross = 0
    for i, a in enumerate(star):
        for j in (tree.query(a) if tree is not None else []):
            if j <= i:
                continue
            x = a.intersection(star[j])
            if x.is_empty or x.geom_type not in ("Point", "MultiPoint"):
                continue
            for q in getattr(x, "geoms", [x]):
                ea = [Point(a.coords[0]), Point(a.coords[-1])]
                eb = [Point(star[j].coords[0]), Point(star[j].coords[-1])]
                if not (any(q.distance(z) < 0.5 for z in ea) and any(q.distance(z) < 0.5 for z in eb)):
                    cross += 1
    # which real source lines lay fully inside a plaza (those the star step deleted outright)
    killed = []
    for layer in ("weimar_atkis_strassenachse", "weimar_atkis_wegpfadsteig", "weimar_atkis_fahrwegachse"):
        for f in json.load(open(DATA / f"{layer}.geojson", encoding="utf-8"))["features"]:
            g = transform(TO, shape(f["geometry"]))
            if g.length < 20 or not g.intersects(pzu):
                continue
            if g.intersection(pzu).length > 0.95 * g.length:
                c = g.centroid
                killed.append(dict(layer=layer, name=f["properties"].get("Name") or "",
                                   length=round(g.length), at=[round(c.x), round(c.y)]))
    killed.sort(key=lambda r: -r["length"])
    out["1_plaza"] = dict(star_segments=len(star), star_inside_plaza=len(inside),
                          star_crossings_away_from_ends=cross,
                          star_km=round(sum(g.length for g in star) / 1000, 2),
                          real_lines_fully_inside_a_plaza=len(killed), examples=killed[:10])


def diag_protected(v2, removed, out):
    prot, grp = protected_street_geoms(), protected_groups()
    vg = [g for g, _ in v2]
    vtree = STRtree(vg) if vg else None
    rg = [g for g, _ in removed]
    rtree = STRtree(rg) if rg else None
    rows = []
    for name, geom in prot.items():
        buf = geom.buffer(6)
        kept = 0.0
        for i in (vtree.query(buf) if vtree is not None else []):
            g = vg[i]
            inter = g.intersection(buf)
            if inter.length > 0.6 * g.length:
                kept += g.length
        rules = defaultdict(float)
        for i in (rtree.query(buf) if rtree is not None else []):
            g = rg[i]
            if g.intersection(buf).length > 0.6 * g.length:
                rules[removed[i][1]["rule"]] += g.length
        src = geom.length
        rows.append(dict(name=name, groups=grp.get(name, []), source_m=round(src), built_m=round(kept),
                         share=round(kept / src, 3) if src else 0.0,
                         removed_by={k: round(v) for k, v in sorted(rules.items(), key=lambda t: -t[1])}))
    rows.sort(key=lambda r: r["share"])
    out["2_protected"] = dict(streets=len(rows), below_90pct=sum(1 for r in rows if r["share"] < 0.9),
                              worst=rows[:20])


def diag_buildings(raw, v2, zone, out):
    rawg = [g for g, _ in raw]
    tree = STRtree(rawg) if rawg else None
    hits, by_cause = [], defaultdict(int)
    for g, p in v2:
        L = zone.inside_length(g)
        if L <= OVERLAP_MIN:
            continue
        src_ov = 0.0
        for i in (tree.query(g.buffer(10)) if tree is not None else []):
            sgm = rawg[i]
            if sgm.intersection(g.buffer(10)).length > 0.6 * sgm.length:
                src_ov = max(src_ov, zone.inside_length(sgm))
        if src_ov > OVERLAP_MIN:
            cause = "already in source"
        elif p.get("straight"):
            cause = "created by straightening"
        elif p.get("simplified"):
            cause = "created by Douglas-Peucker / merge"
        else:
            cause = "other"
        by_cause[cause] += 1
        c = g.interpolate(0.5, normalized=True)
        hits.append(dict(ssId=p.get("ssId"), overlap_m=round(L, 1), cause=cause, length=p.get("length"),
                         at=[round(c.x), round(c.y)]))
    hits.sort(key=lambda h: -h["overlap_m"])
    out["3_buildings"] = dict(segments=len(hits), by_cause=dict(by_cause),
                              total_overlap_m=round(sum(h["overlap_m"] for h in hits)), worst=hits[:15])


def diag_gaps(raw, v2, out):
    craw, k, _ = comp_map([g for g, _ in raw])
    cv2, _, G = comp_map([g for g, _ in v2])
    ends = [n for n in G if G.degree(n) == 1]
    pts = [Point(n) for n in ends]
    tree = STRtree(pts) if pts else None
    seen, uniq = set(), []
    for i, n in enumerate(ends):
        for j in (tree.query(pts[i].buffer(8.0)) if tree is not None else []):
            if j == i:
                continue
            m = ends[j]
            d = pts[i].distance(pts[j])
            if not (0.01 < d <= 8.0) or cv2.get(m) == cv2.get(n):
                continue
            key = tuple(sorted([n, m]))
            if key in seen:
                continue
            seen.add(key)
            uniq.append(dict(a=[round(n[0]), round(n[1])], b=[round(m[0]), round(m[1])], dist=round(d, 2),
                             source_same_component=(craw.get(n) is not None and craw.get(n) == craw.get(m))))
    out["4_gaps"] = dict(components=len(set(cv2.values())), source_components=len(set(craw.values())),
                         free_ends=len(ends),
                         free_end_pairs_within_8m_in_different_components=len(uniq), examples=uniq[:15])


def main(preset="medium", d=str(V1)):
    net = "pedestrian"
    d = Path(d)
    raw = load(V1 / f"raw_{net}.geojson")
    v2 = load(d / f"weimar_ss_{net}_{preset}.geojson")
    removed = load(d / f"weimar_ss_{net}_{preset}_removed.geojson")
    out = dict(preset=preset, dir=str(d), network=net, source_segments=len(raw), built_segments=len(v2),
               removed_segments=len(removed))
    diag_plazas(raw, v2, plaza_polygons(), out)
    print("plazas done", flush=True)
    diag_protected(v2, removed, out)
    print("protected done", flush=True)
    diag_buildings(raw, v2, R33.building_zone(working_area()), out)
    print("buildings done", flush=True)
    diag_gaps(raw, v2, out)
    p = ROOT / "output" / "task33_diagnosis.json"
    json.dump(out, open(p, "w", encoding="utf-8"), indent=1, ensure_ascii=False)
    short = {k: ({kk: vv for kk, vv in v.items() if kk not in ("worst", "examples")} if isinstance(v, dict) else v)
             for k, v in out.items()}
    print(json.dumps(short, indent=1, ensure_ascii=False))
    print("->", p)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main(*sys.argv[1:])
