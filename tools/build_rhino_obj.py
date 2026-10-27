"""TASK_24 — terrain + draped buildings as plain OBJ in EPSG:25832 (Heron-free path for Rhino)."""
import json
from pathlib import Path
import numpy as np, tifffile, shapely
from pyproj import Transformer
from shapely.geometry import shape
from shapely.ops import transform

ROOT = Path(__file__).resolve().parent.parent
DEM = ROOT / "data/processed/dgm_weimar_lightweight/weimar_dgm_25832_15m.tif"
BLD = ROOT / "web/layers/data/weimar_buildings_footprint_height.geojson"
OUT = ROOT / "data/processed/rhino_obj"
NODATA = -9999.0

with tifffile.TiffFile(DEM) as t:
    pg = t.pages[0]; Z = pg.asarray().astype(np.float64)
    px = pg.tags["ModelPixelScaleTag"].value[0]
    ulx, uly = pg.tags["ModelTiepointTag"].value[3:5]
H, W = Z.shape
Z[Z == NODATA] = np.nan
# pixel centres (PixelIsArea: tiepoint = top-left corner)
X0, Y0 = ulx + px / 2, uly - px / 2

# ---- part 1: terrain ---------------------------------------------------- #
valid = ~np.isnan(Z)
idx = np.full((H, W), 0, np.int64)
idx[valid] = np.arange(1, valid.sum() + 1)
rr, cc = np.nonzero(valid)
tri = []
ok = valid[:-1, :-1] & valid[1:, :-1] & valid[:-1, 1:] & valid[1:, 1:]
r, c = np.nonzero(ok)
a, b, d, e = idx[r, c], idx[r, c + 1], idx[r + 1, c], idx[r + 1, c + 1]
faces = np.concatenate([np.stack([a, d, b], 1), np.stack([b, d, e], 1)])  # CCW seen from +Z
with open(OUT / "weimar_terrain_15m.obj", "w") as f:
    f.write("# Weimar terrain 15 m, EPSG:25832 metres, Z = DGM height\no terrain\n")
    for i, j in zip(rr, cc):
        f.write(f"v {X0 + j * px:.2f} {Y0 - i * px:.2f} {Z[i, j]:.2f}\n")
    for q in faces:
        f.write(f"f {q[0]} {q[1]} {q[2]}\n")
print("terrain", len(rr), "verts", len(faces), "faces", (ok.size - ok.sum()), "holes cells")

def height_at(x, y):
    fc, fr = (x - X0) / px, (Y0 - y) / px
    c0, r0 = int(np.floor(fc)), int(np.floor(fr))
    if not (0 <= c0 < W - 1 and 0 <= r0 < H - 1):
        return np.nan
    tx, ty = fc - c0, fr - r0
    z = np.array([[Z[r0, c0], Z[r0, c0 + 1]], [Z[r0 + 1, c0], Z[r0 + 1, c0 + 1]]])
    if np.isnan(z).all():
        return np.nan
    w = np.array([[(1 - tx) * (1 - ty), tx * (1 - ty)], [(1 - tx) * ty, tx * ty]])
    m = ~np.isnan(z)                       # renormalise if a corner is NODATA
    return float((z[m] * w[m]).sum() / w[m].sum())

# ---- part 2: buildings -------------------------------------------------- #
gj = json.load(open(BLD, encoding="utf-8"))
tr = Transformer.from_crs(4326, 25832, always_xy=True).transform
nb = nv = nf = skipped = 0
stats = []
with open(OUT / "weimar_buildings_draped_15m.obj", "w") as f:
    f.write("# Weimar buildings, flat bottom at bilinear DGM height at centroid, EPSG:25832\n")
    for ft in gj["features"]:
        g = transform(tr, shape(ft["geometry"]))
        h = ft["properties"].get("height") or 0
        cen = g.centroid
        z0 = height_at(cen.x, cen.y)
        if np.isnan(z0) or h <= 0:
            skipped += 1; continue
        polys = list(g.geoms) if g.geom_type == "MultiPolygon" else [g]
        vs, fs = [], []
        for p in polys:
            p = shapely.set_precision(p.buffer(0), 0.001) if not p.is_valid else p
            if p.is_empty: continue
            p = shapely.geometry.polygon.orient(p, 1.0)
            for ring_is_ext, ring in [(True, p.exterior)]:
                pts = list(ring.coords)[:-1]
                n = len(pts); base = len(vs)
                vs += [(x, y, z0) for x, y in pts] + [(x, y, z0 + h) for x, y in pts]
                for i in range(n):               # walls, outward normals (exterior CCW)
                    j = (i + 1) % n
                    fs += [(base + i, base + j, base + n + j), (base + i, base + n + j, base + n + i)]
            for t3 in shapely.constrained_delaunay_triangles(p).geoms:   # roof + floor
                cs = list(t3.exterior.coords)[:-1]
                bi = len(vs)
                vs += [(x, y, z0 + h) for x, y in cs] + [(x, y, z0) for x, y in cs]
                fs += [(bi, bi + 1, bi + 2), (bi + 5, bi + 4, bi + 3)]
        if not fs: skipped += 1; continue
        f.write(f"o {ft['properties']['buildingId']}\n")
        for x, y, z in vs: f.write(f"v {x:.2f} {y:.2f} {z:.2f}\n")
        # OBJ indices are global, 1-based
        for q in fs: f.write(f"f {q[0]+nv+1} {q[1]+nv+1} {q[2]+nv+1}\n")
        nb += 1; nv += len(vs); nf += len(fs)
        stats.append((cen.x, cen.y, z0))
print("buildings", nb, "skipped", skipped, "verts", nv, "faces", nf)
np.save(OUT.parent / "_tmp_base_z.npy", np.array(stats))
for p in OUT.glob("*.obj"): print(p.name, round(p.stat().st_size / 1e6, 1), "MB")
