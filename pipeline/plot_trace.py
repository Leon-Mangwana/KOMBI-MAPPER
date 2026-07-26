"""Plot a trace raw vs cleaned, for visual inspection.

Numbers in a test tell you the filter met its tolerance; they do not tell you it
cut a corner, lagged a turn, or invented a stop. This renders three views of the
same trace so those failures are visible:

- the whole route in plan view, raw against cleaned;
- a zoom on one dwell, where the difference is actually legible at 4 m of noise;
- segment speed over time against the dwell threshold, which is precisely what
  `stops.detect_dwells` reads.

Run it against a real trace, or with `--demo` against a synthetic one whose
ground truth is known and drawn alongside:

    python plot_trace.py --demo
    python plot_trace.py --trace-id 42 --out trace-42.png

Needs the `dev` extra (`pip install -e ".[dev]"`) — matplotlib is not a pipeline
dependency.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import matplotlib

matplotlib.use("Agg")

import matplotlib.patheffects as path_effects  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
from matplotlib.gridspec import GridSpec  # noqa: E402
from matplotlib.ticker import FuncFormatter  # noqa: E402

from clean import clean_trace, load_trace  # noqa: E402
from stops import MAX_DWELL_SPEED_MPS, detect_dwells, segment_speeds  # noqa: E402
from synthetic import synthetic_trace  # noqa: E402

# Light chart surface, ink and categorical slots 1–3, plus the reserved
# "critical" status step for rejected fixes. Colour is assigned by entity and
# held across all three panels: raw is always orange, cleaned always blue.
SURFACE = "#fcfcfb"
INK = "#0b0b0b"
INK_SECONDARY = "#52514e"
INK_MUTED = "#898781"
GRIDLINE = "#e1e0d9"
AXIS = "#c3c2b7"

CLEANED = "#2a78d6"  # slot 1
RAW = "#eb6834"  # slot 2
DWELL = "#1baf7a"  # slot 3
DROPPED = "#d03b3b"  # status: critical
TRUTH = INK_MUTED  # a reference, not a series


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--trace-id", type=int, help="Load this trace from the database.")
    source.add_argument(
        "--demo",
        action="store_true",
        help="Use a synthetic trace with known noise, dwells and outliers.",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=Path("trace.png"),
        help="Where to write the PNG (default: trace.png).",
    )
    parser.add_argument("--dpi", type=int, default=150)
    args = parser.parse_args()

    if args.demo:
        # Long enough to round the corner of the default L-shaped route, so the
        # plan view shows a route rather than a straight line.
        generated = synthetic_trace(
            duration_s=600.0,
            noise_sigma_m=4.0,
            dwells=((60.0, 45.0), (200.0, 30.0), (430.0, 40.0)),
            bad_accuracy_indices=(150,),
            jump_indices=(300,),
            seed=2,
        )
        raw, truth, title = generated.noisy, generated.truth, "Synthetic trace (--demo)"
    else:
        raw, truth = load_trace(args.trace_id), None
        title = f"Trace {args.trace_id}"

    cleaned = clean_trace(raw)
    dwells = detect_dwells(cleaned)
    dropped = raw.loc[~raw["seq"].isin(set(cleaned["seq"]))]

    figure = render(raw, cleaned, dwells, dropped, truth, title)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    figure.savefig(args.out, dpi=args.dpi, facecolor=SURFACE)
    print(
        f"{args.out}: {len(raw)} points, {len(dropped)} dropped, {len(dwells)} dwells "
        f"({', '.join(f'{d.duration_s:.0f}s' for d in dwells) or 'none'})"
    )


def render(raw, cleaned, dwells, dropped, truth, title):
    """Lay out the three panels. Returns the figure."""
    plt.rcParams.update(
        {
            # matplotlib resolves families against installed fonts, so name real
            # ones rather than the CSS `system-ui` keyword.
            "font.family": ["Segoe UI", "Helvetica Neue", "DejaVu Sans", "sans-serif"],
            "font.size": 9,
            "text.color": INK,
            "axes.labelcolor": INK_SECONDARY,
            "xtick.color": INK_MUTED,
            "ytick.color": INK_MUTED,
        }
    )

    figure = plt.figure(figsize=(14, 7.5), facecolor=SURFACE)
    # Explicit margins rather than tight_layout: the map panels are equal-aspect,
    # which tight_layout cannot solve and warns about.
    grid = GridSpec(
        2,
        2,
        figure=figure,
        width_ratios=[1.15, 1],
        left=0.055,
        right=0.985,
        top=0.885,
        bottom=0.08,
        hspace=0.30,
        wspace=0.14,
    )
    plan = figure.add_subplot(grid[:, 0])
    zoom = figure.add_subplot(grid[0, 1])
    speed = figure.add_subplot(grid[1, 1])

    for axes in (plan, zoom, speed):
        style_axes(axes)

    draw_plan(plan, raw, cleaned, dwells, dropped, truth)
    draw_zoom(zoom, raw, cleaned, dwells, truth)
    draw_speed(speed, raw, cleaned, dwells)

    figure.suptitle(title, x=0.012, y=0.972, ha="left", fontsize=15, fontweight="600", color=INK)
    figure.text(
        0.012,
        0.932,
        f"{len(raw)} raw points · {len(dropped)} rejected · {len(dwells)} dwells detected"
        + (" · grey dashed line is ground truth" if truth is not None else ""),
        ha="left",
        fontsize=9.5,
        color=INK_SECONDARY,
    )
    return figure


def style_axes(axes) -> None:
    axes.set_facecolor(SURFACE)
    axes.grid(True, color=GRIDLINE, linewidth=0.8)
    axes.set_axisbelow(True)
    for side in ("top", "right"):
        axes.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        axes.spines[side].set_color(AXIS)
        axes.spines[side].set_linewidth(1.0)


def style_metric_ticks(axes) -> None:
    """Plain thousands-separated metres.

    matplotlib's default offset notation turns a UTM northing into `+8.026e6`
    with the real digits hidden in a corner annotation, which is unreadable on a
    coordinate axis.
    """
    # A FuncFormatter renders the absolute value, so it replaces the offset
    # behaviour outright — `ticklabel_format` only works on a ScalarFormatter
    # and would raise here.
    metres = FuncFormatter(lambda value, _position: f"{value:,.0f}")
    for axis in (axes.xaxis, axes.yaxis):
        axis.set_major_formatter(metres)


def xy(frame) -> tuple[np.ndarray, np.ndarray]:
    return frame.geometry.x.to_numpy(), frame.geometry.y.to_numpy()


def draw_plan(axes, raw, cleaned, dwells, dropped, truth) -> None:
    """The whole route, raw against cleaned."""
    if truth is not None:
        axes.plot(*xy(truth), color=TRUTH, linewidth=1.4, linestyle="--", label="ground truth")

    raw_x, raw_y = xy(raw)
    axes.scatter(
        raw_x, raw_y, s=5, color=RAW, alpha=0.55, linewidths=0, label="raw fixes", zorder=3
    )
    axes.plot(*xy(cleaned), color=CLEANED, linewidth=2.0, label="cleaned", zorder=4)

    if len(dropped):
        axes.scatter(
            *xy(dropped),
            s=70,
            marker="x",
            color=DROPPED,
            linewidths=1.8,
            label=f"rejected ({len(dropped)})",
            zorder=6,
        )

    draw_dwell_markers(axes, dwells, label=True)

    # Frame on the *cleaned* track. A rejected fix can be kilometres away — that
    # is why it was rejected — and letting it set the extent squashes the route
    # itself into a line. Anything outside the frame is counted in the corner
    # instead.
    frame_to(axes, *xy(cleaned), pad=0.06)
    off_view = count_off_view(axes, *xy(dropped)) if len(dropped) else 0
    if off_view:
        axes.text(
            0.01,
            0.02,
            f"{off_view} rejected fix{'es' if off_view > 1 else ''} outside this view",
            transform=axes.transAxes,
            fontsize=8.5,
            color=INK_MUTED,
        )

    axes.set_aspect("equal", adjustable="box")
    style_metric_ticks(axes)
    axes.set_title("Plan view — EPSG:32736", loc="left", fontsize=11, color=INK, pad=8)
    axes.set_xlabel("easting (m)")
    axes.set_ylabel("northing (m)")
    axes.legend(**legend_style())


def frame_to(axes, xs: np.ndarray, ys: np.ndarray, *, pad: float) -> None:
    """Set square limits covering `xs`/`ys` with a fractional margin."""
    if not len(xs):
        return
    centre_x = (float(np.max(xs)) + float(np.min(xs))) / 2
    centre_y = (float(np.max(ys)) + float(np.min(ys))) / 2
    span = max(float(np.ptp(xs)), float(np.ptp(ys)), 1.0) * (0.5 + pad)
    axes.set_xlim(centre_x - span, centre_x + span)
    axes.set_ylim(centre_y - span, centre_y + span)


def count_off_view(axes, xs: np.ndarray, ys: np.ndarray) -> int:
    left, right = axes.get_xlim()
    bottom, top = axes.get_ylim()
    inside = (xs >= left) & (xs <= right) & (ys >= bottom) & (ys <= top)
    return int(np.count_nonzero(~inside))


def draw_zoom(axes, raw, cleaned, dwells, truth) -> None:
    """A close-up where the smoothing is actually visible."""
    centre, span, caption = zoom_window(cleaned, dwells)

    if truth is not None:
        axes.plot(*xy(truth), color=TRUTH, linewidth=1.4, linestyle="--", zorder=1)

    raw_x, raw_y = xy(raw)
    axes.plot(raw_x, raw_y, color=RAW, linewidth=0.9, alpha=0.45, zorder=2)
    axes.scatter(raw_x, raw_y, s=26, color=RAW, alpha=0.75, linewidths=0, zorder=3)
    axes.plot(*xy(cleaned), color=CLEANED, linewidth=2.0, zorder=4)
    draw_dwell_markers(axes, dwells, label=False)

    axes.set_xlim(centre[0] - span, centre[0] + span)
    axes.set_ylim(centre[1] - span, centre[1] + span)
    axes.set_aspect("equal", adjustable="box")
    style_metric_ticks(axes)
    axes.tick_params(labelsize=8)
    axes.set_title(
        f"Detail — {caption} ({span * 2:.0f} m across)", loc="left", fontsize=11, color=INK, pad=8
    )


def draw_speed(axes, raw, cleaned, dwells) -> None:
    """Segment speed over time: what `detect_dwells` actually reads."""
    for dwell in dwells:
        axes.axvspan(
            dwell.start_index, dwell.end_index, color=DWELL, alpha=0.16, linewidth=0, zorder=1
        )

    raw_speeds = segment_speeds(raw)
    clean_speeds = segment_speeds(cleaned)

    # Raw is dense, spiky and mostly context; it recedes so the cleaned line and
    # the threshold stay readable on top of it.
    axes.plot(
        np.arange(len(raw_speeds)),
        raw_speeds,
        color=RAW,
        linewidth=0.7,
        alpha=0.3,
        label="raw",
        zorder=2,
    )
    axes.plot(
        np.arange(len(clean_speeds)),
        clean_speeds,
        color=CLEANED,
        linewidth=2.0,
        label="cleaned",
        zorder=3,
        # A surface-coloured ring keeps the line legible where it crosses the
        # raw spikes.
        path_effects=[
            path_effects.Stroke(linewidth=4.0, foreground=SURFACE),
            path_effects.Normal(),
        ],
    )

    finite = clean_speeds[np.isfinite(clean_speeds)]
    # Headroom above the cruising speed leaves a clear band for the legend.
    top = float(np.max(finite)) * 1.9 if len(finite) else 12.0
    axes.set_ylim(0, max(top, MAX_DWELL_SPEED_MPS * 4))
    axes.axhline(MAX_DWELL_SPEED_MPS, color=INK_MUTED, linewidth=1.2, linestyle=":", zorder=4)
    axes.text(
        0.01,
        MAX_DWELL_SPEED_MPS / axes.get_ylim()[1] + 0.025,
        f"dwell threshold {MAX_DWELL_SPEED_MPS:g} m/s",
        transform=axes.transAxes,
        ha="left",
        fontsize=8.5,
        color=INK_MUTED,
        zorder=5,
    )

    visible_raw = raw_speeds[np.isfinite(raw_speeds)]
    if len(visible_raw) and float(np.max(visible_raw)) > axes.get_ylim()[1]:
        axes.text(
            0.01,
            0.96,
            "raw peaks continue off-scale",
            transform=axes.transAxes,
            va="top",
            fontsize=8.5,
            color=INK_MUTED,
        )

    axes.set_title(
        "Segment speed — shaded spans are detected dwells",
        loc="left",
        fontsize=11,
        color=INK,
        pad=8,
    )
    axes.set_xlabel("point index")
    axes.set_ylabel("speed (m/s)")
    axes.legend(**legend_style(loc="upper right"))


def draw_dwell_markers(axes, dwells, *, label: bool) -> None:
    """Dwell centroids, ringed in the surface colour so they read over the track.

    The duration label is not decoration: aqua sits below 3:1 on this surface,
    so the marks carry a visible label rather than leaning on hue alone.
    """
    if not dwells:
        return

    axes.scatter(
        [d.centroid.x for d in dwells],
        [d.centroid.y for d in dwells],
        s=150,
        color=DWELL,
        edgecolors=SURFACE,
        linewidths=2.0,
        zorder=7,
        label="dwell centroid" if label else None,
    )
    if not label:
        return

    for dwell in dwells:
        axes.annotate(
            f"{dwell.duration_s:.0f} s",
            (dwell.centroid.x, dwell.centroid.y),
            textcoords="offset points",
            xytext=(12, 8),
            fontsize=9,
            fontweight="600",
            color=INK_SECONDARY,
            zorder=8,
        )


def zoom_window(cleaned, dwells) -> tuple[tuple[float, float], float, str]:
    """Centre, half-width and caption for the detail panel.

    Frames the first dwell when there is one — that is where cleaning matters
    most — and the middle of the trace otherwise.
    """
    xs, ys = xy(cleaned)

    if dwells:
        dwell = dwells[0]
        centre = (dwell.centroid.x, dwell.centroid.y)
        window = cleaned.iloc[dwell.start_index : dwell.end_index + 1]
        extent = max(
            float(np.ptp(window.geometry.x.to_numpy())),
            float(np.ptp(window.geometry.y.to_numpy())),
        )
        return centre, max(extent * 1.6, 40.0), "first dwell"

    middle = len(cleaned) // 2
    return (float(xs[middle]), float(ys[middle])), 60.0, "mid-trace"


def legend_style(loc: str = "best") -> dict:
    return {
        "loc": loc,
        "frameon": True,
        "facecolor": SURFACE,
        "edgecolor": GRIDLINE,
        "framealpha": 0.95,
        "fontsize": 9,
        "labelcolor": INK_SECONDARY,
    }


if __name__ == "__main__":
    main()
