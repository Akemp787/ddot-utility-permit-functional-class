# Utility Permits: Road Type at Every Work Location

## Summary

The Public Space Division asked which type of road each 2026 utility permit location sits on, because a road's
functional class determines when work is allowed to happen there. This tool answers that question for every permit
location held by DC Water, PEPCO, and Washington Gas, and also reports the most significant road type on each permit.

The results are in one spreadsheet, `output/utility_permit_locations_functional_class.csv`, with one row for every
permit location.

The code is also on GitHub: https://github.com/Akemp787/ddot-utility-permit-functional-class

## Key results

- I reviewed **4,926 permit locations** across **3,102 permit applications**: DC Water (1,887 locations), Washington
  Gas (1,666), and PEPCO (1,373).
- I identified the road type for **4,899 locations (99.5%)**. The other 27 are kept in the spreadsheet and marked for
  a manual check.
- **3,075 of the 3,102 permits** have a complete answer for their most significant road type.
- About half of the locations are on local streets. The rest are on busier roads, mostly arterials and collectors,
  where work hours are usually more restricted. Eight locations are on an Interstate or freeway.
- Every utility permit in scope is a Construction Staging Area permit. None of the Mobile Crane Work Zone permits
  belong to these three utilities.

| Road type (most to least significant) | What it means | Locations | Permits where this is the most significant road |
|---|---|---|---|
| 1. Interstate | Interstate highways | 7 | 7 |
| 2. Other freeway or expressway | Limited-access highways | 1 | 1 |
| 3. Principal arterial | Major city corridors | 492 | 310 |
| 4. Minor arterial | Busy connecting streets | 1,081 | 657 |
| 5. Major collector | Streets that feed traffic to arterials | 802 | 515 |
| 6. Minor collector | Lighter feeder streets | 107 | 64 |
| 7. Local | Neighborhood streets | 2,409 | 1,525 |
| Could not be determined | See "Things to keep in mind" | 27 | 23 |
| **Total** | | **4,926** | **3,102** |

## How I did it

1. **Collected the permits.** I downloaded the 2026 Construction Staging Area and Mobile Crane Work Zone permits from
   DDOT's public permit data. A permit counts if it was applied for or issued on or after January 1, 2026.
2. **Kept the three utilities.** I looked for DC Water, PEPCO, or Washington Gas by name in the owner, permittee, and
   applicant fields, including the variations used in the data (for example "DCWater/CIP" and "Wash Gas & Light
   Co."). I did not assume a permit belonged to a utility based on a contractor's name.
3. **Found the road at each location.** I sent each location's map coordinates to DDOT's road network service, which
   returns the nearby roads and their functional class.
4. **Picked the right road.** I ignored alleys, which do not have a functional class, and used the closest remaining
   road. I did not choose a bigger road simply because one was nearby, since that would make work rules stricter
   than they need to be.
5. **Found the most significant road on each permit** using the official ranking above, from Interstate down to
   local streets.
6. **Checked the results** before saving the spreadsheet (see "How I checked the results").

## How I checked the results

- **Against DDOT's own road records.** I compared every matched road with DDOT's Roadway SubBlock layer, which stores
  the official functional class for each block. All 2,438 matched road blocks agree.
- **Built-in checks on every run.** The tool confirms that no location was lost or counted twice, that no alley was
  ever selected, and that each permit's most significant road matches its locations. If any check fails, the run
  stops instead of saving a wrong answer.
- **Tests for the key rules.** Fourteen automated tests confirm the rules behave as intended, such as skipping alleys,
  ranking road types correctly, and not matching a utility on a contractor's name.
- **Repeatable results.** Anyone can rebuild the same spreadsheet from the files in this folder without an internet
  connection.

## How to read the spreadsheet

The two answers are near the start of each row:

| Column | What it tells you |
|---|---|
| `TrackingNumber` | The permit application. Rows with the same number belong to the same permit. |
| `WorkLocationFullAddress` | Where the work is |
| `Utility` | DC Water, PEPCO, or Washington Gas |
| `FunctionalClass` | **The road type at this location** |
| `PermitHighestFunctionalClass` | **The most significant road type on the whole permit** |
| `PermitHighestClassComplete` | "Yes", or "No" if part of the permit could not be checked |
| `MatchStatus` | "Matched" means the road type was found. Any other value explains why it was not. |
| `ReviewNote` | A short note on rows that deserve a second look |
| `MatchedRoad`, `RoadDistanceM` | The road that was matched and its distance from the permit point, in meters |

To see the rows that need attention, filter `MatchStatus` to anything other than "Matched", or filter `ReviewNote`
for "Close call".

## Things to keep in mind

- **27 locations could not be assigned a road type.** I kept them in the spreadsheet rather than dropping them:
  - 14 are next to a road that DDOT's data lists as unclassified (spelled "Uncassified" in the source).
  - 9 had only alleys nearby.
  - 4 share one map point where DDOT's road service did not respond, even after several tries.
- **4 permits are only partly answered.** For those, the most significant road type could be higher than shown, so
  they are marked "No" in `PermitHighestClassComplete`. Another 23 permits had no location that could be checked.
- **126 locations are close calls.** A road of a different type was almost as close, within about 5 meters
  (16 feet). I used the closest road and named the other one in `ReviewNote` so staff can make the final call.
- **Utilities are found by name.** A permit filed only under a contractor's name, with no utility named anywhere,
  would not be included.
- **Each location is a single map point.** A staging area that runs along several blocks is represented by that one
  point.
- The results reflect DDOT's data on October 13-14, 2026.

---

## Technical notes (for reviewers)

### Setup (Windows 11)

1. Install Python 3.10 or later. I tested on Windows 11 with Python 3.11 (pandas 2.0) and Python 3.12 (pandas 3.0),
   and both produce the same spreadsheet.
2. Open a terminal in this folder and install the packages:

   ```
   pip install -r requirements.txt
   ```

### Running it

| Command | What it does |
|---|---|
| `python permit_functional_class.py --offline` | Rebuilds the spreadsheet from the included data and saved road lookups, in seconds, with no internet needed |
| `python permit_functional_class.py` | Normal run. Looks up any locations that are not already saved. |
| `python permit_functional_class.py --sample 5` | Quick test on 5 permits per utility |
| `python permit_functional_class.py --refresh-source` | Downloads fresh permit data first |
| `python -m unittest -v` | Runs the 14 tests |
| `python check_against_subblock_layer.py` | Repeats the check against DDOT's Roadway SubBlock layer |

Settings such as file locations, the date rule, the 50 m search distance, and the road ranking are listed at the top
of `permit_functional_class.py` with comments. If a run is interrupted, running it again continues where it stopped.

### Folder contents

| File | Purpose |
|---|---|
| `permit_functional_class.py` | The tool |
| `check_against_subblock_layer.py` | Independent check against DDOT's Roadway SubBlock layer |
| `tests/` | Automated tests for the key rules |
| `requirements.txt` | Python packages needed |
| `data/occupancy_permits_2026_target_types.json` | The permit data used for this run |
| `cache/lrs_lookups.jsonl` | Saved road lookups, one per map point |
| `output/utility_permit_locations_functional_class.csv` | The results spreadsheet |

### Decisions and details

- **Date rule.** A permit location is included if its application date or issue date is on or after January 1,
  2026, in Eastern time. I did not filter by permit status, since the request did not ask for it.
- **Data download.** The permit service returns at most 2,000 records per request, so the tool downloads the records
  in batches and confirms the total matches the server's count (7,194 records).
- **Road lookup.** The tool uses DDOT's Linear Referencing System (`getPointOnSubBlock`) with a 50 m search distance.
  Each map point is looked up once (2,591 unique points for 4,926 locations), a few at a time to avoid overloading
  the public service.
- **Which enrichment approach.** The assessment offered two approaches. I used the road network service because every
  permit location already has coordinates. Looking up addresses first (the second approach) would add another
  matching step and more room for error. I used the second approach's Roadway SubBlock layer to check the results
  instead.
- **Ranking.** The ranking follows the official FHWA functional class order, 1 (Interstate) to 7 (Local), which is
  the same coding DDOT uses in its Roadway SubBlock layer. I did not rank by the road service's numeric codes,
  because those run backwards (Interstate is 11 and Local is 19). If the service ever returns a road type the tool
  does not recognize, the run stops so nothing is ranked by guesswork.
- **If this became a regular report,** I would schedule it, save the results to a database table, and review the
  close calls and unresolved locations with permitting staff to fine-tune the 50 m and 5 m settings.

### Use of AI

I used AI tools to help write the code and this document. I ran the analysis, reviewed every output, and checked the
results myself.
