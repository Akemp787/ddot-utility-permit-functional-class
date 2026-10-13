"""
Permit functional-class enrichment (DDOT CS12 Data Scientist assessment, Python test).

Selects 2026 Public Space Occupancy Permit locations (Construction Staging Area and Mobile Crane
Work Zone) for DC Water, PEPCO and Washington Gas, finds the FHWA functional class of the nearest
non-alley roadway at each location with the DDOT Linear Referencing System (LRS), and adds the
highest functional class per permit application. Output: one CSV row per permit application location.

Run from this folder on Windows (any terminal with Python 3.10+ and the packages in requirements.txt):
    python permit_functional_class.py --offline         rebuild the CSV from saved data and lookups (no web requests)
    python permit_functional_class.py                   all locations (resumable)
    python permit_functional_class.py --pilot 5         pilot: 5 applications per utility
    python permit_functional_class.py --refresh-source  re-download permits first
Paths and matching assumptions are set in the CONFIGURATION section below.
"""
import argparse
import hashlib
import json
import re
import shutil
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import requests

# =============================== CONFIGURATION ===============================
SCRIPT_DIR = Path(__file__).resolve().parent
DATA_DIR = SCRIPT_DIR / "data"
OUTPUT_DIR = SCRIPT_DIR / "output"
CHECKPOINT_DIR = SCRIPT_DIR / "checkpoints"
SAVED_PERMITS_JSON = DATA_DIR / "occupancy_permits_2026_target_types.json"
OUTPUT_CSV = OUTPUT_DIR / "utility_permit_locations_functional_class.csv"
PILOT_CSV = OUTPUT_DIR / "pilot_utility_permit_locations_functional_class.csv"

PERMITS_URL = "https://maps2.dcgis.dc.gov/dcgis/rest/services/DCGIS_APPS/DDOT_TOPS/MapServer/24"
LRS_URL = ("https://rh.dcgis.dc.gov/dcgis/rest/services/DDOT/RoadwayBlocks/MapServer/"
           "exts/DdotLrsExtensions/getPointOnSubBlock")

# Scope: ApplicationDate OR IssueDate on/after SCOPE_START, evaluated in DATE_SCOPE_TZ.
SCOPE_START = "2026-01-01"
AFTER_TARGET_YEAR_DATE = "2027-01-01"    # later dates are kept but flagged for review
DATE_SCOPE_TZ = "America/New_York"       # ArcGIS REST returns dates as epoch milliseconds in UTC
TARGET_EVENT_TYPES = ["Construction Staging Area", "Mobile Crane Work Zone"]

# Roadway matching assumptions.
LRS_SEARCH_RADIUS_M = 50                 # meters; the service accepts 0-200
TIE_THRESHOLD_M = 5.0                    # eligible candidates within this distance of the nearest are compared
EXCLUDED_ROUTE_TYPES = {"alley"}         # alleys have no functional class (assessment note)

HTTP_TIMEOUT = 30
HTTP_RETRIES = 3
HTTP_BACKOFF_SECONDS = 2
MAX_WORKERS = 6                          # parallel LRS requests; kept modest for a shared public service
ID_BATCH_SIZE = 500
PROGRESS_EVERY = 10
CHECKPOINT_VERSION = 1

DC_BBOX = {"lon_min": -77.12, "lon_max": -76.90, "lat_min": 38.79, "lat_max": 39.00}

PERMIT_FIELDS = [
    "OBJECTID", "TrackingNumber", "PermitNumber", "StatusDescription", "EventTypeDescription",
    "ApplicationDate", "IssueDate", "OwnerName", "PermitteeName", "ApplicantCompany", "LocationID",
    "WorkLocationAddressID", "WorkLocationFullAddress", "MultiAddresses", "Longitude", "Latitude",
]

# Utilities are matched only on explicit names in these fields (contractor names alone are not used).
UTILITY_FIELDS = ["OwnerName", "PermitteeName", "ApplicantCompany"]
UTILITY_PATTERNS = {
    "DC Water": [r"\bd ?c ?water\b"],
    "PEPCO": [r"\bpepco\b", r"\bpotomac electric power\b"],
    "Washington Gas": [r"\bwashington gas\b", r"\bwash gas\b"],
}
UTILITY_EXACT_ALIASES = {
    "DC Water": {"dc water", "dcwater", "dcwater cip", "dcwater ops"},
    "PEPCO": {"pepco", "pepco holdings inc", "potomac electric power company"},
    "Washington Gas": {"washington gas", "wash gas light co", "wash gas co"},
}

# FHWA functional system order (1 = highest). Source class names are matched after normalization.
# Any class name not listed here (and not in UNCLASSIFIED_NAMES) stops the run unless explicitly allowed.
FHWA_HIERARCHY = [
    (1, "Interstate", ["interstate"]),
    (2, "Other Freeways and Expressways", ["other freeways and expressways", "other freeway and expressway",
                                           "freeways and expressways", "freeway and expressway"]),
    (3, "Other Principal Arterial", ["other principal arterial", "principal arterial",
                                     "principal arterial other"]),
    (4, "Minor Arterial", ["minor arterial"]),
    (5, "Major Collector / Collector", ["major collector", "collector"]),
    (6, "Minor Collector", ["minor collector"]),
    (7, "Local", ["local"]),
]

# Source values that mean "no functional class" (including the source's own misspelling). Kept as text, never ranked.
UNCLASSIFIED_NAMES = {"unclassified", "uncassified"}

OUTPUT_COLUMNS = [
    "TrackingNumber", "PermitNumber", "OBJECTID", "LocationID", "WorkLocationAddressID",
    "WorkLocationFullAddress", "MultiAddresses", "Longitude", "Latitude", "coordinate_source",
    "StatusDescription", "EventTypeDescription", "ApplicationDate_ET", "IssueDate_ET",
    "date_scope_basis", "flag_scope_timezone_sensitive", "flag_date_after_2026",
    "utility_name", "utility_matches", "utility_match_type", "utility_match_fields", "utility_evidence",
    "flag_multiple_utilities", "OwnerName", "PermitteeName", "ApplicantCompany",
    "match_status", "functional_class", "functional_class_code", "functional_class_rank",
    "road_route_name", "road_route_type", "road_subblock_key", "road_offset_m", "address_road_agreement",
    "candidate_count", "eligible_candidate_count", "alley_candidates_excluded", "nearest_alley_offset_m",
    "flag_location_unresolved", "flag_missing_class", "flag_unmapped_class", "flag_lookup_failed",
    "flag_no_eligible_road", "flag_ambiguous_match", "flag_close_conflicting_road",
    "flag_close_conflicting_class", "ambiguity_detail", "lookup_error",
    "app_location_count", "app_resolved_locations", "app_unresolved_locations", "app_ambiguous_locations",
    "app_highest_functional_class", "app_highest_functional_class_code",
    "app_highest_functional_class_rank", "app_functional_classes", "app_highest_class_status",
    "flag_app_highest_class_incomplete", "flag_app_has_ambiguous_location",
]
# =============================================================================


# ----------------------------- HTTP and general helpers -----------------------------
SESSION = requests.Session()


def get_json(url, params, method="get"):
    """HTTP request with bounded retries, timeout, and JSON-level error checks."""
    last_err = None
    for attempt in range(1, HTTP_RETRIES + 1):
        try:
            if method == "post":
                resp = SESSION.post(url, data=params, timeout=HTTP_TIMEOUT)
            else:
                resp = SESSION.get(url, params=params, timeout=HTTP_TIMEOUT)
            resp.raise_for_status()
            data = resp.json()
            if isinstance(data, dict) and "error" in data:
                raise RuntimeError(f"Service error: {data['error']}")
            return data
        except (requests.RequestException, ValueError, RuntimeError) as err:
            last_err = err
            if attempt < HTTP_RETRIES:
                time.sleep(HTTP_BACKOFF_SECONDS * attempt)
    raise RuntimeError(f"Request failed after {HTTP_RETRIES} attempts: {last_err}") from last_err


def _blank_value(v):
    if v is None:
        return True
    try:
        if pd.isna(v):
            return True
    except (TypeError, ValueError):
        pass
    return str(v).strip() == ""


def _blank(series):
    return series.isna() | series.astype(str).str.strip().eq("")


def _to_float(v):
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return None if np.isnan(x) else x


def _clean(v):
    return None if _blank_value(v) else str(v).strip()


def normalize_name(value):
    if _blank_value(value):
        return ""
    text = str(value).lower().replace("&", " ")
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


# ----------------------------- Source data -----------------------------
def scope_where():
    types_sql = ", ".join(f"'{t}'" for t in TARGET_EVENT_TYPES)
    return (f"(ApplicationDate >= DATE '{SCOPE_START}' OR IssueDate >= DATE '{SCOPE_START}') "
            f"AND EventTypeDescription IN ({types_sql})")


def arcgis_count(layer_url, where):
    data = get_json(f"{layer_url}/query", {"where": where, "returnCountOnly": "true", "f": "json"}, "post")
    if "count" not in data:
        raise RuntimeError(f"No 'count' in response: {str(data)[:300]}")
    return int(data["count"])


def download_permits(out_path=SAVED_PERMITS_JSON):
    """Download in-scope permits by OBJECTID batches and verify completeness before saving."""
    where = scope_where()
    layer = get_json(PERMITS_URL, {"f": "json"})
    batch = max(1, min(ID_BATCH_SIZE, int(layer.get("maxRecordCount") or ID_BATCH_SIZE)))
    expected = arcgis_count(PERMITS_URL, where)
    ids_resp = get_json(f"{PERMITS_URL}/query", {"where": where, "returnIdsOnly": "true", "f": "json"}, "post")
    id_field = ids_resp.get("objectIdFieldName") or "OBJECTID"
    ids = sorted(ids_resp.get("objectIds") or [])
    if len(ids) != expected or len(set(ids)) != len(ids):
        raise RuntimeError(f"ID list ({len(ids)}) does not match count ({expected}) or has duplicates")
    print(f"Downloading {len(ids)} records in batches of {batch}", flush=True)
    features = []
    for start in range(0, len(ids), batch):
        chunk = ids[start:start + batch]
        data = get_json(f"{PERMITS_URL}/query",
                        {"objectIds": ",".join(map(str, chunk)), "outFields": "*", "returnGeometry": "true",
                         "outSR": 4326, "f": "json"}, "post")
        got = sorted(f["attributes"][id_field] for f in data.get("features", []))
        if got != chunk:
            raise RuntimeError(f"Batch starting at ID {chunk[0]} returned {len(got)} of {len(chunk)} records")
        features.extend(data["features"])
        print(f"  {len(features)}/{len(ids)}", flush=True)
    payload = {"source_url": PERMITS_URL, "where": where, "geometry_spatial_reference": 4326,
               "downloaded_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
               "record_count": len(features), "features": features}
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_path.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f)
    if out_path.exists():
        backup = out_path.with_name(f"{out_path.stem}_backup_{datetime.now():%Y%m%d_%H%M%S}{out_path.suffix}")
        shutil.copy2(out_path, backup)
        print(f"Previous file backed up to {backup.name}", flush=True)
    tmp.replace(out_path)
    return out_path


def load_permits(path=SAVED_PERMITS_JSON):
    with open(path, "r", encoding="utf-8") as f:
        payload = json.load(f)
    meta = {k: v for k, v in payload.items() if k != "features"}
    rows = []
    for feat in payload.get("features", []):
        attrs = dict(feat.get("attributes") or {})
        geom = feat.get("geometry") or {}
        attrs["geom_x"], attrs["geom_y"] = geom.get("x"), geom.get("y")
        rows.append(attrs)
    df = pd.DataFrame(rows)
    missing = [c for c in PERMIT_FIELDS if c not in df.columns]
    if missing:
        raise KeyError(f"Permit file is missing fields: {missing}")
    meta["feature_count"] = len(rows)
    return df, meta


# ----------------------------- Dates, scope, and base validation -----------------------------
def parse_arcgis_date(series):
    if pd.api.types.is_numeric_dtype(series):
        return pd.to_datetime(series, unit="ms", utc=True, errors="coerce")
    return pd.to_datetime(series, utc=True, errors="coerce")


def add_date_columns(df):
    out = df.copy()
    start_local = pd.Timestamp(SCOPE_START, tz=DATE_SCOPE_TZ)
    start_utc = pd.Timestamp(SCOPE_START, tz="UTC")
    after_local = pd.Timestamp(AFTER_TARGET_YEAR_DATE, tz=DATE_SCOPE_TZ)
    for col in ("ApplicationDate", "IssueDate"):
        utc = parse_arcgis_date(out[col])
        out[f"{col}_utc"] = utc
        out[f"{col}_local"] = utc.dt.tz_convert(DATE_SCOPE_TZ)
    app = out["ApplicationDate_local"].ge(start_local)
    iss = out["IssueDate_local"].ge(start_local)
    out["in_scope"] = app | iss
    out["date_scope_basis"] = np.select([app & iss, app, iss],
                                        ["Both dates", "ApplicationDate only", "IssueDate only"], default="Neither")
    in_scope_utc = out["ApplicationDate_utc"].ge(start_utc) | out["IssueDate_utc"].ge(start_utc)
    out["flag_scope_timezone_sensitive"] = in_scope_utc.ne(out["in_scope"])
    out["flag_date_after_2026"] = (out["ApplicationDate_local"].ge(after_local)
                                   | out["IssueDate_local"].ge(after_local))
    return out


def filter_scope(df):
    in_types = df["EventTypeDescription"].isin(TARGET_EVENT_TYPES)
    keep = df["in_scope"] & in_types
    counts = {"input_rows": len(df), "outside_date_scope": int((~df["in_scope"]).sum()),
              "outside_event_types": int((~in_types).sum()), "kept_rows": int(keep.sum())}
    return df.loc[keep].copy(), counts


def validate_base_records(df):
    lon = pd.to_numeric(df["Longitude"], errors="coerce")
    lat = pd.to_numeric(df["Latitude"], errors="coerce")
    inside = (lon.between(DC_BBOX["lon_min"], DC_BBOX["lon_max"])
              & lat.between(DC_BBOX["lat_min"], DC_BBOX["lat_max"]))
    return pd.Series({
        "rows": len(df),
        "applications": int(df["TrackingNumber"].nunique()),
        "OBJECTID missing": int(df["OBJECTID"].isna().sum()),
        "OBJECTID duplicated": int(df["OBJECTID"].duplicated().sum()),
        "TrackingNumber blank": int(_blank(df["TrackingNumber"]).sum()),
        "LocationID blank": int(_blank(df["LocationID"]).sum()),
        "TrackingNumber+LocationID duplicated": int(df.duplicated(["TrackingNumber", "LocationID"]).sum()),
        "scope timezone-sensitive rows": int(df["flag_scope_timezone_sensitive"].sum()),
        "rows with a date after 2026": int(df["flag_date_after_2026"].sum()),
        "coordinates missing": int((lon.isna() | lat.isna()).sum()),
        "coordinates outside rough DC box": int((~inside & lon.notna() & lat.notna()).sum()),
    }, dtype="object")


# ----------------------------- Utility classification, coordinates, pilot sampling -----------------------------
def match_name(norm):
    return [u for u, pats in UTILITY_PATTERNS.items() if any(re.search(p, norm) for p in pats)]


def classify_utilities(df):
    out = df.copy()
    n = len(out)
    hits = [set() for _ in range(n)]
    fields_hit = [[] for _ in range(n)]
    evidence = [[] for _ in range(n)]
    any_contains = [False] * n
    for field in UTILITY_FIELDS:
        for i, norm in enumerate(out[field].map(normalize_name).tolist()):
            for u in match_name(norm):
                exact = norm in UTILITY_EXACT_ALIASES[u]
                any_contains[i] = any_contains[i] or not exact
                hits[i].add(u)
                if field not in fields_hit[i]:
                    fields_hit[i].append(field)
                evidence[i].append(f"{field}={u} ({'exact alias' if exact else 'contains name'})")
    out["utility_matches"] = ["|".join(sorted(h)) for h in hits]
    out["utility_match_count"] = [len(h) for h in hits]
    out["utility_name"] = [next(iter(h)) if len(h) == 1 else ("MULTIPLE" if h else "") for h in hits]
    out["utility_match_fields"] = ["|".join(f) for f in fields_hit]
    out["utility_evidence"] = ["; ".join(e) for e in evidence]
    out["utility_match_type"] = ["" if not h else ("contains" if c else "exact") for h, c in zip(hits, any_contains)]
    out["flag_multiple_utilities"] = out["utility_match_count"].gt(1)
    out["is_utility"] = out["utility_match_count"].gt(0)
    return out


def add_lookup_coordinates(df):
    out = df.copy()
    lon = pd.to_numeric(out["Longitude"], errors="coerce")
    lat = pd.to_numeric(out["Latitude"], errors="coerce")
    gx = pd.to_numeric(out["geom_x"], errors="coerce")
    gy = pd.to_numeric(out["geom_y"], errors="coerce")
    has_attr = lon.notna() & lat.notna()
    use_geom = ~has_attr & gx.notna() & gy.notna()
    out["lookup_lon"] = lon.where(~use_geom, gx)
    out["lookup_lat"] = lat.where(~use_geom, gy)
    out["coordinate_source"] = np.select([has_attr, use_geom], ["Longitude/Latitude", "geometry"], default="missing")
    return out


def pilot_sample(locs, apps_per_utility, seed=2026):
    """Pick whole applications (all their locations) per single-utility group."""
    apps = (locs.loc[locs["utility_match_count"].eq(1), ["TrackingNumber", "utility_name"]]
                .drop_duplicates("TrackingNumber"))
    picks = []
    for _, g in apps.groupby("utility_name"):
        picks.extend(g.sample(min(apps_per_utility, len(g)), random_state=seed)["TrackingNumber"])
    return locs[locs["TrackingNumber"].isin(picks)].copy()


# ----------------------------- LRS lookups with resumable checkpoints -----------------------------
def lrs_params(radius=LRS_SEARCH_RADIUS_M):
    # routeType is omitted on purpose: omission returns all route types; "All" is rejected by the service.
    return {"f": "json", "inSR": 4326, "outSR": 4326, "searchRadius": int(radius),
            "includeSectionEvents": "true", "returnNearestMatchOnly": "false"}


def checkpoint_path(radius=LRS_SEARCH_RADIUS_M):
    sig = json.dumps({"url": LRS_URL, "params": lrs_params(radius), "version": CHECKPOINT_VERSION}, sort_keys=True)
    return CHECKPOINT_DIR / f"lrs_lookups_{hashlib.sha256(sig.encode('utf-8')).hexdigest()[:12]}.jsonl"


def load_checkpoint(path):
    records = {}
    if Path(path).exists():
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue  # partial line from an interrupted run
                records[int(rec["OBJECTID"])] = rec
    return records


def summarize_candidate(c):
    other = (((c.get("roadData") or {}).get("roadwayAttributes") or {}).get("other") or {})
    return {"routeName": c.get("routeName"), "routeType": c.get("routeType"),
            "subBlockKey": c.get("subBlockKey"), "offsetInMeters": c.get("offsetInMeters"),
            "functionalClass": other.get("functionalClass"),
            "functionalClassCode": other.get("functionalClassCode")}


def lrs_lookup(lon, lat, radius=LRS_SEARCH_RADIUS_M):
    params = dict(lrs_params(radius), x=lon, y=lat)
    try:
        data = get_json(LRS_URL, params)
    except RuntimeError as err:
        return {"lookup_status": "error", "error": str(err)[:500], "candidates": []}
    if not isinstance(data, dict) or "pointOnSubBlocks" not in data:
        keys = list(data)[:10] if isinstance(data, dict) else type(data).__name__
        return {"lookup_status": "error", "error": f"Unexpected response (keys: {keys})", "candidates": []}
    cands = [summarize_candidate(c) for c in (data.get("pointOnSubBlocks") or [])]
    return {"lookup_status": "ok", "error": "", "candidates": cands}


def enrich_locations(locs, radius=LRS_SEARCH_RADIUS_M, offline=False):
    """Query the LRS with a resumable JSONL checkpoint, in parallel, once per unique coordinate.
    Successful lookups are reused only if the request parameters and the coordinates are unchanged;
    failed lookups are retried on the next run. Road selection is recomputed every run."""
    path = checkpoint_path(radius)
    done = load_checkpoint(path)
    results, todo, reused = {}, [], 0
    for oid, lon, lat in zip(locs["OBJECTID"], locs["lookup_lon"], locs["lookup_lat"]):
        oid, lon, lat = int(oid), _to_float(lon), _to_float(lat)
        if lon is None or lat is None:
            results[oid] = {"OBJECTID": oid, "lon": lon, "lat": lat, "lookup_status": "no_coordinates",
                            "error": "", "candidates": []}
            continue
        rec = done.get(oid)
        if rec and rec.get("lookup_status") == "ok" and rec.get("lon") == lon and rec.get("lat") == lat:
            results[oid] = rec
            reused += 1
        else:
            todo.append((oid, lon, lat))
    print(f"LRS checkpoint {path.name}: reused {reused}, to query {len(todo)}", flush=True)
    if todo and offline:
        # Offline: reuse saved failed lookups as-is (they stay flagged lookup_failed); never query the service.
        never_queried = [oid for oid, lon, lat in todo
                         if not (done.get(oid) and done[oid].get("lon") == lon and done[oid].get("lat") == lat)]
        if never_queried:
            raise RuntimeError(f"Offline mode: {len(never_queried)} locations have no saved lookup in {path}")
        for oid, _, _ in todo:
            results[oid] = done[oid]
        print(f"  offline: reused {len(todo)} saved failed lookups (flagged lookup_failed)", flush=True)
        return results
    if not todo:
        return results

    by_coord = {}
    for oid, lon, lat in todo:
        by_coord.setdefault((lon, lat), []).append(oid)
    total = len(by_coord)
    print(f"  {total} unique coordinates to query, {MAX_WORKERS} at a time", flush=True)

    CHECKPOINT_DIR.mkdir(parents=True, exist_ok=True)
    t0, errors = time.time(), 0
    pool = ThreadPoolExecutor(max_workers=MAX_WORKERS)
    try:
        with open(path, "a", encoding="utf-8") as f:
            futures = {pool.submit(lrs_lookup, lon, lat, radius): (lon, lat) for lon, lat in by_coord}
            for i, fut in enumerate(as_completed(futures), 1):
                lon, lat = futures[fut]
                base = fut.result()
                stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
                for oid in by_coord[(lon, lat)]:
                    rec = dict(base, OBJECTID=oid, lon=lon, lat=lat, queried_utc=stamp)
                    f.write(json.dumps(rec) + "\n")
                    results[oid] = rec
                f.flush()
                errors += base["lookup_status"] != "ok"
                if i == 1 or i % PROGRESS_EVERY == 0 or i == total:
                    elapsed = time.time() - t0
                    remaining = elapsed / i * (total - i)
                    print(f"  {i}/{total} coordinates | failures {errors} | {elapsed:.0f}s elapsed | "
                          f"~{remaining / 60:.1f} min left", flush=True)
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    return results


# ----------------------------- Road selection -----------------------------
MATCH_COLUMNS = ["match_status", "lookup_error", "candidate_count", "eligible_candidate_count",
                 "alley_candidates_excluded", "nearest_alley_offset_m", "road_route_name", "road_route_type",
                 "road_subblock_key", "road_offset_m", "functional_class", "functional_class_code",
                 "flag_close_conflicting_road", "flag_close_conflicting_class", "ambiguity_detail"]


def _offset(c):
    try:
        return abs(float(c.get("offsetInMeters")))
    except (TypeError, ValueError):
        return float("inf")


def choose_road(rec, tie_threshold=TIE_THRESHOLD_M):
    """Nearest eligible (non-alley) road by absolute offset; its class is kept even if missing."""
    cands = rec.get("candidates") or []
    is_excluded = [str(c.get("routeType") or "").strip().lower() in EXCLUDED_ROUTE_TYPES for c in cands]
    excluded = [c for c, x in zip(cands, is_excluded) if x]
    eligible = sorted((c for c, x in zip(cands, is_excluded) if not x), key=_offset)
    out = {"lookup_error": rec.get("error") or "", "candidate_count": len(cands),
           "eligible_candidate_count": len(eligible), "alley_candidates_excluded": len(excluded),
           "nearest_alley_offset_m": min(map(_offset, excluded)) if excluded else np.nan,
           "road_route_name": None, "road_route_type": None, "road_subblock_key": None, "road_offset_m": np.nan,
           "functional_class": None, "functional_class_code": None,
           "flag_close_conflicting_road": False, "flag_close_conflicting_class": False, "ambiguity_detail": ""}
    status = rec.get("lookup_status")
    if status == "no_coordinates":
        out["match_status"] = "no_coordinates"
        return out
    if status != "ok":
        out["match_status"] = "lookup_failed"
        return out
    if not cands:
        out["match_status"] = "no_candidates"
        return out
    if not eligible:
        out["match_status"] = "only_excluded_route_types"
        return out
    best = eligible[0]
    best_d = _offset(best)
    out.update(road_route_name=_clean(best.get("routeName")), road_route_type=_clean(best.get("routeType")),
               road_subblock_key=_clean(best.get("subBlockKey")),
               road_offset_m=best_d if np.isfinite(best_d) else np.nan,
               functional_class=_clean(best.get("functionalClass")),
               functional_class_code=_clean(best.get("functionalClassCode")))
    out["match_status"] = "matched" if out["functional_class"] else "matched_missing_class"
    details = []
    for c in eligible[1:]:
        d = _offset(c)
        if d - best_d > tie_threshold:
            break
        diff_road = normalize_name(c.get("routeName")) != normalize_name(best.get("routeName"))
        c_class = _clean(c.get("functionalClass"))
        diff_class = c_class != out["functional_class"]
        if diff_road or diff_class:
            out["flag_close_conflicting_road"] |= diff_road
            out["flag_close_conflicting_class"] |= diff_class
            details.append(f"{c.get('routeName')} [{c.get('routeType')}] {c_class or 'no class'} at {d:.1f} m")
    out["ambiguity_detail"] = "; ".join(details)
    return out


# ----------------------------- Functional-class hierarchy and address agreement -----------------------------
def normalize_class_name(v):
    return "" if _blank_value(v) else normalize_name(str(v).replace("&", " and "))


CLASS_RANK_LOOKUP = {normalize_class_name(alias): rank for rank, _, aliases in FHWA_HIERARCHY for alias in aliases}


def class_rank(v):
    return CLASS_RANK_LOOKUP.get(normalize_class_name(v), np.nan)


def validate_class_mapping(loc):
    """Observed (class, code) pairs with their rank; reports unmapped names and inconsistent codes."""
    m = loc[loc["functional_class"].notna()]
    table = (m.groupby(["functional_class", "functional_class_code"], dropna=False).size()
              .rename("locations").reset_index())
    table["rank"] = table["functional_class"].map(class_rank)
    unranked = table["rank"].isna() & ~table["functional_class"].map(normalize_class_name).isin(UNCLASSIFIED_NAMES)
    issues = [f"Unmapped functional class name: {n!r}" for n in table.loc[unranked, "functional_class"]]
    pairs = table.dropna(subset=["functional_class_code", "rank"])
    code_ranks = pairs.groupby("functional_class_code")["rank"].nunique()
    issues += [f"Code {c!r} maps to {n} different ranks" for c, n in code_ranks[code_ranks > 1].items()]
    rank_codes = pairs.groupby("rank")["functional_class_code"].nunique()
    issues += [f"Rank {int(r)} has {n} different codes" for r, n in rank_codes[rank_codes > 1].items()]
    return table.sort_values(["rank", "functional_class_code"]).reset_index(drop=True), issues


STREET_TYPE_ABBREV = {
    "street": "st", "st": "st", "avenue": "ave", "ave": "ave", "av": "ave", "road": "rd", "rd": "rd",
    "place": "pl", "pl": "pl", "drive": "dr", "dr": "dr", "boulevard": "blvd", "blvd": "blvd",
    "terrace": "ter", "ter": "ter", "court": "ct", "ct": "ct", "circle": "cir", "cir": "cir",
    "parkway": "pkwy", "pkwy": "pkwy", "lane": "ln", "ln": "ln", "way": "way", "square": "sq", "sq": "sq",
    "highway": "hwy", "hwy": "hwy", "bridge": "brg", "brg": "brg", "alley": "aly", "aly": "aly",
    "expressway": "expy", "expy": "expy", "freeway": "fwy", "fwy": "fwy",
}
STREET_TYPES = set(STREET_TYPE_ABBREV.values())
QUADRANTS = {"nw", "ne", "sw", "se"}


def street_core_tokens(text):
    tokens = [STREET_TYPE_ABBREV.get(t, t) for t in normalize_name(text).split()]
    return {t for t in tokens if t not in QUADRANTS and t not in STREET_TYPES and not t.isdigit()}


def address_road_agreement(address, route_name):
    route, addr = street_core_tokens(route_name), street_core_tokens(address)
    if not route or not addr:
        return "unknown"
    return "agree" if route <= addr else "differ"


# ----------------------------- Assembly, application summary, validation, output -----------------------------
def build_location_table(locs, lookups, tie_threshold=TIE_THRESHOLD_M):
    locs = locs.copy()
    locs["OBJECTID"] = locs["OBJECTID"].astype("int64")
    rows = []
    for oid in locs["OBJECTID"]:
        rec = lookups.get(int(oid))
        r = choose_road(rec, tie_threshold) if rec is not None else {"match_status": "not_looked_up"}
        r["OBJECTID"] = int(oid)
        rows.append(r)
    matches = pd.DataFrame(rows).reindex(columns=["OBJECTID"] + MATCH_COLUMNS)
    out = locs.merge(matches, on="OBJECTID", how="left", validate="one_to_one")
    if len(out) != len(locs):
        raise RuntimeError("Row count changed during the lookup join")
    for c in ["flag_close_conflicting_road", "flag_close_conflicting_class"]:
        out[c] = out[c].eq(True)
    out["functional_class_rank"] = out["functional_class"].map(class_rank)
    unclassified = out["functional_class"].map(normalize_class_name).isin(UNCLASSIFIED_NAMES)
    out.loc[unclassified, "match_status"] = "matched_unclassified"
    unmapped = out["match_status"].eq("matched") & out["functional_class_rank"].isna()
    out.loc[unmapped, "match_status"] = "matched_unmapped_class"
    out["location_resolved"] = out["functional_class_rank"].notna()
    out["flag_location_unresolved"] = ~out["location_resolved"]
    out["flag_missing_class"] = out["match_status"].isin(["matched_missing_class", "matched_unclassified"])
    out["flag_unmapped_class"] = out["match_status"].eq("matched_unmapped_class")
    out["flag_lookup_failed"] = out["match_status"].isin(["lookup_failed", "no_coordinates", "not_looked_up"])
    out["flag_no_eligible_road"] = out["match_status"].isin(["no_candidates", "only_excluded_route_types"])
    out["flag_ambiguous_match"] = out["flag_close_conflicting_road"] | out["flag_close_conflicting_class"]
    out["address_road_agreement"] = [address_road_agreement(a, r) for a, r in
                                     zip(out["WorkLocationFullAddress"], out["road_route_name"])]
    return out


def summarize_applications(loc):
    g = loc.groupby("TrackingNumber")
    summary = pd.DataFrame({
        "app_location_count": g.size(),
        "app_resolved_locations": g["location_resolved"].sum(),
        "app_ambiguous_locations": g["flag_ambiguous_match"].sum(),
    })
    summary["app_unresolved_locations"] = summary["app_location_count"] - summary["app_resolved_locations"]
    resolved = loc[loc["location_resolved"]]
    best = (resolved.sort_values(["TrackingNumber", "functional_class_rank", "OBJECTID"])
                    .drop_duplicates("TrackingNumber").set_index("TrackingNumber"))
    summary["app_highest_functional_class"] = best["functional_class"]
    summary["app_highest_functional_class_code"] = best["functional_class_code"]
    summary["app_highest_functional_class_rank"] = best["functional_class_rank"]
    summary["app_functional_classes"] = resolved.groupby("TrackingNumber")["functional_class"].agg(
        lambda s: " | ".join(sorted(set(s))))
    summary["flag_app_highest_class_incomplete"] = summary["app_unresolved_locations"].gt(0)
    summary["flag_app_has_ambiguous_location"] = summary["app_ambiguous_locations"].gt(0)
    summary["app_highest_class_status"] = np.select(
        [summary["app_resolved_locations"].eq(0), summary["flag_app_highest_class_incomplete"]],
        ["no resolved locations", "incomplete: unresolved location(s)"], default="complete")
    return summary.reset_index()


def attach_summary(loc, summary):
    final = loc.merge(summary, on="TrackingNumber", how="left", validate="many_to_one")
    if len(final) != len(loc):
        raise RuntimeError("Row count changed during the application-summary join")
    return final


def validate_final(final, locs):
    resolved = final["location_resolved"]
    loc_min = final[resolved].groupby("TrackingNumber")["functional_class_rank"].min()
    app_rank = final.drop_duplicates("TrackingNumber").set_index("TrackingNumber")["app_highest_functional_class_rank"]
    selected_types = final["road_route_type"].dropna().astype(str).str.strip().str.lower()
    any_unresolved = final.groupby("TrackingNumber")["flag_location_unresolved"].transform("any")
    checks = {
        "row count equals input locations": len(final) == len(locs),
        "OBJECTID set equals input": set(final["OBJECTID"].astype(int)) == set(locs["OBJECTID"].astype(int)),
        "OBJECTID unique": final["OBJECTID"].is_unique,
        "TrackingNumber+LocationID unique": not final.duplicated(["TrackingNumber", "LocationID"]).any(),
        "application count preserved": final["TrackingNumber"].nunique() == locs["TrackingNumber"].nunique(),
        "no excluded route type selected": not selected_types.isin(EXCLUDED_ROUTE_TYPES).any(),
        "matched rows have a mapped rank": final.loc[final["match_status"].eq("matched"),
                                                     "functional_class_rank"].notna().all(),
        "app highest rank equals min location rank": app_rank.reindex(loc_min.index).eq(loc_min).all(),
        "apps without resolved locations have no highest class": app_rank.drop(loc_min.index).isna().all(),
        "summary present on every row": final["app_location_count"].notna().all(),
        "incomplete flag matches unresolved locations": final["flag_app_highest_class_incomplete"].eq(any_unresolved).all(),
    }
    return pd.Series({k: bool(v) for k, v in checks.items()})


def prepare_output(final):
    out = final.rename(columns={"ApplicationDate_local": "ApplicationDate_ET", "IssueDate_local": "IssueDate_ET"})
    for c in ["ApplicationDate_ET", "IssueDate_ET"]:
        out[c] = out[c].dt.strftime("%Y-%m-%d %H:%M:%S")
    for c in ["functional_class_rank", "app_highest_functional_class_rank"]:
        out[c] = out[c].astype("Int64")
    missing = [c for c in OUTPUT_COLUMNS if c not in out.columns]
    if missing:
        raise KeyError(f"Output columns missing: {missing}")
    return out[OUTPUT_COLUMNS].sort_values(["TrackingNumber", "OBJECTID"]).reset_index(drop=True)


def write_output(df, path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    df.to_csv(tmp, index=False, encoding="utf-8-sig")
    try:
        tmp.replace(path)
    except PermissionError as err:
        raise PermissionError(f"Cannot write {path}; close it if it is open in Excel.") from err
    return path


# ----------------------------- Pipeline and command line -----------------------------
def run(pilot_apps_per_utility=None, refresh_source=False, offline=False, radius=LRS_SEARCH_RADIUS_M,
        tie_threshold=TIE_THRESHOLD_M, allow_unmapped_classes=False, output_csv=None, seed=2026):
    if not 0 <= radius <= 200:
        raise ValueError("radius must be between 0 and 200 meters")
    if refresh_source:
        if offline:
            raise ValueError("--refresh-source cannot be combined with --offline")
        download_permits()
    print(f"Loading {SAVED_PERMITS_JSON}", flush=True)
    raw, meta = load_permits(SAVED_PERMITS_JSON)
    print(f"Saved file where-clause: {meta.get('where')}", flush=True)
    dated = add_date_columns(raw)
    scoped, scope_counts = filter_scope(dated)
    base_checks = validate_base_records(scoped)
    print("\nBase checks:\n" + base_checks.to_string(), flush=True)
    hard = ["OBJECTID missing", "OBJECTID duplicated", "TrackingNumber blank", "TrackingNumber+LocationID duplicated"]
    failed = [k for k in hard if base_checks[k] != 0]
    if failed:
        raise RuntimeError(f"Base validation failed: {failed}")
    classified = add_lookup_coordinates(classify_utilities(scoped))
    locs = classified[classified["is_utility"]].copy()
    print("\nUtility locations:\n" + locs["utility_name"].value_counts().to_string(), flush=True)
    print(f"Utility applications: {locs['TrackingNumber'].nunique()}", flush=True)
    target = pilot_sample(locs, pilot_apps_per_utility, seed) if pilot_apps_per_utility else locs
    print(f"\nScope: {scope_counts}\nProcessing {len(target)} locations in "
          f"{target['TrackingNumber'].nunique()} applications", flush=True)
    lookups = enrich_locations(target, radius, offline)
    loc = build_location_table(target, lookups, tie_threshold)
    mapping, issues = validate_class_mapping(loc)
    print("\nFunctional classes found (selected roads):\n" + mapping.to_string(), flush=True)
    if issues:
        print("\nFunctional-class mapping issues:\n  " + "\n  ".join(issues), flush=True)
        if not allow_unmapped_classes:
            raise RuntimeError("Functional-class mapping issues found; review before writing output "
                               "(or rerun with --allow-unmapped-classes).")
    summary = summarize_applications(loc)
    final = attach_summary(loc, summary)
    checks = validate_final(final, target)
    print("\nFinal checks:\n" + checks.to_string(), flush=True)
    if not checks.all():
        raise RuntimeError(f"Final validation failed: {list(checks[~checks].index)}")
    output = prepare_output(final)
    out_path = write_output(output, output_csv or (PILOT_CSV if pilot_apps_per_utility else OUTPUT_CSV))
    apps = output.drop_duplicates("TrackingNumber")
    print("\nMatch status:\n" + output["match_status"].value_counts().to_string(), flush=True)
    print("\nLocation functional class:\n"
          + output["functional_class"].fillna("(none)").value_counts().to_string(), flush=True)
    print("\nHighest functional class per application:\n"
          + apps["app_highest_functional_class"].fillna("(none resolved)").value_counts().to_string(), flush=True)
    print("\nApplication status:\n" + apps["app_highest_class_status"].value_counts().to_string(), flush=True)
    print("\nFlag counts (rows):\n"
          + output[[c for c in output.columns if c.startswith("flag_")]].sum().to_string(), flush=True)
    print(f"\nWrote {len(output)} rows ({len(apps)} applications) to {out_path}", flush=True)
    return output


def parse_args(argv=None):
    p = argparse.ArgumentParser(description="Enrich utility occupancy-permit locations with FHWA functional class.")
    p.add_argument("--pilot", type=int, default=None, metavar="N", help="process N applications per utility")
    p.add_argument("--refresh-source", action="store_true", help="re-download permits before enriching")
    p.add_argument("--offline", action="store_true", help="use saved lookups only; fail if any are missing")
    p.add_argument("--radius", type=int, default=LRS_SEARCH_RADIUS_M, help="LRS search radius in meters (0-200)")
    p.add_argument("--tie-threshold", type=float, default=TIE_THRESHOLD_M, help="meters for close-candidate checks")
    p.add_argument("--allow-unmapped-classes", action="store_true", help="write output despite mapping issues")
    p.add_argument("--output", type=Path, default=None, help="output CSV path")
    return p.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    try:
        run(pilot_apps_per_utility=args.pilot, refresh_source=args.refresh_source, offline=args.offline,
            radius=args.radius, tie_threshold=args.tie_threshold,
            allow_unmapped_classes=args.allow_unmapped_classes, output_csv=args.output)
    except Exception as err:
        print(f"ERROR: {err}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())