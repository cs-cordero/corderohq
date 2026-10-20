import { useState } from "react";
import { formatCurrency, formatYearMonth } from "../format";

// A line chart of the net worth over time, drawn as inline SVG. It has one series, and it
// needs almost no other code. One series needs no legend, because the heading names it. The
// grid lines and the axis ink stay light. The line is 2px. The most recent point has a
// label. A hover shows a crosshair and a tooltip. The colours suit the light slate
// background of the app.

interface NetWorthChartProps {
    months: string[]; // ascending YYYY-MM
    netWorth: number[]; // millionths, aligned with months
}

// Internal drawing coordinates; the SVG scales to its container via viewBox.
const VW = 640;
const VH = 260;
// y-axis labels and the end-of-line value callout both live in the right margin.
const M = { top: 16, right: 64, bottom: 28, left: 16 };
const PLOT_W = VW - M.left - M.right;
const PLOT_H = VH - M.top - M.bottom;

const SHORT_MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

// Compact x-axis label: "2026-09" -> "Sep '26".
function shortLabel(ym: string): string {
    const [y, m] = ym.split("-");
    return `${SHORT_MONTHS[parseInt(m, 10) - 1]} '${y.slice(2)}`;
}

// Abbreviated currency (input is millionths of a dollar), one decimal on K/M.
// `axis` drops a trailing ".0" so round gridline labels stay clean ($5K, $2M);
// the value callout keeps it ($5.4K, and $5.0K for a round figure) so the headline
// number reads consistently.
function abbrev(millionths: number, axis: boolean): string {
    const dollars = millionths / 1_000_000;
    const abs = Math.abs(dollars);
    const sign = dollars < 0 ? "-" : "";
    const fmt = (n: number, suffix: string) => {
        let s = n.toFixed(1);
        if (axis && s.endsWith(".0")) s = s.slice(0, -2);
        return `${sign}$${s}${suffix}`;
    };
    if (abs >= 1_000_000) return fmt(abs / 1_000_000, "M");
    if (abs >= 1_000) return fmt(abs / 1_000, "K");
    return `${sign}$${Math.round(abs)}`;
}

// "Nice" rounded bounds so gridlines land on readable numbers.
function niceBounds(min: number, max: number): { lo: number; hi: number } {
    if (min === max) {
        const pad = Math.abs(min) || 1_000_000;
        return { lo: min - pad, hi: max + pad };
    }
    const span = max - min;
    const step = Math.pow(10, Math.floor(Math.log10(span))) / 2;
    return { lo: Math.floor(min / step) * step, hi: Math.ceil(max / step) * step };
}

export default function NetWorthChart({ months, netWorth }: NetWorthChartProps) {
    const [hover, setHover] = useState<number | null>(null);

    const dataMin = Math.min(...netWorth, 0); // always include the zero baseline
    const dataMax = Math.max(...netWorth, 0);
    const { lo, hi } = niceBounds(dataMin, dataMax);

    const x = (i: number) => (months.length === 1 ? PLOT_W / 2 : (i / (months.length - 1)) * PLOT_W) + M.left;
    const y = (v: number) => M.top + PLOT_H - ((v - lo) / (hi - lo)) * PLOT_H;

    const points = netWorth.map((v, i) => ({ px: x(i), py: y(v), v, ym: months[i] }));
    const linePath = points.map((p, i) => `${i === 0 ? "M" : "L"}${p.px.toFixed(1)},${p.py.toFixed(1)}`).join(" ");
    const areaPath =
        points.length > 1
            ? `${linePath} L${points[points.length - 1].px.toFixed(1)},${y(lo).toFixed(1)} L${points[0].px.toFixed(1)},${y(lo).toFixed(1)} Z`
            : "";

    // Up to 4 horizontal gridlines across the domain.
    const gridVals = [0, 1, 2, 3].map((k) => lo + ((hi - lo) * k) / 3);

    // Thin out x labels so they don't collide on long histories.
    const labelStride = Math.ceil(months.length / 6);

    const last = points[points.length - 1];
    const zeroInDomain = lo < 0 && hi > 0;

    return (
        <div className="networth-chart">
            <svg viewBox={`0 0 ${VW} ${VH}`} role="img" aria-label="Net worth over time">
                {/* gridlines + y labels */}
                {gridVals.map((gv, i) => (
                    <g key={i}>
                        <line className="nw-chart-grid" x1={M.left} x2={M.left + PLOT_W} y1={y(gv)} y2={y(gv)} />
                        <text className="nw-chart-axis" x={M.left + PLOT_W + 6} y={y(gv) + 3}>
                            {abbrev(gv, true)}
                        </text>
                    </g>
                ))}
                {/* emphasized zero baseline when the series crosses it */}
                {zeroInDomain && (
                    <line className="nw-chart-zero" x1={M.left} x2={M.left + PLOT_W} y1={y(0)} y2={y(0)} />
                )}

                {areaPath && <path className="nw-chart-area" d={areaPath} />}
                {points.length > 1 && <path className="nw-chart-line" d={linePath} />}

                {/* x labels — anchor the first at the start and the last at the end so
                    the edge labels don't overflow the plot horizontally. */}
                {points.map((p, i) =>
                    i % labelStride === 0 || i === points.length - 1 ? (
                        <text
                            key={p.ym}
                            className="nw-chart-axis"
                            x={p.px}
                            y={VH - 8}
                            textAnchor={i === 0 ? "start" : i === points.length - 1 ? "end" : "middle"}
                        >
                            {shortLabel(p.ym)}
                        </text>
                    ) : null,
                )}

                {/* crosshair on hover */}
                {hover !== null && (
                    <line
                        className="nw-chart-crosshair"
                        x1={points[hover].px}
                        x2={points[hover].px}
                        y1={M.top}
                        y2={M.top + PLOT_H}
                    />
                )}

                {/* visible dots */}
                {points.map((p, i) => (
                    <circle
                        key={`d${p.ym}`}
                        className={hover === i ? "nw-chart-dot nw-chart-dot-active" : "nw-chart-dot"}
                        cx={p.px}
                        cy={p.py}
                        r={hover === i ? 4.5 : 3}
                    />
                ))}

                {/* latest value as an end-of-line callout in the right margin (past the
                    final point, so it never crosses the line). It shares the margin with
                    the y-axis numbers; a white halo + the line's blue keep it legible and
                    make it read as the highlighted current value. */}
                {last && hover === null && (
                    <text
                        className="nw-chart-last-label"
                        x={M.left + PLOT_W + 6}
                        y={Math.max(M.top + 12, Math.min(last.py + 4, M.top + PLOT_H))}
                        textAnchor="start"
                    >
                        {abbrev(last.v, false)}
                    </text>
                )}

                {/* oversized invisible hit targets */}
                {points.map((p, i) => (
                    <circle
                        key={`h${p.ym}`}
                        cx={p.px}
                        cy={p.py}
                        r={14}
                        fill="transparent"
                        onMouseEnter={() => setHover(i)}
                        onMouseLeave={() => setHover((h) => (h === i ? null : h))}
                    />
                ))}
            </svg>

            {hover !== null && (
                <div className="nw-chart-tooltip">
                    <strong>{formatYearMonth(points[hover].ym)}</strong>
                    <span>{formatCurrency(points[hover].v)}</span>
                </div>
            )}
        </div>
    );
}
