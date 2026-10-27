"""Building footprints (whole city) -> Rhino .3dm, EPSG:25832 absolute, metres.

Same conventions as build3dmExport() in web/app.js: one layer "Building footprints",
flat single-face Brep at z = 0, object name = buildingId, attributes as User Text.
Usage: python tools/export_footprints_3dm.py [--local-origin [E N]]
  --local-origin: subtract (E, N) from all coordinates; default E 663000, N 5651000 =
  the platform's "local origin" for the Base border area (centre rounded to 1 km).
"""
import json, sys
from pathlib import Path
import rhino3dm as r
from pyproj import Transformer
from shapely.geometry import Polygon

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "web/layers/data/weimar_building_footprints.geojson"
ARGS = sys.argv[1:]
LOCAL = "--local-origin" in ARGS
OX, OY = 0, 0
if LOCAL:
    OX, OY = 663000, 5651000
    nums = [a for a in ARGS if a.lstrip("-").isdigit()]
    if len(nums) == 2:
        OX, OY = int(nums[0]), int(nums[1])
OUT = ROOT / ("output/rhino/weimar_building_footprints_local.3dm" if LOCAL else "output/rhino/weimar_building_footprints.3dm")
KEYS = ["usage", "useCategory", "height", "floors", "buildingId", "area", "areaSource"]

tr = Transformer.from_crs(4326, 25832, always_xy=True)

def ring_xy(ring):
    pts = [(X - OX, Y - OY) for X, Y in (tr.transform(x, y) for x, y in ring)]
    if len(pts) > 1 and pts[0] == pts[-1]:
        pts = pts[:-1]
    return pts

def curve(pts):
    pl = r.Polyline(len(pts) + 1)
    for x, y in pts:
        pl.Add(x, y, 0.0)
    pl.Add(pts[0][0], pts[0][1], 0.0)
    return pl.ToPolylineCurve()

def build_poly(rings):
    """rings: [outer, *holes] in 25832. Returns Brep or None."""
    rs = [ring_xy(x) for x in rings]
    if len(rs[0]) < 3:
        return None
    poly = Polygon(rs[0], [h for h in rs[1:] if len(h) >= 3])
    if not poly.is_valid or poly.area < 1e-6:
        return None
    plane = r.Plane.WorldXY()
    if len(rs) == 1:
        return r.Brep.CreateTrimmedPlane(plane, curve(rs[0]))
    # holes: bottom cap of a thin extrusion = one planar face with inner loops
    ex = r.Extrusion.Create(curve(rs[0]), 1.0, True)
    if not ex:
        return None
    for h in rs[1:]:
        if len(h) >= 3 and not ex.AddInnerProfile(curve(h)):
            return None
    brep = ex.ToBrep(True)
    for f in brep.Faces:
        d = f.DuplicateFace(False)
        if abs(d.GetBoundingBox().Max.Z) < 1e-9:
            return d
    return None

d = json.load(open(SRC, encoding="utf-8"))
doc = r.File3dm()
doc.Settings.ModelUnitSystem = r.UnitSystem.Meters
lay = r.Layer()
lay.Name = "Building footprints"
li = doc.Layers.Add(lay)
doc.Strings["crs"] = "EPSG:25832"
doc.Strings["originEasting"] = str(OX)
doc.Strings["originNorthing"] = str(OY)

n = skipped = 0
skipped_ids = []
for f in d["features"]:
    g = f["geometry"]
    parts = [g["coordinates"]] if g["type"] == "Polygon" else g["coordinates"]
    p = f["properties"]
    for rings in parts:
        b = build_poly(rings)
        if b is None:
            skipped += 1
            skipped_ids.append(p.get("buildingId"))
            continue
        a = r.ObjectAttributes()
        a.LayerIndex = li
        if p.get("buildingId"):
            a.Name = str(p["buildingId"])
        for k in KEYS:
            v = p.get(k)
            if v is not None and v != "":
                a.SetUserString(k, str(v))
        doc.Objects.AddBrep(b, a)
        n += 1

OUT.parent.mkdir(parents=True, exist_ok=True)
ok = doc.Write(str(OUT), 7)
print("written" if ok else "WRITE FAILED", OUT, "objects", n, "skipped", skipped, skipped_ids[:20])
