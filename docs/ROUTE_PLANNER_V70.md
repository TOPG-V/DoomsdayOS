# DoomsdayOS v7.0 — StreetZim, multi-stop navigation and road-free trails

The **World Maps** page at `http://127.0.0.1:8765` now accepts **start + up to three stops + destination**. You can click the local map to select a stop, use your browser's GPS (permission required), enter `lat,lon`, or select a matching name from a user-imported offline gazetteer.

> "Unlike traditional offline map apps that ship raster tiles, StreetZim uses vector tiles rendered client-side with MapLibre GL JS. The result: smooth pan, zoom, rotation, and even 3D terrain — all from a single file on your device."

**StreetZim** is a separate, independently developed project ([jasontitus/streetzim](https://github.com/jasontitus/streetzim)). Its self-contained ZIM has its own local MapLibre renderer, offline search, and routing: install a StreetZim `.zim` alongside your other verified archives, start Kiwix and open StreetZim from the library at `http://localhost:8080`. This integration **does not** claim that an ordinary `maps_en_all_*.zim` is a StreetZim archive or that an OSRM graph can be extracted from it automatically.

The **DoomsdayOS workspace planner** supports a separate **local vector MBTiles** map, optional local MapLibre GL JS, local elevation tiles, and independently prepared routing graphs. StreetZim is opened using its own Kiwix renderer; the workspace planner's waypoints do not currently sync into StreetZim's Web Worker.

## Setup: prepare the map renderer and tiles

On the Ubuntu machine, when Internet is available **once**, install the pinned MapLibre JS/CSS assets into the OS application directory:

```bash
sudo /opt/doomsday-firstboot/scripts/setup-maplibre.sh
```

No external scripts or tiles are used by the workspace after that. The archive preserves old-fashioned canvas fallback for raster MBTiles. Enroll an existing OpenMapTiles-schema PBF MBTiles file:

```bash
sudo doomsday-maps tiles /absolute/path/to/region-vector.mbtiles
```

A raster PNG/JPEG MBTiles file is also supported. Tiles **do not automatically cover the whole world**; the browser reports gaps, and no public online map service is contacted. The vector map paints land cover, water, roads, and buildings. Labels require a locally installed style/font package; they are not downloaded at runtime.

**Optional 3D terrain:** Enroll a locally prepared **Mapbox Terrain-RGB encoded** PNG MBTiles file:

```bash
sudo doomsday-maps terrain /absolute/path/terrain-rgb.mbtiles
```

The **3D** button enables available terrain and building extrusions. Incorrectly encoded elevation tiles can distort terrain. StreetZim's own terrain, if included in that archive, works independently through Kiwix.

## Car routing with up to three intermediate stops

Enroll a **locally prepared OSRM driving graph**, extracted from the OpenStreetMap region you want to navigate:

```bash
sudo doomsday-maps router /absolute/path/region-drive.osrm
sudo systemctl enable --now doomsday-osrm.service
```

Then open **World Maps**, select **Car**, enter start/stop/destination, and click **Plan offline route**. The route follows roads contained in the graph, with turn-by-turn maneuvers and distance/time estimates. This uses loopback `127.0.0.1:5000`, not the Internet.

## Walking on pedestrian routes

Create a *separate* OSRM graph using a pedestrian/walking Lua profile for the area, then enroll it:

```bash
sudo doomsday-maps walk-router /absolute/path/region-foot.osrm
sudo systemctl enable --now doomsday-osrm-walk.service
```

Choose **Walk** in the trip planner. Footways, public pedestrian ways, sidewalks, and roads where walking is permitted depend on how the graph was prepared. This mode **does not promise to avoid crossing roads**.

## Strict off-road walking: no crossings of mapped roads

For strict off-road mode, supply a *regional* GeoJSON `FeatureCollection` with both:

- OSM-tagged walking LineStrings (`highway=path`, `footway`, `steps`, or `bridleway`). `footway=crossing` and mapped sidewalks are excluded.
- Motor-road LineStrings (`highway=residential`, `service`, `primary`, `secondary`, `motorway`, etc.).

```bash
sudo doomsday-maps offroad /absolute/path/trails-and-roads.geojson
sudo doomsday-trip status
```

The importer deletes candidate trail edges that cross or touch road geometries. The planner uses shortest-distance Dijkstra over the remaining trail network. A point more than 120 meters from a mapped trail, or any disconnected point, produces **no route**, never a line across roads or private property. No AI models are used. The import refuses an extract without road geometries because it cannot claim to avoid road crossings.

**Limits:** Missing roads, land ownership, terrain hazards, seasonal closures, and crossing structures may not be represented in the supplied GeoJSON. This mode guarantees avoidance only for road geometries present in that local data, **not all real-world roads**, and is not a safety-certified navigation tool. OSM tagged paths can have restrictions; verify public legal access before use.

## Using current location or places by name

Your browser can request GPS only after pressing **Use my current position**; permission and a local hardware/location provider are required. No coordinates are sent to an external server.

Import a local CSV gazetteer (UTF-8 header `name,lat,lon`) to search cities, addresses, and landmarks without an online geocoder:

```bash
sudo doomsday-maps places /absolute/path/places.csv
```

Pick the appropriate destination field in **Click the map to assign a location to**, then choose a place result or click the map. If a name is ambiguous, select a specific result rather than guessing.

**Manchester to USA example:** You can enter your start location, up to three intermediate points, and a chosen city (such as New York) as the destination. `USA` alone is not a point. A car-only road graph cannot draw a route across the Atlantic Ocean. The system reports **No continuous offline route** instead of drawing an imaginary road; ferry/flight/shipping legs would need a separate multimodal transport dataset not currently included. Continents and border crossings require appropriate offline data coverage.

## CLI for testing a known local region

```bash
sudo doomsday-trip route --mode car '51.10,17.00' '52.12,16.98' '52.23,17.98'
sudo doomsday-trip route --mode offroad '51.101,17.011' '51.102,17.020'
```

Both commands use only prepared local graphs. Supplying coordinates is **not** proof that a route exists. No direct overland shortcuts are generated.
