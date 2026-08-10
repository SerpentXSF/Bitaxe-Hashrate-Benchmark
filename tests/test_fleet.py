"""Unit tests for fleet.py's hardware-free helpers."""
import os
import sys
import types
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import fleet


class TestEfficiency(unittest.TestCase):
    def test_jth(self):
        self.assertAlmostEqual(fleet.efficiency_jth({"hashRate": 1200, "power": 18}), 15.0)

    def test_zero_hashrate_is_none(self):
        self.assertIsNone(fleet.efficiency_jth({"hashRate": 0, "power": 18}))

    def test_missing_fields_is_none(self):
        self.assertIsNone(fleet.efficiency_jth({}))


class TestStatusRow(unittest.TestCase):
    def test_unreachable(self):
        self.assertIn("UNREACHABLE", fleet.status_row("10.0.0.1", None))

    def test_reachable_includes_key_values(self):
        info = {"hostname": "Gamma", "frequency": 600, "coreVoltage": 1140,
                "hashRate": 1228, "errorPercentage": 4.7, "temp": 71.2, "vrTemp": 81,
                "fanspeed": 100, "fanrpm": 8194, "power": 23, "uptimeSeconds": 3600}
        row = fleet.status_row("10.0.0.2", info)
        self.assertIn("Gamma", row)
        self.assertIn("600/1140", row)
        self.assertIn("100%/8194", row)
        self.assertIn("[!err !temp !cooling]", row)   # erroring, hot, and fan pegged

    def test_missing_fields_show_na_not_zero(self):
        # An unreadable errorPercentage must not render as a healthy 0.0%.
        info = {"hostname": "X", "frequency": 500, "coreVoltage": 1150,
                "hashRate": 1000, "temp": 50, "power": 18, "uptimeSeconds": 600}
        row = fleet.status_row("10.0.0.3", info)
        self.assertIn("n/a", row)          # missing err/vr/fan -> n/a
        self.assertNotIn("[!", row)        # nothing flagged


class TestRowFlags(unittest.TestCase):
    def test_healthy_no_flags(self):
        self.assertEqual(fleet.row_flags({"errorPercentage": 2.0, "temp": 50, "fanspeed": 60}), [])

    def test_pegged_fan_but_cool_is_not_flagged(self):
        # Many Gammas run the fan at 100% always; cool + pegged is normal.
        self.assertEqual(fleet.row_flags({"errorPercentage": 2.0, "temp": 46, "fanspeed": 100}), [])

    def test_flags_error_hot_and_cooling(self):
        self.assertEqual(
            fleet.row_flags({"errorPercentage": 9.0, "temp": 70, "fanspeed": 100}),
            ["err", "temp", "cooling"])


class TestLoadTargets(unittest.TestCase):
    def test_positional_dedupe(self):
        args = types.SimpleNamespace(ips=["a", "b", "a", "c"], file=None)
        self.assertEqual(fleet.load_targets(args), [("a", []), ("b", []), ("c", [])])


class TestDelta(unittest.TestCase):
    def test_before_after(self):
        before = {"errorPercentage": 6.9, "frequency": 525, "coreVoltage": 1100}
        after = {"errorPercentage": 1.5, "frequency": 500, "coreVoltage": 1120}
        d = fleet._delta(before, after)
        self.assertIn("6.9 -> 1.5", d)
        self.assertIn("525/1100 -> 500/1120", d)


if __name__ == "__main__":
    unittest.main(verbosity=2)
