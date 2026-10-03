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

/* 四页：对准（读数 / 手动 / 曲线）、扫描（参数 / 进度 / 点位 / 历史）、
   预览（相机预览 / 已保存的原始帧）、数据（指定像素在各扫描点上的值）。
   前三页对的是**设备**：后端本来就规定它们互斥（扫描占着设备时手动会被 409 拒、
   相机也归扫描用），所以分页藏起来的不是"还有用的东西"，是"此刻用不了的东西"。
   后一页对的是**盘上的数据**（库 + 图像文件），只读 —— 扫描跑着也能翻旧数据。
   **这是显示状态，不是权限** —— 真正的拒绝永远在后端。 */
const VIEWS = ['align', 'scan', 'ccd', 'data'];
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
  // 数据页只读：进页时刷一次扫描列表（新跑完的扫描要能选到），再把曲线按卡片尺寸重排
  if (view === 'data') {
    loadDataScans();
    loadDataSources();     // 「这条是哪台设备采的」的选项（系数只在后端，前端只拿键与文案）
    fitChart(dataChart, 'data-chart', 240);
    fitChart(specChart, 'spec-chart', 180);
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

/* ==================== 轮询 ====================
   一个定时器 + 一条规矩：**不许重复启动**。重复启动会丢掉旧句柄 —— 之后 stop 只停得掉一个，
   另一个还在后台跑；五处循环从前各写一遍这三句（判空、setInterval、clearInterval），
   写法还各不相同（有的先跑一次再定时、有的只定时、有的靠外面的状态变量兜）。

   顺序是**先定时、再立刻跑一次**：立刻那一次是异步的，里面可能顺着状态机把轮询停掉
   （比如取数被拒就收工）—— 先定时才停得掉；反过来会留下一个没人管的定时器。
   */
function poller(fn, ms) {
  let timer = null;
  return {
    running: function () { return timer !== null; },
    start: function () {
      if (timer !== null) return;          // 已经在跑：什么都不做（状态刷新会反复调进来）
      timer = setInterval(fn, ms);
      fn();
    },
    stop: function () {
      if (timer === null) return;
      clearInterval(timer);
      timer = null;
    },
  };
}

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
let traceActive = false;
let lastServerTs = null;   // 最近一帧遥测的服务器时刻（记录锚点）

function deviceReady() {
  return !!stageState && stageState.connected && stageState.servo;
}

/* ==================== SSE ==================== */

const linkPoll = poller(pollLink, 1000);   // 链路指示灯（只报"界面这头还收得到 SSE 吗"）
const beat = poller(function () {
  fetch('/api/heartbeat', { method: 'POST' }).catch(function () {});
}, 2000);

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
      // 曝光：**这一点采图当时**相机上的值（后端从相机读回，随点位元数据入库）。
      // 老数据 / 没采到图的点没有这个数 —— 如实写「—」，不拿当前曝光倒填。
      '<td>' + (p.exposure_us === null || p.exposure_us === undefined
        ? '—' : (p.exposure_us / 1000).toFixed(2)) + '</td>' +
      // 点位表的图也走 8 位映射：真机扫描帧同样是 16 位 PNG，直连 /data 会是一片黑
      // （假相机的占位图是 8 位，所以以前看不出问题）。data-full 让点击能进同一套大图。
      '<td>' + (p.image_path
        ? '<img class="thumb" loading="lazy" alt="" data-full="' + esc(p.image_path) +
          '" src="' + thumbUrl(p.image_path) + '">'
        : '—') + '</td>';
    frag.appendChild(tr);
  }
  if (points.length > n) {
    const tr = document.createElement('tr');
    tr.innerHTML = '<td colspan="9" class="hint">只显示前 ' + n + ' 点，共 ' + points.length + ' 点</td>';
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

/* ==================== 范围框（三张图共用一套） ====================
   一张图的「坐标范围」= 两个输入框 + 一个钉住标记 + 一个「自动范围」按钮。
   位置曲线、剖面、像素曲线用的是同一套规矩，所以只有这一份实现：
   **输入框是唯一的准** —— 没被手填过（也没被手势改过）就按当前数据铺满，改过一次就不再自动铺满，
   点「自动范围」松钉恢复。铺满的边距、写回的小数位都由这里一处说了算。

   钉住是**每个轴各自的**：位置曲线的 X 与 Y 是两个框，填了 Y 不会把 X 一起钉住
   （从前两个轴共用一个标记，填一个数两个轴就都不再铺满了）。

   opt:
     min / max  两个输入框的 id
     decimals   写回框里的小数位（手势缩放能到多细就是它）
     pin        「范围已钉住」那个角标（两个轴共用一个角标时留空，由 onApply 自己写）
     fit        「自动范围」按钮的 id（可选；给 `unpin` 用）
     fitValues  () => [lo, hi] | null   按当前数据铺满；null = 还没数据，别动框
     onApply    (win, pinned) => void   把窗口套到图上（win = [lo, hi] 或 null）
   */
function rangeBox(opt) {
  const b = {
    pinned: false,
    decimals: opt.decimals === undefined ? 3 : opt.decimals,

    /* 现在框里那个窗口。填反或只填一半 = 没有窗口：不套用（那样画出来是空图），框留着等人改完 */
    window: function () {
      const lo = parseFloat($(opt.min).value), hi = parseFloat($(opt.max).value);
      return lo < hi ? [lo, hi] : null;
    },

    /* 写进框里并钉住：手填、滚轮、拖动、换读法换算，最后都走到这里 */
    set: function (lo, hi) {
      $(opt.min).value = lo.toFixed(b.decimals);
      $(opt.max).value = hi.toFixed(b.decimals);
      b.pinned = true;
      b.apply();
    },

    /* 套到图上：没钉住就先按当前数据铺满（没有数据就只套框里现有的数） */
    apply: function () {
      if (opt.pin) $(opt.pin).hidden = !b.pinned;
      if (!b.pinned && opt.fitValues) {
        const r = opt.fitValues();
        if (r) {
          $(opt.min).value = r[0].toFixed(b.decimals);
          $(opt.max).value = r[1].toFixed(b.decimals);
        }
      }
      opt.onApply(b.window(), b.pinned);
    },

    /* 「自动范围」：松钉 + 重新按数据铺满 */
    unpin: function () { b.pinned = false; b.apply(); },

    /* 接线：手填 = 钉住（框是唯一的准）；「自动范围」= 松钉 */
    wire: function () {
      if (opt.fit) $(opt.fit).onclick = b.unpin;
      [opt.min, opt.max].forEach(function (id) {
        $(id).oninput = function () { b.pinned = true; b.apply(); };
      });
      return b;
    },

    /* 滚轮缩放 + 按住拖动平移。两条手势**只改范围框里的数**（框仍然是唯一的准），
       改完照旧走 apply() —— 直接改图的话，下一次重画（换扫描、换读法）就被顶回去了。
       挂的是 uPlot 的 .u-over，**每建一次图都要重挂**：重建会把旧元素整个丢掉。 */
    attach: function (chart, axis) {
      const over = chart.over;
      /* 向上滚（deltaY < 0）= 放大。因子按 deltaY 的**大小**算，不是"一格一个固定倍数"：
         鼠标滚轮一格约 100（≈1.16 倍），触控板一次只有几 —— 一格一档在触控板上快得没法用。
         Firefox 的行模式（deltaMode=1）折成像素。 */
      over.addEventListener('wheel', function (ev) {
        const win = b.window();
        if (!win) return;                    // 没有窗口：不拦页面滚动
        ev.preventDefault();                 // 监听是 passive:false 挂的，这里才拦得住页面跟着滚
        const box = over.getBoundingClientRect();
        const anchor = chart.posToVal(ev.clientX - box.left, axis);
        const k = Math.pow(1.0015, ev.deltaY * (ev.deltaMode === 1 ? 16 : 1));
        const next = zoomRange(win[0], win[1], k, anchor);
        b.set(next[0], next[1]);
      }, { passive: false });

      /* 按住拖动平移视窗。位移按**一个像素多少值**换算（拖动开始时量一次），
         不拿 posToVal 边拖边问 —— 比例随窗口变，那样拖久了窗口会自己变宽变窄。 */
      over.addEventListener('pointerdown', function (ev) {
        if (ev.button !== 0) return;         // 只认左键，右键留给浏览器菜单
        const win = b.window();
        if (!win) return;
        ev.preventDefault();                 // 挡掉拖动时选中文字
        const box = over.getBoundingClientRect();
        const vpp = (win[1] - win[0]) / Math.max(1, box.width);
        const from = win, startX = ev.clientX;
        over.classList.add('pan');
        if (over.setPointerCapture) over.setPointerCapture(ev.pointerId);
        const move = function (e) {
          // 手往右拖 = 把曲线往右拉 = 看更小的值（跟拖地图一个方向），所以位移取负
          const next = shiftRange(from[0], from[1], -(e.clientX - startX) * vpp);
          b.set(next[0], next[1]);
        };
        const up = function () {
          over.classList.remove('pan');
          over.removeEventListener('pointermove', move);
          over.removeEventListener('pointerup', up);
          over.removeEventListener('pointercancel', up);
        };
        over.addEventListener('pointermove', move);
        over.addEventListener('pointerup', up);
        over.addEventListener('pointercancel', up);   // 触屏被系统打断时同样收尾
      });
    },
  };
  return b;
}

/* ==================== 建图（四张图共用一套壳子） ====================
   点位图、位置曲线、剖面（两张）、像素曲线：**壳子是同一套** —— 宽高按卡片量、图例留着、
   坐标轴配色一致、卡片尺寸变了跟着重排，各图只有 series 与回退高度不同。
   **拖拽缩放默认关掉**（boxed）：uPlot 自带的拖拽直接改 scales，而范围框才是唯一的准
   （见 rangeBox：手势改的是框里的数，改完再套回图上）。点位图没有范围框，那里单独放行。

   尺寸观察者按 id **只挂一个**：换横轴读法会把图 destroy 重建，但元素还是同一个 ——
   每重建一次就挂一个的话，旧观察者会一直对着已经不存在的图重排（图就再也不会自己长回来）。
   opt: { xlabel, series（series[1..] 原样给）, yaxis?, height?, data?, drag? }
   */
const chartSlots = {};        // id → {u, h, obs}；u 每次都换，obs 只挂一次
function makeChart(id, opt) {
  const h = opt.height || 220;
  const slot = chartSlots[id] || (chartSlots[id] = { u: null, h: h, obs: null });
  slot.h = h;
  const cfg = {
    width: chartWidth($(id)),
    height: chartHeight($(id), h),   // 建图时的高度；随后 fitChart 按卡片实际尺寸校正
    scales: { x: { time: false } },
    legend: { show: true },
    series: [{ label: opt.xlabel }].concat(opt.series),
    axes: [
      { stroke: '#64748b', grid: { stroke: '#e2e8f0' }, ticks: { stroke: '#cbd5e1' } },
      Object.assign({ stroke: '#64748b', grid: { stroke: '#e2e8f0' }, ticks: { stroke: '#cbd5e1' } },
                    opt.yaxis)
    ]
  };
  if (!opt.drag) cfg.cursor = { drag: { x: false, y: false } };
  slot.u = new uPlot(cfg, opt.data || [[], []], $(id));
  if (!slot.obs) {
    slot.obs = new ResizeObserver(function () { fitChart(slot.u, id, slot.h); });
    slot.obs.observe($(id));
  }
  return slot.u;
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
    chart = makeChart('chart', {
      xlabel: '序号',
      series: [{ label: '偏差 (nm)', stroke: '#2563eb', width: 1.5,
                 points: { show: showPointsBelow(200), size: 5 } }],
      data: data,
      drag: true,        // 点位图没有范围框：uPlot 自带的拖拽缩放留着（别处一律关掉）
    });
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
const tracePoll = poller(traceTick, TRACE_POLL_MS);   // 记录中的取数节拍（traceTick 是函数声明，提升到位）

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
  traceChart = makeChart('trace', {
    xlabel: '时间 (s)',
    series: [{ label: '位置 (µm)', stroke: '#2563eb', width: 1.5,
               points: { show: showPointsBelow(TRACE_POINT_MAX), size: 4 } }],
    yaxis: { values: traceAxisVals, size: traceAxisSize },
  });
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

/* 位置曲线的范围：X（秒）与 Y（µm）**各是一个范围框**，规矩同一套（见 rangeBox）。
   共用一个「范围已钉住」角标 —— 有两个轴，钉住哪个它都该亮。 */
function traceScales() {
  $('trace-pin').hidden = !(traceX.pinned || traceY.pinned);
  if (!traceChart) return;
  const wx = traceX.window();
  if (wx) traceChart.setScale('x', { min: wx[0], max: wx[1] });
  const wy = traceY.window();
  if (wy) traceChart.setScale('y', { min: wy[0], max: wy[1] });
}

const traceX = rangeBox({
  min: 'trace-xmin', max: 'trace-xmax', decimals: 3, onApply: traceScales,
  fitValues: function () {
    const w = traceLast;
    if (!w || !w.ts.length) return null;        // 还没有数据就别去动输入框
    const span = w.ts[w.ts.length - 1] - traceFrom;
    return [0, span > 0 ? span : traceSecs];
  },
}).wire();

const traceY = rangeBox({
  min: 'trace-ymin', max: 'trace-ymax', decimals: 4, onApply: traceScales,
  fitValues: function () {
    const w = traceLast;
    if (!w || !w.position.length) return null;
    let lo = w.position[0], hi = w.position[0];
    for (let i = 1; i < w.position.length; i++) {
      if (w.position[i] < lo) lo = w.position[i];
      if (w.position[i] > hi) hi = w.position[i];
    }
    const pad = (hi - lo) * 0.05 || 0.0005;     // 一点起伏都没有时给 1 nm 的窗，别退化成一条线
    return [lo - pad, hi + pad];
  },
}).wire();

/* 数据换了就重套一次：没钉住的那个轴跟着铺满，钉住的保持不动 */
function applyTraceRange() { traceX.apply(); traceY.apply(); }

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
  tracePoll.start();        // 先定时再立刻出一帧（见 poller：顺序反了会留下没人管的定时器）
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
  tracePoll.stop();
  if (r) { traceLast = r; renderTrace(); renderTraceStats(); }
  syncTraceUI();
}

/* ==================== 相机预览 ==================== */

/* 预览就是"最近一帧"：后端按 15 fps 连续取帧并缓存一张 JPEG，
   前端反复设 img.src 去取。取不到（204）就跳过，不排队、不重试。
   **界面不做任何处理**：后端给的就是原始灰度，不拉伸、不伪彩。 */
const CCD_LIVE_MS = 100;      // 取帧节拍 10 fps（比后端 15 fps 慢一点，多出来的请求拿同一张）
const CCD_POLL_EVERY = 5;     // 每 5 拍才问一次状态（2 Hz）——状态是内存快照，没必要每帧都问
const ccdPoll = poller(ccdTick, CCD_LIVE_MS);   // 取帧节拍（只由 state 决定，见 syncCcdTimer）
// 「正在打开…」提示里写的等待上限：后端建会话时最多等这么久（config.TL_OPEN_WAIT_S）。
// 这里只是文案用的数字，不参与任何判断 —— 真正的超时在后端。
const CCD_OPEN_WAIT_S = 8;

let ccdAvailable = false;
let ccdInfo = null;
// **界面不持有意图**：「要不要预览」是后端的状态（state === 'preview'），这里只是镜像它。
// 旧代码在前端又存了一份 ccdWanted，于是「谁说了算」有两个答案 —— 掉线后就对不上。
let ccdLive = false;
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
  // 取帧计时器只由 state 决定：是 preview 就取帧，别的状态一律停（不再有两份真相）。
  // ccdTicks 是"每 5 拍问一次状态"的计数器，**只在真的开起来那一次清零** ——
  // 每来一次状态刷新都清的话，那个 5 永远数不到，状态就再也不问了。
  if (!ccdLive) { ccdPoll.stop(); return; }
  if (ccdPoll.running()) return;
  ccdTicks = 0;
  ccdPoll.start();
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
  ccdPoll.stop();
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
const profLast = { preview: null, lightbox: null };    // 最近一次剖面数据（"按数据铺满"要按它算）

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
  // 两张图都是吃满卡片高度的（makeChart 自己挂尺寸观察者）
  const mk = function (id, label, color) {
    return makeChart(id, { xlabel: label, height: 150,
                        series: [{ label: 'ADU', stroke: color, width: 1 }] });
  };
  profCharts[host] = {
    h: mk(cfg.h, '水平（整行）像素', '#2563eb'),
    v: mk(cfg.v, '垂直（整列）像素', '#0d9488'),
  };
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
  profLast[host] = p;
  applyProfRange();                       // 每来一段新数据跟一次（钉住时不动、自动时铺满）
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

/* 纵轴范围 = 一个范围框（rangeBox，与位置曲线同一套规矩）。
   整行与整列共用一条纵轴 —— 各画各的自动范围，两张图就没法互相比较。
   大图弹窗那两张也认这个范围（同一个刻度才可比）；没钉住时各按各的数据铺满。 */
function profExtent(p) {
  if (!p || !p.horizontal || !p.vertical) return null;
  let lo = null, hi = null;
  [p.horizontal, p.vertical].forEach(function (arr) {
    for (let i = 0; i < arr.length; i++) {
      if (lo === null || arr[i] < lo) lo = arr[i];
      if (hi === null || arr[i] > hi) hi = arr[i];
    }
  });
  if (lo === null) return null;
  const pad = Math.max(2, (hi - lo) * 0.05);   // 平的一段也给几 ADU 的窗，别退化成一条线
  return [Math.max(0, lo - pad), hi + pad];
}

const profY = rangeBox({
  min: 'prof-ymin', max: 'prof-ymax', pin: 'prof-pin', fit: 'btn-prof-fit',
  decimals: 0,
  fitValues: function () {
    // 框里跟的是"当前有数据的那一张"：优先预览（框就在它下面），只有大图那条有数据时
    // （比如没开预览、直接点图库）就跟大图 —— 不然点了「自动范围」、图上明明变了，
    // 框里还留着旧值，看着像没生效。
    return profExtent(profLast.preview) || profExtent(profLast.lightbox);
  },
  onApply: function (win, pinned) {
    ['preview', 'lightbox'].forEach(function (host) {
      const c = profCharts[host];
      if (!c) return;
      const r = pinned ? win : profExtent(profLast[host]);   // 钉住 = 同一个刻度；没钉住各按各的数据
      if (!r) return;
      c.h.setScale('y', { min: r[0], max: r[1] });
      c.v.setScale('y', { min: r[0], max: r[1] });
    });
  },
}).wire();

/* 来了一段新剖面就重套一次（钉住时不动、自动时铺满） */
function applyProfRange() { profY.apply(); }

function profClear(host) {
  const cfg = PROF_HOSTS[host];
  profPoint[host] = null;
  profLast[host] = null;
  $(cfg.cut).hidden = true;
  if (profCharts[host]) {
    profCharts[host].h.setData([[], []]);
    profCharts[host].v.setData([[], []]);
  }
  if (cfg.where) setText(cfg.where, '在预览图上点一下');
}

/* 缩略图 URL：**8 位映射**那条路（后端 >>2 → JPEG，与预览同一条口径）。
   16 位 PNG 的原值只占 0~1022（满量程 65535 的 1.6%），把文件直接给浏览器看就是一片黑
   （实测：均值 352 的帧在屏幕上只有 1.4/255）。max_side 是长边像素，不给就用后端的默认值。 */
function thumbUrl(path, maxSide) {
  return '/api/grabs/thumb?path=' + encodeURIComponent(path) +
    (maxSide ? '&max_side=' + maxSide : '');
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
  $('lightbox-img').src = thumbUrl(full, 1440);   // 弹窗里要更大的一张
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

/* 点图取剖面 —— 预览图与图库大图**走同一条路**（点哪儿、算哪一行哪一列都是显示的事，
   后端只管按坐标切）。img 是"看到的那一张"：预览是转过朝向的显示帧，大图是保存的 PNG 本身。
   返回 false = 没点在图里（点在图上才算，见 profPixel）。 */
function profPickFrom(host, img, ev) {
  const pt = profPixel(ev.clientX, ev.clientY, img.getBoundingClientRect(),
                       img.naturalWidth, img.naturalHeight);
  if (!pt) return false;
  if (host === 'lightbox') {
    if (!lightboxPath) return false;
    profPoint.lightbox = { path: lightboxPath, x: pt.x, y: pt.y };
  } else {
    profPoint.preview = pt;
  }
  profFetch(host);
  return true;
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
    img.src = thumbUrl(it.path, 520);

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
  await loadCrops();          // 哪条裁过、裁到哪一块：这一列要照着写
  const tb = $('history').tBodies[0];
  const frag = document.createDocumentFragment();
  for (let i = 0; i < list.length; i++) {
    const s = list[i];
    const rect = cropRects[s.id];
    // 裁到多小单独一列（没裁过写 —）：那一列只放数，按钮那一列只放按钮
    const sizeCell = rect
      ? '<span title="整组一起裁的：原始坐标 x ' + rect[0] + '~' + (rect[0] + rect[2] - 1) +
        '、y ' + rect[1] + '~' + (rect[1] + rect[3] - 1) + '">' + rect[2] + '×' + rect[3] + '</span>'
      : '—';
    // 裁剪的入口在这儿（一组 = 同名的扫描，点哪一条都是裁整组）。
    // **裁过的也留着这个按钮**：还能往里再裁一刀（只能越裁越小）—— 按钮消失等于把路堵死。
    const cropBtn = !s.done ? ''
      : '<button class="ghost" data-crop="' + s.id + '" data-name="' + esc(s.name) +
        '">裁剪</button>';
    const tr = document.createElement('tr');
    tr.innerHTML =
      '<td>' + s.id + '</td>' +
      '<td>' + (esc(s.name) || '—') + '</td>' +
      '<td>' + s.start_um + ' → ' + s.stop_um + '</td>' +
      '<td>' + s.count + '</td>' +
      '<td>' + s.done + '</td>' +
      '<td>' + (STATUS_CN[s.status] || esc(s.status)) + '</td>' +
      '<td>' + fmtTime(s.created_at) + '</td>' +
      '<td>' + sizeCell + '</td>' +
      '<td><button class="ghost" data-open="' + s.id + '">查看</button> ' + cropBtn + ' ' +
          '<button class="ghost" data-del="' + s.id + '">删除</button></td>';
    frag.appendChild(tr);
  }
  if (list.length >= 50) {
    const tr = document.createElement('tr');
    tr.innerHTML = '<td colspan="9" class="hint">最多显示最近 50 次扫描</td>';
    frag.appendChild(tr);
  }
  tb.innerHTML = '';
  tb.appendChild(frag);
}

/* ==================== 数据处理页：指定像素在各扫描点上的值 ====================

   这一页对的是**盘上的数据**（库里的扫描 + data/images 里各点的帧），不是设备：
   一次请求把整条序列取回来，界面只负责把它画出来、把口径写在脸上。
   **前端不做数据加工**：取哪个像素、算哪一段，全部照后端给的画。 */

/* 横轴两种读法指的是**同一串数**：位置是台子走到哪（µm，采图那一刻的读数），
   时间是光走那段往返光程要多久（fs）。换算是**后端算好的另一列**（光程差 = 2×位移，再除以 c），
   这里只挑哪一列画 —— 所以切换**不重新取数**（两千张 PNG 现读要几秒，切换等不起）。

   横轴是**四列里挑一列**：两种读法 × 两种口径，四列都由后端一次下发。
   读法：位置 = 台子走到哪（采图那一刻的读数）；时间 = 光走那段往返光程要多久（系数与换算都在后端）。
   口径：**折算** = 按设备声明的系数折成 µm；**原值** = 设备当时报的数（µm ÷ 系数；XMT 上是
   4/3 µm，也就是厂商上位机显示的那套数）。串还是同一串、换的只是刻度 —— 所以钉住的范围框
   要跟着换算，频谱的频率刻度也跟着变。 */
const DATA_AXIS = {
  pos: { um: { key: 'position_um', unit: 'µm', legend: '实际位置 (µm)',
               span: '位置跨度 (µm)', btn: '横轴：位置 (µm)' },
         raw: { key: 'position_raw', unit: '读回值', legend: '读回原值（设备单位）',
                span: '位置跨度（读回值）', btn: '横轴：位置（读回原值）' } },
  time: { um: { key: 'time_fs', unit: 'fs', legend: '时间 (fs)',
                span: '时间跨度 (fs)', btn: '横轴：时间 (fs)' },
          raw: { key: 'time_raw_fs', unit: 'fs', legend: '时间 (fs，按读回原值算)',
                 span: '时间跨度 (fs，按原值)', btn: '横轴：时间（按原值）' } },
};
const DATA_SCALE_CN = { um: '折算 µm', raw: '读回原值' };

let dataScans = [];        // 后端列的扫描（左栏下拉框的选项）
let dataChart = null;      // 像素曲线
let dataChartKey = null;   // 上面这张图是按哪一列画的：换了列就得重建（见 renderPixelCurve）
let dataAxis = 'pos';      // 现在看的是哪种读法（默认位置：读数就是读数，时间轴是一种刻度）
let dataScale = 'um';      // 位置口径：um = 折算后的 µm（默认）| raw = 设备读回原值
let dataSources = [];      // 后端给的「读回口径」表 [{key,label,factor}]（系数只在后端一处）
let dataScaleTitle = '';   // 「口径」按钮原样的说明（禁用时在后面补上"为什么不能切"）
let dataLast = null;       // 最近一次取回来的响应：切横轴照它重画，不再取数
let dataXs = [];           // 现在这条曲线的横坐标（"自动范围"照它铺满）
let dataOrder = 'seq';     // 连线顺序：seq = 采图顺序（默认，如实） | sort = 按横轴读数排序
let dataSeq = 0;           // 取数请求序号：慢响应回来时可能已经换了扫描或像素

function dataAxisCur() { return DATA_AXIS[dataAxis][dataScale]; }

/* 频谱要的是**时间**那一列（跟横轴按钮在看位置还是时间无关）：口径跟着横轴走。 */
function dataTimeKey() { return dataScale === 'raw' ? 'time_raw_fs' : 'time_fs'; }

/* 这条扫描能不能切到「原值」：系数记下了、而且不是 1。
   不能切时**必须说清为什么** —— 按钮灰着不解释，看着就像功能坏了。 */
function dataScaleState(d) {
  if (!d) return { ok: false, why: '还没有取数。' };
  const f = d.readback_to_um;
  if (f === null || f === undefined) {
    return { ok: false, why: '这条扫描没记下是哪台设备采的（加这一列之前的老数据）—— ' +
      '在左边「这条是哪台设备采的」里指认一次就能切。' };
  }
  if (Math.abs(f - 1) < 1e-9) {
    return { ok: false, why: '这台设备的位置读数本来就是 µm（折算系数是 1），切了也一样。' };
  }
  return { ok: true, why: '读回原值：1 个单位 = ' + f + ' µm（这条扫描记下的设备口径）' };
}

/* 按钮、跨度标题、范围框上的单位：横轴换读法时一起换 —— 框里填的数必须跟横轴同一个单位，
   不然"我填的是 µm 还是 fs"就成了要猜的事。初值也由它写，免得 index.html 和这张表写岔。 */
function dataAxisApplyLabels() {
  const ax = dataAxisCur();
  setText('btn-data-axis', ax.btn);
  setText('btn-data-scale', '口径：' + DATA_SCALE_CN[dataScale]);
  setText('data-span-k', ax.span);
  setText('data-xmin-k', 'X 最小 (' + ax.unit + ')');
  setText('data-xmax-k', 'X 最大 (' + ax.unit + ')');
}

async function loadDataScans() {
  const list = await get('/api/scans');
  if (!list) return;
  dataScans = list;
  const sel = $('data-scan');
  const keep = sel.value;                  // 正在看的那条：刷新列表不该把人挑好的换掉
  const frag = document.createDocumentFragment();
  for (let i = 0; i < list.length; i++) {
    const s = list[i];
    const o = document.createElement('option');
    o.value = String(s.id);
    o.textContent = '#' + s.id + (s.name ? ' · ' + s.name : '') + ' · ' +
      s.start_um + '→' + s.stop_um + ' µm · ' + s.count + ' 点 · ' +
      (STATUS_CN[s.status] || s.status) + ' · ' + fmtTime(s.created_at);
    frag.appendChild(o);
  }
  sel.innerHTML = '';
  sel.appendChild(frag);
  // 没选过就默认最新那次（列表是倒序的）
  const ids = list.map(function (s) { return String(s.id); });
  sel.value = ids.indexOf(keep) >= 0 ? keep : (ids.length ? ids[0] : '');
  dataScanInfo();
}

function dataScanPick() {
  const id = parseInt($('data-scan').value, 10);
  for (let i = 0; i < dataScans.length; i++) if (dataScans[i].id === id) return dataScans[i];
  return null;
}

function dataScanInfo() {
  const s = dataScanPick();
  setText('data-scan-info', !s ? '没有可选的扫描。'
    : '共 ' + s.count + ' 点，已记录 ' + s.done + ' 点 · ' + s.start_um + ' → ' +
      s.stop_um + ' µm · ' + (STATUS_CN[s.status] || s.status));
  dataSourceApply();          // 「哪台设备采的」跟着选中的那条走
}

/* 「这条是哪台设备采的」= 位置读回口径。系数是设备属性、**只在后端一处**（键 → 系数），
   这里只送键；选项与文案也由后端下发 —— 前端一个系数都不写。
   加这一列之前的老数据没记，只能人工指认一次：写进库的只有「这条是哪台设备」这一个字段，
   各点的读数、图、曲线一个字节都不动（原值列是取数时按系数从已存的 µm 反推的）。 */
async function loadDataSources() {
  const r = await get('/api/readback-sources');
  if (!r || !r.sources) return;
  dataSources = r.sources;
  const sel = $('data-source');
  const frag = document.createDocumentFragment();
  const un = document.createElement('option');
  un.value = '';
  un.textContent = '未记录（老数据：不知道是哪台采的）';
  frag.appendChild(un);
  for (let i = 0; i < dataSources.length; i++) {
    const o = document.createElement('option');
    o.value = dataSources[i].key;
    o.textContent = dataSources[i].label;
    frag.appendChild(o);
  }
  sel.innerHTML = '';
  sel.appendChild(frag);
  dataSourceApply();
}

/* 库里的系数对上是哪一项；对不上（设备表改过）就当未记录显示 —— 不猜。 */
function dataSourceKey(f) {
  if (f === null || f === undefined) return '';
  for (let i = 0; i < dataSources.length; i++) {
    if (Math.abs(dataSources[i].factor - f) < 1e-9) return dataSources[i].key;
  }
  return '';
}

function dataSourceApply() {
  const s = dataScanPick();
  const f = s ? s.readback_to_um : null;
  $('data-source').value = dataSourceKey(f);
  if (!s) { setText('data-source-note', '—'); return; }
  if (f === null || f === undefined) {
    setText('data-source-note', '未记录 —— 老数据没记是哪台设备采的，位置轴只能用折算后的 µm。' +
      '指认一次（只写这一个字段）就能切「读回原值」。');
  } else {
    setText('data-source-note', 'µm = 读回值 × ' + f + '（建扫描时按当时那台设备记下的）');
  }
}

/* 指认一次：只改这一个字段，然后把列表与曲线重新拉一遍（口径变了，原值列才有内容）。 */
async function dataSourceSave() {
  const s = dataScanPick();
  if (!s) { toast('先选一条扫描', true); return; }
  const key = $('data-source').value;
  const r = await post('/api/scans/' + s.id + '/readback', { source: key || null });
  if (!r) { dataSourceApply(); return; }        // 失败：把下拉框拨回库里的样子
  let label = '未记录';
  for (let i = 0; i < dataSources.length; i++) {
    if (dataSources[i].key === key) label = dataSources[i].label;
  }
  await loadDataScans();
  if (dataChart) drawPixelCurve();              // 正在看这条：重新取一次数（多出原值列）
  toast('已记下：' + label);
}

/* 后端给的序列 → 图上的两条数组。
   **缺图的点：值原样是 null，线在那里断开**（uPlot 遇 null 断线）—— 不许拿邻点顶上。
   没记下读出位置的点连横坐标都没有，上不了图：丢几个由统计里的「没图的点」一并如实说明。
   横坐标整列照后端给的那一列搬（位置或时间），**前端一个系数都不乘**。
   连线顺序两种：采图顺序（默认）或按横轴读数排序 —— 排序**只是一次置换**（每个点的值跟着
   它自己的横坐标搬），不产生任何新数。 */
function pixelSeries(d) {
  const col = d[dataAxisCur().key];
  const pts = [];
  for (let i = 0; i < d.value.length; i++) {
    const px = col[i];
    if (px === null || px === undefined) continue;    // 没记下读数的点上不了图
    pts.push([px, d.value[i] === undefined ? null : d.value[i]]);
  }
  // sort 是稳定的（ES2019 起）：读数打平时保持采图顺序，不会因为排序把同一点前后挪来挪去
  if (dataOrder === 'sort') pts.sort(function (a, b) { return a[0] - b[0]; });
  const xs = [], ys = [];
  for (let i = 0; i < pts.length; i++) { xs.push(pts[i][0]); ys.push(pts[i][1]); }
  return [xs, ys];
}

async function drawPixelCurve() {
  const s = dataScanPick();
  if (!s) { toast('先选一条扫描', true); return; }
  // 空框不能当成 0：Number('') 是 0，那样"没填"会静默变成左上角那个像素
  const rx = String($('data-x').value || '').trim();
  const ry = String($('data-y').value || '').trim();
  if (!/^\d+$/.test(rx) || !/^\d+$/.test(ry)) {
    toast('像素坐标要填非负整数（列、行）', true);
    return;
  }
  const seq = ++dataSeq;
  const prev = $('data-title').textContent;
  setText('data-title', '读图中…');
  const d = await get('/api/scans/' + s.id + '/pixel?x=' + rx + '&y=' + ry);
  if (!d) {
    // 失败原因 request() 已经弹过；标题得还原 —— 停在"读图中…"就是骗人
    if (seq === dataSeq) setText('data-title', prev);
    return;
  }
  if (seq !== dataSeq) return;            // 慢响应回来时可能已经换了扫描或像素
  renderPixelCurve(d);
}

function renderPixelCurve(d) {
  dataLast = d;
  // 这条扫描没有原值列（系数没记下 / 本来就是 1）时**退回折算口径**：宁可显示折算过的，
  // 也不能拿 undefined 当坐标画出一张空图。按钮那边会把原因写在 title 上。
  if (dataScale === 'raw' && !d.position_raw) dataScale = 'um';
  // 裁过的扫描在标题上写明：坐标口径**没变**（还是原始坐标），只是文件小了、读得快了
  const cp = d.crop;
  $('data-crop').hidden = !cp;
  if (cp) {
    setText('data-crop', '已裁剪 ' + cp[2] + '×' + cp[3] + ' @ (' + cp[0] + ',' + cp[1] +
      ')｜坐标仍是原始坐标');
  }
  const data = pixelSeries(d);
  const xs = data[0], ys = data[1];
  let ok = 0, peak = null, lo = null, hi = null;
  for (let i = 0; i < ys.length; i++) {
    if (ys[i] === null) continue;                // 没图的点不参与统计（它连值都没有）
    ok++;
    if (peak === null || ys[i] > peak) peak = ys[i];
  }
  // 跨度是**位置**的跨度（µm），不是值的跨度 —— 值那一条已经在图上，峰值另算
  for (let i = 0; i < xs.length; i++) {
    if (lo === null || xs[i] < lo) lo = xs[i];
    if (hi === null || xs[i] > hi) hi = xs[i];
  }

  // 换过横轴（读法或口径）就**重建这张图**：图例上那个单位（"实际位置 (µm)" / "读回原值" …）
  // 是建图时一次性写死的，uPlot 事后改 series.label 不会重画那一格 —— 留着它就是一行
  // 写着 µm 的假图例。
  if (dataChart && dataChartKey !== dataAxisCur().key) {
    dataChart.destroy();
    dataChart = null;
  }
  if (!dataChart) {
    dataChartKey = dataAxisCur().key;
    dataChart = makeChart('data-chart', {
      xlabel: dataAxisCur().legend,
      height: 240,
      series: [{ label: '像素值 (ADU)', stroke: '#2563eb', width: 1.5,
                 points: { show: showPointsBelow(400), size: 4 } }],
      data: data,
    });
    dataX.attach(dataChart, 'x');   // 滚轮/拖动挂在新图上（重建后旧元素没了，见 rangeBox.attach）
  } else {
    dataChart.setData(data);
  }
  fitChart(dataChart, 'data-chart', 240);
  dataXs = xs;
  $('data-order-note').hidden = dataOrder !== 'sort';   // 排过序就得写在脸上，别让人以为这是采图顺序
  // 口径也写在脸上：现在画的是折算过的 µm 还是设备原值，不写就要靠猜
  const scale = dataScaleState(d);
  $('data-scale-note').hidden = dataScale !== 'raw';
  if (dataScale === 'raw') setText('data-scale-note', scale.why);
  $('btn-data-scale').disabled = !scale.ok;
  $('btn-data-scale').title = scale.ok ? dataScaleTitle : '不能切原值：' + scale.why;
  applyDataXRange();                      // 横轴范围：没被手填过就按这一段数据铺满

  setText('data-title', '#' + d.scan_id + (d.name ? ' · ' + d.name : '') +
    '　像素 (' + d.x + ', ' + d.y + ')');
  setText('data-n', String(d.count));
  setText('data-ok', String(ok));
  setText('data-missing', String(d.missing));
  setText('data-span', lo === null ? '—' : (hi - lo).toFixed(3));
  setText('data-peak', peak === null ? '—' : String(peak));
  setText('data-size', d.width === null || d.width === undefined ? '—'
    : d.width + '×' + d.height + ' · ' + d.bits + ' 位 · ' + d.full_scale);

  const empty = $('data-empty');
  if (ok > 0) {
    empty.hidden = true;
  } else {
    empty.hidden = false;
    empty.textContent = !d.count ? '这条扫描里还没有点。'
      : (d.missing >= d.count && d.width === null
        ? '这条扫描没有可读的帧（没采图，或者图被删了）—— 没有值可画。'
        : '这个像素一个值都没取到。');
  }
  renderSpectrum(d);     // 曲线画完就把它的谱跟上（同一份数据，不重新取数）
}

/* 横轴范围：一个范围框（rangeBox，与位置曲线同一套规矩），单轴 + 两条手势。 */
const dataX = rangeBox({
  min: 'data-xmin', max: 'data-xmax', pin: 'data-xpin', fit: 'btn-data-fit',
  decimals: 3,
  fitValues: function () {
    if (!dataXs.length) return null;      // 还没有数据就别去动输入框
    let lo = dataXs[0], hi = dataXs[0];
    for (let i = 1; i < dataXs.length; i++) {
      if (dataXs[i] < lo) lo = dataXs[i];
      if (dataXs[i] > hi) hi = dataXs[i];
    }
    const pad = (hi - lo) * 0.05 || 0.0005;   // 两端各留 5%；一点跨度都没有时给个 0.001 的窗
    return [lo - pad, hi + pad];
  },
  onApply: function (win) {
    if (dataChart && win) dataChart.setScale('x', { min: win[0], max: win[1] });
  },
}).wire();

/* 取了一段新数据就重套一次（钉住时不动、自动时铺满） */
function applyDataXRange() { dataX.apply(); }

/* ---- 手势只用得到的数学 ----
   滚轮缩放与拖动平移本身在 rangeBox.attach 里（哪张图要就挂哪张），这里只剩纯函数：
   离线测得了，也免得"缩放"这件事有两份实现。 */

/* 以 anchor（鼠标底下那个值）为定点缩放：factor < 1 放大（窗口变窄），> 1 缩小。
   定点缩放的意义就是**锚点不动** —— 盯着的那一点不会从鼠标底下跑掉。 */
function zoomRange(lo, hi, factor, anchor) {
  return [anchor + (lo - anchor) * factor, anchor + (hi - anchor) * factor];
}

/* 平移：dx 是值的位移，跨度不变 */
function shiftRange(lo, hi, dx) {
  return [lo + dx, hi + dx];
}


/* 换横轴读法：位置 ↔ 时间。**同一串数换刻度，不取数** —— 已经画过就照手里那份重画，
   图例单位、横轴刻度、跨度统计都跟着换；还没画过就只换按钮文字，画的时候自然按新读法来。 */
function dataAxisToggle() {
  dataAxis = dataAxis === 'pos' ? 'time' : 'pos';
  dataAxisApplyLabels();
  // 范围框里填的是**当前单位**的数：钉住过就得把同一个视窗换算过去，不然 20–60 µm 会
  // 原地变成 20–60 fs（那是另一段）。系数照旧来自后端（取数响应里那份），前端不写死。
  const f = dataLast && dataLast.time_fs_per_um;
  const win = dataX.pinned ? dataX.window() : null;
  if (win && f) {
    const k = dataAxis === 'time' ? f : 1 / f;
    dataX.set(win[0] * k, win[1] * k);   // set = 写回框 + 钉住 + 套到图上
  }
  if (dataLast) renderPixelCurve(dataLast);
}

/* 换位置口径：折算 µm ↔ 设备读回原值。**同一串数换刻度，不取数** —— 四列都在手里。
   钉住的范围框按系数换算过去（框里填的数永远跟横轴同一个口径），频谱跟着重算。 */
function dataScaleToggle() {
  const d = dataLast;
  const st = dataScaleState(d);
  if (!st.ok) { toast(st.why, true); return; }        // 不能切：说清为什么，不静默
  const per = d.readback_to_um;                       // µm = 原值 × per
  const win = dataX.pinned ? dataX.window() : null;
  dataScale = dataScale === 'um' ? 'raw' : 'um';
  if (win) {
    const k = dataScale === 'raw' ? 1 / per : per;
    dataX.set(win[0] * k, win[1] * k);
  }
  dataAxisApplyLabels();
  renderPixelCurve(d);
}

/* ==================== 数据页：频谱（功率谱） ====================

   对上面那条「指定像素在各扫描点上的值」做傅里叶变换：横轴频率（THz）、纵轴功率谱。
   口径就是这块功能的全部内容，动其中任何一条之前先想清楚它意味着什么：

   1) **采样位置用真实的**：每个点拿它自己那一刻的 time_fs 进变换（非均匀最小二乘），
      不插值、不假设等间隔。XMT 上读数噪声与步距同量级（实测 0.055 µm 步距上 ±0.056 µm、
      338 个间隔往回走），按"名义等间隔"算等于把那些间隔当成不存在。
   2) **缺一个点整条不算**：没采到图、没记下读数都报错，说清是第几个点。谱里没有"断开"这回事 ——
      补零、跳过、插值都是偷偷换了一条采样序列。
   3) **按采图顺序**，与「连线顺序」按钮无关：排过序的序列不是时间序。
   4) **整条扫描的全部点**，与可视范围框无关（框只管看，不参与变换）。
   5) 已去均值（直流那根柱子会压掉别的峰，所以不画）、不加窗。
   6) 频率格 k/T（T = 真实首末跨度），上限卡在**名义 Nyquist** = 1/(2×平均间距)：
      越界给的是混叠，不是信息。
   7) 纵轴是**功率**：(A²+B²)/2 —— 纯正弦幅度 A 进来，峰高就是 A²/2（单边均方功率）；
      切到 dB 是相对这条谱里最强的那根（0 dB = 最强）。
   8) 算在前端：那两列数据取一次就在手里，换读法、切 dB 都即时重算/重画，不再读几千张 PNG。
      **物理系数（2 与 c）仍然只写在后端一处** —— 这里拿到的已经是 time_fs，只做 fs→THz 的换单位。 */
/* 频谱横轴两种读法：频率（THz）与波长（µm）。λ = 系数 ÷ ν —— **同一串数换刻度**：
   每根谱线的功率不变，只换横坐标（不是把功率谱按密度换算，也不乘雅可比因子）。
   波长列是**降序**的（频率升 → 波长降），画之前倒一次（见 specXY）：一次置换，不产生新数。 */
const SPEC_AXIS = {
  freq: { btn: '横轴：频率 (THz)', legend: '频率 (THz)',
          fmax: '频率上限 (THz)', peak: '最强分量 (THz)', unit: 'THz' },
  wave: { btn: '横轴：波长 (µm)', legend: '波长 (µm)',
          fmax: '波长范围 (µm)', peak: '最强分量 (µm)', unit: 'µm' },
};

const SPEC_MIN_POINTS = 16;     // 比这少就不出图：几个点算出来的"谱"没有意义
const SPEC_BAD_SHOWN = 3;       // 不合格的点最多列几个（够定位就行，不刷屏）
const SPEC_FS_TO_THZ = 1e3;     // 1/fs = 1e15 Hz = 1000 THz：只换单位，不含任何物理系数
const SPEC_DB_FLOOR = -180;     // dB 下限：功率为 0 的频点 log 发散，压在它上面

let specLast = null;            // 最近一次算出来的谱（切 dB 照它重画，不重算）
let specSrc = null;             // 它是对哪一份取数结果算的：同一份就别再算一遍
let specScale = null;           // 算它的时候用的是哪种口径（换口径时间列就变了）
let specAxisTitle = '';         // 「频谱横轴」按钮原样的说明（禁用时在后面补"为什么切不了"）
let specChart = null;
let specChartDb = null;         // 上面那张图是按哪种纵轴画的（换读法要重建：图例写死了）
let specChartAxis = null;       // 上面那张图是按哪种横轴画的（同理：重建）
let specAxis = 'freq';          // 频谱横轴：freq = 频率 (THz) | wave = 波长 (µm)
let specDb = false;             // 纵轴：false = 功率 (ADU²)，true = dB（0 = 最强那根）

/* 这条序列的功率谱；不合格就返回 {error}（说清哪一点、为什么）。
   非均匀 DFT：频率格 k/T，每个频率上解一次 cos/sin 的最小二乘（2×2 正规方程），功率 = (A²+B²)/2。
   均匀采样时它就是教科书那个单边幅度谱的平方，非均匀采样时照样成立 —— 这正是"按真实点间距算"的意义。

   三角函数用**递推**省掉：固定一个点，第 k 格的相位正好是第 1 格的 k 倍，复数乘一步就到下一格
   （K 步累计误差 ~1e-11，可忽略）。2000 点 × 1000 格约 20 ms，所以同步算、不用等。 */
function pixelSpectrum(d) {
  const n = d.value.length;
  const time = d[dataTimeKey()];        // 时间列跟着横轴口径走（原值口径下那串数大 1/系数）
  const bad = [];
  for (let i = 0; i < n; i++) {
    const at = '第 ' + (i + 1) + ' 个点（idx ' + d.idx[i] + '）';
    const tf = time[i];
    if (tf === null || tf === undefined) bad.push(at + '没记下读出位置');
    else if (d.value[i] === null || d.value[i] === undefined) bad.push(at + '没采到图');
  }
  if (bad.length) {
    return { error: '这条扫描有 ' + bad.length + ' 个点不合格，整条做不了谱：' +
      bad.slice(0, SPEC_BAD_SHOWN).join('；') + (bad.length > SPEC_BAD_SHOWN ? ' 等' : '') +
      '。谱不补值、不插值、也不跳过 —— 缺一个点就是另一条采样序列。' };
  }
  if (n < SPEC_MIN_POINTS) {
    return { error: n ? '只有 ' + n + ' 个点，做不了谱（至少要 ' + SPEC_MIN_POINTS + ' 个）。'
                      : '这条扫描里还没有点。' };
  }

  let t0 = Infinity, t1 = -Infinity, mean = 0;
  for (let i = 0; i < n; i++) {
    const t = time[i];
    if (t < t0) t0 = t;
    if (t > t1) t1 = t;
    mean += d.value[i];
  }
  mean /= n;
  const span = t1 - t0;                     // fs：**真实首末跨度**，不是 点数 × 名义步距
  if (!(span > 0)) return { error: '所有点的读出位置都在同一处（跨度 0），做不了谱。' };
  const df = 1 / span;                      // 频率步长（1/fs）
  const kMax = Math.floor((n - 1) / 2);     // 上限 = 1/(2×平均间距)，正好落在第 kMax 格

  const scc = new Float64Array(kMax + 1), sss = new Float64Array(kMax + 1);
  const scs = new Float64Array(kMax + 1), yc = new Float64Array(kMax + 1);
  const ys = new Float64Array(kMax + 1);
  const ph1 = 2 * Math.PI * df;
  for (let i = 0; i < n; i++) {
    const vi = d.value[i] - mean;           // 去均值：直流不画
    const g = ph1 * (time[i] - t0);         // 第 1 格的相位
    const c1 = Math.cos(g), s1 = Math.sin(g);
    let c = c1, s = s1;
    for (let k = 1; k <= kMax; k++) {
      scc[k] += c * c; sss[k] += s * s; scs[k] += c * s;
      yc[k] += vi * c; ys[k] += vi * s;
      const nc = c * c1 - s * s1;           // 走到下一格
      s = s * c1 + c * s1; c = nc;
    }
  }

  const f = new Array(kMax), p = new Array(kMax);
  let peakI = 0;
  for (let k = 1; k <= kMax; k++) {
    const det = scc[k] * sss[k] - scs[k] * scs[k];
    let pw = 0;
    if (det > 0) {
      const A = (yc[k] * sss[k] - ys[k] * scs[k]) / det;
      const B = (ys[k] * scc[k] - yc[k] * scs[k]) / det;
      pw = (A * A + B * B) / 2;
    }
    f[k - 1] = k * df * SPEC_FS_TO_THZ;
    p[k - 1] = pw;
    if (pw > p[peakI]) peakI = k - 1;
  }
  if (!(p[peakI] > 0)) return { error: '这条序列一点起伏都没有（每个点的值都一样），做不了谱。' };
  return {
    f: f, p: p, n: n, span_fs: span, df_thz: df * SPEC_FS_TO_THZ,
    fmax_thz: kMax * df * SPEC_FS_TO_THZ, peakF: f[peakI], peakP: p[peakI],
  };
}

/* 图上的纵轴那一列：功率，或相对最强那根的 dB。**同一串数换刻度** —— 切 dB 不重算。 */
function specSeries() {
  if (!specDb) return specLast.p;
  const p = specLast.p, out = new Array(p.length);
  for (let i = 0; i < p.length; i++) {
    const db = 10 * Math.log10(p[i] / specLast.peakP);
    out[i] = db < SPEC_DB_FLOOR ? SPEC_DB_FLOOR : db;
  }
  return out;
}

function specYLabel() { return specDb ? '功率 (dB，0 = 最强分量)' : '功率 (ADU²)'; }

/* 波长换算的系数来自后端（取数响应里那份）—— 前端一个物理常数都不写。 */
function specC() { return dataLast ? dataLast.wavelength_um_per_thz : null; }

/* 能不能切到波长轴：系数拿到了就能。拿不到（老响应）就禁用并说清为什么。 */
function specAxisState() {
  const c = specC();
  if (!c) return { ok: false, why: '这次取数里没有波长换算系数（后端没给）—— 切不了。' };
  return { ok: true, why: '波长 = ' + c + ' µm·THz ÷ 频率' };
}

/* 图上的两列（横轴照当前读法、纵轴照功率/dB）。波长那一列倒过来画：uPlot 要横坐标递增，
   而 λ 随 ν 递减 —— 一次置换，每个点带着自己的值走，不产生新数。 */
function specXY() {
  const s = specLast, y = specSeries(), n = s.f.length;
  if (specAxis !== 'wave') return [s.f, y];
  const c = specC(), xs = new Array(n), ys = new Array(n);
  for (let i = 0; i < n; i++) {
    xs[i] = c / s.f[n - 1 - i];
    ys[i] = y[n - 1 - i];
  }
  return [xs, ys];
}

/* 横轴读法换一个：按钮、统计条标题、范围框单位一起换 */
function specAxisApplyLabels() {
  const ax = SPEC_AXIS[specAxis];
  setText('btn-spec-axis', ax.btn);
  setText('spec-fmax-k', ax.fmax);
  setText('spec-peak-k', ax.peak);
}

/* 数字别写成 0.00000483：功率跨好几个量级 */
function specNum(x) {
  if (!(x > 0)) return '0';
  return (x >= 1e4 || x < 0.01) ? x.toExponential(2).replace('e+', 'e') : x.toPrecision(4);
}

/* 取数结果 → 频谱卡。同一份数据再来一次（换读法 / 换连线顺序 / 切 dB）不重算。 */
function renderSpectrum(d) {
  // 停在波长轴、这条响应却没有波长系数（老后端）→ **退回频率轴**：宁可显示频率，
  // 也不能拿 undefined 当横坐标画出一张空图。按钮那边会把原因写在 title 上。
  if (specAxis === 'wave' && !specC()) { specAxis = 'freq'; specAxisApplyLabels(); }
  if (specSrc !== d || specScale !== dataScale) {   // 口径换了，时间列就换了，谱要重算
    specLast = pixelSpectrum(d);
    specSrc = d;
    specScale = dataScale;
  }
  const spec = specLast;
  const empty = $('spec-empty');
  if (spec.error) {
    setText('spec-title', '—');
    ['spec-n', 'spec-span', 'spec-df', 'spec-fmax', 'spec-peak', 'spec-peakval']
      .forEach(function (id) { setText(id, '—'); });
    empty.hidden = false;
    empty.className = 'hint warn';       // 不合格是"这条做不了"，不是"还没取数"
    empty.textContent = spec.error;
    if (specChart) specChart.setData([[], []]);   // 别把上一条的谱留在屏幕上冒充这一条
    return;
  }
  empty.hidden = true;
  empty.className = 'hint';

  // 横轴或纵轴读法换了就得**重建这张图**：图例那一格是建图时写死的
  // （与像素曲线换横轴读法同一个原因）
  if (specChart && (specChartDb !== specDb || specChartAxis !== specAxis)) {
    specChart.destroy();
    specChart = null;
  }
  const data = specXY();
  if (!specChart) {
    specChartDb = specDb;
    specChartAxis = specAxis;
    specChart = makeChart('spec-chart', {
      xlabel: SPEC_AXIS[specAxis].legend,
      height: 180,
      series: [{ label: specYLabel(), stroke: '#7c3aed', width: 1.2,
                 points: { show: showPointsBelow(400), size: 4 } }],
      data: data,
    });
    // 滚轮/拖动挂在新图上（重建后旧元素没了，见 rangeBox.attach）
    specX.attach(specChart, 'x');
    specY.attach(specChart, 'y');
  } else {
    specChart.setData(data);
  }
  fitChart(specChart, 'spec-chart', 180);
  applySpecRange();                       // 没被手填过的轴按这条谱铺满

  // 横轴按钮能不能用：后端给了波长系数才能切
  const axSt = specAxisState();
  $('btn-spec-axis').disabled = !axSt.ok;
  $('btn-spec-axis').title = axSt.ok ? specAxisTitle : '切不了波长轴：' + axSt.why;

  setText('spec-title', '#' + d.scan_id + (d.name ? ' · ' + d.name : '') +
    '　像素 (' + d.x + ', ' + d.y + ') · 按采图顺序 · ' +
    (dataScale === 'raw' ? '读回原值' : '折算 µm'));
  setText('spec-n', String(spec.n));
  setText('spec-span', spec.span_fs.toFixed(1));
  setText('spec-df', spec.df_thz.toFixed(3));      // 分辨率永远按频率写（波长刻度上疏密不均）
  if (specAxis === 'wave') {
    const c = specC();
    setText('spec-fmax', (c / spec.fmax_thz).toFixed(3) + ' ~ ' + (c / spec.f[0]).toFixed(1));
    setText('spec-peak', (c / spec.peakF).toFixed(3));
  } else {
    setText('spec-fmax', spec.fmax_thz.toFixed(3));
    setText('spec-peak', spec.peakF.toFixed(3));
  }
  setText('spec-peakval', specNum(spec.peakP));
}

/* 频率轴（X）与纵轴（Y）**各是一个范围框**（同一套规矩，见 rangeBox），各钉各的标记、各一个「自动范围」。 */
function specApply() {
  if (!specChart) return;
  const wx = specX.window();
  if (wx) specChart.setScale('x', { min: wx[0], max: wx[1] });
  const wy = specY.window();
  if (wy) specChart.setScale('y', { min: wy[0], max: wy[1] });
}

const specX = rangeBox({
  min: 'spec-xmin', max: 'spec-xmax', pin: 'spec-xpin', fit: 'btn-spec-fit',
  decimals: 3, onApply: specApply,
  fitValues: function () {
    const s = specLast;
    if (!s || s.error) return null;         // 还没有谱就别去动输入框
    const xs = specXY()[0];                 // 当前读法下真正画出来的那一列（升序）
    const lo = xs[0], hi = xs[xs.length - 1];
    const pad = (hi - lo) * 0.05 || 0.001;
    return [Math.max(0, lo - pad), hi + pad];
  },
}).wire();

const specY = rangeBox({
  min: 'spec-ymin', max: 'spec-ymax', pin: 'spec-ypin', fit: 'btn-spec-yfit',
  decimals: 4, onApply: specApply,
  fitValues: function () {
    const s = specLast;
    if (!s || s.error) return null;
    const ys = specSeries();
    let lo = ys[0], hi = ys[0];
    for (let i = 1; i < ys.length; i++) {
      if (ys[i] < lo) lo = ys[i];
      if (ys[i] > hi) hi = ys[i];
    }
    const pad = (hi - lo) * 0.05 || Math.abs(hi) * 0.05 || 0.001;
    lo -= pad; hi += pad;
    // 两条边不许越：功率没有负的；dB 的天花板就是最强那根（0 dB）
    return [specDb ? lo : Math.max(0, lo), specDb ? Math.min(0, hi) : hi];
  },
}).wire();

function applySpecRange() { specX.apply(); specY.apply(); }

/* 横轴换读法：频率 ↔ 波长。**同一串数换刻度，不重算、不重新取数** —— 每根谱线的功率不变，
   只换横坐标。钉住的范围框按 λ = 系数 ÷ ν 换算过去（**顺序会翻**：频率高的那头波长短），
   所以窗口两端要对调着写。 */
function specAxisToggle() {
  const st = specAxisState();
  if (!st.ok) { toast(st.why, true); return; }
  const c = specC();
  const win = specX.pinned ? specX.window() : null;
  specAxis = specAxis === 'freq' ? 'wave' : 'freq';
  if (win && win[0] > 0) specX.set(c / win[1], c / win[0]);   // 来回都是这个式子（对合）
  specAxisApplyLabels();
  if (specLast && !specLast.error) renderSpectrum(specSrc);
}

/* 纵轴换读法：功率 ↔ dB。**同一串数换刻度，不重算、不重新取数**；
   但两个单位不是线性关系，换算不过去，所以纵轴重新按数据铺满（等于替你按一下「自动范围」）。 */
function specToggleDb() {
  specDb = !specDb;
  $('spec-ymin-k').textContent = specDb ? 'dB 最小' : '功率最小 (ADU²)';
  $('spec-ymax-k').textContent = specDb ? 'dB 最大' : '功率最大 (ADU²)';
  setText('btn-spec-db', specDb ? '纵轴：dB（0 = 最强）' : '纵轴：功率 (ADU²)');
  specY.pinned = false;
  if (specLast && !specLast.error) renderSpectrum(specSrc);
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


/* ==================== 数据页：扫描组与裁剪 ====================
   一组 = **同名的扫描**（后端按 scan.name 分）。裁剪把这一组每条扫描的每一帧都裁成同一块矩形、
   就地换掉原图 —— 之后取数只解这一小块，快十几倍（实测 2000 张 4.8 s → 0.3 s）。
   **坐标口径不变**：面板上这四个数、红框、后端收到的都是**原始坐标**，裁过的和没裁过的能混着比。
   不可逆，所以先把预检（哪几条能裁、为什么不能）和红框摆出来，再动手。 */
let cropRects = {};       // scan_id → [x0,y0,w,h]：历史表那一列照着它写「已裁剪」
let cropSel = null;       // 面板里这一组的预检结果（GET /api/crops/suggest）
let cropDef = { w: 400, h: 300 };   // 默认框尺寸由后端给（config.CROP_W/H），前端不写死

const cropPolling = poller(cropPoll, 700);   // 裁剪进度（cropPoll 是函数声明，提升到位）

function cropNameText(name) { return name ? name : '(没有名字)'; }

function cropInt(id) {
  const v = String($(id).value || '').trim();
  return /^\d+$/.test(v) ? parseInt(v, 10) : NaN;
}

/* 拉一次裁剪状态：默认框尺寸 + 每条扫描裁到哪一块（历史表那一列要照着写）。
   拿不到就当"都没裁过"（按钮能点，点了后端会如实拒），不猜、不缓存旧的。 */
async function loadCrops() {
  const d = await get('/api/crops');
  if (!d) return null;
  if (d.w) cropDef = { w: d.w, h: d.h };
  cropRects = {};
  (d.crops || []).forEach(function (c) { cropRects[c.id] = c.rect; });
  return d;
}

async function openCropPanel(name) {
  if (cropPolling.running()) { toast('上一次裁剪还在跑，等它结束', true); return; }
  const d = await get('/api/crops/suggest?name=' + encodeURIComponent(name) +
    '&w=' + cropDef.w + '&h=' + cropDef.h);
  if (!d) return;                       // 整组不能裁：request() 已经把原因弹出来了
  cropSel = d;
  setText('crop-name', cropNameText(name) + ' · ' + d.scans.length + ' 条扫描');
  // 走缩略图那条路（8 位映射 + 缓存）：16 位原值只占 0~1022，浏览器按满量程渲染是整片黑的，
  // 拿它当预览等于没预览 —— 看不出光斑在哪，就没法判断框住没有
  $('crop-img').src = thumbUrl(d.sample, 760);   // 要看得出光斑在哪，比格子里的缩略图大
  cropFillSuggested();
  cropScanList();
  $('crop-progress').hidden = true;
  $('croppanel').hidden = false;
}

/* 预检逐条列出来：能裁的才进这一批，不能裁的写清为什么（尺寸不一致 / 已经裁过 / 没有图） */
function cropScanList() {
  const box = $('crop-scans');
  box.innerHTML = '';
  let imgs = 0, n = 0;
  for (let i = 0; i < cropSel.scans.length; i++) {
    const s = cropSel.scans[i];
    const row = document.createElement('div');
    row.className = 'croprow' + (s.ok ? '' : ' dim');
    const head = document.createElement('b');
    head.textContent = '#' + s.id;
    const mid = document.createElement('span');
    mid.textContent = s.images + ' 张' + (s.size ? ' · ' + s.size[0] + '×' + s.size[1] : '') +
      (s.ok ? '' : ' · ' + s.why);
    row.appendChild(head);
    row.appendChild(mid);
    box.appendChild(row);
    if (s.ok) { imgs += s.images; n++; }
  }
  const r = cropSel.rect;
  const from = r.source === 'centroid'
    ? '质心在 x ' + r.cx_range[0] + '~' + r.cx_range[1] + '、y ' + r.cy_range[0] + '~' +
      r.cy_range[1] + '（抽样 ' + r.sampled + ' 帧），离框边最近还有 ' + r.margin + ' px'
    : '这些帧没有质心记录（老图），按可裁区域中心给的建议框';
  const b = cropSel.base;
  const cur = b ? '现在已经裁到 ' + b[2] + '×' + b[3] + '（原始坐标 x ' + b[0] + '~' +
    (b[0] + b[2] - 1) + '、y ' + b[1] + '~' + (b[1] + b[3] - 1) + '），再裁只能往里挑；' : '';
  setText('crop-info', n + ' 条可裁 · 共 ' + imgs + ' 张 · ' +
    (cropSel.bytes / 1048576).toFixed(1) + ' MB · 现在画面 ' + cropSel.frame[0] + '×' +
    cropSel.frame[1] + '；' + cur + from + '。' +
    (cropSel.note ? '　' + cropSel.note + '。' : ''));
}

function cropFillSuggested() {
  const r = cropSel.rect;
  $('crop-x0').value = r.x0;
  $('crop-y0').value = r.y0;
  $('crop-w').value = r.w;
  $('crop-h').value = r.h;
  cropDrawRect();
}

/* 红框 = 将要留下的那一块（框外压暗）。按百分比定位，不用知道图显示多大。
   已经裁过的组：图上那张就是**现在这块地**（base，原始坐标），所以要先减掉 base 的原点，
   再除以画面尺寸 —— 四个数本身永远是原始坐标，一个都没换算过。 */
function cropDrawRect() {
  const box = $('crop-rect');
  if (!cropSel || !cropSel.frame) { box.hidden = true; return; }
  const x0 = cropInt('crop-x0'), y0 = cropInt('crop-y0');
  const w = cropInt('crop-w'), h = cropInt('crop-h');
  const fw = cropSel.frame[0], fh = cropSel.frame[1];
  const bx = cropSel.base ? cropSel.base[0] : 0;
  const by = cropSel.base ? cropSel.base[1] : 0;
  if (!(w > 0) || !(h > 0) || !(x0 >= 0) || !(y0 >= 0)) { box.hidden = true; return; }
  box.hidden = false;
  box.style.left = Math.min(100, Math.max(0, (x0 - bx) / fw * 100)) + '%';
  box.style.top = Math.min(100, Math.max(0, (y0 - by) / fh * 100)) + '%';
  box.style.width = Math.min(100, w / fw * 100) + '%';
  box.style.height = Math.min(100, h / fh * 100) + '%';
  cropWarn(x0, y0, w, h);
}

/* 框住没有：把**后端给的**质心范围与手里这四个数摆在一起比一下。
   质心是整幅图的亮度重心（含背景），不等于亮斑中心，所以这是提醒、不是判据 ——
   但范围都跑到框外去了，就说明有帧的亮心多半被切掉了（这个切掉是不可逆的）。
   这一步只看数、不发请求，所以改一个数就能立刻跟着变。 */
function cropWarn(x0, y0, w, h) {
  const el = $('crop-warn');
  const r = cropSel && cropSel.rect;
  if (!r || !r.cx_range || !r.cy_range) { el.hidden = true; return; }
  const b = cropSel.base || [0, 0, r.frame_w, r.frame_h];
  const bad = [], gone = [];      // bad = 这一刀会切掉的；gone = 上一刀就已经切在外面的
  const fits = function (lo, hi, o, s) { return o <= lo && hi <= o + s - 1; };
  const axes = [['x', r.cx_range, b[0], b[2], x0, w], ['y', r.cy_range, b[1], b[3], y0, h]];
  for (let i = 0; i < axes.length; i++) {
    const ax = axes[i][0], rg = axes[i][1];
    if (!fits(rg[0], rg[1], axes[i][2], axes[i][3])) {
      gone.push(ax + ' ' + rg[0] + '~' + rg[1] +
        '（可裁的只剩 ' + axes[i][2] + '~' + (axes[i][2] + axes[i][3] - 1) + '）');
    } else if (!fits(rg[0], rg[1], axes[i][4], axes[i][5])) {
      bad.push(ax + ' ' + rg[0] + '~' + rg[1] +
        '（框里是 ' + axes[i][4] + '~' + (axes[i][4] + axes[i][5] - 1) + '）');
    }
  }
  el.hidden = !bad.length && !gone.length;
  // 两种情形分开说：混在一起会把"上一刀留下的旧账"说成"这一刀会切掉"，那就没人信这条提醒了
  el.className = bad.length ? 'hint warn' : 'hint';
  if (bad.length) {
    setText('crop-warn', '注意：这一刀会把质心范围切在外面 —— ' + bad.join('，') +
      '。质心是整幅亮度重心、不等于亮斑中心，但超出就说明有帧的亮心会被切掉（切掉就找不回来了）。' +
      (gone.length ? '另外 ' + gone.join('，') + ' 是**上一刀**就已经在框外的。' : ''));
  } else if (gone.length) {
    setText('crop-warn', '提醒：' + gone.join('，') +
      ' —— 这是**上一刀**就切在框外的，救不回来；这一刀动不到它，只是说明这一组里还有那样的帧。');
  }
}

async function runCrop() {
  if (!cropSel) return;
  const x0 = cropInt('crop-x0'), y0 = cropInt('crop-y0');
  const w = cropInt('crop-w'), h = cropInt('crop-h');
  const fw = cropSel.frame[0], fh = cropSel.frame[1];
  const b = cropSel.base;
  const out = b ? (x0 < b[0] || y0 < b[1] || x0 + w > b[0] + b[2] || y0 + h > b[1] + b[3])
                : (x0 + w > fw || y0 + h > fh);
  if (!(w >= 16) || !(h >= 16) || !(x0 >= 0) || !(y0 >= 0) || out ||
      (b && w * h >= b[2] * b[3])) {
    // 前端这一道只为省一次往返：真正的拒绝在后端（同一个判据，别只信这里）
    toast(b ? ('这一组现在就是 ' + b[2] + '×' + b[3] + '（原始坐标 x ' + b[0] + '~' +
               (b[0] + b[2] - 1) + '、y ' + b[1] + '~' + (b[1] + b[3] - 1) +
               '）—— 只能往里裁、而且要更小')
            : ('矩形要填在画面 ' + fw + '×' + fh + ' 里，宽高至少 16'), true);
    return;
  }
  const r = await request('POST', '/api/crops/run?name=' + encodeURIComponent(cropSel.name) +
    '&x0=' + x0 + '&y0=' + y0 + '&w=' + w + '&h=' + h);
  if (!r) return;
  $('btn-crop-run').disabled = true;
  $('crop-progress').hidden = false;
  setText('crop-progress', '正在裁剪 0 / ' + r.images + ' 张 …（原图在全部校验通过之前不会动）');
  $('croppanel').hidden = true;
  cropPollStart();
}

function cropPollStart() { cropPolling.start(); }   // 立刻先问一次，再按节拍跟进度

function cropPollStop() {
  cropPolling.stop();
  $('btn-crop-run').disabled = false;
}

async function cropPoll() {
  const d = await get('/api/crops');
  if (!d) return;
  const j = d.job || {};
  if (j.running) {
    setText('crop-progress', '正在裁剪 ' + j.done + ' / ' + j.total + ' 张 …');
    return;
  }
  cropPollStop();
  if (j.error) {
    setText('crop-progress', '裁剪失败，整组已回滚（原图一点没动）：' + j.error);
    toast('裁剪失败：' + j.error, true);
  } else if (j.result) {
    const cut = j.result.edge_peak >= j.result.peak ? ' —— 边框上就有峰值，可能切到光斑了' : '';
    setText('crop-progress', '裁完了：' + j.result.scans + ' 条扫描、' + j.result.frames +
      ' 张，省 ' + (j.result.saved_bytes / 1048576).toFixed(1) + ' MB｜框内峰值 ' +
      j.result.peak + '、边框最大 ' + j.result.edge_peak + cut);
    toast('裁剪完成');
  }
  $('crop-progress').hidden = false;
  await loadCrops();
  loadHistory();            // 裁完历史表那一列要立刻变成「已裁剪」
  // 正在看的那条就在这一组里就重画一次：现在读的是裁剪后的图，取到的值必须一模一样
  const cur = dataScanPick();
  if (cur && dataLast && cur.name === j.group) drawPixelCurve();
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
  $('tab-data').onclick = function () { showView('data'); };

  // 数据页：选扫描、填像素、画曲线。换扫描时**已经画过就跟着重画** ——
  // 不然左边写着 #52、图上还是 #51 的那条，看着像没生效。
  $('btn-data-refresh').onclick = loadDataScans;
  $('btn-crop-close').onclick = function () { $('croppanel').hidden = true; };
  $('btn-crop-fit').onclick = function () { cropFillSuggested(); };
  $('btn-crop-run').onclick = runCrop;
  ['crop-x0', 'crop-y0', 'crop-w', 'crop-h'].forEach(function (id) {
    $(id).oninput = cropDrawRect;      // 改一个数，红框跟着动（不改后端任何东西）
  });
  $('btn-data-draw').onclick = drawPixelCurve;
  $('btn-data-axis').onclick = dataAxisToggle;
  $('btn-data-scale').onclick = dataScaleToggle;   // 口径：折算 µm ↔ 设备读回原值
  dataScaleTitle = $('btn-data-scale').title;      // 原样的说明（禁用时在后面补"为什么不能切"）
  $('data-source').onchange = dataSourceSave;      // 指认「这条是哪台设备采的」
  $('btn-spec-db').onclick = specToggleDb;     // 纵轴：功率 ↔ dB（同一串数换刻度，不重算）
  $('btn-spec-axis').onclick = specAxisToggle; // 横轴：频率 ↔ 波长（同一串数换刻度，不重算）
  specAxisTitle = $('btn-spec-axis').title;    // 原样的说明（禁用时在后面补"为什么切不了"）
  specAxisApplyLabels();                       // 按钮与统计条标题的初值
  // 横轴范围框的接线（手填 = 钉住、「自动范围」= 松钉）由 rangeBox.wire 自己接好了
  // 连线顺序：换一下就照手里那份重画，不重新取数（置换是纯显示的活）
  $('data-order').onchange = function () {
    dataOrder = $('data-order').value;
    if (dataLast) renderPixelCurve(dataLast);
    else $('data-order-note').hidden = dataOrder !== 'sort';
  };
  dataAxisApplyLabels();     // 按钮 / 跨度标题 / 范围框单位的初值，都由 DATA_AXIS 说了算
  $('data-scan').onchange = function () {
    dataScanInfo();
    if (dataChart) drawPixelCurve();
  };

  $('btn-ccd-live').onclick = function () { if (ccdLive) ccdLiveStop(); else ccdLiveStart(); };
  $('btn-ccd-grab').onclick = ccdGrab;
  $('btn-ccd-rot').onclick = ccdRotate;
  $('btn-ccd-reopen').onclick = ccdReopen;
  $('btn-grabs-refresh').onclick = loadGrabs;
  // 曝光改完立刻生效（数字框用 change，回车或失焦才发，别每敲一个字符就打设备）
  $('ccd-preview-ms').onchange = ccdSetExposure;

  // 纵轴范围框同上（手填过就不再自动铺满：想用同一个刻度对比两张图、或看真实动态范围）

  $('btn-trace').onclick = function () { if (traceActive) traceStop(); else traceStart(); };
  $('trace-secs').oninput = syncTraceUI;
  // 两个轴各有各的框（rangeBox 自己接好了手填），但「自动范围」是**一对**：
  // 这个按钮的意思是"都别钉了，按数据铺满"，所以两个一起松。
  $('btn-trace-fit').onclick = function () {
    traceX.pinned = false; traceY.pinned = false; applyTraceRange();
  };

  $('btn-refresh').onclick = loadHistory;

  $('history').addEventListener('click', async function (ev) {
    const b = ev.target.closest('button');
    if (!b) return;
    if (b.dataset.open) {
      pinnedScanId = Number(b.dataset.open);
      loadScan(pinnedScanId);
      return;
    }
    // 一组 = 同名的扫描：点哪一条都是裁整组，所以名字从行上带着走（面板里会把整组列出来）
    if (b.dataset.crop) {
      openCropPanel(b.dataset.name || '');
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
      profPickFrom('lightbox', $(t.id), ev);
      return;
    }
    if (t.id === 'lightbox') closeLightbox();     // 点背景才关（点图现在是取剖面）
  });

  $('ccd-img').onclick = function (ev) {
    if (!ccdLive) return;                     // 没在取帧就没有"过该点的整行"可取
    profPickFrom('preview', $('ccd-img'), ev);
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
  enterTo('data-x', 'btn-data-draw');
  enterTo('data-y', 'btn-data-draw');
}

/* ==================== 启动 ==================== */

wire();
openStream();
renderTraceStats();   // 空统计 + 空态提示；曲线图等第一次记录再建，别摆一个空坐标系
syncTraceUI();
showView(location.hash.slice(1));   // 认 hash：刷新和双标签都停在自己那一页
linkPoll.start();   // 立刻问一次再按节拍问；没有它指示灯会永远停在启动瞬间的"已断开"
loadHistory();
loadGrabs();   // 原始帧列表：存一帧就多一张，开机先列出来
// 相机可用性等切到「预览」那一页再问（首屏不必为一个可能用不到的设备多发一个请求）

// 关页面/刷新时把预览停掉：不然后端会继续按 15 fps 取帧，白占着相机
window.addEventListener('beforeunload', ccdLiveStop);

// 心跳只用于让后端知道界面还在；后端不会因为界面掉线而停扫描
beat.start();
