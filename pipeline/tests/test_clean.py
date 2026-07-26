"""Tests for trace cleaning.

Every case is built from a synthetic trace whose noise, dwells and outliers were
injected on purpose, so assertions are against known truth rather than against
a golden output.
"""

import numpy as np
import pandas as pd
import pytest
from conftest import rmse_against_truth

from clean import (
    MAX_ACCURACY_M,
    MAX_SPEED_KMH,
    UTM36S,
    clean_trace,
    drop_inaccurate,
    drop_speed_outliers,
    kalman_smooth,
    trace_seconds,
)
from synthetic import synthetic_trace


class TestDropInaccurate:
    def test_drops_exactly_the_injected_bad_fixes(self):
        trace = synthetic_trace(duration_s=60.0, bad_accuracy_indices=(10, 25, 40), seed=3)

        kept = drop_inaccurate(trace.noisy)

        assert sorted(set(trace.noisy["seq"]) - set(kept["seq"])) == [10, 25, 40]
        assert (kept["accuracy_m"] <= MAX_ACCURACY_M).all()

    def test_keeps_everything_when_all_fixes_are_good(self, clean_run):
        assert len(drop_inaccurate(clean_run.noisy)) == len(clean_run.noisy)

    def test_boundary_is_inclusive(self):
        trace = synthetic_trace(duration_s=10.0, reported_accuracy_m=MAX_ACCURACY_M, seed=4)

        assert len(drop_inaccurate(trace.noisy)) == len(trace.noisy)

    def test_keeps_points_with_unknown_accuracy(self):
        trace = synthetic_trace(duration_s=10.0, seed=5)
        noisy = trace.noisy.copy()
        noisy.loc[3, "accuracy_m"] = np.nan

        kept = drop_inaccurate(noisy)

        # An unreported accuracy is missing information, not a bad fix.
        assert 3 in set(kept["seq"])

    def test_threshold_is_configurable(self):
        trace = synthetic_trace(duration_s=30.0, reported_accuracy_m=30.0, seed=6)

        assert len(drop_inaccurate(trace.noisy, max_accuracy_m=20.0)) == 0
        assert len(drop_inaccurate(trace.noisy, max_accuracy_m=40.0)) == len(trace.noisy)


class TestDropSpeedOutliers:
    def test_drops_jumps_that_report_a_good_accuracy(self):
        # These carry an honest-looking accuracy, so only the speed gate sees
        # them: 4 km in one second is 14 400 km/h.
        trace = synthetic_trace(duration_s=120.0, jump_indices=(30, 80), seed=7)

        kept = drop_speed_outliers(trace.noisy)

        assert sorted(set(trace.noisy["seq"]) - set(kept["seq"])) == [30, 80]

    def test_keeps_a_plausible_kombi_at_full_speed(self):
        # 30 m/s = 108 km/h: fast for a kombi, still under the limit.
        trace = synthetic_trace(duration_s=100.0, speed_mps=30.0, noise_sigma_m=0.0, seed=8)

        assert len(drop_speed_outliers(trace.noisy)) == len(trace.noisy)

    def test_drops_travel_above_the_limit(self):
        # 40 m/s = 144 km/h. The first point anchors, everything after exceeds.
        trace = synthetic_trace(
            duration_s=60.0,
            speed_mps=40.0,
            noise_sigma_m=0.0,
            waypoints=((0.0, 0.0), (5000.0, 0.0)),
            seed=9,
        )

        kept = drop_speed_outliers(trace.noisy)

        assert len(kept) == 1

    def test_recovers_after_a_jump_instead_of_derailing(self):
        """One bad fix costs one point, not the remainder of the trace."""
        trace = synthetic_trace(duration_s=120.0, jump_indices=(50,), seed=10)

        kept = drop_speed_outliers(trace.noisy)

        assert len(kept) == len(trace.noisy) - 1
        assert set(kept["seq"]) == set(range(len(trace.noisy))) - {50}

    def test_drops_points_that_do_not_advance_the_clock(self):
        trace = synthetic_trace(duration_s=20.0, noise_sigma_m=0.0, seed=11)
        noisy = trace.noisy.copy()
        noisy.loc[7, "recorded_at"] = noisy.loc[6, "recorded_at"]

        kept = drop_speed_outliers(noisy)

        assert 7 not in set(kept["seq"])

    def test_single_point_trace_is_returned_unchanged(self):
        trace = synthetic_trace(duration_s=0.0, seed=12)

        assert len(trace.noisy) == 1
        assert len(drop_speed_outliers(trace.noisy)) == 1


class TestKalmanSmooth:
    def test_reduces_error_against_known_truth(self, clean_run):
        raw_rmse = rmse_against_truth(clean_run.noisy, clean_run.true_xy())
        smoothed_rmse = rmse_against_truth(kalman_smooth(clean_run.noisy), clean_run.true_xy())

        # Injected noise is 4 m per axis, so the raw trace sits around 5.7 m
        # RMSE. Halving that is a conservative bar for a CV filter with an RTS
        # pass at 1 Hz; in practice it does considerably better.
        assert smoothed_rmse < 0.5 * raw_rmse

    def test_backward_pass_beats_the_forward_filter_alone(self, clean_run):
        truth = clean_run.true_xy()
        forward_only = kalman_smooth(clean_run.noisy, rts_smooth=False)
        smoothed = kalman_smooth(clean_run.noisy, rts_smooth=True)

        # The forward filter only ever sees the past, so it lags every turn.
        assert rmse_against_truth(smoothed, truth) < rmse_against_truth(forward_only, truth)

    def test_tracks_a_noise_free_straight_line_almost_exactly(self):
        trace = synthetic_trace(
            duration_s=120.0,
            noise_sigma_m=0.0,
            reported_accuracy_m=5.0,
            waypoints=((0.0, 0.0), (4000.0, 0.0)),
            seed=13,
        )

        smoothed = kalman_smooth(trace.noisy)

        # Constant velocity in a straight line is exactly the model's
        # assumption; only the zero-velocity seed costs anything, and the RTS
        # pass removes most of that.
        assert rmse_against_truth(smoothed, trace.true_xy()) < 1.0

    def test_recovers_the_true_velocity(self):
        trace = synthetic_trace(
            duration_s=200.0,
            speed_mps=12.0,
            noise_sigma_m=3.0,
            waypoints=((0.0, 0.0), (5000.0, 0.0)),
            seed=14,
        )

        smoothed = kalman_smooth(trace.noisy)

        # Skip the settling window at the start, where velocity is seeded at 0.
        settled = smoothed["speed_kf_mps"].to_numpy()[20:]
        assert np.abs(np.mean(settled) - 12.0) < 1.0
        # Velocity points east along the path.
        assert np.mean(smoothed["vx_mps"].to_numpy()[20:]) > 11.0
        assert abs(np.mean(smoothed["vy_mps"].to_numpy()[20:])) < 1.0

    def test_down_weights_a_poorly_reported_fix(self):
        """A fix that admits it is bad should barely move the track."""
        trace = synthetic_trace(duration_s=60.0, noise_sigma_m=0.0, seed=15)
        noisy = trace.noisy.copy()
        offset_index = 30
        original = noisy.geometry.iloc[offset_index]
        noisy.loc[offset_index, "geometry"] = original.__class__(original.x, original.y + 80.0)
        noisy.loc[offset_index, "accuracy_m"] = 45.0  # bad, but under the gate

        smoothed = kalman_smooth(noisy)

        truth = trace.true_xy()[offset_index]
        error = np.hypot(
            smoothed.geometry.iloc[offset_index].x - truth[0],
            smoothed.geometry.iloc[offset_index].y - truth[1],
        )
        assert error < 20.0, "an 80 m offset with a 45 m accuracy should be mostly rejected"

    def test_preserves_shape_columns_crs_and_timestamps(self, clean_run):
        smoothed = kalman_smooth(clean_run.noisy)

        assert len(smoothed) == len(clean_run.noisy)
        assert smoothed.crs == clean_run.noisy.crs
        assert str(smoothed.crs).upper() == UTM36S
        assert {"vx_mps", "vy_mps", "speed_kf_mps"} <= set(smoothed.columns)
        pd.testing.assert_series_equal(smoothed["recorded_at"], clean_run.noisy["recorded_at"])
        pd.testing.assert_series_equal(smoothed["seq"], clean_run.noisy["seq"])

    def test_handles_irregular_sampling(self):
        """dt comes from the timestamps, so dropped samples are not assumed away."""
        trace = synthetic_trace(duration_s=200.0, noise_sigma_m=3.0, seed=16)
        keep = np.ones(len(trace.noisy), dtype=bool)
        keep[50:65] = False  # a 15 s gap, as if the phone throttled GPS
        gapped = trace.noisy.loc[keep].reset_index(drop=True)
        gapped_truth = trace.true_xy()[keep]

        smoothed = kalman_smooth(gapped)

        assert rmse_against_truth(smoothed, gapped_truth) < 4.0

    def test_empty_trace_is_returned_unchanged(self, clean_run):
        empty = clean_run.noisy.iloc[0:0]

        assert len(kalman_smooth(empty)) == 0

    def test_rejects_a_trace_with_missing_timestamps(self, clean_run):
        noisy = clean_run.noisy.copy()
        noisy.loc[5, "recorded_at"] = pd.NaT

        with pytest.raises(ValueError, match="no timestamp"):
            kalman_smooth(noisy)


class TestCleanTrace:
    def test_removes_every_injected_outlier_and_smooths_the_rest(self):
        trace = synthetic_trace(
            duration_s=300.0,
            noise_sigma_m=4.0,
            bad_accuracy_indices=(40, 41, 150),
            jump_indices=(90, 220),
            seed=17,
        )

        cleaned = clean_trace(trace.noisy)

        assert set(cleaned["seq"]) == set(range(len(trace.noisy))) - set(trace.dropped_indices)

        # Row alignment is broken by the drops, so compare against the truth
        # rows that survived.
        survived = np.array(sorted(set(range(len(trace.noisy))) - set(trace.dropped_indices)))
        assert rmse_against_truth(cleaned, trace.true_xy()[survived]) < 3.0

    def test_output_is_reindexed_from_zero_but_keeps_seq(self):
        trace = synthetic_trace(duration_s=100.0, jump_indices=(20,), seed=18)

        cleaned = clean_trace(trace.noisy)

        assert list(cleaned.index) == list(range(len(cleaned)))
        assert 20 not in set(cleaned["seq"])

    def test_thresholds_are_forwarded(self):
        trace = synthetic_trace(duration_s=60.0, reported_accuracy_m=30.0, seed=19)

        assert len(clean_trace(trace.noisy, max_accuracy_m=20.0)) == 0

    def test_kalman_arguments_are_forwarded(self, clean_run):
        truth = clean_run.true_xy()

        loose = clean_trace(clean_run.noisy, accel_noise_mps2=5.0)
        tight = clean_trace(clean_run.noisy, accel_noise_mps2=0.2)

        # A tighter acceleration prior trusts the model over the measurements,
        # which on a mostly-straight path means a closer fit to truth.
        assert rmse_against_truth(tight, truth) < rmse_against_truth(loose, truth)


class TestTraceSeconds:
    def test_starts_at_zero_and_matches_the_sampling_rate(self):
        trace = synthetic_trace(duration_s=10.0, sample_hz=2.0, seed=20)

        seconds = trace_seconds(trace.noisy)

        assert seconds[0] == 0.0
        np.testing.assert_allclose(np.diff(seconds), 0.5)

    def test_rejects_a_frame_with_no_timestamp_column(self, clean_run):
        with pytest.raises(ValueError, match="no 'recorded_at' column"):
            trace_seconds(clean_run.noisy.drop(columns=["recorded_at"]))


def test_speed_limit_constants_are_the_documented_ones():
    assert MAX_ACCURACY_M == 50.0
    assert MAX_SPEED_KMH == 120.0
