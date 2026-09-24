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

// Keep canvases alive across polls, including when the overview is temporarily hidden.
export function atlasCharts() {
  const cloud = document.createElement('div'), bars = document.createElement('div');
  cloud.className = 'atlas-cloud'; bars.className = 'atlas-chart';
  cloud.setAttribute('role', 'img'); bars.setAttribute('role', 'img');
  const canvas = document.createElement('canvas');
  canvas.style.cssText = 'width:100%;height:100%;display:block';
  let barChart, latest, wordSignature, zoomKey;
  let zoom = { start: 0, end: 100 };
  let selectedStates = { Idle: false };
  const stateControls = document.createElement('div');
  stateControls.className = 'atlas-state-legend';
  stateControls.setAttribute('role', 'group');
  stateControls.setAttribute('aria-label', 'Visible history states');
  const syncStates = () => [...stateControls.children].forEach(button => {
    button.setAttribute('aria-pressed', String(selectedStates[button.textContent] !== false));
  });
  const setZoom = (start, span) => {
    span = Math.max(1, Math.min(100, span));
    start = Math.max(0, Math.min(100 - span, start));
    zoom = { start, end: start + span };
    barChart?.dispatchAction({ type: 'dataZoom', ...zoom });
  };
  const paint = async () => {
    if (!latest || !cloud.clientWidth) return;
    let echarts;
    try { echarts = await loadCharts(); }
    catch { cloud.textContent = 'Chart unavailable. Choose a topic below.'; bars.textContent = 'Chart unavailable. Reload to retry.'; return; }
    if (!cloud.clientWidth) return;
    if (!barChart) {
      cloud.replaceChildren(canvas); bars.replaceChildren();
      barChart = echarts.init(bars);
      barChart.on('legendselectchanged', event => { selectedStates = { ...event.selected }; syncStates(); });
      barChart.on('datazoom', event => {
        const selection = event.batch?.[0] || event;
        zoom = { start: selection.start, end: selection.end };
      });
    }
    const { words, samples, states, step, unit = 'Panes' } = latest;
    // Live polls and theme changes preserve the view; a new range/filter starts fresh.
    if (zoomKey !== latest.zoomKey) {
      zoomKey = latest.zoomKey;
      zoom = { start: 0, end: 100 };
    }
    const css = getComputedStyle(document.documentElement);
    const color = name => css.getPropertyValue(name).trim();
    const muted = color('--muted'), line = color('--line');
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
    const indexed = new Map(samples.map(s => [s.t, s]));
    const times = [];
    for (let t = samples[0]?.t; t <= samples[samples.length - 1]?.t; t += step) times.push(t);
    const single = times.length === 1;
    const label = t => times.length && new Date(times[0]).toDateString() !== new Date(times[times.length - 1]).toDateString()
      ? `${new Date(t).toLocaleDateString([], { month: 'short', day: 'numeric' })} ${timeLabel(t)}` : timeLabel(t);
    const order = [1, 4, 0, 5, 2, 3]; // Working at the bottom, idle at the top, like the reference.
    const colors = dark ? ['#f1bd53', '#60c992', '#3f4a55', '#4a5261', '#bea5ee', '#8bbdf5'] : ['#f2ad32', '#39b978', '#e3e8ed', '#d1d8e2', '#9363c4', '#729cc1'];
    [...stateControls.children].forEach((button, i) => button.style.setProperty('--state-color', colors[i]));
    bars.setAttribute('aria-label', samples.length
      ? ` ${unit} by state, ${timeLabel(samples[0].t)} to ${timeLabel(samples[samples.length - 1].t)}. ${samples.filter(s => s.n !== null).length} observed buckets. Latest: ${samples[samples.length - 1].n ? states.map((s, i) => `${samples[samples.length - 1].n[i]} ${s}`).join(', ') : 'No observation'}.`
      : 'No observations yet.');
    barChart.setOption({
      animation: false, textStyle: { color: muted, fontFamily: 'sans-serif' },
      tooltip: { trigger: 'axis', renderMode: 'richText', confine: true, axisPointer: { type: 'shadow' },
        formatter: items => {
          const sample = indexed.get(times[items[0]?.dataIndex]);
          if (!sample?.n) return 'No observation';
          return [label(sample.t), sample.source === 'logs' ? 'Reconstructed from logs' : 'Daemon snapshot',
            ...items.map(item => `${item.seriesName}: ${item.value} ${unit.toLowerCase()}`)].join('\n');
        } },
      legend: { show: false, selected: selectedStates },
      grid: { left: 42, right: 14, top: 28, bottom: 78 },
      dataZoom: [
        { type: 'slider', xAxisIndex: 0, ...zoom, bottom: 4, height: 24,
          left: 42, right: 14, showDetail: false, borderColor: line,
          textStyle: { color: muted }, fillerColor: color('--accent-bg'),
          handleStyle: { color: color('--accent'), borderColor: color('--accent') } },
        { type: 'inside', xAxisIndex: 0, ...zoom, zoomOnMouseWheel: 'ctrl',
          moveOnMouseWheel: false, preventDefaultMouseMove: false },
      ],
      xAxis: { type: 'category', data: times.map(label), axisTick: { show: false },
        axisLine: { lineStyle: { color: line } }, axisLabel: { color: muted, hideOverlap: true } },
      yAxis: { type: 'value', minInterval: 1, name: unit, nameTextStyle: { color: muted },
        splitLine: { lineStyle: { color: line } }, axisLabel: { color: muted } },
      series: order.map(i => ({ name: states[i], type: 'bar', stack: 'panes',
        barMaxWidth: single ? 100 : undefined, barCategoryGap: '0%',
        itemStyle: { color: colors[i] }, emphasis: { focus: 'series' },
        label: { show: times.length < 8, formatter: p => p.value > 0 ? p.value : '', color: dark ? '#101312' : '#243142', fontWeight: 600 },
        data: times.map(t => indexed.get(t)?.n != null ? { value: indexed.get(t).n[i],
          itemStyle: { opacity: indexed.get(t).source === 'logs' ? 0.65 : 1 } } : null),
      })),
    }, true);
    barChart.resize();
  };
  new ResizeObserver(() => {
    if (!cloud.clientWidth) return;
    paint();
  }).observe(cloud);
  new MutationObserver(paint).observe(document.documentElement, { attributes: true, attributeFilter: ['class'] });
  bars.tabIndex = 0;
  bars.dataset.key = 'history-chart';
  bars.addEventListener('keydown', event => {
    if (event.ctrlKey || event.metaKey || event.altKey) return;
    const span = zoom.end - zoom.start;
    if (event.key === '+' || event.key === '=') setZoom(zoom.start + span / 4, span / 2);
    else if (event.key === '-') setZoom(zoom.start - span / 2, span * 2);
    else if (event.key === 'ArrowLeft') setZoom(zoom.start - span / 2, span);
    else if (event.key === 'ArrowRight') setZoom(zoom.start + span / 2, span);
    else return;
    event.preventDefault();
  });
  bars.title = 'Drag the range handles to zoom. Keyboard: + or − to zoom, left or right arrows to pan.';
  return { cloud, bars, stateControls, resetZoom() {
    zoom = { start: 0, end: 100 };
    barChart?.dispatchAction({ type: 'dataZoom', ...zoom });
  }, update(data) {
    latest = data;
    if (!stateControls.children.length) data.states.forEach(state => {
      const button = document.createElement('button');
      button.className = 'atlas-state-toggle'; button.textContent = state;
      button.title = `Show or hide ${state.toLowerCase()} history`;
      button.dataset.key = `history-state:${state}`;
      button.onclick = () => { selectedStates[state] = selectedStates[state] === false; syncStates(); paint(); };
      stateControls.append(button);
    });
    syncStates(); paint();
  } };

}
