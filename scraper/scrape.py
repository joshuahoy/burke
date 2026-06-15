#!/usr/bin/env python3
"""
Edmund Burke Heritage Buildings Scraper
========================================
Primary source: City of Toronto Heritage Register via ArcGIS REST API
  https://services6.arcgis.com/MTZInRnED7jgMJ39/arcgis/rest/services/HRAP_Q2_2026_WFL1/FeatureServer/0

  Queries all records where ARCHITECT_ contains 'BURKE'.
  Coordinates come directly from the feature service (no geocoding needed
  for most records).  Non-geocoded seed buildings (Orillia etc.) are geocoded
  via Nominatim as a fallback.

Scope: Ontario only (Toronto + Orillia etc.). Sackville NB excluded.

Usage:
    cd path/to/Burke
    pip install -r scraper/requirements.txt
    python scraper/scrape.py

Output:
    data/buildings.geojson   (overwrites the seed file)
    scraper/.geocode_cache.json   (persists between runs — for seed geocoding)
"""

import json
import time
import re
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import urlopen

from geopy.geocoders import Nominatim
from geopy.exc import GeocoderTimedOut, GeocoderServiceError

# ── Paths ───────────────────────────────────────────────────────────────────
ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
DATA_DIR.mkdir(exist_ok=True)
OUTPUT_PATH = DATA_DIR / "buildings.geojson"
CACHE_PATH = Path(__file__).resolve().parent / ".geocode_cache.json"

# ── ArcGIS REST API (City of Toronto Heritage Register) ──────────────────────
ARCGIS_URL = (
    "https://services6.arcgis.com/MTZInRnED7jgMJ39/arcgis/rest/services"
    "/HRAP_Q2_2026_WFL1/FeatureServer/0/query"
)
ARCGIS_FIELDS = ",".join([
    "ADDRESS", "ARCHITECT_", "CONSTRUCTI", "STATUS",
    "BUILDING_T", "DESCRIPTIO", "WARD",
    "HOUSE", "STREET", "STREET_TYP",
    "X_COORDINA", "Y_COORDINA",
])

# All architect name variants that contain Burke — a single LIKE '%BURKE%'
# query catches them all in the ArcGIS layer.
ARCGIS_WHERE = "ARCHITECT_ LIKE '%BURKE%'"

# ── Wikipedia / known-works seed data (Ontario scope only) ───────────────────
# Coordinates: [longitude, latitude]  (GeoJSON order)
SEED_BUILDINGS = [
    {
        "name": "St. Luke's United Church",
        "address": "353 Sherbourne St, Toronto, Ontario",
        "year_built": "1874",
        "firm": "HENRY LANGLEY AND EDMUND BURKE",
        "style": "Romanesque Revival",
        "heritage_status": "Unknown",
        "source": "wikipedia",
        "city": "Toronto",
        "coordinates": [-79.3738, 43.6617],
        "wikipedia_url": "https://en.wikipedia.org/wiki/St._Luke%27s_United_Church,_Toronto",
    },
    {
        "name": "St. Andrew's Evangelical Lutheran Church",
        "address": "383 Jarvis St, Toronto, Ontario",
        "year_built": "1878",
        "firm": "HENRY LANGLEY AND EDMUND BURKE",
        "style": "Gothic Revival",
        "heritage_status": "Unknown",
        "source": "wikipedia",
        "city": "Toronto",
        "coordinates": [-79.3753, 43.6620],
        "wikipedia_url": "",
    },
    {
        "name": "Jarvis Street Baptist Church",
        "address": "130 Gerrard St E, Toronto, Ontario",
        "year_built": "1878",
        "firm": "HENRY LANGLEY AND EDMUND BURKE",
        "style": "Gothic Revival",
        "heritage_status": "Designated Part IV",
        "source": "wikipedia",
        "city": "Toronto",
        "coordinates": [-79.3751, 43.6591],
        "wikipedia_url": "https://en.wikipedia.org/wiki/Jarvis_Street_Baptist_Church",
    },
    {
        "name": "McMaster Hall",
        "address": "273 Bloor St W, Toronto, Ontario",
        "year_built": "1881",
        "firm": "HENRY LANGLEY AND EDMUND BURKE",
        "style": "Romanesque Revival",
        "heritage_status": "Unknown",
        "source": "wikipedia",
        "city": "Toronto",
        "coordinates": [-79.3978, 43.6676],
        "wikipedia_url": "",
    },
    {
        "name": "Beverley Street Baptist Church",
        "address": "72 Beverley St, Toronto, Ontario",
        "year_built": "1886",
        "firm": "HENRY LANGLEY AND EDMUND BURKE",
        "style": "Gothic Revival",
        "heritage_status": "Unknown",
        "source": "wikipedia",
        "city": "Toronto",
        "coordinates": [-79.3952, 43.6534],
        "wikipedia_url": "",
    },
    {
        "name": "Trinity-St. Paul's United Church",
        "address": "427 Bloor St W, Toronto, Ontario",
        "year_built": "1887",
        "firm": "HENRY LANGLEY AND EDMUND BURKE",
        "style": "Gothic Revival",
        "heritage_status": "Designated Part IV",
        "source": "wikipedia",
        "city": "Toronto",
        "coordinates": [-79.4053, 43.6641],
        "wikipedia_url": "https://en.wikipedia.org/wiki/Trinity-St._Paul%27s_United_Church",
    },
    {
        "name": "Walmer Road Baptist Church",
        "address": "188 Lowther Ave, Toronto, Ontario",
        "year_built": "1892",
        "firm": "EDMUND BURKE AND HENRY LANGLEY",
        "style": "Gothic Revival",
        "heritage_status": "Designated Part IV",
        "source": "wikipedia",
        "city": "Toronto",
        "coordinates": [-79.4123, 43.6683],
        "wikipedia_url": "https://en.wikipedia.org/wiki/Walmer_Road_Baptist_Church",
    },
    {
        "name": "Mount Pleasant Cemetery Mortuary Chapel",
        "address": "375 Mount Pleasant Rd, Toronto, Ontario",
        "year_built": "1893",
        "firm": "EDMUND BURKE",
        "style": "",
        "heritage_status": "Unknown",
        "source": "wikipedia",
        "city": "Toronto",
        "coordinates": [-79.3789, 43.6968],
        "wikipedia_url": "https://en.wikipedia.org/wiki/Mount_Pleasant_Cemetery,_Toronto",
    },
    {
        "name": "Robert Simpson's Department Store",
        "address": "401 Bay St, Toronto, Ontario",
        "year_built": "1896",
        "firm": "EDMUND BURKE",
        "style": "Romanesque Revival / Chicago School",
        "heritage_status": "Designated Part IV",
        "source": "wikipedia",
        "city": "Toronto",
        "coordinates": [-79.3813, 43.6527],
        "wikipedia_url": "https://en.wikipedia.org/wiki/Hudson%27s_Bay_Queen_Street",
    },
    {
        "name": "Orillia City Hall",
        "address": "20 Mississaga St W, Orillia, Ontario",
        "year_built": "1915",
        "firm": "BURKE, HORWOOD AND WHITE",
        "style": "Romanesque Revival",
        "heritage_status": "Unknown",
        "source": "wikipedia",
        "city": "Orillia",
        "coordinates": [-79.4200, 44.6072],
        "wikipedia_url": "",
    },
    {
        "name": "Prince Edward Viaduct (Bloor Street Viaduct)",
        "address": "Bloor St E over Don Valley, Toronto, Ontario",
        "year_built": "1918",
        "firm": "EDMUND BURKE",
        "style": "Gothic Revival",
        "heritage_status": "Designated Part IV",
        "source": "wikipedia",
        "city": "Toronto",
        "coordinates": [-79.3572, 43.6774],
        "wikipedia_url": "https://en.wikipedia.org/wiki/Prince_Edward_Viaduct",
    },
]

# ── Geocoding ────────────────────────────────────────────────────────────────

def load_geocode_cache() -> dict:
    if CACHE_PATH.exists():
        with open(CACHE_PATH, encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_geocode_cache(cache: dict) -> None:
    with open(CACHE_PATH, "w", encoding="utf-8") as f:
        json.dump(cache, f, indent=2)


def geocode_address(address: str, geocoder, cache: dict) -> list | None:
    """Return [longitude, latitude] for address. Uses persistent cache."""
    if address in cache:
        return cache[address]
    try:
        time.sleep(1.1)  # Nominatim rate-limit: max 1 req/s
        loc = geocoder.geocode(address, timeout=15)
        coords = [loc.longitude, loc.latitude] if loc else None
    except (GeocoderTimedOut, GeocoderServiceError) as exc:
        print(f"  ⚠  Geocode error for '{address}': {exc}")
        coords = None
    cache[address] = coords
    save_geocode_cache(cache)
    return coords


# ── ArcGIS query (two-step: group → fetch by FID) ───────────────────────────

# Fields that uniquely identify one building/unit entry.
# Grouping by these collapses duplicate roll-number rows (e.g. 4× 155 Dalhousie)
# while keeping legitimate unit variants (379 Adelaide vs 379 A Adelaide) distinct.
_GROUP_FIELDS = ",".join([
    "HOUSE", "PREFIX", "STREET", "STREET_TYP", "DIRECTION",
    "UNIT_TYPE", "UNIT",
    "ARCHITECT_", "CONSTRUCTI", "STATUS", "WARD", "BUILDING_T",
])


def fetch_arcgis_records() -> list[dict]:
    """
    Two-step ArcGIS query:
      1. groupByFieldsForStatistics on building-identity fields → one row per
         unique building/unit, selecting MIN(FID) as the representative.
      2. Fetch those FIDs with full geometry and attributes.

    This deduplicates server-side: multiple roll-number rows for the same
    building are collapsed, while distinct unit addresses are preserved.
    """
    print(f"Step 1 — ArcGIS groupBy ({ARCGIS_WHERE!r}) ...")
    group_params = urlencode({
        "where": ARCGIS_WHERE,
        "groupByFieldsForStatistics": _GROUP_FIELDS,
        "outStatistics": json.dumps([{
            "statisticType": "min",
            "onStatisticField": "FID",
            "outStatisticFieldName": "MIN_FID",
        }]),
        "f": "json",
    })
    try:
        with urlopen(f"{ARCGIS_URL}?{group_params}", timeout=30) as resp:
            group_data = json.loads(resp.read())
    except Exception as exc:
        print(f"  ✗ ArcGIS group query failed: {exc}")
        return []

    if "error" in group_data:
        print(f"  ✗ ArcGIS error: {group_data['error']}")
        return []

    grouped = group_data.get("features", [])
    fids = [str(int(f["attributes"]["MIN_FID"])) for f in grouped]
    print(f"  Grouped: {len(fids)} unique buildings")

    print("Step 2 — fetching geometry by FID ...")
    features = _fetch_by_fids(fids)
    print(f"  Retrieved: {len(features)} features with geometry")

    return [r for f in features if (r := _normalise_arcgis(f))]


def _fetch_by_fids(fids: list[str]) -> list[dict]:
    """Fetch features with full geometry for the given list of object IDs."""
    all_features: list[dict] = []
    batch_size = 200  # ArcGIS URL-length safe limit
    for i in range(0, len(fids), batch_size):
        batch = fids[i : i + batch_size]
        params = urlencode({
            "objectIds": ",".join(batch),
            "outFields": ARCGIS_FIELDS,
            "returnGeometry": "true",
            "outSR": "4326",
            "f": "json",
        })
        try:
            with urlopen(f"{ARCGIS_URL}?{params}", timeout=30) as resp:
                data = json.loads(resp.read())
            all_features.extend(data.get("features", []))
        except Exception as exc:
            print(f"  ✗ FID batch {i//batch_size+1} failed: {exc}")
    return all_features


def _normalise_arcgis(feature: dict) -> dict | None:
    """Convert one ArcGIS feature → standard building dict."""
    p = feature.get("attributes") or {}
    geo = feature.get("geometry") or {}

    address = str(p.get("ADDRESS") or "").strip()
    if not address:
        return None

    # Coordinates come from the feature geometry (WGS84 / outSR=4326)
    lng = geo.get("x")
    lat = geo.get("y")
    if lng is None or lat is None:
        # Fall back to the attribute columns
        lng = p.get("X_COORDINA")
        lat = p.get("Y_COORDINA")
    coords = [float(lng), float(lat)] if (lng is not None and lat is not None) else None

    firm = str(p.get("ARCHITECT_") or "").strip().upper()
    year = str(p.get("CONSTRUCTI") or "").strip()
    status = str(p.get("STATUS") or "").strip()
    ward = str(p.get("WARD") or "").strip()
    btype = str(p.get("BUILDING_T") or "").strip()

    # Build a geocoding-friendly string (for any records missing coords)
    geocode_addr = f"{address}, Toronto, Ontario, Canada"

    return {
        "name": btype or address,
        "address": address,
        "_geocode_addr": geocode_addr,
        "year_built": year,
        "firm": firm,
        "style": "",
        "heritage_status": status,
        "source": "toronto_heritage_register",
        "city": "Toronto",
        "ward": ward,
        "detail_url": "",
        "wikipedia_url": "",
        "coordinates": coords,
    }

# -- Geocoding ----------------------------------------------------------------

def load_geocode_cache() -> dict:
    if CACHE_PATH.exists():
        with open(CACHE_PATH, encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_geocode_cache(cache: dict) -> None:
    with open(CACHE_PATH, "w", encoding="utf-8") as f:
        json.dump(cache, f, indent=2)


def geocode_address(address: str, geocoder, cache: dict) -> list | None:
    """Return [longitude, latitude] for address using Nominatim. Caches results."""
    if address in cache:
        return cache[address]
    try:
        time.sleep(1.1)  # Nominatim rate-limit: max 1 req/s
        loc = geocoder.geocode(address, timeout=15)
        coords = [loc.longitude, loc.latitude] if loc else None
    except (GeocoderTimedOut, GeocoderServiceError) as exc:
        print(f"  Geocode error for '{address}': {exc}")
        coords = None
    cache[address] = coords
    save_geocode_cache(cache)
    return coords


def merge_with_seeds(scraped: list[dict], seeds: list[dict]) -> list[dict]:
    """Add seed buildings not already present in the scraped results."""
    def _key(addr: str) -> str:
        return re.sub(r"\s+", " ", addr.lower().strip())

    scraped_keys = {_key(r.get("address", "")) for r in scraped}
    scraped_names = {r.get("name", "").lower() for r in scraped if r.get("name")}
    merged = list(scraped)
    for seed in seeds:
        if _key(seed.get("address", "")) in scraped_keys:
            continue
        sn = seed.get("name", "").lower()
        if sn and any(sn in n for n in scraped_names):
            continue
        merged.append(seed)
    return merged


# -- GeoJSON output -----------------------------------------------------------

def to_geojson(records: list[dict]) -> dict:
    features = []
    for rec in records:
        coords = rec.get("coordinates")
        if not coords or len(coords) != 2:
            continue
        features.append({
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": coords},
            "properties": {
                "name":            rec.get("name", ""),
                "address":         rec.get("address", ""),
                "year_built":      rec.get("year_built", ""),
                "firm":            rec.get("firm", ""),
                "style":           rec.get("style", ""),
                "heritage_status": rec.get("heritage_status", ""),
                "source":          rec.get("source", ""),
                "city":            rec.get("city", ""),
                "ward":            rec.get("ward", ""),
                "wikipedia_url":   rec.get("wikipedia_url", ""),
                "detail_url":      rec.get("detail_url", ""),
            },
        })
    return {"type": "FeatureCollection", "features": features}


# -- Main ---------------------------------------------------------------------

def main() -> None:
    geocoder = Nominatim(user_agent="edmund-burke-heritage-map/1.0 (research)")
    geocode_cache = load_geocode_cache()

    # 1. Fetch from ArcGIS (server-side groupBy deduplication)
    arcgis_records = fetch_arcgis_records()
    print(f"\n{len(arcgis_records)} unique records from Heritage Register")

    # 2. Merge with seed data (adds Orillia + notable missing buildings)
    all_records = merge_with_seeds(arcgis_records, SEED_BUILDINGS)

    # 3. Geocode any records still missing coordinates (seed buildings)
    missing_coords = [r for r in all_records if not r.get("coordinates")]
    if missing_coords:
        print(f"\nGeocoding {len(missing_coords)} seed record(s) via Nominatim ...")
    for rec in missing_coords:
        query = rec.get("_geocode_addr") or rec.get("address", "")
        coords = geocode_address(query, geocoder, geocode_cache)
        if coords:
            rec["coordinates"] = coords
            print(f"  OK  {rec['address']}")
        else:
            print(f"  FAIL  {rec['address']} -- could not geocode (will be omitted)")

    # 4. Sort by year, write GeoJSON
    all_records.sort(
        key=lambda r: int(re.split(r"\D", r.get("year_built", "9999"))[0] or 9999)
    )

    geojson = to_geojson(all_records)
    with open(OUTPUT_PATH, "w", encoding="utf-8") as fh:
        json.dump(geojson, fh, indent=2, ensure_ascii=False)

    located = len(geojson["features"])
    skipped = len(all_records) - located
    print(f"\nWrote {located} features to {OUTPUT_PATH}")
    if skipped:
        print(f"   ({skipped} records skipped -- no coordinates)")
    print("Done.")


if __name__ == "__main__":
    main()
