"""Tests for the discrete Fréchet distance.

Two kinds of assertion here. Most cases are small enough that the coupling
matrix can be worked through by hand, and the expected value is written out
above the test with the arithmetic that produced it. The rest are checked
against `brute_force_frechet`, which enumerates every monotone coupling and
takes the shortest of the longest leashes — the definition itself, at
exponential cost, and therefore only usable on tiny inputs.
"""

import math

import numpy as np
import pytest

from frechet import coupling_matrix, discrete_frechet

ROOT_2 = math.sqrt(2.0)
ROOT_5 = math.sqrt(5.0)


def brute_force_frechet(p, q) -> float:
    """Minimum over every monotone coupling of the longest leash it needs.

    The definition, evaluated directly: walk both polylines forwards, at each
    step advancing the first index, the second, or both, and record the largest
    pairing distance the walk ever required.
    """
    p = [tuple(point) for point in p]
    q = [tuple(point) for point in q]
    last_p, last_q = len(p) - 1, len(q) - 1
    best = math.inf

    def walk(i: int, j: int, longest: float) -> None:
        nonlocal best
        longest = max(longest, math.dist(p[i], q[j]))
        if longest >= best:
            return  # cannot improve on a coupling we have already found
        if i == last_p and j == last_q:
            best = longest
            return
        if i < last_p:
            walk(i + 1, j, longest)
        if j < last_q:
            walk(i, j + 1, longest)
        if i < last_p and j < last_q:
            walk(i + 1, j + 1, longest)

    walk(0, 0, 0.0)
    return best


class TestHandComputedCases:
    def test_identical_polylines_are_zero_apart(self):
        line = np.array([[0.0, 0.0], [10.0, 0.0], [10.0, 10.0]])

        assert discrete_frechet(line, line) == 0.0

    def test_single_points_give_their_separation(self):
        assert discrete_frechet(np.array([[0.0, 0.0]]), np.array([[3.0, 4.0]])) == 5.0

    def test_parallel_offset_lines_give_the_offset(self):
        # Vertex k of each line pairs with vertex k, 5 m away; no coupling does
        # better and the diagonal one achieves it.
        a = np.array([[0.0, 0.0], [10.0, 0.0], [20.0, 0.0]])
        b = np.array([[0.0, 5.0], [10.0, 5.0], [20.0, 5.0]])

        assert discrete_frechet(a, b) == pytest.approx(5.0)

    def test_uneven_vertex_counts_stretch_the_leash(self):
        """The worked example in the module docstring's recursion.

        p = (0,0), (1,0), (2,0)      q = (0,1), (2,1)

        d    = [[1,     √5   ],
                [√2,    √2   ],
                [√5,    1    ]]
        ca   = [[1,     √5   ],
                [√2,    √2   ],
                [√5,    √2   ]]

        The answer is √2 ≈ 1.414, not the 1.0 that the closest pairing of each
        vertex would suggest: q has only two vertices, so somebody has to hold
        still while the other crosses the middle.
        """
        p = np.array([[0.0, 0.0], [1.0, 0.0], [2.0, 0.0]])
        q = np.array([[0.0, 1.0], [2.0, 1.0]])

        assert discrete_frechet(p, q) == pytest.approx(ROOT_2)

    def test_a_detour_vertex_with_no_partner_sets_the_distance(self):
        """Sampling sensitivity, pinned down — see `discrete_frechet`.

        p = (0,0), (10,0)            q = (0,0), (5,1), (10,0)

        The middle vertex of q can only pair with (0,0) or (10,0), both √26 away,
        so ca[1][1] = √26 ≈ 5.099 even though the two curves are never more than
        1 m apart as *curves*. A continuous Fréchet distance would be 1.0.
        """
        p = np.array([[0.0, 0.0], [10.0, 0.0]])
        q = np.array([[0.0, 0.0], [5.0, 1.0], [10.0, 0.0]])

        assert discrete_frechet(p, q) == pytest.approx(math.sqrt(26.0))

    def test_reversed_polylines_are_far_apart(self):
        """Ordering is the whole reason for using Fréchet over Hausdorff.

        The two point sets are identical, so the Hausdorff distance is 0. But a
        coupling must start by pairing the first vertices — (0,0) with (20,0),
        20 m apart — so the Fréchet distance is 20.
        """
        forward = np.array([[0.0, 0.0], [10.0, 0.0], [20.0, 0.0]])
        backward = forward[::-1]

        assert discrete_frechet(forward, backward) == pytest.approx(20.0)

    def test_standing_still_is_allowed(self):
        # p never moves, so its walker waits at the origin while q's runs to the
        # far end: the leash reaches 2 and no coupling avoids it.
        p = np.array([[0.0, 0.0], [0.0, 0.0], [0.0, 0.0]])
        q = np.array([[0.0, 0.0], [1.0, 0.0], [2.0, 0.0]])

        assert discrete_frechet(p, q) == pytest.approx(2.0)

    def test_the_endpoints_always_have_to_be_paired(self):
        # Two lines that coincide everywhere except that one runs 30 m further:
        # the final vertices must pair, so the distance is that overshoot.
        short = np.array([[0.0, 0.0], [100.0, 0.0]])
        long = np.array([[0.0, 0.0], [100.0, 0.0], [130.0, 0.0]])

        assert discrete_frechet(short, long) == pytest.approx(30.0)

    def test_a_perpendicular_spike_is_measured_from_its_tip(self):
        # ca[1][1] pairs the spike tip (1,3) with (1,0) — 3 m — and nothing in
        # the rest of the table exceeds it.
        straight = np.array([[0.0, 0.0], [1.0, 0.0], [2.0, 0.0]])
        spiked = np.array([[0.0, 0.0], [1.0, 3.0], [2.0, 0.0]])

        assert discrete_frechet(straight, spiked) == pytest.approx(3.0)

    def test_l_shape_against_its_diagonal_shortcut(self):
        """p = (0,0), (0,10), (10,10)   q = (0,0), (10,10)

        d    = [[0,      √200 ],
                [10,     10   ],
                [√200,   0    ]]
        ca   = [[0,      √200 ],
                [10,     10   ],
                [√200,   10   ]]

        10: the corner has to pair with one end of the diagonal or the other.
        """
        p = np.array([[0.0, 0.0], [0.0, 10.0], [10.0, 10.0]])
        q = np.array([[0.0, 0.0], [10.0, 10.0]])

        assert discrete_frechet(p, q) == pytest.approx(10.0)

    def test_asymmetric_sampling_of_the_same_line(self):
        # Same geometry, different vertices. Every vertex of the dense line has
        # a partner within 2.5 m on the sparse one — the worst case being the
        # midpoint of each 5 m gap.
        sparse = np.array([[0.0, 0.0], [5.0, 0.0], [10.0, 0.0]])
        dense = np.array([[float(x), 0.0] for x in range(11)])

        assert discrete_frechet(sparse, dense) == pytest.approx(2.0)


class TestAgainstBruteForce:
    """The DP against an exhaustive enumeration of couplings."""

    @pytest.mark.parametrize("seed", range(12))
    def test_matches_on_random_small_polylines(self, seed):
        rng = np.random.default_rng(seed)
        p = rng.uniform(-50.0, 50.0, size=(rng.integers(1, 7), 2))
        q = rng.uniform(-50.0, 50.0, size=(rng.integers(1, 7), 2))

        assert discrete_frechet(p, q) == pytest.approx(brute_force_frechet(p, q))

    @pytest.mark.parametrize("seed", range(6))
    def test_matches_on_near_identical_traces(self, seed):
        """Random points are an easy case; two jittered copies of one path are
        the case that actually arises, and they exercise the diagonal steps."""
        rng = np.random.default_rng(1000 + seed)
        base = np.column_stack([np.arange(6.0) * 20.0, np.zeros(6)])
        p = base + rng.normal(0.0, 3.0, size=base.shape)
        q = base + rng.normal(0.0, 3.0, size=base.shape)

        assert discrete_frechet(p, q) == pytest.approx(brute_force_frechet(p, q))


class TestProperties:
    def test_is_symmetric(self):
        rng = np.random.default_rng(7)
        p = rng.uniform(0.0, 100.0, size=(9, 2))
        q = rng.uniform(0.0, 100.0, size=(13, 2))

        assert discrete_frechet(p, q) == pytest.approx(discrete_frechet(q, p))

    def test_is_translation_invariant(self):
        rng = np.random.default_rng(8)
        p = rng.uniform(0.0, 100.0, size=(10, 2))
        q = rng.uniform(0.0, 100.0, size=(10, 2))
        shift = np.array([296_000.0, 8_026_000.0])

        # Traces arrive as UTM 36S eastings and northings, i.e. six- and
        # seven-figure numbers; the answer must not depend on that offset.
        assert discrete_frechet(p + shift, q + shift) == pytest.approx(discrete_frechet(p, q))

    def test_scales_with_the_coordinates(self):
        rng = np.random.default_rng(9)
        p = rng.uniform(0.0, 100.0, size=(8, 2))
        q = rng.uniform(0.0, 100.0, size=(8, 2))

        assert discrete_frechet(p * 3.0, q * 3.0) == pytest.approx(3.0 * discrete_frechet(p, q))

    def test_is_never_below_the_endpoint_separation(self):
        rng = np.random.default_rng(10)
        for _ in range(20):
            p = rng.uniform(0.0, 100.0, size=(rng.integers(2, 12), 2))
            q = rng.uniform(0.0, 100.0, size=(rng.integers(2, 12), 2))

            floor = max(
                math.dist(p[0], q[0]),
                math.dist(p[-1], q[-1]),
            )
            assert discrete_frechet(p, q) >= floor - 1e-9

    def test_bounds_the_hausdorff_distance(self):
        """A Fréchet coupling is one particular matching, so it can never beat
        the best-matching-per-point that Hausdorff is free to choose."""
        rng = np.random.default_rng(11)
        p = rng.uniform(0.0, 100.0, size=(10, 2))
        q = rng.uniform(0.0, 100.0, size=(10, 2))

        pairwise = np.hypot(p[:, None, 0] - q[None, :, 0], p[:, None, 1] - q[None, :, 1])
        hausdorff = max(pairwise.min(axis=1).max(), pairwise.min(axis=0).max())

        assert discrete_frechet(p, q) >= hausdorff - 1e-9


class TestEarlyExit:
    def test_does_not_change_the_answer_when_the_pair_is_within_the_cutoff(self):
        rng = np.random.default_rng(12)
        base = np.column_stack([np.arange(40.0) * 25.0, np.zeros(40)])
        p = base + rng.normal(0.0, 2.0, size=base.shape)
        q = base + rng.normal(0.0, 2.0, size=base.shape)

        exact = discrete_frechet(p, q)
        assert exact < 20.0
        assert discrete_frechet(p, q, max_distance=20.0) == pytest.approx(exact)

    def test_boundary_equality_is_not_pruned(self):
        # d == max_distance must survive: DBSCAN counts neighbours at exactly
        # eps, so a pair pruned here would be a cluster member lost.
        p = np.array([[0.0, 0.0], [10.0, 0.0]])
        q = np.array([[0.0, 5.0], [10.0, 5.0]])

        assert discrete_frechet(p, q, max_distance=5.0) == pytest.approx(5.0)

    def test_returns_a_lower_bound_above_the_cutoff_when_it_trips(self):
        rng = np.random.default_rng(13)
        p = rng.uniform(0.0, 100.0, size=(30, 2))
        q = rng.uniform(5_000.0, 5_100.0, size=(30, 2))

        exact = discrete_frechet(p, q)
        pruned = discrete_frechet(p, q, max_distance=100.0)

        assert pruned > 100.0
        assert pruned <= exact + 1e-9

    def test_the_endpoint_bound_alone_can_trip_it(self):
        # Identical middles, wildly different starts: rejected before any row is
        # filled, which is what makes the cutoff cheap across a batch.
        p = np.array([[0.0, 0.0], [100.0, 0.0], [200.0, 0.0]])
        q = np.array([[900.0, 0.0], [100.0, 0.0], [200.0, 0.0]])

        assert discrete_frechet(p, q, max_distance=50.0) > 50.0

    @pytest.mark.parametrize("seed", range(8))
    def test_pruned_results_never_misreport_membership(self, seed):
        """The only guarantee the cutoff has to keep: `<= max_distance` decides
        the same way with and without it."""
        rng = np.random.default_rng(2000 + seed)
        cutoff = 40.0
        p = rng.uniform(0.0, 200.0, size=(7, 2))
        q = rng.uniform(0.0, 200.0, size=(7, 2))

        exact = discrete_frechet(p, q)
        pruned = discrete_frechet(p, q, max_distance=cutoff)

        assert (exact <= cutoff) == (pruned <= cutoff)


class TestCouplingMatrix:
    def test_last_cell_is_the_distance(self):
        rng = np.random.default_rng(14)
        p = rng.uniform(0.0, 100.0, size=(11, 2))
        q = rng.uniform(0.0, 100.0, size=(7, 2))

        assert coupling_matrix(p, q)[-1, -1] == pytest.approx(discrete_frechet(p, q))

    def test_matches_the_hand_computed_table(self):
        p = np.array([[0.0, 0.0], [1.0, 0.0], [2.0, 0.0]])
        q = np.array([[0.0, 1.0], [2.0, 1.0]])

        expected = np.array(
            [
                [1.0, ROOT_5],
                [ROOT_2, ROOT_2],
                [ROOT_5, ROOT_2],
            ]
        )
        np.testing.assert_allclose(coupling_matrix(p, q), expected)

    def test_first_row_and_column_are_running_maxima(self):
        rng = np.random.default_rng(15)
        p = rng.uniform(0.0, 100.0, size=(6, 2))
        q = rng.uniform(0.0, 100.0, size=(9, 2))

        accumulated = coupling_matrix(p, q)

        # One walker is pinned at its first vertex, so the leash only lengthens.
        assert np.all(np.diff(accumulated[0, :]) >= -1e-9)
        assert np.all(np.diff(accumulated[:, 0]) >= -1e-9)

    def test_shape_follows_the_inputs(self):
        p = np.zeros((4, 2))
        q = np.ones((6, 2))

        assert coupling_matrix(p, q).shape == (4, 6)


class TestValidation:
    def test_rejects_an_empty_polyline(self):
        with pytest.raises(ValueError, match="empty"):
            discrete_frechet(np.empty((0, 2)), np.array([[0.0, 0.0]]))

    def test_rejects_the_wrong_number_of_columns(self):
        with pytest.raises(ValueError, match=r"shape \(n, 2\)"):
            discrete_frechet(np.zeros((3, 3)), np.zeros((3, 2)))

    def test_rejects_a_one_dimensional_array(self):
        with pytest.raises(ValueError, match=r"shape \(n, 2\)"):
            discrete_frechet(np.zeros(6), np.zeros((3, 2)))

    def test_rejects_non_finite_coordinates(self):
        p = np.array([[0.0, 0.0], [np.nan, 1.0]])

        with pytest.raises(ValueError, match="non-finite"):
            discrete_frechet(p, np.zeros((2, 2)))

    def test_names_the_offending_argument(self):
        with pytest.raises(ValueError, match="^q "):
            discrete_frechet(np.zeros((3, 2)), np.zeros((3, 5)))

    def test_accepts_a_plain_nested_sequence(self):
        assert discrete_frechet([[0.0, 0.0]], [[3.0, 4.0]]) == 5.0
