"""TASK_23 — rewrite DEM GeoTIFFs with the fixed tag layout (same pixels, new tags)."""
import sys
from pathlib import Path
import numpy as np, tifffile
sys.path.insert(0, str(Path(__file__).parent))
from build_dgm_terrain import write_geotiff_25832, NODATA
R = Path(__file__).resolve().parent.parent / "data/processed"
for f in (R/"dgm_weimar/weimar_dgm_25832.tif", R/"dgm_weimar_lightweight/weimar_dgm_25832_15m.tif",
          R/"dgm_weimar_lightweight/weimar_dgm_25832_30m.tif"):
    with tifffile.TiffFile(f) as t:
        pg = t.pages[0]; a = pg.asarray()
        px = pg.tags["ModelPixelScaleTag"].value[0]; ulx, uly = pg.tags["ModelTiepointTag"].value[3:5]
    a = np.where(a == NODATA, np.nan, a).astype(np.float32)
    write_geotiff_25832(f, a, ulx, uly, px)
    print(f.name, a.shape, f.stat().st_size)
