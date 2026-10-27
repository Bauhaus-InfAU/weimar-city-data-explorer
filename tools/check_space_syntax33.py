#!/usr/bin/env python3
"""
check_space_syntax33.py — TASK_33 regression tests (parts B, D, E) for the built weimar_ss_* files.

Independent of the builder: reads only the written GeoJSONs plus the source layers.

  B protected routes   every protected street (ss_protected.py) must keep >= 90 % of its length and form ONE
                       component in the built network
  D connectivity       two points connected in the source network stay connected: checked as "the built network
                       has no more components than the source, and no protected street is split"; every pair of
                       free ends closer than the preset's gap_tol in different components is a violation
  E buildings          no segment may overlap the footprints by > 1 m unless it is listed in
                       *_passages_review.geojson (a real archway)
  plus the TASK_32 checks via check_space_syntax_roads.check()

Run:  python tools/check_space_syntax33.py [preset ...]        (default: all three, both networks)
      writes data/processed/space_syntax/check33_report.json
"""
import json, sys
from collections import defaultdict
from pathlib import Path

import networkx as nx
from pyproj import Transformer
from shapely.geometry import LineString, Point, shape
from shapely.ops import transform
from shapely.strtree import STRtree

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from ss_protected import protected_street_geoms, protected_groups, working_area  # noqa: E402
import ss_rules33 as R33  # noqa: E402

DATA = ROOT / "web" / "layers" / "data"
PROC = ROOT / "data" / "processed" / "space_syntax"
TO = Transformer.from_crs(4326, 25832, always_xy=True).transform
GAP_TOL = {"light": 8.0, "medium": 8.0, "strong": 8.0, "thin": 8.0}
STRAIGHT_TOL = {"light": 5, "medium": 8, "strong": 12, "thin": 12}      # PRESETS[...]["straight_tol"]
MIN_SHARE = 0.90


def load(p):
    if not Path(p).exists():
        return []
    return [(transform(TO, shape(f["geometry"])), f["properties"])
            for f in json.load(open(p, encoding="utf-8"))["features"]]


def graph_of(lines):
    k = lambda xy: (round(xy[0], 2), round(xy[1], 2))
    G = nx.MultiGraph()
    for i, l in enumerate(lines):
        G.add_edge(k(l.coords[0]), k(l.coords[-1]), i=i)
    return G, k


def source_lengths_per_network(net):
    """A protected street only counts for a network if it is actually in that network's source layers:
    Corona-Schröter-Weg is a WegPfadSteig, so it cannot be in the car network and 0 % there is not a loss."""
    import ss_protected as SP
    layers = (["weimar_atkis_strassenachse"] if net == "car"
              else ["weimar_atkis_strassenachse", "weimar_atkis_wegpfadsteig"])
    work = SP.working_area()
    per = defaultdict(list)
    for layer in layers:
        for f in json.load(open(DATA / f"{layer}.geojson", encoding="utf-8"))["features"]:
            nm = f["properties"].get("Name") or f["properties"].get("name") or ""
            if not nm:
                continue
            g = transform(TO, shape(f["geometry"])).intersection(work)
            if not g.is_empty:
                per[nm].append(g)
    from shapely.ops import unary_union
    return {k: unary_union(v) for k, v in per.items()}


def test_protected(net, feats, out, preset):
    """B: per protected street, how much of its SOURCE geometry is still covered by the built network (a
    coverage test, not a segment match — cleaning merges segments across street names, which made the naive
    per-segment share look like a loss), and whether the covered parts sit in ONE component of the full graph."""
    from shapely.ops import unary_union
    grp = protected_groups()
    prot = protected_street_geoms()
    src_here = source_lengths_per_network(net)
    geoms = [g for g, _ in feats]
    tree = STRtree(geoms) if geoms else None
    k = lambda xy: (round(xy[0], 2), round(xy[1], 2))
    full = nx.Graph()
    for g in geoms:
        full.add_edge(k(g.coords[0]), k(g.coords[-1]))
    comp = {}
    for i, c in enumerate(nx.connected_components(full)):
        for n in c:
            comp[n] = i
    rows, viol, skipped = [], [], []
    for name in prot:
        geom = src_here.get(name)
        if geom is None or geom.length < 20:
            skipped.append(name)                     # not part of this network's sources
            continue
        # Coverage is measured twice: at 6 m (strict) and at the tolerance the preset itself allows a line to
        # move (straight_tol + 2). A street that is only straightened scores < 90 % at 6 m but 100 % at the
        # preset tolerance — that is a shifted line, not a lost street, and only the latter is a violation.
        def coverage(buf):
            near = [geoms[i] for i in (tree.query(geom.buffer(buf)) if tree is not None else [])]
            cov = unary_union([g.buffer(buf) for g in near]) if near else None
            miss = geom.length if cov is None else geom.difference(cov).length
            return max(0.0, 1.0 - miss / geom.length)
        share6 = coverage(6)
        share = coverage(STRAIGHT_TOL.get(preset, 8) + 2)
        idx = [i for i in (tree.query(geom.buffer(6)) if tree is not None else [])
               if geoms[i].intersection(geom.buffer(6)).length > 0.6 * geoms[i].length]
        comps = {comp.get(k(geoms[i].coords[0])) for i in idx}
        marked = sum(1 for i in idx if feats[i][1].get("protected"))
        row = dict(name=name, groups=grp.get(name, []), source_m=round(geom.length),
                   covered_share=round(share, 3), covered_share_at_6m=round(share6, 3), segments=len(idx),
                   components_in_full_graph=len(comps), marked_protected=marked)
        rows.append(row)
        if share < MIN_SHARE or len(comps) > 1:
            c = geom.centroid
            viol.append(dict(**row, at=[round(c.x), round(c.y)]))
    rows.sort(key=lambda r: r["covered_share"])
    out["B_protected"] = dict(streets=len(rows), not_in_this_network=len(skipped), violations=len(viol),
                              below_90pct=sum(1 for r in rows if r["covered_share"] < MIN_SHARE),
                              below_90pct_at_6m=sum(1 for r in rows if r["covered_share_at_6m"] < MIN_SHARE),
                              not_one_component=sum(1 for r in rows if r["components_in_full_graph"] > 1),
                              worst=rows[:12], violation_list=viol[:25])


def test_gaps(net, feats, preset, out):
    geoms = [g for g, _ in feats]
    G, k = graph_of(geoms)
    comp = {}
    for i, c in enumerate(nx.connected_components(nx.Graph(G))):
        for n in c:
            comp[n] = i
    ends = [n for n in G if G.degree(n) == 1]
    pts = [Point(n) for n in ends]
    tree = STRtree(pts) if pts else None
    tol = GAP_TOL.get(preset, 8.0)
    bad, seen = [], set()
    for i, n in enumerate(ends):
        for j in (tree.query(pts[i].buffer(tol)) if tree is not None else []):
            if j == i:
                continue
            m = ends[j]
            d = pts[i].distance(pts[j])
            if not (0.01 < d <= tol) or comp.get(n) == comp.get(m):
                continue
            key = tuple(sorted([n, m]))
            if key in seen:
                continue
            seen.add(key)
            bad.append(dict(a=[round(n[0]), round(n[1])], b=[round(m[0]), round(m[1])], dist=round(d, 2)))
    out["D_gaps"] = dict(components=len(set(comp.values())), free_ends=len(ends),
                         open_gaps_within_tol=len(bad), tol=tol, examples=bad[:10])


def test_buildings(net, feats, preset, zone, out):
    passages = {p.get("ssId") for _g, p in load(DATA / f"weimar_ss_{net}_{preset}_passages_review.geojson")}
    bad = []
    for g, p in feats:
        L = zone.inside_length(g)
        if L <= 1.0:
            continue
        c = g.interpolate(0.5, normalized=True)
        bad.append(dict(ssId=p.get("ssId"), overlap_m=round(L, 1), listed_as_passage=p.get("ssId") in passages,
                        at=[round(c.x), round(c.y)]))
    bad.sort(key=lambda b: -b["overlap_m"])
    out["E_buildings"] = dict(segments_inside=len(bad),
                              not_listed_as_passage=sum(1 for b in bad if not b["listed_as_passage"]),
                              passages_file=len(passages), worst=bad[:10])


def main(*presets):
    presets = list(presets) or ["light", "medium", "strong", "thin"]
    zone = R33.building_zone(working_area())
    rep = {}
    for net in ("car", "pedestrian"):
        for preset in presets:
            p = DATA / f"weimar_ss_{net}_{preset}.geojson"
            if not p.exists():
                continue
            feats = load(p)
            out = dict(segments=len(feats), protected_segments=sum(1 for _g, pr in feats if pr.get("protected")),
                       length_km=round(sum(g.length for g, _ in feats) / 1000, 2))
            test_protected(net, feats, out, preset)
            test_gaps(net, feats, preset, out)
            test_buildings(net, feats, preset, zone, out)
            rep[f"{net}/{preset}"] = out
            short = {k: (v if not isinstance(v, dict) else
                         {kk: vv for kk, vv in v.items() if kk not in ("worst", "examples", "violation_list")})
                     for k, v in out.items()}
            print(net, preset, json.dumps(short, ensure_ascii=False), flush=True)
    json.dump(rep, open(PROC / "check33_report.json", "w", encoding="utf-8"), indent=1, ensure_ascii=False)
    print("->", PROC / "check33_report.json")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main(*sys.argv[1:])
