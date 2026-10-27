#!/usr/bin/env python3
"""
ss_rules33.py — TASK_33 rules for build_space_syntax_roads.py (parts C, D, E, F, G).

Kept in its own module so the TASK_31/32 pipeline in build_space_syntax_roads.py stays readable. Every
function takes the builder's `Graph` (duck-typed) or plain source-line dicts and returns a change count.

  C plaza_lines            plazas by perimeter, no invented diagonals (replaces plaza_star_lines)
  D close_gaps             re-connect opposing free ends that the cleaning rules pulled apart
  E building_zone,         footprints (TASK_30) as a forbidden zone: clip, re-attach, flag real passages
    clip_lines_to_outside,
    restore_passages
  F drop_parallel_paths,   redundancy: parallel footway along a street axis, redundant detour edges,
    edge_importance,       edge betweenness -> `importance`, top share added to the protected set
    remove_redundant
  G stable_ss_id,          ids that survive a rebuild + data/manual/space_syntax_overrides.json
    load_overrides, apply_overrides, manual_lines
"""
import hashlib
import json
import math
from collections import defaultdict
from pathlib import Path

import networkx as nx
from pyproj import Transformer
from shapely.geometry import LineString, Point, shape
from shapely.ops import transform, unary_union
from shapely.strtree import STRtree

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "web" / "layers" / "data"
MANUAL = ROOT / "data" / "manual"
TO_UTM = Transformer.from_crs(4326, 25832, always_xy=True).transform

BUILDING_SHRINK = 0.3        # buffer(-0.3 m): do not catch lines that only touch a wall
BUILDING_MIN_OVERLAP = 1.0   # a shorter overlap is not worth cutting
REATTACH_TOL = 5.0           # a free end left by the building clip is snapped to a node within this
PASSAGE_DETOUR = 6.0         # a cut piece is restored as a "passage" if the detour is longer than this x its length


# --------------------------------------------------------------------------- G: stable ids + overrides
def stable_ss_id(coords, network):
    """Id derived from the geometry, so an unchanged segment keeps its id across rebuilds.

    Hash of (both endpoints, rounded to 1 m, order-independent) + (length, rounded to 1 m). NOT stable when the
    segment itself changes: different presets, or a parameter change that moves an endpoint or merges the edge,
    give a different id. That is why `apply_overrides` also matches by midpoint coordinates (`at`) within
    `match_tol` — the user's manual keep/remove entries survive a re-tuned build, the bare id does not.
    """
    a, b = coords[0], coords[-1]
    ends = sorted([(round(a[0]), round(a[1])), (round(b[0]), round(b[1]))])
    L = round(sum(math.dist(coords[i], coords[i + 1]) for i in range(len(coords) - 1)))
    h = hashlib.blake2b(repr((ends, L)).encode(), digest_size=5).hexdigest()
    return f"{network[:3]}_{h}"


def load_overrides():
    p = MANUAL / "space_syntax_overrides.json"
    if not p.exists():
        return {"keep": [], "remove": [], "match_tol": 10.0}
    o = json.load(open(p, encoding="utf-8"))
    o.setdefault("keep", []); o.setdefault("remove", []); o.setdefault("match_tol", 10.0)
    return o


def _entries_for(ov, network):
    return [e for e in ov if not e.get("network") or e["network"] == network]


def apply_overrides(g, network, ov, st, phase):
    """phase 'keep': mark matched edges protected (no rule may delete them).
       phase 'remove': delete matched edges (rule 'G_manual_remove')."""
    entries = _entries_for(ov.get(phase, []), network)
    if not entries:
        return 0
    tol = float(ov.get("match_tol", 10.0))
    ids = list(g.edges)
    mids = [LineString(g.edges[e]["coords"]).interpolate(0.5, normalized=True) for e in ids]
    tree = STRtree(mids) if mids else None
    byid = {stable_ss_id(g.edges[e]["coords"], network): e for e in ids}
    done, miss = 0, []
    for ent in entries:
        e = byid.get(ent.get("ssId"))
        if e is None and ent.get("at") and tree is not None:
            p = Point(ent["at"][0], ent["at"][1])
            cand = [i for i in tree.query(p.buffer(tol)) if mids[i].distance(p) <= tol]
            if cand:
                e = ids[min(cand, key=lambda i: mids[i].distance(p))]
        if e is None or e not in g.edges:
            miss.append(ent.get("ssId") or ent.get("at")); continue
        if phase == "keep":
            g.edges[e]["prot"] = "G manual keep"
        else:
            g.rem(e, "G_manual_remove")
        done += 1
    st[f"overrides_{phase}_applied"] = done
    if miss:
        st[f"overrides_{phase}_unmatched"] = miss
    return done


def manual_lines(network, work):
    """data/manual/manual_lines_<network>.geojson -> source lines (protected, they go through snap/node like the rest)."""
    p = MANUAL / f"manual_lines_{network}.geojson"
    if not p.exists():
        return []
    out = []
    for f in json.load(open(p, encoding="utf-8"))["features"]:
        gm = f.get("geometry")
        if not gm or gm["type"] not in ("LineString", "MultiLineString"):
            continue
        g = transform(TO_UTM, shape(gm)).intersection(work)
        for part in getattr(g, "geoms", [g]):
            if part.geom_type == "LineString" and part.length > 0.01:
                key = ("MANUAL", f"manual_lines_{network}", False, True, "G manual line")
                out.append(dict(coords=list(part.coords), parts={key: part.length}, bridge=False))
    return out


# --------------------------------------------------------------------------- C: plazas
def plaza_lines(lines, work, stats, plaza_polys, snap_tol=3.0, simplify_tol=2.0):
    """TASK_33 part C. Replaces the TASK_31/32 `plaza_star_lines`, which cut every line at the plaza boundary and
    replaced the inside by a star of entry->hub lines (the "diagonals crossing" complaint; it also deleted real
    perimeter paths that lie entirely inside a plaza, e.g. the 361 m axis named Jorge-Semprún-Platz).

    Now: real lines are never cut or removed. A plaza gets its perimeter added as extra lines ONLY where the
    perimeter is not already carried by a real line (within `snap_tol`), and only if the plaza has >= 2 entries.
    Perimeter pieces are protected (group D).
    """
    geoms = [LineString(l["coords"]) for l in lines]
    tree = STRtree(geoms) if geoms else None
    add = []
    served = covered = 0
    for pz, name in plaza_polys:
        ring = pz.exterior.simplify(simplify_tol)
        near = [] if tree is None else [geoms[i] for i in tree.query(pz.buffer(snap_tol))]
        entries = [g for g in near if g.intersects(pz.buffer(snap_tol))]
        if len(entries) < 2:
            continue
        served += 1
        covers = unary_union([g.buffer(snap_tol) for g in near]) if near else None
        missing = ring.difference(covers) if covers is not None else ring
        parts = [p for p in getattr(missing, "geoms", [missing]) if p.geom_type == "LineString" and p.length > 5.0]
        if not parts:
            covered += 1
            continue
        key = ("AX_Platz:perimeter", "weimar_atkis_platz", False, bool(name), "D plaza perimeter")
        for p in parts:
            # The piece was cut at the `snap_tol` buffer of the real lines, so its ends stop ~snap_tol short of
            # them and the 1.5 m end snapping cannot reach: tie each end to the nearest real line explicitly,
            # otherwise the perimeter floats as its own component (seen as 22 extra components in the first run).
            c = list(p.coords)
            for at in (0, -1):
                q = Point(c[at])
                cands = [g for g in near if g.distance(q) <= 3 * snap_tol]
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


# --------------------------------------------------------------------------- E: buildings
class BuildingZone:
    """The TASK_30 footprints as a forbidden zone, indexed. One `unary_union` of 63 450 polygons is far too slow
    for per-segment tests, so the polygons are kept separate behind an STRtree and only the candidates near a
    line are intersected."""

    def __init__(self, polys):
        self.polys = polys
        self.tree = STRtree(polys) if polys else None
        self.is_empty = not polys

    def near(self, geom):
        if self.tree is None:
            return []
        return [self.polys[i] for i in self.tree.query(geom)]

    def inside_length(self, geom):
        tot = 0.0
        for b in self.near(geom):
            x = geom.intersection(b)
            if not x.is_empty:
                tot += x.length
        return tot

    def local_union(self, geom):
        n = self.near(geom)
        return unary_union(n) if n else None


_ZONE = None


def building_zone(work=None):
    global _ZONE
    if _ZONE is None:
        fc = json.load(open(DATA / "weimar_building_footprints.geojson", encoding="utf-8"))
        polys = []
        for f in fc["features"]:
            g = transform(TO_UTM, shape(f["geometry"]))
            for part in getattr(g, "geoms", [g]):
                if part.geom_type != "Polygon" or part.area <= 1:
                    continue
                if work is not None and not part.intersects(work):
                    continue
                b = part.buffer(-BUILDING_SHRINK)
                for q in getattr(b, "geoms", [b]):
                    if q.geom_type == "Polygon" and not q.is_empty and q.area > 0.5:
                        polys.append(q)
        _ZONE = BuildingZone(polys)
    return _ZONE


def clip_lines_to_outside(lines, zone, stats):
    """Cut every source line where it runs inside a building. Returns (lines, cut_pieces) — the pieces are kept so
    `restore_passages` can put real archways (Durchgang) back instead of silently dropping them."""
    out, cuts = [], []
    n_cut = 0
    for l in lines:
        g = LineString(l["coords"])
        loc = None if zone.is_empty else zone.local_union(g)
        if loc is None or not g.intersects(loc):
            out.append(l); continue
        inside = g.intersection(loc)
        if inside.is_empty or getattr(inside, "length", 0.0) <= BUILDING_MIN_OVERLAP:
            out.append(l); continue
        n_cut += 1
        rest = g.difference(loc)
        for part in getattr(rest, "geoms", [rest]):
            if part.geom_type == "LineString" and part.length > 1.0:
                out.append(dict(coords=list(part.coords), parts=dict(l["parts"]), bridge=l["bridge"]))
        for part in getattr(inside, "geoms", [inside]):
            if part.geom_type == "LineString" and part.length > BUILDING_MIN_OVERLAP:
                cuts.append(dict(coords=list(part.coords), parts=dict(l["parts"]), bridge=l["bridge"]))
    stats["building_lines_cut"] = n_cut
    stats["building_pieces_removed"] = len(cuts)
    stats["building_km_removed"] = round(sum(LineString(c["coords"]).length for c in cuts) / 1000, 3)
    return out, cuts


def restore_passages(g, cuts, st):
    """A cut piece whose two ends are now far apart in the graph is a real passage (archway/Durchgang): put it
    back, flag it `passage=true` and list it in *_passages_review.geojson for the user to check by hand."""
    if not cuts:
        st["passages_restored"] = 0
        return []
    nodes = list(g.nodes)
    if not nodes:
        st["passages_restored"] = 0
        return []
    pts = [Point(g.nodes[n]) for n in nodes]
    tree = STRtree(pts)
    S = nx.Graph()
    for e, d in g.edges.items():
        S.add_edge(d["u"], d["v"], weight=g.length(e))
    out = []
    for c in cuts:
        ln = LineString(c["coords"])
        ends = []
        for xy in (ln.coords[0], ln.coords[-1]):
            p = Point(xy)
            cand = [i for i in tree.query(p.buffer(REATTACH_TOL)) if pts[i].distance(p) <= REATTACH_TOL]
            ends.append(nodes[min(cand, key=lambda i: pts[i].distance(p))] if cand else None)
        if ends[0] is None or ends[1] is None or ends[0] == ends[1]:
            continue
        try:
            d = nx.shortest_path_length(S, ends[0], ends[1], weight="weight")
        except (nx.NetworkXNoPath, nx.NodeNotFound):
            d = float("inf")
        if d <= PASSAGE_DETOUR * max(ln.length, 1.0):
            continue                                    # a normal way round exists: the cut stays
        key = list(c["parts"])[0]
        e = g.add_edge(ends[0], ends[1], [g.nodes[ends[0]], *list(ln.coords)[1:-1], g.nodes[ends[1]]],
                       c["parts"], c["bridge"])
        g.edges[e]["passage"] = True
        g.edges[e]["prot"] = "E passage"
        S.add_edge(ends[0], ends[1], weight=ln.length)
        out.append(dict(geom=LineString(g.edges[e]["coords"]), srcClass=key[0],
                        detour=None if d == float("inf") else round(d)))
    st["passages_restored"] = len(out)
    return out


def reattach_or_prune_free_ends(g, st):
    """A free end created by the building clip is snapped to the nearest node within REATTACH_TOL; if there is
    none, the edge is a stub left hanging in front of a wall and is removed."""
    moved = dropped = 0
    for _ in range(3):
        ch = 0
        free = [n for n in list(g.nodes) if n in g.adj and g.deg(n) == 1 and n not in g.border]
        if not free:
            break
        others = [n for n in g.nodes if n not in free]
        if not others:
            break
        pts = [Point(g.nodes[n]) for n in others]
        tree = STRtree(pts)
        for n in free:
            if n not in g.nodes or g.deg(n) != 1:
                continue
            p = Point(g.nodes[n])
            cand = [i for i in tree.query(p.buffer(REATTACH_TOL)) if 0.01 < pts[i].distance(p) <= REATTACH_TOL]
            if not cand:
                continue
            tgt = others[min(cand, key=lambda i: pts[i].distance(p))]
            if tgt not in g.nodes:
                continue
            g.move_node(n, g.nodes[tgt])
            g.merge_nodes(tgt, n)
            moved += 1; ch += 1
        if not ch:
            break
    st["building_free_ends_reattached"] = moved
    st["building_free_ends_dropped"] = dropped
    return moved


def flag_building_crossings(g, zone, st, key=""):
    """Last word on part E. A segment may still sit inside a building after the node moves of the cleaning rules
    (clustering, contraction) even though every simplification candidate was checked. For each such segment try a
    less simplified candidate first; what still crosses is a REAL crossing (an archway, or a footprint drawn over
    a path) and is flagged `passage=True` so it lands in *_passages_review.geojson instead of being hidden."""
    sfx = f"_{key}" if key else ""
    if zone is None or zone.is_empty:
        st["building_crossings_left" + sfx] = 0
        return 0
    fixed = flagged = 0
    for e, d in g.edges.items():
        if not crosses_buildings(d["coords"], zone):
            continue
        cands = d.get("cands") or []
        done = False
        for lvl in range(d.get("lvl", 0) + 1, len(cands)):
            if not crosses_buildings(cands[lvl][0], zone):
                d["lvl"] = lvl; d["coords"], d["straight"] = cands[lvl]
                fixed += 1; done = True; break
        if not done:
            d["passage"] = True
            flagged += 1
    st["building_crossings_fixed_by_less_simplification" + sfx] = fixed
    st["building_crossings_left" + sfx] = flagged
    return flagged


def crosses_buildings(coords, zone):
    if zone is None or zone.is_empty or len(coords) < 2:
        return False
    return zone.inside_length(LineString(coords)) > BUILDING_MIN_OVERLAP


# --------------------------------------------------------------------------- D: gaps
def close_gaps(g, tol, st, zone=None):
    """Free ends that face each other within `tol` and are roughly collinear are joined by a straight line, if the
    two sides are not already connected and the new line does not run through a building."""
    added = 0
    for _ in range(3):
        free = [n for n in g.nodes if n in g.adj and g.deg(n) == 1 and n not in g.border]
        if len(free) < 2:
            break
        pts = [Point(g.nodes[n]) for n in free]
        tree = STRtree(pts)
        S = nx.Graph()
        for e, d in g.edges.items():
            S.add_edge(d["u"], d["v"])
        comp = {}
        for i, c in enumerate(nx.connected_components(S)):
            for n in c:
                comp[n] = i
        made = 0
        for ai, a in enumerate(free):
            if a not in g.nodes or g.deg(a) != 1:
                continue
            pa = Point(g.nodes[a])
            va = _dir_at(g, a)
            best = None
            for j in tree.query(pa.buffer(tol)):
                b = free[j]
                if b == a or b not in g.nodes or g.deg(b) != 1:
                    continue
                d = pa.distance(pts[j])
                if not (0.01 < d <= tol):
                    continue
                if comp.get(a) == comp.get(b):
                    continue
                vb = _dir_at(g, b)
                if va is None or vb is None:
                    continue
                # both ends must point at each other: the join continues both lines
                jx = (g.nodes[b][0] - g.nodes[a][0], g.nodes[b][1] - g.nodes[a][1])
                if _cos(va, jx) < 0.5 or _cos(vb, (-jx[0], -jx[1])) < 0.5:
                    continue
                if zone is not None and crosses_buildings([g.nodes[a], g.nodes[b]], zone):
                    continue
                if best is None or d < best[0]:
                    best = (d, b)
            if best is None:
                continue
            b = best[1]
            key = ("GAP:closed", "task33", False, False, "D gap close")
            e = g.add_edge(a, b, [g.nodes[a], g.nodes[b]], {key: best[0]}, False)
            g.edges[e]["prot"] = "D gap close"
            comp[b] = comp[a]
            made += 1; added += 1
        if not made:
            break
    st["gaps_closed"] = added
    return added


def _dir_at(g, n):
    es = list(g.adj[n])
    if not es:
        return None
    d = g.edges[es[0]]
    c = d["coords"] if d["v"] == n else list(reversed(d["coords"]))
    # direction of the last piece, pointing outwards from the line
    p2, p1 = c[-1], c[-2] if len(c) >= 2 else c[0]
    v = (p2[0] - p1[0], p2[1] - p1[1])
    L = math.hypot(*v)
    return (v[0] / L, v[1] / L) if L > 1e-9 else None


def _cos(a, b):
    L = math.hypot(*b)
    return (a[0] * b[0] + a[1] * b[1]) / L if L > 1e-9 else -1.0


# --------------------------------------------------------------------------- F: redundancy + importance
def drop_parallel_paths(g, P, st, is_protected):
    """A non-protected footway that runs alongside a street axis for most of its length (within `sidewalk_tol`)
    duplicates that street in a pedestrian network: the street axis carries it, the footway goes."""
    tol = P.get("sidewalk_tol", 12.0)
    streets = [e for e, d in g.edges.items() if _dominant_layer(d) == "weimar_atkis_strassenachse"]
    paths = [e for e, d in g.edges.items()
             if _dominant_layer(d) != "weimar_atkis_strassenachse" and not is_protected(e)]
    if not streets or not paths:
        st["parallel_paths_removed"] = 0
        return 0
    sg = [LineString(g.edges[e]["coords"]) for e in streets]
    tree = STRtree(sg)
    n = 0
    for e in paths:
        if e not in g.edges:
            continue
        ln = LineString(g.edges[e]["coords"])
        if ln.length < 10:
            continue
        near = [sg[i] for i in tree.query(ln.buffer(tol))]
        if not near:
            continue
        cov = unary_union([s.buffer(tol) for s in near])
        if ln.difference(cov).length > 0.25 * ln.length:
            continue
        if _would_split(g, e):
            continue
        g.rem(e, "F_parallel_path"); n += 1
    st["parallel_paths_removed"] = n
    return n


def _dominant_layer(d):
    (k, _v) = max(d["parts"].items(), key=lambda kv: kv[1])
    return k[1]


def _would_split(g, e):
    """True if deleting e raises the number of connected components (a bridge in the graph sense)."""
    d = g.edges[e]
    u, v = d["u"], d["v"]
    if u == v:
        return False
    S = nx.Graph()
    for e2, d2 in g.edges.items():
        if e2 != e:
            S.add_edge(d2["u"], d2["v"])
    if u not in S or v not in S:
        return True
    return not nx.has_path(S, u, v)


def edge_importance(g, st, sample=250, seed=7):
    """Edge betweenness (length as weight) on a simple graph; parallel edges share the value of their node pair.
    `sample` sources are used instead of all nodes — an approximation, stated as such in the report."""
    S = nx.Graph()
    for e, d in g.edges.items():
        if d["u"] == d["v"]:
            continue
        L = g.length(e)
        if S.has_edge(d["u"], d["v"]) and S[d["u"]][d["v"]]["weight"] <= L:
            continue
        S.add_edge(d["u"], d["v"], weight=L)
    if S.number_of_edges() == 0:
        return {}
    k = min(sample, S.number_of_nodes())
    bt = nx.edge_betweenness_centrality(S, k=k, weight="weight", seed=seed)
    mx = max(bt.values()) or 1.0
    for e, d in g.edges.items():
        v = bt.get((d["u"], d["v"]), bt.get((d["v"], d["u"]), 0.0))
        g.edges[e]["imp"] = round(v / mx, 4)
    st["importance_sources"] = k
    return bt


def remove_redundant(g, P, st, is_protected):
    """A short non-protected edge whose endpoints stay close together without it (alternative path
    <= detour_max x its length) carries no new connection: removed. Lowest `importance` goes first."""
    dmax, lmax = P.get("detour_max", 1.3), P.get("redundant_max", 60.0)
    order = sorted((e for e in g.edges if not is_protected(e)),
                   key=lambda e: (g.edges[e].get("imp", 0.0), g.length(e)))
    S = nx.Graph()
    for e, d in g.edges.items():
        L = g.length(e)
        if S.has_edge(d["u"], d["v"]) and S[d["u"]][d["v"]]["weight"] <= L:
            continue
        S.add_edge(d["u"], d["v"], weight=L)
    n = 0
    for e in order:
        if e not in g.edges:
            continue
        d = g.edges[e]
        L = g.length(e)
        if L > lmax or d["u"] == d["v"]:
            continue
        if d["u"] in g.border or d["v"] in g.border:
            continue
        w = S[d["u"]][d["v"]]["weight"] if S.has_edge(d["u"], d["v"]) else None
        S.remove_edge(d["u"], d["v"]) if S.has_edge(d["u"], d["v"]) else None
        try:
            alt = nx.shortest_path_length(S, d["u"], d["v"], weight="weight")
        except (nx.NetworkXNoPath, nx.NodeNotFound):
            alt = float("inf")
        if alt <= dmax * L:
            g.rem(e, "F_redundant"); n += 1
        elif w is not None:
            S.add_edge(d["u"], d["v"], weight=w)
    st["redundant_removed"] = n
    return n
