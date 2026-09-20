'use strict';

/*
 * 前端只做两件事：把输入发给后端、把后端的状态画出来。
 * 这里没有限位判断、没有本地权威状态 —— 所有拒绝与夹取都由后端做，
 * 错误信息原样显示后端返回的 detail。
 */

/* ==================== 小工具 ==================== */

function $(id) { return document.getElementById(id); }

function setText(id, text) {
  const el = $(id);
  if (el.textContent !== text) el.textContent = text;
}

function setPill(id, text, cls) {
  const el = $(id);
  if (el.textContent !== text) el.textContent = text;
  const want = 'pill ' + (cls || '');
  if (el.className !== want) el.className = want;
}

function fmt(v, digits) {
  return (v === null || v === undefined || Number.isNaN(v)) ? '—' : Number(v).toFixed(digits);
}

function pad2(n) { return String(n).padStart(2, '0'); }

function fmtTime(sec) {
  if (!sec) return '—';
  const d = new Date(sec * 1000);
  return d.getFullYear() + '-' + pad2(d.getMonth() + 1) + '-' + pad2(d.getDate()) + ' ' +
         pad2(d.getHours()) + ':' + pad2(d.getMinutes()) + ':' + pad2(d.getSeconds());
}

function esc(s) {
  return String(s === null || s === undefined ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

const STATUS_CN = {
  idle: '空闲', pending: '排队中', running: '扫描中',
  paused: '已暂停', done: '已完成', aborted: '已中止', failed: '失败'
};

/* ==================== 提示条 ==================== */

let toastTimer = null;

function toast(text, isErr) {
  const el = $('toast');
  el.textContent = text;
  el.className = 'show' + (isErr ? ' err' : '');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(function () { el.className = ''; }, isErr ? 6000 : 2500);
}

/* ==================== 视图 ==================== */

/* 三页：对准（读数 / 手动 / 曲线）、扫描（参数 / 进度 / 点位 / 历史）、
   预览（相机预览 / 已保存的原始帧）。
   后端本来就规定它们互斥（扫描占着设备时手动会被 409 拒、相机也归扫描用），
   所以分页藏起来的不是"还有用的东西"，是"此刻用不了的东西"。
   **这是显示状态，不是权限** —— 真正的拒绝永远在后端。 */
const VIEWS = ['align', 'scan', 'ccd'];
let view = VIEWS[0];

function showView(v) {
  if (VIEWS.indexOf(v) < 0) v = VIEWS[0];
  // **离开**「预览」页就把预览停掉（连后端一起）：图都不显示了，留着只是白占相机。
  // 原来这句写在"进入预览页"那一段里，位置错了 —— 效果是切回来反而把预览掐掉。
  if (view === 'ccd' && v !== 'ccd' && ccdLive) ccdLiveStop();
  view = v;
  for (let i = 0; i < VIEWS.length; i++) {
    const on = VIEWS[i] === v;
    $('view-' + VIEWS[i]).hidden = !on;
    $('tab-' + VIEWS[i]).className = on ? 'tab active' : 'tab';
  }
  // 藏起来的容器宽度是 0：切回来得重新量一次，不然是一张压扁的图
  if (view === 'scan' && chart) chart.setSize({ width: chartWidth($('chart')), height: 220 });
  if (view === 'align' && traceChart) traceChart.setSize({ width: chartWidth($('trace')), height: 220 });
  if (view === 'ccd') {
    ccdStatus();
    loadGrabs();
  }
  location.hash = v;   // 刷新、或者开两个标签各停一页，都靠它
}

/* ==================== 请求 ==================== */

async function request(method, path, body) {
  let resp;
  try {
    resp = await fetch(path, {
      method: method,
      headers: body === undefined ? undefined : { 'Content-Type': 'application/json' },
      body: body === undefined ? undefined : JSON.stringify(body)
    });
  } catch (err) {
    toast('连不上后端：' + err.message, true);
    return null;
  }
  let data = null;
  try { data = await resp.json(); } catch (err) { /* 空响应体 */ }
  if (!resp.ok) {
    toast(detailText(data, resp.status), true);
    return null;
  }
  return data;
}

/* FastAPI 的 422 把 detail 放成 [{loc, msg}, ...]，直接塞进 textContent 会变成 [object Object] */
function detailText(data, status) {
  const d = data && data.detail;
  if (typeof d === 'string') return d;
  if (Array.isArray(d)) {
    return d.map(function (item) {
      const loc = (item.loc || []).filter(function (p) { return p !== 'body' && p !== 'query'; }).join('.');
      return (loc ? loc + '：' : '') + (item.msg || JSON.stringify(item));
    }).join('；');
  }
  if (d) return JSON.stringify(d);
  return 'HTTP ' + status;
}

function get(p) { return request('GET', p); }
function post(p, b) { return request('POST', p, b); }

/* ==================== 状态 ==================== */

const SCAN_HINT = '单向逼近：从起点到终点单调推进，每点从同一侧逼近；起点前会先退让一段再逼近首点。';
const MAX_ROWS = 2000;
// 偏差告警阈值（nm）。实测闭环残差约 ±60 nm，取 3 倍左右标记可疑点。
const DEV_WARN_NM = 200;

let stageState = null;
let scanActive = false;
let stageCaps = null;          // 能力声明（后端发；没到之前按 PI 的文案与可用性走）
let seeded = false;
let viewScanId = null;         // 视图正在展示的扫描（只有成功渲染才写）
let autoHandledScanId = null;  // 自动载入已处理过的扫描 id（成功失败都算处理过，防止每帧重试）
let pinnedScanId = null;       // 用户手动点"查看"钉住的扫描
let loadSeq = 0;               // 载入请求序号：只让最后一次生效
let prevScanStatus = null;
let lastMsgAt = 0;
let chart = null;

// 位置曲线（观察稳定性）。样本只来自后端遥测的环形缓冲 ——
// 前端不自己攒样本：SSE 会把同一帧重复推来，而且切到后台标签就丢样本。
let traceChart = null;
let traceLast = null;      // 最近一次取回的窗口 {from,to,ts[],position[],target[]}
let traceFrom = 0;         // 本次记录锚定的服务器时刻（X 轴零点）
let traceSecs = 10;        // 本次记录时长
let traceStartedAt = 0;    // 本地墙钟（只用于界面上的进度与收工）
let traceUntil = 0;
let traceTimer = null;     // 记录中的取数定时器
let traceActive = false;
let tracePinned = false;   // 手填过范围就不再自动铺满
let lastServerTs = null;   // 最近一帧遥测的服务器时刻（记录锚点）

function deviceReady() {
  return !!stageState && stageState.connected && stageState.servo;
}

/* ==================== SSE ==================== */

function pollLink() {
  const ok = Date.now() - lastMsgAt < 3000;
  setPill('pill-link', ok ? '界面已连接' : '界面已断开', ok ? 'ok' : 'bad');
}

function openStream() {
  const es = new EventSource('/api/events');
  es.onmessage = function (ev) {
    lastMsgAt = Date.now();
    let data;
    try { data = JSON.parse(ev.data); } catch (err) { return; }
    // 曲线窗口的两端都用服务器时刻，锚点也就得用服务器时刻（前端墙钟没对齐）
    if (typeof data.ts === 'number') lastServerTs = data.ts;
    if (data.caps) renderCaps(data.caps);
    if (data.stage) renderStage(data.stage);
    if (data.scan) renderScan(data.scan);
  };
  // 出错时 EventSource 自动重连，这里只让指示灯自己变红
}

/* 能力差异只改文案与可用性，不复制业务逻辑：真正的拒绝永远在后端（409 + 中文原因）。 */
function releaseByServoOff() { return !stageCaps || stageCaps.release_mode === 'servo_off'; }
function hasStopCommand() { return !stageCaps || stageCaps.has_stop_command; }
function velocitySupported() { return !stageCaps || stageCaps.has_velocity; }

function renderCaps(caps) {
  stageCaps = caps;

  $('btn-stop').title = hasStopCommand()
    ? 'STP：立刻停止运动，保持伺服与当前位置。扫描中会同时中止扫描。'
    : '软停：不再下发新目标，并把当前位置写回成新目标。设备没有停止指令，'
      + '已在途的行程拦不住。扫描中会同时中止扫描。';
  $('btn-estop').title = hasStopCommand()
    ? '丢弃排队命令 + STP，并中止扫描。'
    : '丢弃排队命令 + 软停（设备没有 STP，在途行程拦不住），并中止扫描。';
  $('btn-release').title = releaseByServoOff()
    ? '关伺服卸力，台子会回弹。异常振动时用（手册建议）。会同时中止扫描。'
    : '切至开环（卸力）：输出写零，不保证停在原位。异常振动时用。会同时中止扫描。';

  if (!velocitySupported()) {
    $('vel').disabled = true;
    $('vel').value = '';                      // 别把编出来的 0 留在框里（本来就禁用，不影响输入）
    $('vel').placeholder = '本设备无速度指令';
    $('btn-vel').title = '本设备没有速度指令：速度由闭环自身决定，后端会明确拒绝。';
  } else {
    $('vel').disabled = false;
    $('vel').placeholder = '';
    $('btn-vel').title = '';
  }
}

/* ==================== 渲染：设备 ==================== */

function renderStage(st) {
  stageState = st;

  setText('pos', fmt(st.position, 4));
  setText('target', fmt(st.target, 4));
  setText('ontarget', st.on_target ? '是' : '否');
  // 没有速度指令的设备（XMT）st.velocity 恒为 0：编一个 0 出来比留白更糟
  setText('velocity', velocitySupported() ? String(st.velocity) : '无此指令');
  setText('errcode', st.error ? ('读取失败：' + st.error)
                              : (st.error_code ? ('错误码 ' + st.error_code) : '正常'));
  setText('limits', st.travel_min + ' – ' + st.travel_max);

  setPill('pill-dev',
    st.connected ? ((st.stage_type || '控制器') + ' 已连接') : '设备未连接',
    st.connected ? 'ok' : 'bad');
  const keep = releaseByServoOff() ? '伺服保持' : '闭环保持';
  const down = releaseByServoOff() ? '已释放（未保持）' : '已切至开环（未保持）';
  setPill('pill-servo',
    st.overflow ? '过冲 / 溢出' : (st.servo ? keep : down),
    st.overflow ? 'bad' : (st.servo ? 'ok' : 'warn'));

  // 按钮可用性只是提示；真正的拒绝在后端（409 + 中文原因）
  const canMove = st.connected && st.servo && !scanActive;
  $('btn-move').disabled = !canMove;
  $('btn-vel').disabled = !(st.connected && !scanActive) || !velocitySupported();
  $('btn-servo-on').disabled = !st.connected || st.servo || scanActive;
  $('btn-release').disabled = !st.connected;
  $('btn-stop').disabled = !st.connected;
  $('btn-estop').disabled = !st.connected;
  $('btn-connect').disabled = st.connected;
  // 曲线取自遥测缓冲：没连着设备就没有样本。记录中始终可点（要能停下来）
  $('btn-trace').disabled = !traceActive && !st.connected;
  const jog = document.querySelectorAll('[data-jog]');
  for (let i = 0; i < jog.length; i++) jog[i].disabled = !canMove;

  syncTraceFlag();

  if (!seeded && st.connected) {
    seeded = true;
    // 播的是"设备当前值"；没有速度指令的设备没有值可播（caps 没到之前按支持走）
    if (velocitySupported()) $('vel').value = st.velocity;
    $('move-target').value = st.travel_min;
    $('scan-start').value = st.travel_min;
    $('scan-stop').value = Math.min(st.travel_max, st.travel_min + 10);
    $('scan-count').value = 11;
    $('scan-settle').value = 100;
  }
}

/* ==================== 渲染：扫描 ==================== */

function renderScan(sc) {
  const running = sc.status === 'running';
  const paused = sc.status === 'paused';
  scanActive = running || paused;

  const pct = sc.count ? Math.round(sc.index / sc.count * 100) : 0;
  $('scan-bar').style.width = pct + '%';

  if (scanActive) {
    setText('scan-progress', sc.index + ' / ' + sc.count + ' 点（' + pct + '%）');
    setText('scan-hint', '当前点：目标 ' + fmt(sc.target_um, 4) + ' µm　实际 ' +
      (sc.actual_um === null || sc.actual_um === undefined ? '—' : fmt(sc.actual_um, 4) + ' µm'));
  } else {
    setText('scan-progress', sc.scan_id
      ? ('扫描 #' + sc.scan_id + ' ' + (STATUS_CN[sc.status] || sc.status))
      : '未开始');
    setText('scan-hint', SCAN_HINT);
  }
  setText('scan-msg', sc.message || '');

  const cls = running ? 'run'
            : paused ? 'warn'
            : sc.status === 'failed' ? 'bad'
            : sc.status === 'aborted' ? 'warn'
            : 'ok';
  // 滚到下面的点位表时左栏读数会滚出视野：进度放进本来就 sticky 的顶栏里还看得见
  setPill('pill-scan',
    scanActive ? (paused ? '扫描已暂停' : '扫描中') + (sc.count ? ' ' + sc.index + '/' + sc.count : '')
               : (sc.scan_id ? (STATUS_CN[sc.status] || sc.status) : '空闲'),
    cls);

  $('btn-pause').disabled = !running;
  $('btn-resume').disabled = !paused;
  $('btn-abort').disabled = !scanActive;
  $('btn-scan-start').disabled = scanActive || !deviceReady();
  const fields = ['scan-name', 'scan-start', 'scan-stop', 'scan-count', 'scan-settle'];
  for (let i = 0; i < fields.length; i++) $(fields[i]).disabled = scanActive;

  const justFinished = (prevScanStatus === 'running' || prevScanStatus === 'paused') && !scanActive;
  prevScanStatus = sc.status;
  if (justFinished) loadHistory();   // 否则历史表那行一直停在启动时的状态

  // 自动跟随"扫描器最近跑的那个扫描"：每个 scan_id 只处理一次。
  // 用户钉住的是别的扫描时不抢视图，但同样标记为已处理，免得每帧重来。
  const target = sc.scan_id;
  if (target && !scanActive && autoHandledScanId !== target) {
    autoHandledScanId = target;
    if (pinnedScanId === null || pinnedScanId === target) loadScan(target);
  }

  // 相机归扫描用：预览必须停。**后端才是权威**（预览接口在扫描中一律 409），
  // 这里只是不让界面停在一个"看着还在预览"的假象上。
  if (scanActive && ccdLive) ccdLiveStop();
  $('btn-ccd-live').disabled = !ccdAvailable || scanActive;
  $('btn-ccd-grab').disabled = !ccdAvailable || scanActive;

  syncTraceFlag();
}

/* ==================== 点位 ==================== */

function clearPoints() {
  renderPoints([]);
  renderChart([]);
  setText('chart-title', '未载入');
}

async function loadScan(id) {
  const seq = ++loadSeq;
  const d = await get('/api/scans/' + id);
  // 失败不动视图（多半是刚被删掉的扫描）；乱序返回时只认最后一次请求
  if (!d || seq !== loadSeq) return;
  viewScanId = id;
  setText('chart-title', '#' + d.id + (d.name ? ' · ' + d.name : '') + '　' +
    (STATUS_CN[d.status] || d.status) + '　' + d.start_um + ' → ' + d.stop_um + ' µm　' +
    d.count + ' 点' + (d.message ? '　' + d.message : ''));
  renderPoints(d.points);
  renderChart(d.points);
}

function renderPoints(points) {
  const tb = $('points').tBodies[0];
  $('chart-empty').hidden = points.length > 0;
  const frag = document.createDocumentFragment();
  const n = Math.min(points.length, MAX_ROWS);
  for (let i = 0; i < n; i++) {
    const p = points[i];
    const has = p.actual_um !== null && p.actual_um !== undefined;
    const dev = has ? (p.actual_um - p.target_um) * 1000 : null;
    const tr = document.createElement('tr');
    tr.innerHTML =
      '<td>' + p.idx + '</td>' +
      '<td>' + fmt(p.target_um, 4) + '</td>' +
      '<td>' + fmt(p.actual_um, 4) + '</td>' +
      '<td' + (dev !== null && Math.abs(dev) > DEV_WARN_NM ? ' class="bad"' : '') + '>' +
        (dev === null ? '—' : dev.toFixed(0)) + '</td>' +
      '<td>' + (p.on_target ? '是' : '否') + '</td>' +
      '<td>' + (p.settled_ms === null || p.settled_ms === undefined ? '—' : Math.round(p.settled_ms)) + '</td>' +
      '<td>' + (p.image_path
        ? '<img class="thumb" loading="lazy" alt="" src="/data/' + esc(p.image_path) + '">'
        : '—') + '</td>';
    frag.appendChild(tr);
  }
  if (points.length > n) {
    const tr = document.createElement('tr');
    tr.innerHTML = '<td colspan="7" class="hint">只显示前 ' + n + ' 点，共 ' + points.length + ' 点</td>';
    frag.appendChild(tr);
  }
  tb.innerHTML = '';
  tb.appendChild(frag);
}

/* ==================== 曲线：每点偏差（实际 − 目标） ==================== */

function chartWidth(el) {
  return Math.max(320, el.clientWidth || 600);
}

/* uPlot 绘制时是把 points.show 当**函数**调用的（构造那一下会把布尔值包一层）。
   事后把它改回布尔，下一帧绘制就抛 "points.show is not a function"，
   从此整张图不再重绘（连尺度都冻住）—— 所以这里给的一直是函数。 */
function showPointsBelow(max) {
  return function (u) { return u.data[0].length <= max; };
}

function renderChart(points) {
  const xs = [], ys = [];
  for (let i = 0; i < points.length; i++) {
    const p = points[i];
    xs.push(p.idx);
    ys.push((p.actual_um === null || p.actual_um === undefined)
      ? null : (p.actual_um - p.target_um) * 1000);
  }
  const data = [xs, ys];

  if (!chart) {
    if (!points.length) return;
    chart = new uPlot({
      width: chartWidth($('chart')),
      height: 220,
      scales: { x: { time: false } },
      legend: { show: true },
      series: [
        { label: '序号' },
        { label: '偏差 (nm)', stroke: '#2563eb', width: 1.5,
          points: { show: showPointsBelow(200), size: 5 } }
      ],
      axes: [
        { stroke: '#64748b', grid: { stroke: '#e2e8f0' }, ticks: { stroke: '#cbd5e1' } },
        { stroke: '#64748b', grid: { stroke: '#e2e8f0' }, ticks: { stroke: '#cbd5e1' } }
      ]
    }, data, $('chart'));
    new ResizeObserver(function () {
      chart.setSize({ width: chartWidth($('chart')), height: 220 });
    }).observe($('chart'));
  } else {
    chart.setData(data);
  }
}

/* ==================== 位置曲线（观察稳定性） ==================== */

const TRACE_POLL_MS = 200;     // 记录中的取数间隔：后端按 10 Hz 攒样本，取再快也没有新点
const TRACE_POINT_MAX = 300;   // 点比这多就只画线不画点
const TRACE_PROBE_SECS = 10;   // 还没收到遥测帧时，探测服务器时间用的窗口

/* 位置在 20 µm 上下抖几十 nm：小数位不够就看不出变化，按刻度间隔定小数位 */
function traceAxisVals(u, splits) {
  const step = splits.length > 1 ? Math.abs(splits[1] - splits[0]) : 1e-3;
  const d = Math.min(6, Math.max(3, Math.ceil(-Math.log10(step)) + 1));
  return splits.map(function (v) { return v.toFixed(d); });
}

/* 刻度位数一多就会比 uPlot 默认的 50px 宽，**左边首位会被裁掉**
   （实测：80.0040 显示成 4.0040）。按最长的那条留宽度。 */
function traceAxisSize(u, values) {
  let longest = 0;
  for (let i = 0; values && i < values.length; i++) {
    longest = Math.max(longest, String(values[i]).length);
  }
  return Math.max(50, Math.ceil(longest * 8) + 14);   // 12px 字号下数字约 7px/字符，再留间隙
}

function traceBuild() {
  traceChart = new uPlot({
    width: chartWidth($('trace')),
    height: 220,
    scales: { x: { time: false } },
    legend: { show: true },
    // 拖拽缩放和"四个输入框是唯一的准"直接冲突：拖完图变了、输入框没变，下次取数又被丢掉
    cursor: { drag: { x: false, y: false } },
    series: [
      { label: '时间 (s)' },
      { label: '位置 (µm)', stroke: '#2563eb', width: 1.5,
        points: { show: showPointsBelow(TRACE_POINT_MAX), size: 4 } }
    ],
    axes: [
      { stroke: '#64748b', grid: { stroke: '#e2e8f0' }, ticks: { stroke: '#cbd5e1' } },
      { stroke: '#64748b', grid: { stroke: '#e2e8f0' }, ticks: { stroke: '#cbd5e1' },
        values: traceAxisVals, size: traceAxisSize }
    ]
  }, [[], []], $('trace'));
  new ResizeObserver(function () {
    traceChart.setSize({ width: chartWidth($('trace')), height: 220 });
  }).observe($('trace'));
}

function renderTrace() {
  if (!traceChart) traceBuild();
  const w = traceLast, xs = [], ys = [];
  for (let i = 0; w && i < w.ts.length; i++) {
    xs.push(w.ts[i] - traceFrom);   // X 轴：从记录起点起的秒数（服务器时刻相减）
    ys.push(w.position[i]);
  }
  traceChart.setData([xs, ys]);
  applyTraceRange();
}

/* 坐标范围：四个输入框是唯一的准；没被手填过就先按数据铺满 */
function applyTraceRange() {
  // 钉住是个状态，得看得见 —— 顺手也是"范围为什么不动了"的说明
  $('trace-pin').hidden = !tracePinned;
  if (!traceChart) return;
  if (!tracePinned) fitTraceRange();
  const x0 = parseFloat($('trace-xmin').value), x1 = parseFloat($('trace-xmax').value);
  const y0 = parseFloat($('trace-ymin').value), y1 = parseFloat($('trace-ymax').value);
  // 反向或只填了一半就先不套用（画出来是空图），输入框原样留着等人改完
  if (x0 < x1) traceChart.setScale('x', { min: x0, max: x1 });
  if (y0 < y1) traceChart.setScale('y', { min: y0, max: y1 });
}

function fitTraceRange() {
  const w = traceLast;
  if (!w || !w.ts.length) return;      // 还没有数据就别去动输入框
  const n = w.ts.length;
  let lo = w.position[0], hi = w.position[0];
  for (let i = 1; i < n; i++) {
    if (w.position[i] < lo) lo = w.position[i];
    if (w.position[i] > hi) hi = w.position[i];
  }
  const pad = (hi - lo) * 0.05 || 0.0005;   // 一点起伏都没有时给 1 nm 的窗，别退化成一条线
  $('trace-ymin').value = (lo - pad).toFixed(4);
  $('trace-ymax').value = (hi + pad).toFixed(4);
  const span = w.ts[n - 1] - traceFrom;
  $('trace-xmin').value = '0';
  $('trace-xmax').value = (span > 0 ? span : traceSecs).toFixed(3);
}

function renderTraceStats() {
  const w = traceLast, pos = w ? w.position : [], n = pos.length;
  if (!n) {
    ['tr-n', 'tr-span', 'tr-dt', 'tr-mean', 'tr-std', 'tr-pp', 'tr-target']
      .forEach(function (id) { setText(id, '—'); });
    return;
  }
  let sum = 0, lo = pos[0], hi = pos[0];
  for (let i = 0; i < n; i++) {
    sum += pos[i];
    if (pos[i] < lo) lo = pos[i];
    if (pos[i] > hi) hi = pos[i];
  }
  const mean = sum / n;
  let ss = 0;
  for (let i = 0; i < n; i++) ss += (pos[i] - mean) * (pos[i] - mean);
  const span = w.ts[n - 1] - w.ts[0];
  // 样本标准差（除以 n−1）：这些点是同一段过程的采样，不是全部总体
  const std = n > 1 ? Math.sqrt(ss / (n - 1)) : NaN;
  // 目标只有在窗口里没动过时才是一个数；动过就说明这段里有移动，得说实话
  const tgt = w.target[0];
  let fixed = w.target.length === n;
  for (let i = 1; i < n && fixed; i++) {
    if (Math.abs(w.target[i] - tgt) > 5e-5) fixed = false;
  }
  setText('tr-n', String(n));
  setText('tr-span', fmt(span, 2));
  setText('tr-dt', fmt(n > 1 ? span / (n - 1) * 1000 : NaN, 0));
  setText('tr-mean', fmt(mean, 5));
  setText('tr-std', fmt(std * 1000, 1));      // nm：这些数在 µm 里读不出来
  setText('tr-pp', fmt((hi - lo) * 1000, 0));
  setText('tr-target', fixed ? fmt(tgt, 4) : '窗口内有移动');
}

/* 记录时刻：traceFrom 是服务器时刻（同机无偏移）—— 冻住的那段曲线得有年头 */
function fmtClock(sec, withYear) {
  const d = new Date(sec * 1000);
  return (withYear ? d.getFullYear() + '-' : '') +
         pad2(d.getMonth() + 1) + '-' + pad2(d.getDate()) + ' ' +
         pad2(d.getHours()) + ':' + pad2(d.getMinutes()) + ':' + pad2(d.getSeconds());
}

function syncTraceUI() {
  const b = $('btn-trace');
  b.textContent = traceActive ? '停止记录' : ('记录 ' + $('trace-secs').value + ' 秒');
  b.className = traceActive ? 'warn' : 'primary';
  $('trace-secs').disabled = traceActive;
  const n = traceLast ? traceLast.ts.length : 0;
  if (traceActive) {
    // 报实采点数，不承诺采样率：点数与平均间隔以本次实采为准
    setText('trace-title', '记录中 ' + fmt((Date.now() - traceStartedAt) / 1000, 1) +
      ' / ' + traceSecs + ' s · 已取 ' + n + ' 点');
  } else if (n) {
    setText('trace-title', '已记录 ' +
      fmt(traceLast.ts[n - 1] - traceFrom, 2) + ' s · ' + n + ' 点');
  } else {
    setText('trace-title', '未记录');
  }
  const at = $('trace-at');
  at.hidden = !(traceActive || n);
  if (!at.hidden) setText('trace-at', fmtClock(traceFrom) + ' 起');
  syncEmptyHints();
}

/* 没有数据时不留一块空图框（.chart:empty 已经把高度收掉了），给一句人话 */
function syncEmptyHints() {
  $('trace-empty').hidden = !!(traceLast && traceLast.ts.length);
}

/* 曲线是冻住的一段：目标被改过、或扫描正在动台子，就得说明白 ——
   别让人拿旧曲线当现状。判据拿后端给的目标精确比，不用"偏离若干倍噪声"这种前端阈值：
   0.1 µm 的点动也是真实移动，任何阈值要么被噪声触发、要么漏掉它。 */
function syncTraceFlag() {
  const w = traceLast;
  let text = '';
  if (w && w.ts.length && stageState) {
    if (scanActive) {
      text = '扫描进行中，台子在动 —— 这时的曲线不是稳定性记录。';
    } else if (Math.abs(stageState.target - w.target[0]) > 5e-5) {
      text = '目标已改：记录时 ' + fmt(w.target[0], 4) + ' → 现在 ' +
        fmt(stageState.target, 4) + ' µm（曲线是改动前的）';
    }
  }
  $('trace-flag').hidden = !text;
  if (text) setText('trace-flag', text);
}

/* 窗口两端都是绝对时刻：记录结束后迟到的最后一次取数，拿回来的仍是原来那一段 */
function traceUrl() {
  return '/api/trace?from=' + traceFrom + '&to=' + (traceFrom + traceSecs);
}

async function traceStart() {
  if (traceActive) return;
  const secs = parseFloat($('trace-secs').value);
  if (Number.isNaN(secs)) { toast('请填写记录时长', true); return; }
  traceActive = true;
  traceStartedAt = Date.now();
  let from = lastServerTs;
  if (from === null) {
    // 还没收到遥测帧：问一次服务器时间当锚点。前端墙钟与服务器没对齐，不能拿本地的用
    const probe = await get('/api/trace?seconds=' + TRACE_PROBE_SECS);
    if (!traceActive) return;                      // 探针在飞的时候用户点了停止
    if (!probe) { traceActive = false; syncTraceUI(); return; }
    from = probe.now;
  }
  traceFrom = from;
  traceSecs = secs;
  traceUntil = traceStartedAt + secs * 1000;
  traceLast = null;
  renderTrace();
  renderTraceStats();
  syncTraceUI();
  traceTimer = setInterval(traceTick, TRACE_POLL_MS);
  traceTick();                                     // 立刻出一帧，别等第一个间隔
}

async function traceTick() {
  if (!traceActive) return;
  const r = await get(traceUrl());
  if (!traceActive) return;                        // 取数期间用户点了停止
  if (!r) { traceStop(true); return; }             // 取不到就收工，别每 200 ms 弹一次同样的错
  traceLast = r;
  renderTrace();
  renderTraceStats();
  syncTraceUI();
  // 这一帧取的就是完整窗口（两端都定死了），不用再补取一次
  if (Date.now() >= traceUntil) traceStop(true);
}

/* skipFetch：这一帧刚取过或取不到时才跳过补取。
   用户点停止是插在两次取数中间的，最多差 200 ms 的样本，得补上。 */
async function traceStop(skipFetch) {
  if (!traceActive) return;
  const r = skipFetch ? null : await get(traceUrl());
  traceActive = false;
  if (traceTimer !== null) { clearInterval(traceTimer); traceTimer = null; }
  if (r) { traceLast = r; renderTrace(); renderTraceStats(); }
  syncTraceUI();
}

/* ==================== 相机预览 ==================== */

/* 预览就是"最近一帧"：后端按 15 fps 连续取帧并缓存一张 JPEG，
   前端反复设 img.src 去取。取不到（204）就跳过，不排队、不重试。
   **界面不做任何处理**：后端给的就是原始灰度，不拉伸、不伪彩。 */
const CCD_LIVE_MS = 100;      // 10 fps：比后端取帧还快，多出来的请求会拿到同一张
const CCD_FAIL_MAX = 5;       // 连续失败这么多次就自动停，避免刷屏报错

let ccdAvailable = false;
let ccdInfo = null;
let ccdLive = false;
let ccdTimer = null;
let ccdFails = 0;
let ccdLoadedAt = 0;
let ccdFrames = 0;
let grabsData = null;      // 最近一次 /api/grabs 的结果（改名时要拿旧标签比对）

async function ccdStatus() {
  const d = await get('/api/ccd/status');
  if (!d) return null;
  ccdInfo = d;
  ccdAvailable = !!d.available;
  $('btn-ccd-live').disabled = !ccdAvailable || scanActive;
  $('btn-ccd-grab').disabled = !ccdAvailable || scanActive;
  $('ccd-note').textContent = ccdAvailable
    ? '保存的图一律用原生全幅 ' + d.full_roi[2] + '×' + d.full_roi[3] + '，不裁剪；预览也是全幅，看到的就是存下来的那一片。'
    : (d.message || '当前后端没有接入真相机。');
  ccdPaint(d);
  return d;
}

function ccdPaintForm(st) {
  // 输入框只在用户没在编辑时被覆盖：正在填数字的时候被回写会把输入吃掉
  const el = $('ccd-preview-ms');
  if (document.activeElement !== el) el.value = st.exposure_us / 1000;
  if (st.exposure_min_us) {
    // 范围是相机自报的，写进 min/max 让浏览器先拦一道；后端相机层还会再校验（真正的边界在后端）
    const lo = (st.exposure_min_us / 1000).toFixed(3);
    const hi = (st.exposure_max_us / 1000).toFixed(0);
    el.min = lo; el.max = hi;
    $('ccd-exposure-hint').textContent =
      '预览与采图共用这一个曝光，改完立刻生效（预览开着时不停流直接改，实测 1~2 帧内亮度就变）。' +
      '相机自报范围 ' + lo + ' ~ ' + hi + ' ms。' +
      '亮度优先用曝光调：曝光加倍，信号 ×2、噪声只涨 √2 倍；增益固定 0。';
  }
}

function ccdPaint(st) {
  if (!st) return;
  ccdPaintForm(st);
  $('ccd-sub').textContent = st.open
    ? (st.model || '已连接') + (st.serial ? ' · ' + st.serial : '')
    : (ccdAvailable ? '相机未打开' : '不可用');
  setText('ccd-res', st.preview_roi ? st.preview_roi[2] + '×' + st.preview_roi[3] : '—');
  // 这一格是**相机读回的实际值**（不是输入框里填的请求值）：固件会取整，以读回为准
  setText('ccd-exp', st.exposure_us ? (st.exposure_us / 1000).toFixed(2) : '—');
  setText('ccd-gain', (st.gain === undefined || st.gain === null) ? '—' : String(st.gain));
  if (st.last_error) $('ccd-sub').textContent = '出错：' + st.last_error;
}

/* 刷新用 img.src：浏览器直接解码显示，比 fetch+blob 省事，也不用管 URL 回收。
   设成空串会取消上一张还没下完的请求。 */
function ccdTick() {
  if (!ccdLive) return;
  const img = $('ccd-img');
  img.onload = function () {
    ccdFails = 0;
    ccdFrames++;
    const now = performance.now();
    if (ccdLoadedAt) setText('ccd-fps', (1000 / (now - ccdLoadedAt)).toFixed(1));
    ccdLoadedAt = now;
    img.hidden = false;
    $('ccd-empty').hidden = true;
  };
  img.onerror = function () {
    ccdFails++;
    if (ccdFails >= CCD_FAIL_MAX) {
      ccdLiveStop();
      if (ccdFails > 0) toast('预览中断：连续 ' + ccdFails + ' 次取不到帧', true);
    }
  };
  img.src = '/api/ccd/preview.jpg?t=' + Date.now();
}

async function ccdLiveStart(quiet) {
  const btn = $('btn-ccd-live');
  btn.disabled = true;
  const st = await post('/api/ccd/preview?on=true');
  if (!st) { btn.disabled = false; await ccdStatus(); return; }   // 409/503 的提示 request() 已经弹过
  ccdInfo = st;
  ccdLive = true;
  ccdFails = 0;
  ccdFrames = 0;
  ccdLoadedAt = 0;
  btn.textContent = '停止预览';
  btn.className = '';
  btn.disabled = !ccdAvailable || scanActive;
  $('ccd-empty').hidden = false;
  ccdPaint(st);
  ccdTick();
  ccdTimer = setInterval(ccdTick, CCD_LIVE_MS);
  if (!quiet) toast('预览已开（' + st.preview_roi[2] + '×' + st.preview_roi[3] + '，曝光 ' +
    (st.exposure_us / 1000).toFixed(1) + ' ms）');
}

/* 停预览 = 停界面这一侧 **+ 告诉后端停**。
   后端不会因为"没人来取帧"就自己停：owner 线程一直在按 TL_PREVIEW_FPS 取帧，
   只有 preview?on=false 能停它（原来这里写着"浏览器一断自然就不再消耗设备"，是错的）。
   keepalive 让关页面时这个请求也发得出去。 */
function ccdLiveStop() {
  if (ccdTimer) { clearInterval(ccdTimer); ccdTimer = null; }
  if (!ccdLive) return;
  ccdLive = false;
  fetch('/api/ccd/preview?on=false', { method: 'POST', keepalive: true }).catch(function () {});
  const btn = $('btn-ccd-live');
  btn.textContent = '开始预览';
  btn.className = 'primary';
  btn.disabled = !ccdAvailable || scanActive;
  $('ccd-img').src = '';
  $('ccd-img').hidden = true;
  $('ccd-empty').hidden = false;
  setText('ccd-fps', '—');
}

/* 已保存的采集帧：**只管手动保存的那些**（后端登记表里的，不按文件名前缀猜）。
   扫描各点的图归「扫描」页的点位表，不混进来 —— 一堆自动图会把手动存的淹掉。 */
async function loadGrabs() {
  const d = await get('/api/grabs?limit=120');
  if (!d || !d.items) return;
  grabsData = d;
  const box = $('gallery');
  const frag = document.createDocumentFragment();
  for (let i = 0; i < d.items.length; i++) {
    const it = d.items[i];
    const fig = document.createElement('figure');
    const img = document.createElement('img');
    img.className = 'thumb';
    img.loading = 'lazy';
    img.alt = it.name;
    img.dataset.full = it.path;      // 点缩略图 = 直接看大图（原始帧本身）
    img.src = '/api/grabs/thumb?path=' + encodeURIComponent(it.path);

    // 名称：就是**文件名本身**，点一下改名 = 磁盘上真改（后端 rename + 改库记录）。
    // 还是默认名（grab_<时间戳>.png）时显示成时间，一眼知道是哪一帧。
    const name = document.createElement('span');
    const isDefault = /^grab_\d{8}_\d{6}\.png$/.test(it.name);
    name.className = 'gname' + (isDefault ? ' unnamed' : '');
    name.contentEditable = 'true';
    name.spellcheck = false;
    name.dataset.file = it.name;
    name.dataset.shown = isDefault ? fmtClock(it.mtime) : it.name;
    name.textContent = name.dataset.shown;
    name.title = it.name + ' —— 点这里改名（文件真的会被改名）';
    name.addEventListener('keydown', function (ev) {
      if (ev.key === 'Enter') { ev.preventDefault(); name.blur(); }
      if (ev.key === 'Escape') { name.textContent = name.dataset.shown; name.blur(); }
    });
    name.addEventListener('blur', function () { grabRename(name); });

    const cap = document.createElement('figcaption');
    // 曝光是从图片自己身上读出来的（PNG 的 tEXt 块），不是猜的
    cap.textContent = it.exposure_us
      ? '曝光 ' + (it.exposure_us / 1000).toFixed(2) + ' ms'
      : '曝光未记录';

    const del = document.createElement('button');
    del.className = 'gdel';
    del.textContent = '删除';
    del.title = '删掉这一帧（文件和记录一起删）';
    del.dataset.del = it.name;

    fig.appendChild(img);
    fig.appendChild(name);
    fig.appendChild(cap);
    fig.appendChild(del);
    frag.appendChild(fig);
  }
  box.innerHTML = '';
  box.appendChild(frag);
  $('gallery-empty').hidden = d.items.length > 0;
  setText('grabs-count', d.total ? d.total + ' 张' : '—');
}

/* 改名 = **真改文件名**：后端在磁盘上 rename 并把库里的记录一起改掉。
   缺后缀就补 .png；非法字符 / 同名 / 空名字由后端拒绝并给出原因。 */
async function grabRename(el) {
  const old = el.dataset.file;
  const typed = (el.textContent || '').replace(/\s+$/, '').replace(/^[\s\u3000]+/, '').slice(0, 80);
  // 显示的是时间（默认名）时没动过 → 什么都不做
  if (typed === el.dataset.shown) return;
  if (!typed) { el.textContent = el.dataset.shown; return; }
  const r = await request('POST', '/api/grabs/rename?name=' + encodeURIComponent(old) +
                                 '&new_name=' + encodeURIComponent(typed));
  if (!r) { el.textContent = el.dataset.shown; return; }    // 失败原因 request() 已经弹过
  toast('已改名为 ' + r.name + '（磁盘上的文件也改了）');
  loadGrabs();
}

async function grabDelete(name) {
  if (!confirm('删掉这一帧 ' + name + '？文件和记录都会删，不能恢复。')) return;
  if (await request('DELETE', '/api/grabs/' + encodeURIComponent(name))) {
    toast('已删除 ' + name);
    loadGrabs();
  }
}

async function ccdSetExposure() {
  const ms = parseFloat($('ccd-preview-ms').value);
  if (Number.isNaN(ms) || ms <= 0) { toast('请填写曝光（ms，正数）', true); return; }
  const st = await post('/api/ccd/exposure?exposure_us=' + Math.round(ms * 1000));
  if (!st) return;
  ccdPaint(st);
  // 显示相机读回的实际值：固件会取整，界面说实话
  toast('曝光设为 ' + (st.exposure_us / 1000).toFixed(2) + ' ms（预览与采图都用它）');
  if (ccdLive) ccdTick();   // 立刻换一张，别等下一个节拍
}

async function ccdGrab() {
  const r = await post('/api/ccd/capture');
  if (!r) return;
  toast('已保存 ' + r.path + '（' + r.width + '×' + r.height + '，曝光 ' +
    (r.exposure_us / 1000).toFixed(2) + ' ms，增益 ' + r.gain + '，均值 ' + r.mean.toFixed(1) + '）');
  loadGrabs();     // 存完立刻出现在下面的列表里
  loadHistory();
}

/* ==================== 历史 ==================== */

async function loadHistory() {
  const list = await get('/api/scans');
  if (!list) return;
  const tb = $('history').tBodies[0];
  const frag = document.createDocumentFragment();
  for (let i = 0; i < list.length; i++) {
    const s = list[i];
    const tr = document.createElement('tr');
    tr.innerHTML =
      '<td>' + s.id + '</td>' +
      '<td>' + (esc(s.name) || '—') + '</td>' +
      '<td>' + s.start_um + ' → ' + s.stop_um + '</td>' +
      '<td>' + s.count + '</td>' +
      '<td>' + s.done + '</td>' +
      '<td>' + (STATUS_CN[s.status] || esc(s.status)) + '</td>' +
      '<td>' + fmtTime(s.created_at) + '</td>' +
      '<td><button class="ghost" data-open="' + s.id + '">查看</button> ' +
          '<button class="ghost" data-del="' + s.id + '">删除</button></td>';
    frag.appendChild(tr);
  }
  if (list.length >= 50) {
    const tr = document.createElement('tr');
    tr.innerHTML = '<td colspan="8" class="hint">最多显示最近 50 次扫描</td>';
    frag.appendChild(tr);
  }
  tb.innerHTML = '';
  tb.appendChild(frag);
}

/* ==================== 交互 ==================== */

function wire() {
  $('btn-move').onclick = async function () {
    const v = parseFloat($('move-target').value);
    if (Number.isNaN(v)) { toast('请填写目标位置', true); return; }
    const r = await post('/api/move', { target_um: v });
    if (r) toast(r.clamped ? ('超出量程，后端已夹到 ' + r.target_um + ' µm') : ('移动到 ' + r.target_um + ' µm'));
  };

  const jog = document.querySelectorAll('[data-jog]');
  for (let i = 0; i < jog.length; i++) {
    jog[i].onclick = async function () {
      const d = parseFloat(jog[i].dataset.jog);
      const r = await post('/api/jog', { delta_um: d });
      if (r) toast('点动 ' + d + ' µm → ' + r.target_um + ' µm');
    };
  }

  $('btn-vel').onclick = async function () {
    const v = parseFloat($('vel').value);
    if (Number.isNaN(v)) { toast('请填写速度', true); return; }
    const r = await post('/api/velocity', { velocity: v });
    if (r) toast('速度设为 ' + r.velocity + ' µm/s');
  };

  $('btn-connect').onclick = async function () {
    const r = await post('/api/connect');
    if (r) toast(r.connected ? ('设备已连接：' + r.stage_type) : '仍未连接，检查控制器电源与 USB');
  };

  $('btn-servo-on').onclick = async function () {
    const r = await post('/api/servo', { on: true });
    if (r) toast((releaseByServoOff() ? '伺服已开' : '闭环已开')
      + '，按当前位置 ' + fmt(r.position_um, 4) + ' µm 保持');
  };

  $('btn-release').onclick = async function () {
    if (await post('/api/release')) {
      toast(releaseByServoOff() ? '已释放：伺服关闭，台子回弹'
                                : '已释放：切至开环、输出写零（不保证停在原位）');
      loadHistory();
    }
  };

  $('btn-stop').onclick = async function () {
    const r = await post('/api/stop');
    if (r) {
      // 后端返回 stop=hard/soft，界面按它说实话，别一律写"保持伺服"
      const how = r.stop === 'soft' ? '软停：不再下发新目标（在途行程拦不住）'
                                    : '已停止（保持伺服）';
      toast(r.scan_aborted ? how + '，扫描同时被中止' : how);
    }
    loadHistory();
  };

  $('btn-estop').onclick = async function () {
    if (await post('/api/estop')) {
      toast(hasStopCommand() ? '急停已发出' : '急停已发出（软停：在途行程拦不住）');
      loadHistory();
    }
  };

  $('btn-scan-start').onclick = async function () {
    // 预览占着相机时后端会 409 拒掉扫描，这里顺手先关掉，省得用户看到一句冲突报错。
    // 这只是顺手：**后端自己也会在开始扫描前关预览**（POST /api/scans 里），
    // 所以界面状态不准也不会让扫描和预览抢同一台相机。
    if (ccdLive) {
      ccdLiveStop();
      toast('已关掉相机预览（扫描要用相机）');
    }
    const body = {
      name: $('scan-name').value.trim(),
      start_um: parseFloat($('scan-start').value),
      stop_um: parseFloat($('scan-stop').value),
      count: parseInt($('scan-count').value, 10),
      settle_ms: parseInt($('scan-settle').value, 10)
    };
    const vals = [body.start_um, body.stop_um, body.count, body.settle_ms];
    for (let i = 0; i < vals.length; i++) {
      if (Number.isNaN(vals[i])) { toast('扫描参数没填完整', true); return; }
    }
    if (await post('/api/scans', body)) {
      pinnedScanId = null;   // 新扫描的结果优先，别被之前钉住的视图挡住
      showView('scan');      // 参数、进度、点位都在那一页；之后用户切走就不再抢
      toast('扫描已启动，关掉页面也会继续跑');
      loadHistory();
    }
  };

  const actions = { pause: '已暂停', resume: '已继续', abort: '已中止' };
  Object.keys(actions).forEach(function (action) {
    $('btn-' + action).onclick = async function () {
      if (await post('/api/scans/control', { action: action })) {
        toast(actions[action]);
        if (action === 'abort') loadHistory();
      }
    };
  });

  $('tab-align').onclick = function () { showView('align'); };
  $('tab-scan').onclick = function () { showView('scan'); };
  $('tab-ccd').onclick = function () { showView('ccd'); };

  $('btn-ccd-live').onclick = function () { if (ccdLive) ccdLiveStop(); else ccdLiveStart(); };
  $('btn-ccd-grab').onclick = ccdGrab;
  $('btn-grabs-refresh').onclick = loadGrabs;
  // 曝光改完立刻生效（数字框用 change，回车或失焦才发，别每敲一个字符就打设备）
  $('ccd-preview-ms').onchange = ccdSetExposure;

  $('btn-trace').onclick = function () { if (traceActive) traceStop(); else traceStart(); };
  $('btn-trace-fit').onclick = function () { tracePinned = false; applyTraceRange(); };
  $('trace-secs').oninput = syncTraceUI;
  ['trace-xmin', 'trace-xmax', 'trace-ymin', 'trace-ymax'].forEach(function (id) {
    // 手填过就不再自动铺满：想用同一个窗口对比两段记录，范围得留得住
    $(id).oninput = function () { tracePinned = true; applyTraceRange(); };
  });

  $('btn-refresh').onclick = loadHistory;

  $('history').addEventListener('click', async function (ev) {
    const b = ev.target.closest('button');
    if (!b) return;
    if (b.dataset.open) {
      pinnedScanId = Number(b.dataset.open);
      loadScan(pinnedScanId);
      return;
    }
    // 手动查看不改 autoHandledScanId：否则自动路径会回头去拉已被删掉的那个扫描
    if (b.dataset.del) {
      const id = Number(b.dataset.del);
      if (!confirm('删除扫描 #' + id + ' 及其全部图像？')) return;
      if (await request('DELETE', '/api/scans/' + id)) {
        loadSeq++;   // 作废在途的载入请求，免得删完又被它渲染回来
        // autoHandledScanId 保持不动：这样自动路径不会回头去拉已删除的扫描
        if (viewScanId === id) { viewScanId = null; clearPoints(); }
        if (pinnedScanId === id) pinnedScanId = null;
        toast('已删除扫描 #' + id);
        loadHistory();
      }
    }
  });

  document.addEventListener('click', function (ev) {
    const t = ev.target;
    if (t.dataset && t.dataset.del) { grabDelete(t.dataset.del); return; }
    if (t.classList && t.classList.contains('gname')) return;   // 点名字是改名，不是看大图
    if (t.classList && t.classList.contains('thumb')) {
      // 直接看大图：**原始帧本身**（16 位 PNG，浏览器一般能直接解码；
      // 万一显示不出来，再改成后端转 JPEG —— 后端已经有那条路）。
      $('lightbox-img').src = t.dataset.full ? '/data/' + t.dataset.full : t.getAttribute('src');
      $('lightbox').hidden = false;
    } else if (t.id === 'lightbox' || t.id === 'lightbox-img') {
      $('lightbox').hidden = true;
      $('lightbox-img').src = '';
    }
  });

  const enterTo = function (id, btn) {
    $(id).addEventListener('keydown', function (e) { if (e.key === 'Enter') $(btn).click(); });
  };
  enterTo('move-target', 'btn-move');
  enterTo('vel', 'btn-vel');
  enterTo('trace-secs', 'btn-trace');
  ['scan-name', 'scan-start', 'scan-stop', 'scan-count', 'scan-settle'].forEach(function (id) {
    enterTo(id, 'btn-scan-start');
  });
}

/* ==================== 启动 ==================== */

wire();
openStream();
renderTraceStats();   // 空统计 + 空态提示；曲线图等第一次记录再建，别摆一个空坐标系
syncTraceUI();
showView(location.hash.slice(1));   // 认 hash：刷新和双标签都停在自己那一页
pollLink();
setInterval(pollLink, 1000);   // 没有这行指示灯会永远停在启动瞬间的"已断开"
loadHistory();
loadGrabs();   // 原始帧列表：存一帧就多一张，开机先列出来
// 相机可用性等切到「预览」那一页再问（首屏不必为一个可能用不到的设备多发一个请求）

// 关页面/刷新时把预览停掉：不然后端会继续按 15 fps 取帧，白占着相机
window.addEventListener('beforeunload', ccdLiveStop);

// 心跳只用于让后端知道界面还在；后端不会因为界面掉线而停扫描
fetch('/api/heartbeat', { method: 'POST' }).catch(function () {});
setInterval(function () {
  fetch('/api/heartbeat', { method: 'POST' }).catch(function () {});
}, 2000);
