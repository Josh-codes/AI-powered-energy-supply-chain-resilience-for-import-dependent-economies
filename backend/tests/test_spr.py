"""Tests for response/spr.py — the SPR drawdown linear program.

No database, no network: ``compute_spr_schedule`` is a pure function of
scalars, so this uses bare ``unittest.TestCase`` (the test_score_candidates.py
precedent).

What is and is not pinned to a number: EVERYTHING here is pinned. The LP is
deterministic over its scalar inputs and none of them come from the live
corpus — contrast test_criticality.py, where risk-weighted quantities move
with the news and are asserted by property only.
"""
import unittest

from response.spr import (
    MAX_DAILY_MBD,
    SPR_SAFETY_PCT,
    SPR_TOTAL_MB,
    compute_spr_schedule,
)

AVAILABLE_MB = SPR_TOTAL_MB * (1 - SPR_SAFETY_PCT)  # 29.496 mb


class ShapeAndContractTests(unittest.TestCase):
    def test_schedule_length_equals_duration(self):
        for duration in (1, 7, 14, 45):
            r = compute_spr_schedule(0.5, duration, 10)
            self.assertEqual(len(r["daily_schedule"]), duration, msg=duration)

    def test_documented_keys_are_present(self):
        """The four keys CLAUDE.md's spec returns must survive the rewrite."""
        r = compute_spr_schedule(0.5, 14, 10)
        for key in ("daily_schedule", "total_released_mb", "insufficient", "gap_covered_pct"):
            self.assertIn(key, r)

    def test_schedule_never_contains_none(self):
        """The spec's hard-constraint LP returned None for every day when
        infeasible. Across covered, rate-limited and reserve-limited cases the
        schedule must be all floats."""
        for gap, total in ((0.5, SPR_TOTAL_MB), (3.0, SPR_TOTAL_MB), (1.0, 2.0)):
            r = compute_spr_schedule(gap, 20, 15, spr_total_mb=total)
            self.assertTrue(all(isinstance(v, float) for v in r["daily_schedule"]))
            self.assertEqual(r["status"], "Optimal")

    def test_release_is_zero_after_transit(self):
        r = compute_spr_schedule(0.8, 14, 10)
        self.assertTrue(all(v == 0.0 for v in r["daily_schedule"][10:]))

    def test_release_never_exceeds_daily_limit_or_gap(self):
        for gap in (0.3, 1.0, 2.5):
            r = compute_spr_schedule(gap, 14, 10)
            for v in r["daily_schedule"]:
                self.assertLessEqual(v, min(gap, MAX_DAILY_MBD) + 1e-9)

    def test_total_never_exceeds_releasable_reserve(self):
        r = compute_spr_schedule(1.0, 60, 60)
        self.assertLessEqual(r["total_released_mb"], AVAILABLE_MB + 1e-6)


class CoverageTests(unittest.TestCase):
    def test_coverable_gap_is_fully_met(self):
        r = compute_spr_schedule(0.8, 14, 10)
        for v in r["daily_schedule"][:10]:
            self.assertAlmostEqual(v, 0.8, places=9)
        self.assertAlmostEqual(r["total_released_mb"], 8.0, places=6)
        self.assertFalse(r["insufficient"])
        self.assertAlmostEqual(r["gap_covered_pct"], 100.0, places=6)

    def test_gap_above_daily_limit_is_insufficient(self):
        """The 1.0 mb/d physical limit binds, not the reserve."""
        r = compute_spr_schedule(2.0, 14, 10)
        self.assertTrue(r["insufficient"])
        self.assertAlmostEqual(r["gap_covered_pct"], 50.0, places=6)
        self.assertAlmostEqual(r["total_unmet_mb"], 10.0, places=6)

    def test_schedule_is_best_effort_when_reserves_are_short(self):
        """The spec LP is infeasible here. This one releases the whole
        releasable reserve and reports the rest as unmet."""
        r = compute_spr_schedule(1.0, 60, 60)  # needs 60 mb, 29.496 available
        self.assertTrue(r["insufficient"])
        self.assertAlmostEqual(r["total_released_mb"], AVAILABLE_MB, places=6)
        self.assertAlmostEqual(r["total_unmet_mb"], 60.0 - AVAILABLE_MB, places=6)

    def test_shortfall_is_spread_evenly_when_the_reserve_binds(self):
        """The peak-shortfall term: 5 mb against a 10-day, 1.0 mb/d gap gives
        0.5 released and 0.5 unmet EVERY day, not five full days then a cliff.
        Without the term the solver may return either — this pins the choice."""
        r = compute_spr_schedule(1.0, 10, 10, spr_total_mb=5.0, safety_pct=0.0)
        for v in r["daily_schedule"]:
            self.assertAlmostEqual(v, 0.5, places=6)
        for u in r["unmet_mbd"]:
            self.assertAlmostEqual(u, 0.5, places=6)

    def test_safety_floor_is_never_released(self):
        r = compute_spr_schedule(1.0, 60, 60, spr_total_mb=10.0, safety_pct=0.5)
        self.assertAlmostEqual(r["total_released_mb"], 5.0, places=6)

    def test_days_of_cover(self):
        self.assertAlmostEqual(compute_spr_schedule(0.5, 14, 10)["days_of_cover"], AVAILABLE_MB / 0.5)
        # capped at the physical rate: a 3.0 gap still only draws 1.0/day
        self.assertAlmostEqual(compute_spr_schedule(3.0, 14, 10)["days_of_cover"], AVAILABLE_MB / 1.0)


class GapProfileTests(unittest.TestCase):
    """The per-day gap: cargoes land on different days and a residual can
    stay open after the last one."""

    def test_release_continues_after_the_last_cargo_when_a_residual_remains(self):
        """The bug this mode fixes: the constant-gap model stopped releasing
        at the slowest arrival (day 16 on Cape) with reserve left over while a
        residual gap stayed open."""
        profile = [2.0] * 5 + [0.7] * 25  # last cargo lands day 5, 0.7 mb/d never closes
        r = compute_spr_schedule(2.0, 30, 5, gap_profile=profile)
        for v in r["daily_schedule"][5:]:
            self.assertAlmostEqual(v, 0.7, places=6)
        self.assertAlmostEqual(r["gap_at_horizon_end_mbd"], 0.7)
        self.assertIsNone(r["reserve_exhausted_day"])

    def test_release_follows_the_profile_down_as_cargoes_land(self):
        profile = [0.9, 0.9, 0.6, 0.6, 0.2, 0.0]
        r = compute_spr_schedule(0.9, 6, 4, gap_profile=profile)
        for v, g in zip(r["daily_schedule"], profile):
            self.assertAlmostEqual(v, g, places=6)
        self.assertAlmostEqual(r["required_mb"], sum(profile), places=9)
        self.assertEqual(r["bridge_days"], 5)
        self.assertFalse(r["insufficient"])

    def test_reserve_exhausted_day(self):
        """Days 0-4 are rate-capped (gap 2.0 vs 1.0/day), which fixes the peak
        shortfall at 1.0. Below that peak the early-first tie-break decides:
        the remaining 3 mb go to days 5-10 at 0.5, and the reserve runs dry on
        day 10 with gap still open after it. Pinned because without the
        tie-break any spreading of those 3 mb is equally optimal."""
        profile = [2.0] * 5 + [0.5] * 20
        r = compute_spr_schedule(2.0, 25, 5, gap_profile=profile, spr_total_mb=8.0, safety_pct=0.0)
        expected = [1.0] * 5 + [0.5] * 6 + [0.0] * 14
        for v, e in zip(r["daily_schedule"], expected):
            self.assertAlmostEqual(v, e, places=6)
        self.assertEqual(r["reserve_exhausted_day"], 10)

    def test_constant_profile_matches_constant_gap_mode(self):
        default = compute_spr_schedule(0.8, 14, 10)
        profiled = compute_spr_schedule(0.8, 14, 10, gap_profile=[0.8] * 10 + [0.0] * 4)
        self.assertEqual(
            [round(v, 9) for v in default["daily_schedule"]],
            [round(v, 9) for v in profiled["daily_schedule"]],
        )

    def test_tail_days_are_only_reported_in_constant_mode(self):
        self.assertIsNone(compute_spr_schedule(0.5, 3, 10, gap_profile=[0.5] * 3)["uncovered_tail_days"])
        self.assertEqual(compute_spr_schedule(0.5, 3, 10)["uncovered_tail_days"], 7)

    def test_invalid_profile_raises(self):
        with self.assertRaises(ValueError):
            compute_spr_schedule(0.5, 5, 3, gap_profile=[0.5] * 4)
        with self.assertRaises(ValueError):
            compute_spr_schedule(0.5, 3, 3, gap_profile=[0.5, -0.1, 0.5])


class EdgeCaseTests(unittest.TestCase):
    def test_zero_gap_is_a_zero_schedule_not_a_division_by_zero(self):
        """The spec's gap_covered_pct divides by gap * transit."""
        r = compute_spr_schedule(0.0, 5, 3)
        self.assertEqual(r["daily_schedule"], [0.0] * 5)
        self.assertFalse(r["insufficient"])
        self.assertEqual(r["gap_covered_pct"], 100.0)
        self.assertIsNone(r["days_of_cover"])

    def test_zero_transit_means_nothing_to_bridge(self):
        r = compute_spr_schedule(1.0, 5, 0)
        self.assertEqual(r["bridge_days"], 0)
        self.assertEqual(r["total_released_mb"], 0.0)
        self.assertFalse(r["insufficient"])

    def test_horizon_shorter_than_transit_reports_the_uncovered_tail(self):
        r = compute_spr_schedule(0.5, 3, 10)
        self.assertEqual(r["bridge_days"], 3)
        self.assertEqual(r["uncovered_tail_days"], 7)
        self.assertAlmostEqual(r["required_mb"], 1.5, places=9)

    def test_invalid_inputs_raise(self):
        with self.assertRaises(ValueError):
            compute_spr_schedule(-0.1, 14, 10)
        with self.assertRaises(ValueError):
            compute_spr_schedule(0.5, 0, 10)
        with self.assertRaises(ValueError):  # EARLY_TIE_BREAK * D must stay < 1
            compute_spr_schedule(0.5, 1000, 10)
        with self.assertRaises(ValueError):
            compute_spr_schedule(0.5, 14, -1)
        with self.assertRaises(ValueError):
            compute_spr_schedule(0.5, 14, 10, safety_pct=1.0)
        with self.assertRaises(ValueError):
            compute_spr_schedule(0.5, 14, 10, max_daily_mbd=0)


if __name__ == "__main__":
    unittest.main()
