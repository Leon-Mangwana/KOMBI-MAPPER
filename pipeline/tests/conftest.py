"""Shared helpers for the pipeline tests.

The trace generator itself lives in `synthetic.py` rather than here, because
`plot_trace.py --demo` uses it too — a fixture that only tests can reach would
mean two generators drifting apart.
"""

import numpy as np
import pytest

from synthetic import synthetic_trace


def rmse_against_truth(gdf, true_xy: np.ndarray) -> float:
    """Root-mean-square 2D distance between a frame and the truth positions.

    `gdf` and `true_xy` must be row-aligned, so this is only valid for frames
    where no points were dropped.
    """
    xy = np.column_stack(
        [gdf.geometry.x.to_numpy(dtype=float), gdf.geometry.y.to_numpy(dtype=float)]
    )
    assert len(xy) == len(true_xy), "frame and truth must be row-aligned"
    return float(np.sqrt(np.mean(np.sum((xy - true_xy) ** 2, axis=1))))


@pytest.fixture()
def clean_run():
    """A 300 s noisy trace with no dwells and no outliers."""
    return synthetic_trace(duration_s=300.0, noise_sigma_m=4.0, seed=1)


@pytest.fixture()
def two_dwell_run():
    """A 400 s trace that stands still for 45 s and then for 30 s.

    Both windows start and end on a sample boundary at 1 Hz, so the injected
    durations are exactly recoverable.
    """
    return synthetic_trace(
        duration_s=400.0,
        noise_sigma_m=4.0,
        dwells=((60.0, 45.0), (200.0, 30.0)),
        seed=2,
    )
