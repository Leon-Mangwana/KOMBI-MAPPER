"""Discrete Fréchet distance between two polylines, in projected metres.

The usual picture: a person walks the first polyline and a dog the second, each
moving forwards or standing still but never going back. The Fréchet distance is
the shortest leash that lets both reach the end. It is the right similarity
measure for kombi traces because it respects *order* — two traces that cover the
same roads in opposite directions have a Hausdorff distance of nearly zero and a
Fréchet distance of the whole route length, and they are not the same route.

This is the **discrete** variant of Eiter & Mannila (1994): couplings pair
vertices with vertices rather than points anywhere along the segments. It upper-
bounds the continuous distance and is exact in the limit of dense sampling.
That caveat is not academic here — see `discrete_frechet` — and it is why
callers should simplify both inputs to a comparable vertex spacing first.

Implemented directly on NumPy arrays; no library. Coordinates are assumed to be
**EPSG:32736** metres, like everything else the pipeline passes around. Feeding
this degrees would silently return a number in degrees. See ../CLAUDE.md.
"""

from __future__ import annotations

import numpy as np

__all__ = ["coupling_matrix", "discrete_frechet"]


def discrete_frechet(p: np.ndarray, q: np.ndarray, *, max_distance: float | None = None) -> float:
    """Discrete Fréchet distance between two sequences of points, in metres.

    The recursion is the standard one over the coupling matrix `ca`, where
    `ca[i][j]` is the shortest leash that gets the walkers to vertices `i` and
    `j` respectively:

        ca[0][0]  = d(p₀, q₀)
        ca[i][0]  = max(d(pᵢ, q₀), ca[i-1][0])
        ca[0][j]  = max(d(p₀, qⱼ), ca[0][j-1])
        ca[i][j]  = max(d(pᵢ, qⱼ), min(ca[i-1][j], ca[i-1][j-1], ca[i][j-1]))

    and the answer is `ca[n-1][m-1]`. The three terms of the inner `min` are the
    three legal previous steps: the person advances, both advance, the dog
    advances. Written recursively this needs memoising or it re-explores an
    exponential number of couplings; the memo table is filled bottom-up instead,
    which computes exactly the same entries in the same order of dependency
    without a recursion depth of `n + m`. Only two rows are ever live, so the
    memory is O(m) rather than O(nm) — use `coupling_matrix` when the whole
    table is wanted.

    **Sampling sensitivity.** Because only vertices may be paired, the result
    depends on where the vertices are. A straight 10 m segment `[(0,0),(10,0)]`
    against `[(0,0),(5,1),(10,0)]` scores 5.1 m, not the 1 m a continuous
    Fréchet distance would give, because the middle vertex of the second curve
    has nothing near it to pair with. Two traces of the same road at different
    sampling densities therefore read as further apart than they are, which is
    exactly what `cluster.simplify_traces` exists to prevent.

    Args:
        p: First polyline, `(n, 2)` of projected metres.
        q: Second polyline, `(m, 2)` of projected metres.
        max_distance: Abandon the fill once the distance is known to exceed
            this. See below — the return value changes meaning when it trips.

    Returns:
        The distance in metres. When `max_distance` is set and the computation
        was abandoned, a **lower bound** on the true distance that is strictly
        greater than `max_distance`; its magnitude is otherwise meaningless. A
        caller that only asks "are these two within `max_distance`?" — which is
        all DBSCAN asks — can use the number as-is; a caller that wants the
        distance itself must leave `max_distance` unset.

    Raises:
        ValueError: Either input is empty, is not `(k, 2)`, or holds a
            non-finite coordinate.
    """
    px, py = _as_xy(p, "p")
    qx, qy = _as_xy(q, "q")

    # Every coupling starts at (0, 0) and ends at (n-1, m-1), so both walkers
    # are at those pairs at some point and the leash is at least that long. Free
    # to check, and across a batch of traces it rejects most pairs outright.
    endpoints = max(
        float(np.hypot(px[0] - qx[0], py[0] - qy[0])),
        float(np.hypot(px[-1] - qx[-1], py[-1] - qy[-1])),
    )
    if max_distance is not None and endpoints > max_distance:
        return endpoints

    # Row 0 is a running maximum: the person is stuck at p₀ while the dog walks.
    previous: list[float] = np.maximum.accumulate(np.hypot(qx - px[0], qy - py[0])).tolist()
    if max_distance is not None and min(previous) > max_distance:
        return min(previous)

    width = len(previous)
    for i in range(1, len(px)):
        row: list[float] = np.hypot(qx - px[i], qy - py[i]).tolist()

        running = row[0] if row[0] > previous[0] else previous[0]  # ca[i][0]
        smallest = running
        current = [running]

        for j in range(1, width):
            # min(ca[i-1][j], ca[i-1][j-1], ca[i][j-1]) — spelled out rather
            # than min(...) because this loop runs O(nm) times per pair and is
            # the whole cost of the clustering stage.
            best = previous[j] if previous[j] < previous[j - 1] else previous[j - 1]
            if running < best:
                best = running
            running = row[j] if row[j] > best else best
            current.append(running)
            if running < smallest:
                smallest = running

        previous = current

        # Every coupling is monotone from (0, 0) to (n-1, m-1), so it visits at
        # least one cell of every row; the smallest entry in a row is therefore
        # a lower bound on the final answer.
        if max_distance is not None and smallest > max_distance:
            return smallest

    return previous[-1]


def coupling_matrix(p: np.ndarray, q: np.ndarray) -> np.ndarray:
    """The full `(n, m)` coupling matrix behind `discrete_frechet`.

    `ca[i][j]` is the shortest leash that gets the walkers to `p[i]` and `q[j]`,
    so `ca[-1][-1]` is the Fréchet distance and the rest of the table shows
    where the leash was forced. Kept for tests and for diagnosing a pair of
    traces that clustered surprisingly; `discrete_frechet` keeps two rows and
    should be preferred for anything at scale.

    Raises:
        ValueError: As `discrete_frechet`.
    """
    px, py = _as_xy(p, "p")
    qx, qy = _as_xy(q, "q")

    distances = np.hypot(px[:, None] - qx[None, :], py[:, None] - qy[None, :])
    accumulated = np.empty_like(distances)

    accumulated[0, :] = np.maximum.accumulate(distances[0, :])
    for i in range(1, len(px)):
        accumulated[i, 0] = max(distances[i, 0], accumulated[i - 1, 0])
        for j in range(1, len(qx)):
            accumulated[i, j] = max(
                distances[i, j],
                min(
                    accumulated[i - 1, j],
                    accumulated[i - 1, j - 1],
                    accumulated[i, j - 1],
                ),
            )

    return accumulated


def _as_xy(points: np.ndarray, label: str) -> tuple[np.ndarray, np.ndarray]:
    """Validate an `(n, 2)` array of metres and split it into x and y columns.

    Splitting up front lets the hot loop work on two contiguous 1-D arrays
    instead of striding a 2-D one.
    """
    array = np.asarray(points, dtype=float)
    if array.ndim != 2 or array.shape[1] != 2:
        raise ValueError(f"{label} must have shape (n, 2); got {array.shape}.")
    if len(array) == 0:
        raise ValueError(f"{label} is empty; a polyline needs at least one point.")
    if not np.isfinite(array).all():
        raise ValueError(f"{label} contains a non-finite coordinate.")

    return np.ascontiguousarray(array[:, 0]), np.ascontiguousarray(array[:, 1])
