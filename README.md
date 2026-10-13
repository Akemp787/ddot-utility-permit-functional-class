# Utility Permit Functional Class Enrichment

Python script that enriches 2026 DDOT Public Space Occupancy Permit locations for **DC Water, PEPCO, and Washington Gas**
(permit types *Construction Staging Area* and *Mobile Crane Work Zone*) with the **FHWA functional class** of the roadway
at each permit location, and the **highest functional class per permit application**.

The output CSV has **one row per permit application location**.

Source code is also available on GitHub: https://github.com/Akemp787/ddot-utility-permit-functional-class

## Folder contents

| Path | Purpose |
|---|---|
| `permit_functional_class.py` | The enrichment script. Settings are in the CONFIGURATION section at the top. |
| `requirements.txt` | Required Python packages |
| `data/occupancy_permits_2026_target_types.json` | Saved permit extract used by default |
| `checkpoints/lrs_lookups_*.jsonl` | Saved roadway lookups, so the script can resume or rebuild offline |
| `output/utility_permit_locations_functional_class.csv` | Final enriched CSV |

## Setup (Windows 11)

1. Install Python 3.10 or later. Tested on Windows 11 with Python 3.11 (pandas 2.0) and Python 3.12 (pandas 3.0);
   both produce an identical CSV.
2. Open a terminal (Anaconda Prompt, Command Prompt, or PowerShell) in this folder.
3. Install the packages:

   ```
   pip install -r requirements.txt
   ```

All paths are built relative to the script's own folder with `pathlib`, so the folder can be placed anywhere.

## How to run

| Command | What it does |
|---|---|
| `python permit_functional_class.py --offline` | Rebuilds the CSV from the saved data and saved lookups. No web requests; takes seconds. |
| `python permit_functional_class.py` | Full run. Looks up any locations not already in the checkpoint file. |
| `python permit_functional_class.py --pilot 5` | Small test: 5 applications per utility, written to `output/pilot_...csv`. |
| `python permit_functional_class.py --refresh-source` | Re-downloads the permit data first (the existing file is backed up). |

Optional settings: `--radius` (search radius in meters, 0-200, default 50), `--tie-threshold` (meters, default 5),
`--output` (CSV path), `--allow-unmapped-classes`. Other settings (paths, URLs, date scope, utility aliases,
functional-class hierarchy, number of parallel requests) are in the CONFIGURATION section of the script.

Runs are resumable: if a run is interrupted, run the same command again and it continues where it stopped.
Delete the `checkpoints` folder to force fresh roadway lookups.

## Method

1. **Source data.** DDOT TOPS Occupancy Permits REST layer
   (`https://maps2.dcgis.dc.gov/dcgis/rest/services/DCGIS_APPS/DDOT_TOPS/MapServer/24`).
   The service returns a limited number of records per request, so the script retrieves all matching record IDs and
   downloads them in batches of 500, checking that every batch and the total count are complete.

2. **Date filter.** The assessment asks for permit applications or issued permits on or after January 1, 2026.
   A location is included if its **ApplicationDate OR IssueDate** is on or after 2026-01-01 (US Eastern time).
   ArcGIS stores dates in UTC; they are converted to Eastern time. Records whose inclusion would change under UTC are
   flagged, and any dates after 2026 are kept and flagged. No permit-status filter is applied.

3. **Permit types.** Only *Construction Staging Area* and *Mobile Crane Work Zone*.

4. **Utility identification.** `OwnerName`, `PermitteeName`, and `ApplicantCompany` are normalized (case, punctuation,
   spacing) and matched to explicit utility names and observed aliases, for example DCWater/CIP, DCWater/OPS,
   Pepco Holdings Inc, Potomac Electric Power Company, Wash Gas & Light Co. Utility affiliation is **not** inferred
   from a contractor name or from the word "Washington" alone. The matching fields are kept as evidence, and
   locations matching more than one utility are flagged.

5. **Roadway lookup.** Each location's longitude/latitude is sent to the DDOT Linear Referencing System
   `getPointOnSubBlock` service (search radius 50 m, all route types). Locations that share identical coordinates are
   looked up once, and lookups run a few at a time in parallel. **Alleys are excluded** because they have no
   functional class. The **nearest remaining road** (by absolute offset in meters) is selected. The script does not
   pick the highest-class road nearby, and if the nearest road has no class it is kept and flagged rather than
   replaced by a farther road. Other roads within 5 m of the nearest with a different name or class are flagged as
   ambiguous. The selected road name is also compared with the permit address for review.

   Of the two enrichment approaches offered in the assessment, this script uses approach 1 (the LRS nearest-roadway
   query). Every permit location already has coordinates, so querying the road network at that point is more direct
   than approach 2 (geocoding the address through the Master Address Repository to get a SubBlockKey, then looking up
   the Roadway SubBlock layer), which would add an address-matching step and its own errors. The `road_subblock_key`
   column is kept in the output, so the result can be cross-checked against the Roadway SubBlock layer if needed.

6. **Functional-class hierarchy.** "Highest" uses the explicit FHWA functional system order, **not** the largest
   numeric code (in this data, a larger code means a *lower* class):

   | Rank | FHWA functional class |
   |---|---|
   | 1 | Interstate |
   | 2 | Other Freeways and Expressways |
   | 3 | Principal Arterial (Other Principal Arterial) |
   | 4 | Minor Arterial |
   | 5 | Major Collector |
   | 6 | Minor Collector |
   | 7 | Local |

   Classes returned by the service in the full run: Interstate (code 11), Other Freeway and Expressway (12),
   Principal Arterial (14), Minor Arterial (16), Major Collector (17), Minor Collector (18), Local (19). Each name has
   exactly one code. The script stops if the service returns a class name not in this list, so no class is ranked by
   guesswork.

   The service also returned the value "Uncassified" (its own spelling of "Unclassified", with no code) for 14
   locations. That is not an FHWA functional class, so it is kept as text, not ranked, and flagged as a missing class
   (`match_status = matched_unclassified`). A farther road is not substituted.

7. **Application summary.** For each permit application (`TrackingNumber`), the highest class is the best-ranked class
   among its locations. If any location could not be resolved, the application is flagged
   `flag_app_highest_class_incomplete`, because that location might have had a higher class. The application summary
   is repeated on every location row.

## Key output columns

| Column | Meaning |
|---|---|
| `TrackingNumber` | Permit application ID (groups an application's locations) |
| `OBJECTID`, `LocationID` | Source location IDs (one row per location) |
| `WorkLocationFullAddress`, `Longitude`, `Latitude` | Permit location |
| `utility_name`, `utility_evidence` | Matched utility and which name field matched |
| `functional_class`, `functional_class_code` | **Functional class at this location** |
| `functional_class_rank` | 1 = highest (Interstate) to 7 = Local |
| `road_route_name`, `road_offset_m` | Selected road and distance to it (meters) |
| `match_status` | matched / matched_unclassified / matched_missing_class / matched_unmapped_class / no_candidates / only_excluded_route_types / lookup_failed / no_coordinates |
| `app_highest_functional_class` | **Highest functional class for the permit application** |
| `app_highest_class_status` | complete / incomplete (unresolved locations) / no resolved locations |
| `flag_*` columns | Review flags: missing class, lookup failure, ambiguous match, incomplete application, etc. |

## Results of this run

**Scope.** 7,194 in-scope permit locations were downloaded (all records matching the date and permit-type rules;
no records had dates after 2026 and none changed scope between UTC and Eastern time). Of these, **4,926 locations in
3,102 permit applications** belong to the three utilities:

| Utility | Locations |
|---|---|
| DC Water | 1,887 |
| Washington Gas | 1,666 |
| PEPCO | 1,373 |

No location matched more than one utility. All utility locations are *Construction Staging Area* permits; none of the
*Mobile Crane Work Zone* permits belong to these utilities.

**Location functional class** (4,926 rows):

| Functional class | Locations |
|---|---|
| Interstate | 7 |
| Other Freeway and Expressway | 1 |
| Principal Arterial | 492 |
| Minor Arterial | 1,081 |
| Major Collector | 802 |
| Minor Collector | 107 |
| Local | 2,409 |
| Unclassified ("Uncassified" in source), flagged | 14 |
| No class: only alleys within 50 m, flagged | 9 |
| No class: lookup failed, flagged | 4 |

4,899 of 4,926 locations (99.5%) received a ranked functional class. The 4 failed lookups share one coordinate
where the service returned errors on repeated attempts; rerunning the script retries them.

**Highest functional class per application** (3,102 applications):

| Highest class | Applications |
|---|---|
| Interstate | 7 |
| Other Freeway and Expressway | 1 |
| Principal Arterial | 310 |
| Minor Arterial | 657 |
| Major Collector | 515 |
| Minor Collector | 64 |
| Local | 1,525 |
| None resolved | 23 |

3,075 applications are complete, 4 are incomplete (at least one location unresolved, so the highest class may be
understated), and 23 have no resolved location.

**Review flags.** 164 locations (3.3%) have another road within 5 m with a different name; 126 of those also have a
different class. They are flagged with the alternatives listed in `ambiguity_detail`.

## Validation performed by the script

- No missing or duplicate `OBJECTID`; `TrackingNumber` + `LocationID` is unique.
- Every filtered utility location is kept in the output (row counts and IDs checked after each join).
- No alley is ever selected as the matched road.
- Every functional class returned maps to the hierarchy.
- Each application's highest class equals the best rank among its locations.

## Assumptions and limitations

- Utility matching relies on explicit names in three fields; permits filed only under a contractor's name are not
  captured.
- The 50 m search radius and 5 m tie threshold are configurable assumptions.
- Each permit location is a single point; work areas spanning several blocks are represented by that point.
- Results reflect the DC GIS services at the time of the run.

## AI assistance

AI tools were used to help draft the code and documentation. All runs, outputs, and validation were performed and
reviewed by the author.
