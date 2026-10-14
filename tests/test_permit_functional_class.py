"""Unit tests for the matching and ranking rules. Run from the Python folder: python -m unittest -v"""
import sys
import unittest
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import permit_functional_class as pfc  # noqa: E402


def road(name, route_type, distance, fclass):
    return {"routeName": name, "routeType": route_type, "subBlockKey": name,
            "offsetInMeters": distance, "functionalClass": fclass}


class FunctionalClassRankTests(unittest.TestCase):
    def test_hierarchy_order_not_code_order(self):
        self.assertLess(pfc.class_rank("Interstate"), pfc.class_rank("Principal Arterial"))
        self.assertLess(pfc.class_rank("Principal Arterial"), pfc.class_rank("Local"))

    def test_names_are_matched_case_insensitively(self):
        self.assertEqual(pfc.class_rank("other freeway and expressway"), 2)

    def test_unknown_or_missing_class_has_no_rank(self):
        self.assertIsNone(pfc.class_rank("Something New"))
        self.assertIsNone(pfc.class_rank(None))


class ChooseRoadTests(unittest.TestCase):
    def test_skips_alley_and_takes_nearest_road(self):
        result = pfc.choose_road([road("A ALY", "Alley", 1, None),
                                  road("B ST", "Street", -8, "Local"),
                                  road("C AVE", "Street", 20, "Minor Arterial")])
        self.assertEqual(result["MatchedRoad"], "B ST")
        self.assertEqual(result["FunctionalClass"], "Local")
        self.assertEqual(result["MatchStatus"], "Matched")

    def test_keeps_nearest_road_without_class_instead_of_farther_road(self):
        result = pfc.choose_road([road("NEAR", "Street", 3, None), road("FAR", "Street", 30, "Minor Arterial")])
        self.assertEqual(result["MatchedRoad"], "NEAR")
        self.assertEqual(result["MatchStatus"], "Nearest road has no functional class")

    def test_source_spelling_uncassified_means_no_class(self):
        result = pfc.choose_road([road("X", "Street", 2, "Uncassified")])
        self.assertEqual(result["MatchStatus"], "Nearest road has no functional class")

    def test_flags_close_call_with_different_class(self):
        result = pfc.choose_road([road("A", "Street", 2, "Local"), road("B", "Street", 5, "Minor Arterial")])
        self.assertIn("Close call: B", result["ReviewNote"])

    def test_no_close_call_when_other_road_is_far_or_same_class(self):
        result = pfc.choose_road([road("A", "Street", 2, "Local"), road("B", "Street", 4, "Local"),
                                  road("C", "Street", 30, "Minor Arterial")])
        self.assertNotIn("ReviewNote", result)

    def test_only_alleys_failed_lookup_and_no_roads(self):
        self.assertIn("Only alleys", pfc.choose_road([road("A ALY", "Alley", 1, None)])["MatchStatus"])
        self.assertEqual(pfc.choose_road(None)["MatchStatus"], "Lookup failed")
        self.assertIn("No road", pfc.choose_road([])["MatchStatus"])


class UtilityMatchTests(unittest.TestCase):
    def row(self, owner="", permittee="", applicant=""):
        return {"OwnerName": owner, "PermitteeName": permittee, "ApplicantCompany": applicant}

    def test_observed_aliases(self):
        self.assertEqual(pfc.match_utility(self.row(owner="DCWater/CIP"))[0], "DC Water")
        self.assertEqual(pfc.match_utility(self.row(permittee="Pepco Holdings Inc"))[0], "PEPCO")
        self.assertEqual(pfc.match_utility(self.row(applicant="Potomac Electric Power Company"))[0], "PEPCO")
        self.assertEqual(pfc.match_utility(self.row(owner="Wash Gas & Light Co."))[0], "Washington Gas")

    def test_contractor_name_or_washington_alone_does_not_match(self):
        self.assertEqual(pfc.match_utility(self.row(owner="Washington Paving Co", applicant="Fort Myer Construction"))[0], "")
        self.assertEqual(pfc.match_utility(self.row(owner=None))[0], "")

    def test_reports_where_the_name_was_found_and_multiple_utilities(self):
        self.assertEqual(pfc.match_utility(self.row(owner="PEPCO", permittee="PEPCO")), ("PEPCO", "OwnerName, PermitteeName"))
        self.assertEqual(pfc.match_utility(self.row(owner="DC Water", permittee="PEPCO"))[0], "Multiple")


class PermitSummaryTests(unittest.TestCase):
    def test_highest_class_uses_best_rank_and_flags_incomplete_permits(self):
        locs = pd.DataFrame({"TrackingNumber": [1, 1, 2, 2], "OBJECTID": [10, 11, 20, 21],
                             "Longitude": [-77.0, -77.1, -77.2, -77.3], "Latitude": [38.9] * 4})
        roads = {(-77.0, 38.9): [road("A", "Street", 1, "Local")],
                 (-77.1, 38.9): [road("B", "Street", 1, "Minor Arterial")],
                 (-77.2, 38.9): [road("C", "Street", 1, "Local")],
                 (-77.3, 38.9): None}
        original = pfc.lookup_roads
        pfc.lookup_roads = lambda points, offline=False: roads
        try:
            out = pfc.enrich(locs).set_index("OBJECTID")
        finally:
            pfc.lookup_roads = original
        self.assertEqual(out.loc[10, "PermitHighestFunctionalClass"], "Minor Arterial")
        self.assertEqual(out.loc[10, "PermitHighestClassComplete"], "Yes")
        self.assertEqual(out.loc[20, "PermitHighestFunctionalClass"], "Local")
        self.assertEqual(out.loc[20, "PermitHighestClassComplete"], "No")

    def test_unknown_class_name_stops_the_run(self):
        locs = pd.DataFrame({"TrackingNumber": [1], "OBJECTID": [10], "Longitude": [-77.0], "Latitude": [38.9]})
        original = pfc.lookup_roads
        pfc.lookup_roads = lambda points, offline=False: {(-77.0, 38.9): [road("A", "Street", 1, "Brand New Class")]}
        try:
            with self.assertRaises(ValueError):
                pfc.enrich(locs)
        finally:
            pfc.lookup_roads = original


if __name__ == "__main__":
    unittest.main()
