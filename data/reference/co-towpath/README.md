# C&O Canal towpath reference geometry

- **Source:** National Park Service Public Trails dataset
- **FeatureServer:** https://mapservices.nps.gov/arcgis/rest/services/NationalDatasets/NPS_Public_Trails_Geographic/FeatureServer/0
- **Source filter:** `UNITCODE = 'CHOH'`
- **Source retrieved:** 2026-08-31
- **Source CHOH features:** 359
- **Selected towpath features:** 226

Canawler selects records with the NPS towpath mile-segment naming convention and expected unrestricted, public, existing, extant, pedestrian-and-bicycle trail attributes. It also explicitly recognizes the NPS `Widewater Waste Weir Bridge 3100-059S` record. Unrecognized Towpath-labeled or Towpath-named records make the build fail instead of being guessed into the route.

Processing converts the server's GeoJSON response to a single WGS84 (EPSG:4326) LineString, removes exact duplicate source lines, joins disconnected source chains only across gaps of 50 metres or less, extracts the unique endpoint-to-endpoint route, and orients it from Georgetown, Washington, DC, to Cumberland, Maryland. Coordinates are not simplified and the geometric length is not forced to the canal's 184.5-mile milepost value.

`towpath.geojson` is derived data intended for Canawler analysis, not navigation. The National Park Service is acknowledged as the source of the underlying trail data.
