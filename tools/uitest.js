/*
 * 前端逻辑测试：用假 DOM 把 app.js 真跑起来。
 * 不需要浏览器、不需要服务、不碰硬件，纯 Node。
 *
 * 用法（项目根目录）：node tools/uitest.js
 * 也可以喂别的文件，用来验证测试本身是否敏感：node tools/uitest.js 某个变体.js
 *
 * ---- 覆盖范围（别把它当整页回归）----
 * 覆盖：A-F 点位载入状态机；G 乱序返回保护；H renderStage 的按钮可用性、表单播种、状态文案；
 *       I 历史列表转义；J caps 文案；K 位置曲线的窗口两端、统计口径、坐标范围钉住；
 *       L 界面不许编数、状态可见性；M 两页视图的切换与重新量宽。
 *       （相机预览那块只有"加载 app.js 不炸"这一层被覆盖，预览时序未测。）
 * 不覆盖：CSS 与布局、uPlot 的实际绘制、真实 SSE 时序与 EventSource 重连、拖拽与键盘交互。
 *         uPlot 对 setData([[],[]]) 的处理是静态阅读 vendor bundle 得出的结论，未在浏览器实跑。
 *         DOM 只模拟到"表格里有几行、每行 HTML 是什么"，不模拟样式与事件冒泡。
 *
 * ---- 两条刻意的严格设定（都是被评审抓出假阳性后加的）----
 * 1. getElementById 只认 index.html 里真实存在的 id —— 否则 app.js 引用拼错的 id 时
 *    测试不会像浏览器那样 TypeError。
 * 2. fetch 支持 delayMs（走真 setTimeout）—— 否则"响应跨帧到达/乱序"这类回归测不出来。
 */
'use strict';
const fs = require('fs');
const vm = require('vm');

const APP = process.argv[2] || 'frontend/app.js';
const src = fs.readFileSync(APP, 'utf8');

/* ---------- 只接受 index.html 里真实存在的 id ---------- */
const html = fs.readFileSync('frontend/index.html', 'utf8');
const VALID_IDS = new Set(Array.from(html.matchAll(/id="([^"]+)"/g), (m) => m[1]));

/* ---------- 假 DOM ---------- */
function mkEl(isFragment) {
  const e = {
    textContent: '', className: '', value: '', disabled: false, hidden: false,
    style: {}, dataset: {}, clientWidth: 600, onclick: null, src: '',
    isFragment: !!isFragment, _children: [], _html: '',
    appendChild(c) {
      // 与真实 DOM 一致：插入文档片段是把它里面的孩子搬过来，片段本身不进树
      if (c && c.isFragment) {
        for (let i = 0; i < c._children.length; i++) e._children.push(c._children[i]);
        c._children.length = 0;
      } else {
        e._children.push(c);
      }
      return c;
    },
    addEventListener() {}, getAttribute() { return null; },
    classList: { contains() { return false; } }, closest() { return null; },
    // 真浏览器里 chartHeight() 用它量 uPlot 图例的高度；这里从来没建过真图，如实返回 null
    querySelector() { return null; },
  };
  Object.defineProperty(e, 'children', { get() { return e._children; } });
  Object.defineProperty(e, 'innerHTML', {
    get() { return e._html; },
    set(v) { e._html = String(v); if (e._html === '') e._children.length = 0; },
  });
  return e;
}
function mkTable() { const t = mkEl(); t.tBodies = [mkEl()]; return t; }

const elems = new Map();
let jogButtons = null;
let histHandler = null;
const doc = {
  getElementById(id) {
    if (!VALID_IDS.has(id)) return null;   // 浏览器里就是这个行为
    if (!elems.has(id)) {
      const el = (id === 'points' || id === 'history') ? mkTable() : mkEl();
      if (id === 'history') el.addEventListener = (t, h) => { if (t === 'click') histHandler = h; };
      elems.set(id, el);
    }
    return elems.get(id);
  },
  createElement() { return mkEl(); },
  createDocumentFragment() { return mkEl(true); },
  querySelectorAll() {
    if (!jogButtons) {
      jogButtons = [1, 2, 3, 4, 5, 6].map((n) => { const b = mkEl(); b.dataset.jog = String(n); return b; });
    }
    return jogButtons;
  },
  addEventListener() {},
};

/* ---------- 网络 ---------- */
const calls = [];
let route = () => ({ body: {} });
function fetchImpl(url, opts) {
  const method = (opts && opts.method) || 'GET';
  calls.push(method + ' ' + url);
  const r = route(method, url) || {};
  const resp = {
    ok: r.ok !== false,
    status: r.status || 200,
    json: () => Promise.resolve(r.body === undefined ? {} : r.body),
  };
  const delay = r.delayMs || 0;
  return delay ? new Promise((res) => setTimeout(() => res(resp), delay)) : Promise.resolve(resp);
}

/* ---------- 其它桩 ---------- */
function EventSource(url) { this.url = url; }
function ResizeObserver() { this.observe = () => {}; }
function uPlot(opts, data) {
  this.opts = opts; this.series = opts.series; this.data = data;
  this.scales = {};   // setScale 套用过的范围（真 uPlot 里是 u.scales[key].min/max）
}
uPlot.prototype.setData = function (d) { this.data = d; };
uPlot.prototype.setSize = function (s) { this.size = s; };   // 记下来：切页必须重新量宽
uPlot.prototype.setScale = function (k, limits) { this.scales[k] = { min: limits.min, max: limits.max }; };

/* window / performance 也要有：app.js 会在 window 上挂 beforeunload（关页面停预览），
   预览帧率用 performance.now() 算。假 DOM 里它们不存在会在加载 app.js 那一下就 ReferenceError。 */
const winStub = { addEventListener() {}, removeEventListener() {} };
const sandbox = {
  document: doc, window: winStub, location: { hash: '' }, fetch: fetchImpl,
  EventSource, ResizeObserver, uPlot, performance: { now: () => Date.now() },
  setInterval: () => 0, clearInterval: () => {}, setTimeout, clearTimeout, confirm: () => true, console,
};
vm.createContext(sandbox);
vm.runInContext(src, sandbox);

const run = (code) => vm.runInContext(code, sandbox);
const read = (expr) => vm.runInContext(expr, sandbox);
const tick = () => new Promise((r) => setImmediate(r));
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const rowsOf = (id) => read("$('" + id + "').tBodies[0].children.length");
const htmlOf = (id) => read("$('" + id + "').tBodies[0].children.map(function(r){return r.innerHTML;}).join('')");

/* ---------- 断言 ---------- */
let failed = 0;
function ok(name, cond, extra) {
  console.log((cond ? '  [PASS] ' : '  [FAIL] ') + name + (extra ? '  ' + extra : ''));
  if (!cond) failed++;
}
const scanState = (id, status) => ({
  scan_id: id, status, index: status === 'done' ? 5 : 1, count: 5,
  target_um: 1.0, actual_um: 1.0, message: '',
});
const stageState = (over) => Object.assign({
  connected: true, position: 1.0, target: 1.0, velocity: 100, servo: true,
  on_target: true, overflow: false, error_code: 0, travel_min: 0, travel_max: 100,
  stage_type: 'P-621.1CD', axis: 'X', serial: '1', updated_at: 0,
}, over || {});
// 把扫描 id 编进点位数据，这样能从表格 HTML 里认出"现在显示的是哪个扫描"
const scanDetail = (id) => ({
  id, name: 's' + id, status: 'done', start_um: 0, stop_um: id, count: 5, message: '',
  points: [
    { idx: 0, target_um: id, actual_um: id + 0.01, on_target: 1, settled_ms: 300, image_path: null },
    { idx: 1, target_um: id + 1, actual_um: id + 1.01, on_target: 1, settled_ms: 300, image_path: null },
  ],
});
const detailRoute = (m, u) => u.startsWith('/api/scans/')
  ? { body: scanDetail(Number(u.split('/').pop())) } : { body: [] };
const driveScan = (id, st) => run('renderScan(' + JSON.stringify(scanState(id, st)) + ')');
const driveStage = (over) => run('renderStage(' + JSON.stringify(stageState(over)) + ')');
const click = (dataset) => histHandler({ target: { closest: () => ({ dataset }) } });
const reset = () => run('autoHandledScanId=null; pinnedScanId=null; viewScanId=null; prevScanStatus=null;');
const count = (m) => calls.filter((c) => c === m).length;

(async () => {
  console.log('\n[A] 终态自动载入');
  reset(); calls.length = 0; route = detailRoute;
  driveScan(24, 'running'); await tick();
  ok('运行中不载入', count('GET /api/scans/24') === 0);
  driveScan(24, 'done'); await tick(); await tick();
  ok('终态自动载入一次', count('GET /api/scans/24') === 1);
  driveScan(24, 'done'); await tick(); await tick();
  ok('后续帧不重复载入', count('GET /api/scans/24') === 1);
  ok('视图绑定到 24', read('viewScanId') === 24, 'viewScanId=' + read('viewScanId'));
  ok('表格里确实是 #24 的点', htmlOf('points').indexOf('24.0000') >= 0 && rowsOf('points') === 2,
     rowsOf('points') + ' 行');

  console.log('\n[B] 运行中点"查看"同一扫描，终态必须重载');
  reset(); calls.length = 0; route = detailRoute;
  driveScan(30, 'running'); await tick();
  await click({ open: '30' }); await tick(); await tick();
  const afterClick = count('GET /api/scans/30');
  driveScan(30, 'done'); await tick(); await tick(); await tick();
  ok('终态恰好又载一次（不是零次也不是两次）', count('GET /api/scans/30') === afterClick + 1,
     afterClick + ' -> ' + count('GET /api/scans/30'));

  console.log('\n[C] 钉住别的扫描，不该被抢，也不能每帧重来');
  reset(); calls.length = 0; route = detailRoute;
  await click({ open: '22' }); await tick(); await tick();
  calls.length = 0;
  driveScan(31, 'running'); await tick();
  driveScan(31, 'done'); await tick(); await tick();
  driveScan(31, 'done'); await tick(); await tick();
  driveScan(31, 'done'); await tick(); await tick();
  ok('未抢走用户钉住的视图', count('GET /api/scans/31') === 0);
  ok('视图仍是 22', read('viewScanId') === 22, 'viewScanId=' + read('viewScanId'));
  ok('表格里仍是 #22 的点', htmlOf('points').indexOf('22.0000') >= 0);

  console.log('\n[D] 删掉最新扫描后，查看其它历史不能被清掉');
  reset(); calls.length = 0;
  route = (m, u) => m === 'DELETE' ? { body: { ok: true } } : detailRoute(m, u);
  driveScan(24, 'done'); await tick(); await tick();
  ok('先自动载入了 #24', htmlOf('points').indexOf('24.0000') >= 0);
  await click({ del: '24' }); await tick(); await tick(); await tick();
  ok('删除后表格清空', rowsOf('points') === 0, rowsOf('points') + ' 行');
  await click({ open: '22' }); await tick(); await tick(); await tick();
  calls.length = 0;
  driveScan(24, 'done'); await tick(); await tick();
  driveScan(24, 'done'); await tick(); await tick();
  ok('不再回头拉已删除的扫描', count('GET /api/scans/24') === 0);
  ok('视图仍是 22', read('viewScanId') === 22, 'viewScanId=' + read('viewScanId'));
  ok('表格里仍是 #22 的点（没被清掉）', htmlOf('points').indexOf('22.0000') >= 0 && rowsOf('points') === 2,
     rowsOf('points') + ' 行');

  console.log('\n[E] 载入失败不清视图');
  reset(); calls.length = 0; route = detailRoute;
  await click({ open: '22' }); await tick(); await tick();
  const titleBefore = read("$('chart-title').textContent");
  const rowsBefore = rowsOf('points');
  ok('已正常载入 #22', read('viewScanId') === 22, 'title=' + JSON.stringify(titleBefore));
  route = (m, u) => u === '/api/scans/99'
    ? { ok: false, status: 404, body: { detail: '扫描不存在' } } : { body: {} };
  run('loadScan(99)'); await tick(); await tick();
  ok('失败后视图绑定不变', read('viewScanId') === 22);
  ok('失败后标题没被清成"未载入"', read("$('chart-title').textContent") === titleBefore);
  ok('失败后表格内容没被清掉', rowsOf('points') === rowsBefore && htmlOf('points').indexOf('22.0000') >= 0,
     rowsOf('points') + ' 行');

  console.log('\n[F] 新扫描仍会自动载入');
  run('autoHandledScanId = 24; pinnedScanId = null; prevScanStatus = null;');
  calls.length = 0; route = detailRoute;
  driveScan(25, 'running'); await tick();
  driveScan(25, 'done'); await tick(); await tick();
  ok('新扫描自动载入', count('GET /api/scans/25') === 1);
  ok('表格换成了 #25 的点', htmlOf('points').indexOf('25.0000') >= 0);

  console.log('\n[G] 乱序返回：只认最后一次请求');
  reset(); calls.length = 0;
  route = (m, u) => u === '/api/scans/22' ? { body: scanDetail(22), delayMs: 60 }
    : u === '/api/scans/23' ? { body: scanDetail(23) } : { body: [] };
  click({ open: '22' });
  click({ open: '23' });
  await sleep(150); await tick(); await tick();
  ok('后点的那次胜出', read('viewScanId') === 23, 'viewScanId=' + read('viewScanId'));
  ok('表格是 #23 的点（迟到的 #22 没覆盖它）', htmlOf('points').indexOf('23.0000') >= 0);

  console.log('\n[H] renderStage：按钮可用性与表单播种');
  run('seeded = false; scanActive = false; stageState = null; stageCaps = null;');
  jogButtons.forEach((b) => { b.disabled = false; });

  driveStage({ connected: false, servo: false });
  ok('未连接时"重连"可点', read("$('btn-connect').disabled") === false);
  ok('未连接时不能移动', read("$('btn-move').disabled") === true);
  ok('未连接时不能设定速度', read("$('btn-vel').disabled") === true);
  ok('未连接时不能点动', jogButtons.every((b) => b.disabled === true));
  ok('未连接时不能停止', read("$('btn-stop').disabled") === true);
  ok('未连接时不能急停', read("$('btn-estop').disabled") === true);
  ok('状态灯显示未连接', read("$('pill-dev').className") === 'pill bad',
     JSON.stringify(read("$('pill-dev').textContent")));

  driveStage({ connected: true, servo: false });
  ok('伺服关时不能移动', read("$('btn-move').disabled") === true);
  ok('伺服关时不能点动', jogButtons.every((b) => b.disabled === true));
  ok('伺服关时"开启伺服"可点', read("$('btn-servo-on').disabled") === false);
  ok('已连接时"重连"变灰', read("$('btn-connect').disabled") === true);
  ok('伺服关时才能"释放"', read("$('btn-release').disabled") === false);
  ok('伺服状态灯为警告色', read("$('pill-servo').className") === 'pill warn',
     JSON.stringify(read("$('pill-servo').textContent")));
  ok('播种终点 = 10 µm', String(read("$('scan-stop').value")) === '10');
  ok('播种点数 = 11', String(read("$('scan-count').value")) === '11');
  ok('播种稳定延时 = 100 ms', String(read("$('scan-settle').value")) === '100');
  ok('播种起点 = 行程下限', String(read("$('scan-start').value")) === '0');
  ok('播种速度 = 设备速度', String(read("$('vel').value")) === '100');
  ok('显示行程范围', read("$('limits').textContent") === '0 – 100',
     JSON.stringify(read("$('limits').textContent")));
  ok('无错误时显示"正常"', read("$('errcode').textContent") === '正常');

  driveStage({ connected: true, servo: true });
  ok('连接且伺服开时可以移动', read("$('btn-move').disabled") === false);
  ok('连接且伺服开时可以点动', jogButtons.every((b) => b.disabled === false));
  ok('deviceReady() 为真', read('deviceReady()') === true);
  ok('伺服状态灯为正常色', read("$('pill-servo').className") === 'pill ok');
  ok('伺服已开时"开启伺服"变灰', read("$('btn-servo-on').disabled") === true);

  driveStage({ connected: true, servo: true, error_code: 7 });
  ok('错误码会显示出来', read("$('errcode').textContent") === '错误码 7',
     JSON.stringify(read("$('errcode').textContent")));
  driveStage({ connected: true, servo: true, overflow: true });
  ok('过冲时状态灯报警', read("$('pill-servo').className") === 'pill bad',
     JSON.stringify(read("$('pill-servo').textContent")));

  run('scanActive = true;');
  driveStage({ connected: true, servo: true });
  ok('扫描中禁止手动移动', read("$('btn-move').disabled") === true);
  ok('扫描中禁止开始新扫描', read("$('btn-scan-start').disabled") === true);
  ok('扫描中禁止改速度', read("$('btn-vel').disabled") === true);
  ok('扫描中仍可停止', read("$('btn-stop').disabled") === false);
  ok('扫描中仍可急停', read("$('btn-estop').disabled") === false);
  run('scanActive = false;');

  console.log('\n[I] 历史列表渲染');
  route = () => ({ body: [
    { id: 7, name: '<img src=x onerror=alert(1)>', start_um: 0, stop_um: 5, count: 6, done: 6,
      status: 'done', created_at: 1700000000 },
    { id: 8, name: '', start_um: 1, stop_um: 2, count: 3, done: 1, status: 'aborted', created_at: 1700000100 },
  ] });
  await run('loadHistory()'); await tick(); await tick();
  ok('列出两行', rowsOf('history') === 2, rowsOf('history') + ' 行');
  const hist = htmlOf('history');
  ok('扫描名被转义（无裸标签）', hist.indexOf('<img src=x') < 0 && hist.indexOf('&lt;img') >= 0);
  ok('状态显示中文', hist.indexOf('已完成') >= 0 && hist.indexOf('已中止') >= 0);
  ok('空名称显示占位符', hist.indexOf('—') >= 0);
  route = () => ({ body: [] });
  await run('loadHistory()'); await tick(); await tick();
  ok('空列表渲染 0 行', rowsOf('history') === 0);

  console.log('\n[J] caps：界面按设备能力改文案与可用性');
  const capsPI = { name: 'PI E-709', platform: 'Windows + Linux', has_on_target: true,
    has_stop_command: true, release_mode: 'servo_off', has_setpoint_ack: true,
    has_velocity: true, unit: 'µm', default_settle_ms: 100 };
  const capsXMT = { name: '芯明天 E53.D1S-H', platform: '仅 Windows', has_on_target: false,
    has_stop_command: false, release_mode: 'open_loop_zero', has_setpoint_ack: false,
    has_velocity: false, unit: 'µm', default_settle_ms: 300 };
  const driveCaps = (c) => run('renderCaps(' + JSON.stringify(c) + ')');

  driveCaps(capsXMT);
  driveStage({ connected: true, servo: true });
  // 稳定延时的默认值每台设备不同，由 caps 下发，前端不写死
  ok('XMT：稳定延时默认 300 ms', String(read("$('scan-settle').value")) === '300',
     String(read("$('scan-settle').value")));
  run("$('scan-settle').value = 250");      // 用户改过之后，后续 caps 不许再覆盖
  driveCaps(capsXMT);
  ok('重复下发 caps 不覆盖用户输入', String(read("$('scan-settle').value")) === '250',
     String(read("$('scan-settle').value")));
  run("$('scan-settle').value = 300");      // 回到「我们播的值、用户没改过」的样子
  driveCaps(capsPI);                         // 换设备：要按新设备重播
  ok('换设备后按新设备重播（PI 100 ms）', String(read("$('scan-settle').value")) === '100',
     String(read("$('scan-settle').value")));
  driveCaps(capsXMT);                        // 后面几条用例仍按 XMT 的文案走
  ok('无速度指令的设备：速度按钮变灰', read("$('btn-vel').disabled") === true);
  ok('无速度指令的设备：速度输入框也禁用', read("$('vel').disabled") === true);
  ok('停止按钮说明写"软停"', read("$('btn-stop').title").indexOf('软停') >= 0,
     JSON.stringify(read("$('btn-stop').title").slice(0, 16)));
  ok('急停按钮说明写"软停"而不是"+ STP"',
     read("$('btn-estop').title").indexOf('软停') >= 0
     && read("$('btn-estop').title").indexOf('+ STP') < 0,
     JSON.stringify(read("$('btn-estop').title").slice(0, 20)));
  ok('释放按钮说明写"切至开环"', read("$('btn-release').title").indexOf('开环') >= 0);
  ok('状态灯说"闭环保持"', read("$('pill-servo').textContent") === '闭环保持',
     JSON.stringify(read("$('pill-servo').textContent")));
  driveStage({ connected: true, servo: false });
  ok('释放后状态灯说"已切至开环（未保持）"',
     read("$('pill-servo').textContent") === '已切至开环（未保持）',
     JSON.stringify(read("$('pill-servo').textContent")));

  route = (m, u) => u === '/api/stop'
    ? { body: { ok: true, scan_aborted: false, stop: 'soft' } } : { body: [] };
  await run("$('btn-stop').onclick()"); await tick();
  ok('软停的提示语不说"保持伺服"', read("$('toast').textContent").indexOf('软停') >= 0
     && read("$('toast').textContent").indexOf('保持伺服') < 0,
     JSON.stringify(read("$('toast').textContent")));

  driveCaps(capsPI);
  driveStage({ connected: true, servo: true });
  ok('有速度指令的设备：速度按钮可点', read("$('btn-vel').disabled") === false);
  ok('有速度指令的设备：速度输入框可用', read("$('vel').disabled") === false);
  ok('停止按钮说明写 STP', read("$('btn-stop').title").indexOf('STP') >= 0);
  ok('释放按钮说明写"回弹"', read("$('btn-release').title").indexOf('回弹') >= 0);
  ok('状态灯说"伺服保持"', read("$('pill-servo').textContent") === '伺服保持',
     JSON.stringify(read("$('pill-servo').textContent")));

  console.log('\n[K] 位置曲线：窗口两端、统计口径、坐标范围');
  // 一段已知数据：位置 20.0000 / .0010 / .0020 / .0030 µm，间隔 0.25 s
  const POS = [20.0000, 20.0010, 20.0020, 20.0030];
  const traceWin = (from, pos, tgt) => ({
    from: from, to: from + 10, now: from + 10,
    ts: pos.map((p, i) => from + i * 0.25),
    position: pos,
    target: (tgt || pos.map(() => 20)),
  });
  const traceRoute = (from, pos, tgt) => (m, u) => u.indexOf('/api/trace') === 0
    ? { body: traceWin(from, pos, tgt) } : { body: [] };

  run('traceActive = false; traceTimer = null; tracePinned = false; traceLast = null;');
  ok('初始状态是"未记录"', read("$('trace-title').textContent") === '未记录',
     JSON.stringify(read("$('trace-title').textContent")));
  ok('初始统计是空的', read("$('tr-n').textContent") === '—' && read("$('tr-std').textContent") === '—');

  route = traceRoute(1000, POS);
  calls.length = 0;
  run('lastServerTs = 1000; traceUntil = Date.now() + 1e6;');
  run("$('trace-secs').value = '10'; traceStart()");
  await tick(); await tick();

  ok('记录中按钮变"停止记录"', read("$('btn-trace').textContent") === '停止记录',
     JSON.stringify(read("$('btn-trace').textContent")));
  ok('记录中锁住时长输入', read("$('trace-secs').disabled") === true);
  ok('记录中报实采点数（不承诺采样率）',
     read("$('trace-title').textContent").indexOf('已取') > 0,
     JSON.stringify(read("$('trace-title').textContent")));
  ok('请求两端都是绝对时刻', count('GET /api/trace?from=1000&to=1010') >= 1, calls.join(' | '));
  ok('X 轴零点 = 服务器锚点（不是本地墙钟）', read('traceChart.data[0][1]') === 0.25,
     String(read('traceChart.data[0][1]')));
  ok('点数', read("$('tr-n').textContent") === '4', read("$('tr-n').textContent"));
  ok('跨度', read("$('tr-span').textContent") === '0.75', read("$('tr-span').textContent"));
  ok('平均间隔 250 ms', read("$('tr-dt').textContent") === '250', read("$('tr-dt').textContent"));
  ok('均值', read("$('tr-mean').textContent") === '20.00150', read("$('tr-mean').textContent"));
  ok('标准差按样本口径（除以 n−1）= 1.29 nm', read("$('tr-std').textContent") === '1.3',
     read("$('tr-std').textContent"));
  ok('峰峰值 3 nm', read("$('tr-pp').textContent") === '3', read("$('tr-pp').textContent"));
  ok('目标没动过就显示目标值', read("$('tr-target').textContent") === '20.0000',
     read("$('tr-target').textContent"));
  ok('自动铺满：X 是 0~实际跨度', read('JSON.stringify(traceChart.scales.x)') === '{"min":0,"max":0.75}',
     read('JSON.stringify(traceChart.scales.x)'));
  // 这条抓的是真机上真会炸的地方：uPlot 绘制时把 points.show 当函数调，
  // 事后改成布尔 → 下一帧抛 "points.show is not a function"，整张图从此不再重绘
  ok('points.show 是函数（uPlot 会当函数调）',
     read('typeof traceChart.series[1].points.show') === 'function',
     read('typeof traceChart.series[1].points.show'));
  ok('自动铺满：Y 罩住数据且留了余量',
     read('traceChart.scales.y.min < 20 && traceChart.scales.y.max > 20.003'), read('JSON.stringify(traceChart.scales.y)'));

  run("$('trace-ymin').value = '19.9'; $('trace-ymax').value = '20.1'; tracePinned = true; applyTraceRange();");
  ok('手填的范围被套用', read('JSON.stringify(traceChart.scales.y)') === '{"min":19.9,"max":20.1}',
     read('JSON.stringify(traceChart.scales.y)'));
  run('renderTrace()');
  ok('再取一帧也不会被自动铺满顶掉（两段记录好对比）',
     read('JSON.stringify(traceChart.scales.y)') === '{"min":19.9,"max":20.1}',
     read('JSON.stringify(traceChart.scales.y)'));
  run("$('trace-xmin').value = '5'; $('trace-xmax').value = '1'; applyTraceRange();");
  ok('范围填反了就不套用（画出来是空图）', read('JSON.stringify(traceChart.scales.x)') === '{"min":0,"max":0.75}',
     read('JSON.stringify(traceChart.scales.x)'));
  run("$('btn-trace-fit').onclick()");
  ok('点「自动范围」恢复铺满', read('traceChart.scales.y.min < 20 && traceChart.scales.y.max > 20.003'),
     read('JSON.stringify(traceChart.scales.y)'));

  const before = count('GET /api/trace?from=1000&to=1010');
  run('traceUntil = 0;');                     // 让"到点收工"立刻成立
  await run('traceTick()'); await tick(); await tick();
  ok('到点自动收工', read('traceActive') === false);
  ok('到点那一帧取的就是完整窗口，不再重复取一次',
     count('GET /api/trace?from=1000&to=1010') === before + 1,
     before + ' -> ' + count('GET /api/trace?from=1000&to=1010'));
  ok('收工后按钮复原', read("$('btn-trace').textContent") === '记录 10 秒',
     JSON.stringify(read("$('btn-trace').textContent")));
  ok('收工后时长输入解锁', read("$('trace-secs').disabled") === false);
  ok('标题报出实际录到的一段', read("$('trace-title').textContent").indexOf('4 点') > 0,
     JSON.stringify(read("$('trace-title').textContent")));

  // 提前停：窗口里目标动过，标准差没有意义，界面得说实话
  route = traceRoute(2000, [20, 21], [20, 21]);
  run('traceUntil = Date.now() + 1e6; lastServerTs = 2000;');
  run("$('trace-secs').value = '10'; traceStart()");
  await tick(); await tick();
  ok('窗口内目标动过就不给一个数', read("$('tr-target').textContent") === '窗口内有移动',
     read("$('tr-target').textContent"));
  await run("$('btn-trace').onclick()"); await tick(); await tick();
  ok('可以提前停', read('traceActive') === false);
  ok('提前停也会补取最后一段', calls[calls.length - 1] === 'GET /api/trace?from=2000&to=2010',
     calls[calls.length - 1]);

  // 还没收到遥测帧时：锚点必须问服务器要
  route = (m, u) => u === '/api/trace?seconds=10'
    ? { body: { from: 3000, to: 3010, now: 3000, ts: [], position: [], target: [] } }
    : { body: { from: 3000, to: 3010, now: 3000, ts: [3000, 3000.25], position: [20, 20.001], target: [20, 20] } };
  calls.length = 0;
  run('traceActive = false; traceTimer = null; lastServerTs = null; traceUntil = Date.now() + 1e6;');
  run("$('trace-secs').value = '10'; traceStart()");
  await tick(); await tick();
  ok('没有遥测帧时先探服务器时间', calls.indexOf('GET /api/trace?seconds=10') === 0, calls.join(' | '));
  ok('锚点用服务器时间，不用本地墙钟', read('traceFrom') === 3000, String(read('traceFrom')));
  ok('之后按锚点取窗口', calls.indexOf('GET /api/trace?from=3000&to=3010') > 0, calls.join(' | '));
  await run('traceStop(true)'); await tick();
  ok('停得住', read('traceActive') === false && read('traceTimer') === null);

  // 后端拒绝（比如填了 600 秒）：提示一次就收工，别每 200 ms 弹一次，也别把已有曲线清掉
  route = (m, u) => u.indexOf('/api/trace') === 0
    ? { ok: false, status: 422, body: { detail: '窗口长度要在 1~60 秒之间' } } : { body: [] };
  run('traceActive = true; traceTimer = 0; traceUntil = Date.now() + 1e6; traceLast = { ts: [1, 1.25], position: [20, 20.001], target: [20, 20] };');
  await run('traceTick()'); await tick();
  ok('取数被拒就收工（不刷屏）', read('traceActive') === false && read('traceTimer') === null);
  ok('已有曲线不被清掉', read('traceLast.ts.length') === 2, String(read('traceLast.ts.length')));
  ok('提示语用后端的原话', read("$('toast').textContent").indexOf('窗口长度') >= 0,
     JSON.stringify(read("$('toast').textContent")));

  // 点位曲线同一处：老代码在第二次渲染时把它改成了布尔，整张图会冻住
  run('renderChart([{ idx: 0, target_um: 1, actual_um: 1.01 }, { idx: 1, target_um: 2, actual_um: 2.01 }])');
  run('renderChart([{ idx: 0, target_um: 3, actual_um: 3.01 }, { idx: 1, target_um: 4, actual_um: 4.01 }])');
  ok('点位曲线第二次渲染后 points.show 仍是函数',
     read('typeof chart.series[1].points.show') === 'function',
     read('typeof chart.series[1].points.show'));

  console.log('\n[L] 界面不许编数、状态要看得见');
  // XMT 没有速度指令：st.velocity 恒为 0，读数和输入框都不能出现这个编出来的 0
  run('stageCaps = null;');
  driveCaps(capsXMT);
  driveStage({ connected: true, servo: true });
  ok('无速度指令的设备：读数写"无此指令"', read("$('velocity').textContent") === '无此指令',
     JSON.stringify(read("$('velocity').textContent")));
  ok('无速度指令的设备：输入框清空并给占位',
     read("$('vel').value") === '' && read("$('vel').placeholder") === '本设备无速度指令',
     JSON.stringify([read("$('vel').value"), read("$('vel').placeholder")]));
  run('seeded = false;');
  driveStage({ connected: true, servo: true });
  ok('播种也不会把 0 写进速度框', read("$('vel').value") === '',
     JSON.stringify(read("$('vel').value")));
  driveCaps(capsPI);
  driveStage({ connected: true, servo: true });
  ok('有速度指令的设备：读数照常显示', read("$('velocity').textContent") === '100',
     JSON.stringify(read("$('velocity').textContent")));

  // 曲线是冻住的一段：目标被改过要说明白（精确比目标，不用噪声阈值）
  run('scanActive = false;');
  driveStage({ connected: true, servo: true, target: 25.0 });
  ok('目标改过就提示曲线是旧的',
     read("$('trace-flag').hidden") === false
     && read("$('trace-flag').textContent").indexOf('目标已改') === 0,
     JSON.stringify(read("$('trace-flag').textContent")));
  driveStage({ connected: true, servo: true, target: 20.0 });
  ok('目标没改就不提示（噪声不算移动）', read("$('trace-flag').hidden") === true);
  run('scanActive = true;');
  driveStage({ connected: true, servo: true, target: 20.0 });
  ok('扫描中直接说台子在动', read("$('trace-flag').textContent").indexOf('扫描进行中') === 0,
     JSON.stringify(read("$('trace-flag').textContent")));
  run('scanActive = false;');

  // 钉住是个状态，得看得见；没数据时不留空图框
  // 钉住是个状态，得看得见；空态也别留一块空图框
  run('traceActive = false; traceTimer = null;');
  // 先把空态推到「有数据」再推回「没数据」：只断言最终值的话，
  // 「根本没同步过」（初值恰好等于期望值）会蒙混过关 —— 变异测试抓的就是这个
  run('traceLast = { ts: [1, 2], position: [20, 20.001], target: [20, 20] }; tracePinned = false; syncTraceUI();');
  ok('有数据时空态提示收起来', read("$('trace-empty').hidden") === true,
     String(read("$('trace-empty').hidden")));
  ok('没钉住时不显示钉住标记', read("$('trace-pin').hidden") === true);
  run("$('trace-xmin').value = '1'; $('trace-xmin').oninput();");
  ok('手填范围后钉住标记出现', read("$('trace-pin').hidden") === false);
  run("$('btn-trace-fit').onclick()");
  ok('点「自动范围」后钉住标记消失', read("$('trace-pin').hidden") === true);
  run('traceLast = null; syncTraceUI();');
  ok('没数据时空态提示可见', read("$('trace-empty').hidden") === false);

  console.log('\n[M] 视图：三页分开，但拒绝永远在后端');
  run("location.hash = ''; showView('');");
  ok('没给视图名就落回「对准」', read('view') === 'align');
  ok('对准页可见、扫描页与预览页藏起来',
     read("$('view-align').hidden") === false && read("$('view-scan').hidden") === true &&
     read("$('view-ccd').hidden") === true);
  ok('对准页的标签高亮',
     read("$('tab-align').className") === 'tab active' && read("$('tab-scan').className") === 'tab' &&
     read("$('tab-ccd').className") === 'tab');

  run("showView('scan')");
  ok('切到扫描页：两页对调',
     read("$('view-align').hidden") === true && read("$('view-scan').hidden") === false);
  ok('扫描页的标签高亮', read("$('tab-scan').className") === 'tab active');
  ok('视图写进 hash（刷新、双标签各停一页都靠它）', read('location.hash') === 'scan',
     JSON.stringify(read('location.hash')));

  run("showView('ccd')");
  ok('切到预览页：只有预览页可见',
     read("$('view-ccd').hidden") === false && read("$('view-align').hidden") === true &&
     read("$('view-scan').hidden") === true);
  ok('预览页的标签高亮', read("$('tab-ccd').className") === 'tab active');
  ok('预览页也写 hash', read('location.hash') === 'ccd', JSON.stringify(read('location.hash')));

  run("showView('瞎写的')");
  ok('非法视图名落回「对准」', read('view') === 'align' && read("$('view-align').hidden") === false);

  // 藏起来的容器宽度是 0：切回来不重新量宽，图就是压扁的
  run('showView("scan")');
  ok('切到扫描页会重新量点位图的宽度', read('chart.size && chart.size.width') === 600,
     JSON.stringify(read('chart.size')));

  // 开始扫描自动切到扫描页（之后用户切走就不再抢）
  run('showView("align")');
  route = (m, u) => u === '/api/scans' ? { body: { scan_id: 9 } } : { body: [] };
  await run("$('btn-scan-start').onclick()"); await tick(); await tick();
  ok('开始扫描自动切到扫描页', read('view') === 'scan', read('view'));

  route = () => ({ body: [] });

  console.log('\n[N] 预览质心：后端算好坐标，前端只按比例摆（不量像素、不做阈值/背景扣除）');
  const cenBody = (centroid) => ({
    available: true, state: 'preview', open: true, model: 'CS165MU', serial: '1', frames: 7,
    exposure_us: 12000, gain: 0, gain_locked: true,
    exposure_min_us: 40, exposure_max_us: 26843432,
    preview_roi: [0, 0, 1440, 1080], full_roi: [0, 0, 1440, 1080], saturation_adu: 1022, centroid,
  });
  const cen = (over) => Object.assign(
    { cx: 718.2, cy: 540.0, sum: 6.339e8, peak: 463, saturated: 0, width: 1440, height: 1080 }, over || {});
  let ccdBody = cenBody(cen());
  route = (m, u) => (u.indexOf('/api/ccd/') === 0 ? { body: ccdBody } : { body: [] });

  run('showView("ccd")');
  await run('ccdLiveStart(true)'); await tick(); await tick();
  await run('ccdStatus()'); await tick();
  ok('预览开着时十字线画出来', read("$('ccd-cross').hidden") === false);
  // 718.2/1440 = 49.875%，540/1080 = 50% —— 百分比而不是像素：缩放/letterbox 都不会偏
  ok('十字线按比例落在质心上（49.875% / 50%）',
     Math.abs(parseFloat(read("$('ccd-cross').style.left")) - 49.875) < 1e-6 &&
     Math.abs(parseFloat(read("$('ccd-cross').style.top")) - 50) < 1e-6,
     read("$('ccd-cross').style.left") + ' / ' + read("$('ccd-cross').style.top"));
  ok('质心读数（px）', read("$('ccd-cen').textContent") === '718.20, 540.00',
     JSON.stringify(read("$('ccd-cen').textContent")));
  // 口径说明只能有一个来源：index.html 里写过一份，被这里的 innerHTML 覆盖过（评审抓到的）
  ok('口径说明由状态刷新写上（含传感器坐标与后端下发的饱和门限）',
     read("$('ccd-note').innerHTML").indexOf('强度加权重心') >= 0 &&
     read("$('ccd-note').innerHTML").indexOf('传感器坐标') >= 0 &&
     read("$('ccd-note').innerHTML").indexOf('1022') >= 0,
     JSON.stringify(read("$('ccd-note').innerHTML").slice(0, 80)));
  ok('ΣI 用科学计数法', read("$('ccd-sum').textContent") === '6.34e+8',
     JSON.stringify(read("$('ccd-sum').textContent")));
  ok('没饱和时不标警告', read("$('ccd-sat').textContent") === '463 / 0' &&
     read("$('ccd-sat').className") === '', JSON.stringify(read("$('ccd-sat').textContent")));

  // 峰值顶到满量程 1022 就是饱和：对称光斑削顶不偏，落在强度梯度上才会往亮侧偏
  ccdBody = cenBody(cen({ peak: 1022, saturated: 1234 }));
  await run('ccdStatus()'); await tick();
  ok('饱和时读数标出来并加警告样式',
     read("$('ccd-sat').textContent") === '1022 / 1234' && read("$('ccd-sat').className") === 'on',
     JSON.stringify(read("$('ccd-sat').textContent")) + ' ' + JSON.stringify(read("$('ccd-sat').className")));

  // 后端还没出帧（刚开预览）：读数留空、十字线别画在 (0,0)
  ccdBody = cenBody(null);
  await run('ccdStatus()'); await tick();
  ok('后端还没有帧时十字线收起、读数留空',
     read("$('ccd-cross').hidden") === true && read("$('ccd-cen').textContent") === '—',
     JSON.stringify(read("$('ccd-cen').textContent")));

  ccdBody = cenBody(cen());
  await run('ccdStatus()'); await tick();

  // 朝向：只转"看的方向"，一次 90°，四下一圈；保存的文件不受影响
  route = (m, u) => {
    if (u.indexOf('/api/ccd/rotation') === 0) {
      const deg = parseInt(u.split('deg=')[1], 10);
      return { body: Object.assign(cenBody(cen()), { rotation: deg }) };
    }
    return u.indexOf('/api/ccd/') === 0 ? { body: ccdBody } : { body: [] };
  };
  await run('ccdStatus()'); await tick();
  ok('默认朝向写"传感器原始"', read("$('ccd-rot').textContent") === '0°（传感器原始）',
     JSON.stringify(read("$('ccd-rot').textContent")));
  await run('ccdRotate()'); await tick();
  ok('点一下转 90°，并调了后端', read("$('ccd-rot').textContent") === '90°（仅预览）' &&
     calls.indexOf('POST /api/ccd/rotation?deg=90') >= 0,
     JSON.stringify(read("$('ccd-rot').textContent")));
  await run('ccdRotate()'); await tick();
  await run('ccdRotate()'); await tick();
  await run('ccdRotate()'); await tick();
  ok('连点四下转回 0°（一圈四档，不会越转越多）',
     read('ccdInfo.rotation') === 0 && read("$('ccd-rot').textContent") === '0°（传感器原始）',
     JSON.stringify(read('ccdInfo.rotation')));

  // 读数永远是**传感器坐标**（与保存的 PNG 同一套），十字线按朝向换算到显示帧上。
  // 期望值来自后端 test_clockwise_rotation_maps_the_centroid_this_way 那张表：
  // 传感器质心 (718.2, 540.0)，W=1440 H=1080
  const expectCross = async (deg, left, top) => {
    ccdBody = Object.assign(cenBody(cen()), { rotation: deg });
    await run('ccdStatus()'); await tick();
    const L = read("$('ccd-cross').style.left"), T = read("$('ccd-cross').style.top");
    ok('朝向 ' + deg + '°：十字线落在显示帧的正确位置', L === left && T === top, L + ' / ' + T);
    ok('朝向 ' + deg + '°：读数仍是传感器坐标（拿去对文件不用换算）',
       read("$('ccd-cen').textContent") === '718.20, 540.00', read("$('ccd-cen').textContent"));
  };
  await expectCross(90, '49.907%', '49.875%');
  await expectCross(180, '50.056%', '49.907%');
  await expectCross(270, '50.000%', '50.056%');
  await expectCross(0, '49.875%', '50.000%');
  route = (m, u) => (u.indexOf('/api/ccd/') === 0 ? { body: ccdBody } : { body: [] });
  await run('ccdStatus()'); await tick();

  run('ccdLiveStop()');
  ok('停预览后十字线收起（那个数是上一帧的残留，不该继续显示）',
     read("$('ccd-cross').hidden") === true && read("$('ccd-cen').textContent") === '—');
  route = () => ({ body: [] });

  console.log('\n[O] 图库大图：走 8 位映射，不能直接给浏览器看 16 位 PNG');
  run("showLightbox('images/对焦前.png')");
  const bigSrc = read("$('lightbox-img').src");
  ok('大图走缩略图接口（>>2 → JPEG），不是 /data 下的原始 PNG',
     bigSrc.indexOf('/api/grabs/thumb?') === 0 && bigSrc.indexOf('max_side=1440') > 0 &&
     bigSrc.indexOf('/data/') !== 0, JSON.stringify(bigSrc));
  ok('路径编码过（文件名可能是中文）',
     bigSrc.indexOf(encodeURIComponent('images/对焦前.png')) > 0, JSON.stringify(bigSrc));
  ok('给出原始 16 位 PNG 的下载链接（定量用）',
     read("$('lightbox-raw').href").indexOf('/data/images/') === 0 &&
     read("$('lightbox-raw').href").indexOf('%E5%AF%B9%E7%84%A6%E5%89%8D') > 0,
     JSON.stringify(read("$('lightbox-raw').href")));
  ok('说明条写出来（告诉用户显示的是 8 位映射）',
     read("$('lightbox-cap').hidden") === false && read("$('lightbox').hidden") === false);
  run("showLightbox('', 'fallback.jpg')");
  ok('没有原始路径时退回缩略图，且不显示说明条',
     read("$('lightbox-img').src") === 'fallback.jpg' && read("$('lightbox-cap').hidden") === true);

  console.log('\n[P] 图库格子：曝光一行 + 质心一行（都从文件自己身上读）');
  route = (m, u) => (u.indexOf('/api/grabs') === 0
    ? { body: { total: 2, items: [
        { name: 'a.png', path: 'images/a.png', exposure_us: 11995, centroid: [719.5, 539.25], mtime: 1 },
        { name: 'b.png', path: 'images/b.png', exposure_us: null, centroid: null, mtime: 0 },
      ] } }
    : { body: [] });
  await run('loadGrabs()'); await tick(); await tick();
  const capA = read("$('gallery').children[0].children[2].innerHTML");
  const capB = read("$('gallery').children[1].children[2].innerHTML");
  ok('有质心时按"质心 cx, cy"显示', capA.indexOf('质心 719.50, 539.25') >= 0, JSON.stringify(capA));
  ok('格子里的缩略图要 520（260 会把细结构平均成斑块，看着跟预览不是一张图）',
     read("$('gallery').children[0].children[0].src").indexOf('max_side=520') > 0,
     JSON.stringify(read("$('gallery').children[0].children[0].src")));
  ok('曝光与质心各占一行（质心是新加的那一行）',
     capA.indexOf('曝光 11.99 ms') >= 0 && capA.indexOf('<br>') > 0, JSON.stringify(capA));
  ok('老图没有就写"未记录"，不猜值',
     capB.indexOf('质心未记录') >= 0 && capB.indexOf('曝光未记录') >= 0, JSON.stringify(capB));

  // 大图上也带一份（单张比对时最常用）
  run("showLightbox('images/a.png', '', grabMeta('images/a.png'))");
  ok('大图说明里也标出曝光与质心（写明是传感器坐标）',
     read("$('lightbox-meta').textContent").indexOf('曝光 11.99 ms') >= 0 &&
     read("$('lightbox-meta').textContent").indexOf('质心(传感器) 719.50, 539.25') >= 0,
     JSON.stringify(read("$('lightbox-meta').textContent")));
  route = () => ({ body: [] });

  console.log('\n[Q] 点位表的图也走 8 位映射 + 大图说明条只有一个来源');
  reset(); calls.length = 0;
  route = (m, u) => (u.indexOf('/api/scans/') === 0
    ? { body: { id: 40, name: 's40', status: 'done', start_um: 0, stop_um: 1, count: 1, message: '',
                points: [{ idx: 0, target_um: 0, actual_um: 0, on_target: 1, settled_ms: 300,
                           image_path: 'images/scan0040_00000.png' }] } }
    : { body: [] });
  driveScan(40, 'done'); await tick(); await tick();
  const rowHtml = htmlOf('points');
  ok('点位表的缩略图走 /api/grabs/thumb（真机扫描帧是 16 位 PNG，直连 /data 是黑的）',
     rowHtml.indexOf('/api/grabs/thumb?path=') >= 0 && rowHtml.indexOf('src="/data/') < 0,
     JSON.stringify(rowHtml.slice(rowHtml.indexOf('<img'), rowHtml.indexOf('<img') + 120)));
  ok('带上 data-full，点开进同一套大图（含原始文件链接）',
     rowHtml.indexOf('data-full="images/scan0040_00000.png"') >= 0);

  // 说明条：图库里的帧立刻有；点位表的扫描帧要现问后端；没有路径时留空（不许留上一张的数字）
  run("showLightbox('images/a.png', '', { exposure_us: 11995, centroid: [1, 2] })");
  ok('图库的帧：说明条立刻写出来',
     read("$('lightbox-meta').textContent").indexOf('曝光 11.99 ms') >= 0,
     JSON.stringify(read("$('lightbox-meta').textContent")));
  route = (m, u) => (u.indexOf('/api/image/meta') === 0
    ? { body: { exposure_us: 8000, centroid: [10, 20] } } : { body: [] });
  run("showLightbox('images/scan0040_00000.png', '')");     // 第三个参数缺省 = 图库列表里没有它
  ok('扫描帧：先写"读取中…"（不猜）', read("$('lightbox-meta').textContent") === '读取中…',
     JSON.stringify(read("$('lightbox-meta').textContent")));
  await tick(); await tick();
  ok('扫描帧：取回文件自己的元数据后写上去',
     read("$('lightbox-meta').textContent").indexOf('曝光 8.00 ms') >= 0 &&
     read("$('lightbox-meta').textContent").indexOf('质心(传感器) 10.00, 20.00') >= 0,
     JSON.stringify(read("$('lightbox-meta').textContent")));
  run("showLightbox('', '/data/images/x.png')");
  ok('没有相对路径时说明条留空，也不留上一张的数字',
     read("$('lightbox-cap').hidden") === true && read("$('lightbox-meta').textContent") === '',
     JSON.stringify(read("$('lightbox-meta').textContent")));
  route = () => ({ body: [] });

  console.log('\n[R] 轮廓图：点图取整行/整列，原值上图，切线画在像素中心');
  const box = { left: 10, top: 20, width: 600, height: 450 };     // 1440×1080 的图显示成 600×450
  const boxJs = JSON.stringify(box);
  ok('点正中 → 图心 (720, 540)',
     read('JSON.stringify(profPixel(310, 245, ' + boxJs + ', 1440, 1080))') === '{"x":720,"y":540}',
     read('JSON.stringify(profPixel(310, 245, ' + boxJs + ', 1440, 1080))'));
  ok('点左上角 → (0, 0)',
     read('JSON.stringify(profPixel(10, 20, ' + boxJs + ', 1440, 1080))') === '{"x":0,"y":0}');
  ok('点右下角 → 最后一个像素 (1439, 1079)',
     read('JSON.stringify(profPixel(610, 470, ' + boxJs + ', 1440, 1080))') === '{"x":1439,"y":1079}');
  ok('点在图上边缘外 → null（不猜坐标）',
     read('profPixel(9, 20, ' + boxJs + ', 1440, 1080)') === null &&
     read('profPixel(310, 471, ' + boxJs + ', 1440, 1080)') === null);
  ok('图还没有像素尺寸时不猜坐标', read('profPixel(100, 100, ' + boxJs + ', 0, 0)') === null);

  const profPayload = {
    x: 720, y: 540, width: 1440, height: 1080, rotation: 90, bits: 16, full_scale: 1022,
    horizontal: [1, 2, 3], vertical: [4, 5],
  };
  run('profApply("preview", ' + JSON.stringify(profPayload) + ')');
  ok('水平剖面原样上图（不做任何处理）',
     read('JSON.stringify(profCharts.preview.h.data)') === '[[0,1,2],[1,2,3]]',
     read('JSON.stringify(profCharts.preview.h.data)'));
  ok('垂直剖面原样上图',
     read('JSON.stringify(profCharts.preview.v.data)') === '[[0,1],[4,5]]',
     read('JSON.stringify(profCharts.preview.v.data)'));
  ok('切线画在那一行/列的**像素中心**',
     read("$('ccd-cut').hidden") === false &&
     read("$('ccd-cut-v').style.left") === ((720 + 0.5) / 1440 * 100) + '%' &&
     read("$('ccd-cut-h').style.top") === ((540 + 0.5) / 1080 * 100) + '%',
     read("$('ccd-cut-v').style.left") + ' / ' + read("$('ccd-cut-h').style.top"));
  ok('读数写明坐标、尺寸、朝向、位深',
     read("$('prof-where').textContent").indexOf('(720, 540)') >= 0 &&
     read("$('prof-where').textContent").indexOf('1440×1080') >= 0 &&
     read("$('prof-where').textContent").indexOf('预览 90°') >= 0 &&
     read("$('prof-where').textContent").indexOf('16 位') >= 0,
     JSON.stringify(read("$('prof-where').textContent")));
  run('profApply("lightbox", ' + JSON.stringify(profPayload) + ')');
  ok('大图那一套也能画（读保存的 PNG，同一套画法）',
     read("$('lb-cut').hidden") === false &&
     read('JSON.stringify(profCharts.lightbox.h.data)') === '[[0,1,2],[1,2,3]]');
  run('profClear("preview")');
  ok('清掉之后切线收起、数据清空、提示复位',
     read("$('ccd-cut').hidden") === true &&
     read('JSON.stringify(profCharts.preview.h.data)') === '[[],[]]' &&
     read("$('prof-where').textContent") === '在预览图上点一下');


  console.log('\n[O] 重开相机（手动）：界面按 state 写字，意图在后端');
  const failedBody = Object.assign(cenBody(cen()),
    { state: 'failed', open: false, failure: 'TLCameraError: 设备已断开', centroid: null });
  route = (m, u) => (u.indexOf('/api/ccd/') === 0 ? { body: failedBody } : { body: [] });
  await run('ccdStatus()'); await tick();
  ok('掉线时状态行写「掉线：原因 + 点重开相机」',
     read("$('ccd-sub').textContent").indexOf('掉线：') >= 0 &&
     read("$('ccd-sub').textContent").indexOf('重开相机') >= 0,
     JSON.stringify(read("$('ccd-sub').textContent")));
  ok('掉线时画面收掉（不留上一帧）', read("$('ccd-img').hidden") === true);
  ok('掉线时取帧计时器停掉（ccdLive 由 state 派生）', read('ccdTimer') === null);

  // 重开成功：后端把状态带回 preview（意图在后端，界面只管显示）
  route = (m, u) => {
    if (u.indexOf('/api/ccd/reopen') === 0) return { body: cenBody(cen()) };
    return u.indexOf('/api/ccd/') === 0 ? { body: ccdBody } : { body: [] };
  };
  calls.length = 0;
  await run("$('btn-ccd-reopen').onclick()"); await tick(); await tick();   // 真的点按钮
  ok('点「重开相机」确实发 POST /api/ccd/reopen', calls.indexOf('POST /api/ccd/reopen') >= 0,
     JSON.stringify(calls.slice(-3)));
  ok('重开成功后画面恢复取帧（state=preview → ccdLive → 计时器起来）',
     read('ccdLive') === true && read('ccdTimer') !== null,
     JSON.stringify([read('ccdLive'), String(read('ccdTimer'))]));
  ok('预览按钮回到「停止预览」', read("$('btn-ccd-live').textContent") === '停止预览',
     JSON.stringify(read("$('btn-ccd-live').textContent")));
  ok('成功后有提示、状态行不再是掉线',
     read("$('toast').textContent").indexOf('相机已重开') >= 0 &&
     read("$('ccd-sub').textContent").indexOf('掉线：') < 0,
     JSON.stringify(read("$('ccd-sub').textContent")));

  // 重开失败：把后端的 503 原因照实弹出来，按钮回到可点
  route = (m, u) => (u.indexOf('/api/ccd/reopen') === 0
    ? { ok: false, status: 503, body: { detail: '没发现相机：检查 USB 连接与相机电源。' } }
    : (u.indexOf('/api/ccd/') === 0 ? { body: failedBody } : { body: [] }));
  await run('ccdReopen()'); await tick(); await tick();
  ok('重开失败时弹出后端的中文原因', read("$('toast').textContent").indexOf('没发现相机') >= 0,
     JSON.stringify(read("$('toast').textContent")));
  ok('失败后按钮仍可点（可以再试）', read("$('btn-ccd-reopen').disabled") === false);
  route = (m, u) => (u.indexOf('/api/ccd/') === 0 ? { body: ccdBody } : { body: [] });
  console.log('\n[S] 点位表：列数对得上，「间距」= 相邻两点实际之差');
  reset(); calls.length = 0;
  route = (m, u) => (u.indexOf('/api/scans/') === 0
    ? { body: { id: 42, name: 's42', status: 'done', start_um: 0, stop_um: 2, count: 3, message: '',
                points: [
                  { idx: 0, target_um: 0, actual_um: 0.02, on_target: 1, settled_ms: 300, image_path: null },
                  { idx: 1, target_um: 1, actual_um: 1.05, on_target: 1, settled_ms: 300, image_path: null },
                  { idx: 2, target_um: 2, actual_um: 2.01, on_target: 1, settled_ms: 300, image_path: null },
                ] } }
    : { body: [] });
  driveScan(42, 'done'); await tick(); await tick();
  const rows42 = read("$('points').tBodies[0].children.map(function(r){return r.innerHTML;})");
  const firstRow = rows42[0] || '';
  ok('每行 8 个格子', (rows42.join('').match(/<td/g) || []).length === 24,
     (rows42.join('').match(/<td/g) || []).length + ' 个');
  ok('第一行没有「上一点」，间距写「—」',
     firstRow.split('</td>')[3].indexOf('—') >= 0, JSON.stringify(firstRow.split('</td>')[3]));
  ok('间距 = 实际之差（1.05 − 0.02 = 1.0300）', rows42.join('').indexOf('1.0300') >= 0);
  ok('间距 = 实际之差（2.01 − 1.05 = 0.9600）', rows42.join('').indexOf('0.9600') >= 0);
  // 超过 MAX_ROWS 时补一行说明，它的 colspan 必须跟列数一致（否则表格会错位）
  const many = [];
  for (let i = 0; i < 2001; i++) many.push({ idx: i, target_um: i, actual_um: i, on_target: 1, settled_ms: 1, image_path: null });
  run('renderPoints(' + JSON.stringify(many) + ')');
  const cut = read("$('points').tBodies[0].children[2000].innerHTML");
  ok('截断行的 colspan = 8（与列数一致）', cut.indexOf('colspan="8"') >= 0, JSON.stringify(cut.slice(0, 60)));
  route = () => ({ body: [] });
  console.log(failed ? '\n===== ' + failed + ' 项失败 =====' : '\n===== 全部通过 =====');
  process.exit(failed ? 1 : 0);
})();
