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
    await loadScript('/m/vendor/wordcloud.js');
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
  const paint = async () => {
    if (!latest || !cloud.clientWidth) return;
    let echarts;
    try { echarts = await loadCharts(); }
    catch { cloud.textContent = 'Chart unavailable. Choose a topic below.'; bars.textContent = 'Chart unavailable. Reload to retry.'; return; }
    if (!cloud.clientWidth) return;
    if (!barChart) {
      cloud.replaceChildren(canvas); bars.replaceChildren();
      barChart = echarts.init(bars);
      barChart.on('datazoom', event => {
        const selection = event.batch?.[0] || event;
        zoom = { start: selection.start, end: selection.end };
      });
    }
    const { words, samples, states, step } = latest;
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
    cloud.setAttribute('aria-label', `Topics sized by number of matching panes: ${words.map(([w, n]) => `${w}: ${n}`).join(', ')}. Use the topic selector below to explore.`);
    const indexed = new Map(samples.map(s => [s.t, s]));
    const times = [];
    for (let t = samples[0]?.t; t <= samples.at(-1)?.t; t += step) times.push(t);
    const single = times.length === 1;
    const label = t => times.length && times.at(-1) - times[0] >= 86400000
      ? `${new Date(t).toLocaleDateString([], { month: 'short', day: 'numeric' })} ${timeLabel(t)}` : timeLabel(t);
    const order = [1, 0, 2, 3]; // Working at the bottom, idle at the top, like the reference.
    const colors = dark ? ['#f1bd53', '#60c992', '#7e91ab', '#4a5261'] : ['#f2ad32', '#39b978', '#a3b2c7', '#d1d8e2'];
    bars.setAttribute('aria-label', samples.length
      ? `Pane state counts, ${timeLabel(samples[0].t)} to ${timeLabel(samples.at(-1).t)}. ${samples.length} observations. Latest: ${states.map((s, i) => `${samples.at(-1).n[i]} ${s}`).join(', ')}.`
      : 'No observations yet.');
    barChart.setOption({
      animation: false, textStyle: { color: muted, fontFamily: 'sans-serif' },
      tooltip: { trigger: 'axis', renderMode: 'richText', confine: true, axisPointer: { type: 'shadow' },
        formatter: items => {
          const sample = indexed.get(times[items[0]?.dataIndex]);
          if (!sample) return 'No observation';
          return [label(sample.t), sample.source === 'logs' ? 'Reconstructed from logs' : 'Daemon snapshot',
            ...items.map(item => `${item.seriesName}: ${item.value} panes`)].join('\n');
        } },
      legend: { top: 0, right: 0, itemWidth: 10, itemHeight: 10, textStyle: { color: muted, fontSize: 11 } },
      grid: { left: 42, right: 14, top: 55, bottom: 78 },
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
      yAxis: { type: 'value', minInterval: 1, name: 'Panes', nameTextStyle: { color: muted },
        splitLine: { lineStyle: { color: line } }, axisLabel: { color: muted } },
      series: order.map(i => ({ name: states[i], type: 'bar', stack: 'panes',
        barMaxWidth: single ? 100 : undefined, barCategoryGap: '0%',
        itemStyle: { color: colors[i] }, emphasis: { focus: 'series' },
        label: { show: times.length < 8, formatter: p => p.value > 0 ? p.value : '', color: dark ? '#101312' : '#243142', fontWeight: 600 },
        data: times.map(t => indexed.has(t) ? { value: indexed.get(t).n[i],
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
  return { cloud, bars, resetZoom() {
    zoom = { start: 0, end: 100 };
    barChart?.dispatchAction({ type: 'dataZoom', ...zoom });
  }, update(data) { latest = data; paint(); } };
}
