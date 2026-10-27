# City Data Explorer — Weimar

A browser-based digital twin of Weimar (Thuringia, Germany), built entirely from open geodata.
Layers can be inspected on interactive 2D/3D maps and exported for a selected area as GeoJSON
or Rhino 3DM for further analysis in Rhino / Grasshopper.

**Open it:** https://bauhaus-infau.github.io/weimar-city-data-explorer/

## Maps

- **Urban Morphology** — buildings, parcels, mobility, districts
- **Simplified Model for Analysis** — precise geometry for Rhino / Grasshopper, incl. the
  Space Syntax road networks and counting gates
- **Detailled Buildings (LOD2)** — buildings with roofs (WEGA LoD2)
- **Natural Elements** — water, geology, terrain, protected areas

Each map has a layer list on the left (eye icon toggles visibility), a 2D/3D switch and
tools on the right: export (area selection → GeoJSON zip or 3DM), scale grid, PNG/PDF image
export, legend.

## Run locally

The page must be served over HTTP (not opened as `file://`):

```
python -m http.server 8000
```

then open http://localhost:8000/.

## Repository layout

```
index.html, app.js, detailed.js, style.css   the web application (no build step)
layers/layers.json                           layer manifest: sources, layers, sidebar, palettes
layers/base_border*.json                     standard export areas
layers/data/                                 prepared vector data (GeoJSON, LoD2 binary)
terrain/weimar-dgm/                          terrain tiles (Terrarium PNG) built from DGM1
materials/                                   counting sheets for the Space Syntax survey
tools/                                       Python scripts that downloaded and prepared the data
```

`tools/` documents how the data in `layers/data/` and `terrain/` was produced. The scripts
expect the original working folders (`data/raw`, `data/processed`), which are not part of this
repository, so they are a reference rather than a one-click rebuild.

## Data sources and licences

- Geoportal Thüringen / GDI-Th (ALKIS, ATKIS, DGM1, LoD2 CityGML, WMS services) —
  © GDI-Th, [Datenlizenz Deutschland – Namensnennung 2.0](https://www.govdata.de/dl-de/by-2-0)
- OpenStreetMap — © OpenStreetMap contributors, [ODbL](https://opendatacommons.org/licenses/odbl/)
- Basemap — © CARTO, © OpenStreetMap contributors

The attribution for each individual layer is stored in `layers/layers.json` and shown in the
application.
