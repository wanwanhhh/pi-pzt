/*
 * 前端逻辑测试：用假 DOM 把 app.js 真跑起来。
 * 不需要浏览器、不需要服务、不碰硬件，纯 Node。
 *
 * 用法（项目根目录）：node tools/uitest.js
 * 也可以喂别的文件，用来验证测试本身是否敏感：node tools/uitest.js 某个变体.js
 *
 * ---- 覆盖范围（别把它当整页回归）----
 * 覆盖：A-F 点位载入状态机；G 乱序返回保护；H renderStage 的按钮可用性、表单播种、状态文案。
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
function uPlot(opts, data) { this.opts = opts; this.series = opts.series; this.data = data; }
uPlot.prototype.setData = function (d) { this.data = d; };
uPlot.prototype.setSize = function () {};

const sandbox = {
  document: doc, fetch: fetchImpl, EventSource, ResizeObserver, uPlot,
  setInterval: () => 0, setTimeout, clearTimeout, confirm: () => true, console,
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
  run('seeded = false; scanActive = false; stageState = null;');
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

  console.log(failed ? '\n===== ' + failed + ' 项失败 =====' : '\n===== 全部通过 =====');
  process.exit(failed ? 1 : 0);
})();
