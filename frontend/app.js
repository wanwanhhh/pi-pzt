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
  if (view === 'scan') fitChart(chart, 'chart', 220);
  if (view === 'align') fitChart(traceChart, 'trace', 220);
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
let settleSeededFor = '';      // 「稳定延时」是按哪台设备播的种（换设备要重播）
let settleSeededValue = null;  // 播下去的值：框里还是它，说明用户没改过
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

  // 默认等待时长**每台设备不一样**（PI = 到位后的延时，XMT = 唯一的等待），值随 caps 下发。
  // 换设备要重播（不然会留着上一台的默认值）；但框里要是用户改过的值，就不动它。
  const dev = caps.name || '';
  if (dev !== settleSeededFor && typeof caps.default_settle_ms === 'number') {
    const box = $('scan-settle');
    if (String(box.value) === '' || String(box.value) === String(settleSeededValue)) {
      box.value = caps.default_settle_ms;
      settleSeededValue = caps.default_settle_ms;
    }
    settleSeededFor = dev;
  }

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
    // caps 还没到时先按 PI 的默认值预填，等 caps 到了再按设备改（XMT 是 300）
    if (String($('scan-settle').value) === '') {
      $('scan-settle').value = 100;
      settleSeededValue = 100;
    }
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

  // 相机归扫描用由**后端**管（begin_scan → 状态变 held，预览取帧自动让位）。
  // 界面**不许在这里发 on=false**：那会把「用户想要预览」的意图清掉，扫描结束后就接不回来。
  // 按钮可用性：扫描中相机接口一律 409，所以直接禁用（真正的拒绝在后端）。
  if (scanActive) {
    $('btn-ccd-live').disabled = true;
    $('btn-ccd-grab').disabled = true;
    $('btn-ccd-rot').disabled = true;
    $('btn-ccd-reopen').disabled = true;
  } else if (ccdInfo) {
    ccdPaint(ccdInfo);      // 扫描结束后按后端状态把按钮与画面恢复
  }

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
    // 间距 = 相邻两点「实际」之差：实际走了多少，一眼能看出来（显示，不是判断）
    const prev = i > 0 ? points[i - 1] : null;
    const gap = has && prev && prev.actual_um !== null && prev.actual_um !== undefined
      ? p.actual_um - prev.actual_um : null;
    const tr = document.createElement('tr');
    tr.innerHTML =
      '<td>' + p.idx + '</td>' +
      '<td>' + fmt(p.target_um, 4) + '</td>' +
      '<td>' + fmt(p.actual_um, 4) + '</td>' +
      '<td>' + (gap === null ? '—' : fmt(gap, 4)) + '</td>' +
      '<td' + (dev !== null && Math.abs(dev) > DEV_WARN_NM ? ' class="bad"' : '') + '>' +
        (dev === null ? '—' : dev.toFixed(0)) + '</td>' +
      '<td>' + (p.on_target ? '是' : '否') + '</td>' +
      '<td>' + (p.settled_ms === null || p.settled_ms === undefined ? '—' : Math.round(p.settled_ms)) + '</td>' +
      // 点位表的图也走 8 位映射：真机扫描帧同样是 16 位 PNG，直连 /data 会是一片黑
      // （假相机的占位图是 8 位，所以以前看不出问题）。data-full 让点击能进同一套大图。
      '<td>' + (p.image_path
        ? '<img class="thumb" loading="lazy" alt="" data-full="' + esc(p.image_path) +
          '" src="/api/grabs/thumb?path=' + encodeURIComponent(p.image_path) + '">'
        : '—') + '</td>';
    frag.appendChild(tr);
  }
  if (points.length > n) {
    const tr = document.createElement('tr');
    tr.innerHTML = '<td colspan="8" class="hint">只显示前 ' + n + ' 点，共 ' + points.length + ' 点</td>';
    frag.appendChild(tr);
  }
  tb.innerHTML = '';
  tb.appendChild(frag);
}

/* ==================== 曲线：每点偏差（实际 − 目标） ==================== */

/* 320 是"卡片最窄也要能放下坐标轴"的老下限，但预览页的剖面栏在 1280 下只有 279px 可用 ——
   下限比容器还大就直接撑出横向滚动条。220 一样放得下两条轴，留出约 160px 画线。 */
function chartWidth(el) {
  return Math.max(220, el.clientWidth || 600);
}

/* 图跟着卡片长：卡片是 .grow、图区是 flex:1，clientHeight 就是"还剩多少高度"。
   **uPlot 的 height 只算绘图区，图例另占一行**（实测 28px，字号 12px 下）：
   不把它扣掉，整块就比卡片高一行，图例正好压在下面的统计条上
   （实测：587 的框里塞了 615 的内容）。还没建图时按实测值先留一行。
   量不到高度（还没布局 / 图区被 :empty 收成 0）就用回退值，别把图压成一条线。 */
const CHART_LEGEND_PX = 28;

function chartHeight(el, fallback) {
  const lg = el.querySelector('.u-legend');
  const avail = el.clientHeight || ((fallback || 220) + CHART_LEGEND_PX);
  // 下限压到 90：矮屏下卡片只剩一百多像素时，硬撑到 140 会让图比卡片还高（实测差 5px）
  return Math.max(90, avail - ((lg && lg.offsetHeight) || CHART_LEGEND_PX));
}

/* 按卡片当前尺寸重排一张图。尺寸没变就不调 setSize —— 这函数在遥测的每个节拍都会被叫到
   （曲线每 200 ms 一次），没必要每次都让 uPlot 重排。 */
function fitChart(u, id, fallback) {
  if (!u) return;
  const width = chartWidth($(id)), height = chartHeight($(id), fallback);
  const last = u._fitSize;
  if (last && last.width === width && last.height === height) return;
  u._fitSize = { width: width, height: height };
  u.setSize({ width: width, height: height });
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
      height: chartHeight($('chart'), 220),   // 建图时的高度；随后 fitChart 按卡片实际尺寸校正
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
    new ResizeObserver(function () { fitChart(chart, 'chart', 220); }).observe($('chart'));
  } else {
    chart.setData(data);
  }
  // 点位表/提示条一多一少，图区的可用高度就变了，跟着重排一次
  fitChart(chart, 'chart', 220);
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
    height: chartHeight($('trace'), 220),   // 建图时的高度；随后 fitChart 按卡片实际尺寸校正
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
  new ResizeObserver(function () { fitChart(traceChart, 'trace', 220); }).observe($('trace'));
}

function renderTrace() {
  if (!traceChart) traceBuild();
  const w = traceLast, xs = [], ys = [];
  for (let i = 0; w && i < w.ts.length; i++) {
    xs.push(w.ts[i] - traceFrom);   // X 轴：从记录起点起的秒数（服务器时刻相减）
    ys.push(w.position[i]);
  }
  traceChart.setData([xs, ys]);
  fitChart(traceChart, 'trace', 220);   // 提示条显隐会改图区高度，每帧跟一次
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
const CCD_LIVE_MS = 100;      // 取帧节拍 10 fps（比后端 15 fps 慢一点，多出来的请求拿同一张）
const CCD_POLL_EVERY = 5;     // 每 5 拍才问一次状态（2 Hz）——状态是内存快照，没必要每帧都问
// 「正在打开…」提示里写的等待上限：后端建会话时最多等这么久（config.TL_OPEN_WAIT_S）。
// 这里只是文案用的数字，不参与任何判断 —— 真正的超时在后端。
const CCD_OPEN_WAIT_S = 8;

let ccdAvailable = false;
let ccdInfo = null;
// **界面不持有意图**：「要不要预览」是后端的状态（state === 'preview'），这里只是镜像它。
// 旧代码在前端又存了一份 ccdWanted，于是「谁说了算」有两个答案 —— 掉线后就对不上。
let ccdLive = false;
let ccdTimer = null;
let ccdTicks = 0;
let ccdFails = 0;
let ccdLoadedAt = 0;
let ccdFrames = 0;
let grabsData = null;      // 最近一次 /api/grabs 的结果（改名时要拿旧标签比对）

async function ccdStatus() {
  const d = await get('/api/ccd/status');
  if (!d) return null;
  ccdAvailable = !!d.available;
  ccdPaint(d);          // 状态、按钮、画面收放都在这里按 state 走
  // **这段文字只能在这里写一次**：以前 index.html 里也写了一份，结果被这一行整段覆盖，
  // 界面上永远看不到新加的口径说明（评审抓到的）。数字一律取后端下发的值，别在 JS 里写死。
  $('ccd-note').innerHTML = ccdAvailable
    ? '保存一律<b>原生全幅</b> ' + d.full_roi[2] + '×' + d.full_roi[3] + '、16 位、不裁剪也不拉伸；' +
      '预览也是全幅，看到的就是存下来的那一片。曝光（相机读回值）写在<b>图片自己身上</b>，列表里直接能看到。<br>' +
      '质心是<b>整幅图的强度加权重心、不做任何处理</b>（不扣背景、不设阈值、不开窗）：' +
      '背景也照权重参与，所以光斑占总强度越小，它越偏向背景的重心；峰值到 ' + d.saturation_adu + ' 就是饱和。<br>' +
      '质心读数永远是<b>传感器坐标</b>（与保存的 PNG 同一套，<b>转预览不影响它</b>），' +
      '十字线则跟着预览朝向画到当前画面上。'
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

/* 后端给的质心永远是**传感器坐标**（与保存的 PNG 同一套），所以读数不受预览朝向影响 ——
   拿这个数去对文件里的像素，不用做任何换算。十字线要画在"看到的那一帧"上，所以这里按当前
   朝向做一次坐标换算（显示，不是计算）。映射与后端
   test_clockwise_rotation_maps_the_centroid_this_way 是同一张表（顺时针）：
     90°：(x', y') = (H-1-y, x)，显示尺寸 (H, W)；180°：(W-1-x, H-1-y)；270°：(y, W-1-x)。 */
function ccdDisplayPoint(c, rotation) {
  const k = (((rotation || 0) % 360) + 360) % 360;
  const W = c.width, H = c.height;
  if (k === 90) return { x: H - 1 - c.cy, y: c.cx, w: H, h: W };
  if (k === 180) return { x: W - 1 - c.cx, y: H - 1 - c.cy, w: W, h: H };
  if (k === 270) return { x: c.cy, y: W - 1 - c.cx, w: H, h: W };
  return { x: c.cx, y: c.cy, w: W, h: H };
}

/* 十字线 + 读数。**坐标由后端在原生 16 位帧上算好**，这里只按比例摆到图上
   （显示，不是计算）。left/top 用百分比：十字线的父元素与图片同尺寸，缩放不会偏。
   后端给的是整幅图的强度加权重心，不扣背景 —— 这个口径写在 ccd-note 的提示里。 */
function ccdPaintCentroid(st) {
  const cross = $('ccd-cross');
  const c = (ccdLive && st) ? st.centroid : null;
  if (!c || c.cx === null || c.cx === undefined || !c.width || !c.height) {
    cross.hidden = true;
    setText('ccd-cen', '—'); setText('ccd-sum', '—'); setText('ccd-sat', '—');
    $('ccd-sat').className = '';
    $('ccd-stack').className = 'ccdstack';
    return;
  }
  const p = ccdDisplayPoint(c, st.rotation);
  // 90°/270° 时画面是竖的：给容器加个 class，由 CSS 按高度收宽度（详见 style.css）
  $('ccd-stack').className = 'ccdstack' + ((st.rotation === 90 || st.rotation === 270) ? ' tall' : '');
  cross.hidden = false;
  cross.style.left = (p.x / p.w * 100).toFixed(3) + '%';
  cross.style.top = (p.y / p.h * 100).toFixed(3) + '%';
  setText('ccd-cen', c.cx.toFixed(2) + ', ' + c.cy.toFixed(2));
  setText('ccd-sum', c.sum.toExponential(2));
  // 峰值到满量程（1022）就是饱和：对称光斑削顶不偏，但落在强度梯度上时会往亮侧偏
  const sat = $('ccd-sat');
  sat.textContent = c.peak + ' / ' + c.saturated;
  sat.className = c.saturated ? 'on' : '';
}

/* 旋转：转的是**看的方向**。后端把预览帧转过来显示（90° 整数倍是精确置换，不重采样、不丢数），
   保存的原生帧永远是传感器朝向；质心读数跟着预览一起转（看到什么就读到什么）。
   连点四下一圈：0 → 90 → 180 → 270 → 0。 */
async function ccdRotate() {
  const cur = (ccdInfo && ccdInfo.rotation) || 0;
  const next = (cur + 90) % 360;
  const st = await post('/api/ccd/rotation?deg=' + next);
  if (!st) return;                       // 失败原因 request() 已经弹过
  ccdInfo = st;
  ccdPaint(st);
  toast('预览朝向 ' + (next ? next + '°' : '0°（传感器原始）') + '；保存的文件不受影响');
}

/* 重开相机（手动）：USB 接触不良 / 相机报错之后，后端不会自己试 —— 点这里丢掉旧句柄重开。
   预览原本开着就接着开；失败原因照旧由 request() 弹出来（电源、ThorCam 占用、USB）。 */
async function ccdReopen() {
  const btn = $('btn-ccd-reopen');
  btn.disabled = true;
  setText('ccd-sub', '正在重开相机…（最多等 ' + CCD_OPEN_WAIT_S + ' s）');
  const st = await post('/api/ccd/reopen');
  if (!st) { await ccdStatus(); return; }        // 失败原因 request() 已经弹过
  ccdFails = 0;
  ccdPaint(st);
  const who = (st.model || '相机') + (st.serial ? '（' + st.serial + '）' : '');
  // 预览要不要继续，由**后端**的意图决定（state 就是答案）：它记着「用户想要预览」，
  // 重开成功后自己会接着取帧；这里只如实转述。
  toast(ccdLive ? ('相机已重开：' + who + '，预览继续') : ('相机已重开：' + who));
}

/* 界面按**后端的状态**写字，不自己推断：off / idle / opening / preview / held / failed。
   旧代码靠 st.open + last_error 拼，掉线时拼出「已连接 + 出错」这种自相矛盾的样子。 */
const CCD_STATE_TEXT = {
  off: '未接入相机',
  idle: '相机空闲（未打开）',
  opening: '正在打开相机…',
  held: '相机归扫描用（预览让位）',
};

function ccdPaint(st) {
  if (!st) return;
  ccdInfo = st;
  const state = st.state || (st.available ? 'idle' : 'off');
  ccdLive = (state === 'preview');            // 镜像后端：界面不持有意图
  ccdPaintForm(st);
  setText('ccd-rot', st.rotation ? st.rotation + '°（仅预览）' : '0°（传感器原始）');
  ccdPaintCentroid(st);
  $('ccd-sub').textContent = (state === 'preview')
    ? ((st.model || '已连接') + (st.serial ? ' · ' + st.serial : ''))
    : (state === 'failed')
      ? ('掉线：' + (st.failure || '原因未知') + ' —— 点「重开相机」')
      : (CCD_STATE_TEXT[state] || state);
  setText('ccd-res', st.preview_roi ? st.preview_roi[2] + '×' + st.preview_roi[3] : '—');
  // 这一格是**相机读回的实际值**（不是输入框里填的请求值）：固件会取整，以读回为准
  setText('ccd-exp', st.exposure_us ? (st.exposure_us / 1000).toFixed(2) : '—');
  setText('ccd-gain', (st.gain === undefined || st.gain === null) ? '—' : String(st.gain));

  // 不是预览态就把画面收掉：浏览器的 <img> 在解码失败时会把**上一帧留在屏幕上** ——
  // 那正是「旧帧冒充实时画面」的前端那一半。
  if (!ccdLive) {
    $('ccd-img').src = '';
    $('ccd-img').hidden = true;
    $('ccd-empty').hidden = false;
    setText('ccd-fps', '—');
  }
  syncCcdTimer();
  const busy = scanActive || !ccdAvailable || state === 'opening';
  $('btn-ccd-live').disabled = busy;
  $('btn-ccd-live').textContent = ccdLive ? '停止预览' : '开始预览';
  $('btn-ccd-live').className = ccdLive ? '' : 'primary';
  $('btn-ccd-grab').disabled = busy;
  $('btn-ccd-rot').disabled = busy;
  $('btn-ccd-reopen').disabled = busy;
}

/* 刷新用 img.src：浏览器直接解码显示，比 fetch+blob 省事，也不用管 URL 回收。
   设成空串会取消上一张还没下完的请求。 */
function ccdTick() {
  if (!ccdLive) return;
  // 状态是内存快照：按 2 Hz 问就够，不必跟着 10 fps 的取帧节拍每帧都问一次
  if (++ccdTicks % CCD_POLL_EVERY === 0) ccdStatus();
  if (profPoint.preview) profFetch('preview');   // 轮廓图：点在哪儿就一直跟着刷新
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
    // **界面不自己停**：拿不到帧就如实显示「取不到帧」，让状态（下一次 ccdStatus）说话。
    // 旧代码在这里 POST on=false 自动停 —— 于是后端以为「用户不要预览了」，
    // 重开之后自然也不接着取帧，界面看着就像「重开没用」。
    ccdFails++;
    setText('ccd-fps', '取不到帧 ×' + ccdFails);
  };
  img.src = '/api/ccd/preview.jpg?t=' + Date.now();
}

function syncCcdTimer() {
  // 取帧计时器只由 state 决定：是 preview 就取帧，别的状态一律停（不再有两份真相）
  if (ccdLive && ccdTimer === null) {
    ccdTicks = 0;
    ccdTick();
    ccdTimer = setInterval(ccdTick, CCD_LIVE_MS);
  } else if (!ccdLive && ccdTimer !== null) {
    clearInterval(ccdTimer);
    ccdTimer = null;
  }
}

async function ccdLiveStart(quiet) {
  const btn = $('btn-ccd-live');
  btn.disabled = true;
  setText('ccd-sub', '正在打开相机…（最多等 ' + CCD_OPEN_WAIT_S + ' s）');
  const st = await post('/api/ccd/preview?on=true');
  if (!st) { await ccdStatus(); return; }    // 409/503 的提示 request() 已经弹过
  ccdFails = 0;
  ccdFrames = 0;
  ccdLoadedAt = 0;
  ccdPaint(st);                               // state=preview → syncCcdTimer 开始取帧
  if (!quiet && ccdLive) {
    toast('预览已开（' + st.preview_roi[2] + '×' + st.preview_roi[3] + '，曝光 ' +
      (st.exposure_us / 1000).toFixed(1) + ' ms）');
  }
}

/* 停预览 = 停界面这一侧 **+ 告诉后端停**。
   后端不会因为"没人来取帧"就自己停：owner 线程一直在按 TL_PREVIEW_FPS 取帧，
   只有 preview?on=false 能停它（原来这里写着"浏览器一断自然就不再消耗设备"，是错的）。
   keepalive 让关页面时这个请求也发得出去。 */
function ccdLiveStop() {
  // 「停」= 告诉后端不要预览了（后端会顺手把会话收掉）。界面这边由下一次状态刷新收尾。
  fetch('/api/ccd/preview?on=false', { method: 'POST', keepalive: true })
    .then(function () { return ccdStatus(); })
    .catch(function () {});
  if (ccdTimer) { clearInterval(ccdTimer); ccdTimer = null; }
  ccdLive = false;
  $('ccd-img').src = '';
  $('ccd-img').hidden = true;
  $('ccd-empty').hidden = false;
  setText('ccd-fps', '—');
  ccdPaintCentroid(null);      // 十字线和质心读数一起收起来（停预览后那个数是上一帧的残留）
  profClear('preview');        // 剖面同理：预览停了就没有"实时"，别留着上次的线
}

/* ==================== 轮廓图：过点的整行 / 整列 ====================
   数据来自后端**原生 16 位**帧（预览那条路）或保存的 PNG（图库那条路），一律**不做处理**：
   不平滑、不扣背景、不归一化 —— 画出来是什么就是什么。切线方向跟着预览朝向走（后端在旋转后的
   视图上切），所以转了 90° 之后"水平"仍是你眼睛看到的水平。 */
const PROF_HOSTS = {
  preview: { h: 'prof-h', v: 'prof-v', cut: 'ccd-cut', cv: 'ccd-cut-v', ch: 'ccd-cut-h', where: 'prof-where' },
  lightbox: { h: 'lb-prof-h', v: 'lb-prof-v', cut: 'lb-cut', cv: 'lb-cut-v', ch: 'lb-cut-h', where: null },
};
const profPoint = { preview: null, lightbox: null };   // {x, y}；大图那条另带 path
const profCharts = {};                                 // host -> {h, v}
const profSeq = { preview: 0, lightbox: 0 };           // 迟到的响应不许覆盖新的

/* 点击位置 → 图像像素坐标（纯算术，便于测）：box 是图片在屏幕上的矩形，nw/nh 是它的像素尺寸 */
function profPixel(clientX, clientY, box, nw, nh) {
  if (!nw || !nh || !box || !box.width || !box.height) return null;
  const fx = (clientX - box.left) / box.width;
  const fy = (clientY - box.top) / box.height;
  if (fx < 0 || fx > 1 || fy < 0 || fy > 1) return null;      // 点在图上才算
  return {
    x: Math.min(nw - 1, Math.max(0, Math.round(fx * nw))),
    y: Math.min(nh - 1, Math.max(0, Math.round(fy * nh))),
  };
}

function profBuild(host) {
  const cfg = PROF_HOSTS[host];
  const mk = function (id, label, color) {
    return new uPlot({
      width: chartWidth($(id)),
      height: chartHeight($(id), 150),
      scales: { x: { time: false } },
      legend: { show: true },
      cursor: { drag: { x: false, y: false } },
      series: [{ label: label }, { label: 'ADU', stroke: color, width: 1 }],
      axes: [
        { stroke: '#64748b', grid: { stroke: '#e2e8f0' }, ticks: { stroke: '#cbd5e1' } },
        { stroke: '#64748b', grid: { stroke: '#e2e8f0' }, ticks: { stroke: '#cbd5e1' } },
      ],
    }, [[], []], $(id));
  };
  profCharts[host] = {
    h: mk(cfg.h, '水平（整行）像素', '#2563eb'),
    v: mk(cfg.v, '垂直（整列）像素', '#0d9488'),
  };
  // 剖面图也是吃满卡片高度的：卡片尺寸一变（切页、缩放窗口）就跟着重排
  new ResizeObserver(function () {
    fitChart(profCharts[host].h, cfg.h, 150);
    fitChart(profCharts[host].v, cfg.v, 150);
  }).observe($(cfg.h));
}

/* 把后端给的剖面上图：两张图各一条线，并在图上画出那两条切线 */
function profApply(host, p) {
  const cfg = PROF_HOSTS[host];
  if (!p || !p.horizontal || !p.vertical) return;
  if (!profCharts[host]) profBuild(host);
  const xsH = [], ysH = [], xsV = [], ysV = [];
  for (let i = 0; i < p.horizontal.length; i++) { xsH.push(i); ysH.push(p.horizontal[i]); }
  for (let i = 0; i < p.vertical.length; i++) { xsV.push(i); ysV.push(p.vertical[i]); }
  profCharts[host].h.setData([xsH, ysH]);
  profCharts[host].v.setData([xsV, ysV]);
  $(cfg.cut).hidden = false;
  $(cfg.cv).style.left = (p.x + 0.5) / p.width * 100 + '%';    // 画在像素中心
  $(cfg.ch).style.top = (p.y + 0.5) / p.height * 100 + '%';
  if (cfg.where) {
    setText(cfg.where, '(' + p.x + ', ' + p.y + ') · ' + p.width + '×' + p.height +
      (p.rotation ? ' · 预览 ' + p.rotation + '°' : '') + ' · ' + p.bits + ' 位');
  }
}

async function profFetch(host) {
  const pt = profPoint[host];
  if (!pt) return;
  const seq = ++profSeq[host];
  const url = host === 'lightbox'
    ? '/api/image/profile?path=' + encodeURIComponent(pt.path) + '&x=' + pt.x + '&y=' + pt.y
    : '/api/ccd/profile?x=' + pt.x + '&y=' + pt.y;
  const p = await get(url);
  if (seq !== profSeq[host]) return;        // 慢响应回来时可能已经点了别处
  if (p) profApply(host, p);
}

function profClear(host) {
  const cfg = PROF_HOSTS[host];
  profPoint[host] = null;
  $(cfg.cut).hidden = true;
  if (profCharts[host]) {
    profCharts[host].h.setData([[], []]);
    profCharts[host].v.setData([[], []]);
  }
  if (cfg.where) setText(cfg.where, '在预览图上点一下');
}

function grabMeta(path) {
  const items = (grabsData && grabsData.items) || [];
  for (let i = 0; i < items.length; i++) if (items[i].path === path) return items[i];
  return null;
}

/* 大图：**走 8 位映射**（后端 >>2 → JPEG，与预览同一条口径），不是直接给浏览器看 16 位 PNG ——
   16 位 PNG 里的值只占 0~1022（满量程 65535 的 1.6%），浏览器按满量程渲染就是一片黑
   （实测：均值 352 的帧在屏幕上只有 1.4/255）。要像素真值就走下面那个链接拿原始文件。 */
function closeLightbox() {
  $('lightbox').hidden = true;
  $('lightbox-img').src = '';
  profClear('lightbox');
}

/* 一行元数据文案（大图说明条用） */
function metaLine(m) {
  return (m && m.exposure_us ? '曝光 ' + (m.exposure_us / 1000).toFixed(2) + ' ms' : '曝光未记录') +
    ' · ' +
    (m && m.centroid
      ? '质心(传感器) ' + m.centroid[0].toFixed(2) + ', ' + m.centroid[1].toFixed(2)
      : '质心未记录');
}

let lightboxToken = 0;   // 慢响应回来时可能已经翻到别的图了：只认最后一次
let lightboxPath = '';   // 当前大图对应的 data/ 相对路径（点图取剖面要用）

async function showLightbox(full, fallbackSrc, meta) {
  const cap = $('lightbox-cap');
  const raw = $('lightbox-raw');
  const line = $('lightbox-meta');
  lightboxToken++;
  if (!full) {
    // 没有相对路径（比如别处塞进来的图）：只显示给来的 src，说明条留空 ——
    // **绝不能留上一张的数字**，那正是"不猜值"的反面
    $('lightbox-img').src = fallbackSrc || '';
    line.textContent = '';
    cap.hidden = true;
    $('lightbox').hidden = false;
    return;
  }
  const enc = full.split('/').map(encodeURIComponent).join('/');
  lightboxPath = full;              // 点图取剖面时要拿它去问后端
  profClear('lightbox');            // 换了一张图：上一个点的剖面与切线都不算了
  $('lightbox-img').src = '/api/grabs/thumb?path=' + encodeURIComponent(full) + '&max_side=1440';
  raw.href = '/data/' + enc;        // 原始 16 位 PNG：下载/本地看，别指望浏览器显示
  line.textContent = meta ? metaLine(meta) : '读取中…';
  cap.hidden = false;
  $('lightbox').hidden = false;
  if (meta) return;                 // 图库里的帧：列表已经把它带回来了
  // 点位表的扫描帧不在图库列表里：它的曝光/质心同样写在文件自己身上，去问后端要一份
  const token = lightboxToken;
  const m = await get('/api/image/meta?path=' + encodeURIComponent(full));
  if (token === lightboxToken && !$('lightbox').hidden) line.textContent = metaLine(m);
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
    // max_side=520 是**余量**，不是修复：格子里内容约 123×92 CSS px，按"源长边 ≥ 2× 设备像素"
    // 算，DPR=1 时 260 就够、DPR=2 要约 490，取 520 覆盖到 DPR≈2。
    // 实测（同一真帧、对理想面积平均算 RMSE）：260/520/1440 三档配浏览器**平滑**缩放都是
    // 1.43~1.53 —— 档位本身差别很小；把格子里的图弄花的其实是 CSS 的
    // image-rendering: pixelated（最近邻抽样，源图越大越糟），那个删掉了（见 style.css）。
    img.src = '/api/grabs/thumb?path=' + encodeURIComponent(it.path) + '&max_side=520';

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
    // 曝光与质心都是从图片自己身上读出来的（PNG 的 tEXt 块），不是猜的；
    // 质心是**传感器坐标**，与文件里的像素同一套（转预览不影响它，老图没有就写"未记录"）
    cap.innerHTML =
      (it.exposure_us ? '曝光 ' + (it.exposure_us / 1000).toFixed(2) + ' ms' : '曝光未记录') +
      '<br>' +
      (it.centroid
        ? '质心 ' + it.centroid[0].toFixed(2) + ', ' + it.centroid[1].toFixed(2)
        : '质心未记录');

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
    (r.exposure_us / 1000).toFixed(2) + ' ms，质心 ' +
    (r.centroid ? r.centroid[0].toFixed(2) + ', ' + r.centroid[1].toFixed(2) : '—') +
    '，增益 ' + r.gain + '，均值 ' + r.mean.toFixed(1) + '）');
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

/* ==================== 分栏拖动 ==================== */

/* 分栏线拖的是**显示**，不是业务：只改 .view 上的一个 CSS 变量（grid 模板读它），
   后端不知道也不关心你眼睛怎么分的栏。所以这里没有一条限位/业务判断。
   值存在 localStorage（按视图 + 变量名），双击复位、方向键微调。
   图不用管：每一张图都有 ResizeObserver，列宽一变自己按新尺寸重排。 */
const LAYOUT_KEY = 'layout.';

function layoutSave(viewId, name, px) {
  try { localStorage.setItem(LAYOUT_KEY + viewId + name, String(px)); } catch (err) { /* 隐私模式禁写：不影响本次拖动 */ }
}

function layoutLoad(viewId, name) {
  try { return parseFloat(localStorage.getItem(LAYOUT_KEY + viewId + name)) || 0; } catch (err) { return 0; }
}

/* 这条线管的那一格现在多大。**管哪一侧由 data-of 说了算**，不能靠猜：
   对准/扫描的任务栏在线的左边（prev），预览页的剖面栏、图库栏和底部的历史带在线的右边/下边（next）。
   猜错不会报错，只会"拖了没反应/一拖就顶到头"—— 所以写在 HTML 上。 */
function gutterBox(g, axis, of) {
  const box = of === 'next' ? g.nextElementSibling : g.previousElementSibling;
  if (!box) return 0;
  const r = box.getBoundingClientRect();
  return Math.round(axis === 'x' ? r.width : r.height);
}

function gutterApply(g, view, name, px, save) {
  const min = parseFloat(g.dataset.min) || 120;
  const max = parseFloat(g.dataset.max) || 900;
  const v = Math.max(min, Math.min(max, Math.round(px)));
  view.style.setProperty(name, v + 'px');
  if (save) layoutSave(view.id, name, v);
}

function wireGutters() {
  const list = document.querySelectorAll('.gutter');
  for (let i = 0; i < list.length; i++) {
    const g = list[i];
    const view = g.closest ? g.closest('.view') : null;
    const name = g.dataset ? g.dataset.var : '';
    if (!view || !name) continue;             // 结构不对就当没这条线（测试用的假 DOM 走到这里）
    const axis = g.dataset.axis === 'y' ? 'y' : 'x';
    const of = g.dataset.of === 'next' ? 'next' : 'prev';
    // 位移进哪一格：管的是**线右边/下边**那一格时，鼠标往哪拖、那一格就变小（反号）。
    // 判据只有一条 —— 线跟着手走：往左拖，线左移，右边的格子就更宽。
    const sign = of === 'next' ? -1 : 1;
    const saved = layoutLoad(view.id, name);
    if (saved) gutterApply(g, view, name, saved, false);   // 恢复也走夹取：min/max 事后改小、或存储被手改，都不该把栏拖坏

    g.addEventListener('pointerdown', function (ev) {
      ev.preventDefault();                 // 挡掉拖动时选中文字
      if (g.focus) g.focus();              // preventDefault 会把"点一下获得焦点"一起挡掉，这里补回来：
                                           // 不补的话鼠标点完焦点还在 body 上，方向键就没反应
      const from = gutterBox(g, axis, of);
      const start = axis === 'x' ? ev.clientX : ev.clientY;
      g.classList.add('on');
      if (g.setPointerCapture) g.setPointerCapture(ev.pointerId);
      const move = function (e) {
        const d = (axis === 'x' ? e.clientX : e.clientY) - start;
        gutterApply(g, view, name, from + sign * d, false);
      };
      const up = function () {
        g.classList.remove('on');
        g.removeEventListener('pointermove', move);
        g.removeEventListener('pointerup', up);
        gutterApply(g, view, name, gutterBox(g, axis, of), true);   // 落盘只在松手时写一次
      };
      g.addEventListener('pointermove', move);
      g.addEventListener('pointerup', up);
      g.addEventListener('pointercancel', up);   // 触屏被系统打断时同样收尾（不然 .on 高亮与监听留着）
    });

    g.addEventListener('dblclick', function () {
      view.style.removeProperty(name);        // 复位 = 回到 CSS 里的默认值
      layoutSave(view.id, name, 0);
    });

    g.addEventListener('keydown', function (ev) {
      const d = ev.shiftKey ? 64 : 16;
      const step = axis === 'x'
        ? (ev.key === 'ArrowLeft' ? -d : ev.key === 'ArrowRight' ? d : 0)
        : (ev.key === 'ArrowUp' ? -d : ev.key === 'ArrowDown' ? d : 0);
      if (!step) return;
      ev.preventDefault();
      // 方向键同样是"线跟着按键走"：按左键 = 线左移（管左边那格就变小，管右边那格就变大）
      gutterApply(g, view, name, gutterBox(g, axis, of) + sign * step, true);
    });
  }
}

/* ==================== 交互 ==================== */

function wire() {
  wireGutters();
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
    // **不用先关预览**：扫描器一开始就跑 capture.begin() → begin_scan()，设备层会把取帧
    // 停下、会话留给扫描（状态变 held）。界面这边什么都不用做，也不许发 on=false
    // —— 那会清掉「用户想要预览」的意图，扫描结束后预览就回不来了。
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
  $('btn-ccd-rot').onclick = ccdRotate;
  $('btn-ccd-reopen').onclick = ccdReopen;
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
      showLightbox(t.dataset.full || '', t.getAttribute('src'), grabMeta(t.dataset.full || ''));
      return;
    }
    if (t.id === 'lightbox-img') {
      // 点大图 = 取过该点的整行/整列剖面（读的是保存的那个 16 位 PNG）
      const img = $(t.id);
      const pt = profPixel(ev.clientX, ev.clientY, img.getBoundingClientRect(),
                           img.naturalWidth, img.naturalHeight);
      if (pt && lightboxPath) {
        profPoint.lightbox = { path: lightboxPath, x: pt.x, y: pt.y };
        profFetch('lightbox');
      }
      return;
    }
    if (t.id === 'lightbox') closeLightbox();     // 点背景才关（点图现在是取剖面）
  });

  $('ccd-img').onclick = function (ev) {
    if (!ccdLive) return;
    const img = $('ccd-img');
    const pt = profPixel(ev.clientX, ev.clientY, img.getBoundingClientRect(),
                         img.naturalWidth, img.naturalHeight);
    if (!pt) return;
    profPoint.preview = pt;
    profFetch('preview');
  };

  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape' && !$('lightbox').hidden) closeLightbox();
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
