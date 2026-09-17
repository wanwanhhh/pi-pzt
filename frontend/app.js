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
      const loc = (item.loc || []).filter(function (p) { return p !== 'body'; }).join('.');
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
    $('btn-vel').title = '本设备没有速度指令：速度由闭环自身决定，后端会明确拒绝。';
  } else {
    $('vel').disabled = false;
    $('btn-vel').title = '';
  }
}

/* ==================== 渲染：设备 ==================== */

function renderStage(st) {
  stageState = st;

  setText('pos', fmt(st.position, 4));
  setText('target', fmt(st.target, 4));
  setText('ontarget', st.on_target ? '是' : '否');
  setText('velocity', String(st.velocity));
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
  const jog = document.querySelectorAll('[data-jog]');
  for (let i = 0; i < jog.length; i++) jog[i].disabled = !canMove;

  if (!seeded && st.connected) {
    seeded = true;
    $('vel').value = st.velocity;
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
  setPill('pill-scan',
    scanActive ? (paused ? '扫描已暂停' : '扫描中')
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

function chartWidth() {
  return Math.max(320, $('chart').clientWidth || 600);
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
      width: chartWidth(),
      height: 220,
      scales: { x: { time: false } },
      legend: { show: true },
      series: [
        { label: '序号' },
        { label: '偏差 (nm)', stroke: '#2563eb', width: 1.5,
          points: { show: points.length <= 200, size: 5 } }
      ],
      axes: [
        { stroke: '#64748b', grid: { stroke: '#e2e8f0' }, ticks: { stroke: '#cbd5e1' } },
        { stroke: '#64748b', grid: { stroke: '#e2e8f0' }, ticks: { stroke: '#cbd5e1' } }
      ]
    }, data, $('chart'));
    new ResizeObserver(function () {
      chart.setSize({ width: chartWidth(), height: 220 });
    }).observe($('chart'));
  } else {
    chart.series[1].points.show = xs.length <= 200;
    chart.setData(data);
  }
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
    if (t.classList && t.classList.contains('thumb')) {
      $('lightbox-img').src = t.getAttribute('src');
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
  ['scan-name', 'scan-start', 'scan-stop', 'scan-count', 'scan-settle'].forEach(function (id) {
    enterTo(id, 'btn-scan-start');
  });
}

/* ==================== 启动 ==================== */

wire();
openStream();
pollLink();
setInterval(pollLink, 1000);   // 没有这行指示灯会永远停在启动瞬间的"已断开"
loadHistory();

// 心跳只用于让后端知道界面还在；后端不会因为界面掉线而停扫描
fetch('/api/heartbeat', { method: 'POST' }).catch(function () {});
setInterval(function () {
  fetch('/api/heartbeat', { method: 'POST' }).catch(function () {});
}, 2000);
