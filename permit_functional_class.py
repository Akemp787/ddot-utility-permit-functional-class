"""
Utility permit functional class enrichment (DDOT CS12 Data Scientist assessment, Python test).

For 2026 Public Space Occupancy Permits ("Construction Staging Area" and "Mobile Crane Work Zone") held by
DC Water, PEPCO, or Washington Gas, find the FHWA functional class of the road at each permit location and the
highest functional class across each permit application. Output: one CSV row per permit application location.

Usage (from this folder):
    python permit_functional_class.py                   look up roads (cached points are reused)
    python permit_functional_class.py --offline         rebuild the CSV from saved data and cache only
    python permit_functional_class.py --sample 5        quick test on 5 applications per utility
    python permit_functional_class.py --refresh-source  re-download the permit data first
Settings are in the CONFIGURATION section below.
"""
import argparse
import json
import logging
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import pandas as pd
import requests

# =============================== CONFIGURATION ===============================
SCRIPT_DIR = Path(__file__).resolve().parent
PERMITS_JSON = SCRIPT_DIR / "data" / "occupancy_permits_2026_target_types.json"
CACHE_FILE = SCRIPT_DIR / "cache" / "lrs_lookups.jsonl"
OUTPUT_CSV = SCRIPT_DIR / "output" / "utility_permit_locations_functional_class.csv"
SAMPLE_CSV = SCRIPT_DIR / "output" / "sample_utility_permit_locations_functional_class.csv"

PERMITS_URL = "https://maps2.dcgis.dc.gov/dcgis/rest/services/DCGIS_APPS/DDOT_TOPS/MapServer/24"
LRS_URL = ("https://rh.dcgis.dc.gov/dcgis/rest/services/DDOT/RoadwayBlocks/MapServer/"
           "exts/DdotLrsExtensions/getPointOnSubBlock")

SCOPE_START = "2026-01-01"           # ApplicationDate OR IssueDate on/after this date, in US Eastern time
LOCAL_TZ = "America/New_York"
PERMIT_TYPES = ["Construction Staging Area", "Mobile Crane Work Zone"]

SEARCH_RADIUS_M = 50                 # LRS search radius in meters (service allows 0-200)
CLOSE_CALL_M = 5                     # flag another road with a different class within this distance of the nearest
EXCLUDED_ROUTE_TYPES = {"alley"}     # alleys have no functional class (assessment note)
MAX_WORKERS = 6                      # parallel lookups; kept low because it is a shared public service
HTTP_TIMEOUT, HTTP_RETRIES = 30, 3

# Utilities are identified only by explicit names and observed aliases, never by contractor names.
UTILITY_FIELDS = ["OwnerName", "PermitteeName", "ApplicantCompany"]
UTILITY_PATTERNS = {
    "DC Water": r"\bd ?c ?water\b",                                 # DC Water, DCWater/CIP, DCWater/OPS
    "PEPCO": r"\bpepco\b|\bpotomac electric power\b",              # PEPCO, Pepco Holdings Inc
    "Washington Gas": r"\bwashington gas\b|\bwash gas\b",          # Washington Gas, Wash Gas & Light Co.
}

# FHWA functional class order, 1 = highest. Matches the HPMS codes 1-7 in DDOT's Roadway SubBlock layer.
# Ranking uses the class name, not the LRS code (codes 11-19 run opposite to importance). An unknown name
# stops the run so nothing is ranked by guesswork.
FUNCTIONAL_CLASS_RANK = {
    "interstate": 1,
    "other freeway and expressway": 2,
    "principal arterial": 3,
    "minor arterial": 4,
    "major collector": 5,
    "minor collector": 6,
    "local": 7,
}
NO_CLASS_VALUES = {"unclassified", "uncassified"}   # "Uncassified" is the source's own spelling

OUTPUT_COLUMNS = [
    "TrackingNumber", "PermitNumber", "OBJECTID", "LocationID", "WorkLocationFullAddress", "Utility",
    "FunctionalClass", "PermitHighestFunctionalClass", "PermitHighestClassComplete",
    "FunctionalClassRank", "PermitHighestClassRank", "MatchStatus", "ReviewNote",
    "MatchedRoad", "MatchedRoadType", "RoadDistanceM", "SubBlockKey",
    "PermitLocations", "PermitUnresolvedLocations", "UtilityMatchedOn",
    "EventTypeDescription", "StatusDescription", "ApplicationDate", "IssueDate", "Longitude", "Latitude",
]
# =============================================================================

log = logging.getLogger("permits")


def get_json(url, params, method="GET"):
    """Request JSON with a timeout and a few retries. ArcGIS reports errors inside the JSON body."""
    for attempt in range(1, HTTP_RETRIES + 1):
        try:
            if method == "POST":
                resp = requests.post(url, data=params, timeout=HTTP_TIMEOUT)
            else:
                resp = requests.get(url, params=params, timeout=HTTP_TIMEOUT)
            resp.raise_for_status()
            data = resp.json()
            if "error" in data:
                raise RuntimeError(data["error"])
            return data
        except (requests.RequestException, ValueError, RuntimeError) as err:
            if attempt == HTTP_RETRIES:
                raise RuntimeError(f"{url} failed after {HTTP_RETRIES} attempts: {err}") from err
            time.sleep(2 * attempt)


def normalize(text):
    """Lowercase and turn punctuation into spaces: 'DCWater/CIP' -> 'dcwater cip'."""
    if not isinstance(text, str):
        return ""
    return " ".join(re.sub(r"[^a-z0-9]+", " ", text.lower()).split())


# ----------------------------- Permits -----------------------------
def download_permits(path=PERMITS_JSON):
    """Download in-scope permits in ID batches; the service returns at most 2,000 records per request."""
    types = ", ".join(f"'{t}'" for t in PERMIT_TYPES)
    where = (f"(ApplicationDate >= DATE '{SCOPE_START}' OR IssueDate >= DATE '{SCOPE_START}') "
             f"AND EventTypeDescription IN ({types})")
    query = f"{PERMITS_URL}/query"
    ids = sorted(get_json(query, {"where": where, "returnIdsOnly": "true", "f": "json"}, "POST")["objectIds"] or [])
    expected = get_json(query, {"where": where, "returnCountOnly": "true", "f": "json"}, "POST")["count"]
    if len(ids) != expected:
        raise RuntimeError(f"Server returned {len(ids)} IDs but reports {expected} matching records")
    features = []
    for start in range(0, len(ids), 500):
        batch = ids[start:start + 500]
        data = get_json(query, {"objectIds": ",".join(map(str, batch)), "outFields": "*",
                                "outSR": 4326, "f": "json"}, "POST")
        if len(data["features"]) != len(batch):
            raise RuntimeError(f"Batch starting at OBJECTID {batch[0]} came back incomplete")
        features.extend(data["features"])
        log.info("  downloaded %d / %d", len(features), len(ids))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"source_url": PERMITS_URL, "where": where, "features": features}), encoding="utf-8")


def load_permits(path=PERMITS_JSON):
    """Read the saved permits and keep in-scope rows. Dates arrive in UTC and are compared in Eastern time."""
    df = pd.DataFrame([f["attributes"] for f in json.loads(path.read_text(encoding="utf-8"))["features"]])
    for col in ["ApplicationDate", "IssueDate"]:
        df[col] = pd.to_datetime(df[col], unit="ms", utc=True).dt.tz_convert(LOCAL_TZ)
    start = pd.Timestamp(SCOPE_START, tz=LOCAL_TZ)
    in_scope = (df["ApplicationDate"] >= start) | (df["IssueDate"] >= start)
    kept = df[in_scope & df["EventTypeDescription"].isin(PERMIT_TYPES)].copy()
    log.info("Permit locations: %d in file, %d in scope", len(df), len(kept))
    return kept


def match_utility(row):
    """Return (utility, fields where its name was found). A name found in two utilities' patterns is 'Multiple'."""
    found = {}
    for field in UTILITY_FIELDS:
        text = normalize(row[field])
        for utility, pattern in UTILITY_PATTERNS.items():
            if re.search(pattern, text):
                found.setdefault(utility, []).append(field)
    if not found:
        return "", ""
    if len(found) > 1:
        return "Multiple", "; ".join(f"{u}: {', '.join(f)}" for u, f in found.items())
    (utility, fields), = found.items()
    return utility, ", ".join(fields)


# ----------------------------- Road lookups -----------------------------
def lookup_point(lon, lat):
    """Nearby roads from the LRS. routeType is omitted on purpose: omitting it returns every route type."""
    data = get_json(LRS_URL, {"f": "json", "x": lon, "y": lat, "inSR": 4326, "outSR": 4326,
                              "searchRadius": SEARCH_RADIUS_M, "includeSectionEvents": "true",
                              "returnNearestMatchOnly": "false"})
    if "pointOnSubBlocks" not in data:
        raise RuntimeError(f"Unexpected LRS response: {list(data)[:5]}")
    roads = []
    for c in data["pointOnSubBlocks"] or []:
        other = ((c.get("roadData") or {}).get("roadwayAttributes") or {}).get("other") or {}
        roads.append({"routeName": c.get("routeName"), "routeType": c.get("routeType"),
                      "subBlockKey": c.get("subBlockKey"), "offsetInMeters": c.get("offsetInMeters"),
                      "functionalClass": other.get("functionalClass")})
    return roads


def load_cache():
    """Cached lookups keyed by (lon, lat, radius). A half-written last line from an interrupted run is skipped."""
    cache = {}
    if CACHE_FILE.exists():
        for line in CACHE_FILE.read_text(encoding="utf-8").splitlines():
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            cache[(rec["lon"], rec["lat"], rec["radius"])] = rec["candidates"]
    return cache


def lookup_roads(points, offline=False):
    """Return {(lon, lat): nearby roads, or None if the lookup failed}.
    Each point is looked up once and saved to the cache, so an interrupted run resumes where it stopped."""
    cache = load_cache()
    results, todo = {}, []
    for lon, lat in points:
        roads = cache.get((lon, lat, SEARCH_RADIUS_M))
        if roads is None:
            todo.append((lon, lat))
        else:
            results[(lon, lat)] = roads
    log.info("Road lookups: %d unique points, %d cached, %d to query", len(points), len(results), len(todo))
    if offline or not todo:
        if todo:
            log.warning("  offline: %d points are not cached and will be reported as 'Lookup failed'", len(todo))
        results.update({p: None for p in todo})
        return results

    CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    started = time.time()
    pool = ThreadPoolExecutor(MAX_WORKERS)
    try:
        with open(CACHE_FILE, "a", encoding="utf-8") as cache_out:
            futures = {pool.submit(lookup_point, lon, lat): (lon, lat) for lon, lat in todo}
            for done, future in enumerate(as_completed(futures), 1):
                lon, lat = futures[future]
                try:
                    roads = future.result()
                    cache_out.write(json.dumps({"lon": lon, "lat": lat, "radius": SEARCH_RADIUS_M,
                                                "candidates": roads}) + "\n")
                    cache_out.flush()
                except RuntimeError as err:
                    log.warning("  lookup failed at (%s, %s): %s", lon, lat, err)
                    roads = None
                results[(lon, lat)] = roads
                if done % 100 == 0 or done == len(todo):
                    log.info("  %d / %d points looked up (%.0f s)", done, len(todo), time.time() - started)
    finally:
        pool.shutdown(wait=False, cancel_futures=True)   # Ctrl+C stops promptly; finished points stay cached
    return results


def distance(road):
    try:
        return abs(float(road.get("offsetInMeters")))
    except (TypeError, ValueError):
        return float("inf")


def choose_road(roads):
    """Pick the nearest non-alley road. Its class is kept even if missing; a farther road is never substituted."""
    if roads is None:
        return {"MatchStatus": "Lookup failed", "ReviewNote": "Road service did not respond; rerun to retry"}
    if not roads:
        return {"MatchStatus": f"No road within {SEARCH_RADIUS_M} m"}
    eligible = sorted((r for r in roads if str(r.get("routeType") or "").lower() not in EXCLUDED_ROUTE_TYPES),
                      key=distance)
    if not eligible:
        return {"MatchStatus": f"Only alleys within {SEARCH_RADIUS_M} m"}
    best = eligible[0]
    fclass = (best.get("functionalClass") or "").strip()
    result = {"MatchedRoad": best.get("routeName"), "MatchedRoadType": best.get("routeType"),
              "SubBlockKey": best.get("subBlockKey"), "RoadDistanceM": round(distance(best), 1),
              "FunctionalClass": fclass or None}
    if not fclass or fclass.lower() in NO_CLASS_VALUES:
        result["MatchStatus"] = "Nearest road has no functional class"
        return result
    result["MatchStatus"] = "Matched"
    rivals = [r for r in eligible[1:] if distance(r) - distance(best) <= CLOSE_CALL_M
              and (r.get("functionalClass") or "").strip() != fclass]
    if rivals:
        r = rivals[0]
        result["ReviewNote"] = (f"Close call: {r.get('routeName')} ({r.get('functionalClass') or 'no class'}) "
                                f"is {distance(r):.1f} m away")
    return result


def class_rank(name):
    return FUNCTIONAL_CLASS_RANK.get(normalize(name)) if isinstance(name, str) else None


# ----------------------------- Enrichment -----------------------------
def enrich(locs, offline=False):
    """Add the location's functional class and the permit's highest functional class to every location row."""
    has_xy = locs["Longitude"].notna() & locs["Latitude"].notna()
    points = sorted(set(zip(locs.loc[has_xy, "Longitude"], locs.loc[has_xy, "Latitude"])))
    roads = lookup_roads(points, offline)
    matches = [choose_road(roads[(lon, lat)]) if ok else {"MatchStatus": "No coordinates"}
               for lon, lat, ok in zip(locs["Longitude"], locs["Latitude"], has_xy)]
    match_cols = ["FunctionalClass", "MatchStatus", "ReviewNote", "MatchedRoad", "MatchedRoadType",
                  "RoadDistanceM", "SubBlockKey"]
    out = locs.reset_index(drop=True).join(pd.DataFrame(matches).reindex(columns=match_cols))

    matched = out["MatchStatus"].eq("Matched")
    out["FunctionalClassRank"] = out["FunctionalClass"].where(matched).map(class_rank).astype(float)
    unknown = out.loc[matched & out["FunctionalClassRank"].isna(), "FunctionalClass"].unique()
    if len(unknown):
        raise ValueError(f"Unrecognized functional class names {list(unknown)}. Check them, then add them "
                         f"to FUNCTIONAL_CLASS_RANK (or NO_CLASS_VALUES).")

    # Highest class per permit = best (lowest) rank among its resolved locations.
    best = (out.dropna(subset=["FunctionalClassRank"]).sort_values("FunctionalClassRank")
               .drop_duplicates("TrackingNumber").set_index("TrackingNumber"))
    by_permit = out.groupby("TrackingNumber")
    out["PermitHighestFunctionalClass"] = out["TrackingNumber"].map(best["FunctionalClass"])
    out["PermitHighestClassRank"] = out["TrackingNumber"].map(best["FunctionalClassRank"])
    out["PermitLocations"] = by_permit["OBJECTID"].transform("size")
    out["PermitUnresolvedLocations"] = by_permit["FunctionalClassRank"].transform(lambda s: s.isna().sum())
    # "No" means at least one location is unresolved, so the true highest class could be higher than shown.
    out["PermitHighestClassComplete"] = out["PermitUnresolvedLocations"].eq(0).map({True: "Yes", False: "No"})
    return out


def validate(out, locs):
    """Stop the run if anything was lost, duplicated, or ranked inconsistently."""
    lowest_rank = out.groupby("TrackingNumber")["FunctionalClassRank"].transform("min")
    checks = {
        "one output row per input location": len(out) == len(locs) and set(out["OBJECTID"]) == set(locs["OBJECTID"]),
        "OBJECTID unique": out["OBJECTID"].is_unique,
        "TrackingNumber + LocationID unique": not out.duplicated(["TrackingNumber", "LocationID"]).any(),
        "no alley selected": not out["MatchedRoadType"].fillna("").str.lower().isin(EXCLUDED_ROUTE_TYPES).any(),
        "every matched location has a rank": out.loc[out["MatchStatus"].eq("Matched"), "FunctionalClassRank"].notna().all(),
        "permit highest class = best location rank": lowest_rank.fillna(0).eq(out["PermitHighestClassRank"].fillna(0)).all(),
    }
    log.info("Checks:")
    for name, ok in checks.items():
        log.info("  %-42s %s", name, "OK" if ok else "FAILED")
    failed = [name for name, ok in checks.items() if not ok]
    if failed:
        raise ValueError(f"Validation failed: {failed}")


def write_csv(out, path):
    out = out.copy()
    for col in ["ApplicationDate", "IssueDate"]:
        out[col] = out[col].dt.strftime("%Y-%m-%d")
    for col in ["FunctionalClassRank", "PermitHighestClassRank"]:
        out[col] = out[col].astype("Int64")
    out = out[OUTPUT_COLUMNS].sort_values(["TrackingNumber", "OBJECTID"])
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        out.to_csv(path, index=False, encoding="utf-8-sig")   # BOM so Excel opens it cleanly
    except PermissionError:
        raise PermissionError(f"Cannot write {path}. Close it if it is open in Excel.") from None
    log.info("Wrote %d rows (%d permit applications) to %s", len(out), out["TrackingNumber"].nunique(), path)


def sample_permits(locs, per_utility, seed=2026):
    """All locations of N randomly chosen applications per utility, for a quick test run."""
    apps = locs.drop_duplicates("TrackingNumber")
    picked = apps.groupby("Utility")["TrackingNumber"].apply(lambda s: s.sample(min(per_utility, len(s)), random_state=seed))
    return locs[locs["TrackingNumber"].isin(picked)]


def main(argv=None):
    parser = argparse.ArgumentParser(description="Add FHWA functional class to utility permit locations.")
    parser.add_argument("--offline", action="store_true", help="use saved data and cached road lookups only")
    parser.add_argument("--sample", type=int, metavar="N", help="quick test on N applications per utility")
    parser.add_argument("--refresh-source", action="store_true", help="re-download the permit data first")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stdout)

    if args.refresh_source:
        download_permits()
    permits = load_permits()
    utility = permits.apply(match_utility, axis=1)
    permits["Utility"], permits["UtilityMatchedOn"] = utility.str[0], utility.str[1]
    locs = permits[permits["Utility"] != ""].reset_index(drop=True)
    log.info("Utility locations: %d in %d applications | %s", len(locs), locs["TrackingNumber"].nunique(),
             locs["Utility"].value_counts().to_dict())
    if args.sample:
        locs = sample_permits(locs, args.sample)
        log.info("Sample: %d locations in %d applications", len(locs), locs["TrackingNumber"].nunique())

    out = enrich(locs, args.offline)
    validate(out, locs)
    write_csv(out, SAMPLE_CSV if args.sample else OUTPUT_CSV)
    permits_out = out.drop_duplicates("TrackingNumber")
    log.info("Location results: %s", out["MatchStatus"].value_counts().to_dict())
    log.info("Highest class complete for permit: %s", permits_out["PermitHighestClassComplete"].value_counts().to_dict())


if __name__ == "__main__":
    main()
