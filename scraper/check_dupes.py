"""Find seed records that are likely duplicates of heritage register records."""
import json, re
from pathlib import Path

geojson = json.loads((Path(__file__).parent.parent / "data" / "buildings.geojson").read_text())
features = geojson["features"]

register = [f for f in features if f["properties"]["source"] == "toronto_heritage_register"]
seeds    = [f for f in features if f["properties"]["source"] != "toronto_heritage_register"]

def street_key(addr: str) -> str:
    """Normalise to just the street portion for loose matching."""
    addr = addr.split(",")[0]              # drop city/province suffix
    addr = re.sub(r"\s+", " ", addr.lower().strip())
    addr = re.sub(r"\bst\b\.?", "st", addr)
    addr = re.sub(r"\bave?\b\.?", "ave", addr)
    addr = re.sub(r"\bblvd\b\.?", "blvd", addr)
    addr = re.sub(r"\brd\b\.?", "rd", addr)
    return addr

reg_keys = {street_key(f["properties"]["address"]): f["properties"] for f in register}

print(f"Register records : {len(register)}")
print(f"Seed records     : {len(seeds)}")
print()
print("Checking seeds for address matches in register...\n")

for s in seeds:
    sp = s["properties"]
    sk = street_key(sp["address"])
    if sk in reg_keys:
        rp = reg_keys[sk]
        print(f"DUPLICATE SEED   : {sp['name']}")
        print(f"  seed address   : {sp['address']}")
        print(f"  reg  address   : {rp['address']}")
        print(f"  seed firm      : {sp['firm']}")
        print(f"  reg  firm      : {rp['firm']}")
        print()
    else:
        # Also check if any register address starts with the same number+street
        seed_num = re.match(r"^(\d+\w*)\s+(\w+)", sk)
        if seed_num:
            prefix = seed_num.group(1) + " " + seed_num.group(2)
            matches = [k for k in reg_keys if k.startswith(prefix)]
            if matches:
                print(f"POSSIBLE MATCH   : {sp['name']}")
                print(f"  seed address   : {sp['address']}")
                print(f"  reg  candidates: {[reg_keys[m]['address'] for m in matches]}")
                print()
