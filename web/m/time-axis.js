// Human-friendly x-axis for the history chart. ECharts' time scale already snaps ticks
// to round boundaries (5/10/15/30 min, 1/2/4/6/12 h, days, weeks, months) for whatever
// span is visible, so zoom and pan re-tick for free; this module only decides how many
// ticks the width can hold and how each one reads.
const TWO_WEEKS = 14 * 864e5;
// History buckets are ≥1 min, so zooming past one minute only magnifies a single bar.
export const MIN_VIEW_SPAN = 60e3;
const CLOCK_UNITS = new Set(['minute', 'second', 'millisecond']);
const format = (t, options) => new Date(t).toLocaleString([], options);

// The visible span (after zoom/pan) decides whether dates read as weekdays or months.
const visibleSpan = chart => {
  const [start, end] = chart.getModel().getComponent('xAxis').axis.scale.getExtent();
  return end - start;
};

export function timeAxis(chart, width, labelStyle) {
  return {
    type: 'time',
    // ECharts may place up to ~2.5 ticks per requested split, so ~100px per split keeps
    // labels ≥40px apart on a phone and gives a desktop a dozen or so.
    splitNumber: Math.max(4, Math.floor(width / 100)),
    // The extent's own edges sit on arbitrary minutes; only the round ticks get labels.
    axisLabel: { ...labelStyle, hideOverlap: true, showMinLabel: false, showMaxLabel: false,
      formatter: (t, _, { time }) => timeLabel(t, time.upperTimeUnit, visibleSpan(chart)) },
  };
}

// Times only, with the date appearing just on the tick where the day turns over. The
// meridiem rides on whole hours; the ticks between them stay short ("2:15", "2:15:30").
export function timeLabel(t, unit, span) {
  const date = new Date(t);
  if (CLOCK_UNITS.has(unit)) return new Intl.DateTimeFormat([], { hour: 'numeric', minute: '2-digit',
    second: unit === 'minute' ? undefined : '2-digit' }).formatToParts(date).filter(part => part.type !== 'dayPeriod').map(part => part.value).join('').trim();
  if (unit === 'hour') return format(t, { hour: 'numeric' });
  if (span <= TWO_WEEKS) return `${format(t, { weekday: 'short' })} ${date.getDate()}`;
  if (unit === 'day') return format(t, { month: 'short', day: 'numeric' });
  return format(t, unit === 'year' ? { year: 'numeric' } : { month: 'short' });
}
