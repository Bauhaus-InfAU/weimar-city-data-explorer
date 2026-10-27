#!/usr/bin/env python3
"""
check_space_syntax_roads.py — TASK_31 part D: independent connectivity / topology check of the
weimar_ss_*.geojson files (does not import the builder). Loads GeoJSON -> EPSG:25832 -> Shapely/networkx.

  python tools/check_space_syntax_roads.py            # table for raw + light/medium/strong, both networks
Prints (and writes data/processed/space_syntax/check_report.json):
  segments, nodes, length_km, components, dangling ends (deg-1 nodes, of which on the area border),
  crossings without node (bridge-flagged pairs are declared unlinks, counted separately),
  overlaps/duplicates, segments shorter than min_seg, degree-2 nodes (not on the border), self-loops, mean/median length.
"""
import json, math, statistics, sys
from pathlib import Path
import networkx as nx
from pyproj import Transformer
from shapely.geometry import LineString, Point, box, shape
from shapely.ops import transform
from shapely.strtree import STRtree

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "web" / "layers" / "data"
PROC = ROOT / "data" / "processed" / "space_syntax"
TO = Transformer.from_crs(4326, 25832, always_xy=True).transform
MIN_SEG = {"light": 8, "medium": 15, "strong": 25, "raw": 0}
SPUR = {"light": 25, "medium": 40, "strong": 70, "raw": 0}      # TASK_32 (v1: 15/25/40)
LOOP = {"light": 80, "medium": 120, "strong": 200, "raw": 0}
BUFFER_M = 2000.0


def work_boundary():
    b = json.load(open(ROOT / "web" / "layers" / "base_border.json"))
    xs, ys = zip(*[TO(p["lng"], p["lat"]) for p in (b["pointA"], b["pointB"])])
    return box(min(xs), min(ys), max(xs), max(ys)).buffer(BUFFER_M, join_style=2).exterior


def check(path, min_seg, bound, spur=0, loop=0):
    fc = json.load(open(path, encoding="utf-8"))
    lines = [transform(TO, shape(f["geometry"])) for f in fc["features"]]
    br = [bool(f["properties"].get("bridge")) for f in fc["features"]]
    key = lambda xy: (round(xy[0], 2), round(xy[1], 2))
    G = nx.MultiGraph()
    for i, l in enumerate(lines):
        G.add_edge(key(l.coords[0]), key(l.coords[-1]), i=i)
    deg = dict(G.degree())
    border = {n for n in G.nodes if bound.distance(Point(n)) < 0.1}
    tree = STRtree(lines)
    cross = ovl = unl = 0
    for i, a in enumerate(lines):
        for j in tree.query(a):
            if j <= i:
                continue
            x = a.intersection(lines[j])
            if x.is_empty:
                continue
            if x.geom_type not in ("Point", "MultiPoint"):
                ovl += 1; continue
            for p in getattr(x, "geoms", [x]):
                ends_a = [Point(a.coords[0]), Point(a.coords[-1])]; ends_b = [Point(lines[j].coords[0]), Point(lines[j].coords[-1])]
                if any(p.distance(q) < 0.05 for q in ends_a) and any(p.distance(q) < 0.05 for q in ends_b):
                    continue
                if br[i] or br[j]: unl += 1
                else: cross += 1
    # TASK_32: free ends shorter than spur_len (not on the border) and pendant loops <= loop_max
    short_free = short_free_protected = 0
    for n, dg in deg.items():
        if dg == 1 and n not in border:
            (u, v, dat), = G.edges(n, data=True)
            other = v if u == n else u
            if deg[other] >= 3 and lines[dat["i"]].length < spur:
                # TASK_33: a protected main route is exempt from the spur rule, so it is counted separately
                if fc["features"][dat["i"]]["properties"].get("protected"):
                    short_free_protected += 1
                else:
                    short_free += 1
    H = nx.Graph()
    for u, v, dat in G.edges(data=True):
        H.add_edge(u, ("a", dat["i"])); H.add_edge(("a", dat["i"]), ("b", dat["i"])); H.add_edge(("b", dat["i"]), v)
    pend = 0
    for blk in nx.biconnected_components(H):
        sub = H.subgraph(blk)
        if sub.number_of_edges() != len(blk) or len(blk) < 3:
            continue
        orig = [x for x in blk if not isinstance(x, tuple)]
        if sum(1 for x in orig if H.degree(x) > 2) == 1 and sum(lines[x[1]].length for x in blk if isinstance(x, tuple) and x[0] == "a") <= loop and loop:
            pend += 1
    L = [l.length for l in lines]
    geo_keys = [tuple(sorted([tuple(map(lambda v: round(v, 2), c)) for c in l.coords]))[:3] + (round(l.length, 1),) for l in lines]
    return dict(
        segments=len(lines), nodes=G.number_of_nodes(), length_km=round(sum(L) / 1000, 2),
        components=nx.number_connected_components(nx.Graph(G)),
        dangling_ends=sum(1 for n, d in deg.items() if d == 1),
        dangling_on_border=sum(1 for n, d in deg.items() if d == 1 and n in border),
        crossings_without_node=cross, declared_unlinks=unl, overlaps=ovl,
        duplicate_geometries=len(geo_keys) - len(set(geo_keys)),
        shorter_than_min_seg=sum(1 for v in L if v < min_seg - 1e-6), min_seg=min_seg, shortest_m=round(min(L), 1),
        degree2_nodes_not_border=sum(1 for n, d in deg.items() if d == 2 and n not in border),
        self_loops=sum(1 for u, v in G.edges() if u == v),
        mean_len=round(statistics.mean(L), 1), median_len=round(statistics.median(L), 1),
        free_ends_shorter_than_spur=short_free, free_ends_short_but_protected=short_free_protected, spur_len=spur, pendant_loops_le_loop_max=pend, loop_max=loop,
        straight_edges=sum(1 for f in fc["features"] if f["properties"].get("straight")),
        vertices=sum(len(l.coords) for l in lines),
        inAOI=sum(1 for f in fc["features"] if f["properties"].get("inAOI")) or None,
    )


def main():
    bound = work_boundary()
    rep = {}
    if len(sys.argv) > 1:          # python check_space_syntax_roads.py <dir with weimar_ss_* files> [tag]
        d = Path(sys.argv[1]); out = {}
        for net in ("car", "pedestrian"):
            for tag in ("light", "medium", "strong"):
                p = d / f"weimar_ss_{net}_{tag}.geojson"
                if p.exists():
                    out[f"{net}/{tag}"] = check(p, MIN_SEG[tag], bound, SPUR[tag], LOOP[tag]); print(net, tag, out[f"{net}/{tag}"])
        json.dump(out, open(PROC / (sys.argv[2] if len(sys.argv) > 2 else "check_report_dir.json"), "w"), indent=1)
        return
    for net in ("car", "pedestrian"):
        for tag in ("raw", "light", "medium", "strong"):
            p = PROC / f"raw_{net}.geojson" if tag == "raw" else DATA / f"weimar_ss_{net}_{tag}.geojson"
            if p.exists():
                rep[f"{net}/{tag}"] = check(p, MIN_SEG[tag], bound, SPUR[tag], LOOP[tag])
                print(net, tag, rep[f"{net}/{tag}"])
    json.dump(rep, open(PROC / "check_report.json", "w"), indent=1)


if __name__ == "__main__":
    main()
