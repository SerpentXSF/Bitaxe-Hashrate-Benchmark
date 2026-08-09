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

    def test_missing_fan_shows_na(self):
        info = {"hostname": "X", "frequency": 500, "coreVoltage": 1150,
                "hashRate": 1000, "errorPercentage": 2.0, "temp": 50, "vrTemp": 55,
                "power": 18, "uptimeSeconds": 600}
        self.assertIn("n/a", fleet.status_row("10.0.0.3", info))


class TestLoadIps(unittest.TestCase):
    def test_dedupes_preserving_order(self):
        args = types.SimpleNamespace(ips=["a", "b", "a", "c"], file=None)
        self.assertEqual(fleet.load_ips(args), ["a", "b", "c"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
