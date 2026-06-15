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
OUTPUT_JS_PATH = DATA_DIR / "buildings.js"   # loaded by index.html via <script>
CACHE_PATH = Path(__file__).resolve().parent / ".geocode_cache.json"
ACO_PATH = DATA_DIR / "aco_buildings.json"   # written by scraper/scrape_aco.py

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
        # Use address as placeholder name; merge_with_aco will overwrite
        # with the proper building name from ACO when available.
        # Generic BUILDING_T values like "Residential" / "Religious" are
        # stored in building_type but never used as the primary name.
        "name": address,
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
        # ACO-enrichment slots (populated by merge_with_aco)
        "building_type":    btype,
        "alternate_name":   "",
        "neighbourhood":    "",
        "current_use":      "",
        "former_use":       "",
        "heritage_district": "",
        "awards":           "",
        "notes":            "",
        "thumbnail_url":    "",
        "images":           [],
        "aco_id":           "",
    }

def _addr_key(addr: str) -> str:
    """Normalised address key: house number + first word of street name (uppercase).

    Handles '176 YONGE ST' == '176 Yonge Street' == '176 Yonge'.
    """
    parts = re.sub(r"[^a-zA-Z0-9 ]", "", addr).upper().split()
    if len(parts) >= 2:
        return f"{parts[0]} {parts[1]}"
    return addr.upper().strip()


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


def load_aco_records() -> list[dict]:
    """Load aco_buildings.json and normalise to the standard building dict.

    Only records that have coordinates (lat + lng from Google Maps JS) are kept.
    Returns [] if the file doesn't exist yet (run scrape_aco.py first).
    """
    if not ACO_PATH.exists():
        print("  aco_buildings.json not found — skipping ACO data.")
        print("  Run: python scraper/scrape_aco.py")
        return []

    with open(ACO_PATH, encoding="utf-8") as f:
        raw = json.load(f)

    records = []
    for b in raw:
        lat = b.get("lat")
        lng = b.get("lng")
        if lat is None or lng is None:
            continue  # no coordinates — skip
        records.append({
            "name":             b.get("name") or b.get("address", ""),
            "address":          b.get("address", ""),
            "year_built":       str(b["year_built"]) if b.get("year_built") else "",
            "firm":             b.get("firm", ""),
            "style":            b.get("main_style", ""),
            "heritage_status":  b.get("heritage_status", ""),
            "source":           "aco_toronto",
            "city":             b.get("city", "Toronto"),
            "ward":             "",
            "wikipedia_url":    b.get("wikipedia_url") or "",
            "detail_url":       b.get("detail_url", ""),
            "coordinates":      [lng, lat],
            # ACO-specific extras
            "aco_id":           b.get("id", ""),
            "alternate_name":   b.get("alternate_name", ""),
            "neighbourhood":    b.get("neighbourhood", ""),
            "building_type":    b.get("building_type", ""),
            "current_use":      b.get("current_use", ""),
            "former_use":       b.get("former_use", ""),
            "heritage_district": b.get("heritage_conservation_district", ""),
            "awards":           b.get("awards", ""),
            "notes":            b.get("notes", ""),
            "thumbnail_url":    b.get("thumbnail_url") or "",
            "images":           b.get("images") or [],
            "companies":        b.get("companies") or [],
            "sources":          b.get("sources") or [],
        })
    return records


# Building-type words that should never be used as a primary display name.
_GENERIC_NAMES = frozenset([
    "residential", "religious", "commercial", "industrial", "institutional",
    "educational", "government", "transportation", "mixed use", "other",
    "unknown", "office", "retail", "warehouse", "recreational",
])


def _is_generic_name(name: str, address: str) -> bool:
    """True if name is a generic building-type word or just an address."""
    n = name.strip().lower()
    if not n:
        return True
    if n == address.strip().lower():
        return True
    # Normalise: remove numbers and punctuation, check word set
    words = frozenset(re.sub(r"[^a-z ]", " ", n).split())
    return bool(words & _GENERIC_NAMES)


def merge_with_aco(existing: list[dict], aco: list[dict]) -> list[dict]:
    """Merge ACO records into existing list.

    For each ACO record:
    - If an existing record matches by address key, *enrich* it: overwrite the
      name if the existing name is generic (building-type word or bare address),
      and fill in any empty ACO-specific fields.
    - Otherwise append as a new record.
    """
    # Build index: addr_key → position in merged list
    merged = list(existing)
    addr_to_idx: dict[str, int] = {}
    for i, r in enumerate(merged):
        ak = _addr_key(r.get("address", ""))
        if ak:
            addr_to_idx[ak] = i

    existing_names = {r.get("name", "").lower() for r in merged if r.get("name")}
    added = enriched = 0

    for rec in aco:
        ak = _addr_key(rec.get("address", ""))
        matched_idx = addr_to_idx.get(ak) if ak else None

        # Fallback: name-based match
        if matched_idx is None:
            rn = rec.get("name", "").lower()
            if rn:
                for i, r in enumerate(merged):
                    en = r.get("name", "").lower()
                    if rn and en and (rn in en or en in rn):
                        matched_idx = i
                        break

        if matched_idx is not None:
            # Enrich the existing record with ACO data
            tgt = merged[matched_idx]
            aco_name = rec.get("name", "").strip()
            if aco_name and _is_generic_name(tgt.get("name", ""), tgt.get("address", "")):
                tgt["name"] = aco_name
            # Fill empty enrichment fields from ACO
            for field in ("alternate_name", "neighbourhood", "building_type",
                          "current_use", "former_use", "heritage_district",
                          "awards", "notes", "thumbnail_url", "images",
                          "aco_id", "wikipedia_url", "detail_url"):
                if not tgt.get(field):
                    tgt[field] = rec.get(field, "" if field != "images" else [])
            enriched += 1
        else:
            merged.append(rec)
            if ak:
                addr_to_idx[ak] = len(merged) - 1
            existing_names.add(rec.get("name", "").lower())
            added += 1

    print(f"  Added {added} new ACO buildings, enriched {enriched} Heritage Register records")
    return merged


# -- GeoJSON output -----------------------------------------------------------

def to_geojson(records: list[dict]) -> dict:
    features = []
    for rec in records:
        coords = rec.get("coordinates")
        if not coords or len(coords) != 2:
            continue
        # images: store first full-size URL (if any) as primary_image
        imgs = rec.get("images") or []
        features.append({
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": coords},
            "properties": {
                "name":             rec.get("name", ""),
                "address":          rec.get("address", ""),
                "year_built":       rec.get("year_built", ""),
                "firm":             rec.get("firm", ""),
                "style":            rec.get("style", ""),
                "heritage_status":  rec.get("heritage_status", ""),
                "source":           rec.get("source", ""),
                "city":             rec.get("city", ""),
                "ward":             rec.get("ward", ""),
                "wikipedia_url":    rec.get("wikipedia_url", ""),
                "detail_url":       rec.get("detail_url", ""),
                # ACO-specific (empty string for Heritage Register records)
                "aco_id":           str(rec.get("aco_id") or ""),
                "alternate_name":   rec.get("alternate_name", ""),
                "neighbourhood":    rec.get("neighbourhood", ""),
                "building_type":    rec.get("building_type", ""),
                "current_use":      rec.get("current_use", ""),
                "former_use":       rec.get("former_use", ""),
                "heritage_district": rec.get("heritage_district", ""),
                "awards":           rec.get("awards", ""),
                "notes":            rec.get("notes", ""),
                "thumbnail_url":    rec.get("thumbnail_url", ""),
                "primary_image":    imgs[0] if imgs else "",
                "image_count":      len(imgs),
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

    # 3. Merge ACO Toronto data (deduplicated against Heritage Register)
    print("\nLoading ACO Toronto data ...")
    aco_records = load_aco_records()
    if aco_records:
        all_records = merge_with_aco(all_records, aco_records)

    # 4. Geocode any records still missing coordinates (seed buildings)
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

    # 5. Sort by year, write GeoJSON + JS data file
    all_records.sort(
        key=lambda r: int(re.split(r"\D", r.get("year_built", "9999"))[0] or 9999)
    )

    geojson = to_geojson(all_records)

    # GeoJSON (for reference / download)
    with open(OUTPUT_PATH, "w", encoding="utf-8") as fh:
        json.dump(geojson, fh, indent=2, ensure_ascii=False)

    # JS data file — loaded synchronously by index.html via <script> tag,
    # avoids any fetch() / CORS / path issues on GitHub Pages.
    js_content = "window.BUILDINGS_DATA = " + json.dumps(geojson, ensure_ascii=False) + ";\n"
    with open(OUTPUT_JS_PATH, "w", encoding="utf-8") as fh:
        fh.write(js_content)

    located = len(geojson["features"])
    skipped = len(all_records) - located
    print(f"\nWrote {located} features to {OUTPUT_PATH} and {OUTPUT_JS_PATH.name}")
    if skipped:
        print(f"   ({skipped} records skipped -- no coordinates)")
    print("Done.")


if __name__ == "__main__":
    main()
