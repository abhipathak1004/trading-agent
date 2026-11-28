import { useId, useMemo } from "react";

/**
 * The strategy's NAV against its benchmark, as an inline SVG.
 *
 * There is no charting library in this app (by design — `package.json` is
 * react + react-dom only), so this is a small, accessible plot rather than a
 * dependency. Three rules it keeps:
 *
 * 1. `viewBox` + `preserveAspectRatio` so it scales with the panel and never
 *    forces horizontal page overflow.
 * 2. Colour is never the only signal: both series are labelled, the table
 *    below carries every plotted number, and the SVG is `aria-hidden` because
 *    it duplicates that table.
 * 3. Values come from aihf untouched. Nothing here normalises, compounds, or
 *    re-bases the curve — the numbers are the ones it serialised.
 */

export type CurveSeries = {
  label: string;
  values: number[];
  tone: "accent" | "muted";
};

type Props = {
  dates: string[];
  series: CurveSeries[];
  /** Shown above the plot; defaults to a plain description. */
  title?: string;
  labelX?: string;
};

const WIDTH = 720;
const HEIGHT = 240;
const PAD_LEFT = 52;
const PAD_RIGHT = 12;
const PAD_TOP = 12;
const PAD_BOTTOM = 26;

function path(values: number[], min: number, max: number): string {
  if (values.length === 0) {
    return "";
  }
  const span = max - min || 1;
  const innerWidth = WIDTH - PAD_LEFT - PAD_RIGHT;
  const innerHeight = HEIGHT - PAD_TOP - PAD_BOTTOM;
  const step = values.length > 1 ? innerWidth / (values.length - 1) : 0;
  return values
    .map((value, index) => {
      const x = PAD_LEFT + index * step;
      const y = PAD_TOP + innerHeight - ((value - min) / span) * innerHeight;
      return `${index === 0 ? "M" : "L"}${x.toFixed(2)},${y.toFixed(2)}`;
    })
    .join(" ");
}

export default function EquityCurve({ dates, series, title, labelX = "Session" }: Props) {
  const gradientId = useId();
  const points = useMemo(() => {
    const all = series.flatMap((entry) => entry.values);
    if (all.length === 0) {
      return { min: 0, max: 0, all };
    }
    const min = Math.min(...all);
    const max = Math.max(...all);
    // A flat curve would divide by zero in `path`; pad it so the line is mid-height.
    return min === max ? { min: min - 1, max: max + 1, all } : { min, max, all };
  }, [series]);

  if (dates.length === 0 || points.all.length === 0) {
    return <p className="panel-note">No equity curve in this result.</p>;
  }

  const innerHeight = HEIGHT - PAD_TOP - PAD_BOTTOM;
  const ticks = [points.max, (points.max + points.min) / 2, points.min];
  const stride = Math.max(1, Math.ceil(dates.length / 5));

  return (
    <figure className="ahf-curve">
      <figcaption className="ahf-curve__caption">
        <span>{title ?? "Strategy NAV vs. benchmark"}</span>
        <span className="ahf-curve__legend">
          {series.map((entry) => (
            <span key={entry.label} className={`ahf-curve__key ahf-curve__key--${entry.tone}`}>
              <span aria-hidden="true" className="ahf-curve__swatch" />
              {entry.label}
            </span>
          ))}
        </span>
      </figcaption>

      <svg
        className="ahf-curve__plot"
        viewBox={`0 0 ${WIDTH} ${HEIGHT}`}
        preserveAspectRatio="xMidYMid meet"
        role="presentation"
        aria-hidden="true"
      >
        <defs>
          <linearGradient id={gradientId} x1="0" x2="0" y1="0" y2="1">
            <stop offset="0%" stopColor="var(--accent)" stopOpacity="0.28" />
            <stop offset="100%" stopColor="var(--accent)" stopOpacity="0" />
          </linearGradient>
        </defs>

        {ticks.map((tick, index) => {
          const y = PAD_TOP + (innerHeight / 2) * index;
          return (
            <g key={tick}>
              <line
                className="ahf-curve__grid"
                x1={PAD_LEFT}
                x2={WIDTH - PAD_RIGHT}
                y1={y}
                y2={y}
              />
              <text className="ahf-curve__tick" x={PAD_LEFT - 8} y={y + 4} textAnchor="end">
                {Math.round(tick).toLocaleString()}
              </text>
            </g>
          );
        })}

        {series.map((entry) => (
          <path
            key={entry.label}
            className={`ahf-curve__line ahf-curve__line--${entry.tone}`}
            d={path(entry.values, points.min, points.max)}
            fill="none"
          />
        ))}

        {dates.map((date, index) =>
          index % stride === 0 || index === dates.length - 1 ? (
            <g key={`${date}-${index}`}>
              <line
                className="ahf-curve__grid ahf-curve__grid--faint"
                x1={
                  PAD_LEFT +
                  (dates.length > 1 ? (index * (WIDTH - PAD_LEFT - PAD_RIGHT)) / (dates.length - 1) : 0)
                }
                x2={
                  PAD_LEFT +
                  (dates.length > 1 ? (index * (WIDTH - PAD_LEFT - PAD_RIGHT)) / (dates.length - 1) : 0)
                }
                y1={PAD_TOP}
                y2={HEIGHT - PAD_BOTTOM}
              />
              <text
                className="ahf-curve__tick"
                x={
                  PAD_LEFT +
                  (dates.length > 1 ? (index * (WIDTH - PAD_LEFT - PAD_RIGHT)) / (dates.length - 1) : 0)
                }
                y={HEIGHT - PAD_BOTTOM + 16}
                textAnchor="middle"
              >
                {date.slice(5)}
              </text>
            </g>
          ) : null,
        )}
      </svg>

      <details className="ahf-curve__data">
        <summary>Curve data ({dates.length} sessions)</summary>
        <div className="table-scroll" role="region" aria-label="Equity curve data" tabIndex={0}>
          <table className="judgment-table">
            <caption className="ahf-curve__caption-text">
              Every plotted value, as aihf recorded it. {labelX} is the session key.
            </caption>
            <thead>
              <tr>
                <th scope="col">{labelX}</th>
                {series.map((entry) => (
                  <th scope="col">{entry.label}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {dates.map((date, index) => (
                <tr key={date}>
                  <th scope="row">{date}</th>
                  {series.map((entry) => (
                    <td key={entry.label}>{entry.values[index]?.toLocaleString() ?? "—"}</td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </details>
    </figure>
  );
}
