// Local, pinned distributions: the mobile list never downloads chart libraries.
let libraries;
function loadScript(src) {
  return new Promise((resolve, reject) => {
    const script = document.createElement('script');
    script.src = src;
    script.onload = resolve;
    script.onerror = () => { script.remove(); reject(new Error('Could not load charts')); };
    document.head.append(script);
  });
}
function loadCharts() {
  if (!libraries) libraries = (async () => {
    if (!window.echarts) await loadScript('/m/vendor/echarts.min.js');
    if (!window.WordCloud) await loadScript('/m/vendor/wordcloud.js');
    return window.echarts;
  })().catch(error => { libraries = null; throw error; });
  return libraries;
}
const timeLabel = t => new Date(t).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });

// The word cloud, kept alive across polls, including when the overview is temporarily hidden.
export function atlasCharts() {
  const cloud = document.createElement('div');
  cloud.className = 'atlas-cloud';
  cloud.setAttribute('role', 'img');
  const canvas = document.createElement('canvas');
  canvas.style.cssText = 'width:100%;height:100%;display:block';
  let latest, wordSignature;
  const paint = async () => {
    if (!latest || !cloud.clientWidth) return;
    try { await loadCharts(); } catch { cloud.textContent = 'Chart unavailable. Choose a topic below.'; return; }
    if (!cloud.clientWidth) return;
    if (canvas.parentNode !== cloud) cloud.replaceChildren(canvas);
    const { words } = latest;
    const dark = !document.documentElement.classList.contains('light');
    const palette = dark ? ['#83ddb3', '#8bbdf5', '#bea5ee', '#f1c56a', '#e89f91']
      : ['#176c50', '#2867a0', '#76509e', '#956315', '#a24e41'];
    const ratio = window.devicePixelRatio || 1;
    const signature = JSON.stringify([words, dark, cloud.clientWidth, cloud.clientHeight, ratio]);
    if (wordSignature !== signature) {
      wordSignature = signature;
      canvas.width = Math.round(cloud.clientWidth * ratio);
      canvas.height = Math.round(cloud.clientHeight * ratio);
      const weights = words.map(([, n]) => n);
      const min = Math.min(...weights), max = Math.max(...weights);
      window.WordCloud(canvas, {
        list: words, fontFamily: 'sans-serif', fontWeight: '600',
        weightFactor: n => (14 + 40 * (n - min) / Math.max(1, max - min)) * ratio,
        gridSize: Math.max(4, Math.round(7 * ratio)), rotateRatio: 0,
        shrinkToFit: true, drawOutOfBound: false, backgroundColor: 'transparent',
        color: word => palette[words.findIndex(([name]) => name === word) % palette.length],
        click: item => latest.selectWord(item[0]),
        hover: item => {
          canvas.style.cursor = item ? 'pointer' : 'default';
          canvas.title = item ? `${item[0]} · ${item[1]} panes` : '';
        },
      });
    }
    cloud.setAttribute('aria-label', `Topics sized by number of matching panes: ${words.map(([w, n]) => `${w}: ${n}`).join(', ')}. Click a word to explore, or Tab to its topic button.`);
  };
  new ResizeObserver(paint).observe(cloud);
  new MutationObserver(paint).observe(document.documentElement, { attributes: true, attributeFilter: ['class'] });
  return { cloud, update(data) { latest = data; paint(); } };
}

// Which states the fleet charts stack. One object for every chart, so hiding Idle in the
// split hides it on the dashboard too.
export const shownStates = { Idle: false };
const ORDER = [1, 4, 0, 5, 2, 3]; // Running at the bottom, idle at the top.

// THE fleet chart: every surface (the split's strip and body, the dashboard, its small
// multiples) is one of these. Stacked state bars, the trailing average of Running as a
// purple line, the goal as a dashed one. `axis` adds axes, tooltip labels and zoom; without
// it the chart is a sparkline. `update` takes rows of {t, n, source, ma}.
export function fleetChart(axis) {
  const el = document.createElement('div');
  el.className = axis ? 'fleet-chart fleet-chart-axis' : 'fleet-chart';
  el.setAttribute('role', 'img');
  let chart, latest, zoomKey, zoom = { start: 0, end: 100 };
  const setZoom = (start, span) => {
    span = Math.max(1, Math.min(100, span));
    start = Math.max(0, Math.min(100 - span, start));
    zoom = { start, end: start + span };
    chart?.dispatchAction({ type: 'dataZoom', ...zoom });
  };
  const paint = async () => {
    if (!latest || !el.clientWidth) return;
    let echarts;
    try { echarts = await loadCharts(); } catch { el.textContent = 'Chart unavailable. Reload to retry.'; return; }
    if (!el.clientWidth) return;
    if (!chart) {
      el.replaceChildren();
      chart = echarts.init(el);
      chart.on('datazoom', event => {
        const selection = event.batch?.[0] || event;
        zoom = { start: selection.start, end: selection.end };
      });
    }
    const { rows, states, step, unit = 'Panes', goal, max, average } = latest;
    // Live polls and theme changes preserve the view; a new range/filter starts fresh.
    if (zoomKey !== latest.zoomKey) { zoomKey = latest.zoomKey; zoom = { start: 0, end: 100 }; }
    const css = getComputedStyle(document.documentElement);
    const color = name => css.getPropertyValue(name).trim();
    const muted = color('--muted'), line = color('--line'), fg = color('--fg'), purple = color('--purple');
    const dark = !document.documentElement.classList.contains('light');
    const single = rows.length === 1, last = rows.at(-1), avg = rows.findLast(r => r.ma != null)?.ma;
    const label = t => rows.length && new Date(rows[0].t).toDateString() !== new Date(last.t).toDateString()
      ? `${new Date(t).toLocaleDateString([], { month: 'short', day: 'numeric' })} ${timeLabel(t)}` : timeLabel(t);
    el.setAttribute('aria-label', rows.length
      ? `${unit} by state, ${label(rows[0].t)} to ${label(last.t)}. Latest: ${last.n ? states.map((s, i) => `${last.n[i]} ${s}`).join(', ') : 'no observation'}.${avg == null ? '' : ` ${average} average of Running: ${avg.toFixed(1)}.`}${goal ? ` Goal: ${goal}.` : ''}`
      : 'No observations yet.');
    chart.setOption({
      animation: false, textStyle: { color: muted, fontFamily: 'sans-serif' },
      tooltip: { trigger: 'axis', renderMode: 'richText', confine: true, axisPointer: { type: 'shadow' },
        formatter: items => {
          const row = rows[items[0]?.dataIndex];
          if (!row?.n) return 'No observation';
          return [label(row.t), row.source === 'logs' ? 'Reconstructed from logs' : 'Daemon snapshot',
            ...items.filter(item => item.value[1] != null)
              .map(item => `${item.seriesName}: ${+item.value[1].toFixed(1)} ${unit.toLowerCase()}`)].join('\n');
        } },
      grid: axis ? { left: 42, right: 14, top: 28, bottom: 78 } : { left: 0, right: 0, top: 2, bottom: 0 },
      dataZoom: axis ? [
        { type: 'slider', xAxisIndex: 0, ...zoom, bottom: 4, height: 24,
          left: 42, right: 14, showDetail: false, borderColor: line,
          textStyle: { color: muted }, fillerColor: color('--accent-bg'),
          handleStyle: { color: color('--accent'), borderColor: color('--accent') } },
        { type: 'inside', xAxisIndex: 0, ...zoom, zoomOnMouseWheel: 'ctrl',
          moveOnMouseWheel: false, preventDefaultMouseMove: false },
      ] : [],
      // ECharts' time scale snaps ticks to round units for the visible span (so zoom re-ticks) and
      // formats each by its unit, emphasising the higher unit where it turns over.
      xAxis: { type: 'time', show: axis, axisTick: { show: false }, axisLine: { lineStyle: { color: line } },
        // One bucket has no extent of its own; ECharts would pad it to two whole days.
        ...(single && { min: rows[0].t - step, max: rows[0].t + step }),
        axisLabel: { color: muted, hideOverlap: true, formatter: { day: '{MMM} {d}' } } },
      // The goal line is a markLine, which ECharts leaves out of the extent; a shared `max`
      // puts small multiples on one scale.
      yAxis: { type: 'value', show: axis, minInterval: 1, name: unit, nameTextStyle: { color: muted },
        max: max ?? (extent => Math.max(extent.max, goal || 0)),
        splitLine: { lineStyle: { color: line } }, axisLabel: { color: muted } },
      series: [...ORDER.filter(i => shownStates[states[i]] !== false).map(i => ({
        name: states[i], type: 'bar', stack: 'panes', barMaxWidth: single ? 100 : undefined, barCategoryGap: '0%',
        itemStyle: { color: color(`--chart-s${i}`) }, emphasis: { focus: 'series' },
        label: { show: axis && rows.length < 8, formatter: p => p.value[1] > 0 ? p.value[1] : '', color: dark ? '#101312' : '#243142', fontWeight: 600 },
        data: rows.map(r => r.n ? { value: [r.t, r.n[i]], itemStyle: { opacity: r.source === 'logs' ? 0.65 : 1 } } : [r.t, null]),
      })), { name: `${average} average`, type: 'line', z: 5, showSymbol: false, data: rows.map(r => [r.t, r.ma]),
        itemStyle: { color: purple }, lineStyle: { color: purple, width: axis ? 2.2 : 1.6 },
        markLine: goal ? { silent: true, symbol: 'none', data: [{ yAxis: goal }],
          lineStyle: { color: fg, type: 'dashed', opacity: 0.7, width: 1.2 },
          label: { show: axis, position: 'insideEndTop', formatter: `goal ${goal}`, color: fg } } : undefined }],
    }, true);
    chart.resize();
  };
  const resize = new ResizeObserver(paint), theme = new MutationObserver(paint);
  resize.observe(el);
  theme.observe(document.documentElement, { attributes: true, attributeFilter: ['class'] });
  if (axis) {
    el.tabIndex = 0;
    el.title = 'Drag the range handles to zoom. Keyboard: + or − to zoom, left or right arrows to pan.';
    el.addEventListener('keydown', event => {
      if (event.ctrlKey || event.metaKey || event.altKey) return;
      const span = zoom.end - zoom.start;
      if (event.key === '+' || event.key === '=') setZoom(zoom.start + span / 4, span / 2);
      else if (event.key === '-') setZoom(zoom.start - span / 2, span * 2);
      else if (event.key === 'ArrowLeft') setZoom(zoom.start - span / 2, span);
      else if (event.key === 'ArrowRight') setZoom(zoom.start + span / 2, span);
      else return;
      event.preventDefault();
    });
  }
  return { el, resetZoom: () => setZoom(0, 100), update(data) { latest = data; paint(); },
    dispose() { resize.disconnect(); theme.disconnect(); chart?.dispose(); } };
}
