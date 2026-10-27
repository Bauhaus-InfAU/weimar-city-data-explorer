"""Pedestrian counting gates (teacher's table) -> web/layers/data/weimar_counting_gates.geojson.

Input : data/raw/teacher_ss_model/Calculations/Pedestrian counting (1).csv  (gate, X, Y in a local frame + counts)
Frame : the table is a pure translation of EPSG:25832 (scale 1.000, rotation 0 deg - fitted on the 120 gates).
        SHIFT is the translation, cross-checked two ways: gate A5 sits at the Belvederer Allee / Steinbrueckenweg junction
        (664240, 5648569) and the best match of all gates against weimar_ss_*_city2 gives the same shift to ~5 m.
        Gates are hand-placed counting lines, so ~10 m from the network is normal; coordinates are NOT snapped.
Output: Point per gate, properties = every CSV column (empty cells omitted) + `zone` (letter of the gate name).
"""
import csv, json
from pathlib import Path
from pyproj import Transformer

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "data/raw/teacher_ss_model/Calculations/Pedestrian counting (1).csv"
OUT = ROOT / "web/layers/data/weimar_counting_gates.geojson"
SHIFT = (655274.3, 5644835.1)
FR = Transformer.from_crs(25832, 4326, always_xy=True).transform

def num(v):
    f = float(v); return int(f) if f.is_integer() else round(f, 3)

feats = []
for r in csv.DictReader(open(SRC, encoding="utf8")):
    name = r.pop("Counting Gate").strip()
    x, y = float(r.pop("X")), float(r.pop("Y"))
    props = {"gate": name, "zone": name[0]}
    props.update({k: num(v) for k, v in r.items() if v not in ("", None)})
    lng, lat = FR(x + SHIFT[0], y + SHIFT[1])
    feats.append({"type": "Feature", "properties": props, "geometry": {"type": "Point", "coordinates": [round(lng, 7), round(lat, 7)]}})
json.dump({"type": "FeatureCollection", "features": feats}, open(OUT, "w", encoding="utf8"), ensure_ascii=False, separators=(",", ":"))
print(OUT, len(feats))
