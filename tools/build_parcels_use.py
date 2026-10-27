"""Parcels use layer: web/layers/data/weimar_parcels.geojson -> weimar_parcels_use.geojson.

* adds `useCategory` (grouped from the ALKIS "Tatsaechliche Nutzung" text in `usage`)
* clips every parcel to the Weimar administrative boundary (parcels in the AOI box
  that lie outside the city are dropped, parcels crossing the border are cut).
  `area` / `areaSource` stay the official values of the uncut parcel.

Run: python tools/build_parcels_use.py
"""
import json
from pathlib import Path

from shapely.geometry import shape, mapping
from shapely.prepared import prep

ROOT = Path(__file__).resolve().parent.parent / "web" / "layers" / "data"

CATEGORY = {
    "Wohnbauflaeche": "Residential",
    "Flaeche Gemischter Nutzung": "Mixed",
    "Industrie Und Gewerbeflaeche": "Industry & Commerce",
    "Flaeche Besonderer Funktionaler Praegung": "Special function",
    "Sport Freizeit Und Erholungsflaeche": "Sport & Recreation",
    "Landwirtschaft": "Agriculture",
    "Wald": "Forest",
    "Gehoelz": "Trees & shrubs",
    "Strassenverkehr": "Road & path",
    "Weg": "Road & path",
    "Platz": "Road & path",
    "Bahnverkehr": "Rail & air",
    "Flugverkehr": "Rail & air",
    "Fliessgewaesser": "Water",
    "Stehendes Gewaesser": "Water",
    "Unland Vegetationslose Flaeche": "Barren & mining",
    "Tagebau Grube Steinbruch": "Barren & mining",
    "Friedhof": "Cemetery",
}


def r5(coords):
    if isinstance(coords[0], (int, float)):
        return [round(coords[0], 6), round(coords[1], 6)]
    return [r5(c) for c in coords]


def main():
    boundary = shape(json.load(open(ROOT / "weimar_boundary.geojson", encoding="utf-8"))["features"][0]["geometry"])
    pb = prep(boundary)
    src = json.load(open(ROOT / "weimar_parcels.geojson", encoding="utf-8"))
    out, dropped, cut = [], 0, 0
    for f in src["features"]:
        g = shape(f["geometry"])
        if not g.is_valid:
            g = g.buffer(0)
        if not pb.intersects(g):
            dropped += 1
            continue
        if not pb.contains(g):
            g = g.intersection(boundary)
            cut += 1
            if g.is_empty or g.geom_type not in ("Polygon", "MultiPolygon"):
                polys = [p for p in getattr(g, "geoms", []) if p.geom_type == "Polygon"]
                if not polys:
                    dropped += 1
                    continue
                from shapely.geometry import MultiPolygon
                g = MultiPolygon(polys)
        p = dict(f["properties"])
        p["useCategory"] = CATEGORY.get(p.get("usage"), "Other")
        geom = mapping(g)
        geom["coordinates"] = r5(geom["coordinates"])
        out.append({"type": "Feature", "properties": p, "geometry": geom})
    fc = {"type": "FeatureCollection", "features": out}
    with open(ROOT / "weimar_parcels_use.geojson", "w", encoding="utf-8") as fh:
        json.dump(fc, fh, ensure_ascii=False, separators=(",", ":"))
    from collections import Counter
    print(len(src["features"]), "->", len(out), "kept;", dropped, "dropped,", cut, "cut")
    print(Counter(f["properties"]["useCategory"] for f in out).most_common())


if __name__ == "__main__":
    main()
