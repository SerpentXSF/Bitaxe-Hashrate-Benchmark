"""Unit tests for the error-aware benchmark's pure decision logic.

These exercise the functions that do no hardware I/O, so they run anywhere:
    python -m pytest tests/            (or)     python tests/test_benchmark_logic.py
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bitaxe_hashrate_benchmark as b


def make_info(temp=55, hr=1200, power=18, vr=60, ep=2.0, ec=1000, voltage=5000):
    return {
        "temp": temp, "vrTemp": vr, "voltage": voltage,
        "hashRate": hr, "power": power, "errorPercentage": ep,
        "hashrateMonitor": {"asics": [{"errorCount": ec}]},
    }


def sample(ec, ep):
    return {"error_count": ec, "error_percentage": ep}


class TestSumErrorCount(unittest.TestCase):
    def test_single_asic(self):
        info = {"hashrateMonitor": {"asics": [{"errorCount": 1234}]}}
        self.assertEqual(b.sum_error_count(info), 1234)

    def test_multi_asic_summed(self):
        info = {"hashrateMonitor": {"asics": [{"errorCount": 100}, {"errorCount": 250}]}}
        self.assertEqual(b.sum_error_count(info), 350)

    def test_missing_monitor(self):
        self.assertIsNone(b.sum_error_count({"errorPercentage": 2.0}))

    def test_empty_asics(self):
        self.assertIsNone(b.sum_error_count({"hashrateMonitor": {"asics": []}}))


class TestComputeWindowError(unittest.TestCase):
    def test_mean_of_error_percentage(self):
        # errorPercentage is a noisy rolling rate; the metric is its mean.
        samples = [sample(1000, 14.2), sample(1100, 10.2), sample(1200, 18.6)]
        rate, method = b.compute_window_error(samples)
        self.assertEqual(method, "errorPercentage-mean")
        self.assertAlmostEqual(rate, (14.2 + 10.2 + 18.6) / 3, places=6)

    def test_mean_works_without_raw_counts(self):
        samples = [sample(None, 4.0), sample(None, 6.0)]
        rate, method = b.compute_window_error(samples)
        self.assertEqual(method, "errorPercentage-mean")
        self.assertAlmostEqual(rate, 5.0, places=6)

    def test_no_data_returns_none(self):
        rate, method = b.compute_window_error([sample(None, None), sample(None, None)])
        self.assertIsNone(rate)
        self.assertEqual(method, "unavailable")


class TestWindowErrorCount(unittest.TestCase):
    def test_delta_of_cumulative_counts(self):
        samples = [sample(1959645, 14.2), sample(1959807, 10.2), sample(1960013, 18.6)]
        self.assertEqual(b.window_error_count(samples), 1960013 - 1959645)

    def test_counter_reset_skips_the_reset_step(self):
        # Reboot mid-window (100 -> 5): skip that decrease, sum the valid +115 step.
        self.assertEqual(b.window_error_count([sample(100, 5.0), sample(5, 4.0), sample(120, 3.0)]), 115)

    def test_only_a_reset_step_yields_zero(self):
        # A lone backwards step means no measurable accrual, not a bogus delta.
        self.assertEqual(b.window_error_count([sample(9000, 5.0), sample(10, 3.0)]), 0)

    def test_missing_counts_returns_none(self):
        self.assertIsNone(b.window_error_count([sample(None, 5.0), sample(None, 3.0)]))


class TestGate(unittest.TestCase):
    def test_within_ceiling_passes(self):
        self.assertTrue(b.passes_error_gate(3.0, 3.5))

    def test_over_ceiling_fails(self):
        self.assertFalse(b.passes_error_gate(4.0, 3.5))

    def test_exactly_at_ceiling_passes(self):
        self.assertTrue(b.passes_error_gate(3.5, 3.5))

    def test_unknown_error_passes(self):
        self.assertTrue(b.passes_error_gate(None, 3.5))


class TestSelectBest(unittest.TestCase):
    def _r(self, cv, f, hr, jth, er):
        return {
            "coreVoltage": cv, "frequency": f, "averageHashRate": hr,
            "efficiencyJTH": jth, "errorRate": er,
            "passedErrorGate": b.passes_error_gate(er, 3.5),
        }

    def test_prefers_lowest_jth_among_passers(self):
        rs = [
            self._r(1150, 575, 1200, 15.0, 2.0),   # passes, eff 15.0
            self._r(1100, 575, 1180, 14.2, 3.0),   # passes, eff 14.2  <- best
            self._r(1050, 575, 1220, 13.9, 9.0),   # BEST eff but OVER ceiling
        ]
        best = b.select_best(rs, 3.5, gate_enabled=True)
        self.assertEqual(best["coreVoltage"], 1100)

    def test_ignores_low_error_high_jth_when_better_passer_exists(self):
        rs = [
            self._r(1200, 575, 1150, 16.0, 0.5),   # super low error but worst eff
            self._r(1100, 575, 1180, 14.2, 3.0),   # best eff among passers
        ]
        best = b.select_best(rs, 3.5, gate_enabled=True)
        self.assertEqual(best["coreVoltage"], 1100)

    def test_tie_breaks_on_hashrate(self):
        rs = [
            self._r(1100, 575, 1180, 14.2, 2.0),
            self._r(1120, 600, 1250, 14.2, 3.0),   # same eff, higher hashrate
        ]
        best = b.select_best(rs, 3.5, gate_enabled=True)
        self.assertEqual(best["frequency"], 600)

    def test_falls_back_to_lowest_error_when_none_pass(self):
        rs = [
            self._r(1050, 600, 1280, 14.0, 12.0),
            self._r(1070, 600, 1270, 14.5, 8.0),   # lowest error of the failing set
        ]
        best = b.select_best(rs, 3.5, gate_enabled=True)
        self.assertEqual(best["errorRate"], 8.0)

    def test_gate_disabled_uses_pure_efficiency(self):
        rs = [
            self._r(1150, 575, 1200, 15.0, 2.0),
            self._r(1050, 575, 1220, 13.9, 9.0),   # over ceiling but best eff
        ]
        best = b.select_best(rs, 3.5, gate_enabled=False)
        self.assertEqual(best["coreVoltage"], 1050)

    def test_excludes_out_of_tolerance_passer(self):
        # A gate-passing but throttling (low-hashrate) combo must not win.
        throttled = {**self._r(1100, 575, 850, 12.0, 1.5), "hashrateWithinTolerance": False}
        healthy = {**self._r(1150, 575, 1180, 14.5, 2.5), "hashrateWithinTolerance": True}
        best = b.select_best([throttled, healthy], 3.5, gate_enabled=True)
        self.assertEqual(best["coreVoltage"], 1150)

    def test_recomputes_gate_ignoring_stale_flag(self):
        # A resumed record can carry a stale passedErrorGate. Selection must
        # recompute from errorRate + the active ceiling, not trust the flag.
        stale = {"coreVoltage": 1050, "frequency": 575, "averageHashRate": 1250,
                 "efficiencyJTH": 13.5, "errorRate": 9.0, "passedErrorGate": True,
                 "hashrateWithinTolerance": True}
        real = {"coreVoltage": 1150, "frequency": 575, "averageHashRate": 1180,
                "efficiencyJTH": 15.0, "errorRate": 2.0, "passedErrorGate": True,
                "hashrateWithinTolerance": True}
        best = b.select_best([stale, real], 3.5, gate_enabled=True)
        self.assertEqual(best["coreVoltage"], 1150)   # 9% rejected despite the flag

    def test_legacy_result_without_flag_is_gated_by_error_rate(self):
        # A file predating passedErrorGate must still be gated by errorRate.
        legacy_pass = {"coreVoltage": 1150, "frequency": 575, "averageHashRate": 1180,
                       "efficiencyJTH": 15.0, "errorRate": 2.0, "hashrateWithinTolerance": True}
        legacy_fail = {"coreVoltage": 1050, "frequency": 575, "averageHashRate": 1250,
                       "efficiencyJTH": 13.0, "errorRate": 8.0, "hashrateWithinTolerance": True}
        best = b.select_best([legacy_pass, legacy_fail], 3.5, gate_enabled=True)
        self.assertEqual(best["coreVoltage"], 1150)

    def test_empty_returns_none(self):
        self.assertIsNone(b.select_best([], 3.5))


class TestErrorStatsAndTrim(unittest.TestCase):
    def test_trimmed_mean_when_enough_samples(self):
        # 5 samples [1,2,3,4,100] -> drop the 1 and the 100 -> mean(2,3,4) = 3.
        s = [sample(None, x) for x in (1, 2, 3, 4, 100)]
        rate, method = b.compute_window_error(s)
        self.assertEqual(method, "errorPercentage-trimmed-mean")
        self.assertAlmostEqual(rate, 3.0, places=6)

    def test_plain_mean_when_few_samples(self):
        rate, method = b.compute_window_error([sample(None, 2.0), sample(None, 4.0)])
        self.assertEqual(method, "errorPercentage-mean")
        self.assertAlmostEqual(rate, 3.0, places=6)

    def test_error_stats(self):
        st = b.window_error_stats([sample(None, 2.0), sample(None, 4.0), sample(None, 6.0)])
        self.assertEqual((st["min"], st["max"], st["n"]), (2.0, 6.0, 3))
        self.assertGreater(st["std"], 0)

    def test_error_stats_none_without_data(self):
        self.assertIsNone(b.window_error_stats([sample(1, None)]))


class TestBenchmarkIterationResult(unittest.TestCase):
    """benchmark_iteration returns a result DICT (fields read by name), which
    removes the positional-arity crash class that bit the old tuple twice."""

    def setUp(self):
        self._saved = (b.small_core_count, b.asic_count, b.benchmark_time,
                       b.sample_interval, b.error_gate_enabled, b.max_error_rate, b.results)
        b.small_core_count = 2040
        b.asic_count = 1
        b.benchmark_time = 20
        b.sample_interval = 1
        b.error_gate_enabled = True
        b.max_error_rate = 3.5
        b.results = []

    def tearDown(self):
        (b.small_core_count, b.asic_count, b.benchmark_time,
         b.sample_interval, b.error_gate_enabled, b.max_error_rate, b.results) = self._saved

    def _run(self, info_or_seq):
        kw = {"side_effect": info_or_seq} if isinstance(info_or_seq, list) else {"return_value": info_or_seq}
        with mock.patch.object(b, "get_system_info", **kw), \
             mock.patch.object(b.time, "sleep", return_value=None):
            return b.benchmark_iteration(1150, 525)

    def test_iter_fail_is_dict(self):
        r = b._iter_fail("X")
        self.assertFalse(r["ok"])
        self.assertEqual(r["reason"], "X")

    def test_success(self):
        r = self._run(make_info())
        self.assertTrue(r["ok"])
        self.assertIsNotNone(r["averageHashRate"])
        self.assertIsNotNone(r["errorRate"])
        self.assertFalse(r["earlyAborted"])

    def test_thermal_guard(self):
        r = self._run(make_info(temp=99))
        self.assertFalse(r["ok"])
        self.assertEqual(r["reason"], "CHIP_TEMP_EXCEEDED")

    def test_zero_hashrate(self):
        r = self._run(make_info(hr=0))
        self.assertFalse(r["ok"])
        self.assertEqual(r["reason"], "ZERO_HASHRATE")

    def test_boot_glitch_skipped(self):
        seq = [make_info(temp=2) for _ in range(3)] + [make_info() for _ in range(20)]
        r = self._run(seq)
        self.assertTrue(r["ok"])
        self.assertIsNotNone(r["averageHashRate"])

    def test_unreachable_error_records_early_floor(self):
        # Sustained error over the ceiling -> abort early, but still a recorded
        # (partial) floor so select_best always has a fallback.
        r = self._run(make_info(ep=20.0))
        self.assertTrue(r["ok"])
        self.assertTrue(r["earlyAborted"])
        self.assertEqual(r["reason"], "EARLY_ABORT")
        self.assertFalse(b.passes_error_gate(r["errorRate"], 3.5))

    def test_single_spike_does_not_early_abort(self):
        # One 100% rolling-rate spike among otherwise-low readings must NOT abort:
        # the bound drops the max, mirroring the trimmed-mean final metric.
        seq = ([make_info(ep=1.0) for _ in range(6)] + [make_info(ep=100.0)]
               + [make_info(ep=1.0) for _ in range(13)])
        r = self._run(seq)
        self.assertTrue(r["ok"])
        self.assertFalse(r["earlyAborted"])
        self.assertTrue(b.passes_error_gate(r["errorRate"], 3.5))


class TestComboPasses(unittest.TestCase):
    def setUp(self):
        self._saved = (b.error_gate_enabled, b.max_error_rate)
        b.error_gate_enabled = True
        b.max_error_rate = 3.5

    def tearDown(self):
        (b.error_gate_enabled, b.max_error_rate) = self._saved

    def test_recomputes_from_error_rate_not_stale_flag(self):
        over = {"errorRate": 9.0, "passedErrorGate": True, "hashrateWithinTolerance": True}
        self.assertFalse(b.combo_passes(over))          # flag says pass, error says no
        good = {"errorRate": 2.0, "passedErrorGate": False, "hashrateWithinTolerance": True}
        self.assertTrue(b.combo_passes(good))           # flag says fail, error says yes


class TestEfficiencyResume(unittest.TestCase):
    """Efficiency resume must stop at a recorded failing voltage, not skip past it."""

    def setUp(self):
        self._saved = (b.initial_voltage, b.initial_frequency, b.resume_enabled, b.results,
                       b.error_gate_enabled, b.max_error_rate, b.voltage_increment)
        b.initial_voltage = 1150
        b.initial_frequency = 600
        b.resume_enabled = True
        b.error_gate_enabled = True
        b.max_error_rate = 3.5
        b.voltage_increment = 20

    def tearDown(self):
        (b.initial_voltage, b.initial_frequency, b.resume_enabled, b.results,
         b.error_gate_enabled, b.max_error_rate, b.voltage_increment) = self._saved

    @staticmethod
    def _rec(cv, er):
        return {"coreVoltage": cv, "frequency": 600, "averageHashRate": 1200,
                "efficiencyJTH": 15.0, "errorRate": er, "passedErrorGate": er <= 3.5,
                "hashrateWithinTolerance": True}

    def test_stops_at_recorded_failure(self):
        # Start (1150) passed, the next voltage down (1130) failed: must stop there,
        # not probe below a known failure. Everything is recorded, so no run_combo.
        b.results = [self._rec(1150, 2.0), self._rec(1130, 9.0)]
        with mock.patch.object(b, "run_combo") as rc:
            b.run_efficiency()
        rc.assert_not_called()


class TestRefineControlFlow(unittest.TestCase):
    """Refine should drop frequency when a frequency is thermally boxed in."""

    def setUp(self):
        self._saved = (b.initial_voltage, b.initial_frequency, b.max_error_rate,
                       b.error_gate_enabled, b.resume_enabled, b.max_temp, b.results)
        b.initial_voltage = 1100
        b.initial_frequency = 600
        b.max_error_rate = 3.5
        b.error_gate_enabled = True
        b.resume_enabled = False
        b.max_temp = 68
        b.results = []

    def tearDown(self):
        (b.initial_voltage, b.initial_frequency, b.max_error_rate,
         b.error_gate_enabled, b.resume_enabled, b.max_temp, b.results) = self._saved

    @staticmethod
    def _passing(v, f):
        return {"coreVoltage": v, "frequency": f, "efficiencyJTH": 15.0,
                "averageHashRate": 1200, "passedErrorGate": True,
                "hashrateWithinTolerance": True, "errorRate": 2.0}

    def test_sweep_returns_capped_on_thermal(self):
        with mock.patch.object(b, "run_combo", return_value=(None, "CHIP_TEMP_EXCEEDED")):
            self.assertEqual(b._refine_sweep_at(600), "capped")

    def test_refine_drops_frequency_when_thermally_capped(self):
        calls = []

        def fake(v, f):
            calls.append((v, f))
            if f >= 600:
                return None, "CHIP_TEMP_EXCEEDED"     # 600 MHz is thermally boxed
            return self._passing(v, f), None          # 575 MHz clears the ceiling

        with mock.patch.object(b, "run_combo", side_effect=fake):
            b.run_refine()

        tried_freqs = {f for _, f in calls}
        self.assertIn(600, tried_freqs)
        self.assertIn(575, tried_freqs)   # dropped by frequency_increment (25)

    def test_resume_uses_recorded_passer(self):
        # A resumed refine must honor a recorded passing combo, not climb past it.
        b.resume_enabled = True
        b.results = [self._passing(b.initial_voltage, b.initial_frequency)]
        with mock.patch.object(b, "run_combo", return_value=(None, "CHIP_TEMP_EXCEEDED")):
            outcome = b._refine_sweep_at(b.initial_frequency)
        self.assertEqual(outcome, "passed")


class TestCheckMode(unittest.TestCase):
    """--check must measure the current setting without applying or rebooting."""

    def setUp(self):
        self._saved = (b.small_core_count, b.asic_count, b.benchmark_time, b.sample_interval,
                       b.default_voltage, b.default_frequency, b.error_gate_enabled, b.max_error_rate)
        b.small_core_count = 2040
        b.asic_count = 1
        b.benchmark_time = 20
        b.sample_interval = 1
        b.default_voltage = 1150
        b.default_frequency = 525
        b.error_gate_enabled = True
        b.max_error_rate = 3.5

    def tearDown(self):
        (b.small_core_count, b.asic_count, b.benchmark_time, b.sample_interval,
         b.default_voltage, b.default_frequency, b.error_gate_enabled, b.max_error_rate) = self._saved

    def test_check_is_read_only(self):
        with mock.patch.object(b, "get_system_info", return_value=make_info()), \
             mock.patch.object(b.time, "sleep", return_value=None), \
             mock.patch.object(b, "set_system_settings") as ss, \
             mock.patch.object(b, "restart_system") as rs:
            b.run_check()
        ss.assert_not_called()   # nothing applied
        rs.assert_not_called()   # no reboot

    def test_sigint_during_check_makes_no_changes(self):
        # Ctrl+C during a --check must exit without PATCHing/rebooting the device.
        saved = (b.check_mode, b.handling_interrupt, b.system_reset_done)
        b.check_mode = True
        b.handling_interrupt = False
        b.system_reset_done = False
        try:
            with mock.patch.object(b, "set_system_settings") as ss:
                with self.assertRaises(SystemExit) as cm:
                    b.handle_sigint(2, None)
            ss.assert_not_called()
            self.assertEqual(cm.exception.code, 0)
        finally:
            (b.check_mode, b.handling_interrupt, b.system_reset_done) = saved


if __name__ == "__main__":
    unittest.main(verbosity=2)
