#!/usr/bin/env python3
"""
ss_protected.py — TASK_33 part B: the protected list of main routes for the Space Syntax networks.

A protected segment may not be deleted (spur / lollipop / detour / redundancy / island rules) and may not be
straightened past `straight_tol`; it may still be noded, snapped and Douglas-Peucker simplified.

Three static groups, all read from the source ATKIS layers (never modified):
  A classified streets   AX_Strassenachse with a Name and Widmung 1301/1303/1305/1306
                         (Bundesautobahn / Bundes- / Landes- / Kreisstraße)
  B the 30 longest named streets in the working area (by summed length in EPSG:25832)
  C the historic-centre list named in the task; every name below was verified to exist in the data
    (Theaterplatz exists ONLY as an AX_Platz polygon, not as a street axis — it is protected via the plaza
    perimeter, group D in the builder)
Two dynamic groups are decided inside the builder, where the graph is known:
  D plaza perimeters (every edge that belongs to a plaza outline)
  E pedestrian paths (WegPfadSteig / OSM footway / pedestrian) that connect two different streets
  F the top importance_top share of edges by edge betweenness (part F of the task)

`protected_street_geoms()` -> {street name: unary_union(LineString) in EPSG:25832}, used both by the builder
(to mark edges) and by the regression test (>= 90 % of each protected street's length must survive).
"""
import json
from collections import defaultdict
from functools import lru_cache
from pathlib import Path

from pyproj import Transformer
from shapely.geometry import box, shape
from shapely.ops import transform, unary_union

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "web" / "layers" / "data"
TO = Transformer.from_crs(4326, 25832, always_xy=True).transform

CLASSIFIED_WIDMUNG = {"1301", "1303", "1305", "1306"}   # Autobahn / Bundes- / Landes- / Kreisstraße
LONGEST_N = 30
BUFFER_M = 2000.0

# group C — historic centre, from the task text; all verified present in the data
PROTECTED_NAMES = [
    "Markt", "Frauenplan", "Schillerstraße", "Marktstraße", "Frauentorstraße",
    "Theaterplatz", "Goetheplatz", "Jorge-Semprún-Platz", "Bahnhofstraße",
    # "Ilm-Park-Wege" from the task is not a name in the data; the Ilm park paths are covered by
    # Corona-Schröter-Weg / Karolinenpromenade / Pappelallee (named WegPfadSteig) which are added below
    "Corona-Schröter-Weg", "Karolinenpromenade", "Pappelallee",
]


def working_area():
    b = json.load(open(ROOT / "web" / "layers" / "base_border.json"))
    xs, ys = zip(*[TO(p["lng"], p["lat"]) for p in (b["pointA"], b["pointB"])])
    return box(min(xs), min(ys), max(xs), max(ys)).buffer(BUFFER_M, join_style=2)


@lru_cache(maxsize=1)
def protected_street_geoms():
    work = working_area()
    per_name = defaultdict(list)
    classified, named_len = set(), defaultdict(float)
    for layer in ("weimar_atkis_strassenachse", "weimar_atkis_wegpfadsteig"):
        fc = json.load(open(DATA / f"{layer}.geojson", encoding="utf-8"))
        for f in fc["features"]:
            p = f["properties"]
            name = p.get("Name") or p.get("name") or ""
            if not name:
                continue
            g = transform(TO, shape(f["geometry"])).intersection(work)
            if g.is_empty:
                continue
            per_name[name].append(g)
            named_len[name] += g.length
            w = "" if p.get("Widmung") in (None, "") else str(p["Widmung"]).split(".")[0]
            if w in CLASSIFIED_WIDMUNG:
                classified.add(name)
    longest = {n for n, _ in sorted(named_len.items(), key=lambda t: -t[1])[:LONGEST_N]}
    keep = classified | longest | {n for n in PROTECTED_NAMES if n in per_name}
    return {n: unary_union(per_name[n]) for n in sorted(keep)}


@lru_cache(maxsize=1)
def protected_groups():
    """{name: [groups]} so the report can say why a street is protected."""
    work = working_area()
    classified, named_len, present = set(), defaultdict(float), set()
    for layer in ("weimar_atkis_strassenachse", "weimar_atkis_wegpfadsteig"):
        fc = json.load(open(DATA / f"{layer}.geojson", encoding="utf-8"))
        for f in fc["features"]:
            p = f["properties"]
            name = p.get("Name") or p.get("name") or ""
            if not name:
                continue
            g = transform(TO, shape(f["geometry"])).intersection(work)
            if g.is_empty:
                continue
            present.add(name); named_len[name] += g.length
            w = "" if p.get("Widmung") in (None, "") else str(p["Widmung"]).split(".")[0]
            if w in CLASSIFIED_WIDMUNG:
                classified.add(name)
    longest = {n for n, _ in sorted(named_len.items(), key=lambda t: -t[1])[:LONGEST_N]}
    out = defaultdict(list)
    for n in classified:
        out[n].append("A classified")
    for n in longest:
        out[n].append("B longest30")
    for n in PROTECTED_NAMES:
        if n in present:
            out[n].append("C centre")
    return dict(out)


def plaza_polygons(min_area=400.0):
    work = working_area()
    fc = json.load(open(DATA / "weimar_atkis_platz.geojson", encoding="utf-8"))
    out = []
    for f in fc["features"]:
        g = transform(TO, shape(f["geometry"])).intersection(work)
        for part in getattr(g, "geoms", [g]):
            if part.geom_type == "Polygon" and part.area >= min_area:
                out.append((part, f["properties"].get("Name") or f["properties"].get("name") or ""))
    return out


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8")
    gs, grp = protected_street_geoms(), protected_groups()
    print(f"{len(gs)} protected streets, total {sum(g.length for g in gs.values())/1000:.1f} km")
    for n, g in sorted(gs.items(), key=lambda t: -t[1].length)[:40]:
        print(f"  {g.length:8.0f} m  {n:34s} {','.join(grp.get(n, []))}")
    print(f"{len(plaza_polygons())} plazas >= 400 m2")
