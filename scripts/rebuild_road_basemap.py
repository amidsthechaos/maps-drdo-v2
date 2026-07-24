"""Rebuild data/roads_basemap.tif from PostGIS road_edges (RGB, dark bg + light roads).

Uses the actual DB extent (not a previous GeoTIFF envelope). Streams geometries in
batches so ~20M+ edges fit in memory. Writes EPSG:4326 for GeoTiffTileService.
"""
from __future__ import annotations

import os
import re
import time
from pathlib import Path

os.environ.pop("PROJ_LIB", None)
os.environ.pop("PROJ_DATA", None)

import numpy as np
import rasterio
from rasterio import features
from rasterio.transform import from_bounds
from shapely import wkb
from shapely.geometry import mapping

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / "roads_basemap.tif"
OUT_TMP = ROOT / "data" / "roads_basemap.tmp.tif"
OUT_ALT = ROOT / "data" / "roads_basemap_new.tif"
PROPS = ROOT / "georoute-backend" / "src" / "main" / "resources" / "application.properties"

WIDTH = 4096
HEIGHT = 4096
BG = (14, 18, 26)
ROAD = (210, 216, 224)
MAJOR = (240, 244, 250)
BATCH = 20_000

MAJOR_TYPES = {
    "motorway", "trunk", "primary", "secondary", "tertiary",
    "motorway_link", "trunk_link", "primary_link", "secondary_link",
    "tertiary_link",
}


def load_jdbc() -> dict:
    text = PROPS.read_text(encoding="utf-8")
    url = re.search(r"^spring\.datasource\.url=(.+)$", text, re.M).group(1).strip()
    user = re.search(r"^spring\.datasource\.username=(.+)$", text, re.M).group(1).strip()
    password = re.search(r"^spring\.datasource\.password=(.+)$", text, re.M).group(1).strip()
    m = re.match(r"jdbc:postgresql://([^:/]+)(?::(\d+))?/(.+)", url)
    return {
        "host": m.group(1),
        "port": int(m.group(2) or 5432),
        "dbname": m.group(3),
        "user": user,
        "password": password,
    }


def looks_projected(minx: float, miny: float, maxx: float, maxy: float) -> bool:
    return abs(minx) > 180 or abs(maxx) > 180 or abs(miny) > 90 or abs(maxy) > 90


def burn_batch(mask: np.ndarray, geoms: list, transform) -> None:
    if not geoms:
        return
    shapes = ((mapping(g), 1) for g in geoms if not g.is_empty)
    features.rasterize(
        shapes,
        out=mask,
        transform=transform,
        all_touched=True,
        default_value=1,
        dtype=np.uint8,
    )


def main() -> None:
    try:
        import psycopg2
    except ImportError as e:
        raise SystemExit("psycopg2 required: pip install psycopg2-binary") from e

    t0 = time.time()
    cfg = load_jdbc()
    conn = psycopg2.connect(
        host=cfg["host"], port=cfg["port"], dbname=cfg["dbname"],
        user=cfg["user"], password=cfg["password"],
    )

    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT ST_XMin(e), ST_YMin(e), ST_XMax(e), ST_YMax(e)
                FROM (SELECT ST_Extent(geom) AS e FROM road_edges) t
                """
            )
            row = cur.fetchone()
            if not row or row[0] is None:
                raise SystemExit("road_edges is empty — cannot build basemap")
            raw = tuple(float(v) for v in row)

        projected = looks_projected(*raw)
        if projected:
            extent_sql = (
                "SELECT ST_XMin(e), ST_YMin(e), ST_XMax(e), ST_YMax(e) "
                "FROM (SELECT ST_Extent(ST_Transform(ST_SetSRID(geom, 3857), 4326)) AS e "
                "FROM road_edges) t"
            )
            geom_sql = "ST_Transform(ST_SetSRID(e.geom, 3857), 4326)"
            print(f"Detected projected coords {raw}; reprojecting 3857 -> 4326")
        else:
            extent_sql = (
                "SELECT ST_XMin(e), ST_YMin(e), ST_XMax(e), ST_YMax(e) "
                "FROM (SELECT ST_Extent(geom) AS e FROM road_edges) t"
            )
            geom_sql = "e.geom"
            print(f"Using WGS84 extent from DB: {raw}")

        with conn.cursor() as cur:
            cur.execute(extent_sql)
            minx, miny, maxx, maxy = (float(v) for v in cur.fetchone())

        pad_x = max((maxx - minx) * 0.02, 0.01)
        pad_y = max((maxy - miny) * 0.02, 0.01)
        bounds = (minx - pad_x, miny - pad_y, maxx + pad_x, maxy + pad_y)
        transform = from_bounds(*bounds, WIDTH, HEIGHT)

        other_mask = np.zeros((HEIGHT, WIDTH), dtype=np.uint8)
        major_mask = np.zeros((HEIGHT, WIDTH), dtype=np.uint8)
        other_batch: list = []
        major_batch: list = []
        kept = 0

        with conn.cursor(name="roads_basemap_stream") as cur:
            cur.itersize = 5000
            cur.execute(
                f"""
                SELECT e.road_type, ST_AsBinary({geom_sql})
                FROM road_edges e
                """
            )
            for road_type, geom_wkb in cur:
                if not geom_wkb:
                    continue
                geom = wkb.loads(bytes(geom_wkb))
                if geom.is_empty:
                    continue
                kept += 1
                fclass = (road_type or "").lower()
                if fclass in MAJOR_TYPES:
                    major_batch.append(geom)
                    if len(major_batch) >= BATCH:
                        burn_batch(major_mask, major_batch, transform)
                        major_batch.clear()
                else:
                    other_batch.append(geom)
                    if len(other_batch) >= BATCH:
                        burn_batch(other_mask, other_batch, transform)
                        other_batch.clear()

                if kept % 500_000 == 0:
                    print(f"  streamed {kept} edges...")

        burn_batch(major_mask, major_batch, transform)
        burn_batch(other_mask, other_batch, transform)
    finally:
        conn.close()

    if kept == 0:
        raise SystemExit("No road geometries returned")

    rgb = np.zeros((3, HEIGHT, WIDTH), dtype=np.uint8)
    rgb[0] = BG[0]
    rgb[1] = BG[1]
    rgb[2] = BG[2]

    m = other_mask > 0
    other_px = int(m.sum())
    rgb[0][m] = ROAD[0]
    rgb[1][m] = ROAD[1]
    rgb[2][m] = ROAD[2]

    m = major_mask > 0
    major_px = int(m.sum())
    rgb[0][m] = MAJOR[0]
    rgb[1][m] = MAJOR[1]
    rgb[2][m] = MAJOR[2]

    profile = {
        "driver": "GTiff",
        "height": HEIGHT,
        "width": WIDTH,
        "count": 3,
        "dtype": "uint8",
        "crs": "EPSG:4326",
        "transform": transform,
        "compress": "LZW",
        "tiled": True,
        "blockxsize": 256,
        "blockysize": 256,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    write_path = OUT
    try:
        with rasterio.open(OUT_TMP, "w", **profile) as dst:
            dst.write(rgb)
        try:
            os.replace(OUT_TMP, OUT)
        except OSError:
            # Backend may have the old GeoTIFF open — write alternate and keep it.
            if OUT_TMP.exists():
                if OUT_ALT.exists():
                    OUT_ALT.unlink()
                os.replace(OUT_TMP, OUT_ALT)
            write_path = OUT_ALT
            print(f"WARNING: could not replace {OUT} (file locked). Wrote {OUT_ALT} instead.")
            print("Update geotiff.base.path to ../data/roads_basemap_new.tif and restart backend.")
    finally:
        if OUT_TMP.exists() and write_path != OUT_TMP:
            try:
                OUT_TMP.unlink()
            except OSError:
                pass

    with rasterio.open(write_path) as ds:
        unique_r = len(np.unique(ds.read(1)))

    print(
        f"Wrote {write_path} ({write_path.stat().st_size} bytes), roads kept={kept}, "
        f"roadPx={other_px + major_px}, uniqueR={unique_r}, bounds={list(bounds)}, "
        f"ms={int((time.time() - t0) * 1000)}"
    )


if __name__ == "__main__":
    main()
