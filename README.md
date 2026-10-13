# Utility Permits: Road Type at Every Work Location

**What this is:** a tool that tells the Public Space Division what kind of road each 2026 utility work location sits on,
and the most significant road type for each permit, so the right time-of-day work rules can be applied.

**Who it covers:** DC Water, PEPCO, and Washington Gas permits for *Construction Staging Area* and *Mobile Crane Work
Zone* work, applied for or issued on or after January 1, 2026.

**The result:** one spreadsheet (CSV), with one row for every permit work location.

Source code is also available on GitHub: https://github.com/Akemp787/ddot-utility-permit-functional-class

---

## At a glance

| | |
|---|---|
| Utility work locations reviewed | **4,926** |
| Permit applications those locations belong to | **3,102** |
| Locations where the road type was identified | **4,899 (99.5%)** |
| Locations that need a manual look | **27** (flagged in the file) |
| Applications with a complete answer | **3,075 of 3,102** |

| Utility | Work locations |
|---|---|
| DC Water | 1,887 |
| Washington Gas | 1,666 |
| PEPCO | 1,373 |

All of these utility permits are *Construction Staging Area* permits. None of the *Mobile Crane Work Zone* permits
belong to these three utilities.

---

## Why road type matters

Every road in DC has a federal **functional class**, a label for how important the road is to traffic, from
Interstates down to local neighborhood streets. The road's class decides **what time of day work is allowed**, and
busier roads typically have tighter work windows. Many permits cover several locations on different roads, so each
permit needs both answers:

1. **The road type at each location**
2. **The most significant road type on the whole permit**, which points to the most restrictive rule for that permit

---

## What the tool does, in plain terms

1. **Gets the permits.** Downloads all 2026 permits of the two requested types from DDOT's public permit data.
2. **Keeps only the three utilities.** Looks for DC Water, PEPCO, or Washington Gas by name in the owner, permittee, and
   applicant fields. It does not guess from contractor names.
3. **Finds the road at each location.** Sends each location's map coordinates to DDOT's road network service, which
   returns the roads nearby.
4. **Picks the right road.** Ignores alleys, which have no road class, and picks the **closest** remaining road. It
   does not pick a bigger road just because one is nearby.
5. **Finds the most significant road per permit,** using the official federal ranking below.
6. **Checks its own work** (nothing lost, nothing duplicated, every answer traceable), then saves the spreadsheet.

---

## Road types, from most to least significant

| Rank | Road type | Plain meaning | Locations | Permits (as their most significant road) |
|---|---|---|---|---|
| 1 | Interstate | Highways such as I-295 and I-395 | 7 | 7 |
| 2 | Other Freeway and Expressway | Limited-access highways | 1 | 1 |
| 3 | Principal Arterial | Major city corridors | 492 | 310 |
| 4 | Minor Arterial | Busy connecting streets | 1,081 | 657 |
| 5 | Major Collector | Streets that feed traffic to arterials | 802 | 515 |
| 6 | Minor Collector | Lighter feeder streets | 107 | 64 |
| 7 | Local | Neighborhood streets | 2,409 | 1,525 |
| | *Could not be determined (flagged)* | See "Things to keep in mind" | 27 | 23 |
| | **Total** | | **4,926** | **3,102** |

**Key takeaway:** about half of utility work locations are on local streets, and the other half are on busier roads
(arterials and collectors) where work hours are typically more restricted. A small number (8 locations) are on highways.

---

## How to read the spreadsheet

The file is `output/utility_permit_locations_functional_class.csv` and opens in Excel. The most useful columns:

| Column | What it tells you |
|---|---|
| `TrackingNumber` | The permit application. Rows with the same number belong to the same permit. |
| `WorkLocationFullAddress` | Where the work is |
| `utility_name` | DC Water, PEPCO, or Washington Gas |
| `functional_class` | **Road type at this location** |
| `road_route_name` | The name of the road that was matched |
| `app_highest_functional_class` | **Most significant road type on the whole permit** |
| `app_highest_class_status` | "complete", or a warning that some locations on the permit could not be resolved |
| `match_status` | "matched" means the road type was found. Anything else explains why not. |
| Columns starting with `flag_` | TRUE marks a row worth a second look |

**Tip:** filter `flag_location_unresolved` to TRUE to see the 27 locations that need a manual check, or filter
`flag_ambiguous_match` to TRUE to see the close calls between two roads.

---

## Things to keep in mind

- **27 locations could not be assigned a road type.** They are kept in the file and clearly flagged, not dropped:
  - 14: DDOT's road data lists the nearest road as "Unclassified" (spelled "Uncassified" in the source)
  - 9: only alleys were found nearby, and alleys have no road type
  - 4: DDOT's service did not respond for one location point, even after repeated tries
- **23 permits have no road type at all,** and **4 permits are only partly resolved.** For those 4, the true "most
  significant road" could be higher than shown, and they are marked as incomplete.
- **164 locations (3.3%) are close calls between two roads:** a second road was almost as near, within about
  5 meters (16 feet) of the closest one. The closest road is used, and the other road is listed so staff can decide.
- **Utilities are identified by name.** A permit filed only under a contractor's name, with no utility named, would
  not be included.
- **Each location is a single map point.** A staging area that stretches along several blocks is represented by
  that one point.
- Results reflect DDOT's data at the time the tool was run.

---

## For technical reviewers: how to run it

### Setup (Windows 11)

1. Install Python 3.10 or later. Tested on Windows 11 with Python 3.11 (pandas 2.0) and Python 3.12 (pandas 3.0);
   both produce an identical CSV.
2. Open a terminal (Command Prompt, PowerShell, or Anaconda Prompt) in this folder.
3. Install the packages:

   ```
   pip install -r requirements.txt
   ```

All paths are built relative to the script's folder with `pathlib`, so the folder can be placed anywhere.

### Run

| Command | What it does |
|---|---|
| `python permit_functional_class.py --offline` | **Fastest.** Rebuilds the spreadsheet from the included data and saved road lookups. No internet needed; takes seconds. |
| `python permit_functional_class.py` | Full run. Looks up any locations not already saved (requires internet). |
| `python permit_functional_class.py --pilot 5` | Small test: 5 permits per utility, saved as `output/pilot_...csv`. |
| `python permit_functional_class.py --refresh-source` | Downloads fresh permit data first (the old file is backed up). |

Optional settings: `--radius` (search distance in meters, 0-200, default 50), `--tie-threshold` (meters, default 5),
`--output` (CSV path), `--allow-unmapped-classes`. All other settings (paths, web addresses, date rule, utility
names, road ranking, number of parallel requests) are in the CONFIGURATION section at the top of the script, with
comments. If a run is interrupted, run the same command again and it continues where it stopped.

### Folder contents

| Path | Purpose |
|---|---|
| `permit_functional_class.py` | The tool. Settings are in the CONFIGURATION section at the top. |
| `requirements.txt` | Required Python packages |
| `data/occupancy_permits_2026_target_types.json` | Saved permit data used by default |
| `checkpoints/lrs_lookups_*.jsonl` | Saved road lookups, so runs can resume or rebuild offline |
| `output/utility_permit_locations_functional_class.csv` | The final spreadsheet |

### Technical details

- **Data source:** DDOT TOPS Occupancy Permits REST layer
  (`https://maps2.dcgis.dc.gov/dcgis/rest/services/DCGIS_APPS/DDOT_TOPS/MapServer/24`). The service caps records per
  request, so the tool fetches all matching record IDs and downloads them in batches of 500, verifying each batch and
  the total (7,194 records).
- **Date rule:** a location is included if its `ApplicationDate` **or** `IssueDate` is on or after 2026-01-01, in US
  Eastern time (source dates are UTC). No records had dates after 2026, and none changed between UTC and Eastern time.
  No permit-status filter is applied.
- **Utility matching:** `OwnerName`, `PermitteeName`, and `ApplicantCompany` are normalized (case, punctuation,
  spacing) and matched to explicit names and observed aliases (for example DCWater/CIP, Pepco Holdings Inc,
  Potomac Electric Power Company, Wash Gas & Light Co.). Evidence is kept in `utility_evidence`; multiple-utility
  matches would be flagged (there were none).
- **Road lookup:** DDOT Linear Referencing System `getPointOnSubBlock`, 50 m search radius, all route types. Alleys are
  excluded; the nearest remaining road by absolute offset is selected. A nearest road without a class is kept and
  flagged, never replaced by a farther road. Other roads within 5 m with a different name or class are flagged and
  listed in `ambiguity_detail`. Identical coordinates are looked up once; lookups run 6 at a time with timeouts,
  retries, and a resumable checkpoint.
- **Approach choice:** of the two enrichment approaches offered, this uses approach 1 (the road network query at each
  location's coordinates). Approach 2 (address lookup in the Master Address Repository, then the Roadway SubBlock
  layer) would add an address-matching step and its own errors. `road_subblock_key` is kept so results can be
  cross-checked against the Roadway SubBlock layer.
- **Ranking:** "most significant" uses the explicit FHWA functional class order above, **not** the numeric code. In
  this data a larger code means a *lower* class (Interstate 11, Other Freeway and Expressway 12, Principal Arterial 14,
  Minor Arterial 16, Major Collector 17, Minor Collector 18, Local 19), so taking the largest code would be backwards.
  The tool stops if it meets a class name it does not recognize, so nothing is ranked by guesswork. "Uncassified" is
  treated as no class: kept as text, not ranked, and flagged (`match_status = matched_unclassified`).
- **Permit summary:** the most significant class across a permit's resolved locations, repeated on every row of that
  permit. If any location is unresolved, `flag_app_highest_class_incomplete` is set.
- **Built-in checks:** no missing or duplicate record IDs; `TrackingNumber` + `LocationID` unique; every location kept
  after each join; no alley ever selected; every class mapped to a rank; each permit's highest class equals the best
  rank among its locations. The run stops if any check fails.

### AI assistance

AI tools were used to help draft the code and documentation. All runs, outputs, and validation were performed and
reviewed by the author.
