#!/usr/bin/env python3
"""
merge_manual_center_roads.py — TASK_19: fold the user's manually-digitised
centre-city road additions (drawn in QGIS) into the full-city ATKIS road
layers, without duplicating anything.

Background
----------
The user hand-detailed three road layers in QGIS, in the city-centre area
only (not the full Weimar boundary), starting from an export of the current
full-city layer and *adding* new lines to it — never editing existing
geometry. The edited files live in `New roads/` and contain a mix of:
  - untouched originals from the centre area (same
    `eindeutigerObjektidentifikator` as the full-city layer), and
  - brand-new hand-drawn lines (id is null/empty — never existed in ATKIS).

Merge rule (TASK_19), per layer:
  1. Start from the current full-city `web/layers/data/weimar_atkis_<type>.geojson`.
  2. Any full-city feature whose id also appears in `New roads/...` is
     DROPPED (the New roads version replaces it — the user's edit wins even
     if the geometry differs slightly, e.g. snapping).
  3. ALL features from `New roads/...` are kept (this re-adds the replaced
     ones with the user's version, plus every feature with no id match,
     which includes both her real new lines and any untouched-but-unmatched
     feature — id-less features always fall in this "kept anyway" bucket).
  4. Full-city features whose id has no match in `New roads/...` are kept
     unchanged (everything outside the edited centre area).

Net effect: outside the centre, unchanged; inside the centre, the user's
version (including her additions); nothing duplicated.

Then: run every result through `geojson_clean.py` (no-op for LineStrings,
kept for pattern consistency / future polygon reuse of this script), and a
two-part overlap QA — geometry-key exact-duplicate scan + Hausdorff
near-duplicate scan (pair distance < 0.5 m, symmetric Hausdorff < 1.0 m) —
across the FINAL merged feature set of each layer.

Usage:
  python tools/merge_manual_center_roads.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pyproj
from shapely.geometry import shape
from shapely.ops import transform as shp_transform
from shapely.strtree import STRtree

sys.path.insert(0, str(Path(__file__).resolve().parent))
from geojson_clean import clean_features  # noqa: E402

TO_25832 = pyproj.Transformer.from_crs("EPSG:4326", "EPSG:25832", always_xy=True)
_to25832 = lambda geom: shp_transform(lambda x, y, z=None: TO_25832.transform(x, y), geom)

REPO_ROOT = Path(__file__).resolve().parents[1]
NEW_ROADS_DIR = REPO_ROOT / "New roads"
LAYER_DIR = REPO_ROOT / "web" / "layers" / "data"

TYPES = {
    "strassenachse": "atkis_verkehr_strassenachse.geojson",
    "fahrwegachse": "atkis_verkehr_fahrwegachse.geojson",
    "wegpfadsteig": "atkis_verkehr_wegpfadsteig.geojson",
}

ID_FIELD = "eindeutigerObjektidentifikator"


def load_fc(path: Path) -> list[dict]:
    fc = json.loads(path.read_text(encoding="utf-8"))
    return fc["features"]


def feat_id(feat: dict) -> str | None:
    v = (feat.get("properties") or {}).get(ID_FIELD)
    return v if v else None


def geom_key(geom: dict):
    def r(pt):
        return (round(pt[0], 7), round(pt[1], 7))

    t = geom["type"]
    c = geom["coordinates"]
    if t == "Point":
        return (t, r(c))
    if t == "LineString":
        pts = tuple(r(p) for p in c)
        return (t, min(pts, pts[::-1]))
    if t == "Polygon":
        rings = [tuple(sorted(r(p) for p in ring)) for ring in c]
        return (t, tuple(sorted(rings)))
    return (t, json.dumps(c, sort_keys=True))


def overlap_qa(label: str, features: list[dict]) -> bool:
    """Geometry-key exact-dup scan + Hausdorff near-dup scan. Returns True if clean."""
    print(f"  -- overlap QA: {label} ({len(features)} features) --")

    # 1) exact geometry-key duplicates (direction-independent)
    by_key: dict = {}
    for i, fe in enumerate(features):
        by_key.setdefault(geom_key(fe["geometry"]), []).append(i)
    exact_dups = {k: v for k, v in by_key.items() if len(v) > 1}
    if exact_dups:
        print(f"     !! {len(exact_dups)} exact-geometry-key group(s) with >1 feature")
        for k, idxs in list(exact_dups.items())[:10]:
            ids = [feat_id(features[i]) for i in idxs]
            print(f"        {ids}")
    else:
        print("     geometry-key scan: clean, no exact duplicates")

    # 2) Hausdorff near-duplicate scan (EPSG:25832 meters, pair dist < 0.5 m,
    #    symmetric Hausdorff < 1.0 m — same thresholds as TASK_14's QA)
    geoms_m = [_to25832(shape(fe["geometry"])) for fe in features]
    tree = STRtree(geoms_m)
    flagged = []
    for i, gi in enumerate(geoms_m):
        for j in tree.query(gi.buffer(0.5)):
            j = int(j)
            if j <= i:
                continue
            gj = geoms_m[j]
            if gi.distance(gj) >= 0.5:
                continue
            hd = max(gi.hausdorff_distance(gj), gj.hausdorff_distance(gi))
            if hd < 1.0:
                flagged.append((i, j, round(hd, 3)))
    if flagged:
        print(f"     !! {len(flagged)} Hausdorff near-duplicate pair(s)")
        for i, j, hd in flagged[:10]:
            print(f"        idx {i} <-> {j}  hausdorff={hd} m  ids={feat_id(features[i])!r}/{feat_id(features[j])!r}")
    else:
        print("     Hausdorff scan: clean, 0 near-duplicate pairs")

    return not exact_dups and not flagged


def main() -> int:
    import argparse

    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--only", choices=sorted(TYPES), default=None,
                     help="run a single road type instead of all three")
    ap.add_argument("--base", type=Path, default=None,
                     help="override the base full-city file for --only "
                          "(default: the current live web/layers/data/weimar_atkis_<type>.geojson)")
    args = ap.parse_args()

    types = {args.only: TYPES[args.only]} if args.only else TYPES

    overall_clean = True
    print("=== merge manual centre-city road additions ===\n")

    for slug, fname in types.items():
        city_path = args.base if (args.only and args.base) else LAYER_DIR / f"weimar_atkis_{slug}.geojson"
        new_path = NEW_ROADS_DIR / fname

        city_feats = load_fc(city_path)
        new_feats = load_fc(new_path)

        new_ids = {fid for fid in (feat_id(f) for f in new_feats) if fid is not None}

        kept_from_city = [f for f in city_feats if feat_id(f) not in new_ids]
        n_replaced = len(city_feats) - len(kept_from_city)

        merged = kept_from_city + new_feats
        n_added = len(new_feats) - n_replaced  # features from New roads with no id match in city file

        print(f"[{slug}]")
        print(f"  full-city (before):     {len(city_feats)}")
        print(f"  New roads file:         {len(new_feats)}")
        print(f"  replaced (id matched):  {n_replaced}")
        print(f"  added (no id match):    {n_added}")
        print(f"  merged (before clean):  {len(merged)}")

        kept, summary = clean_features(merged, label=slug)

        clean = overlap_qa(slug, kept)
        overall_clean &= clean

        out_path = LAYER_DIR / f"weimar_atkis_{slug}.geojson"
        out_path.write_text(
            json.dumps({"type": "FeatureCollection", "features": kept}, ensure_ascii=False),
            encoding="utf-8",
        )
        print(f"  -> wrote {out_path.relative_to(REPO_ROOT)}: {len(kept)} features\n")

    print("=== overlap QA verdict: " + ("ALL CLEAN" if overall_clean else "PROBLEMS FOUND, see above") + " ===")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
