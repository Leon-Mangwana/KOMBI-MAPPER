"""Tests for dwell detection.

Dwell windows are injected at known times and positions, so both the reported
duration and the reported centroid can be checked against truth.
"""

import numpy as np
import pandas as pd
import pytest

from clean import clean_trace
from stops import (
    MAX_DWELL_SPEED_MPS,
    MIN_DWELL_S,
    detect_dwells,
    dwells_to_gdf,
    segment_speeds,
)
from synthetic import synthetic_trace

SAMPLE_S = 1.0


def true_position_at(trace, elapsed_s: float) -> np.ndarray:
    """Ground-truth position at `elapsed_s` into the trace."""
    return trace.true_xy()[int(round(elapsed_s / SAMPLE_S))]


class TestSegmentSpeeds:
    def test_matches_the_generated_travel_speed(self):
        trace = synthetic_trace(duration_s=60.0, speed_mps=10.0, noise_sigma_m=0.0, seed=30)

        np.testing.assert_allclose(segment_speeds(trace.noisy), 10.0, atol=1e-6)

    def test_is_zero_across_an_injected_dwell(self):
        trace = synthetic_trace(
            duration_s=120.0, noise_sigma_m=0.0, dwells=((40.0, 30.0),), seed=31
        )

        speeds = segment_speeds(trace.noisy)

        # Segments 40..69 span the stationary window.
        np.testing.assert_allclose(speeds[40:70], 0.0, atol=1e-6)

    def test_is_one_shorter_than_the_trace(self):
        trace = synthetic_trace(duration_s=30.0, seed=32)

        assert len(segment_speeds(trace.noisy)) == len(trace.noisy) - 1

    def test_zero_length_intervals_are_not_stationary(self):
        trace = synthetic_trace(duration_s=20.0, noise_sigma_m=0.0, seed=33)
        noisy = trace.noisy.copy()
        noisy.loc[5, "recorded_at"] = noisy.loc[4, "recorded_at"]

        # A duplicate timestamp carries no speed information; reporting 0 would
        # invent a stop.
        assert np.isinf(segment_speeds(noisy)[4])

    def test_short_trace_has_no_segments(self):
        trace = synthetic_trace(duration_s=0.0, seed=34)

        assert len(segment_speeds(trace.noisy)) == 0


class TestDetectDwells:
    def test_finds_the_injected_dwells_with_the_right_durations(self):
        trace = synthetic_trace(
            duration_s=400.0,
            noise_sigma_m=0.0,
            dwells=((60.0, 45.0), (200.0, 30.0)),
            seed=35,
        )

        dwells = detect_dwells(trace.noisy)

        assert len(dwells) == 2
        assert [d.duration_s for d in dwells] == pytest.approx([45.0, 30.0], abs=SAMPLE_S)

    def test_reports_start_times_that_match_the_injected_windows(self):
        trace = synthetic_trace(
            duration_s=300.0, noise_sigma_m=0.0, dwells=((90.0, 40.0),), seed=36
        )

        (dwell,) = detect_dwells(trace.noisy)

        expected = pd.to_datetime(trace.noisy["recorded_at"].iloc[0]) + pd.Timedelta(seconds=90.0)
        assert abs((dwell.start_time - expected).total_seconds()) <= SAMPLE_S
        assert abs((dwell.end_time - expected).total_seconds() - 40.0) <= SAMPLE_S

    def test_centroid_sits_where_the_vehicle_actually_stood(self):
        trace = synthetic_trace(
            duration_s=300.0, noise_sigma_m=0.0, dwells=((100.0, 35.0),), seed=37
        )

        (dwell,) = detect_dwells(trace.noisy)

        stood_at = true_position_at(trace, 100.0)
        assert np.hypot(dwell.centroid.x - stood_at[0], dwell.centroid.y - stood_at[1]) < 1.0

    def test_ignores_a_pause_shorter_than_the_minimum(self):
        trace = synthetic_trace(
            duration_s=200.0,
            noise_sigma_m=0.0,
            dwells=((50.0, 12.0), (120.0, 30.0)),
            seed=38,
        )

        dwells = detect_dwells(trace.noisy)

        # A 12 s pause at a robot is traffic, not a stop.
        assert len(dwells) == 1
        assert dwells[0].duration_s == pytest.approx(30.0, abs=SAMPLE_S)

    def test_boundary_duration_counts(self):
        trace = synthetic_trace(
            duration_s=200.0, noise_sigma_m=0.0, dwells=((50.0, MIN_DWELL_S),), seed=39
        )

        assert len(detect_dwells(trace.noisy)) == 1

    def test_a_slow_crawl_under_the_speed_threshold_is_a_dwell(self):
        # 0.5 m/s for the whole trace: shuffling forward in a rank queue.
        trace = synthetic_trace(
            duration_s=60.0,
            speed_mps=0.5,
            noise_sigma_m=0.0,
            waypoints=((0.0, 0.0), (200.0, 0.0)),
            seed=40,
        )

        (dwell,) = detect_dwells(trace.noisy)

        assert dwell.duration_s == pytest.approx(60.0, abs=SAMPLE_S)

    def test_movement_above_the_speed_threshold_is_not_a_dwell(self):
        trace = synthetic_trace(
            duration_s=60.0,
            speed_mps=2.0,
            noise_sigma_m=0.0,
            waypoints=((0.0, 0.0), (500.0, 0.0)),
            seed=41,
        )

        assert detect_dwells(trace.noisy) == []

    def test_a_moving_segment_splits_one_dwell_into_two(self):
        """Maximal runs: pulling forward 20 m mid-stop ends the dwell."""
        trace = synthetic_trace(
            duration_s=200.0,
            noise_sigma_m=0.0,
            dwells=((40.0, 30.0), (72.0, 30.0)),  # 2 s of travel between them
            seed=42,
        )

        dwells = detect_dwells(trace.noisy)

        assert len(dwells) == 2
        assert all(d.duration_s == pytest.approx(30.0, abs=SAMPLE_S) for d in dwells)

    def test_thresholds_are_configurable(self):
        trace = synthetic_trace(
            duration_s=200.0, noise_sigma_m=0.0, dwells=((50.0, 12.0),), seed=43
        )

        assert detect_dwells(trace.noisy) == []
        assert len(detect_dwells(trace.noisy, min_duration_s=10.0)) == 1

    def test_indices_bracket_the_dwell(self):
        trace = synthetic_trace(
            duration_s=200.0, noise_sigma_m=0.0, dwells=((60.0, 40.0),), seed=44
        )

        (dwell,) = detect_dwells(trace.noisy)

        assert dwell.start_index == 60
        assert dwell.end_index == 100
        assert dwell.point_count == 41

    def test_a_trace_that_never_stops_has_no_dwells(self):
        trace = synthetic_trace(duration_s=300.0, noise_sigma_m=0.0, seed=45)

        assert detect_dwells(trace.noisy) == []

    def test_short_traces_return_nothing(self):
        trace = synthetic_trace(duration_s=0.0, seed=46)

        assert detect_dwells(trace.noisy) == []
        assert detect_dwells(trace.noisy.iloc[0:0]) == []


class TestDwellsToGdf:
    def test_carries_centroids_and_attributes(self, two_dwell_run):
        dwells = detect_dwells(clean_trace(two_dwell_run.noisy))

        frame = dwells_to_gdf(dwells)

        assert len(frame) == len(dwells)
        assert str(frame.crs).upper() == "EPSG:32736"
        assert {"start_time", "duration_s", "point_count"} <= set(frame.columns)
        assert frame.geometry.iloc[0].equals(dwells[0].centroid)

    def test_empty_input_gives_an_empty_frame(self):
        assert len(dwells_to_gdf([])) == 0


class TestOnCleanedNoisyTraces:
    """Detection on realistic input: noisy points put through `clean_trace`."""

    def test_raw_noise_alone_would_hide_the_dwells(self, two_dwell_run):
        # 4 m of per-axis noise makes a parked kombi look like it is doing
        # ~8 m/s between consecutive fixes. This is the reason `clean.py`
        # exists, and it is worth pinning down.
        raw_speeds = segment_speeds(two_dwell_run.noisy)
        stationary = raw_speeds[60:105]  # inside the first injected dwell
        assert np.median(stationary) > MAX_DWELL_SPEED_MPS

        assert detect_dwells(two_dwell_run.noisy) == []

    def test_cleaning_recovers_both_dwells(self, two_dwell_run):
        dwells = detect_dwells(clean_trace(two_dwell_run.noisy))

        assert len(dwells) == 2
        assert [d.duration_s for d in dwells] == pytest.approx([45.0, 30.0], abs=5.0)

    def test_centroids_land_near_where_the_kombi_stood(self, two_dwell_run):
        dwells = detect_dwells(clean_trace(two_dwell_run.noisy))

        for dwell, start_s in zip(dwells, (60.0, 200.0), strict=True):
            stood_at = true_position_at(two_dwell_run, start_s)
            offset = np.hypot(dwell.centroid.x - stood_at[0], dwell.centroid.y - stood_at[1])
            # Comfortably inside a stop-clustering radius, from 4 m noise.
            assert offset < 10.0

    def test_survives_outliers_in_the_same_trace(self):
        trace = synthetic_trace(
            duration_s=400.0,
            noise_sigma_m=4.0,
            dwells=((60.0, 45.0), (200.0, 30.0)),
            bad_accuracy_indices=(150,),
            jump_indices=(300,),
            seed=47,
        )

        dwells = detect_dwells(clean_trace(trace.noisy))

        assert len(dwells) == 2

    def test_thresholds_are_the_documented_ones(self):
        assert MIN_DWELL_S == 20.0
        assert MAX_DWELL_SPEED_MPS == 1.0
