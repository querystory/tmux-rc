// Human-friendly x-axis for the history chart. ECharts' time scale already snaps ticks
// to round boundaries (5/10/15/30 min, 1/2/4/6/12 h, days, weeks, months) for whatever
// span is visible, so zoom and pan re-tick for free; this module only decides how many
// ticks the width can hold and how each one reads.
const TWO_WEEKS = 14 * 864e5;
// History buckets are ≥1 min, so zooming past one minute only magnifies a single bar.
export const MIN_VIEW_SPAN = 60e3;
const CLOCK_UNITS = new Set(['minute', 'second', 'millisecond']);

// The visible span (after zoom/pan) decides whether dates read as weekdays or months.
const visibleSpan = chart => {
  const [start, end] = chart.getModel().getComponent('xAxis').axis.scale.getExtent();
  return end - start;
};

// `locale` defaults to the browser's; tests pin one.
export function timeAxis(chart, width, labelStyle, locale) {
  return {
    type: 'time',
    // ECharts may place up to ~2.5 ticks per requested split, so ~100px per split keeps
    // labels ≥40px apart on a phone and gives a desktop a dozen or so.
    splitNumber: Math.max(4, Math.floor(width / 100)),
    // The extent's own edges sit on arbitrary minutes; only the round ticks get labels.
    axisLabel: { ...labelStyle, hideOverlap: true, showMinLabel: false, showMaxLabel: false,
      formatter: (t, _, { time }) => timeLabel(t, time.upperTimeUnit, visibleSpan(chart), locale) },
  };
}

// Times only, with the date appearing just on the tick where the day turns over. The
// meridiem rides on whole hours; the ticks between them stay short ("2:15", "2:15:30").
export function timeLabel(t, unit, span, locale) {
  const format = options => new Date(t).toLocaleString(locale, options);
  if (CLOCK_UNITS.has(unit)) return new Intl.DateTimeFormat(locale, { hour: 'numeric', minute: '2-digit',
    second: unit === 'minute' ? undefined : '2-digit' }).formatToParts(t).filter(part => part.type !== 'dayPeriod').map(part => part.value).join('').trim();
  if (unit === 'hour') return format({ hour: 'numeric' });
  if (span <= TWO_WEEKS && unit === 'day') return `${format({ weekday: 'short' })} ${format({ day: 'numeric' })}`;
  if (span <= TWO_WEEKS || unit === 'day') return format({ month: 'short', day: 'numeric' });
  return format(unit === 'year' ? { year: 'numeric' } : { month: 'short' });
}
