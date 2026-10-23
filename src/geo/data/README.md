# Bundled geographic data

`communes.csv.gz` and `departements.csv` come from the French government's
[geo.api.gouv.fr](https://geo.api.gouv.fr) (data from INSEE and IGN, published by
Etalab under the [Licence Ouverte 2.0](https://www.etalab.gouv.fr/licence-ouverte-open-licence/)).
They are regenerated with `python scripts/build_geo_data.py`; do not edit them by hand.

| File | Columns |
|---|---|
| `communes.csv.gz` | `insee, name, postcodes (space-separated), department, region, population, lat, lon` |
| `departements.csv` | `code, name, region` |
