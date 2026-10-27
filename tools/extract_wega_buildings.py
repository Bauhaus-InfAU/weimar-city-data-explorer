"""Re-extract the WEGA LoD2 buildings (b3dm) correctly — TASK 41.

Why: the TASK 12 extraction read the glTF POSITION accessor as tightly packed float32, but the
WEGA tiles interleave POSITION+NORMAL (byteStride 24) and the CITY_MODEL node matrix is
column-major followed by the glTF Y-up -> Z-up conversion. The old OBJ files were therefore
skewed (no vertical walls). Verified here: after the fix ~60 % of all triangles are vertical walls.

Input : data/raw/wega_buildings_b3dm/*.b3dm
Output: data/processed/wega_buildings_obj/*.obj       (EPSG:25832 metres, z = ellipsoidal height; the old,
                                                      broken set is kept in wega_buildings_obj_broken_task12/)
        web/layers/data/weimar_detailed_buildings.{json,bin}   compact mesh for the web viewer
Run   : python tools/extract_wega_buildings.py
"""
import glob
import json
import os
import shutil
import struct
import sys
from pathlib import Path

import numpy as np
import pygltflib
from pyproj import Transformer

ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / "data" / "raw" / "wega_buildings_b3dm"
OBJ_DIR = ROOT / "data" / "processed" / "wega_buildings_obj"
OBJ_OLD = ROOT / "data" / "processed" / "wega_buildings_obj_broken_task12"
WEB = ROOT / "web" / "layers" / "data"
ANCHOR_LNGLAT = (11.3297, 50.9795)       # Markt — the viewer's local origin
EDGE_ANGLE_DEG = 22.0      # thin inner lines start here


ecef2utm = Transformer.from_crs("EPSG:4978", "EPSG:25832", always_xy=True)
utm2merc = Transformer.from_crs("EPSG:25832", "EPSG:3857", always_xy=True)
ll2merc = Transformer.from_crs("EPSG:4326", "EPSG:3857", always_xy=True)
Y2Z = np.array([[1, 0, 0], [0, 0, -1], [0, 1, 0]], float)
NP = {5120: np.int8, 5121: np.uint8, 5122: np.int16, 5123: np.uint16, 5125: np.uint32, 5126: np.float32}
NC = {"SCALAR": 1, "VEC2": 2, "VEC3": 3, "VEC4": 4}


def read_accessor(g, blob, i):
    a = g.accessors[i]
    bv = g.bufferViews[a.bufferView]
    dt = np.dtype(NP[a.componentType])
    n = NC[a.type]
    item = dt.itemsize * n
    stride = bv.byteStride or item
    start = (bv.byteOffset or 0) + (a.byteOffset or 0)
    raw = np.frombuffer(blob, np.uint8, a.count * stride - (stride - item), start)
    raw = np.lib.stride_tricks.as_strided(raw, shape=(a.count, item), strides=(stride, 1))
    return np.ascontiguousarray(raw).view(dt).reshape(a.count, n)


def node_world(g):
    """world rotation (3x3) per node index, column-major matrices, parents applied."""
    W = {}

    def walk(ni, parent):
        n = g.nodes[ni]
        m = np.array(n.matrix).reshape(4, 4).T[:3, :3] if n.matrix else np.eye(3)
        W[ni] = parent @ m
        for c in n.children or []:
            walk(c, W[ni])
    scene = g.scenes[g.scene or 0]
    for r in scene.nodes:
        walk(r, np.eye(3))
    return W


def parse_tile(path):
    b = open(path, "rb").read()
    _, _, _, ftj, ftb, btj, btb = struct.unpack("<4sIIIIII", b[:28])
    ft = json.loads(b[28:28 + ftj])
    rtc = np.array(ft["RTC_CENTER"], float)
    bt = json.loads(b[28 + ftj + ftb:28 + ftj + ftb + btj])
    ids = bt["id"]
    glb = b[28 + ftj + ftb + btj + btb:]
    tmp = ROOT / "data" / "raw" / "_tmp_tile.glb"
    tmp.write_bytes(glb)
    g = pygltflib.GLTF2().load_binary(str(tmp))
    blob = g.binary_blob()
    W = node_world(g)
    out = {}          # building index -> list of (verts Nx3 utm, faces Mx3 local)
    for ni, node in enumerate(g.nodes):
        if node.mesh is None:
            continue
        R = Y2Z @ W[ni]
        for prim in g.meshes[node.mesh].primitives:
            P = read_accessor(g, blob, prim.attributes.POSITION).astype(float)
            bid = read_accessor(g, blob, prim._BATCHID if hasattr(prim, "_BATCHID") and prim._BATCHID is not None
                                else prim.attributes._BATCHID)[:, 0].astype(int)
            I = read_accessor(g, blob, prim.indices)[:, 0].astype(np.int64).reshape(-1, 3)
            ecef = P @ R.T + rtc
            x, y, z = ecef2utm.transform(ecef[:, 0], ecef[:, 1], ecef[:, 2])
            V = np.column_stack([x, y, z])
            fb = bid[I[:, 0]]
            for b_ in np.unique(fb):
                f = I[fb == b_]
                used, inv = np.unique(f.ravel(), return_inverse=True)
                out.setdefault(int(b_), []).append((V[used], inv.reshape(-1, 3)))
    return ids, out


def merge_parts(parts):
    V = np.vstack([p[0] for p in parts])
    F = []
    off = 0
    for v, f in parts:
        F.append(f + off)
        off += len(v)
    return V, np.vstack(F)


def dedupe(V, F):
    key = np.round(V, 3)
    u, first, inv = np.unique(key, axis=0, return_index=True, return_inverse=True)
    inv = inv.ravel()
    V2 = V[first]
    F2 = inv[F]
    ok = (F2[:, 0] != F2[:, 1]) & (F2[:, 1] != F2[:, 2]) & (F2[:, 0] != F2[:, 2])
    return V2, F2[ok]


def feature_edges(V, F):
    """(outline, inner) edge arrays.
    outline = the object's contour in plan: open boundary edges and edges where a vertical wall meets a
    non-wall face (eaves / base); inner = every other crease sharper than EDGE_ANGLE_DEG
    (ridges, hips, wall corners, dormers) — drawn thinner."""
    a, b, c = V[F[:, 0]], V[F[:, 1]], V[F[:, 2]]
    n = np.cross(b - a, c - a)
    ln = np.linalg.norm(n, axis=1)
    n = n / np.where(ln > 1e-12, ln, 1)[:, None]
    wall = np.abs(n[:, 2]) < 0.25
    emap = {}
    for fi, (i, j, k) in enumerate(F):
        for p, q in ((i, j), (j, k), (k, i)):
            emap.setdefault((p, q) if p < q else (q, p), []).append(fi)
    cin = np.cos(np.radians(EDGE_ANGLE_DEG))
    outl, inner = [], []
    for e, fl in emap.items():
        if len(fl) != 2:
            outl.append(e); continue
        if float(n[fl[0]] @ n[fl[1]]) >= cin:
            continue
        (outl if wall[fl[0]] != wall[fl[1]] else inner).append(e)
    return np.array(outl, np.uint16).reshape(-1, 2), np.array(inner, np.uint16).reshape(-1, 2)


def main():
    if OBJ_DIR.exists() and not OBJ_OLD.exists():
        OBJ_DIR.rename(OBJ_OLD)
        print("old (broken) OBJ set ->", OBJ_OLD.name)
    OBJ_DIR.mkdir(parents=True, exist_ok=True)
    ax, ay = ll2merc.transform(*ANCHOR_LNGLAT)

    B = []        # per building: dict
    files = sorted(glob.glob(str(RAW / "*.b3dm")))
    for ti, fp in enumerate(files):
        ids, out = parse_tile(fp)
        name = Path(fp).stem
        lines = [f"# WEGA buildings, EPSG:25832, z = ellipsoidal height (TASK 41 re-extraction of {name}.b3dm)"]
        voff = 0
        flines = []
        for bi, parts in out.items():
            V, F = merge_parts(parts)
            bid = ids[bi]
            lines.extend(f"v {x:.3f} {y:.3f} {z:.3f}" for x, y, z in V)
            flines.append(f"o {bid}")
            flines.extend(f"f {a + voff + 1} {b + voff + 1} {c + voff + 1}" for a, b, c in F)
            voff += len(V)
            V2, F2 = dedupe(V, F)
            if len(F2) == 0:
                continue
            B.append((bid, V2, F2))
        (OBJ_DIR / f"{name}.obj").write_text("\n".join(lines + flines) + "\n")
        if ti % 20 == 0:
            print(f"{ti + 1}/{len(files)} tiles, {len(B)} buildings", flush=True)
    seen, uniq = set(), []
    for t in B:
        if t[0] not in seen:
            seen.add(t[0]); uniq.append(t)
    B = uniq
    print(len(B), "unique buildings")

    pos, idx, edg, edgT, tab, bbox, height, basez = [], [], [], [], [], [], [], []
    ids_out = []
    vcount = icount = ecount = tcount = 0
    for bid, V, F in B:
        mx, my = utm2merc.transform(V[:, 0], V[:, 1])
        x = mx - ax; y = my - ay
        z0 = float(V[:, 2].min())
        z = V[:, 2] - z0
        P = np.column_stack([x, y, z]).astype(np.float32)
        EO, EI = feature_edges(P.astype(float), F)
        pos.append(P)
        idx.append(F.astype(np.uint16).ravel())
        edg.append(EI.ravel()); edgT.append(EO.ravel())
        tab.append((vcount, len(P), icount, F.size, ecount, EI.size, tcount, EO.size))
        bbox.append((x.min(), y.min(), x.max(), y.max()))
        height.append(float(z.max()))
        basez.append(z0)
        ids_out.append(bid.replace("urn:adv:oid:", ""))
        vcount += len(P); icount += F.size; ecount += EI.size; tcount += EO.size

    def pad(a):
        b = a.tobytes()
        return b + b"\0" * (-len(b) % 4)
    sections = [
        ("pos", np.vstack(pos).astype(np.float32)),
        ("idx", np.concatenate(idx).astype(np.uint16)),
        ("edg", np.concatenate(edg).astype(np.uint16)),
        ("edgT", np.concatenate(edgT).astype(np.uint16)),
        ("tab", np.array(tab, np.uint32).ravel()),
        ("bbox", np.array(bbox, np.float32).ravel()),
        ("height", np.array(height, np.float32)),
        ("basez", np.array(basez, np.float32)),
    ]
    meta = {"anchor": {"lng": ANCHOR_LNGLAT[0], "lat": ANCHOR_LNGLAT[1], "mx": ax, "my": ay},
            "buildings": len(B), "vertices": vcount, "indices": icount, "edgeIndices": ecount, "outlineIndices": tcount, "tabColumns": 8,
            "crs": "x,y = EPSG:3857 metres minus anchor; z = metres above the building's lowest vertex",
            "sections": {}, "ids": ids_out}
    WEB.mkdir(parents=True, exist_ok=True)
    off = 0
    with open(WEB / "weimar_detailed_buildings.bin", "wb") as fh:
        for name, arr in sections:
            data = pad(arr)
            meta["sections"][name] = {"offset": off, "bytes": arr.nbytes, "dtype": str(arr.dtype), "count": int(arr.size)}
            fh.write(data); off += len(data)
    json.dump(meta, open(WEB / "weimar_detailed_buildings.json", "w"), separators=(",", ":"))
    print("web bin", off / 1048576, "MB;", len(B), "buildings,", vcount, "verts,", icount // 3, "tris,", ecount // 2, "edges inner,", tcount // 2, "outline edges")
    print("height: mean %.1f median %.1f max %.1f" % (np.mean(height), np.median(height), np.max(height)))


if __name__ == "__main__":
    sys.exit(main())
