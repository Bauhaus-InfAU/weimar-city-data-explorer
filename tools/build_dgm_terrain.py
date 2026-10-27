"""
TASK 10 — turn the downloaded DGM1 tiles into (a) one canonical GeoTIFF DEM for
the Weimar AOI and (b) a Terrarium-encoded raster-dem tile pyramid for MapLibre.

Why this and not rasterio / rio-rgbify (what TASK_10 asked for): on this machine
Smart App Control is in enforcement mode
(HKLM\\SYSTEM\\CurrentControlSet\\Control\\CI\\Policy\\VerifiedAndReputablePolicyState = 1)
and blocks the freshly-built native .pyd / bundled GDAL DLLs that ship in the
rasterio wheel:
    ImportError: DLL load failed while importing _base:
    An Application Control policy has blocked this file.
GDAL CLI is not installed either. numpy, pyproj (native PROJ DLL loads fine),
Pillow and tifffile (pure-Python) are all allowed, so the whole job is done with
just those. .xyz parsing is trivial ASCII ("E N Z"), reprojection is pyproj,
Terrarium encoding is arithmetic — no hand-rolled raster decoding anywhere.

Inputs :  data/raw/dgm_th/dgm_2014-2019/*.zip        (each -> one 1000x1000 .xyz, 1 m, EPSG:25832)
Outputs:  data/processed/dgm_weimar/weimar_dgm_25832.tif   Float32 GeoTIFF, EPSG:25832, AOI-clipped
          web/terrain/weimar-dgm/<z>/<x>/<y>.png            Terrarium PNG raster-dem tiles (EPSG:3857)
          web/terrain/weimar-dgm/metadata.json              bounds / zoom / encoding summary

Run:  python tools/build_dgm_terrain.py
"""

from __future__ import annotations

import functools
import json
import math
import re
import sys
import time
import zipfile
from pathlib import Path

import numpy as np
from PIL import Image
from pyproj import Transformer

print = functools.partial(print, flush=True)  # noqa: A001 — unbuffered progress

# --------------------------------------------------------------------------- #
# config
# --------------------------------------------------------------------------- #
ROOT = Path(__file__).resolve().parents[1]
RAW_DIR = ROOT / "data" / "raw" / "dgm_th" / "dgm_2014-2019"
DEM_OUT = ROOT / "data" / "processed" / "dgm_weimar" / "weimar_dgm_25832.tif"
TILE_DIR = ROOT / "web" / "terrain" / "weimar-dgm"

# Weimar AOI (DATA.md), EPSG:4326  — west, south, east, north
AOI_W, AOI_S, AOI_E, AOI_N = 11.2365, 50.91673, 11.40122, 51.03776

ZOOM_MIN, ZOOM_MAX = 10, 16
TILE_PX = 256
NODATA = -9999.0

# Web Mercator constants
MERC_R = 6378137.0
MERC_C = 2.0 * math.pi * MERC_R          # full circumference
MERC_ORIGIN = -MERC_C / 2.0             # x/y of the top-left of the world


# --------------------------------------------------------------------------- #
# part 0 — enumerate tiles, filter to AOI, report
# --------------------------------------------------------------------------- #
def enumerate_tiles():
    tiles = []
    for zp in sorted(RAW_DIR.glob("dgm1_*_1_th_*.zip")):
        m = re.match(r"dgm1_(\d+)_(\d+)_1_th", zp.name)
        if not m:
            continue
        ekm, nkm = int(m.group(1)), int(m.group(2))
        tiles.append((ekm, nkm, zp))
    return tiles


def aoi_bounds_25832():
    t = Transformer.from_crs(4326, 25832, always_xy=True)
    xs, ys = [], []
    for lon, lat in ((AOI_W, AOI_S), (AOI_W, AOI_N), (AOI_E, AOI_S), (AOI_E, AOI_N)):
        x, y = t.transform(lon, lat)
        xs.append(x)
        ys.append(y)
    return min(xs), min(ys), max(xs), max(ys)


# --------------------------------------------------------------------------- #
# part 1 — mosaic
# --------------------------------------------------------------------------- #
def read_xyz_z(zp: Path, ekm: int, nkm: int) -> np.ndarray:
    """Return the tile's 1000x1000 Float32 elevation grid, row 0 = north."""
    with zipfile.ZipFile(zp) as z:
        name = next(n for n in z.namelist() if n.endswith(".xyz"))
        raw = z.read(name)
    # fast numeric parse: bytes.split() is C-level, ~7x quicker than np.fromstring
    arr = np.array(raw.split(), dtype=np.float64)
    if arr.size != 3_000_000:
        raise ValueError(f"{zp.name}: expected 3,000,000 numbers, got {arr.size}")
    arr = arr.reshape(-1, 3)
    e, n, hh = arr[:, 0], arr[:, 1], arr[:, 2]
    # sanity: the tile must sit where its name says (integer-metre grid)
    if not (e.min() == ekm * 1000 and e.max() == ekm * 1000 + 999):
        raise ValueError(f"{zp.name}: E range {e.min()}..{e.max()} != {ekm*1000}..{ekm*1000+999}")
    if not (n.min() == nkm * 1000 and n.max() == nkm * 1000 + 999):
        raise ValueError(f"{zp.name}: N range {n.min()}..{n.max()} != {nkm*1000}..{nkm*1000+999}")
    # .xyz is row-major from the NW corner: first 1000 rows share N = nkm*1000+999,
    # E ascending; so reshape(1000, 1000) already gives row 0 = north, col 0 = west.
    g = hh.reshape(1000, 1000).astype(np.float32)
    # guard against a differently ordered file
    if n[0] != nkm * 1000 + 999 or e[0] != ekm * 1000:
        # rebuild explicitly from coordinates
        ci = (e - ekm * 1000).astype(np.int32)
        ri = (nkm * 1000 + 999 - n).astype(np.int32)
        g = np.full((1000, 1000), np.nan, np.float32)
        g[ri, ci] = hh.astype(np.float32)
    return g


def build_mosaic(tiles):
    ekms = sorted({t[0] for t in tiles})
    nkms = sorted({t[1] for t in tiles})
    min_e, max_e = ekms[0], ekms[-1]
    min_n, max_n = nkms[0], nkms[-1]
    W = (max_e - min_e + 1) * 1000
    H = (max_n - min_n + 1) * 1000
    print(f"  mosaic grid  : {W} x {H} px  ({W*H*4/1e6:.0f} MB Float32)")
    mosaic = np.full((H, W), np.nan, np.float32)

    # pixel-CENTRE coord of mosaic[0, 0]
    e0 = float(min_e * 1000)              # west-most column centre
    n0 = float(max_n * 1000 + 999)        # north-most row centre

    t0 = time.time()
    for i, (ekm, nkm, zp) in enumerate(tiles, 1):
        g = read_xyz_z(zp, ekm, nkm)
        r_off = (max_n - nkm) * 1000
        c_off = (ekm - min_e) * 1000
        mosaic[r_off:r_off + 1000, c_off:c_off + 1000] = g
        if i % 25 == 0 or i == len(tiles):
            print(f"  ..{i:3d}/{len(tiles)} tiles  ({time.time()-t0:.0f}s)")
    return mosaic, e0, n0


def fill_small_gaps(mosaic, iters=3):
    """Nearest-ish fill of interior NaN holes (tile-grid corners) by iterative
    4-neighbour dilation. The large un-covered eastern wedge stays NaN — those
    tiles are simply not emitted."""
    m = mosaic
    for _ in range(iters):
        nan = np.isnan(m)
        if not nan.any():
            break
        filled = m.copy()
        for dr, dc in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            shifted = np.roll(m, (dr, dc), axis=(0, 1))
            take = nan & ~np.isnan(shifted)
            filled[take] = shifted[take]
        m = filled
    return m


def write_geotiff_25832(path: Path, arr: np.ndarray, ulx: float, uly: float, px=1.0):
    """Float32 GeoTIFF with the GeoKeys for EPSG:25832. `ulx/uly` = map coord of
    the top-left pixel CORNER (RasterPixelIsArea)."""
    import tifffile

    data = np.where(np.isnan(arr), np.float32(NODATA), arr).astype(np.float32)

    CITATION = "ETRS89 / UTM zone 32N|"
    geo_keys = (
        1, 1, 0, 4,
        1024, 0, 1, 1,        # GTModelTypeGeoKey        = ModelTypeProjected
        1025, 0, 1, 1,        # GTRasterTypeGeoKey       = RasterPixelIsArea
        3072, 0, 1, 25832,    # ProjectedCSTypeGeoKey    = ETRS89 / UTM zone 32N
        3073, 34737, len(CITATION), 0,  # PCSCitationGeoKey -> GeoAsciiParams[0:n]
    )
    extratags = [
        (33550, "d", 3, (px, px, 0.0), True),                     # ModelPixelScaleTag
        (33922, "d", 6, (0.0, 0.0, 0.0, ulx, uly, 0.0), True),    # ModelTiepointTag
        (34735, "H", len(geo_keys), geo_keys, True),              # GeoKeyDirectoryTag
        (34737, "s", 0, CITATION, True),   # GeoAsciiParamsTag
        (42113, "s", 0, str(int(NODATA)), True),                  # GDAL_NODATA
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    last = None
    for kw in (dict(compression="lzw", predictor=3),
               dict(compression="zlib", predictor=3),
               dict(compression="zlib"),
               {}):
        try:
            tifffile.imwrite(
                str(path), data, photometric="minisblack", metadata=None,
                extratags=extratags, **kw,
            )
            return kw
        except Exception as exc:  # noqa: BLE001 — try the next fallback
            last = exc
            print(f"    (tifffile {kw} failed: {exc}; trying next)")
    raise last


# --------------------------------------------------------------------------- #
# part 2 — Terrarium raster-dem tiles
# --------------------------------------------------------------------------- #
def lonlat_to_merc(lon, lat):
    x = MERC_R * math.radians(lon)
    y = MERC_R * math.log(math.tan(math.pi / 4 + math.radians(lat) / 2))
    return x, y


def bilinear_nan(mosaic, col_f, row_f):
    """Vectorised bilinear sample; result is NaN where all 4 corners are NaN."""
    H, W = mosaic.shape
    c0 = np.floor(col_f).astype(np.int64)
    r0 = np.floor(row_f).astype(np.int64)
    fx = (col_f - c0).astype(np.float32)
    fy = (row_f - r0).astype(np.float32)
    c0 = np.clip(c0, 0, W - 1); c1 = np.clip(c0 + 1, 0, W - 1)
    r0 = np.clip(r0, 0, H - 1); r1 = np.clip(r0 + 1, 0, H - 1)
    v00 = mosaic[r0, c0]; v01 = mosaic[r0, c1]
    v10 = mosaic[r1, c0]; v11 = mosaic[r1, c1]
    w00 = (1 - fx) * (1 - fy); w01 = fx * (1 - fy)
    w10 = (1 - fx) * fy;       w11 = fx * fy
    out = np.zeros(col_f.shape, np.float32)
    wsum = np.zeros(col_f.shape, np.float32)
    for v, w in ((v00, w00), (v01, w01), (v10, w10), (v11, w11)):
        ok = ~np.isnan(v)
        out[ok] += v[ok] * w[ok]
        wsum[ok] += w[ok]
    bad = wsum <= 1e-6
    out[~bad] /= wsum[~bad]
    out[bad] = np.nan
    return out


def terrarium_encode(elev, fill_value=0.0):
    """elev (H,W float, NaN allowed) -> uint8 (H,W,3) Terrarium RGB.
    NaN (no DGM coverage) is filled with `fill_value` — set it to the data floor
    so the un-covered eastern strip steps down gently to the valley level rather
    than dropping a ~280 m cliff to sea level."""
    v = np.where(np.isnan(elev), fill_value, elev) + 32768.0
    v = np.clip(v, 0.0, 65535.996)
    r = np.floor(v / 256.0)
    g = np.floor(v - r * 256.0)
    b = np.floor((v - np.floor(v)) * 256.0)
    return np.stack([r, g, b], axis=-1).astype(np.uint8)


def build_tiles(mosaic, e0, n0):
    fill_value = float(np.nanmin(mosaic))
    to_25832 = Transformer.from_crs(3857, 25832, always_xy=True)
    xw, ys = lonlat_to_merc(AOI_W, AOI_S)
    xe, yn = lonlat_to_merc(AOI_E, AOI_N)
    mx_min, mx_max = min(xw, xe), max(xw, xe)
    my_min, my_max = min(ys, yn), max(ys, yn)

    H, W = mosaic.shape
    total_written = 0
    per_zoom = {}

    for z in range(ZOOM_MIN, ZOOM_MAX + 1):
        span = MERC_C / (2 ** z)                       # tile size in metres
        tx0 = int((mx_min - MERC_ORIGIN) // span)
        tx1 = int((mx_max - MERC_ORIGIN) // span)
        ty0 = int((-my_max + (MERC_C / 2)) // span)    # y grows southward
        ty1 = int((-my_min + (MERC_C / 2)) // span)

        rel = np.arange(TILE_PX, dtype=np.float64)
        written = 0
        t_zoom = time.time()
        for tx in range(tx0, tx1 + 1):
            x_left = MERC_ORIGIN + tx * span
            px_x = x_left + (rel + 0.5) / TILE_PX * span                # (256,)
            for ty in range(ty0, ty1 + 1):
                y_top = (MERC_C / 2) - ty * span
                px_y = y_top - (rel + 0.5) / TILE_PX * span             # (256,)
                gx, gy = np.meshgrid(px_x, px_y)                        # (256,256)
                ex, ny_ = to_25832.transform(gx.ravel(), gy.ravel())
                col_f = (np.asarray(ex) - e0).astype(np.float32)
                row_f = (n0 - np.asarray(ny_)).astype(np.float32)
                inside = (col_f > -1) & (col_f < W) & (row_f > -1) & (row_f < H)
                if inside.mean() < 0.01:
                    continue
                elev = bilinear_nan(mosaic, col_f, row_f).reshape(TILE_PX, TILE_PX)
                if np.isnan(elev).mean() > 0.98:
                    continue
                rgb = terrarium_encode(elev, fill_value)
                out = TILE_DIR / str(z) / str(tx) / f"{ty}.png"
                out.parent.mkdir(parents=True, exist_ok=True)
                Image.fromarray(rgb, "RGB").save(out)
                written += 1
        per_zoom[z] = written
        total_written += written
        print(f"  z{z:<2d}: x[{tx0}..{tx1}] y[{ty0}..{ty1}]  -> {written} tiles"
              f"  ({time.time()-t_zoom:.0f}s)")
    return total_written, per_zoom


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
def main():
    print("TASK 10 — DGM -> canonical GeoTIFF + MapLibre raster-dem tiles\n")

    tiles = enumerate_tiles()
    aw, as_, ae, an = aoi_bounds_25832()
    print("Part 0 — inputs")
    print(f"  raw dir      : {RAW_DIR.relative_to(ROOT)}")
    print(f"  .zip tiles   : {len(tiles)}  (vintage dgm_2014-2019, 1 m)")
    print(f"  AOI (25832)  : E {aw:.0f}..{ae:.0f}  N {as_:.0f}..{an:.0f}")
    # every tile in this folder was already AOI-filtered by download_dgm.py; confirm
    def tile_in_aoi(ekm, nkm):
        return not (ekm * 1000 + 999 < aw or ekm * 1000 > ae
                    or nkm * 1000 + 999 < as_ or nkm * 1000 > an)
    in_aoi = [t for t in tiles if tile_in_aoi(t[0], t[1])]
    out_aoi = [t for t in tiles if not tile_in_aoi(t[0], t[1])]
    print(f"  intersect AOI: {len(in_aoi)}   outside AOI: {len(out_aoi)}")
    if out_aoi:
        print("   ! outside:", [f"{e}_{n}" for e, n, _ in out_aoi])
    if not in_aoi:
        sys.exit("no tiles intersect the AOI — nothing to do")

    print("\nPart 1 — mosaic")
    mosaic, e0, n0 = build_mosaic(in_aoi)
    valid = np.isfinite(mosaic)
    print(f"  elevation    : {np.nanmin(mosaic):.1f} .. {np.nanmax(mosaic):.1f} m"
          f"   ({100*valid.mean():.1f}% of the mosaic rectangle has data)")

    mosaic = fill_small_gaps(mosaic)

    # clip to the AOI rectangle (in 25832)
    H, W = mosaic.shape
    c_lo = max(0, int(math.floor(aw - e0)))
    c_hi = min(W, int(math.ceil(ae - e0)) + 1)
    r_lo = max(0, int(math.floor(n0 - an)))
    r_hi = min(H, int(math.ceil(n0 - as_)) + 1)
    clip = mosaic[r_lo:r_hi, c_lo:c_hi]
    e0c = e0 + c_lo          # centre of clipped col 0
    n0c = n0 - r_lo          # centre of clipped row 0
    ulx = e0c - 0.5          # top-left pixel CORNER
    uly = n0c + 0.5
    print(f"  AOI clip     : {clip.shape[1]} x {clip.shape[0]} px")

    if "--skip-dem" in sys.argv and DEM_OUT.exists():
        print(f"\n  --skip-dem: keeping existing {DEM_OUT.relative_to(ROOT)}")
    else:
        print("\n  writing canonical DEM ...")
        used = write_geotiff_25832(DEM_OUT, clip, ulx, uly)
        mb = DEM_OUT.stat().st_size / 1e6
        print(f"  -> {DEM_OUT.relative_to(ROOT)}  ({mb:.1f} MB, tifffile {used or 'no-compression'})")

    print("\nPart 2 — Terrarium raster-dem tiles")
    TILE_DIR.mkdir(parents=True, exist_ok=True)
    total, per_zoom = build_tiles(mosaic, e0, n0)
    meta = {
        "name": "Weimar DGM1 (2014-2019) terrain",
        "description": "Terrarium-encoded raster-dem tiles from the Thuringia DGM1 "
                       "(1 m airborne-laserscanning DTM), EPSG:25832 -> 3857. "
                       "TASK 10 prototype: Blue-green Infrastructure viewport only.",
        "encoding": "terrarium",
        "tileSize": TILE_PX,
        "minzoom": ZOOM_MIN,
        "maxzoom": ZOOM_MAX,
        "bounds": [AOI_W, AOI_S, AOI_E, AOI_N],
        "tiles_per_zoom": per_zoom,
        "tiles_total": total,
        "attribution": "© GDI-Th (TLVermGeo), Datenlizenz Deutschland – Namensnennung 2.0",
        "canonical_dem": "data/processed/dgm_weimar/weimar_dgm_25832.tif",
        "source_note": "145 of 178 AOI tiles present on disk; the eastern strip "
                       "(E-km 666 rows 5652-5656, 667-668 all) was not downloaded — "
                       "raster-dem tiles are simply absent there.",
    }
    (TILE_DIR / "metadata.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    print(f"  total tiles  : {total}")
    print(f"  -> {(TILE_DIR / 'metadata.json').relative_to(ROOT)}")
    print("\ndone.")


if __name__ == "__main__":
    main()
