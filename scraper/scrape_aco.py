"""
Scrape ACO Toronto (TOBuilt) for Edmund Burke buildings.
Searches multiple Burke architect name variants, deduplicates, and
writes data/aco_buildings.json for merging into buildings.geojson.

Captures all available fields per building:
  name, address, city, neighbourhood, year_built, alternate_name,
  awards, notes, construction_status, building_type, current_use,
  former_use, heritage_status, heritage_conservation_district,
  main_style, companies [{role, name, url}],
  sources [{title, author, url}], images [full URL],
  thumbnail_url, lat, lng, detail_url

Usage: python scraper/scrape_aco.py
"""
import urllib.request
import urllib.parse
import re
import json
import time
import os

BASE = "https://www.acotoronto.ca"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Referer": BASE + "/tobuilt.php",
}

# Burke architect name variants in the ACO autocomplete list
BURKE_ARCHITECTS = [
    "Burke & Horwood",
    "Burke, Horwood & White Associates",
    "Langley and Burke",
    "Langley & Burke",
    "Edmund Burke",
]

OUTPUT_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "aco_buildings.json")


def _get(url, extra_headers=None):
    h = {**HEADERS}
    if extra_headers:
        h.update(extra_headers)
    req = urllib.request.Request(url, headers=h)
    with urllib.request.urlopen(req, timeout=20) as r:
        return r.read().decode("utf-8", errors="replace")


def _post(url, fields):
    data = urllib.parse.urlencode(fields).encode()
    h = {**HEADERS, "Content-Type": "application/x-www-form-urlencoded", "Origin": BASE}
    req = urllib.request.Request(url, data=data, headers=h, method="POST")
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8", errors="replace")


def _strip(html_fragment):
    """Strip HTML tags and normalise whitespace."""
    text = re.sub(r"<[^>]+>", " ", html_fragment)
    text = re.sub(r"&nbsp;", " ", text)
    text = re.sub(r"&amp;", "&", text)
    text = re.sub(r"&lt;", "<", text)
    text = re.sub(r"&gt;", ">", text)
    text = re.sub(r"&#\d+;", "", text)
    return re.sub(r"\s+", " ", text).strip()


def _field(html, label):
    """Extract the col-sm-9 value cell following a given output_title label."""
    pattern = (
        rf"output_title[^>]*>\s*{re.escape(label)}\s*</div>"
        r".*?<div[^>]*col-sm-9[^>]*>\s*(.*?)\s*</div>"
    )
    m = re.search(pattern, html, re.DOTALL | re.IGNORECASE)
    return _strip(m.group(1)) if m else ""


def search_architect(name):
    """POST search, follow sid redirect; return list of (building_id, thumbnail_url) tuples."""
    html = _post(BASE + "/search_buildingsDB_d.php", {"Architect": name, "BuildingName": ""})
    m = re.search(r"sid=(\d+)", html)
    if not m:
        print(f"  No sid for '{name}'")
        return []
    sid = m.group(1)
    results_html = _get(BASE + f"/search_buildingsR-d.php?sid={sid}")

    # Cards appear as: href="building.php?ID=NNN" ... background-image:url(...)
    card_pattern = re.compile(
        r'href=["\']building\.php\?ID=(\d+)["\"].*?'
        r"background-image:url\(([^)]+)\)",
        re.DOTALL,
    )
    pairs = []
    seen_ids = set()
    for m in card_pattern.finditer(results_html):
        bid, thumb = m.group(1), m.group(2).strip()
        if bid not in seen_ids:
            seen_ids.add(bid)
            if "nophoto" in thumb:
                thumb_url = None
            elif thumb.startswith("http"):
                thumb_url = thumb
            else:
                thumb_url = BASE + thumb
            pairs.append((bid, thumb_url))

    # Fallback: any IDs not captured by the card pattern
    for bid in re.findall(r"building\.php\?ID=(\d+)", results_html):
        if bid not in seen_ids:
            seen_ids.add(bid)
            pairs.append((bid, None))

    print(f"  '{name}' -> {len(pairs)} buildings")
    return pairs


def parse_detail(building_id, thumbnail_url=None):
    """Fetch building detail page and extract ALL fields."""
    html = _get(BASE + f"/building.php?ID={building_id}")

    # ── Name & Location ──────────────────────────────────────────────────────
    name = address = city = neighbourhood = ""
    loc_m = re.search(
        r"Name &(?:amp;)?\s*Location:.*?<b>(.*?)</b>(.*?)</span>",
        html, re.DOTALL | re.IGNORECASE,
    )
    if loc_m:
        name = _strip(loc_m.group(1))
        rest = re.sub(r"<br\s*/?>", "\n", loc_m.group(2), flags=re.IGNORECASE)
        parts = [_strip(p) for p in rest.split("\n") if _strip(p)]
        if len(parts) >= 1:
            address = parts[0]
        if len(parts) >= 2:
            city = parts[1]
        if len(parts) >= 3:
            neighbourhood = parts[2]

    # ── Year Completed ───────────────────────────────────────────────────────
    year_m = re.search(
        r"Year Completed:.*?<div[^>]*>\s*(\d+)\s*</div>",
        html, re.DOTALL | re.IGNORECASE,
    )
    year = int(year_m.group(1)) if year_m else None
    if year == 0:
        year = None

    # ── Simple label -> value fields ─────────────────────────────────────────
    alternate_name      = _field(html, "Alternate Name:")
    construction_status = _field(html, "Status:")
    building_type       = _field(html, "Building Type:")
    current_use         = _field(html, "Current Use:")
    former_use          = _field(html, "Former Use:")
    heritage_status     = _field(html, "Heritage Status:")
    heritage_district   = _field(html, "Heritage Conservation District:")
    main_style          = _field(html, "Main Style:")

    # ── Awards ───────────────────────────────────────────────────────────────
    awards_m = re.search(
        r"Awards:.*?<div[^>]*col-sm-9[^>]*>(.*?)</div>",
        html, re.DOTALL | re.IGNORECASE,
    )
    awards = _strip(awards_m.group(1)) if awards_m else ""

    # ── Notes ────────────────────────────────────────────────────────────────
    # Notes div has style="font-size: 1em;" so the pattern is slightly different
    notes_m = re.search(
        r"Notes:.*?<div[^>]*col-sm-9[^>]*style[^>]*>(.*?)</div>",
        html, re.DOTALL | re.IGNORECASE,
    )
    if not notes_m:
        notes_m = re.search(
            r"Notes:.*?<div[^>]*col-sm-9[^>]*>(.*?)</div>",
            html, re.DOTALL | re.IGNORECASE,
        )
    notes = _strip(notes_m.group(1)) if notes_m else ""

    # ── Lat / Lng from Google Maps JS ────────────────────────────────────────
    coord_m = re.search(r"center:\s*\{lat:\s*([-\d.]+),\s*lng:\s*([-\d.]+)\}", html)
    lat = float(coord_m.group(1)) if coord_m else None
    lng = float(coord_m.group(2)) if coord_m else None

    # ── Companies ────────────────────────────────────────────────────────────
    companies = []
    companies_m = re.search(
        r"Companies:.*?<ul>(.*?)</ul>",
        html, re.DOTALL | re.IGNORECASE,
    )
    if companies_m:
        for li in re.findall(r"<li>(.*?)</li>", companies_m.group(1), re.DOTALL | re.IGNORECASE):
            role_m = re.search(r"<strong>([^<]+)</strong>", li, re.IGNORECASE)
            role = _strip(role_m.group(1)) if role_m else ""
            # Prefer the first <a> that is NOT a search_by_tag link
            named_link = re.search(
                r'<a\s+href=["\'](?!search_by_tag)([^"\']+)["\'][^>]*>([^<]+)</a>',
                li, re.IGNORECASE,
            )
            if named_link:
                href = named_link.group(1).strip()
                cname = _strip(named_link.group(2))
                curl = href if href.startswith("http") else None
            else:
                # Plain-text name — strip role, spans, leftover tags
                plain = re.sub(r"<strong>[^<]+</strong>\s*-\s*", "", li, flags=re.IGNORECASE)
                plain = re.sub(r"<span[^>]*>.*?</span>", "", plain, flags=re.DOTALL | re.IGNORECASE)
                cname = _strip(plain)
                curl = None
            if role and cname:
                companies.append({"role": role, "name": cname, "url": curl})

    # Primary Burke firm = first Architect entry that includes a Burke/Langley name
    burke_firms = [
        c["name"] for c in companies
        if c["role"].lower() == "architect"
        and any(k in c["name"].lower() for k in ("burke", "langley"))
    ]
    firm = burke_firms[0] if burke_firms else (
        next((c["name"] for c in companies if c["role"].lower() == "architect"), "")
    )

    # ── Sources ──────────────────────────────────────────────────────────────
    sources = []
    source_modal_m = re.search(
        r'id=["\']SourceModal["\'].*?<div class=["\']modal-body["\']>(.*?)</div>\s*</div>\s*</div>\s*</div>',
        html, re.DOTALL | re.IGNORECASE,
    )
    if source_modal_m:
        for li in re.findall(
            r"<li[^>]*>(.*?)</li>", source_modal_m.group(1), re.DOTALL | re.IGNORECASE
        ):
            title_m  = re.search(r"font-weight:bold[^>]*>([^<]+)<", li, re.IGNORECASE)
            author_m = re.search(r"Author</em>\s*-\s*([^<\n]+)", li, re.IGNORECASE)
            url_m    = re.search(r'href=["\']([^"\']+)["\'][^>]*>More information', li, re.IGNORECASE)
            title  = _strip(title_m.group(1))  if title_m  else ""
            author = _strip(author_m.group(1)) if author_m else ""
            url    = url_m.group(1).strip()    if url_m    else ""
            if title:
                sources.append({"title": title, "author": author, "url": url})

    # ── Photos ───────────────────────────────────────────────────────────────
    # Full-size images via ekko-lightbox <a href="/images/buildings/Full/...">
    image_urls = []
    for m in re.finditer(
        r'href=["\'](/images/buildings/[^"\']+\.(?:jpg|jpeg|png|gif))["\']',
        html, re.IGNORECASE,
    ):
        full_url = BASE + m.group(1)
        if full_url not in image_urls:
            image_urls.append(full_url)
    # Fallback: Medium-size <img src="...">
    if not image_urls:
        for m in re.finditer(
            r'src=["\'](/images/buildings/[^"\']+\.(?:jpg|jpeg|png|gif))["\']',
            html, re.IGNORECASE,
        ):
            full_url = BASE + m.group(1)
            if full_url not in image_urls:
                image_urls.append(full_url)

    return {
        "id": building_id,
        "name": name,
        "address": address,
        "city": city,
        "neighbourhood": neighbourhood,
        "year_built": year,
        "alternate_name": alternate_name,
        "awards": awards,
        "notes": notes,
        "construction_status": construction_status,
        "building_type": building_type,
        "current_use": current_use,
        "former_use": former_use,
        "heritage_status": heritage_status,
        "heritage_conservation_district": heritage_district,
        "main_style": main_style,
        "firm": firm,
        "companies": companies,
        "sources": sources,
        "images": image_urls,
        "thumbnail_url": thumbnail_url,
        "lat": lat,
        "lng": lng,
        "aco_source": "aco_toronto",
        "detail_url": BASE + f"/building.php?ID={building_id}",
    }


def main():
    # Collect all unique (id, thumbnail_url) pairs across all architect variants
    all_pairs = []
    seen_ids = set()
    for name in BURKE_ARCHITECTS:
        pairs = search_architect(name)
        for bid, thumb in pairs:
            if bid not in seen_ids:
                seen_ids.add(bid)
                all_pairs.append((bid, thumb))
        time.sleep(1)

    print(f"\nTotal unique building IDs: {len(all_pairs)}")

    # Fetch each detail page
    buildings = []
    for i, (bid, thumb) in enumerate(all_pairs, 1):
        print(f"  [{i}/{len(all_pairs)}] ID={bid}", end=" ", flush=True)
        try:
            rec = parse_detail(bid, thumbnail_url=thumb)
            buildings.append(rec)
            imgs = f" | {len(rec['images'])} photo(s)" if rec["images"] else ""
            print(
                f"-> {rec['name']!r} | {rec['address']}, {rec['city']}"
                f" | {rec['year_built']}{imgs}"
            )
        except Exception as e:
            print(f"ERROR: {e}")
        time.sleep(0.5)

    # Write output
    os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(buildings, f, ensure_ascii=False, indent=2)
    print(f"\nWrote {len(buildings)} records to {OUTPUT_PATH}")

    # Summary
    by_firm = {}
    for b in buildings:
        by_firm.setdefault(b["firm"], 0)
        by_firm[b["firm"]] += 1
    print("\nBy firm:")
    for firm, count in sorted(by_firm.items(), key=lambda x: -x[1]):
        print(f"  {firm!r}: {count}")

    has_photos  = sum(1 for b in buildings if b["images"])
    has_thumbs  = sum(1 for b in buildings if b["thumbnail_url"])
    has_sources = sum(1 for b in buildings if b["sources"])
    has_awards  = sum(1 for b in buildings if b["awards"])
    print(f"\nBuildings with full-size photos : {has_photos}/{len(buildings)}")
    print(f"Buildings with thumbnail        : {has_thumbs}/{len(buildings)}")
    print(f"Buildings with sources          : {has_sources}/{len(buildings)}")
    print(f"Buildings with awards           : {has_awards}/{len(buildings)}")


if __name__ == "__main__":
    main()
