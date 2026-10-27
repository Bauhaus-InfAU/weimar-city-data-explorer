"""Teacher's Space Syntax model (Rhino 3dm, local coords) -> web/layers/data/weimar_ss_{car,pedestrian}_city2.geojson.

Geometry is not modified: every LineCurve becomes one LineString, only translated by SHIFT
(EPSG:25832) and reprojected to WGS84. SHIFT was fitted by matching the model's car lines to
weimar_ss_car_city (stable to 0.1 m in all four quadrants, scale 1.0, no rotation).
"""
import json, sys
import rhino3dm
from pyproj import Transformer

SRC = "data/raw/teacher_ss_model/181018_Weimar LOD1 and Street network_EPSG25832.3dm"
SHIFT = (663652.047, 5649880.847)
LAYERS = {"car": "car", "pedestrian": "pedestrian"}

t = Transformer.from_crs(25832, 4326, always_xy=True)
m = rhino3dm.File3dm.Read(SRC)
out = {k: [] for k in LAYERS}
for o in m.Objects:
    name = m.Layers.FindIndex(o.Attributes.LayerIndex).FullPath
    if name not in out:
        continue
    c = o.Geometry
    a, b = c.PointAtStart, c.PointAtEnd
    pts = [(a.X + SHIFT[0], a.Y + SHIFT[1]), (b.X + SHIFT[0], b.Y + SHIFT[1])]
    length = ((pts[1][0] - pts[0][0]) ** 2 + (pts[1][1] - pts[0][1]) ** 2) ** 0.5
    out[name].append({
        "type": "Feature",
        "properties": {"ssId": f"{name[:3]}{len(out[name]) + 1}", "network": name,
                       "source": "teacher_3dm", "length": round(length, 1)},
        "geometry": {"type": "LineString",
                     "coordinates": [[round(v, 7) for v in t.transform(x, y)] for x, y in pts]},
    })
for name, feats in out.items():
    path = f"web/layers/data/weimar_ss_{name}_city2.geojson"
    json.dump({"type": "FeatureCollection", "features": feats}, open(path, "w"), separators=(",", ":"))
    print(path, len(feats))
