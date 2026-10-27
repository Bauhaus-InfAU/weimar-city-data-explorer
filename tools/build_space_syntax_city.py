#!/usr/bin/env python3
"""
build_space_syntax_city.py — Space Syntax networks (car + pedestrian) for the WHOLE city of Weimar.

Inside the Base border (the working plane) nothing is rebuilt: her hand-drawn TASK_35 networks
(web/layers/data/weimar_ss_{car,pedestrian}_final.geojson) are copied coordinate-for-coordinate. Around them, out to
the city boundary, the network is generated with the TASK_32/34 rules (imported from build_space_syntax_roads.py —
same graph cleaning, same car-reference logic for pedestrian) and stitched onto her lines at the Base border edge:

  1. Area. ring = city boundary minus the Base border quadrilateral — the SAME polygon prepare_ss_final.py clipped her
     drawing with (4 WGS84 corners -> UTM), so her edge ends and the generated edge ends lie on the same line.
  2. Car. ATKIS Strassenachse in the ring -> self-snap/node -> small compact roundabouts collapsed to one node (the
     v2 rule, RING_MAX_LEN / RING_COMPACT) -> TASK_32 cleaning with CITY_PRESET -> islands -> simplification.
     Ends on the Base border edge are protected (no border-stub pruning), so every street reaching the centre keeps
     its stub for the seam.
  3. Seam. Each generated end on the Base border edge is matched one-to-one with her nearest edge end (same network,
     <= SEAM_TOL along the edge) and moved EXACTLY onto it (her WGS84 coordinate is written verbatim). Unmatched
     generated ends: a short dead stub (< spur_len) is dropped, a longer one stays and is reported; her unmatched
     ends are reported. Nothing of hers is moved.
  4. Pedestrian. Same sources as TASK_34 (WegPfadSteig, Fahrwegachse, OSM footway/path/pedestrian/steps/cycleway),
     but outside the centre field and forest paths are dropped: a source line stays only if >= KEEP_SHARE of its
     length runs through built-up land (BUILTUP_BUFFER m around buildings) or a park-like area (OSM park, cemetery,
     garden, ...). Then the TASK_34 pipeline against the generated car (snap ends <= CAR_SNAP_TOL, at-grade
     crossings noded, car-touch points protected), CITY_PRESET cleaning, seam stitching as for car (a pedestrian end
     with no pedestrian partner may join her car edge end instead). Every pedestrian vertex lying on a generated car
     line is spliced into that car line too (TASK_35 convention: identical shared vertices, car not split).

Output (web/layers/data/): weimar_ss_car_city.geojson, weimar_ss_pedestrian_city.geojson (attribute `source` =
manual | auto), ss_city_seam_review.geojson (seam points to look at in QGIS). Report: output/ss_city_report.json.

Run:  python tools/build_space_syntax_city.py
"""
import json
import math
import sys
import time
from collections import Counter, defaultdict
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import networkx as nx
from shapely.geometry import LineString, Point, Polygon, shape, mapping
from shapely.ops import polygonize, transform, unary_union
from shapely.strtree import STRtree

sys.path.insert(0, str(Path(__file__).resolve().parent))
import build_space_syntax_roads as ss  # noqa: E402  (TASK_32/34 rules, reused unchanged)

ROOT = ss.ROOT
DATA = ss.DATA
TO_UTM, FROM_UTM = ss.TO_UTM, ss.FROM_UTM

# --------------------------------------------------------------------------- parameters
CITY_PRESET = "strong"          # outskirts may be simplified strongly (her wish); geometry stays on the real axes
SEAM_TOL = 25.0                 # generated end <-> her end, max distance along the Base border edge (m)
SEAM_EPS = 2.0                  # an end this close to the Base border edge counts as "on the seam" (m)
SEAM_REVIEW_MOVE = 5.0          # seam moves bigger than this go to the review file (m)
BUILTUP_BUFFER = 120.0          # built-up land = this buffer around building centroids (m)
KEEP_SHARE = 0.5                # pedestrian source line kept if this share of it is built-up or park
PED_SPUR_LEN = 100.0            # pedestrian dead ends shorter than this are pruned (strong preset: 70)
PAR_TOL = 15.0                  # a pedestrian stretch within this of a street ...
PAR_ANGLE = 25.0                # ... and running at most this many degrees off its direction ...
PAR_MIN_RUN = 25.0              # ... over at least this length is a sidewalk: cut away (m)
EXT_MAX = 300.0                 # unmatched seam end: follow the source lines into the centre at most this far (m)
EXT_JOIN_TOL = 3.0              # ... until within this of her network (m)
NON_PARK = {"allotments", "garden"}   # allotment / private-garden grids are not parks (dense lanes, no through-routes)
DUP_TOL, DUP_ANGLE, DUP_MIN_RUN = 12.0, 20.0, 20.0   # the same path twice (ATKIS + OSM): within / degrees / over (m)
CAR_GAP = 12.0                  # street axis ending this short of another street: joined (if it heads there)
CAR_GAP_ANY = 5.0               # ... and this short: joined whatever the direction (undershoot / overshoot)
TWIN_SHARE = 0.7                # final pass: a pedestrian line alongside another (DUP_TOL) over this share of it ...
TWIN_DETOUR = 2.0               # ... is dropped if its ends stay connected within this x its length + 50 m
CAR_TO_PED_GAP = 5.0
PED_END_SNAP = 5.0              # a dead-end path this close to another line is joined onto it straight            # a street ending this close to a path is joined onto it
DETOUR_RATIO = 1.3              # pedestrian line with both ends on streets: redundant if the street way between
DETOUR_NEAR = 60.0              # ... its ends is at most this ratio longer and stays within this distance (m)
GROW_MAX = 400.0                # dead-end pedestrian line: grown along the source lines at most this far (m)
GROW_ENTRY = 8.0                # ... entering the source lines within this of its end (it was simplified)
DANGLE_DROP = 150.0             # dead-end pedestrian line still dangling after that: removed if shorter (m)
CONNECT_MAX_DIST = 3000.0       # field/forest paths: route between settlements at most this far apart (m)
CAR_WEIGHT = 2.0                # ... a street counts this many times its length on such a route
ATKIS_CITY = ROOT / "data" / "processed" / "atkis_verkehr_city"   # tools/fetch_atkis_verkehr_city.py
PARK_LANDUSE = {"park", "cemetery", "recreation_ground", "village_green", "playground",
                "dog_park", "common", "pitch", "sports_centre", "stadium"}

MANUAL = {"car": DATA / "weimar_ss_car_final.geojson", "pedestrian": DATA / "weimar_ss_pedestrian_final.geojson"}
OUT = {"car": DATA / "weimar_ss_car_city.geojson", "pedestrian": DATA / "weimar_ss_pedestrian_city.geojson"}
REVIEW = DATA / "ss_city_seam_review.geojson"
REPORT = ROOT / "output" / "ss_city_report.json"
PIN = {}   # UTM coordinate of a seam join -> her exact WGS84 coordinate, written verbatim for every line using it


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


@contextmanager
def atkis_city_set():
    """ss.load_lines / ss.plaza_perimeter_lines read ATKIS from ss.DATA: point them at the full-city ATKIS set
    (tools/fetch_atkis_verkehr_city.py) for the duration; OSM keeps coming from the platform data folder."""
    orig_data, orig_osm = ss.DATA, ss.load_osm_pedestrian

    def osm(work):
        ss.DATA = orig_data
        try:
            return orig_osm(work)
        finally:
            ss.DATA = ATKIS_CITY

    ss.DATA, ss.load_osm_pedestrian = ATKIS_CITY, osm
    try:
        yield
    finally:
        ss.DATA, ss.load_osm_pedestrian = orig_data, orig_osm


# --------------------------------------------------------------------------- area
def base_border_quad():
    """Exactly prepare_ss_final.load_border_polygon_utm(): her drawing was clipped with this polygon."""
    b = json.load(open(ss.BORDER))
    a, b2 = b["pointA"], b["pointB"]
    west, east = sorted([a["lng"], b2["lng"]])
    south, north = sorted([a["lat"], b2["lat"]])
    return Polygon([TO_UTM(x, y) for x, y in [(west, south), (east, south), (east, north), (west, north)]])


def city_polygon():
    fc = json.load(open(DATA / "weimar_boundary.geojson", encoding="utf-8"))
    return unary_union([transform(TO_UTM, shape(f["geometry"])) for f in fc["features"]])


# --------------------------------------------------------------------------- her networks (read-only)
def load_manual(network, quad):
    fc = json.load(open(MANUAL[network], encoding="utf-8"))
    feats, ends = [], []
    for f in fc["features"]:
        c84 = f["geometry"]["coordinates"]
        utm = [TO_UTM(x, y) for x, y in c84]
        feats.append(dict(f=f, utm=utm))
        for k in (0, -1):
            if Point(utm[k]).distance(quad.exterior) < 0.05:
                ends.append(dict(xy=utm[k], wgs=tuple(c84[k]), network=network))
    return feats, ends


# --------------------------------------------------------------------------- car
def collapse_roundabouts(g, st):
    """v2 (TASK_32) car rule: a small compact face of the street graph (perimeter <= RING_MAX_LEN, 4piA/P^2 >=
    RING_COMPACT) is a roundabout -> its ring edges are deleted and its nodes merged into one node at the centroid."""
    geoms = {e: LineString(d["coords"]) for e, d in g.edges.items()}
    done = 0
    for face in polygonize(list(geoms.values())):
        P, A = face.length, face.area
        if P > ss.RING_MAX_LEN or P == 0 or 4 * math.pi * A / P ** 2 < ss.RING_COMPACT:
            continue
        ring = face.exterior
        ring_edges = [e for e, gm in geoms.items() if e in g.edges and ring.distance(gm.interpolate(0.5, normalized=True)) < 0.05
                      and ring.distance(Point(g.nodes[g.edges[e]["u"]])) < 0.05
                      and ring.distance(Point(g.nodes[g.edges[e]["v"]])) < 0.05]
        nodes = {g.edges[e][k] for e in ring_edges for k in ("u", "v")}
        if len(ring_edges) < 2 or nodes & (g.border | g.car_nodes):
            continue
        c = face.centroid
        for e in ring_edges:
            g.rem(e, "2_roundabout")
        nodes = [n for n in nodes if n in g.nodes]
        hub = nodes[0]
        for n in nodes:
            g.nodes[n] = (c.x, c.y)
        for n in nodes[1:]:
            g.merge_nodes(hub, n)
        g.move_node(hub, (c.x, c.y))
        g.drop_node_if_free(hub)
        done += 1
    st["roundabouts_collapsed"] = done
    return g


def split_self_loops(g, P, st):
    """An edge starting and ending at the same node (a loop round a block hanging on one junction) survives the
    lollipop rule when it is longer than loop_max — and then Douglas-Peucker (simplify_edges) flattens the closed
    line into a spike there-and-back (3 vertices, zero area). Fixed here, after simplify_edges: the loop takes back
    its source shape; a loop up to loop_max goes (the lollipop rule), a longer one is split at its farthest vertex
    into two edges, each simplified on its own."""
    import shapely
    split = dropped = 0
    for e in [e for e, d in g.edges.items() if d["u"] == d["v"]]:
        d = g.edges[e]
        c = list(d["orig"] or d["coords"])
        u = d["u"]
        L = LineString(c).length
        if len(c) < 4 or L <= P["loop_max"]:
            g.rem(e, "2_loop"); g.drop_node_if_free(u); dropped += 1
            continue
        k = max(range(1, len(c) - 1), key=lambda i: math.dist(c[i], c[0]))
        m = g.new_node(c[k])
        parts = {kk: v / 2 for kk, v in d["parts"].items()}
        br = d["bridge"]
        g.del_edge(e)
        for seg, (a, b) in ((c[:k + 1], (u, m)), (c[k:], (m, u))):
            sm = list(shapely.simplify(LineString(seg), P["simplify_tol"]).coords) if len(seg) > 2 else seg
            ne = g.add_edge(a, b, sm, parts, br, True)
            g.edges[ne]["orig"] = seg
        split += 1
    st["self_loops_split"] = st.get("self_loops_split", 0) + split
    st["self_loops_dropped"] = st.get("self_loops_dropped", 0) + dropped


def seam_keys(pieces, quad):
    keys = set()
    for p in pieces:
        for k in (0, -1):
            xy = p["coords"][k]
            if Point(xy).distance(quad.exterior) < SEAM_EPS:
                keys.add((round(xy[0], 3), round(xy[1], 3)))
    return keys


def build_car(ring, W, quad, P, st):
    with atkis_city_set():
        lines = ss.load_lines("car", ring)
    st["source_lines_clipped"] = len(lines)
    lines = ss.snap_lines(lines, ss.SNAP_TOL, st)
    noded = ss.node_lines(lines)
    st["noded_pieces"] = len(noded)
    keys = seam_keys(noded, quad)
    st["seam_anchor_points"] = len(keys)
    g = ss.Graph.build(noded, W, keys)          # seam ends ride in car_nodes: never pruned / merged / moved
    st["input_edges"], st["input_nodes"] = len(g.edges), len(g.nodes)
    collapse_roundabouts(g, st)
    log("car: cleaning", len(g.edges), "edges")
    g = ss.clean_graph(g, P, W, st)
    g = ss.remove_islands_vs_car(g, st)
    g = ss.simplify_edges(g, P, st)
    split_self_loops(g, P, st)
    ss.prune_spurs(g, P, st); ss.dissolve_deg2(g, st)
    g = ss.remove_islands_vs_car(g, st)
    ss.dissolve_deg2(g, st)
    return g


# --------------------------------------------------------------------------- pedestrian source filter
def keep_zone(ring):
    log("built-up zone from building centroids")
    fc = json.load(open(DATA / "weimar_building_footprints.geojson", encoding="utf-8"))
    pts = []
    for f in fc["features"]:
        g = f.get("geometry")
        if g:
            pts.append(transform(TO_UTM, shape(g)).representative_point())
    builtup = unary_union([p.buffer(BUILTUP_BUFFER, quad_segs=4) for p in pts])
    fc = json.load(open(DATA / "osm_landcover.geojson", encoding="utf-8"))
    parks = [transform(TO_UTM, shape(f["geometry"])).buffer(0) for f in fc["features"]
             if f.get("geometry") and f["properties"].get("landuse") in PARK_LANDUSE]
    parks = unary_union(parks).buffer(10)
    gardens = unary_union([transform(TO_UTM, shape(f["geometry"])).buffer(0) for f in fc["features"]
                           if f.get("geometry") and f["properties"].get("landuse") in NON_PARK])
    # garden sheds are buildings, so allotments sit inside "built-up": cut them out explicitly
    zone = unary_union([builtup, parks]).difference(gardens.buffer(5))
    return zone.intersection(ring.buffer(1)), builtup.intersection(ring.buffer(1))


def cut_parallel(lines, refs_for, tol, angle, min_run, st, tag):
    """Cut out of each line the stretches that run alongside a reference line (within `tol`, direction within
    `angle` degrees, over at least `min_run` m); a remaining piece that now ends next to the reference starts from
    it — a short straight link onto its nearest point. `refs_for(i, line)` -> reference LineStrings for line i;
    refs_for=None: the reference is everything already output (lines are processed in the given order).
    Crossing a reference is not "alongside" (angle), so crossings are never cut."""
    grid = defaultdict(list)                     # refs_for=None: 50 m cells -> output pieces
    cell = lambda x, y: (int(x // 50), int(y // 50))

    def emit(d):
        out.append(d)
        if refs_for is None:
            g = LineString(d["coords"])
            x0, y0, x1, y1 = g.bounds
            for cx in range(int(x0 // 50), int(x1 // 50) + 1):
                for cy in range(int(y0 // 50), int(y1 // 50) + 1):
                    grid[(cx, cy)].append(g)

    def grid_refs(l):
        g = LineString(l["coords"])
        x0, y0, x1, y1 = g.buffer(tol).bounds
        seen = {}
        for cx in range(int(x0 // 50), int(x1 // 50) + 1):
            for cy in range(int(y0 // 50), int(y1 // 50) + 1):
                for r in grid.get((cx, cy), ()):
                    seen[id(r)] = r
        return list(seen.values())
    import shapely

    def direction(a, b):
        return math.degrees(math.atan2(b[1] - a[1], b[0] - a[0]))

    def ref_dir(cl, p):
        s = cl.project(p)
        a, b = cl.interpolate(max(0.0, s - 2.0)), cl.interpolate(min(cl.length, s + 2.0))
        return direction((a.x, a.y), (b.x, b.y))

    out, n_cut, km = [], 0, 0.0
    for li, l in enumerate(lines):
        refs = [] if l["bridge"] else (grid_refs(l) if refs_for is None else refs_for(li, l))
        if not refs:
            emit(l); continue
        rtree = STRtree(refs)
        c = list(shapely.segmentize(LineString(l["coords"]), 5.0).coords)
        par = []
        for a, b in zip(c, c[1:]):
            m = Point((a[0] + b[0]) / 2, (a[1] + b[1]) / 2)
            d = direction(a, b)
            hit = False
            for i in rtree.query(m.buffer(tol)):
                cl = refs[i]
                if cl.distance(m) > tol:
                    continue
                diff = abs((d - ref_dir(cl, m) + 180) % 360 - 180)
                if min(diff, 180 - diff) <= angle:
                    hit = True; break
            par.append(hit)
        # runs of parallel segments, only long ones count
        runs, s = [], None
        for i, h in enumerate(par + [False]):
            if h and s is None:
                s = i
            elif not h and s is not None:
                if sum(math.dist(c[j], c[j + 1]) for j in range(s, i)) >= min_run:
                    runs.append((s, i))
                s = None
        if not runs:
            emit(l); continue
        n_cut += 1
        km += sum(math.dist(c[j], c[j + 1]) for s_, e in runs for j in range(s_, e))
        cuts, prev = [], 0
        for s_, e in runs:
            cuts.append((prev, s_, prev > 0, True)); prev = e
        cuts.append((prev, len(c) - 1, True, False))
        for a, b, link_start, link_end in cuts:
            piece = c[a:b + 1]
            if len(piece) < 2 or LineString(piece).length < 15.0:
                continue
            for at, do in ((0, link_start and a > 0), (-1, link_end)):
                if not do:
                    continue
                p = Point(piece[at])
                cand = [refs[i] for i in rtree.query(p.buffer(tol + 5))]
                if not cand:
                    continue
                cl = min(cand, key=lambda g: g.distance(p))
                q = cl.interpolate(cl.project(p))
                if q.distance(p) > 0.01:
                    piece = [(q.x, q.y)] + piece if at == 0 else piece + [(q.x, q.y)]
            emit(dict(coords=list(shapely.simplify(LineString(piece), 0.5).coords), parts=l["parts"], bridge=False))
    st[f"{tag}_lines_cut"] = n_cut
    st[f"{tag}_km_removed"] = round(km / 1000, 1)
    return out


def remove_sidewalks(lines, car, st):
    """A pedestrian stretch running alongside a street duplicates the street for Space Syntax — the same rule her
    centre follows (paths only where there is no street)."""
    return cut_parallel(lines, lambda i, l: [car["lines"][j] for j in car["line_tree"].query(
        LineString(l["coords"]).buffer(PAR_TOL)) if not car["bridge"][j]], PAR_TOL, PAR_ANGLE, PAR_MIN_RUN, st,
        "sidewalk")


SOURCE_RANK = {"weimar_atkis_wegpfadsteig": 0, "weimar_atkis_fahrwegachse": 1, "weimar_atkis_platz": 2,
               "weimar_pedestrian_osm": 3}


def merge_duplicate_paths(lines, st):
    """The same path drawn twice — ATKIS and OSM, or two OSM ways side by side — a few metres apart: the graph
    cannot merge them (different nodes), so they stay as two near-identical lines. Lines are ranked (ATKIS path,
    ATKIS track, plaza outline, OSM; longer first); each line loses the stretches running alongside a higher-ranked
    one (DUP_TOL, DUP_ANGLE, DUP_MIN_RUN) and its remaining pieces start from that line."""
    rank = lambda l: (SOURCE_RANK.get(next(iter(l["parts"]))[1], 9), -LineString(l["coords"]).length)
    return cut_parallel(sorted(lines, key=rank), None,
                        DUP_TOL, DUP_ANGLE, DUP_MIN_RUN, st, "duplicate_path")


def filter_field_forest(lines, zone, st):
    """Split pedestrian source lines into in-zone (built-up / park) and open-land (field / forest) ones."""
    from shapely.prepared import prep
    pz = prep(zone)
    kept, open_land, cnt = [], [], Counter()
    for l in lines:
        ln = LineString(l["coords"])
        cls = next(iter(l["parts"]))[0]
        if pz.contains(ln):
            kept.append(l); continue
        inside = ln.intersection(zone).length if pz.intersects(ln) else 0.0
        if inside >= KEEP_SHARE * ln.length:
            kept.append(l)
        else:
            open_land.append(l); cnt[cls] += 1
    st["field_forest_lines"] = dict(cnt.most_common())
    st["field_forest_lines_total"] = len(open_land)
    return kept, open_land


def select_connectors(urban, open_land, car, builtup, st):
    """Field / forest paths are kept only where they carry a main walking direction: for every pair of settlements
    (connected parts of the built-up zone) at most CONNECT_MAX_DIST apart, the shortest walk between them is found
    on paths (built-up + field/forest) plus streets (streets cost CAR_WEIGHT x their length, so a path is preferred
    where one exists and a street only bridges a gap); field/forest pieces on any such route are kept, the rest
    (field grid, dead-end tracks) is not."""
    tag = lambda l, kind: dict(coords=list(l["coords"]), bridge=l["bridge"],
                               parts={(f"{kind}|{k[0]}", k[1], k[2]): v for k, v in l["parts"].items()})
    ped = [tag(l, "U") for l in urban] + [tag(l, "O") for l in open_land]
    ped = ss.snap_ends_to_car(ped, car, {}, set())
    ped = ss.snap_lines(ped, ss.SNAP_TOL, {})
    cars = [dict(coords=list(g.coords), bridge=b, parts={("C|car", "car", False): g.length})
            for g, b in zip(car["lines"], car["bridge"])]
    pieces = ss.node_lines(ped + cars)
    key = lambda xy: (round(xy[0], 2), round(xy[1], 2))
    G = nx.Graph()
    for i, pc in enumerate(pieces):
        kind = next(iter(pc["parts"]))[0].split("|", 1)[0]
        L = LineString(pc["coords"]).length
        w = L * (CAR_WEIGHT if kind == "C" else 1.0)
        u, v = key(pc["coords"][0]), key(pc["coords"][-1])
        if u == v:
            continue
        if not G.has_edge(u, v) or G[u][v]["w"] > w:
            G.add_edge(u, v, w=w, i=i)
    comps = [c for c in getattr(builtup, "geoms", [builtup]) if c.area > 0]
    from shapely.prepared import prep
    anchors = []
    nodes = list(G.nodes)
    ntree = STRtree([Point(n) for n in nodes])
    for c in comps:
        pc_ = prep(c)
        anchors.append([nodes[j] for j in ntree.query(c) if pc_.contains(Point(nodes[j]))])
    used = set()
    pairs = 0
    for a, ca in enumerate(comps):
        if not anchors[a]:
            continue
        for n in anchors[a]:
            G.add_edge("SRC", n, w=0)
        dist, path = nx.single_source_dijkstra(G, "SRC", cutoff=CONNECT_MAX_DIST * CAR_WEIGHT * 2, weight="w")
        G.remove_node("SRC")
        for b in range(a + 1, len(comps)):
            if not anchors[b] or ca.distance(comps[b]) > CONNECT_MAX_DIST:
                continue
            reach = [n for n in anchors[b] if n in dist]
            if not reach:
                continue
            tgt = min(reach, key=dist.get)
            p = path[tgt][1:]                      # drop the virtual source
            for u, v in zip(p, p[1:]):
                used.add(G[u][v]["i"])
            pairs += 1
    keep = []
    for i in used:
        pc = pieces[i]
        k0 = next(iter(pc["parts"]))[0]
        if k0.startswith("O|"):
            keep.append(dict(coords=pc["coords"], bridge=pc["bridge"],
                             parts={(k[0].split("|", 1)[1], k[1], k[2]): v for k, v in pc["parts"].items()}))
    st["settlements"] = sum(1 for x in anchors if x)
    st["settlement_pairs_routed"] = pairs
    st["field_forest_pieces_kept"] = len(keep)
    st["field_forest_km_kept"] = round(sum(LineString(k["coords"]).length for k in keep) / 1000, 1)
    st["field_forest_km_total"] = round(sum(LineString(l["coords"]).length for l in open_land) / 1000, 1)
    return keep


def car_reference(car_feats):
    lines = [f["geom"] for f in car_feats]
    bridge = [bool(f["props"]["bridge"]) for f in car_feats]
    nodeset = set()
    for g in lines:
        for c in (g.coords[0], g.coords[-1]):
            nodeset.add((round(c[0], 3), round(c[1], 3)))
    nodes = [Point(*n) for n in nodeset]
    return dict(lines=lines, bridge=bridge, nodes=nodes, node_tree=STRtree(nodes), line_tree=STRtree(lines))


def build_pedestrian(ring, W, quad, P, car, zone, builtup, st):
    with atkis_city_set():
        lines = ss.load_lines("pedestrian", ring)
    st["source_lines_clipped"] = len(lines)
    log("pedestrian: sidewalks and duplicate paths")
    lines = remove_sidewalks(lines, car, st)
    lines = merge_duplicate_paths(lines, st)
    all_sources = list(lines)                  # for growing dead ends later
    lines, open_land = filter_field_forest(lines, zone, st)
    log("pedestrian: routing between settlements over", len(open_land), "field/forest lines")
    lines = lines + select_connectors(lines, open_land, car, builtup, st)
    st["source_lines_kept"] = len(lines)
    with atkis_city_set():
        lines = ss.plaza_perimeter_lines(lines, ring, st)
    # plaza outlines are added after the cleaning above: they get the same two passes
    lines = merge_duplicate_paths(remove_sidewalks(lines, car, st.setdefault("plaza_pass", {})), st["plaza_pass"])
    touch = set()
    lines = ss.snap_ends_to_car(lines, car, st, touch)
    lines = ss.node_at_car_crossings(lines, car, st, touch)
    lines = ss.snap_lines(lines, ss.SNAP_TOL, st)
    noded = ss.node_lines(lines)
    st["noded_pieces"] = len(noded)
    keys = {(round(p.x, 3), round(p.y, 3)) for p in car["nodes"]} | touch | seam_keys(noded, quad)
    g = ss.Graph.build(noded, W, keys)
    st["input_edges"], st["input_nodes"] = len(g.edges), len(g.nodes)
    log("pedestrian: cleaning", len(g.edges), "edges")
    g = ss.clean_graph(g, P, W, st)
    g = ss.remove_islands_vs_car(g, st)
    g = ss.simplify_edges(g, P, st)
    split_self_loops(g, P, st)
    ss.prune_spurs(g, P, st); ss.dissolve_deg2(g, st)
    g = ss.remove_islands_vs_car(g, st)
    ss.dissolve_deg2(g, st)
    return g, all_sources


# --------------------------------------------------------------------------- seam
def free_end_degree(feats):
    deg = Counter()
    for f in feats:
        c = f["geom"].coords
        deg[c[0]] += 1; deg[c[-1]] += 1
    return deg


def stitch(feats, manual_ends, fallback_ends, quad, P, others, network, review, st, manual_vertices=None,
           extend=None):
    """Move each generated free end on the seam exactly onto her nearest edge end (one-to-one, globally nearest
    first). `fallback_ends` (pedestrian: her car edge ends), then `manual_vertices` (any vertex of her lines near
    the edge, shared) are tried for the ends that found no partner; what is still left is continued into the centre
    along the source lines (`extend`, see make_extender)."""
    deg = free_end_degree(feats)
    auto_ends = [(i, k) for i, f in enumerate(feats) for k in (0, -1)
                 if deg[f["geom"].coords[k]] == 1 and Point(f["geom"].coords[k]).distance(quad.exterior) < SEAM_EPS]
    st["seam_auto_ends"] = len(auto_ends)
    st["seam_manual_ends"] = len(manual_ends)

    def crosses(new_seg, xy, self_geom):
        cand = [f["geom"] for f in feats] + list(others)
        for og in cand:
            if og is self_geom or not new_seg.intersects(og):
                continue
            x = new_seg.intersection(og)
            pts = [x] if x.geom_type == "Point" else (list(x.geoms) if x.geom_type == "MultiPoint" else None)
            if pts is None or any(q.distance(Point(xy)) > 0.02 and q.distance(Point(new_seg.coords[0])) > 0.02
                                  for q in pts):
                return True
        return False

    def match(ends, targets, tag, unique=True):
        pairs = []
        for a, (i, k) in enumerate(ends):
            p = Point(feats[i]["geom"].coords[k])
            for b, t in enumerate(targets):
                d = p.distance(Point(t["xy"]))
                if d <= SEAM_TOL:
                    pairs.append((d, a, b))
        used_a, used_b, done = set(), set(), []
        for d, a, b in sorted(pairs):
            if a in used_a or b in used_b:
                continue
            i, k = ends[a]
            t = targets[b]
            c = list(feats[i]["geom"].coords)
            other = c[1] if k == 0 else c[-2]
            new_seg = LineString([other, t["xy"]])
            if d > 0.01 and crosses(new_seg, t["xy"], feats[i]["geom"]):
                review.append(dict(xy=t["xy"], kind=f"{network}: seam join rejected (would cross)", dist=round(d, 1)))
                continue
            c[k] = t["xy"]
            feats[i]["geom"] = LineString(c)
            PIN[t["xy"]] = t["wgs"]
            used_a.add(a); done.append(d)
            if unique:
                used_b.add(b)
            if d > SEAM_REVIEW_MOVE:
                review.append(dict(xy=t["xy"], kind=f"{network}: seam end moved > {SEAM_REVIEW_MOVE} m", dist=round(d, 1)))
        st[f"seam_joined_{tag}"] = len(done)
        st[f"seam_joined_{tag}_max_move_m"] = round(max(done), 2) if done else 0
        return [e for a, e in enumerate(ends) if a not in used_a], [t for b, t in enumerate(targets) if b not in used_b]

    rest, manual_left = match(auto_ends, manual_ends, "to_manual")
    if fallback_ends:
        rest, _ = match(rest, fallback_ends, "to_manual_car")
    if manual_vertices:
        # her pedestrian network inside is sparse: a path reaching the edge where she drew nothing joins the
        # nearest vertex of her lines (car or pedestrian) instead of stopping dead at the border
        rest, _ = match(rest, manual_vertices, "to_manual_vertex", unique=False)
    if extend:
        rest = extend(feats, rest)
    # generated ends that found no partner: short dead stubs go, longer ones stay and are reported
    drop = set()
    deg = free_end_degree(feats)
    for i, k in rest:
        f = feats[i]
        far = f["geom"].coords[-1 if k == 0 else 0]
        if f["geom"].length < P["spur_len"] and deg[far] >= 3:
            drop.add(i)
            review.append(dict(xy=f["geom"].coords[k], kind=f"{network}: generated stub dropped (no partner in centre)",
                               dist=round(f["geom"].length, 1)))
        else:
            review.append(dict(xy=f["geom"].coords[k], kind=f"{network}: generated end without partner in centre",
                               dist=None))
    for t in manual_left:
        review.append(dict(xy=t["xy"], kind=f"{network}: her edge end without continuation outside", dist=None))
    st["seam_generated_stubs_dropped"] = len(drop)
    st["seam_generated_ends_unmatched_kept"] = len(rest) - len(drop)
    st["seam_manual_ends_unmatched"] = len(manual_left)
    return [f for i, f in enumerate(feats) if i not in drop]


def splice_manual(m, xy):
    """Add the vertex xy (lying on her line) to her line m — her shape is unchanged, only a vertex is added so that
    a generated line can end on it. Returns her exact WGS84 coordinate for xy."""
    c = m["utm"]
    if xy in c:
        return tuple(m["f"]["geometry"]["coordinates"][c.index(xy)])
    ln = LineString(c)
    s = ln.project(Point(xy))
    acc = 0.0
    for idx in range(len(c) - 1):
        seg = math.dist(c[idx], c[idx + 1])
        if acc + seg >= s - 1e-9:
            break
        acc += seg
    w = tuple(FROM_UTM(*xy))
    c.insert(idx + 1, xy)
    m["f"]["geometry"]["coordinates"].insert(idx + 1, list(w))
    m["spliced"] = m.get("spliced", 0) + 1
    return w


def make_extender(network, quad, targets, P, review, st):
    """Unmatched generated seam end -> follow the source lines (ATKIS / OSM, same network) into the centre along
    the shortest way until her network (`targets`: her lines of this network, + her car for pedestrian) is within
    EXT_JOIN_TOL, at most EXT_MAX. The end point lands exactly on her line (her vertex if one is near, otherwise a
    vertex is added to her line). The added stretch is simplified with the preset's Douglas-Peucker tolerance."""
    import shapely
    with atkis_city_set():
        src = ss.load_lines(network, quad)
    her_lines = [LineString(m["utm"]) for m in targets]
    her_tree = STRtree(her_lines)
    marks = [dict(coords=list(m["utm"]), bridge=False, parts={("HER", "her", False): 1.0}) for m in targets]
    pieces = [p for p in ss.node_lines(src + marks) if next(iter(p["parts"]))[0] != "HER"]
    G = nx.Graph()
    for i, pc in enumerate(pieces):
        u, v = pc["coords"][0], pc["coords"][-1]
        if u != v:
            G.add_edge(u, v, w=LineString(pc["coords"]).length, i=i)
    nodes = list(G.nodes)
    ntree = STRtree([Point(n) for n in nodes])
    near_her = {n for n in nodes if any(her_lines[j].distance(Point(n)) <= EXT_JOIN_TOL
                                        for j in her_tree.query(Point(n).buffer(EXT_JOIN_TOL)))}
    def extend(feats, rest):
        left, exts, taken, pins = [], [], set(), set()
        for i, k in rest:
            E = feats[i]["geom"].coords[k]
            j = ntree.query_nearest(Point(E), max_distance=1.0)
            if not len(j):
                left.append((i, k)); st["seam_no_source_inside"] = st.get("seam_no_source_inside", 0) + 1
                continue
            start = nodes[int(j[0])]
            dist, path = nx.single_source_dijkstra(G, start, cutoff=EXT_MAX, weight="w")
            # stop at her network, or at an extension laid before (two ends following the same source path)
            reach = [n for n in dist if n in near_her or (n in taken and n != start)]
            if not reach:
                left.append((i, k)); st["seam_source_never_reaches_her"] = st.get("seam_source_never_reaches_her", 0) + 1
                continue
            tgt = min(reach, key=dist.get)
            ext = [E]
            for u, v in zip(path[tgt], path[tgt][1:]):
                pc = pieces[G[u][v]["i"]]["coords"]
                seg = pc if pc[0] == u else list(reversed(pc))
                ext += seg[1:]
            if tgt in taken:
                T = tgt
            else:
                # land exactly on her line: her nearest line, her vertex if within EXT_JOIN_TOL, else a new vertex
                p = Point(ext[-1])
                jj = min(her_tree.query(p.buffer(EXT_JOIN_TOL + 0.01)), key=lambda q: her_lines[q].distance(p))
                m, hl = targets[jj], her_lines[jj]
                q = hl.interpolate(hl.project(p))
                vx = min(m["utm"], key=lambda v: math.dist(v, (q.x, q.y)))
                T = vx if math.dist(vx, (q.x, q.y)) <= EXT_JOIN_TOL else (q.x, q.y)
                PIN[T] = splice_manual(m, T)
                ext = ext[:-1] + [T] if len(ext) > 1 else ext + [T]
            # inner path nodes only: the start is replaced by E and a her-line end by T, so they are not vertices
            taken |= set(path[tgt][1:-1])
            pins.add(T)
            exts.append((i, k, ext))
        # simplify each extension between pinned points (joins of later extensions must stay vertices)
        outs = []
        for i, k, ext in exts:
            cut = [0] + [n for n in range(1, len(ext) - 1) if ext[n] in pins] + [len(ext) - 1]
            out = [ext[0]]
            for a, b in zip(cut, cut[1:]):
                part = ext[a:b + 1]
                s = list(shapely.simplify(LineString(part), P["simplify_tol"]).coords) if len(part) > 2 else part
                s[0], s[-1] = part[0], part[-1]
                out += s[1:]
            outs.append(out)
        # two extensions along nearly the same path can cross once simplified: those keep their source shape
        # (the source lines are noded, so raw paths only meet at shared vertices)
        lines_ = [LineString(o) for o in outs]
        for a in range(len(outs)):
            for b in range(a + 1, len(outs)):
                x = lines_[a].intersection(lines_[b])
                pts = [x] if x.geom_type == "Point" else (list(x.geoms) if x.geom_type == "MultiPoint" else None)
                if x.is_empty or (pts is not None and all((q.x, q.y) in pins or (q.x, q.y) in (outs[a][0], outs[b][0])
                                                          for q in pts)):
                    continue
                outs[a], outs[b] = exts[a][2], exts[b][2]
                st["extensions_kept_unsimplified"] = st.get("extensions_kept_unsimplified", 0) + 2
        done = []
        for (i, k, _ext), out in zip(exts, outs):
            c = list(feats[i]["geom"].coords)
            c = c + out[1:] if k == -1 else list(reversed(out))[:-1] + c
            feats[i]["geom"] = LineString(c)
            L = LineString(out).length
            done.append(L)
            review.append(dict(xy=out[-1], kind=f"{network}: extended into the centre to her line", dist=round(L, 1)))
        st["seam_extended_into_centre"] = len(done)
        st["seam_extended_km"] = round(sum(done) / 1000, 2)
        return left
    return extend


def node_extensions_on_manual(feats, manual, st):
    """An extension may cross one of her lines on its way (car crossing her pedestrian path): at-grade crossing ->
    the same vertex in both (added to her line, shape unchanged)."""
    her = [(m, LineString(m["utm"])) for m in manual if not m["f"]["properties"].get("bridge")]
    tree = STRtree([h[1] for h in her])
    n = 0
    for f in feats:
        if f["props"]["bridge"]:
            continue
        ln = f["geom"]
        for j in tree.query(ln):
            m, hl = her[j]
            hl = LineString(m["utm"])
            x = ln.intersection(hl)
            pts = [x] if x.geom_type == "Point" else (list(x.geoms) if x.geom_type == "MultiPoint" else [])
            for q in pts:
                xy = (q.x, q.y)
                on_her = min(m["utm"], key=lambda v: math.dist(v, xy))
                if math.dist(on_her, xy) < 0.02 and any(math.dist(on_her, v) < 0.02 for v in ln.coords):
                    continue
                if math.dist(on_her, xy) < 0.02:
                    xy = on_her
                PIN[xy] = splice_manual(m, xy)
                c = list(ln.coords)
                if not any(math.dist(xy, v) < 0.02 for v in c):
                    s = ln.project(Point(xy)); acc = 0.0
                    for idx in range(len(c) - 1):
                        seg = math.dist(c[idx], c[idx + 1])
                        if acc + seg >= s - 1e-9:
                            break
                        acc += seg
                    c.insert(idx + 1, xy)
                else:
                    c = [xy if math.dist(xy, v) < 0.02 else v for v in c]
                ln = LineString(c); n += 1
        f["geom"] = ln
    st["extension_crossings_noded_on_her_lines"] = n


class Targets:
    """Lines a growing end may land on: generated features (UTM geom) and her lines (utm + exact WGS84). Landing
    between vertices adds a vertex there (her shape unchanged)."""
    def __init__(self, car_feats, ped_feats, man_all):
        self.items = [("auto", f) for f in car_feats + ped_feats] + [("her", m) for m in man_all]
        self.geoms = [f["geom"] if k == "auto" else LineString(f["utm"]) for k, f in self.items]
        self.tree = STRtree(self.geoms)

    def nearest(self, p, tol, exclude=None):
        cand = [j for j in self.tree.query(p.buffer(tol)) if self.items[j][1] is not exclude
                and self.geoms[j].distance(p) <= tol]
        return min(cand, key=lambda j: self.geoms[j].distance(p)) if cand else None

    def land(self, j, p, snap=1.0):
        """Exact point on target j nearest to p (its vertex if within `snap`), made a vertex of j."""
        kind, f = self.items[j]
        g = self.geoms[j]
        q = g.interpolate(g.project(p))
        c = list(f["geom"].coords) if kind == "auto" else f["utm"]
        vx = min(c, key=lambda v: math.dist(v, (q.x, q.y)))
        T = vx if math.dist(vx, (q.x, q.y)) <= snap else (q.x, q.y)
        if kind == "her":
            PIN[T] = splice_manual(f, T)
        elif T not in c:
            s = g.project(Point(T)); acc = 0.0
            for idx in range(len(c) - 1):
                seg = math.dist(c[idx], c[idx + 1])
                if acc + seg >= s - 1e-9:
                    break
                acc += seg
            c.insert(idx + 1, T)
            f["geom"] = LineString(c)
        return T


def free_ends(feats, all_geoms, city, quad):
    """(i, k) ends of feats that touch no other line (not even between its vertices), off the city boundary and the
    seam. `all_geoms` = every line of the network, feats' own geometries included."""
    edge = city.exterior if city.geom_type == "Polygon" else city.boundary
    tree = STRtree(all_geoms)
    out = []
    for i, f in enumerate(feats):
        c = f["geom"].coords
        for k in (0, -1):
            p = Point(c[k])
            if edge.distance(p) <= 2.0 or quad.exterior.distance(p) <= SEAM_EPS:
                continue
            touching = [j for j in tree.query(p.buffer(0.01)) if all_geoms[j].distance(p) < 0.01]
            # own line touches its own end; any other line (or the own line looping back) attaches it
            if len(touching) <= 1:
                out.append((i, k))
    return out


def close_car_gaps(car_feats, man_car, man_ped, city, quad, review, st):
    """A street axis ending a few metres short of another street (ATKIS gap / undershoot): joined by a straight
    link when the link is short (<= CAR_GAP), roughly continues the street's own direction (<= 45 deg) and crosses
    nothing on its way."""
    tg = Targets(car_feats, [], man_car)
    others = [f["geom"] for f in car_feats] + [LineString(m["utm"]) for m in man_car + man_ped]
    ends = free_ends(car_feats, [f["geom"] for f in car_feats] + [LineString(m["utm"]) for m in man_car], city, quad)
    n = 0
    for i, k in ends:
        c = list(car_feats[i]["geom"].coords)
        p, prev = Point(c[k]), (c[1] if k == 0 else c[-2])
        j = tg.nearest(p, CAR_GAP, exclude=car_feats[i])
        if j is None:
            continue
        g = tg.geoms[j]
        q = g.interpolate(g.project(p))
        if q.distance(p) < 0.01:
            continue
        d1 = math.atan2(p.y - prev[1], p.x - prev[0]); d2 = math.atan2(q.y - p.y, q.x - p.x)
        if q.distance(p) > CAR_GAP_ANY and abs((math.degrees(d1 - d2) + 180) % 360 - 180) > 45:
            continue
        link = LineString([c[k], (q.x, q.y)])
        if any(link.crosses(o) for o in others if o is not car_feats[i]["geom"] and o is not g):
            continue
        T = tg.land(j, q)
        c = [T] + c if k == 0 else c + [T]
        car_feats[i]["geom"] = LineString(c)
        n += 1
        review.append(dict(xy=T, kind="car: gap to the next street closed", dist=round(link.length, 1)))
    st["car_gaps_closed"] = n


def grow_dead_ends(ped_feats, car_feats, man_all, sources, city, quad, P, review, st):
    """A generated pedestrian line that stops short — its continuation was a field/forest path not on a settlement
    route, or the cleaning cut it — is grown along the source lines (all pedestrian sources, sidewalks and
    duplicates already removed) by the shortest way to the nearest line of the network (car, pedestrian, hers), at
    most GROW_MAX. Lands exactly on that line; later growths may stop on earlier ones. Simplified like the rest."""
    import shapely
    tg = Targets(car_feats, ped_feats, man_all)
    marks = [dict(coords=list(g.coords), bridge=False, parts={("NET", "net", False): 1.0}) for g in tg.geoms]
    pieces = [p for p in ss.node_lines([dict(l, coords=list(l["coords"])) for l in sources] + marks)
              if next(iter(p["parts"]))[0] != "NET"]
    G = nx.Graph()
    for i, pc in enumerate(pieces):
        u, v = pc["coords"][0], pc["coords"][-1]
        if u != v:
            G.add_edge(u, v, w=LineString(pc["coords"]).length, i=i)
    nodes = list(G.nodes)
    ntree = STRtree([Point(n) for n in nodes])
    on_net = {}
    for n in nodes:
        j = tg.nearest(Point(n), 0.05)
        if j is not None:
            on_net[n] = j
    ends = free_ends(ped_feats, list(tg.geoms), city, quad)
    taken, pins, exts, failed = set(), set(), [], []
    for i, k in ends:
        own = ped_feats[i]["geom"]
        E = own.coords[k]
        other_end = own.coords[-1 if k == 0 else 0]
        # growing back to the line's own start would only close it into a loop round to itself
        not_home = lambda xy: math.dist(xy, other_end) > 1.0
        # enter the source graph at a nearby source node (the line was simplified: up to a few metres off)
        prev = own.coords[1] if k == 0 else own.coords[-2]
        # only entries ahead of the end (not back along its own line)
        ahead = lambda n: math.dist(n, E) < 1.0 or ((E[0] - prev[0]) * (n[0] - E[0]) + (E[1] - prev[1]) * (n[1] - E[1])) > 0
        # a line of the network right there (<= PED_END_SNAP): join it straight
        jn = tg.nearest(Point(E), PED_END_SNAP, exclude=ped_feats[i])
        if jn is not None:
            q = tg.geoms[jn].interpolate(tg.geoms[jn].project(Point(E)))
            link = LineString([E, (q.x, q.y)])
            if not_home((q.x, q.y)) and not any(link.crosses(tg.geoms[j]) for j in tg.tree.query(link) if j != jn):
                T = tg.land(jn, Point(E), snap=0.5)
                exts.append((i, k, [E, T])); pins.add(T)
                continue
        # the straight entry step onto the source lines must not cross the network either
        crosses_net = lambda n: any(LineString([E, n]).crosses(tg.geoms[j]) for j in tg.tree.query(LineString([E, n])))
        near = [nodes[j] for j in ntree.query(Point(E).buffer(GROW_ENTRY)) if ahead(nodes[j])
                and (nodes[j] == E or not crosses_net(nodes[j]))]
        if not near:
            failed.append((i, k)); continue
        G.add_node("E")
        for n in near:
            G.add_edge("E", n, w=math.dist(E, n), i=None)
        # never walk back along the own line (source nodes within 3 m of it, except the entries, are closed)
        own_nodes = [nodes[j] for j in ntree.query(own.buffer(3.0)) if nodes[j] not in near]
        dist, path = nx.single_source_dijkstra(nx.restricted_view(G, own_nodes, []), "E", cutoff=GROW_MAX,
                                               weight="w")
        G.remove_node("E")
        reach = [n for n in dist if n != "E" and not_home(n)
                 and ((n in on_net and tg.items[on_net[n]][1] is not ped_feats[i]) or n in taken)]
        if not reach:
            failed.append((i, k)); continue
        tgt = min(reach, key=dist.get)
        pth = path[tgt][1:]
        ext = [E, pth[0]] if pth[0] != E else [E]
        for u, v in zip(pth, pth[1:]):
            pc = pieces[G[u][v]["i"]]["coords"]
            seg = pc if pc[0] == u else list(reversed(pc))
            ext += seg[1:]
        if tgt in taken:
            T = tgt
        else:
            T = tg.land(on_net[tgt], Point(tgt), snap=0.5)
            ext[-1] = T
        taken |= set(pth[:-1])
        pins.add(T)
        exts.append((i, k, ext))
    # simplify between pinned points; keep the source shape where a simplified growth would cross anything
    net_geoms = [f["geom"] for f in car_feats + ped_feats] + [LineString(m["utm"]) for m in man_all]
    net_tree = STRtree(net_geoms)
    outs = []
    for i, k, ext in exts:
        cut = [0] + [n for n in range(1, len(ext) - 1) if ext[n] in pins] + [len(ext) - 1]
        out = [ext[0]]
        for a, b in zip(cut, cut[1:]):
            part = ext[a:b + 1]
            s = list(shapely.simplify(LineString(part), P["simplify_tol"]).coords) if len(part) > 2 else part
            s[0], s[-1] = part[0], part[-1]
            out += s[1:]
        ln = LineString(out)
        outs.append(ext if any(ln.crosses(net_geoms[j]) for j in net_tree.query(ln)) else out)
    # a straight join (2 points) checked only against the network as it was: dropped if it crosses another growth
    lines_ = [LineString(o) for o in outs]
    t_ = STRtree(lines_)
    gone = {a for a in range(len(outs)) if len(exts[a][2]) == 2
            and any(b != a and lines_[a].crosses(lines_[b]) for b in t_.query(lines_[a]))}
    exts = [e for a, e in enumerate(exts) if a not in gone]
    outs = [o for a, o in enumerate(outs) if a not in gone]
    # growths among themselves: revert both of a crossing pair to the (noded) source shape, until none cross
    for _ in range(5):
        lines_ = [LineString(o) for o in outs]
        t_ = STRtree(lines_)
        bad = {a for a in range(len(outs)) for b in t_.query(lines_[a]) if b != a and lines_[a].crosses(lines_[b])}
        bad = {a for a in bad if outs[a] is not exts[a][2]}
        if not bad:
            break
        for a in bad:
            outs[a] = exts[a][2]
    done = []
    for (i, k, ext), out in zip(exts, outs):
        c = list(ped_feats[i]["geom"].coords)
        c = c + out[1:] if k == -1 else list(reversed(out))[:-1] + c
        ped_feats[i]["geom"] = LineString(c)
        done.append(LineString(out).length)
        review.append(dict(xy=out[-1], kind="pedestrian: dead end grown to the network", dist=round(done[-1], 1)))
    st["dead_ends_found"] = len(ends)
    st["dead_ends_grown"] = len(done)
    st["dead_ends_grown_km"] = round(sum(done) / 1000, 2)


def remove_redundant_detours(car_feats, ped_feats, man_car, review, st):
    """A pedestrian line whose both ends are on streets and which runs next to the streets between those ends (a
    rear lane parallel to the street, a path round the corner) adds nothing to the network: the street way is
    hardly longer. Removed when the street way <= DETOUR_RATIO x its length (+ 10 m) and stays within DETOUR_NEAR
    of it. Paths through blocks that really cut a corner are longer-ratio and stay."""
    G = nx.Graph()
    for c in [list(f["geom"].coords) for f in car_feats] + [m["utm"] for m in man_car]:
        for u, v in zip(c, c[1:]):
            G.add_edge(u, v, w=math.dist(u, v))
    drop = set()
    for i, f in enumerate(ped_feats):
        c = f["geom"].coords
        a, b = c[0], c[-1]
        if a == b or a not in G or b not in G:
            continue
        L = f["geom"].length
        try:
            d, path = nx.single_source_dijkstra(G, a, target=b, cutoff=DETOUR_RATIO * L + 10, weight="w")
        except nx.NetworkXNoPath:
            continue
        if len(path) < 2 or LineString(path).hausdorff_distance(f["geom"]) > DETOUR_NEAR:
            continue
        drop.add(i)
        review.append(dict(xy=f["geom"].interpolate(0.5, normalized=True).coords[0],
                           kind="pedestrian: redundant next to the streets, removed", dist=round(L, 1)))
    st["redundant_detours_removed"] = len(drop)
    return [f for i, f in enumerate(ped_feats) if i not in drop]


def parallel_share(g, others, tol, angle):
    """Share of g's length running within tol of one of `others` and within `angle` degrees of its direction."""
    import shapely
    c = list(shapely.segmentize(g, 5.0).coords)
    tot = hit = 0.0
    for a, b in zip(c, c[1:]):
        L = math.dist(a, b); tot += L
        m = Point((a[0] + b[0]) / 2, (a[1] + b[1]) / 2)
        d = math.degrees(math.atan2(b[1] - a[1], b[0] - a[0]))
        for o in others:
            if o.distance(m) > tol:
                continue
            s = o.project(m)
            p1, p2 = o.interpolate(max(0.0, s - 2.0)), o.interpolate(min(o.length, s + 2.0))
            df = abs((d - math.degrees(math.atan2(p2.y - p1.y, p2.x - p1.x)) + 180) % 360 - 180)
            if min(df, 180 - df) <= angle:
                hit += L; break
    return hit / tot if tot else 0.0


def remove_parallel_twins(car_feats, ped_feats, man_all, review, st):
    """Final pass on the finished network: two paths side by side (double track, both sides of an alley, two
    nearly identical lines brought together by straightening) — the shorter one goes when it runs alongside another
    pedestrian line over TWIN_SHARE of its length AND its two ends stay connected without it (union network, way
    <= TWIN_DETOUR x its length + 50 m), so nothing gets cut off."""
    G = nx.Graph()
    def add(c, key):
        for u, v in zip(c, c[1:]):
            G.add_edge(u, v, w=math.dist(u, v), k=key)
    for f in car_feats:
        add(list(f["geom"].coords), None)
    for m in man_all:
        add(m["utm"], None)
    for i, f in enumerate(ped_feats):
        add(list(f["geom"].coords), i)
    geoms = [f["geom"] for f in ped_feats]
    tree = STRtree(geoms)
    drop = set()
    for i in sorted(range(len(ped_feats)), key=lambda i: geoms[i].length):
        g = geoms[i]
        if ped_feats[i]["props"]["bridge"] or g.length < 20:
            continue
        others = [geoms[j] for j in tree.query(g.buffer(DUP_TOL)) if j != i and j not in drop]
        if not others or parallel_share(g, others, DUP_TOL, DUP_ANGLE) < TWIN_SHARE:
            continue
        c = list(g.coords)
        edges = [(u, v) for u, v in zip(c, c[1:]) if G.has_edge(u, v) and G[u][v]["k"] == i]
        saved = [(u, v, dict(G[u][v])) for u, v in edges]
        G.remove_edges_from(edges)
        try:
            d = nx.dijkstra_path_length(G, c[0], c[-1], weight="w")
        except (nx.NetworkXNoPath, nx.NodeNotFound):
            d = math.inf
        if d <= TWIN_DETOUR * g.length + 50:
            drop.add(i)
            review.append(dict(xy=g.interpolate(0.5, normalized=True).coords[0], kind="pedestrian: parallel twin removed",
                               dist=round(g.length, 1)))
        else:
            for u, v, a in saved:
                G.add_edge(u, v, **a)
    st["parallel_twins_removed"] = len(drop)
    return [f for i, f in enumerate(ped_feats) if i not in drop]


def join_car_ends_to_paths(car_feats, ped_feats, man_ped, city, quad, review, st):
    """A street ending right at a path (the path continues it) but not on it: the street end lands on the path."""
    tg = Targets([], ped_feats, man_ped)
    ends = free_ends(car_feats, [f["geom"] for f in car_feats + ped_feats] + [LineString(m["utm"]) for m in man_ped],
                     city, quad)
    n = 0
    for i, k in ends:
        c = list(car_feats[i]["geom"].coords)
        p = Point(c[k])
        j = tg.nearest(p, CAR_TO_PED_GAP)
        if j is None:
            continue
        T = tg.land(j, p, snap=0.5)
        if T != c[k]:
            c = [T] + c if k == 0 else c + [T]
            car_feats[i]["geom"] = LineString(c)
        n += 1
        review.append(dict(xy=T, kind="car: street end joined onto the path continuing it", dist=round(p.distance(Point(T)), 1)))
    st["car_ends_joined_to_paths"] = n


def weld_ends(car_feats, ped_feats, man_all, st, tol=0.05):
    """Last topology pass: a generated line end lying within `tol` of another line (mm-gaps left by rounding,
    projections, simplification) is put exactly on it — onto its vertex if one is that close, otherwise a vertex
    is added there (to her line too: shape unchanged). Visually nothing changes; the network gets connected."""
    tg = Targets(car_feats, ped_feats, man_all)
    n = 0
    for f in car_feats + ped_feats:
        for k in (0, -1):
            c = list(f["geom"].coords)
            p = Point(c[k])
            cand = [j for j in tg.tree.query(p.buffer(tol)) if tg.items[j][1] is not f]
            # current geometry of the candidate (vertices may have been added since the index was built)
            cur = lambda j: tg.items[j][1]["geom"] if tg.items[j][0] == "auto" else LineString(tg.items[j][1]["utm"])
            cand = [j for j in cand if cur(j).distance(p) <= tol]
            if not cand:
                continue
            j = min(cand, key=lambda j: cur(j).distance(p))
            verts = list(cur(j).coords)
            if c[k] in verts:
                continue
            tg.geoms[j] = cur(j)
            T = tg.land(j, p, snap=tol)
            c[k] = T
            f["geom"] = LineString(c)
            n += 1
    st["mm_gaps_welded"] = n


def open_closed_lines(feats, st):
    """A generated line that starts and ends at the same point: a flattened there-and-back spike becomes the open
    line up to its far point (then an ordinary dead end); a real loop is split at its far point into two lines."""
    out, spikes, loops = [], 0, 0
    for f in feats:
        c = list(f["geom"].coords)
        if len(c) < 3 or c[0] != c[-1]:
            out.append(f); continue
        k = max(range(1, len(c) - 1), key=lambda i: math.dist(c[i], c[0]))
        if len(c) < 4 or Polygon(c).area < 1.0:
            out.append(dict(f, geom=LineString(c[:k + 1]))); spikes += 1
        else:
            out.append(dict(f, geom=LineString(c[:k + 1])))
            out.append(dict(f, geom=LineString(c[k:])))
            loops += 1
    st["closed_spikes_opened"] = spikes
    st["closed_loops_split"] = loops
    return out


def node_remaining_crossings(car_feats, ped_feats, man_all, st):
    """Final safety net: wherever two lines (at least one generated, none a bridge) still cross or touch between
    vertices, the same vertex goes into both (her line: added vertex, shape unchanged)."""
    items = [("auto", f) for f in car_feats + ped_feats] + [("her", m) for m in man_all]
    geom = lambda it: it[1]["geom"] if it[0] == "auto" else LineString(it[1]["utm"])
    bridge = lambda it: bool((it[1]["props"] if it[0] == "auto" else it[1]["f"]["properties"]).get("bridge"))
    tree = STRtree([geom(it) for it in items])
    n = 0

    def put(it, xy):
        if it[0] == "her":
            PIN[xy] = splice_manual(it[1], xy); return
        c = list(it[1]["geom"].coords)
        if xy in c:
            return
        ln = it[1]["geom"]; s_ = ln.project(Point(xy)); acc = 0.0
        for idx in range(len(c) - 1):
            seg = math.dist(c[idx], c[idx + 1])
            if acc + seg >= s_ - 1e-9:
                break
            acc += seg
        c.insert(idx + 1, xy)
        it[1]["geom"] = LineString(c)

    for a, ia in enumerate(items):
        if ia[0] != "auto" or bridge(ia):
            continue
        for b in tree.query(geom(ia)):
            ib = items[b]
            if b == a or bridge(ib) or (ib[0] == "auto" and b < a):
                continue
            ga, gb = geom(ia), geom(ib)
            x = ga.intersection(gb)
            pts = [x] if x.geom_type == "Point" else (list(x.geoms) if x.geom_type == "MultiPoint" else [])
            for q in pts:
                va = min(ga.coords, key=lambda v: math.dist(v, (q.x, q.y)))
                vb = min(gb.coords, key=lambda v: math.dist(v, (q.x, q.y)))
                if va == vb:
                    continue
                # prefer an existing vertex (hers first) as the shared point
                xy = vb if math.dist(vb, (q.x, q.y)) < 0.05 else va if math.dist(va, (q.x, q.y)) < 0.05 else (q.x, q.y)
                put(ia, xy); put(ib, xy); n += 1
    st["remaining_crossings_noded"] = n


def prune_final_spurs(car_feats, ped_feats, man_all, city, review, st):
    """The graph-level spur rule only sees the pedestrian graph, so a path hanging off a STREET (its attached end is
    a car vertex, degree 1 among pedestrian lines) and ending dead after a few dozen metres survived. Checked here on
    the whole union (car + pedestrian + her lines): a generated pedestrian line with one free end, the other end
    attached, shorter than DANGLE_DROP, is removed — repeated until nothing changes. Free ends on the city boundary
    (the path continues outside the data) do not count as dead ends."""
    edge = city.exterior if city.geom_type == "Polygon" else city.boundary
    gone = 0
    while True:
        verts = Counter()
        for f in car_feats + ped_feats:
            for v in set(f["geom"].coords):
                verts[v] += 1
        for m in man_all:
            for v in set(m["utm"]):
                verts[v] += 1
        drop = set()
        for i, f in enumerate(ped_feats):
            if f["geom"].length >= DANGLE_DROP:
                continue
            c = f["geom"].coords
            free = [verts[c[k]] == 1 and edge.distance(Point(c[k])) > 1.0 for k in (0, -1)]
            if free[0] != free[1]:
                drop.add(i)
                review.append(dict(xy=c[0] if free[0] else c[-1], kind="pedestrian: dead-end stub off the network removed",
                                   dist=round(f["geom"].length, 1)))
        if not drop:
            break
        gone += len(drop)
        ped_feats = [f for i, f in enumerate(ped_feats) if i not in drop]
    st["final_dead_end_stubs_removed"] = gone
    return ped_feats


def weld_to_car(car_feats, ped_feats, st, tol=0.01):
    """Graph nodes are rounded to the mm (Graph.node_at) while a car end moved by node clustering is not, so a
    pedestrian end "snapped onto car" can sit 1 mm off the car vertex — touching, but not the same vertex. Every
    pedestrian vertex within `tol` of a car vertex takes that car vertex's exact coordinate."""
    verts = list({c for f in car_feats for c in f["geom"].coords})
    tree = STRtree([Point(v) for v in verts])
    n = 0
    for f in ped_feats:
        c = list(f["geom"].coords)
        changed = False
        for k, xy in enumerate(c):
            j = tree.query_nearest(Point(xy), max_distance=tol)
            if len(j) and verts[int(j[0])] != xy:
                c[k] = verts[int(j[0])]; changed = True; n += 1
        if changed:
            f["geom"] = LineString(c)
    st["ped_vertices_welded_to_car_vertex"] = n


def splice_into_car(car_feats, ped_feats, st):
    """TASK_35 convention: every pedestrian vertex sitting on a generated car line becomes a vertex of that car
    line too (same coordinate), the car object is not split."""
    tree = STRtree([f["geom"] for f in car_feats])
    pts = set()
    for f in ped_feats:
        if f["props"]["bridge"]:
            continue
        for c in f["geom"].coords:
            pts.add(c)
    add = defaultdict(list)
    for xy in pts:
        p = Point(xy)
        for j in tree.query(p.buffer(0.01)):
            cf = car_feats[j]
            if cf["props"]["bridge"] or cf["geom"].distance(p) > 0.005:
                continue
            if any(math.dist(xy, c) < 0.005 for c in cf["geom"].coords):
                continue
            add[j].append(xy)
    n = 0
    for j, xys in add.items():
        ln = car_feats[j]["geom"]
        c = list(ln.coords)
        for xy in sorted(xys, key=lambda q: ln.project(Point(q)), reverse=True):
            s = ln.project(Point(xy))
            acc = 0.0
            for idx in range(len(c) - 1):
                seg = math.dist(c[idx], c[idx + 1])
                if acc + seg >= s - 1e-9:
                    break
                acc += seg
            c.insert(idx + 1, xy); n += 1
            ln = LineString(c)
        car_feats[j]["geom"] = LineString(c)
    st["ped_vertices_spliced_into_car"] = n


def drop_car_overlaps(ped_feats, car, st, against=None):
    """A footway drawn as a sidewalk next to a street can end up with both ends snapped onto the same car line
    (CAR_SNAP_TOL) and be straightened onto it: it then runs ON the car line — a duplicate of the street, not a
    path. The part lying on car is cut away; the rest of the line stays (its new ends are vertices on car).
    `against(i, f)` -> lines to test instead of car (used for pedestrian-on-pedestrian duplicates)."""
    tol = 0.02
    out, cut, dropped = [], 0, 0
    for fi, f in enumerate(ped_feats):
        ln = f["geom"]
        if f["props"]["bridge"]:
            out.append(f); continue
        if against:
            near = against(fi, f)
        else:
            near = [car["lines"][i] for i in car["line_tree"].query(ln.buffer(tol)) if not car["bridge"][i]]
        if not near:
            out.append(f); continue
        c = list(ln.coords)
        # a segment lies on car when both its ends and its middle are on the same car line (segment-wise, so the
        # remaining pieces keep their exact vertices)
        on = [any(cl.distance(Point(a)) < tol and cl.distance(Point(b)) < tol
                  and cl.distance(Point((a[0] + b[0]) / 2, (a[1] + b[1]) / 2)) < tol for cl in near)
              for a, b in zip(c, c[1:])]
        if not any(on):
            out.append(f); continue
        pieces, cur = [], [c[0]]
        for s, is_on in enumerate(on):
            if is_on:
                if len(cur) >= 2:
                    pieces.append(cur)
                cur = [c[s + 1]]
            else:
                cur.append(c[s + 1])
        if len(cur) >= 2:
            pieces.append(cur)
        pieces = [LineString(p) for p in pieces if LineString(p).length >= 1.0]
        if not pieces:
            dropped += 1; continue
        cut += 1
        for p in pieces:
            out.append(dict(f, geom=p))
    st["ped_on_car_cut"] = cut
    st["ped_on_car_dropped"] = dropped
    return out


def drop_detached(car_feats, ped_feats, man_car, man_ped, city, review, rep):
    """After the seam: a generated piece of network (car and/or pedestrian) that reaches neither her network, nor
    the main network, nor the city boundary (where the real street continues outside the data) is cut off for good
    — e.g. paths that ran up to the Base border where she drew nothing. Removed and reported."""
    key = tuple                         # exact coordinates: a 1 mm gap is a gap
    G = nx.Graph()
    items = [("car", i, f["geom"]) for i, f in enumerate(car_feats)] + [("pedestrian", i, f["geom"]) for i, f in enumerate(ped_feats)]
    for net, i, gm in items:
        c = [key(p) for p in gm.coords]
        G.add_node(("line", net, i))
        for p in c:
            G.add_edge(("line", net, i), p)
    for m in man_car + man_ped:
        c = [key(p) for p in m["utm"]]
        G.add_node(("manual", id(m)))
        for p in c:
            G.add_edge(("manual", id(m)), p)
    comps = sorted(nx.connected_components(G), key=len, reverse=True)
    edge = city.exterior if city.geom_type == "Polygon" else city.boundary
    gone = {"car": set(), "pedestrian": set()}
    for ci, comp in enumerate(comps):
        if ci == 0 or any(n[0] == "manual" for n in comp if isinstance(n[0], str)):
            continue
        pts = [n for n in comp if not isinstance(n[0], str)]
        if any(edge.distance(Point(p)) < 1.0 for p in pts):
            continue
        lines = [n for n in comp if isinstance(n[0], str) and n[0] == "line"]
        for _t, net, i in lines:
            gone[net].add(i)
        review.append(dict(xy=pts[0], kind="detached piece removed (reaches nothing)", dist=round(sum(
            (car_feats if net == "car" else ped_feats)[i]["geom"].length for _t, net, i in lines), 1)))
    rep["car"]["detached_lines_removed"] = len(gone["car"])
    rep["pedestrian"]["detached_lines_removed"] = len(gone["pedestrian"])
    return ([f for i, f in enumerate(car_feats) if i not in gone["car"]],
            [f for i, f in enumerate(ped_feats) if i not in gone["pedestrian"]])


# --------------------------------------------------------------------------- output / checks
def write_city(network, manual_feats, auto_feats):
    out = []
    for m in manual_feats:
        f = json.loads(json.dumps(m["f"]))
        f["properties"] = dict(f["properties"], source="manual")
        out.append(f)
    for i, a in enumerate(auto_feats):
        c84 = [list(PIN[xy]) if xy in PIN else list(FROM_UTM(*xy)) for xy in a["geom"].coords]
        p = a["props"]
        props = dict(ssId=f"{network[:3]}_city_{i + 1:05d}", network=network, length=round(a["geom"].length, 2),
                     srcClass=p["srcClass"], srcLayer=p["srcLayer"], simplified=p["simplified"],
                     straight=p["straight"], bridge=p["bridge"], turnaround_removed=p["turnaround_removed"],
                     source="auto")
        out.append({"type": "Feature", "properties": props, "geometry": {"type": "LineString", "coordinates": c84}})
    fc = {"type": "FeatureCollection", "name": OUT[network].stem,
          "crs": {"type": "name", "properties": {"name": "urn:ogc:def:crs:OGC:1.3:CRS84"}}, "features": out}
    OUT[network].write_text(json.dumps(fc, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    return fc


def check_written(fcs, quad, rep):
    """Independent check on the WRITTEN files: crossings without a shared vertex (within / between networks),
    union connectivity, free ends left on the seam."""
    lines = []
    for net, fc in fcs.items():
        for f in fc["features"]:
            lines.append((net, f["properties"]["source"], bool(f["properties"].get("bridge")),
                          LineString([tuple(c) for c in f["geometry"]["coordinates"]])))
    geoms = [l[3] for l in lines]
    tree = STRtree(geoms)
    bad = Counter()
    for i, a in enumerate(geoms):
        verts = set(a.coords)
        for j in tree.query(a):
            if j <= i:
                continue
            b = geoms[j]
            if lines[i][2] or lines[j][2]:
                continue
            x = a.intersection(b)
            if x.is_empty:
                continue
            pts = [x] if x.geom_type == "Point" else (list(x.geoms) if x.geom_type == "MultiPoint" else [])
            if not pts:
                bad[(lines[i][0], lines[j][0], "overlap")] += 1; continue
            bverts = set(b.coords)
            for q in pts:
                if (q.x, q.y) in verts and (q.x, q.y) in bverts:
                    continue
                if any(math.dist((q.x, q.y), v) < 1e-7 for v in verts) and any(math.dist((q.x, q.y), v) < 1e-7 for v in bverts):
                    continue
                bad[(lines[i][0], lines[j][0], f"{lines[i][1]}x{lines[j][1]}")] += 1
    rep["crossings_without_shared_vertex"] = {"|".join(k): v for k, v in bad.items()}
    G = nx.Graph()
    for net, src, br, g in lines:
        c = list(g.coords)
        for u, v in zip(c, c[1:]):
            G.add_edge(u, v)
    comps = sorted(nx.connected_components(G), key=len, reverse=True)
    rep["union_components"] = len(comps)
    rep["union_largest_component_share"] = round(len(comps[0]) / G.number_of_nodes(), 4) if comps else 0
    deg = Counter()
    for _n, _s, _b, g in lines:
        deg[g.coords[0]] += 1; deg[g.coords[-1]] += 1
    q84 = Polygon([FROM_UTM(x, y) for x, y in quad.exterior.coords])
    rep["free_ends_on_seam_after_stitch"] = sum(1 for p, d in deg.items() if d == 1 and Point(p).distance(q84.exterior) < 2e-5)


def main():
    t0 = time.time()
    P = ss.PRESETS[CITY_PRESET]
    quad = base_border_quad()
    city = city_polygon()
    ring = city.difference(quad)
    W = SimpleNamespace(exterior=ring.boundary)       # Graph only uses work.exterior: city edge + Base border edge
    rep = {"preset": CITY_PRESET, "params": P, "seam_tol": SEAM_TOL, "builtup_buffer": BUILTUP_BUFFER,
           "keep_share": KEEP_SHARE, "ring_km2": round(ring.area / 1e6, 2)}
    review = []

    man_car, man_car_ends = load_manual("car", quad)
    man_ped, man_ped_ends = load_manual("pedestrian", quad)
    rep["manual"] = dict(car_lines=len(man_car), car_edge_ends=len(man_car_ends),
                         pedestrian_lines=len(man_ped), pedestrian_edge_ends=len(man_ped_ends))
    man_geoms = {"car": [LineString(m["utm"]) for m in man_car], "pedestrian": [LineString(m["utm"]) for m in man_ped]}

    st = {}
    g = build_car(ring, W, quad, P, st)
    car_feats = ss.to_features(g, "car", quad, W)
    car_feats = [f for f in car_feats if f["geom"].length >= 0.1]
    car_feats = stitch(car_feats, man_car_ends, None, quad, P, man_geoms["car"], "car", review, st,
                       extend=make_extender("car", quad, man_car, P, review, st))
    node_extensions_on_manual(car_feats, man_ped, st)
    close_car_gaps(car_feats, man_car, man_ped, city, quad, review, st)
    st["removed_by_rule"] = dict(Counter(r for _c, r in g.removed))
    rep["car"] = st
    log("car done", len(car_feats), "lines")

    zone, builtup = keep_zone(ring)
    car = car_reference(car_feats)
    st = {}
    Pp = dict(P, spur_len=PED_SPUR_LEN)
    rep["params_pedestrian"] = Pp
    g, ped_sources = build_pedestrian(ring, W, quad, Pp, car, zone, builtup, st)
    ped_feats = ss.to_features(g, "pedestrian", quad, W)
    ped_feats = ss.snap_final_free_ends(ped_feats, car, st)
    ped_feats = ss.insert_final_car_crossings(ped_feats, car, st)
    ped_feats = drop_car_overlaps(ped_feats, car, st)
    ped_feats = [f for f in ped_feats if f["geom"].length >= 0.1]
    man_vertices = [dict(xy=xy, wgs=tuple(w), network=net)
                    for net, ms in (("car", man_car), ("pedestrian", man_ped)) for m in ms
                    for xy, w in zip(m["utm"], m["f"]["geometry"]["coordinates"])
                    if Point(xy).distance(quad.exterior) <= SEAM_TOL]
    ped_feats = stitch(ped_feats, man_ped_ends, man_car_ends, quad, Pp, man_geoms["pedestrian"] + man_geoms["car"],
                       "pedestrian", review, st, man_vertices,
                       extend=make_extender("pedestrian", quad, man_ped + man_car, Pp, review, st))
    node_extensions_on_manual(ped_feats, man_ped + man_car, st)
    grow_dead_ends(ped_feats, car_feats, man_ped + man_car, ped_sources, city, quad, Pp, review, st)
    join_car_ends_to_paths(car_feats, ped_feats, man_ped, city, quad, review, rep["car"])
    car = car_reference(car_feats)             # growths may have added vertices to car lines
    # the seam moves end segments by up to SEAM_TOL: repeat the two car-contact safety passes on the result
    ped_feats = ss.insert_final_car_crossings(ped_feats, car, {})
    ped_feats = drop_car_overlaps(ped_feats, car, {})
    st["removed_by_rule"] = dict(Counter(r for _c, r in g.removed))
    rep["pedestrian"] = st
    weld_to_car(car_feats, ped_feats, st)
    # two sources (OSM + ATKIS) of the same path can end up as two lines on identical vertices once welded: the
    # shorter one loses the segments lying on a longer (or equal, earlier) one
    geoms = [f["geom"] for f in ped_feats]
    ptree = STRtree(geoms)
    rank = lambda i: (-geoms[i].length, i)
    ped_feats = drop_car_overlaps(ped_feats, None, st.setdefault("ped_on_ped", {}),
                                  against=lambda i, f: [geoms[j] for j in ptree.query(geoms[i].buffer(0.02))
                                                        if j != i and rank(j) < rank(i)])
    splice_into_car(car_feats, ped_feats, rep["car"])
    weld_ends(car_feats, ped_feats, man_ped + man_car, st.setdefault("weld_before_detours", {}))
    ped_feats = open_closed_lines(ped_feats, st)
    ped_feats = remove_redundant_detours(car_feats, ped_feats, man_car, review, st)
    ped_feats = remove_parallel_twins(car_feats, ped_feats, man_ped + man_car, review, st)
    ped_feats = prune_final_spurs(car_feats, ped_feats, man_ped + man_car, city, review, st)
    car_feats, ped_feats = drop_detached(car_feats, ped_feats, man_car, man_ped, city, review, rep)
    weld_ends(car_feats, ped_feats, man_ped + man_car, rep["pedestrian"])
    # a weld can make two near-identical lines identical: the same duplicate pass as above, once more
    geoms = [f["geom"] for f in ped_feats]
    ptree = STRtree(geoms)
    ped_feats = drop_car_overlaps(ped_feats, None, rep["pedestrian"].setdefault("ped_on_ped_after_weld", {}),
                                  against=lambda i, f: [geoms[j] for j in ptree.query(geoms[i].buffer(0.02))
                                                        if j != i and rank(j) < rank(i)])
    node_remaining_crossings(car_feats, ped_feats, man_ped + man_car, rep["pedestrian"])
    # what still ends in the open (a real trail end, or a way not found): listed for review
    all_g = [f["geom"] for f in car_feats + ped_feats] + [LineString(m["utm"]) for m in man_car + man_ped]
    for net, feats in (("car", car_feats), ("pedestrian", ped_feats)):
        for i, k in free_ends(feats, all_g, city, quad):
            review.append(dict(xy=feats[i]["geom"].coords[k], kind=f"{net}: dead end left (no continuation found)",
                               dist=round(feats[i]["geom"].length, 1)))
    rep["car"]["output_lines"], rep["pedestrian"]["output_lines"] = len(car_feats), len(ped_feats)
    log("pedestrian done", len(ped_feats), "lines")

    fcs = {"car": write_city("car", man_car, car_feats), "pedestrian": write_city("pedestrian", man_ped, ped_feats)}
    REVIEW.write_text(json.dumps({"type": "FeatureCollection", "name": REVIEW.stem, "features": [
        {"type": "Feature", "properties": {"kind": r["kind"], "dist_m": r["dist"]},
         "geometry": mapping(Point(FROM_UTM(*r["xy"])))} for r in review]}, ensure_ascii=False), encoding="utf-8")
    rep["review_points"] = dict(Counter(r["kind"] for r in review))
    rep["check"] = {}
    check_written(fcs, quad, rep["check"])
    # her lines: every original vertex kept, in order, with its exact coordinate; added vertices lie on her line
    for net in ("car", "pedestrian"):
        src = json.load(open(MANUAL[net], encoding="utf-8"))["features"]
        got = [f for f in fcs[net]["features"] if f["properties"]["source"] == "manual"]
        ok, added = len(src) == len(got), 0
        for a, b in zip(src, got):
            ca, cb = a["geometry"]["coordinates"], b["geometry"]["coordinates"]
            it = iter(cb)
            ok &= all(any(v == w for w in it) for v in ca)          # subsequence, exact floats
            extra = len(cb) - len(ca)
            if extra:
                added += extra
                la = LineString([TO_UTM(*v) for v in ca])
                ok &= all(la.distance(Point(TO_UTM(*w))) < 0.01 for w in cb)
        rep["check"][f"{net}_manual_shape_unchanged"] = ok
        rep["check"][f"{net}_manual_vertices_added"] = added
    rep["seconds"] = round(time.time() - t0)
    REPORT.parent.mkdir(exist_ok=True)
    REPORT.write_text(json.dumps(rep, indent=1, ensure_ascii=False, default=str), encoding="utf-8")
    log("check", rep["check"])
    log("review", rep["review_points"])
    log("report ->", REPORT)


if __name__ == "__main__":
    main()
