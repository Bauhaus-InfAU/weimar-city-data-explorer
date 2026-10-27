#!/usr/bin/env python3
"""
build_space_syntax_roads.py — TASK_34: pedestrian Space Syntax network, built against a FIXED car reference.

Supersedes TASK_33 (v3, per-network protected lists / building-avoidance / redundancy thinning — all of that
is reverted, see DATA_LOG.md "TASK 34" for why). Car is **not rebuilt here**: the platform's
`web/layers/data/weimar_ss_car_*.geojson` files are the TASK_32 (v2) output, copied byte-identical from
`data/processed/space_syntax_v2_task32/` — verified by checksum in the TASK_34 report. This script only
produces the pedestrian network, and it treats car as read-only reference geometry: car gets no new nodes, no
edits, nothing written back to it.

Why pedestrian must be rebuilt: TASK_32's pedestrian preset had `PEDESTRIAN_ADD_STREETS = True`, i.e. it
re-derived the car streets a second time, independently, inside the pedestrian pipeline — the same physical
street ended up as two slightly different lines (different simplification, different node positions), so car
and pedestrian never lined up and "the same street" had two names, two geometries, two `ssId`s. Fixed here:

  * `PEDESTRIAN_ADD_STREETS = False` — car streets are never duplicated into pedestrian; pedestrian only carries
    WegPfadSteig, Fahrwegachse and OSM footway/path/pedestrian/steps/cycleway (part 2 of the task).
  * every pedestrian line end within `CAR_SNAP_TOL` (8 m) of the car reference is snapped exactly onto it (onto
    a car vertex if one is within `CAR_NODE_TOL` (3 m), else onto the nearest point of the car line); every
    at-grade crossing of a pedestrian line with a car line gets a vertex in the PEDESTRIAN line at that exact
    coordinate (car stays untouched) — bridge/tunnel pairs are excluded (unlinks), as before.
  * the two TASK_33-diagnosed v2 bugs in plaza handling are fixed (not a preference, an actual bug): plazas no
    longer get an invented "star of diagonals" between entries, and a real source line lying entirely inside a
    plaza is no longer silently deleted.
  * connectivity / the island rule are evaluated on car (fixed) UNION pedestrian (built) — a pedestrian branch
    that only reaches the network through car is not an island.

Everything else (parameter set, graph-cleaning rules: spur pruning, lollipop/turnaround removal, duplicate and
near-parallel merging, node clustering, short-edge contraction, Douglas-Peucker + straightening) is the TASK_32
pipeline, byte-for-byte the same code — nothing from TASK_33 (protected lists, building-avoidance, redundancy /
importance thinning) is included.

Run:  python tools/build_space_syntax_roads.py [light|medium|strong ...]     (pedestrian only; default: all three)
      python tools/check_space_syntax_roads.py                              (topology, unchanged from TASK_32)
      python tools/check_ss_car_reference.py                                (TASK_34 part 3: car/pedestrian match)
"""
import json, math, sys, statistics
from collections import defaultdict, Counter
from pathlib import Path

import networkx as nx
import shapely
from pyproj import Transformer
from shapely.geometry import LineString, MultiLineString, Point, Polygon, box, shape, mapping
from shapely.ops import transform, unary_union, polygonize
from shapely.strtree import STRtree

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from geojson_clean import clean_features  # noqa: E402

DATA = ROOT / "web" / "layers" / "data"
PROC = ROOT / "data" / "processed" / "space_syntax"
BORDER = ROOT / "web" / "layers" / "base_border.json"
CAR_REFERENCE = DATA / "weimar_ss_car_strong.geojson"   # v2 (TASK_32), unchanged — read-only reference

BUFFER_M = 2000.0                 # working area = Base border + this buffer (edge effect)
PEDESTRIAN_ADD_STREETS = False     # TASK_34: car streets are never duplicated into pedestrian (was True in v2)
PLAZA_MIN_AREA = 400.0
SNAP_TOL = 1.5                    # pedestrian-to-pedestrian self-snap (unchanged from TASK_31/32)
CAR_SNAP_TOL = 8.0                # TASK_34 part 2: pedestrian end -> car line, within this
CAR_NODE_TOL = 3.0                # ... snapped to a car NODE (vertex) if one is within this, else to the line
PLAZA_CONNECT_TOL = 3.0           # a plaza-perimeter piece's end is tied to the nearest real line within this

PRESETS = {
    # unchanged from TASK_32
    "light":  dict(spur_len=25, min_seg=8,  simplify_tol=3.0, node_cluster=8,  straight_tol=5,  dup_tol=3,
                   loop_max=80,  stem_max=40,  parallel_tol=10),
    "medium": dict(spur_len=40, min_seg=15, simplify_tol=4.0, node_cluster=12, straight_tol=8,  dup_tol=6,
                   loop_max=120, stem_max=60,  parallel_tol=15),
    "strong": dict(spur_len=70, min_seg=25, simplify_tol=6.0, node_cluster=20, straight_tol=12, dup_tol=12,
                   loop_max=200, stem_max=100, parallel_tol=20),
}
UNNAMED_WIDMUNG = ("9997", "9999", "")     # Strassenachse without a name and with one of these Widmung codes = "unnamed"
RING_MAX_LEN, RING_COMPACT = 170.0, 0.85     # roundabout: circumference <= m, 4*pi*A/P^2 >= value (car only, unused here)

# TASK_34 part 2: OSM highway classes counted as pedestrian paths. Deliberately NOT residential/service/tertiary/
# living_street (present in the raw OSM extract via foot=yes tagging on ordinary streets — those are roads, ATKIS
# already carries them in "car") and NOT track (heavily overlaps weimar_atkis_fahrwegachse, which is already a
# pedestrian source below; adding OSM track on top would double most field/forest tracks).
OSM_PEDESTRIAN_HIGHWAY = {"footway", "path", "pedestrian", "steps", "cycleway"}

TO_UTM = Transformer.from_crs(4326, 25832, always_xy=True).transform
FROM_UTM = Transformer.from_crs(25832, 4326, always_xy=True).transform

NETWORKS = {
    "car": [("weimar_atkis_strassenachse", "AX_Strassenachse", "Widmung")],
    "pedestrian": [("weimar_atkis_wegpfadsteig", "AX_WegPfadSteig", "Art"),
                   ("weimar_atkis_fahrwegachse", "AX_Fahrwegachse", "Funktion")],
}


# ----------------------------------------------------------------------------- input
def working_area():
    b = json.load(open(BORDER))
    xs, ys = zip(*[TO_UTM(p["lng"], p["lat"]) for p in (b["pointA"], b["pointB"])])
    aoi = box(min(xs), min(ys), max(xs), max(ys))
    return aoi, aoi.buffer(BUFFER_M, join_style=2)


def load_osm_pedestrian(work):
    """TASK_34 part 2: OSM footway/path/pedestrian/steps/cycleway as an extra pedestrian source."""
    out = []
    fc = json.load(open(DATA / "weimar_pedestrian_osm.geojson", encoding="utf-8"))
    for f in fc["features"]:
        g = f.get("geometry")
        if not g or g["type"] != "LineString":
            continue
        p = f["properties"]
        hw = p.get("highway") or ""
        if hw not in OSM_PEDESTRIAN_HIGHWAY:
            continue
        cls = f"osm:highway={hw}"
        geom = transform(TO_UTM, shape(g)).intersection(work)
        for part in getattr(geom, "geoms", [geom]):
            # OSM ways occasionally degenerate to a couple of near-coincident nodes (mapping artefacts, e.g. a
            # POI way): a 0.01 m floor (fine for ATKIS) lets several-mm slivers through, so OSM gets a real floor.
            if part.geom_type == "LineString" and part.length > 0.5:
                out.append(dict(coords=list(part.coords),
                                parts={(cls, "weimar_pedestrian_osm", False): part.length}, bridge=False))
    return out


def load_lines(network, work):
    out = []
    srcs = list(NETWORKS[network])
    if network == "pedestrian" and PEDESTRIAN_ADD_STREETS:
        srcs += NETWORKS["car"]
    for layer, obj, key in srcs:
        fc = json.load(open(DATA / f"{layer}.geojson", encoding="utf-8"))
        for f in fc["features"]:
            g = f.get("geometry")
            if not g or g["type"] != "LineString":
                continue
            p = f["properties"]
            v = p.get(key)
            cls = f"{obj}:{key}={'' if v in (None, '') else str(v).split('.')[0]}"
            # unnamed street axis: no name and Widmung 9997/9999/empty (TASK_32 rule 1: unnamed dead ends go faster)
            unnamed = (obj == "AX_Strassenachse" and not (p.get("Name") or p.get("name"))
                       and ("" if p.get("Widmung") in (None, "") else str(p.get("Widmung")).split(".")[0]) in UNNAMED_WIDMUNG)
            geom = transform(TO_UTM, shape(g))
            geom = geom.intersection(work)
            for part in getattr(geom, "geoms", [geom]):
                if part.geom_type == "LineString" and part.length > 0.01:
                    out.append(dict(coords=list(part.coords), parts={(cls, layer, unnamed): part.length},
                                    bridge=str(p.get("hatDirektUntenExistiert")) == "1"))
    if network == "pedestrian":
        out += load_osm_pedestrian(work)
    return out


def plaza_perimeter_lines(lines, work, stats):
    """TASK_34 part 2: fixes the two v2 bugs diagnosed in TASK_33 part A.

      bug 1 (invented diagonals) — v2's `plaza_star_lines` cut every line at the plaza boundary and replaced the
        inside with a straight-line star from every entry to one hub point. Not done any more: real lines are
        NEVER cut or removed at a plaza.
      bug 2 (real lines silently deleted) — a line lying entirely inside a plaza had an empty
        `difference(plaza)`, so v2's step produced nothing for it: the line vanished, outside the rule log.
        Not possible any more, for the same reason as bug 1: lines are never touched.

    What IS added: the part of a plaza's perimeter that no real line already runs along (so a plaza still reads
    as a closed shape you can walk around), only for plazas with >= 2 real entries. Each added piece is tied to
    the nearest real line within PLAZA_CONNECT_TOL m at each end, otherwise it would float as its own isolated
    component (found and fixed as part of getting this rework to actually connect, not a preference)."""
    fc = json.load(open(DATA / "weimar_atkis_platz.geojson", encoding="utf-8"))
    plazas = [transform(TO_UTM, shape(f["geometry"])).intersection(work) for f in fc["features"]]
    plazas = [p for p in plazas if p.geom_type == "Polygon" and p.area >= PLAZA_MIN_AREA]
    stats["plazas_total"] = len(plazas)
    geoms = [LineString(l["coords"]) for l in lines]
    tree = STRtree(geoms)
    add = []
    served = covered = 0
    for pz in plazas:
        ring = pz.exterior.simplify(2.0)
        near_idx = tree.query(pz.buffer(SNAP_TOL))
        near = [geoms[i] for i in near_idx]
        entries = [g for g in near if g.intersects(pz.buffer(SNAP_TOL))]
        if len(entries) < 2:
            continue
        served += 1
        covers = unary_union([g.buffer(SNAP_TOL) for g in near]) if near else None
        missing = ring.difference(covers) if covers is not None else ring
        parts = [p for p in getattr(missing, "geoms", [missing]) if p.geom_type == "LineString" and p.length > 5.0]
        if not parts:
            covered += 1
            continue
        key = ("AX_Platz:perimeter", "weimar_atkis_platz", False)
        for part in parts:
            c = list(part.coords)
            for at in (0, -1):
                q = Point(c[at])
                cands = [g for g in near if g.distance(q) <= 3 * SNAP_TOL]
                if not cands:
                    continue
                tgt = min(cands, key=lambda g: g.distance(q))
                proj = tgt.interpolate(tgt.project(q))
                if proj.distance(q) > 0.01:
                    c = ([(proj.x, proj.y)] + c) if at == 0 else (c + [(proj.x, proj.y)])
            if LineString(c).length > 5.0:
                add.append(dict(coords=c, parts={key: LineString(c).length}, bridge=False))
    stats["plazas_with_entries"] = served
    stats["plazas_perimeter_already_covered"] = covered
    stats["plaza_perimeter_lines_added"] = len(add)
    stats["plaza_perimeter_km_added"] = round(sum(LineString(a["coords"]).length for a in add) / 1000, 2)
    return lines + add


# ----------------------------------------------------------------------------- TASK_34 part 2: car reference
def load_car_reference():
    """The fixed car network (v2 / TASK_32, byte-identical to data/processed/space_syntax_v2_task32/), as
    read-only geometry. Never rebuilt, never modified — every function below only reads from this."""
    fc = json.load(open(CAR_REFERENCE, encoding="utf-8"))
    lines, bridge = [], []
    for f in fc["features"]:
        g = transform(TO_UTM, shape(f["geometry"]))
        lines.append(g)
        bridge.append(bool(f["properties"].get("bridge")))
    nodeset = set()
    for g in lines:
        nodeset.add((round(g.coords[0][0], 3), round(g.coords[0][1], 3)))
        nodeset.add((round(g.coords[-1][0], 3), round(g.coords[-1][1], 3)))
    nodes = [Point(*n) for n in nodeset]
    return dict(lines=lines, bridge=bridge, nodes=nodes,
               node_tree=STRtree(nodes), line_tree=STRtree(lines))


def snap_ends_to_car(lines, car, stats, touch):
    """TASK_34 part 2: every pedestrian line end within CAR_SNAP_TOL of car is moved exactly onto it — onto a
    car vertex if one is within CAR_NODE_TOL, else onto the nearest point of the nearest car line. Car itself is
    never touched (no vertex is added to it). Every coordinate used is added to `touch` (a set), so the caller
    can protect it (Graph.build's car_node_keys) through the rest of the cleaning pipeline — a line-projection
    snap target is NOT one of car's own vertices, so it would otherwise drift away again during cleaning."""
    snapped_node = snapped_line = 0
    for l in lines:
        for k in (0, -1):
            p = Point(l["coords"][k])
            node_cand = [i for i in car["node_tree"].query(p.buffer(CAR_NODE_TOL))
                        if car["nodes"][i].distance(p) <= CAR_NODE_TOL]
            if node_cand:
                best = min(node_cand, key=lambda i: car["nodes"][i].distance(p))
                xy = (car["nodes"][best].x, car["nodes"][best].y)
                if xy != l["coords"][k]:
                    l["coords"][k] = xy; snapped_node += 1
                touch.add((round(xy[0], 3), round(xy[1], 3)))
                continue
            line_cand = [i for i in car["line_tree"].query(p.buffer(CAR_SNAP_TOL))
                        if car["lines"][i].distance(p) <= CAR_SNAP_TOL]
            if not line_cand:
                continue
            best = min(line_cand, key=lambda i: car["lines"][i].distance(p))
            proj = car["lines"][best].interpolate(car["lines"][best].project(p))
            xy = (proj.x, proj.y)
            if xy != l["coords"][k]:
                l["coords"][k] = xy; snapped_line += 1
            touch.add((round(xy[0], 3), round(xy[1], 3)))
    stats["ped_ends_snapped_to_car_node"] = snapped_node
    stats["ped_ends_snapped_to_car_line"] = snapped_line
    return lines


def node_at_car_crossings(lines, car, stats, touch):
    """TASK_34 part 2: every at-grade crossing of a pedestrian line with a car line gets a vertex inserted into
    the PEDESTRIAN line (car stays untouched). Bridge/tunnel pairs (either side flagged) are excluded — those
    are declared union unlinks, written/reported by check_ss_car_reference.py, not connected here."""
    inserted = skipped_unlink = 0
    for l in lines:
        ln = LineString(l["coords"])
        cand = [i for i in car["line_tree"].query(ln)]
        new_pts = []
        for i in cand:
            cg, cb = car["lines"][i], car["bridge"][i]
            x = ln.intersection(cg)
            if x.is_empty:
                continue
            pts = [x] if x.geom_type == "Point" else (list(x.geoms) if x.geom_type == "MultiPoint" else [])
            for q in pts:
                # already an end of the pedestrian line (from snap_ends_to_car): not a new interior node
                if q.distance(Point(ln.coords[0])) < 0.02 or q.distance(Point(ln.coords[-1])) < 0.02:
                    continue
                if l["bridge"] or cb:
                    skipped_unlink += 1; continue
                new_pts.append((q.x, q.y))
        if not new_pts:
            continue
        c = list(l["coords"])
        for xy in new_pts:
            if xy in c:
                continue
            s = ln.project(Point(xy))
            acc, idx = 0.0, 0
            for idx in range(len(c) - 1):
                seg = math.dist(c[idx], c[idx + 1])
                if acc + seg >= s - 1e-9:
                    break
                acc += seg
            c.insert(idx + 1, xy); inserted += 1
            touch.add((round(xy[0], 3), round(xy[1], 3)))
        l["coords"] = c
    stats["ped_vertices_inserted_at_car_crossings"] = inserted
    stats["ped_car_bridge_crossings_skipped"] = skipped_unlink
    return lines


# ----------------------------------------------------------------------------- snap + node (pedestrian self)
def _uf(n):
    p = list(range(n))
    def find(a):
        while p[a] != a:
            p[a] = p[p[a]]; a = p[a]
        return a
    def union(a, b):
        p[find(a)] = find(b)
    return find, union


def snap_lines(lines, tol, stats):
    """1) cluster line ends within tol (centroid) 2) end -> nearest other line within tol: insert vertex."""
    ends = [(i, k) for i in range(len(lines)) for k in (0, -1)]
    pts = [Point(lines[i]["coords"][k]) for i, k in ends]
    tree = STRtree(pts)
    find, union = _uf(len(ends))
    for a, p in enumerate(pts):
        for b in tree.query(p.buffer(tol)):
            if b > a and p.distance(pts[b]) <= tol:
                union(a, b)
    groups = defaultdict(list)
    for a in range(len(ends)):
        groups[find(a)].append(a)
    moved = 0
    single = []
    for g in groups.values():
        if len(g) == 1:
            single.append(g[0]); continue
        cx = sum(pts[a].x for a in g) / len(g); cy = sum(pts[a].y for a in g) / len(g)
        for a in g:
            i, k = ends[a]
            if lines[i]["coords"][k] != (cx, cy):
                lines[i]["coords"][k] = (cx, cy); moved += 1
    stats["snap_end_clusters"] = sum(1 for g in groups.values() if len(g) > 1)
    stats["snap_ends_moved"] = moved
    # end-to-line
    geoms = [LineString(l["coords"]) for l in lines]
    ltree = STRtree(geoms)
    inserted = 0
    for a in single:
        i, k = ends[a]
        p = Point(lines[i]["coords"][k])
        best, bd = None, tol + 1e-9
        for j in ltree.query(p.buffer(tol)):
            if j == i or (lines[j]["bridge"] and not lines[i]["bridge"]):
                continue
            d = geoms[j].distance(p)
            if d < bd:
                best, bd = j, d
        if best is None:
            continue
        c = lines[best]["coords"]
        line = LineString(c)
        s = line.project(p)
        q = line.interpolate(s)
        qxy = (q.x, q.y)
        if qxy in c:
            lines[i]["coords"][k] = qxy
            continue
        # insert vertex after the segment containing s
        acc = 0.0
        for idx in range(len(c) - 1):
            seg = math.dist(c[idx], c[idx + 1])
            if acc + seg >= s - 1e-9:
                c.insert(idx + 1, qxy); break
            acc += seg
        lines[i]["coords"][k] = qxy
        inserted += 1
        geoms[best] = LineString(c)
    stats["snap_end_to_line_inserted"] = inserted
    return lines


def node_lines(lines):
    """Split lines at every crossing (unary_union), except bridge x non-bridge. Attributes follow the pieces."""
    out = []
    for flag in (False, True):
        sub = [l for l in lines if l["bridge"] == flag and len(l["coords"]) >= 2]
        if not sub:
            continue
        geoms = [LineString(l["coords"]) for l in sub]
        u = unary_union(geoms)
        pieces = [g for g in getattr(u, "geoms", [u]) if g.geom_type == "LineString" and g.length > 1e-3]
        tree = STRtree(geoms)
        for pc in pieces:
            mid = pc.interpolate(0.5, normalized=True)
            j = tree.query_nearest(mid, max_distance=0.2)
            if len(j) == 0:
                j = tree.query_nearest(mid)
            src = sub[int(j[0])]
            tot = sum(src["parts"].values()) or 1.0
            out.append(dict(coords=list(pc.coords), bridge=flag,
                            parts={k: v / tot * pc.length for k, v in src["parts"].items()}))
    return out


# ----------------------------------------------------------------------------- graph
class Graph:
    def __init__(self, work):
        self.nodes, self.edges = {}, {}
        self.adj = defaultdict(set)
        self._n = self._e = 0
        self.bound = work.exterior
        self.border = set()
        self.keymap = {}
        self.removed = []          # [(coords, rule)] deleted by the TASK_32 rules, for the *_removed.geojson files
        self.car_nodes = set()     # TASK_34: nodes that coincide with the car reference — never treated as islands,
                                   # never pruned/merged/contracted away (the join to car must stay a real vertex)

    def node_at(self, xy):
        key = (round(xy[0], 3), round(xy[1], 3))
        if key in self.keymap:
            return self.keymap[key]
        n = self._n; self._n += 1
        self.nodes[n] = key; self.keymap[key] = n
        if self.bound.distance(Point(key)) < 0.05:
            self.border.add(n)
        return n

    def new_node(self, xy):
        n = self._n; self._n += 1
        self.nodes[n] = (xy[0], xy[1])
        return n

    def add_edge(self, u, v, coords, parts, bridge, merged=False):
        e = self._e; self._e += 1
        coords = [tuple(c) for c in coords]
        coords[0], coords[-1] = self.nodes[u], self.nodes[v]
        self.edges[e] = dict(u=u, v=v, coords=coords, parts=dict(parts), bridge=bridge, merged=merged, straight=False, orig=None, turn=False)
        self.adj[u].add(e); self.adj[v].add(e)
        return e

    def del_edge(self, e):
        d = self.edges.pop(e)
        self.adj[d["u"]].discard(e); self.adj[d["v"]].discard(e)
        return d

    def rem(self, e, rule):
        d = self.del_edge(e)
        self.removed.append((list(d["coords"]), rule))
        return d

    def unnamed(self, e):
        parts = self.edges[e]["parts"]
        tot = sum(parts.values()) or 1.0
        return sum(v for k, v in parts.items() if k[2]) / tot >= 0.5

    def deg(self, n):
        return sum(2 if self.edges[e]["u"] == self.edges[e]["v"] else 1 for e in self.adj[n])

    def length(self, e):
        c = self.edges[e]["coords"]
        return sum(math.dist(c[i], c[i + 1]) for i in range(len(c) - 1))

    def other(self, e, n):
        d = self.edges[e]
        return d["v"] if d["u"] == n else d["u"]

    def drop_node_if_free(self, n):
        if not self.adj[n]:
            self.nodes.pop(n, None); self.adj.pop(n, None); self.border.discard(n); self.car_nodes.discard(n)

    @staticmethod
    def build(pieces, work, car_node_keys=frozenset()):
        g = Graph(work)
        for pc in pieces:
            c = pc["coords"]
            u, v = g.node_at(c[0]), g.node_at(c[-1])
            if u == v and len(c) <= 2:
                continue
            g.add_edge(u, v, c, pc["parts"], pc["bridge"])
        if car_node_keys:
            g.car_nodes = {n for n, xy in g.nodes.items() if (round(xy[0], 3), round(xy[1], 3)) in car_node_keys}
        return g

    def pieces(self):
        return [dict(coords=list(d["coords"]), parts=d["parts"], bridge=d["bridge"]) for d in self.edges.values()]

    def move_node(self, n, xy):
        self.nodes[n] = (xy[0], xy[1])
        for e in self.adj[n]:
            d = self.edges[e]
            if d["u"] == n: d["coords"][0] = self.nodes[n]
            if d["v"] == n: d["coords"][-1] = self.nodes[n]

    def merge_nodes(self, keep, gone):
        """redirect all edges of `gone` to `keep` (coordinates already equal)."""
        for e in list(self.adj[gone]):
            d = self.edges[e]
            if d["u"] == gone: d["u"] = keep; d["coords"][0] = self.nodes[keep]
            if d["v"] == gone: d["v"] = keep; d["coords"][-1] = self.nodes[keep]
            self.adj[keep].add(e)
        self.adj[gone].clear()
        self.nodes.pop(gone, None); self.adj.pop(gone, None); self.border.discard(gone)
        if gone in self.car_nodes:
            self.car_nodes.discard(gone); self.car_nodes.add(keep)

    def merge_parts(self, into, frm):
        a, b = self.edges[into]["parts"], self.edges[frm]["parts"]
        for k, v in b.items():
            a[k] = a.get(k, 0) + v
        self.edges[into]["bridge"] |= self.edges[frm]["bridge"]
        self.edges[into]["turn"] |= self.edges[frm]["turn"]
        self.edges[into]["merged"] = True


def _oriented(d, n_end):
    """coords of edge d oriented so that it ENDS at node n_end (returns coords, start node)."""
    if d["v"] == n_end:
        return list(d["coords"]), d["u"]
    return list(reversed(d["coords"])), d["v"]


# --- graph operations (unchanged from TASK_32 except: no rule ever touches a node in g.car_nodes — the join to
# the fixed car reference must stay a real vertex of pedestrian, or the two networks drift apart again).
def dissolve_deg2(g, st):
    n_changed = 0
    again = True
    while again:
        again = False
        for n in list(g.nodes):
            if n in g.border or n in g.car_nodes or n not in g.adj or len(g.adj[n]) != 2:
                continue
            e1, e2 = list(g.adj[n])
            if e1 == e2:
                continue
            d1, d2 = g.edges[e1], g.edges[e2]
            if d1["u"] == d1["v"] or d2["u"] == d2["v"]:
                continue
            if g.other(e1, n) == g.other(e2, n):     # would create a self-loop: keep the node
                continue
            c1, a = _oriented(d1, n)
            c2r, b = _oriented(d2, n)
            c2 = list(reversed(c2r))              # starts at n
            coords = c1 + c2[1:]
            parts = dict(d1["parts"])
            for k, v in d2["parts"].items():
                parts[k] = parts.get(k, 0) + v
            br = d1["bridge"] or d2["bridge"]
            g.del_edge(e1); g.del_edge(e2)
            g.nodes.pop(n, None); g.adj.pop(n, None)
            ne = g.add_edge(a, b, coords, parts, br, merged=True)
            g.edges[ne]["turn"] = d1["turn"] or d2["turn"]
            n_changed += 1; again = True
    st["dissolved_deg2"] = st.get("dissolved_deg2", 0) + n_changed
    return n_changed


def fix_loops_and_dups(g, P, st):
    ch = 0
    for e in list(g.edges):
        if e not in g.edges:
            continue
        d = g.edges[e]
        if d["u"] != d["v"]:
            continue
        L = g.length(e)
        if L < 4 * P["min_seg"]:
            g.del_edge(e); g.drop_node_if_free(d["u"]); st["selfloops_removed"] = st.get("selfloops_removed", 0) + 1
        else:  # split at the middle vertex so that no edge starts and ends at the same node
            c = d["coords"]; half = L / 2
            acc, k = 0.0, 0
            for k in range(len(c) - 1):
                seg = math.dist(c[k], c[k + 1])
                if acc + seg >= half:
                    break
                acc += seg
            t = (half - acc) / seg if seg else 0.0
            mid = (c[k][0] + t * (c[k + 1][0] - c[k][0]), c[k][1] + t * (c[k + 1][1] - c[k][1]))
            m = g.new_node(mid)
            c1 = c[:k + 1] + [mid]
            c2 = [mid] + c[k + 1:]
            u = d["u"]; parts = d["parts"]; br = d["bridge"]
            g.del_edge(e)
            g.add_edge(u, m, c1, {k: v / 2 for k, v in parts.items()}, br, True)
            g.add_edge(m, u, c2, {k: v / 2 for k, v in parts.items()}, br, True)
            st["selfloops_split"] = st.get("selfloops_split", 0) + 1
        ch += 1
    groups = defaultdict(list)
    for e, d in g.edges.items():
        groups[frozenset((d["u"], d["v"]))].append(e)
    for es in groups.values():
        if len(es) < 2:
            continue
        es = sorted(es, key=g.length)
        keep = []
        for e in es:
            ge = LineString(g.edges[e]["coords"])
            dup, rule = None, None
            for k in keep:
                hd = LineString(g.edges[k]["coords"]).hausdorff_distance(ge)
                if hd <= P["dup_tol"]:
                    dup, rule = k, "dup"; break
                # TASK_32 rule 2: short detour = two edges between one pair of nodes of nearly equal length
                if abs(g.length(e) - g.length(k)) < P["parallel_tol"] and hd <= 2 * P["parallel_tol"]:
                    dup, rule = k, "2_parallel"; break
            if dup is None:
                keep.append(e)
            else:
                g.merge_parts(dup, e)
                if rule == "dup":
                    g.del_edge(e); st["duplicate_edges_removed"] = st.get("duplicate_edges_removed", 0) + 1
                else:
                    g.rem(e, rule); st["parallel_detours_removed"] = st.get("parallel_detours_removed", 0) + 1
                ch += 1
    return ch


def prune_spurs(g, P, st):
    """TASK_32 rule 1: free ends (dead end -> nearest node of degree >= 3) <= spur_len are removed; unnamed street
    axes (Widmung 9997/9999, no name) up to 2 x spur_len. Free ends on the area border are clip artefacts: kept,
    except stubs shorter than min_seg. TASK_34: a free end that IS a car-reference node is never a spur (it is
    an attachment point to car, e.g. a path that ends exactly at a street) — those short stubs would otherwise
    be deleted by this rule, breaking the join to car."""
    total = 0
    while True:
        ch = 0
        for e in sorted(g.edges, key=g.length):
            if e not in g.edges:
                continue
            d = g.edges[e]
            for a, b in ((d["u"], d["v"]), (d["v"], d["u"])):
                if a == b or g.deg(a) != 1 or g.deg(b) < 3 or a in g.car_nodes:
                    continue
                L = g.length(e)
                if a in g.border:
                    lim, rule = P["min_seg"], "1_border_stub"
                elif g.unnamed(e):
                    lim, rule = 2 * P["spur_len"], ("1_spur" if L < P["spur_len"] else "1_unnamed_spur")
                else:
                    lim, rule = P["spur_len"], "1_spur"
                if L < lim:
                    g.rem(e, rule); g.drop_node_if_free(a)
                    ch += 1; st["spurs_removed"] = st.get("spurs_removed", 0) + 1
                    break
        total += ch
        if not ch:
            return total
        dissolve_deg2(g, st)


def remove_lollipops(g, P, st):
    """TASK_32 rule 2: a small cycle attached to the network at ONE node (turnaround loop). Cycle perimeter <= loop_max
    -> removed; the stem (now a dead end) <= stem_max -> removed too, longer stems stay and get turnaround_removed."""
    H = nx.Graph()
    for e, d in g.edges.items():
        H.add_edge(d["u"], ("a", e)); H.add_edge(("a", e), ("b", e)); H.add_edge(("b", e), d["v"])
    done = 0
    for blk in list(nx.biconnected_components(H)):
        sub = H.subgraph(blk)
        if sub.number_of_edges() != len(blk) or len(blk) < 3:
            continue                                              # not a simple cycle
        orig = [n for n in blk if not isinstance(n, tuple)]
        eids = {n[1] for n in blk if isinstance(n, tuple)}
        if any(e not in g.edges for e in eids):
            continue
        attach = [n for n in orig if H.degree(n) > 2 or n in g.border]
        if len(attach) != 1:
            continue
        per = sum(g.length(e) for e in eids)
        if per > P["loop_max"]:
            continue
        at = attach[0]
        if any(n in g.border for n in orig if n != at):
            continue
        for e in eids:
            g.rem(e, "2_loop")
        for n in orig:
            if n != at:
                g.drop_node_if_free(n)
        st["turnaround_loops_removed"] = st.get("turnaround_loops_removed", 0) + 1
        done += 1
        if g.deg(at) == 1 and at not in g.border and at not in g.car_nodes:
            (se,) = list(g.adj[at])
            L = g.length(se)
            if L <= P["stem_max"]:
                g.rem(se, "2_stem"); g.drop_node_if_free(at)
                st["turnaround_stems_removed"] = st.get("turnaround_stems_removed", 0) + 1
            else:
                g.edges[se]["turn"] = True
                st["turnaround_stems_kept"] = st.get("turnaround_stems_kept", 0) + 1
    return done


def cluster_nodes(g, P, st, forced=()):
    tol = P["node_cluster"]
    ids = [n for n in g.nodes if n not in g.border and n not in g.car_nodes and g.deg(n) >= 3]
    if not ids and not forced:
        return 0
    pts = [Point(g.nodes[n]) for n in ids]
    idx = {n: i for i, n in enumerate(ids)}
    find, union = _uf(len(ids))
    tree = STRtree(pts)
    for a, p in enumerate(pts):
        for b in tree.query(p.buffer(tol)):
            if b > a and p.distance(pts[b]) <= tol:
                union(a, b)
    groups = defaultdict(list)
    for i, n in enumerate(ids):
        groups[find(i)].append(n)
    clusters = [g_ for g_ in groups.values() if len(g_) > 1]
    # forced pairs (near-parallel edges): extra unions between arbitrary non-border, non-car nodes
    if forced:
        owner = {}
        for ci, cl in enumerate(clusters):
            for n in cl: owner[n] = ci
        for a, b in forced:
            if a not in g.nodes or b not in g.nodes or a in g.border or b in g.border or a in g.car_nodes or b in g.car_nodes:
                continue
            ca, cb = owner.get(a), owner.get(b)
            if ca is None and cb is None:
                clusters.append([a, b]); owner[a] = owner[b] = len(clusters) - 1
            elif ca is None:
                clusters[cb].append(a); owner[a] = cb
            elif cb is None:
                clusters[ca].append(b); owner[b] = ca
            elif ca != cb:
                clusters[ca] += clusters[cb]
                for n in clusters[cb]: owner[n] = ca
                clusters[cb] = []
    done = 0
    for cl in clusters:
        cl = [n for n in cl if n in g.nodes]
        if len(cl) < 2:
            continue
        cx = sum(g.nodes[n][0] for n in cl) / len(cl); cy = sum(g.nodes[n][1] for n in cl) / len(cl)
        hub = cl[0]
        inner = [e for e in g.edges if g.edges[e]["u"] in cl and g.edges[e]["v"] in cl]
        for e in inner:
            if g.length(e) <= 3 * tol + 1:
                g.del_edge(e)
        for n in cl:
            g.nodes[n] = (cx, cy)
        for n in cl[1:]:
            g.merge_nodes(hub, n)
        g.move_node(hub, (cx, cy))
        done += 1
    st["node_clusters_merged"] = st.get("node_clusters_merged", 0) + done
    return done


def contract_short(g, P, st):
    done = 0
    while True:
        # measured as the chord: after simplification / "straight" the edge can shrink down to the chord
        cand = [(math.dist(d["coords"][0], d["coords"][-1]), e) for e, d in g.edges.items()
                if d["u"] != d["v"] and d["u"] not in g.border and d["v"] not in g.border
                and d["u"] not in g.car_nodes and d["v"] not in g.car_nodes
                and g.deg(d["u"]) >= 2 and g.deg(d["v"]) >= 2]
        cand = [c for c in cand if c[0] < P["min_seg"]]
        if not cand:
            break
        _, e = min(cand)
        d = g.del_edge(e)
        u, v = d["u"], d["v"]
        mx = ((g.nodes[u][0] + g.nodes[v][0]) / 2, (g.nodes[u][1] + g.nodes[v][1]) / 2)
        g.nodes[v] = mx
        g.merge_nodes(u, v)
        g.move_node(u, mx)
        done += 1
    st["short_edges_contracted"] = st.get("short_edges_contracted", 0) + done
    return done


def near_parallel_pairs(g, P):
    """pairs of long-ish edges that run alongside each other within dup_tol, with matching ends -> node pairs to merge."""
    tol = P["dup_tol"]
    es = [e for e in g.edges if g.length(e) >= 3 * tol]
    geoms = {e: LineString(g.edges[e]["coords"]) for e in es}
    tree = STRtree([geoms[e] for e in es])
    forced, seen = [], set()
    for e in es:
        a = geoms[e]
        for j in tree.query(a.buffer(tol)):
            f = es[j]
            if f <= e or (e, f) in seen:
                continue
            seen.add((e, f))
            b = geoms[f]
            da, db = g.edges[e], g.edges[f]
            if {da["u"], da["v"]} & {db["u"], db["v"]}:
                continue
            short, long_ = (a, b) if a.length <= b.length else (b, a)
            if short.difference(long_.buffer(tol)).length > 0.1 * short.length:
                continue
            nu = lambda n: Point(g.nodes[n])
            d1 = nu(da["u"]).distance(nu(db["u"])) + nu(da["v"]).distance(nu(db["v"]))
            d2 = nu(da["u"]).distance(nu(db["v"])) + nu(da["v"]).distance(nu(db["u"]))
            m = ((da["u"], db["u"]), (da["v"], db["v"])) if d1 <= d2 else ((da["u"], db["v"]), (da["v"], db["u"]))
            if all(nu(x).distance(nu(y)) <= 2.5 * tol for x, y in m):
                forced += list(m)
    return forced


def crossings(lines_geoms, bridge_flags, tol=0.02):
    """indices (i,j) whose intersection is not a shared end vertex of both: (violations, declared unlink points)."""
    tree = STRtree(lines_geoms)
    bad, unlinks = [], []
    for i, a in enumerate(lines_geoms):
        for j in tree.query(a):
            if j <= i:
                continue
            b = lines_geoms[j]
            x = a.intersection(b)
            if x.is_empty:
                continue
            pts = []
            if x.geom_type == "Point": pts = [x]
            elif x.geom_type == "MultiPoint": pts = list(x.geoms)
            else:
                bad.append((i, j, "overlap")); continue
            ea = [Point(a.coords[0]), Point(a.coords[-1])]
            eb = [Point(b.coords[0]), Point(b.coords[-1])]
            for p in pts:
                if any(p.distance(q) < tol for q in ea) and any(p.distance(q) < tol for q in eb):
                    continue
                if bridge_flags[i] or bridge_flags[j]:
                    unlinks.append((i, j, p))
                else:
                    bad.append((i, j, "crossing"))
    return bad, unlinks


def renode_if_needed(g, work, st):
    ids = list(g.edges)
    geoms = [LineString(g.edges[e]["coords"]) for e in ids]
    bad, _ = crossings(geoms, [g.edges[e]["bridge"] for e in ids])
    if not bad:
        return g
    st["renoded"] = st.get("renoded", 0) + 1
    st["crossings_after_moves"] = st.get("crossings_after_moves", 0) + len(bad)
    car_keys = {g.nodes[n] for n in g.car_nodes}
    ng = Graph.build(node_lines(g.pieces()), work, car_keys)
    ng.removed = g.removed
    return ng


def clean_graph(g, P, work, st):
    for it in range(12):
        ch = 0
        ch += remove_lollipops(g, P, st)
        ch += dissolve_deg2(g, st)
        ch += fix_loops_and_dups(g, P, st)
        ch += dissolve_deg2(g, st)
        ch += prune_spurs(g, P, st)
        forced = near_parallel_pairs(g, P)
        if forced:
            st["parallel_pairs_merged"] = st.get("parallel_pairs_merged", 0) + len(forced) // 2
        ch += cluster_nodes(g, P, st, forced)
        ch += contract_short(g, P, st)
        ch += fix_loops_and_dups(g, P, st)
        ch += dissolve_deg2(g, st)
        ng = renode_if_needed(g, work, st)
        if ng is not g:
            g = ng; ch += 1
        if not ch:
            break
    st["clean_iterations"] = it + 1
    return g


def simplify_edges(g, P, st):
    """Rule 3. Per edge three candidates, best first: one straight line (Hausdorff to the chord <= straight_tol),
    Douglas-Peucker (simplify_tol), original. A candidate that would cross another edge is downgraded one level."""
    tol, stol = P["simplify_tol"], P["straight_tol"]
    v_before = sum(len(d["coords"]) for d in g.edges.values())
    for e, d in g.edges.items():
        orig = list(d["coords"])
        cands = []
        chord = [orig[0], orig[-1]]
        if len(orig) > 2 and d["u"] != d["v"] and LineString(orig).hausdorff_distance(LineString(chord)) <= stol:
            cands.append((chord, True))
        if len(orig) > 2:
            new = list(shapely.simplify(LineString(orig), tol, preserve_topology=False).coords)
            if new[0] == orig[0] and new[-1] == orig[-1] and len(new) >= 2 and len(new) < len(orig):
                cands.append((new, len(new) == 2))
        cands.append((orig, len(orig) == 2))
        d["orig"] = orig; d["cands"] = cands; d["lvl"] = 0
        d["coords"], d["straight"] = cands[0]
    reverted = 0
    for _ in range(12):
        ids = list(g.edges)
        geoms = [LineString(g.edges[e]["coords"]) for e in ids]
        bad, _u = crossings(geoms, [g.edges[e]["bridge"] for e in ids])
        if not bad:
            break
        touched = set()
        for i, j, _k in bad:
            touched.add(ids[i]); touched.add(ids[j])
        for e in touched:
            d = g.edges[e]
            if d["lvl"] < len(d["cands"]) - 1:
                d["lvl"] += 1; d["coords"], d["straight"] = d["cands"][d["lvl"]]; reverted += 1
    st["simplify_downgraded_for_topology"] = reverted
    st["edges_straight"] = sum(1 for d in g.edges.values() if d["straight"])
    st["vertices_before"] = v_before
    st["vertices_after"] = sum(len(d["coords"]) for d in g.edges.values())
    return g


# ----------------------------------------------------------------------------- TASK_34: island rule vs. car
def remove_islands_vs_car(g, st):
    """TASK_34 replacement for TASK_32's `union_filter`: car is fixed reference geometry, its nodes are marked
    on this graph as `car_nodes` (Graph.build/renode_if_needed). A pedestrian component is kept if it is the
    largest pedestrian component, OR it touches the working-area border, OR any of its nodes is a car_node —
    i.e. it reaches the rest of the network (car ∪ pedestrian) through car."""
    S = nx.Graph()
    for e, d in g.edges.items():
        S.add_edge(d["u"], d["v"])
    comps = list(nx.connected_components(S))
    sizes = [sum(1 for e, d in g.edges.items() if d["u"] in c) for c in comps]
    biggest = sizes.index(max(sizes)) if sizes else None
    removed = []
    for i, c in enumerate(comps):
        if i == biggest or (c & g.car_nodes) or (c & g.border):
            continue
        es = [e for e, d in g.edges.items() if d["u"] in c]
        removed.append(dict(edges=len(es), length=round(sum(g.length(e) for e in es), 1),
                            at=[round(v, 1) for v in g.nodes[next(iter(c))]]))
        for e in es:
            g.rem(e, "1_disconnected")
    for n in list(g.nodes):
        g.drop_node_if_free(n)
    st["islands_removed"] = st.get("islands_removed", []) + removed
    st["union_components_total"] = len(comps)
    st["union_components_removed"] = st.get("union_components_removed", 0) + len(removed)
    return g


# ----------------------------------------------------------------------------- output
def to_features(g, network, aoi, work):
    feats = []
    for e, d in sorted(g.edges.items(), key=lambda kv: (kv[1]["coords"][0], kv[1]["coords"][-1])):
        (cls, layer, _un), _ = max(d["parts"].items(), key=lambda kv: kv[1])
        line = LineString(d["coords"])
        simplified = bool(d["merged"] or (d["orig"] is not None and d["orig"] != d["coords"]))
        feats.append(dict(geom=line, props=dict(network=network, length=round(line.length, 1), srcClass=cls, srcLayer=layer,
                                                 simplified=simplified, straight=bool(d["straight"]),
                                                 inAOI=bool(line.intersects(aoi)), bridge=bool(d["bridge"]),
                                                 turnaround_removed=bool(d["turn"]))))
    return feats


def write_geojson(path, feats, name, network=None):
    out = []
    for i, f in enumerate(feats):
        props = dict(f["props"])
        if network:
            props = dict(ssId=f"{network[:3]}{i + 1:05d}", **props)
        out.append({"type": "Feature", "properties": props, "geometry": mapping(transform(FROM_UTM, f["geom"]))})
    fc = {"type": "FeatureCollection", "name": name, "crs": {"type": "name", "properties": {"name": "urn:ogc:def:crs:OGC:1.3:CRS84"}},
          "features": out}
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(fc, fh, ensure_ascii=False, separators=(",", ":"))


def _pairs(g):
    out = defaultdict(list)
    for e, d in g.edges.items():
        out[frozenset((d["u"], d["v"]))].append(e)
    return out


def prepare_pedestrian(work, report, car):
    st0 = {}
    touch = set()                                          # every coordinate placed exactly on car
    lines = load_lines("pedestrian", work)
    st0["source_lines_clipped"] = len(lines)
    st0["source_bridge_flagged"] = sum(1 for l in lines if l["bridge"])
    lines = plaza_perimeter_lines(lines, work, st0)
    lines = snap_ends_to_car(lines, car, st0, touch)       # TASK_34 part 2
    lines = node_at_car_crossings(lines, car, st0, touch)  # TASK_34 part 2
    lines = snap_lines(lines, SNAP_TOL, st0)               # pedestrian-to-pedestrian, as before
    noded = node_lines(lines)
    st0["noded_pieces"] = len(noded)
    st0["noded_vertices"] = sum(len(l["coords"]) for l in noded)
    st0["car_touch_points"] = len(touch)
    report["pedestrian/prep"] = st0
    print("pedestrian prep", st0)
    return noded, touch


def build_one(preset, work, noded, car, car_keys):
    P = PRESETS[preset]
    st = {}
    g = Graph.build(noded, work, car_keys)
    st["input_edges"], st["input_nodes"] = len(g.edges), len(g.nodes)
    st["input_car_anchor_nodes"] = len(g.car_nodes)
    g = clean_graph(g, P, work, st)
    g = remove_islands_vs_car(g, st)
    g = simplify_edges(g, P, st)
    n0 = st.get("spurs_removed", 0)
    prune_spurs(g, P, st); dissolve_deg2(g, st)
    st["spurs_removed_after_straightening"] = st.get("spurs_removed", 0) - n0
    g = remove_islands_vs_car(g, st)                       # re-check after straightening/final spur pass
    st["edges_straight"] = sum(1 for d in g.edges.values() if d["straight"])
    return g, st


def snap_final_free_ends(feats, car, st):
    """TASK_34 safety net, second half: a free end created DURING cleaning (spur removal exposing a former
    interior point, node clustering moving a node to a centroid, short-edge contraction averaging two node
    positions) never went through snap_ends_to_car (which only ran once, on the raw source lines) and can end
    up a few metres from car without being exactly on it. Snapped here for every degree-1 pedestrian end that
    is still within CAR_SNAP_TOL — mirrors snap_ends_to_car's own node-vs-line logic. A genuine dead end that is
    simply not near car (the common case: most path ends are interior junctions or far from any street) is
    untouched.

    Moving an endpoint by up to CAR_SNAP_TOL creates a brand new final segment, which — unlike the original
    pre-cleaning snap — is not re-noded against the rest of the network afterwards. A candidate is only
    committed if that new segment does not cross anything else (car or another pedestrian line) except at its
    own new endpoint; otherwise the free end is left as it was (reported, not silently forced)."""
    all_geoms = list(car["lines"]) + [f["geom"] for f in feats]
    tree = STRtree(all_geoms)
    from collections import Counter
    deg = Counter()
    for f in feats:
        deg[f["geom"].coords[0]] += 1
        deg[f["geom"].coords[-1]] += 1
    fixed = rejected = 0
    for f in feats:
        ln = f["geom"]
        c = list(ln.coords)
        changed = False
        for k in (0, -1):
            if deg[c[k]] != 1:
                continue
            p = Point(c[k])
            node_cand = [i for i in car["node_tree"].query(p.buffer(CAR_NODE_TOL))
                        if car["nodes"][i].distance(p) <= CAR_NODE_TOL]
            if node_cand:
                best = min(node_cand, key=lambda i: car["nodes"][i].distance(p))
                xy = (car["nodes"][best].x, car["nodes"][best].y)
            else:
                line_cand = [i for i in car["line_tree"].query(p.buffer(CAR_SNAP_TOL))
                            if car["lines"][i].distance(p) <= CAR_SNAP_TOL]
                if not line_cand:
                    continue
                best = min(line_cand, key=lambda i: car["lines"][i].distance(p))
                proj = car["lines"][best].interpolate(car["lines"][best].project(p))
                xy = (proj.x, proj.y)
            if xy == c[k]:
                continue
            other = c[1] if k == 0 else c[-2]
            new_seg = LineString([other, xy])
            safe = True
            for j in tree.query(new_seg.buffer(0.5)):
                og = all_geoms[j]
                if og is ln:
                    continue
                x = new_seg.intersection(og)
                if x.is_empty:
                    continue
                # touching only at the new endpoint (xy) itself is fine — that IS the intended car join
                pts = [x] if x.geom_type == "Point" else (list(x.geoms) if x.geom_type == "MultiPoint" else None)
                if pts is None or any(q.distance(Point(xy)) > 0.02 for q in pts):
                    safe = False; break
            if not safe:
                rejected += 1; continue
            c[k] = xy; changed = True; fixed += 1
        if changed:
            f["geom"] = LineString(c)
    st["final_free_ends_snapped_to_car"] = fixed
    st["final_free_ends_snap_rejected_topology"] = rejected
    return feats


def insert_final_car_crossings(feats, car, st):
    """TASK_34 safety net: graph cleaning (dissolve_deg2 merging degree-2 chains, Douglas-Peucker / straightening
    in simplify_edges) can simplify away an interior vertex that used to sit exactly on a car crossing, even
    though it started out protected as a GRAPH NODE (car_nodes) at the time it was created — dissolve_deg2 folds
    a car_node's neighbours into one edge only when the car_node itself is NOT the shared node, but a vertex
    that stays inside the resulting long edge's coordinate list can still be dropped later by
    `shapely.simplify()`. Run once, directly on the final line geometry, right before writing: for every
    pedestrian line that still crosses car (or no longer touches it because simplification moved it off), find
    the intersection points and make sure the line's coordinate list has a vertex there. This changes vertex
    lists only — the line's spatial path is unchanged, so it cannot create a new pedestrian-pedestrian crossing."""
    fixed = 0
    for f in feats:
        ln = f["geom"]
        cand = car["line_tree"].query(ln)
        new_pts = []
        for i in cand:
            cg, cb = car["lines"][i], car["bridge"][i]
            if f["props"]["bridge"] or cb:
                continue
            x = ln.intersection(cg)
            if x.is_empty:
                continue
            pts = [x] if x.geom_type == "Point" else (list(x.geoms) if x.geom_type == "MultiPoint" else [])
            for q in pts:
                if any(q.distance(Point(c)) < 0.02 for c in ln.coords):
                    continue
                new_pts.append((q.x, q.y))
        if not new_pts:
            continue
        c = list(ln.coords)
        for xy in new_pts:
            if any(math.dist(xy, p) < 0.02 for p in c):
                continue
            s_ = ln.project(Point(xy))
            acc, idx = 0.0, 0
            for idx in range(len(c) - 1):
                seg = math.dist(c[idx], c[idx + 1])
                if acc + seg >= s_ - 1e-9:
                    break
                acc += seg
            c.insert(idx + 1, xy); fixed += 1
        f["geom"] = LineString(c)
    st["final_car_crossing_vertices_added"] = fixed
    return feats


def finish(preset, g, st, work, aoi, report, car):
    dissolve_deg2(g, st)
    st["output_edges"], st["output_nodes"] = len(g.edges), len(g.nodes)
    feats = to_features(g, "pedestrian", aoi, work)
    feats = snap_final_free_ends(feats, car, st)
    feats = insert_final_car_crossings(feats, car, st)
    # A handful of edges can end up near-zero-length — node clustering / short-edge contraction averaging two
    # DIFFERENT graph nodes into coordinates a few mm-cm apart (not a true self-loop — u != v — so
    # fix_loops_and_dups never sees it), or, rarer, both ends of an already-short edge independently snapping
    # onto the same nearby point of car. Degenerate, carries no real geometry, dropped here (after the two
    # passes above, so anything THEY create is caught too).
    degenerate = [f for f in feats if f["geom"].length < 0.1]
    feats = [f for f in feats if f["geom"].length >= 0.1]
    st["degenerate_edges_dropped"] = len(degenerate)
    geoms = [f["geom"] for f in feats]
    flags = [f["props"]["bridge"] for f in feats]
    bad, unl = crossings(geoms, flags)
    st["crossings_without_node"] = len(bad)
    st["unlinks_declared"] = len(unl)
    st["straight_share"] = round(st["edges_straight"] / max(1, len(g.edges)), 3)
    st["vertices_after"] = sum(len(f["geom"].coords) for f in feats)
    write_geojson(DATA / f"weimar_ss_pedestrian_{preset}.geojson", feats, f"weimar_ss_pedestrian_{preset}", "pedestrian")
    pts = [{"type": "Feature", "properties": dict(network="pedestrian", a=feats[i]["props"]["srcClass"], b=feats[j]["props"]["srcClass"],
                                                  aBridge=feats[i]["props"]["bridge"], bBridge=feats[j]["props"]["bridge"]),
            "geometry": mapping(transform(FROM_UTM, p))} for i, j, p in unl]
    with open(DATA / f"weimar_ss_pedestrian_{preset}_unlinks.geojson", "w", encoding="utf-8") as fh:
        json.dump({"type": "FeatureCollection", "name": f"weimar_ss_pedestrian_{preset}_unlinks", "features": pts}, fh, separators=(",", ":"))
    rem, by_rule = [], {}
    for coords, rule in g.removed:
        if len(coords) < 2:
            continue
        ln = LineString(coords)
        rem.append({"type": "Feature", "properties": dict(network="pedestrian", rule=rule, length=round(ln.length, 1)),
                    "geometry": mapping(transform(FROM_UTM, ln))})
        r = by_rule.setdefault(rule, [0, 0.0]); r[0] += 1; r[1] += ln.length
    with open(DATA / f"weimar_ss_pedestrian_{preset}_removed.geojson", "w", encoding="utf-8") as fh:
        json.dump({"type": "FeatureCollection", "name": f"weimar_ss_pedestrian_{preset}_removed", "features": rem}, fh,
                  ensure_ascii=False, separators=(",", ":"))
    st["removed_by_rule"] = {k: dict(segments=v[0], length_m=round(v[1], 1)) for k, v in sorted(by_rule.items())}
    st["parallel_multiedges_kept"] = sum(1 for grp in _pairs(g).values() if len(grp) > 1)
    report[f"pedestrian/{preset}"] = st
    print("pedestrian", preset, {k: v for k, v in st.items()})


def main():
    aoi, work = working_area()
    PROC.mkdir(parents=True, exist_ok=True)
    presets = [a for a in sys.argv[1:] if a in PRESETS] or list(PRESETS)
    report = {"buffer_m": BUFFER_M, "presets": PRESETS, "snap_tol": SNAP_TOL, "car_snap_tol": CAR_SNAP_TOL,
              "car_node_tol": CAR_NODE_TOL, "pedestrian_add_streets": PEDESTRIAN_ADD_STREETS,
              "osm_pedestrian_highway": sorted(OSM_PEDESTRIAN_HIGHWAY), "ring": [RING_MAX_LEN, RING_COMPACT],
              "task": 34, "car_reference": str(CAR_REFERENCE.relative_to(ROOT))}
    car = load_car_reference()
    report["car_reference_lines"] = len(car["lines"])
    report["car_reference_nodes"] = len(car["nodes"])
    noded, touch = prepare_pedestrian(work, report, car)
    # TASK_34 fix: protect every coordinate a pedestrian line was snapped onto (car's own vertices AND the
    # line-projection / crossing-insertion targets `touch` collected in prepare_pedestrian) from later cleaning
    # steps (dissolve_deg2, node clustering, contraction) — those only checked car's raw vertex list before,
    # which is a small minority of the actual snap targets (most ends snap onto a car LINE's interior).
    car_keys = {(round(p.x, 3), round(p.y, 3)) for p in car["nodes"]} | touch
    report["pedestrian/prep"]["car_anchor_points_total"] = len(car_keys)
    g0 = Graph.build(noded, work, car_keys)
    write_geojson(PROC / "raw_pedestrian.geojson", to_features(g0, "pedestrian", aoi, work), "raw_pedestrian", "pedestrian")
    for preset in presets:
        g, st = build_one(preset, work, noded, car, car_keys)
        finish(preset, g, st, work, aoi, report, car)
    old = json.load(open(PROC / "build_report.json")) if (PROC / "build_report.json").exists() else {}
    old.update(report)
    json.dump(old, open(PROC / "build_report.json", "w"), indent=1, default=str)


if __name__ == "__main__":
    main()
