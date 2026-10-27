#!/usr/bin/env python3
"""
report_space_syntax33.py — TASK_33: the v2 -> v3 comparison table and the network composition.

Reads the built files plus the archived TASK_32 output (data/processed/space_syntax_v2_task32) and prints
markdown-ready rows: segments, nodes, length, components, free ends, straight edges, vertices, protected,
inside-building segments — and, per network/preset, what the segments are made of (srcClass) and what each
rule removed.

Run:  python tools/report_space_syntax33.py   -> output/task33_report.json + a printed table
"""
import json, sys
from collections import defaultdict
from pathlib import Path

import networkx as nx
from pyproj import Transformer
from shapely.geometry import shape
from shapely.ops import transform

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "web" / "layers" / "data"
V2 = ROOT / "data" / "processed" / "space_syntax_v2_task32"
TO = Transformer.from_crs(4326, 25832, always_xy=True).transform
PRESETS = ("light", "medium", "strong", "thin")


def stats(path):
    if not Path(path).exists():
        return None
    fc = json.load(open(path, encoding="utf-8"))
    feats = [(transform(TO, shape(f["geometry"])), f["properties"]) for f in fc["features"]]
    k = lambda xy: (round(xy[0], 2), round(xy[1], 2))
    G = nx.MultiGraph()
    for g, _ in feats:
        G.add_edge(k(g.coords[0]), k(g.coords[-1]))
    comp = nx.number_connected_components(nx.Graph(G)) if G.number_of_edges() else 0
    cls = defaultdict(lambda: [0, 0.0])
    for g, p in feats:
        c = cls[p.get("srcClass", "?")]
        c[0] += 1; c[1] += g.length
    return dict(segments=len(feats), nodes=G.number_of_nodes(), length_km=round(sum(g.length for g, _ in feats) / 1000, 2),
                components=comp, free_ends=sum(1 for n in G if G.degree(n) == 1),
                straight=sum(1 for _g, p in feats if p.get("straight")),
                vertices=sum(len(g.coords) for g, _ in feats),
                protected=sum(1 for _g, p in feats if p.get("protected")),
                passages=sum(1 for _g, p in feats if p.get("passage")),
                srcClass={kk: dict(segments=v[0], km=round(v[1] / 1000, 2))
                          for kk, v in sorted(cls.items(), key=lambda t: -t[1][1])})


def removed_by_rule(net, preset):
    p = DATA / f"weimar_ss_{net}_{preset}_removed.geojson"
    if not p.exists():
        return {}
    out = defaultdict(lambda: [0, 0.0])
    for f in json.load(open(p, encoding="utf-8"))["features"]:
        g = transform(TO, shape(f["geometry"]))
        r = out[f["properties"]["rule"]]
        r[0] += 1; r[1] += g.length
    return {k: dict(segments=v[0], km=round(v[1] / 1000, 2)) for k, v in sorted(out.items())}


def main():
    rep = {}
    print("| network / preset | segments | nodes | length km | comp | free ends | straight | vertices | protected | passages |")
    print("|---|---|---|---|---|---|---|---|---|---|")
    for net in ("car", "pedestrian"):
        v2p = "strong" if net == "car" else "medium"
        v2 = stats(V2 / f"weimar_ss_{net}_{v2p}.geojson")
        if v2:
            rep[f"{net}/v2_{v2p}"] = v2
            print(f"| {net} v2 ({v2p}, TASK_32) | {v2['segments']} | {v2['nodes']} | {v2['length_km']} | {v2['components']} "
                  f"| {v2['free_ends']} | {v2['straight']} | {v2['vertices']} | – | – |")
        for preset in PRESETS:
            s = stats(DATA / f"weimar_ss_{net}_{preset}.geojson")
            if not s:
                continue
            s["removed_by_rule"] = removed_by_rule(net, preset)
            rep[f"{net}/v3_{preset}"] = s
            print(f"| {net} v3 {preset} | {s['segments']} | {s['nodes']} | {s['length_km']} | {s['components']} "
                  f"| {s['free_ends']} | {s['straight']} | {s['vertices']} | {s['protected']} | {s['passages']} |")
    json.dump(rep, open(ROOT / "output" / "task33_report.json", "w", encoding="utf-8"), indent=1, ensure_ascii=False)
    print()
    for key in ("car/v3_strong", "pedestrian/v3_medium", "pedestrian/v3_thin"):
        if key in rep:
            print(f"== {key} composition")
            for c, v in list(rep[key]["srcClass"].items())[:10]:
                print(f"   {v['km']:7.2f} km {v['segments']:5d}  {c}")
            print("   removed:", rep[key]["removed_by_rule"])
    print("->", ROOT / "output" / "task33_report.json")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
