"""
Independent check of the results: compare each matched road's functional class rank with DDOT's Roadway SubBlock
layer (approach 2's data source), which stores the HPMS functional class as codes 1-7 (1 = Interstate ... 7 = Local).

Run after permit_functional_class.py:  python check_against_subblock_layer.py
"""
import pandas as pd

from permit_functional_class import OUTPUT_CSV, get_json

SUBBLOCK_QUERY = ("https://maps2.dcgis.dc.gov/dcgis/rest/services/DCGIS_DATA/"
                  "Transportation_WebMercator/MapServer/162/query")


def main():
    results = pd.read_csv(OUTPUT_CSV, dtype={"SubBlockKey": str})
    roads = results.dropna(subset=["SubBlockKey", "FunctionalClassRank"]).drop_duplicates("SubBlockKey")
    keys = roads["SubBlockKey"].tolist()
    layer_codes = {}
    for start in range(0, len(keys), 200):
        where = "SUBBLOCKKEY IN ({})".format(",".join(f"'{k}'" for k in keys[start:start + 200]))
        data = get_json(SUBBLOCK_QUERY, {"where": where, "outFields": "SUBBLOCKKEY,FHWAFUNCTIONALCLASS",
                                         "returnGeometry": "false", "f": "json"}, "POST")
        layer_codes.update({f["attributes"]["SUBBLOCKKEY"]: f["attributes"]["FHWAFUNCTIONALCLASS"]
                            for f in data["features"]})

    roads = roads.assign(SubBlockLayerCode=roads["SubBlockKey"].map(layer_codes)).dropna(subset=["SubBlockLayerCode"])
    agrees = roads["SubBlockLayerCode"].astype(int).eq(roads["FunctionalClassRank"].astype(int))
    print(f"Road blocks checked: {len(keys)} | found in SubBlock layer: {len(roads)} | "
          f"same class: {agrees.sum()} ({agrees.mean():.1%})")
    if not agrees.all():
        print(roads.loc[~agrees, ["SubBlockKey", "MatchedRoad", "FunctionalClass", "FunctionalClassRank",
                                  "SubBlockLayerCode"]].to_string(index=False))


if __name__ == "__main__":
    main()
