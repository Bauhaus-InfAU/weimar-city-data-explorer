#!/usr/bin/env python3
"""
bake_building_use.py — TASK_39: writes `useCategory` into the building GeoJSON files.

Reads tools/building_use_categories.json (usage text / ALKIS code -> one of 18 categories) and sets
properties.useCategory on every feature of
  web/layers/data/weimar_building_footprints.geojson   (flat footprints, TASK_30)
  web/layers/data/weimar_buildings_footprint_height.geojson   (3D buildings source)
Empty usage -> "Other" (counted as `empty`); usage not in the table -> "Other" (counted as `unmapped`).
Idempotent; rerun after editing the mapping table.  Usage: python tools/bake_building_use.py
"""
import json, sys, collections
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "web" / "layers" / "data"
FILES = ["weimar_building_footprints.geojson", "weimar_buildings_footprint_height.geojson"]
MAP = json.load(open(Path(__file__).with_name("building_use_categories.json"), encoding="utf-8"))
CATS, TEXT = MAP["categories"], MAP["text"]
assert all(c in CATS for c in TEXT.values()), "mapping uses a category outside the list"

def classify(usage):
    if usage in (None, ""): return "Other", "empty"
    cat = TEXT.get(str(usage).strip())
    return (cat, "mapped") if cat else ("Other", "unmapped")

def main():
    report = {}
    for name in FILES:
        path = DATA / name
        fc = json.load(open(path, encoding="utf-8"))
        n = collections.Counter(); why = collections.Counter(); unm = collections.Counter()
        for f in fc["features"]:
            p = f["properties"]
            cat, reason = classify(p.get("usage"))
            p["useCategory"] = cat
            n[cat] += 1; why[reason] += 1
            if reason == "unmapped": unm[p.get("usage")] += 1
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(fc, fh, ensure_ascii=False, separators=(",", ":"))
        report[name] = dict(total=len(fc["features"]), byCategory={c: n[c] for c in CATS}, reasons=dict(why), unmapped=dict(unm))
    print(json.dumps(report, ensure_ascii=False, indent=1))

if __name__ == "__main__":
    main()
