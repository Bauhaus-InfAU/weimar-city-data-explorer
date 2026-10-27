"""TASK_22 — lightweight DEM for Rhino: block-mean resample of the 1 m DGM.
NODATA (-9999) is excluded from block means; a block is NODATA only if fully empty."""
import sys
from pathlib import Path
import numpy as np
import tifffile

sys.path.insert(0, str(Path(__file__).parent))
from build_dgm_terrain import write_geotiff_25832, NODATA

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "data/processed/dgm_weimar/weimar_dgm_25832.tif"
OUT = ROOT / "data/processed/dgm_weimar_lightweight"


def build(step):
    with tifffile.TiffFile(SRC) as t:
        p = t.pages[0]
        a = p.asarray()
        ulx, uly = p.tags["ModelTiepointTag"].value[3:5]
    H, W = a.shape
    nh, nw = -(-H // step), -(-W // step)
    pad = np.full((nh * step, nw * step), np.nan, np.float32)
    pad[:H, :W] = np.where((a == NODATA) | ~np.isfinite(a), np.nan, a)
    blocks = pad.reshape(nh, step, nw, step)
    valid = ~np.isnan(blocks)
    cnt = valid.sum(axis=(1, 3))
    s = np.where(valid, blocks, 0).sum(axis=(1, 3), dtype=np.float64)
    out = np.where(cnt > 0, s / np.maximum(cnt, 1), np.nan).astype(np.float32)
    path = OUT / f"weimar_dgm_25832_{step}m.tif"
    write_geotiff_25832(path, out, ulx, uly, float(step))
    mb = path.stat().st_size / 1e6
    print(f"{step} m: {out.shape[1]}x{out.shape[0]} = {out.size:,} px, {mb:.2f} MB, "
          f"px x{a.size / out.size:.0f}, size x{SRC.stat().st_size / path.stat().st_size:.0f}, "
          f"nodata {np.isnan(out).sum()}, zmin {np.nanmin(out):.1f} zmax {np.nanmax(out):.1f}")


for s in (15, 30):
    build(s)
