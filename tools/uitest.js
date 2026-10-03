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
 *       L 界面不许编数、状态可见性；M 两页视图的切换与重新量宽；
 *       N–V 预览质心与图库、轮廓图、点位表、数据页（像素曲线 / 横轴范围与手势 / 裁剪 / 频谱 /
 *       位置口径：折算 µm ↔ 设备读回原值 / 频谱横轴：频率 ↔ 波长）。
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
    _listeners: {},          // 记下来：测试能自己派事件（横轴的滚轮缩放与拖动平移）
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
    addEventListener(t, fn) { (e._listeners[t] = e._listeners[t] || []).push(fn); },
    removeEventListener(t, fn) {
      const a = e._listeners[t] || [];
      const i = a.indexOf(fn);
      if (i >= 0) a.splice(i, 1);
    },
    dispatch(t, ev) { (e._listeners[t] || []).slice().forEach((fn) => fn(ev)); },
    // 手势要量像素：给个固定的假矩形（宽 400 → 一个像素多少值算得清）
    getBoundingClientRect() { return { left: 0, top: 0, width: 400, height: 200 }; },
    getAttribute() { return null; },
    classList: { add() {}, remove() {}, contains() { return false; } }, closest() { return null; },
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
const calls = [];      // "方法 URL"，按顺序
const posts = [];      // 带请求体的那些：{url, body}（body 是 JSON 字符串，与原样发给后端的一致）
let route = () => ({ body: {} });
function fetchImpl(url, opts) {
  const method = (opts && opts.method) || 'GET';
  calls.push(method + ' ' + url);
  // 请求体另记一份（calls 的格式别动：一堆用例在按精确字符串比对）
  if (opts && opts.body !== undefined) posts.push({ url: url, body: opts.body });
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
/* 计数是为了断言"只起一个/只挂一个"：setInterval 与 ResizeObserver 一旦重复注册，
   在真浏览器里的症状分别是"停不干净、后台还在跑"和"旧观察者对着不存在的图重排"。 */
let intervalCount = 0;
let observerCount = 0;
function EventSource(url) { this.url = url; }
function ResizeObserver() { observerCount++; this.observe = () => {}; }
function uPlot(opts, data) {
  this.opts = opts; this.series = opts.series; this.data = data;
  this.scales = {};    // setScale 套用过的范围（真 uPlot 里是 u.scales[key].min/max）
  this.over = mkEl();  // 真 uPlot 建的 .u-over：滚轮/拖动挂在它上面（destroy 后它就没了）
}
uPlot.prototype.setData = function (d) { this.data = d; };
uPlot.prototype.setSize = function (s) { this.size = s; };   // 记下来：切页必须重新量宽
uPlot.prototype.setScale = function (k, limits) { this.scales[k] = { min: limits.min, max: limits.max }; };
// 真 uPlot 的 destroy 会把画布和图例一起拆掉：app.js 换横轴读法时靠它把旧图例（写着 µm 的那行）丢掉
uPlot.prototype.destroy = function () { this.destroyed = true; };
/* 像素 → 值：真 uPlot 按绘图区做线性映射，桩里按 over 的宽度（400px）插值 ——
   「缩放锚点不动」那条用例靠它算鼠标底下是哪个值。 */
uPlot.prototype.posToVal = function (px, key) {
  const s = this.scales[key] || { min: 0, max: 1 };
  return s.min + (px / 400) * (s.max - s.min);
};

/* window / performance 也要有：app.js 会在 window 上挂 beforeunload（关页面停预览），
   预览帧率用 performance.now() 算。假 DOM 里它们不存在会在加载 app.js 那一下就 ReferenceError。 */
const winStub = { addEventListener() {}, removeEventListener() {} };
const sandbox = {
  document: doc, window: winStub, location: { hash: '' }, fetch: fetchImpl,
  EventSource, ResizeObserver, uPlot, performance: { now: () => Date.now() },
  setInterval: () => { intervalCount++; return intervalCount; }, clearInterval: () => {},
  setTimeout, clearTimeout, confirm: () => true, console,
  __stats: () => ({ intervals: intervalCount, observers: observerCount }),
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

  run('traceActive = false; tracePoll.stop(); traceX.pinned = false; traceY.pinned = false; traceLast = null;');
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

  run("$('trace-ymin').value = '19.9'; $('trace-ymax').value = '20.1'; $('trace-ymin').oninput()");
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
  // 两个轴**各钉各的**：位置曲线的 X 与 Y 是两个范围框，填了 Y 不该把 X 一起钉住
  // （从前两个轴共用一个标记，填一个数两个轴就都不再按数据铺满了）。
  run("$('trace-ymin').value = '19.9'; $('trace-ymax').value = '20.1'; $('trace-ymin').oninput()");
  ok('只填 Y：Y 钉住、X 照旧跟着数据铺满（角标亮着说明有轴被钉住）',
     read('traceY.pinned') === true && read('traceX.pinned') === false &&
     read("$('trace-pin').hidden") === false &&
     read('JSON.stringify(traceChart.scales.x)') === '{"min":0,"max":0.75}',
     read('traceX.pinned') + '/' + read('traceY.pinned') + ' x=' + read('JSON.stringify(traceChart.scales.x)'));
  run('renderTrace()');
  ok('再取一帧：X 跟着新数据重铺，Y 保持填的那个窗',
     read('JSON.stringify(traceChart.scales.y)') === '{"min":19.9,"max":20.1}' &&
     read('traceChart.scales.x.min') === 0,
     read('JSON.stringify(traceChart.scales.y)'));
  run("$('btn-trace-fit').onclick()");

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
  run('traceActive = false; tracePoll.stop(); lastServerTs = null; traceUntil = Date.now() + 1e6;');
  run("$('trace-secs').value = '10'; traceStart()");
  await tick(); await tick();
  ok('没有遥测帧时先探服务器时间', calls.indexOf('GET /api/trace?seconds=10') === 0, calls.join(' | '));
  ok('锚点用服务器时间，不用本地墙钟', read('traceFrom') === 3000, String(read('traceFrom')));
  ok('之后按锚点取窗口', calls.indexOf('GET /api/trace?from=3000&to=3010') > 0, calls.join(' | '));
  await run('traceStop(true)'); await tick();
  ok('停得住', read('traceActive') === false && read('tracePoll.running()') === false);

  // 后端拒绝（比如填了 600 秒）：提示一次就收工，别每 200 ms 弹一次，也别把已有曲线清掉
  route = (m, u) => u.indexOf('/api/trace') === 0
    ? { ok: false, status: 422, body: { detail: '窗口长度要在 1~60 秒之间' } } : { body: [] };
  run('traceActive = true; traceUntil = Date.now() + 1e6; traceLast = { ts: [1, 1.25], position: [20, 20.001], target: [20, 20] }; tracePoll.start();');
  await tick(); await tick();
  ok('取数被拒就收工（不刷屏）', read('traceActive') === false && read('tracePoll.running()') === false);
  ok('已有曲线不被清掉', read('traceLast.ts.length') === 2, String(read('traceLast.ts.length')));
  ok('提示语用后端的原话', read("$('toast').textContent").indexOf('窗口长度') >= 0,
     JSON.stringify(read("$('toast').textContent")));

  // 轮询只有一份实现（poller）：**不许重复启动** —— 重复启动会丢掉旧句柄，
  // 之后 stop 只停得掉一个，另一个在后台接着跑（状态刷新会反复调 syncCcdTimer，最容易踩）
  run('traceActive = false; tracePoll.stop();');
  const iv0 = read('__stats().intervals');
  run('tracePoll.start(); tracePoll.start(); tracePoll.start();');
  ok('重复 start 只起一个定时器',
     read('__stats().intervals') === iv0 + 1 && read('tracePoll.running()') === true,
     read('__stats().intervals') + ' vs ' + iv0);
  run('tracePoll.stop(); tracePoll.stop();');
  ok('重复 stop 不炸，停完是真的停了',
     read('__stats().intervals') === iv0 + 1 && read('tracePoll.running()') === false);

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
  run('traceActive = false; tracePoll.stop();');
  // 先把空态推到「有数据」再推回「没数据」：只断言最终值的话，
  // 「根本没同步过」（初值恰好等于期望值）会蒙混过关 —— 变异测试抓的就是这个
  run('traceLast = { ts: [1, 2], position: [20, 20.001], target: [20, 20] }; traceX.pinned = false; traceY.pinned = false; syncTraceUI();');
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
  // 纵轴范围：与位置曲线同一套规矩（输入框是唯一的准）。整行与整列共用一条纵轴。
  ok('自动范围：按数据铺满并写回输入框（1~5 留余量 → 0~7）',
     Number(read("$('prof-ymin').value")) === 0 && Number(read("$('prof-ymax').value")) === 7,
     read("$('prof-ymin').value") + ' ~ ' + read("$('prof-ymax').value"));
  run("$('prof-ymin').value = '0'; $('prof-ymax').value = '1022'; $('prof-ymin').oninput()");
  ok('钉住后：整行与整列用同一条纵轴（两张图才可比）',
     read('JSON.stringify(profCharts.preview.h.scales.y)') === '{"min":0,"max":1022}' &&
     read('JSON.stringify(profCharts.preview.v.scales.y)') === '{"min":0,"max":1022}',
     read('JSON.stringify(profCharts.preview.h.scales.y)'));
  ok('大图弹窗那两张也认同一个刻度',
     read('JSON.stringify(profCharts.lightbox.h.scales.y)') === '{"min":0,"max":1022}',
     read('JSON.stringify(profCharts.lightbox.h.scales.y)'));
  run('profApply("preview", ' + JSON.stringify(profPayload) + ')');
  ok('再取一段也不会被自动铺满顶掉', 
     read('JSON.stringify(profCharts.preview.h.scales.y)') === '{"min":0,"max":1022}');
  run("$('prof-ymin').value = '900'; $('prof-ymax').value = '100'; applyProfRange();");
  ok('范围填反了就不套用（留着上一次的范围）',
     read('JSON.stringify(profCharts.preview.h.scales.y)') === '{"min":0,"max":1022}',
     read('JSON.stringify(profCharts.preview.h.scales.y)'));
  run("$('btn-prof-fit').onclick()");
  ok('点「自动范围」恢复按数据铺满',
     read('JSON.stringify(profCharts.preview.h.scales.y)') === '{"min":0,"max":7}',
     read('JSON.stringify(profCharts.preview.h.scales.y)'));

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
  ok('掉线时取帧计时器停掉（ccdLive 由 state 派生）', read('ccdPoll.running()') === false);

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
     read('ccdLive') === true && read('ccdPoll.running()') === true,
     JSON.stringify([read('ccdLive'), read('ccdPoll.running()')]));
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
  ok('每行 9 个格子（含曝光）', (rows42.join('').match(/<td/g) || []).length === 27,
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
  ok('截断行的 colspan = 9（与列数一致）', cut.indexOf('colspan="9"') >= 0, JSON.stringify(cut.slice(0, 60)));
  console.log('\n[T] 数据处理页：指定像素在各扫描点上的值（只读盘上的数据）');
  run('dataChart = null; dataScans = []; dataSeq = 0;');
  const dataScanRow = (id) => ({
    id, name: '', start_um: 0, stop_um: 10, count: 5, done: 5, status: 'done',
    created_at: 1790240166, message: '',
  });
  // 一条 5 点、其中第 3 点没图的序列：位置是**读出位置**（不是序号）
  const pixelBody = (id) => ({
    scan_id: id, name: '', status: 'done', count: 5,
    x: 700, y: 540, width: 1440, height: 1080, bits: 16, full_scale: 1022, missing: 1,
    idx: [0, 1, 2, 3, 4],
    position_um: [5.0413, 5.4481, 5.9526, 6.3236, 6.8],
    // time_fs **故意跟位置不成比例**（真换算是 6.6713 fs/µm）：前端要是自己乘了个系数，
    // 下面「横轴画的是后端给的那一列」那条就会红。
    time_fs: [10, 20, 30, 40, 50],
    // 位置 → 时间的系数（真值 2/c）：界面只用它换算「横轴范围」那两个框，不碰曲线上的数
    time_fs_per_um: 6.6712819,
    value: [122, 107, null, 105, 113],
  });
  const dataRoute = (m, u) => {
    if (u === '/api/scans') return { body: [dataScanRow(52), dataScanRow(51)] };
    const px = u.match(/^\/api\/scans\/(\d+)\/pixel/);
    if (px) return { body: pixelBody(Number(px[1])) };
    return { body: [] };
  };

  calls.length = 0; route = dataRoute;
  run("showView('data')");
  await tick(); await tick();
  ok('切到数据页：只有数据页可见',
     read("$('view-data').hidden") === false && read("$('view-align').hidden") === true &&
     read("$('view-scan').hidden") === true && read("$('view-ccd').hidden") === true);
  ok('数据页标签高亮、hash 记下来（刷新还在这一页）',
     read("$('tab-data').className") === 'tab active' && read('location.hash') === 'data',
     read("$('tab-data').className") + ' / ' + read('location.hash'));
  ok('进页就问扫描列表', count('GET /api/scans') === 1, JSON.stringify(calls));
  const opts = read("Array.prototype.map.call($('data-scan').children, function(o){return o.value + ':' + o.textContent;})");
  ok('下拉框按后端给的顺序列出（最新在前）',
     opts.length === 2 && opts[0].indexOf('52:') === 0 && opts[1].indexOf('51:') === 0,
     JSON.stringify(opts));
  ok('没选过就默认最新那条（#52）', read("$('data-scan').value") === '52',
     read("$('data-scan').value"));
  ok('这一页只读：发出去的全是 GET，没有一条控制/设备请求',
     calls.length > 0 && calls.every((c) => c.indexOf('GET ') === 0), JSON.stringify(calls));

  run("$('data-x').value = '700'; $('data-y').value = '540';");
  calls.length = 0;
  await run("$('btn-data-draw').onclick()"); await tick(); await tick();
  ok('画曲线：把扫描 id 与像素坐标原样发给后端',
     calls.indexOf('GET /api/scans/52/pixel?x=700&y=540') >= 0, JSON.stringify(calls));
  ok('横轴是后端给的**读出位置**（不是序号）',
     read('JSON.stringify(dataChart.data[0])') === JSON.stringify([5.0413, 5.4481, 5.9526, 6.3236, 6.8]),
     read('JSON.stringify(dataChart.data[0])'));
  ok('缺图的点是 null：线在那儿断开（不许拿邻点顶上）',
     read('JSON.stringify(dataChart.data[1])') === JSON.stringify([122, 107, null, 105, 113]),
     read('JSON.stringify(dataChart.data[1])'));
  ok('统计：扫描点 / 有值 / 没图的点',
     read("$('data-n').textContent") === '5' && read("$('data-ok').textContent") === '4' &&
     read("$('data-missing').textContent") === '1',
     [read("$('data-n').textContent"), read("$('data-ok').textContent"),
      read("$('data-missing').textContent")].join(' / '));
  ok('统计：位置跨度按读出位置算（6.8 − 5.0413 = 1.759）',
     read("$('data-span').textContent") === '1.759', read("$('data-span').textContent"));
  ok('统计：峰值 = 有值那些点里的最大值（122；缺图那点不参与）',
     read("$('data-peak').textContent") === '122', read("$('data-peak').textContent"));
  ok('幅面 / 位深 / 满量程照文件说（1440×1080 · 16 位 · 1022）',
     read("$('data-size').textContent") === '1440×1080 · 16 位 · 1022',
     read("$('data-size').textContent"));
  ok('标题写明哪条扫描、哪个像素',
     read("$('data-title').textContent").indexOf('#52') >= 0 &&
     read("$('data-title').textContent").indexOf('(700, 540)') >= 0,
     JSON.stringify(read("$('data-title').textContent")));
  ok('有值就不显示空态提示', read("$('data-empty').hidden") === true);

  // 换扫描：已经画过就得跟着重画（不然左边写 #51、图上还是 #52 那条）
  calls.length = 0;
  run("$('data-scan').value = '51'; $('data-scan').onchange()");
  await tick(); await tick();
  ok('换扫描自动重画，且问的是新那条',
     calls.indexOf('GET /api/scans/51/pixel?x=700&y=540') >= 0 &&
     read("$('data-title').textContent").indexOf('#51') >= 0,
     JSON.stringify(calls) + ' / ' + read("$('data-title').textContent"));

  // 输入校验：空框不能当成 0（Number('') === 0 会静默变成左上角那个像素）
  calls.length = 0;
  run("$('data-x').value = ''; $('data-y').value = '540';");
  await run("$('btn-data-draw').onclick()"); await tick();
  ok('空坐标不发请求、弹提示',
     calls.length === 0 && read("$('toast').textContent").indexOf('非负整数') >= 0,
     JSON.stringify(calls) + ' / ' + JSON.stringify(read("$('toast').textContent")));
  run("$('data-x').value = '700.5';");
  await run("$('btn-data-draw').onclick()"); await tick();
  ok('小数不是像素坐标：不发请求', calls.length === 0, JSON.stringify(calls));
  run("$('data-x').value = '-3';");
  await run("$('btn-data-draw').onclick()"); await tick();
  ok('负数不发请求', calls.length === 0, JSON.stringify(calls));

  // 后端拒绝（点在图外）：照实弹后端的中文原因，标题不许停在"读图中…"
  run("$('data-x').value = '9999';");
  route = (m, u) => (u.indexOf('/pixel') > 0
    ? { ok: false, status: 400, body: { detail: '点 (9999, 540) 超出画面 1440×1080' } }
    : dataRoute(m, u));
  await run("$('btn-data-draw').onclick()"); await tick(); await tick();
  ok('越界时弹出后端的原因',
     read("$('toast').textContent").indexOf('超出画面') >= 0,
     JSON.stringify(read("$('toast').textContent")));
  ok('失败后标题还原（不留"读图中…"）',
     read("$('data-title').textContent").indexOf('#51') >= 0,
     JSON.stringify(read("$('data-title').textContent")));
  ok('失败不清掉上一次画出来的曲线',
     read('JSON.stringify(dataChart.data[1])') === JSON.stringify([122, 107, null, 105, 113]),
     read('JSON.stringify(dataChart.data[1])'));

  // 整条扫描都没有帧（CCD 后端是 null，或图被删光）：如实说，不画一条空坐标系
  run("$('data-x').value = '700';");
  route = (m, u) => (u.indexOf('/pixel') > 0
    ? { body: { scan_id: 7, name: '', status: 'done', count: 3, x: 700, y: 540,
                width: null, height: null, bits: null, full_scale: null, missing: 3,
                idx: [0, 1, 2], position_um: [1, 2, 3], time_fs: [6.7, 13.3, 20],
                value: [null, null, null] } }
    : dataRoute(m, u));
  await run("$('btn-data-draw').onclick()"); await tick(); await tick();
  ok('没有帧时说清楚，而不是画成"像素全黑"',
     read("$('data-empty').hidden") === false &&
     read("$('data-empty').textContent").indexOf('没有可读的帧') >= 0,
     JSON.stringify(read("$('data-empty').textContent")));
  ok('幅面/位深没有就说没有（不猜一个 1440×1080 出来）',
     read("$('data-size').textContent") === '—', read("$('data-size').textContent"));
  ok('没有值就没有峰值（不是 0）', read("$('data-peak').textContent") === '—',
     read("$('data-peak').textContent"));

  // 刷新列表不许把用户挑好的那条换掉
  route = dataRoute;
  run("$('data-scan').value = '51'; loadDataScans()");
  await tick(); await tick();
  ok('刷新列表保留已选的那条', read("$('data-scan').value") === '51',
     read("$('data-scan').value"));

  // 横轴换读法：位置 ↔ 时间。两种读法是**同一串数换刻度**（后端两列一起给下来），
  // 所以切换既不该再取一次数、也不该自己乘系数 —— 乘出来的曲线看着一样，错在系数上没人看得出。
  console.log('\n[T] 数据页横轴：位置 ↔ 时间（换算在后端，切换不取数）');
  run("dataAxis = 'pos'; dataLast = null; dataChart = null; dataChartAxis = null;");
  run("$('data-scan').value = '52'; $('data-x').value = '700'; $('data-y').value = '540';");
  route = dataRoute;
  calls.length = 0;
  await run("$('btn-data-draw').onclick()"); await tick(); await tick();
  ok('默认画位置：横轴是后端给的读出位置',
     read('JSON.stringify(dataChart.data[0])') === JSON.stringify([5.0413, 5.4481, 5.9526, 6.3236, 6.8]),
     read('JSON.stringify(dataChart.data[0])'));
  // 初值写在 index.html 里（假 DOM 不会解析 HTML，所以拿文件本身跟 DATA_AXIS 对一遍：
  // 两边写岔了就是"打开页面写着位置、点一下变成时间 / 时间"那种错位）
  ok('按钮与跨度标题的初值写在 index.html 里，且与 DATA_AXIS 对得上',
     html.indexOf('>' + read('DATA_AXIS.pos.um.btn') + '</button>') >= 0 &&
     html.indexOf('>' + read('DATA_AXIS.pos.um.span') + '</dt>') >= 0 &&
     html.indexOf('>口径：' + read('DATA_SCALE_CN.um') + '</button>') >= 0,
     read('DATA_AXIS.pos.um.btn') + ' / ' + read('DATA_AXIS.pos.um.span'));

  run('axisOldChart = dataChart;');
  calls.length = 0;
  run("$('btn-data-axis').onclick()");
  await tick();
  ok('点一下换成时间：横轴画的是后端给的那一列（不是前端乘出来的）',
     read('JSON.stringify(dataChart.data[0])') === JSON.stringify([10, 20, 30, 40, 50]),
     read('JSON.stringify(dataChart.data[0])'));
  ok('切换一个请求都不发（两千张 PNG 现读要几秒，切换等不起）',
     calls.length === 0, JSON.stringify(calls));
  ok('纵轴那一列原样不动（换的只是横轴刻度）',
     read('JSON.stringify(dataChart.data[1])') === JSON.stringify([122, 107, null, 105, 113]),
     read('JSON.stringify(dataChart.data[1])'));
  ok('图例单位跟着换：旧图拆掉、新图按新读法建',
     read('dataChart.series[0].label') === '时间 (fs)' && read('axisOldChart.destroyed') === true,
     read('dataChart.series[0].label') + ' / destroyed=' + read('axisOldChart.destroyed'));
  ok('按钮与跨度标题都换成时间',
     read("$('btn-data-axis').textContent") === '横轴：时间 (fs)' &&
     read("$('data-span-k').textContent") === '时间跨度 (fs)',
     read("$('btn-data-axis').textContent") + ' / ' + read("$('data-span-k').textContent"));
  ok('跨度按时间算（50 − 10 = 40）', read("$('data-span').textContent") === '40.000',
     read("$('data-span').textContent"));

  run("$('btn-data-axis').onclick()");
  await tick();
  ok('再点一下换回位置',
     read('JSON.stringify(dataChart.data[0])') === JSON.stringify([5.0413, 5.4481, 5.9526, 6.3236, 6.8]) &&
     read("$('btn-data-axis').textContent") === '横轴：位置 (µm)',
     read('JSON.stringify(dataChart.data[0])') + ' / ' + read("$('btn-data-axis').textContent"));

  run('axisOldChart = dataChart;');
  await run("$('btn-data-draw').onclick()"); await tick(); await tick();
  ok('读法没变就接着用这张图（不白重建，也不重复挂尺寸观察者）',
     read('dataChart === axisOldChart') === true, 'same=' + read('dataChart === axisOldChart'));

  // 横轴范围：两个框是唯一的准，没手填过就按这一段数据铺满（与位置曲线同一套规矩）
  console.log('\n[T] 数据页横轴范围：填了钉住、自动铺满、钉住时换读法跟着换算');
  run("dataAxis = 'pos'; dataLast = null; dataChart = null; dataChartAxis = null;");
  run("dataX.pinned = false; dataXs = [];");
  run("$('data-scan').value = '52'; $('data-x').value = '700'; $('data-y').value = '540';");
  run("$('data-xmin').value = ''; $('data-xmax').value = '';");
  route = dataRoute;
  await run("$('btn-data-draw').onclick()"); await tick(); await tick();
  ok('没手填过：按这一段数据铺满（5.0413~6.8 两端各留 5%）',
     read("$('data-xmin').value") === '4.953' && read("$('data-xmax').value") === '6.888',
     read("$('data-xmin').value") + ' ~ ' + read("$('data-xmax').value"));
  ok('铺满的数就套在图上了',
     read('JSON.stringify(dataChart.scales.x)') === JSON.stringify({ min: 4.953, max: 6.888 }),
     read('JSON.stringify(dataChart.scales.x)'));
  ok('没钉住就不显示"已钉住"', read("$('data-xpin').hidden") === true);
  ok('范围框的单位跟着横轴（µm）',
     read("$('data-xmin-k').textContent") === 'X 最小 (µm)' &&
     read("$('data-xmax-k').textContent") === 'X 最大 (µm)',
     read("$('data-xmin-k').textContent") + ' / ' + read("$('data-xmax-k').textContent"));

  run("$('data-xmin').value = '5.5'; $('data-xmax').value = '6'; $('data-xmin').oninput()");
  await tick();
  ok('手填就钉住（写明了），图上按填的来',
     read("$('data-xpin').hidden") === false &&
     read('JSON.stringify(dataChart.scales.x)') === JSON.stringify({ min: 5.5, max: 6 }),
     read("$('data-xpin').hidden") + ' / ' + read('JSON.stringify(dataChart.scales.x)'));
  await run("$('btn-data-draw').onclick()"); await tick(); await tick();
  ok('钉住之后重新取数：不被自动铺满顶掉',
     read("$('data-xmin').value") === '5.5' && read("$('data-xmax').value") === '6' &&
     read('JSON.stringify(dataChart.scales.x)') === JSON.stringify({ min: 5.5, max: 6 }),
     read("$('data-xmin').value") + ' ~ ' + read("$('data-xmax').value"));

  // 钉住时换读法：同一个视窗换算过去，不是把 5.5–6 原地当成 fs（那是另一段）
  run("$('btn-data-axis').onclick()"); await tick();
  ok('钉住的范围跟着换读法换算（×6.6712819）',
     read("$('data-xmin').value") === '36.692' && read("$('data-xmax').value") === '40.028',
     read("$('data-xmin').value") + ' ~ ' + read("$('data-xmax').value"));
  ok('范围框的单位跟着换成 fs',
     read("$('data-xmin-k').textContent") === 'X 最小 (fs)' &&
     read("$('data-xmax-k').textContent") === 'X 最大 (fs)',
     read("$('data-xmin-k').textContent"));
  ok('换算后的范围套在新图上',
     read('JSON.stringify(dataChart.scales.x)') === JSON.stringify({ min: 36.692, max: 40.028 }),
     read('JSON.stringify(dataChart.scales.x)'));

  calls.length = 0;
  run("$('btn-data-fit').onclick()"); await tick();
  ok('点「自动范围」：松钉、按当前那一列重新铺满（也不发请求）',
     read("$('data-xpin').hidden") === true && calls.length === 0 &&
     read("$('data-xmin').value") === '8.000' && read("$('data-xmax').value") === '52.000',
     read("$('data-xmin').value") + ' ~ ' + read("$('data-xmax').value") + ' | ' + JSON.stringify(calls));

  run("$('data-xmin').value = '50'; $('data-xmax').value = '10'; $('data-xmin').oninput()");
  await tick();
  ok('填反了不套用（那样是空图），框原样留着等人改完',
     read("$('data-xmin').value") === '50' && read("$('data-xmax').value") === '10' &&
     read('JSON.stringify(dataChart.scales.x)') === JSON.stringify({ min: 8, max: 52 }),
     read('JSON.stringify(dataChart.scales.x)'));

  // 连线顺序：采图顺序（默认，如实）↔ 按横轴排序（一次置换，值跟着自己的横坐标走）。
  // XMT 上真实存在"读数往回走"：读数噪声 σ40 nm，步距比它小的时候相邻点就穿插。
  console.log('\n[T] 数据页连线顺序：采图顺序 ↔ 按横轴排序');
  const zigzag = {
    scan_id: 54, name: '', status: 'done', count: 5,
    x: 700, y: 540, width: 1440, height: 1080, bits: 16, full_scale: 1022, missing: 0,
    idx: [0, 1, 2, 3, 4],
    position_um: [5.0, 5.5, 5.3, 6.0, 5.8],       // 第 3 点往回走了 0.2 µm
    time_fs: [33.3564, 36.6921, 35.3578, 40.0277, 38.6934],
    time_fs_per_um: 6.6712819,
    value: [10, 20, 30, 40, 50],
  };
  run("dataOrder = 'seq'; dataAxis = 'pos'; dataLast = null; dataChart = null; dataChartAxis = null;");
  run("dataX.pinned = false; dataXs = []; $('data-order').value = 'seq'; $('data-xmin').value = ''; $('data-xmax').value = '';");
  run("$('data-scan').value = '52'; $('data-x').value = '700'; $('data-y').value = '540';");
  route = (m, u) => (u.indexOf('/pixel') > 0 ? { body: zigzag } : dataRoute(m, u));
  calls.length = 0;
  await run("$('btn-data-draw').onclick()"); await tick(); await tick();
  ok('默认按采图顺序连线：读数往回走的地方就画回去（不改数据）',
     read('JSON.stringify(dataChart.data[0])') === JSON.stringify([5, 5.5, 5.3, 6, 5.8]) &&
     read('JSON.stringify(dataChart.data[1])') === JSON.stringify([10, 20, 30, 40, 50]),
     read('JSON.stringify(dataChart.data[0])') + ' / ' + read('JSON.stringify(dataChart.data[1])'));
  ok('没排序就不显示"已排序"那条说明', read("$('data-order-note').hidden") === true);
  ok('选择框的默认项就是采图顺序（index.html 里 seq 排在 sort 前面）',
     html.indexOf('id="data-order"') >= 0 &&
     html.indexOf('value="seq"') > 0 && html.indexOf('value="seq"') < html.indexOf('value="sort"'),
     html.indexOf('value="seq"') + ' < ' + html.indexOf('value="sort"'));

  calls.length = 0;
  run("$('data-order').value = 'sort'; $('data-order').onchange()"); await tick();
  ok('按横轴排序：横坐标升序，**值跟着自己的横坐标走**（不是把值单独排一遍）',
     read('JSON.stringify(dataChart.data[0])') === JSON.stringify([5, 5.3, 5.5, 5.8, 6]) &&
     read('JSON.stringify(dataChart.data[1])') === JSON.stringify([10, 30, 20, 50, 40]),
     read('JSON.stringify(dataChart.data[0])') + ' / ' + read('JSON.stringify(dataChart.data[1])'));
  ok('排序是纯置换：点数 / 有值 / 没图的点 / 峰值 / 跨度都不变',
     read("$('data-n').textContent") === '5' && read("$('data-ok').textContent") === '5' &&
     read("$('data-missing').textContent") === '0' && read("$('data-peak').textContent") === '50' &&
     read("$('data-span').textContent") === '1.000',
     [read("$('data-ok').textContent"), read("$('data-peak').textContent"),
      read("$('data-span').textContent")].join(' / '));
  ok('排过序就写在脸上（换个顺序不是小事，得让人看得出来）',
     read("$('data-order-note').hidden") === false,
     read("$('data-order-note').textContent"));
  ok('换顺序不重新取数（置换是显示的活）', calls.length === 0, JSON.stringify(calls));

  run("$('btn-data-axis').onclick()"); await tick();
  ok('换成时间读法：照旧排序，横轴按时间升序（同一套置换）',
     read('JSON.stringify(dataChart.data[0])') === JSON.stringify([33.3564, 35.3578, 36.6921, 38.6934, 40.0277]) &&
     read('JSON.stringify(dataChart.data[1])') === JSON.stringify([10, 30, 20, 50, 40]),
     read('JSON.stringify(dataChart.data[0])'));

  run("$('btn-data-axis').onclick(); $('data-order').value = 'seq'; $('data-order').onchange()");
  await tick(); await tick();
  ok('切回采图顺序：值又跟着回到采图那一次的排列，说明也收起来',
     read('JSON.stringify(dataChart.data[0])') === JSON.stringify([5, 5.5, 5.3, 6, 5.8]) &&
     read('JSON.stringify(dataChart.data[1])') === JSON.stringify([10, 20, 30, 40, 50]) &&
     read("$('data-order-note').hidden") === true,
     read('JSON.stringify(dataChart.data[1])'));

  // 横轴缩放/平移：滚轮放大、按住拖动平移视窗。两条手势**只改范围框里的数**（框仍是唯一的准），
  // 并且**换读法重建图之后必须还挂着** —— 这是这套实现最容易失灵的地方（重建后旧元素没了）。
  console.log('\n[T] 数据页横轴：滚轮缩放 + 拖动平移（改的是范围框，不动数据）');
  ok('定点缩放：锚点不动、跨度按倍数缩',
     read('JSON.stringify(zoomRange(10, 20, 0.5, 15))') === JSON.stringify([12.5, 17.5]),
     read('JSON.stringify(zoomRange(10, 20, 0.5, 15))'));
  ok('平移：只挪窗口、跨度不变',
     read('JSON.stringify(shiftRange(10, 20, -2.5))') === JSON.stringify([7.5, 17.5]),
     read('JSON.stringify(shiftRange(10, 20, -2.5))'));

  run("dataOrder = 'seq'; dataAxis = 'pos'; dataLast = null; dataChart = null; dataChartAxis = null;");
  run("dataX.pinned = false; dataXs = []; $('data-xmin').value = ''; $('data-xmax').value = '';");
  run("$('data-scan').value = '52'; $('data-x').value = '700'; $('data-y').value = '540';");
  route = dataRoute;
  await run("$('btn-data-draw').onclick()"); await tick(); await tick();
  const xMin = () => read('dataChart.scales.x.min'), xMax = () => read('dataChart.scales.x.max');
  const win0 = { min: xMin(), max: xMax(), span: xMax() - xMin(), mid: (xMin() + xMax()) / 2 };
  ok('手势挂上了：滚轮 + 拖动各一个监听（挂在 uPlot 的 .u-over 上）',
     read("typeof dataChart.over._listeners.wheel[0]") === 'function' &&
     read("typeof dataChart.over._listeners.pointerdown[0]") === 'function');

  calls.length = 0;
  run("dataChart.over.dispatch('wheel', { clientX: 200, deltaY: -100, deltaMode: 0, preventDefault: function () {} })");
  await tick();
  ok('滚轮向上 = 放大（跨度变小，一格约 1.16 倍）',
     (xMax() - xMin()) < win0.span * 0.9 && (xMax() - xMin()) > win0.span * 0.8,
     win0.span + ' -> ' + (xMax() - xMin()));
  ok('锚点不动：鼠标底下那个值缩放前后一样（±0.002 = 框只有 3 位小数）',
     Math.abs((xMin() + xMax()) / 2 - win0.mid) < 0.002,
     win0.mid + ' -> ' + (xMin() + xMax()) / 2);
  ok('缩放写回范围框并钉住（框仍然是唯一的准）',
     read("$('data-xmin').value") === xMin().toFixed(3) &&
     read("$('data-xmax').value") === xMax().toFixed(3) &&
     read("$('data-xpin').hidden") === false,
     read("$('data-xmin').value") + ' ~ ' + read("$('data-xmax').value"));
  ok('缩放一个请求都不发', calls.length === 0, JSON.stringify(calls));

  run("dataChart.over.dispatch('wheel', { clientX: 200, deltaY: 100, deltaMode: 0, preventDefault: function () {} })");
  await tick();
  ok('滚轮向下 = 缩小（同一档来回，跨度回到起点附近）',
     Math.abs((xMax() - xMin()) - win0.span) < 0.01,
     win0.span + ' -> ' + (xMax() - xMin()));

  const panFrom = { min: xMin(), max: xMax(), span: xMax() - xMin() };
  run("dataChart.over.dispatch('pointerdown', { button: 0, clientX: 200, pointerId: 1, preventDefault: function () {} })");
  run("dataChart.over.dispatch('pointermove', { clientX: 240 })");     // 手往右拖 40 px
  await tick();
  ok('拖动平移：手往右拖 = 窗口往左（看更小的值），跨度不变',
     (xMin() - panFrom.min) < -0.05 &&
     Math.abs((xMax() - panFrom.max) - (xMin() - panFrom.min)) < 0.002 &&
     Math.abs((xMax() - xMin()) - panFrom.span) < 0.002,
     panFrom.min + '~' + panFrom.max + ' -> ' + xMin() + '~' + xMax());
  ok('拖动也写回范围框（跟滚轮同一条路）',
     read("$('data-xmin').value") === xMin().toFixed(3), read("$('data-xmin').value"));
  run("dataChart.over.dispatch('pointerup', { clientX: 240 })");
  await tick();
  ok('松手后不再跟着动（move/up 监听收掉了）',
     read("(dataChart.over._listeners.pointermove || []).length") === 0 &&
     read("(dataChart.over._listeners.pointerup || []).length") === 0);

  const zoomed = { min: xMin(), max: xMax() };
  run("$('btn-data-axis').onclick()"); await tick();
  ok('换读法重建图之后手势仍然挂着（这条盯的就是"切一次读法就失灵"）',
     read("typeof dataChart.over._listeners.wheel[0]") === 'function' &&
     read("typeof dataChart.over._listeners.pointerdown[0]") === 'function');
  ok('缩放后的窗口也跟着换读法换算（×6.6712819）',
     Math.abs(xMin() - zoomed.min * 6.6712819) < 0.002 &&
     Math.abs(xMax() - zoomed.max * 6.6712819) < 0.002,
     zoomed.min + '~' + zoomed.max + ' µm -> ' + xMin() + '~' + xMax() + ' fs');

  run("$('btn-data-fit').onclick()"); await tick();
  ok('「自动范围」一键复位：松钉 + 按当前那一列重新铺满',
     read("$('data-xpin').hidden") === true &&
     read("$('data-xmin').value") === '8.000' && read("$('data-xmax').value") === '52.000',
     read("$('data-xmin').value") + ' ~ ' + read("$('data-xmax').value"));

  run("$('data-xmin').value = ''; $('data-xmax').value = '';");
  ok('没有窗口（框空着/填反）时不缩放，也不拦页面滚动',
     read("(function () { var p = false; dataChart.over.dispatch('wheel', " +
          "{ clientX: 200, deltaY: -100, deltaMode: 0, preventDefault: function () { p = true; } }); return p; })()") === false);

  // 四张图共用一套壳子（makeChart）：坐标轴、图例、尺寸观察者只有一份实现。
  // 拖拽缩放按"有没有范围框"分：有框的一律关掉（框才是唯一的准），点位图没有框，放行。
  console.log('\n[T] 建图：四张图同一套壳子');
  ok('壳子一致：x 轴非时间轴 + 图例开着（四张图都一样）',
     read('chart.opts.scales.x.time') === false && read('chart.opts.legend.show') === true &&
     read('traceChart.opts.scales.x.time') === false && read('traceChart.opts.legend.show') === true &&
     read('profCharts.preview.h.opts.scales.x.time') === false &&
     read('dataChart.opts.scales.x.time') === false && read('dataChart.opts.legend.show') === true);
  ok('有范围框的图关掉拖拽缩放，点位图留着 uPlot 自带的',
     read('traceChart.opts.cursor.drag.x') === false &&
     read('profCharts.preview.h.opts.cursor.drag.x') === false &&
     read('dataChart.opts.cursor.drag.x') === false &&
     read('typeof chart.opts.cursor') === 'undefined',
     read('chart.opts.cursor'));
  ok('两列 series：第一列是横轴名字，第二列是那条线',
     read('traceChart.opts.series[0].label') === '时间 (s)' &&
     read('traceChart.opts.series[1].label') === '位置 (µm)' &&
     read('dataChart.opts.series[1].label') === '像素值 (ADU)' &&
     read('profCharts.preview.v.opts.series[1].label') === 'ADU');
  const obs0 = read('__stats().observers');
  run("$('btn-data-axis').onclick()"); await tick();   // 换读法 = destroy 重建这张图
  ok('重建图不会重复挂尺寸观察者（挂两个，旧的那个会一直对着不存在的图重排）',
     read('__stats().observers') === obs0, read('__stats().observers') + ' vs ' + obs0);


  // 数据页「扫描组 / 裁剪」：一组 = 同名的扫描；裁剪改的是文件，**坐标口径不变**。
  // 前端这一层要盯的是：预检如实列出来、四个数原样发给后端、点完了给进度和结果、
  // 正在看的那条裁完要重画。真正的文件操作在后端（backend/tests/test_crop.py 盯着）。
  console.log('\n[T] 历史扫描：裁剪');
  const cropGroupsBody = {
    w: 400, h: 300,
    job: { running: false, group: '', done: 0, total: 0, error: null, result: null },
    // 每条扫描裁没裁过（历史表那一列照着写）
    crops: [{ id: 41, rect: [520, 390, 400, 300], frames: 3, state: 'done' }],
    groups: [],
  };
  const histBody = [
    { id: 41, name: 'xmt 联调', start_um: 0, stop_um: 5, count: 3, done: 3,
      status: 'done', created_at: 1700000000 },
    { id: 36, name: '冒烟', start_um: 0, stop_um: 5, count: 8, done: 5,
      status: 'done', created_at: 1700000100 },
    { id: 9, name: '', start_um: 0, stop_um: 5, count: 2, done: 0,
      status: 'failed', created_at: 1700000200 },
  ];
  const cropSuggestBody = {
    name: 'xmt 联调', images: 9, bytes: 9437184, frame: [1440, 1080],
    sample: 'images/scan0041_00000.png',
    scans: [
      { id: 41, points: 3, images: 3, size: [1440, 1080], ok: true, why: '' },
      { id: 42, points: 3, images: 3, size: [1440, 1080], ok: true, why: '' },
      { id: 43, points: 3, images: 3, size: [64, 64], ok: false,
        why: '画面尺寸不一致（64×64，这一组按 1440×1080 裁）' },
    ],
    rect: { x0: 520, y0: 390, w: 400, h: 300, frame_w: 1440, frame_h: 1080,
            source: 'centroid', sampled: 9, cx_range: [642.7, 677.5], cy_range: [567.0, 582.0],
            margin: 150 },
    note: '这一组的画面只有 1440×1080，整幅都框进去了 —— 裁了不会更快（多半是假相机采的占位小图）',
  };
  const cropRoute = (m, url) => {
    if (url.indexOf('/api/crops/suggest') === 0) return { body: cropSuggestBody };
    if (url.indexOf('/api/crops/run') === 0) return { body: { ok: true, scans: [41, 42], images: 6 } };
    if (url.indexOf('/api/scans/52/pixel') === 0) return { body: pixelBody(52) };
    if (url.indexOf('/api/crops') === 0) return { body: cropGroupsBody };
    if (url.indexOf('/api/scans') === 0) return { body: histBody };
    return { body: {} };
  };
  route = cropRoute;
  await run('loadHistory()'); await tick(); await tick();
  const histHtml = htmlOf('history');
  ok('历史表里给出「裁剪」入口（一组 = 同名的扫描，名字带在按钮上）',
     rowsOf('history') === 3 && histHtml.indexOf('data-crop="36"') > 0 &&
     histHtml.indexOf('data-name="冒烟"') > 0, histHtml.slice(-160));
  ok('裁过的也留着「裁剪」按钮（还能往里再裁一刀）',
     histHtml.indexOf('data-crop="41"') > 0, histHtml.slice(-200));
  ok('裁到多小**单独一列**（裁过的写尺寸、没裁过的写 —）',
     histHtml.indexOf('>400×300<') > 0 && histHtml.indexOf('>—<') > 0,
     histHtml.indexOf('>400×300<'));
  ok('一个图都没有的扫描不给裁剪按钮',
     (histHtml.match(/data-crop=/g) || []).length === 2, histHtml.match(/data-crop="\d+"/g));

  click({ crop: '36', name: '冒烟' }); await tick(); await tick();
  ok('后端说"整幅都框进去了、裁了不会更快"，面板就照写（有没有收益由后端判，前端只显示）',
     read("$('crop-info').textContent").indexOf('裁了不会更快') > 0,
     read("$('crop-info').textContent"));
  ok('点一组 → 拉预检，四个数按建议框填好',
     read("$('crop-x0').value") === 520 && read("$('crop-y0').value") === 390 &&
     read("$('crop-w').value") === 400 && read("$('crop-h').value") === 300 &&
     read("$('croppanel').hidden") === false,
     read("$('crop-x0').value + ',' + $('crop-y0').value + ',' + $('crop-w').value + " +
          "'" + "' + $('crop-h').value"));
  ok('代表帧走 8 位映射的缩略图（16 位原值给浏览器看是整片黑的，等于没预览）',
     read("$('crop-img').src").indexOf('/api/grabs/thumb?path=images%2Fscan0041_00000.png') === 0,
     read("$('crop-img').src"));
  ok('红框按原始坐标换算成百分比画在代表帧上（520/1440、400/1440）',
     read("$('crop-rect').style.left") === (520 / 1440 * 100) + '%' &&
     read("$('crop-rect').style.width") === (400 / 1440 * 100) + '%' &&
     read("$('crop-rect').hidden") === false,
     read("$('crop-rect').style.left") + ' / ' + read("$('crop-rect').style.width"));
  ok('预检逐条列出来：不能裁的写清为什么',
     read("$('crop-scans').children.length") === 3 &&
     read("$('crop-scans').children[2].children[1].textContent").indexOf('画面尺寸不一致') > 0,
     read("$('crop-scans').children[2].children[1].textContent"));
  ok('说明里写清质心范围与离边余量（框选得对不对，看这个）',
     read("$('crop-info').textContent").indexOf('离框边最近还有 150 px') > 0,
     read("$('crop-info').textContent"));

  // 当前选中的那条（#52）在假数据里叫什么名字 —— 裁完要不要重画就看它
  const curName = read("(dataScanPick() || {}).name") || '';
  // 框住没有：质心范围是后端给的，改一个数就立刻比一次（这一条是给"框选小了把亮心切掉"兜底的）
  run("$('crop-x0').value = 100; $('crop-w').value = 400; cropDrawRect();");
  ok('框比质心范围小 → 当场红字提醒，并写出两边各是多少',
     read("$('crop-warn').hidden") === false &&
     read("$('crop-warn').textContent").indexOf('x 642.7~677.5（框里是 100~499）') > 0 &&
     read("$('crop-warn').textContent").indexOf('找不回来') > 0,
     read("$('crop-warn').textContent"));
  run("$('crop-x0').value = 520; cropDrawRect();");
  ok('框住了就不吓人（提醒收起来）', read("$('crop-warn').hidden") === true);

  calls.length = 0;
  run("$('crop-x0').value = 1200; $('crop-w').value = 400;");   // 1200+400 > 1440：越界
  await run('runCrop()'); await tick();
  ok('矩形越界：一个请求都不发（前端先挡一道，后端还会再挡）',
     calls.length === 0, JSON.stringify(calls));

  // 跑着的时候：按钮锁住、面板关掉、进度行跟着走（进度由后端报，前端只照着写）
  cropGroupsBody.job = { running: true, group: curName, done: 3, total: 6, error: null, result: null };
  run("$('crop-x0').value = 520; $('crop-w').value = 400; dataLast = null;");   // 先不牵扯重画
  await run('runCrop()'); await tick(); await tick();
  ok('点「裁剪」把四个数原样发给后端（**原始坐标**，不做任何换算）',
     calls[0] === 'POST /api/crops/run?name=xmt%20%E8%81%94%E8%B0%83&x0=520&y0=390&w=400&h=300',
     JSON.stringify(calls));
  ok('跑起来就锁住按钮、关掉面板、进度行跟着后端的数走',
     read("$('btn-crop-run').disabled") === true && read("$('croppanel').hidden") === true &&
     read("$('crop-progress').hidden") === false &&
     read("$('crop-progress').textContent") === '正在裁剪 3 / 6 张 …',
     read("$('crop-progress').textContent"));

  cropGroupsBody.job = { running: false, group: curName, done: 6, total: 6, error: null,
                         result: { scans: 2, frames: 6, saved_bytes: 8388608, peak: 900,
                                   edge_peak: 120, rect: [520, 390, 400, 300] } };
  calls.length = 0;
  await run('cropPoll()'); await tick(); await tick();
  ok('裁完报到结果：几条几张省了多少，并**把裁剪状态和历史表重新拉一遍**（那一列要跟着变）',
     read("$('crop-progress').textContent").indexOf('裁完了：2 条扫描、6 张，省 8.0 MB') === 0 &&
     calls.indexOf('GET /api/crops') >= 0 && calls.indexOf('GET /api/scans') >= 0 &&
     read("$('btn-crop-run').disabled") === false,
     read("$('crop-progress').textContent"));
  ok('裁完不自动重画别的扫描（正在看的那条不在这一组里就不动它）',
     calls.filter(function (c) { return c.indexOf('GET /api/scans/52/pixel') === 0; }).length === 0,
     JSON.stringify(calls));
  ok('边界报警：边框上有接近峰值的像素就说出来（切到光斑就靠这条发现）',
     read("$('crop-progress').textContent").indexOf('边框最大 120') > 0,
     read("$('crop-progress').textContent"));

  cropGroupsBody.job = { running: false, group: 'xmt 联调', done: 3, total: 6,
                         error: '模拟：第三张读不动', result: null };
  await run('cropPoll()'); await tick();
  ok('失败就说"整组已回滚"（后端确实一张都没改）',
     read("$('crop-progress').textContent").indexOf('整组已回滚') > 0 &&
     read("$('btn-crop-run').disabled") === false,
     read("$('crop-progress').textContent"));

  // 裁过的扫描：标题上写明"已裁剪"，而且要写清坐标口径没变
  run("dataAxis = 'pos'; dataOrder = 'seq'; dataLast = null; dataChart = null; dataChartAxis = null;");
  run("dataX.pinned = false; dataXs = []; $('data-xmin').value = ''; $('data-xmax').value = '';");
  route = cropRoute;
  run("$('data-scan').value = '52'; $('data-x').value = '720'; $('data-y').value = '540';");
  await run("$('btn-data-draw').onclick()"); await tick(); await tick();
  ok('没裁过的扫描：不显示已裁剪标注',
     read("$('data-crop').hidden") === true, read("$('data-crop').textContent"));
  run("renderPixelCurve(Object.assign({}, dataLast, { crop: [520, 390, 400, 300] }))"); await tick();
  ok('裁过的扫描：标题写明矩形，并强调**坐标仍是原始坐标**',
     read("$('data-crop').hidden") === false &&
     read("$('data-crop').textContent").indexOf('已裁剪 400×300 @ (520,390)') === 0 &&
     read("$('data-crop').textContent").indexOf('坐标仍是原始坐标') > 0,
     read("$('data-crop').textContent"));

  // 正在看的那条**就在被裁的那一组里** → 裁完自动重画一次（读的是裁剪后的图，值必须一模一样）
  cropGroupsBody.job = { running: false, group: curName, done: 6, total: 6, error: null,
                         result: { scans: 2, frames: 6, saved_bytes: 8388608, peak: 900,
                                   edge_peak: 120, rect: [520, 390, 400, 300] } };
  calls.length = 0;
  await run('cropPoll()'); await tick(); await tick();
  ok('正在看的那条就在这一组里 → 自动重画一次（读的是裁剪后的图，值必须一模一样）',
     calls.filter(function (c) { return c.indexOf('GET /api/scans/52/pixel') === 0; }).length === 1,
     JSON.stringify(calls));

  // 再裁（越裁越小）：图上那张就是"现在这块地"，四个数仍是**原始坐标**
  const recropBody = {
    name: '冒烟', images: 6, bytes: 1048576, frame: [500, 700],
    base: [200, 300, 500, 700], sample: 'images/scan0036_00000.png',
    scans: [{ id: 36, points: 8, images: 6, size: [500, 700], rect: [200, 300, 500, 700],
              ok: true, why: '' }],
    rect: { x0: 300, y0: 400, w: 200, h: 200, frame_w: 500, frame_h: 700,
            base: [200, 300, 500, 700], cropped: true, source: 'centroid', sampled: 6,
            cx_range: [340.0, 360.0], cy_range: [430.0, 450.0], margin: 40 },
  };
  route = (m, url) => (url.indexOf('/api/crops/suggest') === 0
    ? { body: recropBody } : cropRoute(m, url));
  await run("openCropPanel('冒烟')"); await tick(); await tick();
  ok('再裁：红框按 (原始坐标 − base 原点) ÷ 现在画面 换算（四个数本身没换算过）',
     read("$('crop-rect').style.left") === ((300 - 200) / 500 * 100) + '%' &&
     read("$('crop-rect').style.top") === ((400 - 300) / 700 * 100) + '%' &&
     read("$('crop-rect').style.width") === (200 / 500 * 100) + '%',
     read("$('crop-rect').style.cssText"));
  ok('再裁：预检里写明现在裁到多大、只能往里挑',
     read("$('crop-info').textContent").indexOf(
       '现在已经裁到 500×700（原始坐标 x 200~699、y 300~999）') > 0 &&
     read("$('crop-info').textContent").indexOf('现在画面 500×700') > 0,
     read("$('crop-info').textContent"));

  calls.length = 0;
  run("$('crop-x0').value = 150; $('crop-y0').value = 350;" +
      "$('crop-w').value = 200; $('crop-h').value = 200;");
  await run('runCrop()'); await tick();
  ok('再裁：往框外挪一个像素就不发请求（裁掉的找不回来）',
     calls.length === 0, JSON.stringify(calls));
  run("$('crop-x0').value = 200; $('crop-y0').value = 300;" +
      "$('crop-w').value = 500; $('crop-h').value = 700;");
  await run('runCrop()'); await tick();
  ok('再裁：跟现在一样大也不发请求（得严格更小才有意义）',
     calls.length === 0, JSON.stringify(calls));
  run("$('crop-x0').value = 250; $('crop-y0').value = 350;" +
      "$('crop-w').value = 300; $('crop-h').value = 400;");
  await run('runCrop()'); await tick();
  ok('再裁：往里挑一块 → 照原样发给后端（还是原始坐标）',
     calls[0] === 'POST /api/crops/run?name=%E5%86%92%E7%83%9F&x0=250&y0=350&w=300&h=400',
     JSON.stringify(calls));

  // 提醒要分清"这一刀会切掉"和"上一刀早就切在框外"（后者救不回来，不该拿来吓人）
  run("$('crop-x0').value = 200; $('crop-y0').value = 300;" +
      "$('crop-w').value = 160; $('crop-h').value = 200; cropDrawRect();");
  ok('框会把质心切掉 → 红字说清"这一刀"会切走什么',
     read("$('crop-warn').hidden") === false && read("$('crop-warn').className") === 'hint warn' &&
     read("$('crop-warn').textContent").indexOf('这一刀会把质心范围切在外面') > 0 &&
     read("$('crop-warn').textContent").indexOf('x 340~360（框里是 200~359）') > 0,
     read("$('crop-warn').textContent"));
  recropBody.rect.cx_range = [100.0, 120.0];        // 比"可裁的那块地"还靠外：上一刀的旧账
  run("$('crop-x0').value = 200; $('crop-y0').value = 300;" +
      "$('crop-w').value = 400; $('crop-h').value = 400; cropDrawRect();");
  ok('质心本来就在上一刀外面 → 只说"救不回来、这一刀动不到它"，不冒充成这一刀切掉的',
     read("$('crop-warn').hidden") === false && read("$('crop-warn').className") === 'hint' &&
     read("$('crop-warn').textContent").indexOf('上一刀**就已经在框外') < 0 &&
     read("$('crop-warn').textContent").indexOf('这是') > 0,
     read("$('crop-warn').textContent"));

  // ==================== 数据页：频谱（功率谱） ====================
  // 这块的规矩：**真实点间距**（非均匀最小二乘，不插值）、**整条扫描的全部点**、**缺一个点整条不算**、
  // 按**采图顺序**、纵轴是功率（可切 dB）。全部离线可测：造一条已知频率的正弦，看峰落在哪一格。
  console.log('\n[U] 数据页频谱：功率谱（真实点间距 / 整条 / 缺一个点就不算）');

  // 造一条序列：n 个点、采样步距 100 fs（jitter=1 时按 ±40% 步距抖动 —— 就是"真实点间距"那件事），
  // 值 = 直流 ofs + 幅度 amp 的正弦，正好落在第 k0 格上。数组顺序 = 采图顺序。
  const seriesOf = (n, k0, amp, ofs, jitter) => {
    let seed = 12345;
    const rnd = () => { seed = (seed * 1103515245 + 12345) & 0x7fffffff; return seed / 0x7fffffff - 0.5; };
    const dt = 100, tf = [], value = [];
    for (let i = 0; i < n; i++) tf.push(i * dt + (jitter ? rnd() * 0.8 * dt : 0));
    for (let i = 0; i < n; i++) value.push(ofs + amp * Math.sin(2 * Math.PI * k0 * i / (n - 1)));
    return { scan_id: 53, name: '', count: n, x: 700, y: 540, width: 1440, height: 1080,
             bits: 16, full_scale: 1022, missing: 0, time_fs_per_um: 6.6712819,
             position_um: tf.map((t) => t / 6.6712819),
             idx: Array.from({ length: n }, (_, i) => i), time_fs: tf, value: value };
  };
  const specOf = (o) => JSON.parse(read('JSON.stringify(pixelSpectrum(' + JSON.stringify(o) + '))'));

  const dfTHz = 1000 / (255 * 100);        // 256 点、100 fs 步距 → 频率步长 1/T（THz）
  const s1 = specOf(seriesOf(256, 20, 100, 500, 0));
  ok('均匀采样：峰正好落在第 20 格、峰高 = A²/2（那个 500 的直流被去均值剃掉了）',
     Math.abs(s1.peakF - 20 * dfTHz) < 1e-12 && Math.abs(s1.peakP - 5000) < 1e-3,
     s1.peakF + ' THz / ' + s1.peakP);
  ok('频率轴：步长 1/T、上限卡在名义 Nyquist（第 floor((n-1)/2) 格）',
     Math.abs(s1.df_thz - dfTHz) < 1e-12 && Math.abs(s1.fmax_thz - 127 * dfTHz) < 1e-12,
     s1.df_thz + ' / ' + s1.fmax_thz);
  ok('真实跨度 = 首末两点之差（不是 点数 × 名义步距）',
     Math.abs(s1.span_fs - 255 * 100) < 1e-9, s1.span_fs);

  const s2 = specOf(seriesOf(256, 20, 100, 500, 1));
  // 频率网格跟着**真实跨度**走（span 抖动过就不等于 255×100），所以要比的是"落在第几格"，
  // 不是"频率等于多少" —— 后者只有在名义等间隔下才成立。
  const binNear = (fThz, spanFs) => Math.round(fThz * spanFs / 1000);
  ok('真实点间距（±40% 步距抖动）：峰仍落在第 20 格、峰高基本不变 —— 最小二乘在真频点上无偏',
     binNear(s2.peakF, s2.span_fs) === 20 && Math.abs(s2.peakP - 5000) / 5000 < 0.02,
     s2.peakF + ' THz / ' + s2.peakP + '（跨度 ' + s2.span_fs + ' fs）');
  const rev = seriesOf(256, 20, 100, 500, 1);
  rev.time_fs.reverse(); rev.value.reverse();
  const relDiff = (a, b) => {
    let m = 0;
    for (let i = 0; i < a.length; i++) {
      const d = Math.abs(a[i] - b[i]) / (Math.abs(b[i]) || 1);
      if (d > m) m = d;
    }
    return m;
  };
  ok('采图顺序倒过来算，谱一模一样（每个点带自己的时间，不靠"相邻点间距"那类假设）',
     relDiff(specOf(rev).p, s2.p) < 1e-9, relDiff(specOf(rev).p, s2.p));

  const gap = seriesOf(64, 6, 100, 500, 0); gap.value[2] = null;
  ok('没采到图的点：整条不算，说清是第几个点（不补零、不跳过）',
     /第 3 个点（idx 2）没采到图/.test(specOf(gap).error) &&
     specOf(gap).error.indexOf('整条') > 0, specOf(gap).error);
  const nopos = seriesOf(64, 6, 100, 500, 0); nopos.time_fs[7] = null;
  ok('没记下读出位置的点：同样整条不算',
     /第 8 个点（idx 7）没记下读出位置/.test(specOf(nopos).error), specOf(nopos).error);
  ok('点太少（< 16）不给算', /只有 8 个点/.test(specOf(seriesOf(8, 2, 100, 0, 0)).error));
  ok('一条直线（每个点的值都一样）→ 说出来，不画一条 0 的谱',
     /一点起伏都没有/.test(specOf(seriesOf(64, 6, 0, 500, 0)).error));
  const same = seriesOf(64, 6, 100, 500, 0); same.time_fs = same.time_fs.map(() => 3);
  ok('所有点挤在同一处（真实跨度 0）→ 说出来', /跨度 0/.test(specOf(same).error));

  // 建图这一层：一份取数结果 → 曲线 + 谱；切 dB / 换读法 / 换连线顺序都不重新取数
  console.log('\n[U] 数据页频谱：与界面的接线（不重新取数、切 dB 不重算）');
  const specBody = seriesOf(64, 6, 100, 500, 0);
  route = (m, url) => (url.indexOf('/pixel') > 0 ? { body: specBody } : dataRoute(m, url));
  run("dataAxis='pos'; dataOrder='seq'; dataLast=null; dataChart=null; dataChartAxis=null;" +
      "specChart=null; specChartDb=null; specLast=null; specSrc=null; specDb=false;" +
      "specX.pinned=false; specY.pinned=false;" +
      "$('spec-xmin').value=''; $('spec-xmax').value=''; $('spec-ymin').value=''; $('spec-ymax').value='';");
  run("$('data-scan').value='52'; $('data-x').value='700'; $('data-y').value='540';");
  calls.length = 0;
  await run("$('btn-data-draw').onclick()"); await tick(); await tick();
  ok('画出曲线就把谱一起算出来（同一份数据，不额外取数）',
     read("$('spec-n').textContent") === '64' && read('specChart.data[0].length') === 31 &&
     calls.filter((c) => c.indexOf('/pixel') > 0).length === 1,
     read("$('spec-n').textContent") + ' / ' + JSON.stringify(calls));
  ok('纵轴默认功率、图例写明单位', read('specChart.opts.series[1].label') === '功率 (ADU²)',
     read('specChart.opts.series[1].label'));
  ok('统计条：真实跨度 / 最高频率 / 最强分量',
     read("$('spec-span').textContent") === '6300.0' &&
     read("$('spec-fmax').textContent") === (31 * 1000 / 6300).toFixed(3) &&
     read("$('spec-peak').textContent") === (6 * 1000 / 6300).toFixed(3),
     [read("$('spec-span').textContent"), read("$('spec-fmax').textContent"),
      read("$('spec-peak').textContent")].join(' / '));
  ok('横轴范围按这条谱铺满（两端各 5%，频率不为负）',
     read("$('spec-xmin').value") === '0.000' && read("$('spec-xmax').value") === '5.159' &&
     read('specChart.scales.x.max') === 5.159,
     read("$('spec-xmin').value") + ' ~ ' + read("$('spec-xmax').value"));
  ok('纵轴：功率没有负的（下限夹到 0）、上限比峰高留 5%',
     read("$('spec-ymin').value") === '0.0000' && parseFloat(read("$('spec-ymax').value")) > 5000,
     read("$('spec-ymin').value") + ' ~ ' + read("$('spec-ymax').value"));

  run('specLast.__tag = 1;');
  const chart0 = read('specChart');
  calls.length = 0;
  run("$('btn-spec-db').onclick()"); await tick();
  const dbVals = JSON.parse(read('JSON.stringify(specChart.data[1])'));
  ok('切 dB：纵轴换成相对最强那根的 dB（峰 = 0 dB），图例跟着换成 dB（重建图）',
     Math.abs(Math.max.apply(null, dbVals)) < 1e-9 &&
     read('specChart.opts.series[1].label').indexOf('dB') > 0 && read('specChart') !== chart0,
     read('specChart.opts.series[1].label') + ' / max ' + Math.max.apply(null, dbVals));
  ok('切 dB 不重算谱、也不重新取数（同一串数换刻度）',
     read('specLast.__tag') === 1 && calls.length === 0, JSON.stringify(calls));
  ok('切 dB 之后手势仍挂在图上（重建会把旧元素整个丢掉）',
     read("typeof specChart.over._listeners.wheel[0]") === 'function' &&
     read("typeof specChart.over._listeners.pointerdown[0]") === 'function');
  ok('dB 的纵轴范围按数据铺满，天花板就是 0 dB（最强那根）',
     parseFloat(read("$('spec-ymax').value")) === 0 &&
     parseFloat(read("$('spec-ymin').value")) < 0,
     read("$('spec-ymin').value") + ' ~ ' + read("$('spec-ymax').value"));

  const dbBefore = read('JSON.stringify(specChart.data[1])');
  calls.length = 0;
  run("$('data-order').value = 'sort'; $('data-order').onchange()"); await tick();
  ok('换连线顺序：曲线重画，谱一点没变、不重算也不取数（谱按采图顺序）',
     read('specLast.__tag') === 1 && read('JSON.stringify(specChart.data[1])') === dbBefore &&
     calls.length === 0, JSON.stringify(calls));
  run("$('btn-data-axis').onclick()"); await tick();
  ok('换横轴读法（位置 ↔ 时间）：谱同样不动（那是曲线的刻度，不是采样的刻度）',
     read('specLast.__tag') === 1 && read('JSON.stringify(specChart.data[1])') === dbBefore);
  run("$('data-xmin').value = '5'; $('data-xmax').value = '6'; $('data-xmin').oninput()"); await tick();
  ok('曲线上的可视范围框只管看：不参与变换（谱还是整条、点数不变）',
     read('specLast.__tag') === 1 && read('specChart.data[0].length') === 31);

  run("$('spec-xmin').value = '1'; $('spec-xmax').value = '2'; $('spec-xmin').oninput()"); await tick();
  ok('手填频率范围 = 钉住，并套到图上',
     read("$('spec-xpin').hidden") === false && read('specChart.scales.x.min') === 1 &&
     read('specChart.scales.x.max') === 2, read('JSON.stringify(specChart.scales.x)'));
  run("$('btn-spec-fit').onclick()"); await tick();
  ok('频率轴「自动范围」松钉、按这条谱重新铺满',
     read("$('spec-xpin').hidden") === true && read("$('spec-xmin').value") === '0.000' &&
     read("$('spec-xmax').value") === '5.159');
  run("$('spec-ymin').value = '-20'; $('spec-ymax').value = '-1'; $('spec-ymin').oninput()"); await tick();
  ok('两个轴各钉各的：填了纵轴范围不该把频率轴一起钉住',
     read("$('spec-ypin').hidden") === false && read("$('spec-xpin').hidden") === true &&
     read('specChart.scales.y.min') === -20);
  run("$('btn-spec-yfit').onclick()"); await tick();
  ok('纵轴「自动范围」复位（回到 0 dB 天花板）',
     read("$('spec-ypin').hidden") === true && parseFloat(read("$('spec-ymax').value")) === 0);

  // 缺一个点：谱整条不算，但**曲线照旧能看**（那是两件事）
  const gapBody = seriesOf(64, 6, 100, 500, 0);
  gapBody.value[9] = null; gapBody.missing = 1;
  route = (m, url) => (url.indexOf('/pixel') > 0 ? { body: gapBody } : dataRoute(m, url));
  calls.length = 0;
  await run("$('btn-data-draw').onclick()"); await tick(); await tick();
  ok('有一个点没图：整条不算、红字说清第几个点，旧谱清空（不留上一条的谱冒充这一条）',
     read("$('spec-empty').hidden") === false &&
     read("$('spec-empty').className").indexOf('warn') >= 0 &&
     /第 10 个点（idx 9）没采到图/.test(read("$('spec-empty').textContent")) &&
     read('specChart.data[0].length') === 0,
     read("$('spec-empty').textContent"));
  ok('不合格时统计条不冒充数字（一律写 —）',
     read("$('spec-n').textContent") === '—' && read("$('spec-peak').textContent") === '—' &&
     read("$('spec-title').textContent") === '—');
  ok('曲线照旧能看：没图的点只是断开（谱不算，不牵连曲线）',
     read("$('data-ok').textContent") === '63' && read("$('data-missing').textContent") === '1');
  run("$('btn-spec-db').onclick()"); await tick();
  ok('不合格时切 dB 也不炸（没有谱可画，图仍是空的）',
     read('specChart.data[0].length') === 0 && read("$('spec-empty').hidden") === false);

  // ==================== 数据页：位置口径（折算 µm ↔ 设备读回原值） ====================
  // 口径是**设备属性**（XMT 读回 4/3 µm、PI 的位置就是 µm），后端四列一起下发；前端只挑一列画，
  // 一个系数都不写。老数据没记是哪台设备采的 → 按钮禁用并说清为什么，**绝不猜一个系数**。
  console.log('\n[V] 数据页位置口径：折算 µm ↔ 读回原值（四列一起下发、切换不取数）');

  const RAW_FACTOR = 0.75;
  // 与后端 png_pixel_series 同一套口径：系数是 1 或没记下时**不给原值列**
  const withRaw = (body, factor) => Object.assign({}, body, {
    readback_to_um: factor,
    position_raw: (factor === null || factor === 1)
      ? null : body.position_um.map((p) => (p === null ? null : p / factor)),
    time_raw_fs: (factor === null || factor === 1)
      ? null : body.time_fs.map((t) => (t === null ? null : t / factor)),
  });
  const srcList = { sources: [
    { key: 'pi', label: 'PI E-709（位置本来就是 µm）', factor: 1.0 },
    { key: 'xmt', label: 'XMT E53.D1S-H（读回 = 4/3 µm）', factor: RAW_FACTOR },
  ] };
  const rawScans = () => [
    { id: 53, name: 'xmt 联调', count: 64, done: 64, start_um: 0, stop_um: 7, status: 'done',
      created_at: 1700000000, readback_to_um: RAW_FACTOR },
    { id: 52, name: '', count: 64, done: 64, start_um: 0, stop_um: 7, status: 'done',
      created_at: 1700000100, readback_to_um: null },
    { id: 51, name: 'pi', count: 64, done: 64, start_um: 0, stop_um: 7, status: 'done',
      created_at: 1700000200, readback_to_um: 1.0 },
  ];
  let rows = rawScans();
  const rawBody = withRaw(seriesOf(64, 6, 100, 500, 0), RAW_FACTOR);
  rawBody.scan_id = 53;
  const oldBody = withRaw(seriesOf(64, 6, 100, 500, 0), null);   // 老数据：没记是哪台设备采的
  oldBody.scan_id = 52;
  let body52 = oldBody;                       // 指认之后这条也会多出原值列（后端真的会给）
  const piBody = withRaw(seriesOf(64, 6, 100, 500, 0), 1.0);     // PI：位置本来就是 µm
  piBody.scan_id = 51;
  posts.length = 0;
  route = (m, url) => {
    if (url.indexOf('/api/readback-sources') === 0) return { body: srcList };
    if (url.indexOf('/readback') > 0) {
      rows[1].readback_to_um = RAW_FACTOR;      // 指认之后库里那条就变了
      body52 = withRaw(seriesOf(64, 6, 100, 500, 0), RAW_FACTOR);
      body52.scan_id = 52;
      return { body: { scan_id: 52, readback_to_um: RAW_FACTOR } };
    }
    if (url.indexOf('/pixel') > 0) {
      return { body: url.indexOf('/53/') > 0 ? rawBody
        : (url.indexOf('/51/') > 0 ? piBody : body52) };
    }
    if (url.indexOf('/api/scans') === 0) return { body: rows };
    return { body: {} };
  };
  const resetData = () => run("dataAxis='pos'; dataScale='um'; dataOrder='seq'; dataLast=null;" +
    "dataChart=null; dataChartKey=null; specChart=null; specChartDb=null; specLast=null;" +
    "specSrc=null; specScale=null; specDb=false; dataSources=[];" +
    "dataX.pinned=false; dataXs=[]; specX.pinned=false; specY.pinned=false;" +
    "$('data-xmin').value=''; $('data-xmax').value=''; $('spec-xmin').value=''; $('spec-xmax').value='';");

  resetData();
  await run('loadDataSources()'); await tick();
  ok('口径下拉框的选项来自后端（未记录 + 后端给的两台设备，前端一个系数都不写）',
     read("$('data-source').children.length") === 3 &&
     read("$('data-source').children[1].value") === 'pi' &&
     read("$('data-source').children[2].value") === 'xmt',
     read("$('data-source').children.map(function (o) { return o.value; }).join(',')"));
  await run('loadDataScans()'); await tick();
  // 选中的那条（列表里第一条 #53，库里记着 0.75）
  run("$('data-scan').value = '53'; $('data-scan').onchange()"); await tick();
  ok('下拉框跟着选中的扫描走：库里记的是 0.75 → 选中 XMT 那一项',
     read("$('data-source').value") === 'xmt' &&
     read("$('data-source-note').textContent").indexOf('0.75') > 0,
     read("$('data-source').value") + ' / ' + read("$('data-source-note').textContent"));

  calls.length = 0;
  await run("$('btn-data-draw').onclick()"); await tick(); await tick();
  ok('默认口径还是折算 µm（读数就是读数），按钮可用',
     read('JSON.stringify(dataChart.data[0])') === JSON.stringify(rawBody.position_um) &&
     read("$('btn-data-scale').disabled") === false, read("$('btn-data-scale').disabled"));

  const peakUm = read('specLast.peakF');       // 折算口径下的峰位（画完就是它）
  // 钉住一个窗口，再看换口径时框里的数怎么走（框里填的永远是**当前口径**的数）
  run("$('data-xmin').value = '20'; $('data-xmax').value = '80'; $('data-xmin').oninput()"); await tick();
  calls.length = 0;
  run("$('btn-data-scale').onclick()"); await tick();
  ok('切原值：横轴换成设备读回原值（µm ÷ 0.75），**不重新取数**',
     read('JSON.stringify(dataChart.data[0])') === JSON.stringify(rawBody.position_raw) &&
     calls.length === 0, read('JSON.stringify(dataChart.data[0].slice(0, 3))'));
  ok('图上的横轴单位跟着换（重建图，不是改个标签）',
     read('dataChart.opts.series[0].label') === '读回原值（设备单位）' &&
     read("$('data-scale-note').hidden") === false,
     read('dataChart.opts.series[0].label') + ' / ' + read("$('data-scale-note').textContent"));
  ok('钉住的窗口按系数换算过去：20~80 µm → ×(1/0.75) = 26.667~106.667 原值',
     read("$('data-xmin').value") === (20 / RAW_FACTOR).toFixed(3) &&
     read("$('data-xmax').value") === (80 / RAW_FACTOR).toFixed(3),
     read("$('data-xmin').value") + ' ~ ' + read("$('data-xmax').value"));
  const peakRaw = read('specLast.peakF');
  ok('频谱跟着口径走：原值口径下时间列大 1/0.75 → 频率小 0.75 倍（同一串数换刻度）',
     Math.abs(peakRaw - peakUm * RAW_FACTOR) < 1e-9 &&
     read("$('spec-title').textContent").indexOf('读回原值') > 0,
     peakUm + ' THz（折算） -> ' + peakRaw + ' THz（原值）');
  run("$('btn-data-scale').onclick()"); await tick();
  ok('切回折算：窗口按系数换回去（26.667~106.667 原值 → ×0.75 = 20~80 µm）',
     read("$('data-xmin').value") === '20.000' && read("$('data-xmax').value") === '80.000' &&
     Math.abs(read('specLast.peakF') - peakUm) < 1e-9,
     read("$('data-xmin').value") + ' ~ ' + read("$('data-xmax').value"));

  // 原值口径 + 换读法：位置与时间两列都得跟着口径走
  run("$('btn-data-scale').onclick()"); await tick();   // 回原值口径（框：20~80 µm → 26.667~106.667）
  run("$('btn-data-axis').onclick()"); await tick();    // 再换成时间
  ok('原值口径下换读法：横轴换成**按原值算**的时间列',
     read('JSON.stringify(dataChart.data[0])') === JSON.stringify(rawBody.time_raw_fs) &&
     read('dataChart.opts.series[0].label') === '时间 (fs，按读回原值算)',
     read('dataChart.opts.series[0].label'));
  // 范围框只有 3 位小数（缩放能到多细就是它说了算）：先落进框、再乘系数，两次四舍五入后
  // 误差上界约 1e-3 × 6.67 ≈ 0.007，所以这里比的是"同一个系数"，不是逐个 bit
  ok('位置 → 时间的换算在两种口径下都是同一个系数（×6.6712819）',
     Math.abs(parseFloat(read("$('data-xmin').value")) - 20 / RAW_FACTOR * 6.6712819) < 0.01 &&
     Math.abs(parseFloat(read("$('data-xmax').value")) - 80 / RAW_FACTOR * 6.6712819) < 0.01,
     read("$('data-xmin').value") + ' ~ ' + read("$('data-xmax').value"));

  // 老数据（没记设备）：按钮禁用 + 说清为什么；原值口径遇到它要退回折算，不能拿 undefined 画图
  resetData();
  run("dataScale = 'raw';");      // 故意停在原值口径，再看换一条没有原值列的扫描会怎样
  run("$('data-scan').value = '52'; $('data-scan').onchange()"); await tick();
  await run("$('btn-data-draw').onclick()"); await tick(); await tick();
  ok('老数据没记是哪台设备采的：按钮禁用，title 说清"指认一次就能切"',
     read("$('btn-data-scale').disabled") === true &&
     read("$('btn-data-scale').title").indexOf('指认一次') > 0,
     read("$('btn-data-scale').title"));
  ok('原值口径下遇到没有原值列的扫描：自动退回折算口径（不拿 undefined 画空图）',
     read('dataScale') === 'um' &&
     read('JSON.stringify(dataChart.data[0])') === JSON.stringify(oldBody.position_um));

  // PI：位置本来就是 µm，切了也一样 → 禁用并说明
  resetData();
  run("$('data-scan').value = '51'; $('data-scan').onchange()"); await tick();
  await run("$('btn-data-draw').onclick()"); await tick(); await tick();
  ok('PI 的扫描：按钮禁用，理由是"位置读数本来就是 µm"',
     read("$('btn-data-scale').disabled") === true &&
     read("$('btn-data-scale').title").indexOf('本来就是 µm') > 0,
     read("$('btn-data-scale').title"));

  // 指认一次：送的是**键**（系数只在后端），库里那条跟着变，正在看的曲线重新取一次数
  resetData();
  await run('loadDataSources()'); await tick();
  await run('loadDataScans()'); await tick();
  run("$('data-scan').value = '52'; $('data-scan').onchange()"); await tick();
  await run("$('btn-data-draw').onclick()"); await tick(); await tick();
  ok('未记录时说明行写清"只能用折算后的 µm、指认一次就能切"',
     read("$('data-source').value") === '' &&
     read("$('data-source-note').textContent").indexOf('指认一次') > 0,
     read("$('data-source-note').textContent"));
  calls.length = 0;
  posts.length = 0;
  run("$('data-source').value = 'xmt'; $('data-source').onchange()"); await tick(); await tick(); await tick();
  ok('指认只把**键**发给后端（系数只在后端一处，前端不写也不送）',
     posts.length === 1 && posts[0].url === '/api/scans/52/readback' &&
     posts[0].body === '{"source":"xmt"}', JSON.stringify(posts));
  ok('指认完把列表与曲线重新拉一遍（口径变了，原值列才有内容）',
     calls.indexOf('GET /api/scans') >= 0 &&
     calls.filter((c) => c.indexOf('GET /api/scans/52/pixel') === 0).length === 1,
     JSON.stringify(calls));
  ok('指认完那条的说明行与按钮跟着变（0.75 → 可切原值）',
     read("$('data-source-note').textContent").indexOf('0.75') > 0 &&
     read("$('btn-data-scale').disabled") === false,
     read("$('data-source-note').textContent"));

  // ==================== 数据页：频谱横轴 频率 ↔ 波长 ====================
  // 同一串数换刻度：λ = 系数 ÷ ν —— **每根谱线的功率不变**，只换横坐标（不做密度换算）。
  // 波长列是降序的（ν 升 → λ 降），画之前倒一次；范围框按同一个式子换算（两端会翻）。
  console.log('\n[W] 频谱横轴：频率 (THz) ↔ 波长 (µm)');

  const C_UM_THZ = 299.792458;      // 后端给的系数（前端不许写常数，测试里当期望值）
  const waveBody = withRaw(seriesOf(64, 6, 100, 500, 0), null);
  waveBody.scan_id = 52;
  waveBody.wavelength_um_per_thz = C_UM_THZ;
  const waveRows = [{ id: 52, name: '波长', count: 64, done: 64, start_um: 0, stop_um: 7,
                      status: 'done', created_at: 1700000000, readback_to_um: null }];
  route = (m, url) => (url.indexOf('/api/readback-sources') === 0 ? { body: srcList }
    : url.indexOf('/pixel') > 0 ? { body: waveBody }
    : url.indexOf('/api/scans') === 0 ? { body: waveRows } : { body: {} });
  resetData();
  await run('loadDataSources()'); await tick();
  await run('loadDataScans()'); await tick();
  run("$('data-scan').value = '52'; $('data-scan').onchange()"); await tick();
  calls.length = 0;
  await run("$('btn-data-draw').onclick()"); await tick(); await tick();
  ok('默认还是频率轴（读数就是读数）',
     read("$('btn-spec-axis').textContent") === '横轴：频率 (THz)' &&
     read('specChart.opts.series[0].label') === '频率 (THz)' &&
     read("$('btn-spec-axis').disabled") === false,
     read('specChart.opts.series[0].label'));

  const fFirst = read('specLast.f[0]');
  const fMax = read('specLast.f[specLast.f.length - 1]');
  const fPeak = read('specLast.peakF');
  const yFreq = JSON.parse(read('JSON.stringify(specChart.data[1])'));
  run("$('spec-xmin').value = '1'; $('spec-xmax').value = '2'; $('spec-xmin').oninput()"); await tick();
  run('specLast.__w = 1;');
  calls.length = 0;
  run("$('btn-spec-axis').onclick()"); await tick();
  const waveX = JSON.parse(read('JSON.stringify(specChart.data[0])'));
  const waveY = JSON.parse(read('JSON.stringify(specChart.data[1])'));
  ok('切波长：横轴换成 λ = 系数 ÷ ν，且**升序**画（倒了一次，值跟着自己的点走）',
     read('specChart.opts.series[0].label') === '波长 (µm)' &&
     Math.abs(waveX[0] - C_UM_THZ / fMax) < 1e-9 &&
     Math.abs(waveX[waveX.length - 1] - C_UM_THZ / fFirst) < 1e-9,
     waveX[0] + ' ~ ' + waveX[waveX.length - 1] + ' µm');
  ok('每根谱线的功率一根没变（只是换横坐标，不做密度换算、不乘雅可比因子）',
     JSON.stringify(waveY.slice().reverse()) === JSON.stringify(yFreq) &&
     JSON.stringify(waveY.slice().sort((a, b) => a - b)) ===
     JSON.stringify(yFreq.slice().sort((a, b) => a - b)));
  ok('切波长不重算谱、也不重新取数（同一串数换刻度）',
     read('specLast.__w') === 1 && calls.length === 0, JSON.stringify(calls));
  ok('钉住的范围框按同一个式子换算过去：1~2 THz → 系数/2 ~ 系数/1 µm',
     Math.abs(parseFloat(read("$('spec-xmin').value")) - C_UM_THZ / 2) < 0.002 &&
     Math.abs(parseFloat(read("$('spec-xmax').value")) - C_UM_THZ / 1) < 0.002,
     read("$('spec-xmin').value") + ' ~ ' + read("$('spec-xmax').value") + ' µm');
  ok('统计条跟着换：波长范围 (µm) 与最强分量 (µm)（分辨率仍按频率写）',
     read("$('spec-fmax-k').textContent") === '波长范围 (µm)' &&
     read("$('spec-peak-k').textContent") === '最强分量 (µm)' &&
     html.indexOf('<dt id="spec-df-k">频率分辨率 (THz)</dt>') >= 0 &&   // 静态标题：假 DOM 不解析 HTML 文本

     Math.abs(parseFloat(read("$('spec-peak').textContent")) - C_UM_THZ / fPeak) < 0.002,
     read("$('spec-fmax').textContent") + ' | ' + read("$('spec-peak').textContent") + ' µm');

  run("$('btn-spec-axis').onclick()"); await tick();
  ok('切回频率：横轴、统计条、范围框都换回去（两端再对调一次）',
     read('specChart.opts.series[0].label') === '频率 (THz)' &&
     read("$('spec-peak-k').textContent") === '最强分量 (THz)' &&
     Math.abs(parseFloat(read("$('spec-xmin').value")) - 1) < 0.01 &&
     Math.abs(parseFloat(read("$('spec-xmax').value")) - 2) < 0.01,
     read("$('spec-xmin').value") + ' ~ ' + read("$('spec-xmax').value") + ' THz');

  // 后端没给波长系数（老响应）：按钮禁用 + 说清为什么；停在波长轴时也退回频率轴
  const noCBody = seriesOf(64, 6, 100, 500, 0);
  noCBody.scan_id = 52;
  route = (m, url) => (url.indexOf('/api/readback-sources') === 0 ? { body: srcList }
    : url.indexOf('/pixel') > 0 ? { body: noCBody }
    : url.indexOf('/api/scans') === 0 ? { body: waveRows } : { body: {} });
  resetData();
  run("specAxis = 'wave';");                       // 故意停在波长轴，再看换一条没有系数的数据会怎样
  run("$('data-scan').value = '52'; $('data-scan').onchange()"); await tick();
  await run("$('btn-data-draw').onclick()"); await tick(); await tick();
  ok('取数里没有波长系数（老响应）：按钮禁用、title 说清原因',
     read("$('btn-spec-axis').disabled") === true &&
     read("$('btn-spec-axis').title").indexOf('波长换算系数') > 0,
     read("$('btn-spec-axis').title"));
  ok('停在波长轴时遇到没有系数的数据：自动退回频率轴（不拿 undefined 画空图）',
     read('specAxis') === 'freq' &&
     read('specChart.opts.series[0].label') === '频率 (THz)' &&
     read('JSON.stringify(specChart.data[0])') === read('JSON.stringify(specLast.f)'));

  route = () => ({ body: [] });
  console.log(failed ? '\n===== ' + failed + ' 项失败 =====' : '\n===== 全部通过 =====');
  process.exit(failed ? 1 : 0);
})();
