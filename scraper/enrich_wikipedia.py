"""
Enrich aco_buildings.json with Wikipedia URLs via the Wikipedia search API.

Searches by building name (and alternate name), accepts matches where the
article title has >= 60% similarity. Results are written back in-place.

Re-run scraper/scrape.py afterwards to regenerate buildings.geojson.

Usage:
    python scraper/enrich_wikipedia.py
"""
import json
import re
import time
import urllib.request
import urllib.parse
from difflib import SequenceMatcher
from pathlib import Path

ROOT     = Path(__file__).resolve().parent.parent
ACO_PATH = ROOT / "data" / "aco_buildings.json"

WIKI_API = "https://en.wikipedia.org/w/api.php"
HEADERS  = {
    "User-Agent": "edmund-burke-heritage-map/1.0 (research; github.com/joshuahoy/burke)"
}

ACCEPT_THRESHOLD = 0.60   # minimum title-similarity to auto-accept

# Manually verified Wikipedia URLs keyed by ACO building ID.
# These always take precedence over the search result, and prevent the
# script from overwriting a curated URL with a wrong match on re-run.
CURATED: dict[str, str] = {
    "2103": "https://en.wikipedia.org/wiki/St._James_Cathedral,_Toronto",
    "3104": "https://en.wikipedia.org/wiki/Little_Trinity_Anglican_Church",
    "2037": "https://en.wikipedia.org/wiki/Keg_Mansion",
    "4576": "https://en.wikipedia.org/wiki/Branksome_Hall",
    "3220": "https://en.wikipedia.org/wiki/Osgoode_Hall",
    "3427": "https://en.wikipedia.org/wiki/Trinity-St._Paul%27s_United_Church",
    "2475": "https://en.wikipedia.org/wiki/Casey_House",
    "3583": "https://en.wikipedia.org/wiki/Prince_Edward_Viaduct",
    "5301": "https://en.wikipedia.org/wiki/Walmer_Road_Baptist_Church",
    "1986": "https://en.wikipedia.org/wiki/The_Royal_Conservatory_of_Music",
    "3020": "https://en.wikipedia.org/wiki/Bank_of_British_North_America",
    "2002": "https://en.wikipedia.org/wiki/St._Luke%27s_United_Church,_Toronto",
    "7269": "https://en.wikipedia.org/wiki/Metropolitan_Community_Church_of_Toronto",
    "2842": "https://en.wikipedia.org/wiki/Simpsons_%28department_store%29",
    "2157": "https://en.wikipedia.org/wiki/Covenant_House",
    "3362": "https://en.wikipedia.org/wiki/St._Andrew%27s_Presbyterian_Church,_Toronto",
    "2159": "https://en.wikipedia.org/wiki/Betty_Oliphant_Theatre",
    "2934": "https://en.wikipedia.org/wiki/299_Queen_Street_West",
}

# IDs that should never receive an auto-matched URL (too ambiguous or
# no article exists). The curated dict above takes priority if known.
NEVER_MATCH: frozenset[str] = frozenset([
    "4731",   # Edward Fisher House — no Wikipedia article
    "3365",   # Samuel Building — article doesn't exist
    "3497",   # Bathurst Street Theatre — no article
    "10156",  # R.S. McKinnon House — no article
    "4855",   # U of T McCorkell House — no article
    "3200",   # Canadian Magazine Building — no article
    "3528",   # Gage Building — no article
    "3806",   # Church of Epiphany and St Mark — no article
    "4413",   # Lorne Hall — no article
    "2032",   # Samuel R. Briggs House — no article
    "6846",   # Dufferin Street Baptist Church — no article
    "2158",   # Anson Jones House — no article
    "7011",   # C.J. Holman House — no article
    "5226",   # Ukrainian Evangelical Baptist Church — no article
    "4643",   # John Cox House — wrong article, no correct one found
    "10154",  # J.W. Mickleborough House — duplicate of 3453, ignored
])


def _norm(text: str) -> str:
    text = re.sub(r"[^\w\s]", " ", text.lower())
    return re.sub(r"\s+", " ", text).strip()


def _sim(a: str, b: str) -> float:
    return SequenceMatcher(None, _norm(a), _norm(b)).ratio()


def _wiki_url(title: str) -> str:
    return "https://en.wikipedia.org/wiki/" + urllib.parse.quote(title.replace(" ", "_"))


def wiki_search(query: str, limit: int = 3) -> list:
    params = urllib.parse.urlencode({
        "action": "query", "list": "search",
        "srsearch": query, "srlimit": limit,
        "format": "json", "utf8": 1,
    })
    req = urllib.request.Request(f"{WIKI_API}?{params}", headers=HEADERS)
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                return json.loads(r.read()).get("query", {}).get("search", [])
        except urllib.error.HTTPError as e:
            if e.code == 429:
                wait = int(e.headers.get("Retry-After", 0) or 0) or (2 ** (attempt + 2))
                print(f"\n    rate-limited, waiting {wait}s ...", end=" ", flush=True)
                time.sleep(wait)
            else:
                print(f"\n    HTTP {e.code}: {e}")
                return []
        except Exception as e:
            print(f"\n    API error: {e}")
            return []
    return []


def best_match(name: str, alt: str, candidates: list) -> tuple:
    """Return (url, score) for the highest-scoring candidate."""
    best_url, best_score = None, 0.0
    for c in candidates:
        title = c.get("title", "")
        score = max(_sim(name, title), _sim(alt, title) if alt else 0)
        if score > best_score:
            best_score, best_url = score, _wiki_url(title)
    return best_url, best_score


def find_url(name: str, alt: str = "", city: str = "Toronto") -> tuple:
    """Return (url, score, strategy) or (None, 0, '') if nothing passes threshold."""
    strategies = [(name, "name"), (f"{name} {city}", "name+city")]
    if alt and _norm(alt) != _norm(name):
        strategies = [(alt, "alt"), (f"{alt} {city}", "alt+city")] + strategies

    best_url, best_score, best_strat = None, 0.0, ""
    for query, strat in strategies:
        results = wiki_search(query)
        url, score = best_match(name, alt, results)
        if score > best_score:
            best_score, best_url, best_strat = score, url, strat
        if best_score >= ACCEPT_THRESHOLD:
            break
        time.sleep(1.2)  # respect Wikipedia rate limit between strategies

    return (best_url, best_score, best_strat) if best_score >= ACCEPT_THRESHOLD else (None, best_score, best_strat)


def main():
    with open(ACO_PATH, encoding="utf-8") as f:
        buildings = json.load(f)

    # Apply curated overrides first (always wins)
    curated_applied = 0
    for b in buildings:
        bid = str(b.get("id", ""))
        if bid in CURATED:
            b["wikipedia_url"] = CURATED[bid]
            curated_applied += 1

    to_enrich = [b for b in buildings
                 if not b.get("wikipedia_url")
                 and str(b.get("id", "")) not in NEVER_MATCH]
    already   = sum(1 for b in buildings if b.get("wikipedia_url"))
    print(f"Total: {len(buildings)}  |  Curated: {curated_applied}  |  "
          f"Already have URL: {already}  |  To search: {len(to_enrich)}\n")

    updated, low_conf = 0, []

    for b in to_enrich:
        name = b.get("name", "").strip()
        alt  = b.get("alternate_name", "").strip()
        city = b.get("city", "Toronto").strip()

        # Skip pure address records (no meaningful name to search for)
        if not name or re.match(r"^\d+\s+\w+", name) and len(name.split()) <= 4:
            b["wikipedia_url"] = ""
            continue

        print(f"  {name!r}", end=" ... ", flush=True)
        url, score, strat = find_url(name, alt, city)
        time.sleep(1.5)

        if url:
            b["wikipedia_url"] = url
            updated += 1
            print(f"OK ({score:.2f}, {strat})")
            print(f"      {url}")
        else:
            b["wikipedia_url"] = ""
            if score >= 0.35:
                low_conf.append((name, score, strat))
                print(f"skip ({score:.2f})")
            else:
                print(f"no match ({score:.2f})")

    with open(ACO_PATH, "w", encoding="utf-8") as f:
        json.dump(buildings, f, ensure_ascii=False, indent=2)

    print(f"\nApplied {curated_applied} curated URLs, found {updated} new via search.")

    if low_conf:
        print("\nLow-confidence (not added — review manually):")
        for nm, sc, st in sorted(low_conf, key=lambda x: -x[1]):
            print(f"  {sc:.2f}  {nm!r}")


if __name__ == "__main__":
    main()
