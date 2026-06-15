/* ASIAIR OPS 共享主题层 v2 — 三套皮肤(A 任务控制台 / B 星图册 / C 精密仪器)+ 顶栏 + 工具。
 * 用法:<script src="/ops-theme.js"></script>(替代旧 topbar.js)
 * 页面骨架约定:body 内由页面自行布局;本文件负责
 *   1) html[data-skin] 三套设计 token(颜色/字体/形状)与通用组件样式
 *   2) 注入固定顶栏:品牌 / 导航 / 时钟 / 主控状态 / 协作角色 / 设备 / 皮肤切换
 *   3) window.OPS:{device, skin, fmt..., fetchJSON, onSkin, onControl}
 * 皮肤持久化:localStorage["asiair-ops:skin"],默认 B。
 */
(() => {
if (window.__opsThemeLoaded) return; window.__opsThemeLoaded = true;

/* ───────────────────────── 1. 皮肤 token + 通用样式 ───────────────────────── */
const CSS = `

@font-face{font-family:"OPS Serif SC";src:url("fonts/noto-serif-sc-400.woff2") format("woff2");font-weight:300 500;font-display:swap}
@font-face{font-family:"OPS Serif SC";src:url("fonts/noto-serif-sc-700.woff2") format("woff2");font-weight:600 800;font-display:swap}
@font-face{font-family:"OPS Mono";src:url("fonts/dejavu-mono-400.woff2") format("woff2");font-weight:300 500;font-display:swap}
@font-face{font-family:"OPS Mono";src:url("fonts/dejavu-mono-700.woff2") format("woff2");font-weight:600 800;font-display:swap}
@font-face{font-family:"OPS Latin Serif";src:url("fonts/dejavu-serif-400.woff2") format("woff2");font-weight:300 700;font-display:swap}
@font-face{font-family:"OPS Latin Serif";src:url("fonts/dejavu-serif-italic.woff2") format("woff2");font-style:italic;font-display:swap}
/* 可选升级:把 cormorant-*.woff2 / spectral-*.woff2 放入 fonts/ 并解开下两行注释即可启用更优雅的拉丁衬线
@font-face{font-family:"Cormorant Garamond";src:url("fonts/cormorant-500.woff2") format("woff2");font-weight:500 600}
@font-face{font-family:"Spectral";src:url("fonts/spectral-300.woff2") format("woff2");font-weight:300 400} */

/* —— B · 星图册 —— */
:root,html[data-skin="B"]{
  --bg:#0a0e1a; --scrim:rgba(8,11,20,.86); --scrim0:rgba(8,11,20,0);
  --text:#e9e2d0; --muted:#8b91a5; --quiet:#5d6478;
  --ac:#c9a227; --ac-soft:rgba(201,162,39,.45);
  --good:#7fae8a; --warn:#c9a227; --bad:#c46a5a;
  --line:rgba(139,145,165,.28); --line-dim:rgba(139,145,165,.13);
  --display:"Cormorant Garamond","OPS Serif SC","OPS Latin Serif","Noto Serif SC","Songti SC",serif;
  --body:"Spectral","OPS Serif SC","OPS Latin Serif","Noto Serif SC",serif;
  --mono:"Spectral","OPS Serif SC","OPS Latin Serif",serif;
  --pill:999px; --glow:0 2px 14px rgba(0,0,0,.7);
  --lab-ls:.3em; --lab-tt:none; --lab-w:300;
  --sky-bg:#0a0e1a; --sky-star:#e9e2d0;
  --sky-grid-eq:rgba(201,162,39,.40); --sky-grid-alt:rgba(139,145,165,.28);
  --sky-horizon:rgba(233,226,208,.55); --sky-ground:rgba(8,11,20,.95);
  --sky-scope:#c9a227; --sky-target:#9db4d0;
  color-scheme:dark;
}

*{box-sizing:border-box}
html,body{margin:0;min-height:100%;background:var(--bg);color:var(--text)}
body{font:13.5px/1.65 var(--body)}
::selection{background:var(--ac);color:var(--bg)}
button{font:inherit}
a{color:inherit}
.num{font-family:var(--mono);font-variant-numeric:tabular-nums}

/* 标签字 */
.lab{font-family:var(--display);font-weight:var(--lab-w);letter-spacing:var(--lab-ls);text-transform:var(--lab-tt);color:var(--muted)}

/* —— 顶栏 —— */
.ops-top{position:fixed;left:0;right:0;top:0;z-index:60;display:flex;align-items:center;gap:18px;
  padding:10px 22px;background:linear-gradient(180deg,var(--scrim),var(--scrim0))}
.ops-brand{line-height:1.15;white-space:nowrap}
.ops-brand b{display:block;font:600 17px var(--display);letter-spacing:.16em}
html[data-skin="B"] .ops-brand b{font-weight:600;letter-spacing:.12em}
.ops-brand span{font:var(--lab-w) 9.5px var(--display);letter-spacing:.38em;color:var(--ac);text-transform:uppercase}
.ops-nav{display:flex;gap:6px}
html[data-skin="B"] .ops-nav{gap:22px}
.ops-nav a{position:relative;color:var(--muted);text-decoration:none;font:var(--lab-w) 14px var(--display);
  letter-spacing:.16em;padding:5px 10px;text-transform:var(--lab-tt)}
.ops-nav a:hover{color:var(--text)}
.ops-nav a.active{color:var(--text)}
html[data-skin="B"] .ops-nav a.active::after{content:"";position:absolute;left:14%;right:14%;bottom:0;height:1px;background:var(--ac)}
.ops-spacer{flex:1}
.ops-acts{display:flex;align-items:center;gap:12px;font-size:12px;color:var(--muted)}
#ops-clock{font:500 12px var(--mono);font-variant-numeric:tabular-nums;color:var(--muted)}
.ops-lamp{display:inline-flex;align-items:center;gap:7px;font:var(--lab-w) 11px var(--display);
  letter-spacing:.14em;padding:3px 11px;border:1px solid var(--line);border-radius:var(--pill);text-transform:var(--lab-tt)}
.ops-lamp i{width:7px;height:7px;border-radius:50%;background:var(--quiet)}
html[data-skin="B"] .ops-lamp i{transform:rotate(45deg);border-radius:0;width:6px;height:6px}
.ops-lamp.self{color:var(--ac);border-color:var(--ac-soft)}
.ops-lamp.self i{background:var(--ac)}
.ops-lamp.busy{color:var(--warn);border-color:var(--warn)}
.ops-lamp.busy i{background:var(--warn)}
select.ops-sel{background:transparent;color:var(--text);border:1px solid var(--line);border-radius:var(--pill);
  font:400 12px var(--body);padding:4px 10px;min-width:96px}
select.ops-sel:focus-visible{outline:1px solid var(--ac);outline-offset:2px}

/* —— 通用:数据行 / 分组 / 按钮 / 抽屉 / 状态行 —— */
.hgroup{padding:12px 0 12px;border-top:1px solid var(--line)}
.hgroup:first-child{border-top:0;padding-top:0}
.hgroup h3{margin:0 0 7px;font:var(--lab-w) 12px var(--display);letter-spacing:var(--lab-ls);
  color:var(--ac);text-transform:var(--lab-tt)}
html[data-skin="B"] .hgroup h3{color:var(--text);font-size:14px}
html[data-skin="B"] .hgroup h3 .rn{color:var(--ac);margin-right:8px}
.hrow{display:flex;justify-content:space-between;align-items:baseline;gap:10px;padding:4px 0;position:relative}
.hrow .k{font:var(--lab-w) 11.5px var(--display);letter-spacing:.14em;color:var(--muted);text-transform:var(--lab-tt)}
.hrow .v{font:500 13.5px var(--mono);font-variant-numeric:tabular-nums;text-align:right;color:var(--text);overflow-wrap:anywhere}
html[data-skin="B"] .hrow .v{font-family:var(--display);font-size:15px}
.hrow .v.ac{color:var(--ac)}
.hrow .v.dim{color:var(--quiet)}
.hrow.live::before{content:"";position:absolute;left:-12px;top:6px;bottom:6px;width:3px;background:var(--ac)}
.lampline{display:inline-flex;align-items:center;gap:7px}
.lampline i{width:7px;height:7px;border-radius:50%;background:var(--quiet)}
html[data-skin="B"] .lampline i{transform:rotate(45deg);border-radius:0;width:6px;height:6px}
.lampline.on i{background:var(--good)}
.lampline.warn i{background:var(--warn)}
.lampline.bad i{background:var(--bad)}

button.con{background:transparent;color:var(--muted);border:1px solid var(--line);border-radius:var(--pill);
  font:var(--lab-w) 11.5px var(--display);letter-spacing:.12em;padding:5px 13px;cursor:pointer;text-transform:var(--lab-tt)}
button.con:hover{color:var(--text);border-color:var(--muted)}
button.con.on{color:var(--ac);border-color:var(--ac-soft)}
button.con:focus-visible{outline:1px solid var(--ac);outline-offset:2px}
button.con:disabled{opacity:.45;cursor:not-allowed}
.con-note{font:var(--lab-w) 10.5px var(--display);letter-spacing:.12em;color:var(--quiet);text-transform:var(--lab-tt)}
html[data-skin="B"] .con-note{font:italic 300 11.5px var(--body);text-transform:none;letter-spacing:.05em}

.statusline{display:flex;align-items:center;gap:13px;border-top:1px solid var(--line);padding-top:8px;font-size:12px;color:var(--muted)}
.statusline .kind{font:600 12px var(--mono)}
.statusline .msg{overflow:hidden;white-space:nowrap;text-overflow:ellipsis;flex:1}
.statusline .ts{font-family:var(--mono);color:var(--quiet)}
html[data-skin="B"] .statusline{font-style:italic;justify-content:center}
html[data-skin="B"] .statusline .msg{flex:0 1 auto;max-width:56vw}

.drawer{position:fixed;left:0;right:0;bottom:0;z-index:70;transform:translateY(100%);transition:transform .32s cubic-bezier(.3,.8,.3,1);
  background:var(--bg);border-top:1px solid var(--ac-soft);max-height:48vh;display:flex;flex-direction:column}
.drawer.open{transform:translateY(0)}
.drawer-head{display:flex;align-items:center;gap:12px;padding:9px 22px;border-bottom:1px solid var(--line)}
.drawer-head b{font:var(--lab-w) 12.5px var(--display);letter-spacing:.22em;color:var(--ac);text-transform:var(--lab-tt)}
.drawer-head button{margin-left:auto}
.drawer pre{margin:0;padding:12px 22px;overflow:auto;font:12px/1.7 var(--mono);color:var(--muted)}

/* 入场编排 */
@media (prefers-reduced-motion:no-preference){
  .seq{opacity:0;transform:translateY(8px);animation:opsSeq .65s cubic-bezier(.2,.7,.3,1) forwards}
  .seq.d1{animation-delay:.14s}.seq.d2{animation-delay:.3s}.seq.d3{animation-delay:.48s}.seq.d4{animation-delay:.64s}
  @keyframes opsSeq{to{opacity:1;transform:none}}
}
/* 大数字 */
.bignum{font:700 32px/1.1 var(--mono);font-variant-numeric:tabular-nums;color:var(--ac);text-shadow:var(--glow);white-space:nowrap}
html[data-skin="B"] .bignum{font:500 38px/1.1 var(--display);color:var(--text)}
`;

const style = document.createElement("style");
style.id = "ops-theme-style";
style.textContent = CSS;
document.head.appendChild(style);

/* ───────────────────────── 2. 皮肤状态 ───────────────────────── */
const SKIN_KEY = "asiair-ops:skin";
const store = {
  get(k, fb){ try{ return localStorage.getItem(k) || fb; }catch(e){ return fb; } },
  set(k, v){ try{ localStorage.setItem(k, v); }catch(e){} },
};
const pad = n => String(n).padStart(2,"0");
const urlDevice = new URLSearchParams(location.search).get("device") || "";
let skin = "B";
document.documentElement.dataset.skin = skin;

const skinCbs = [];
function setSkin(s){
  skin = s; store.set(SKIN_KEY, s);
  document.documentElement.dataset.skin = s;
  skinCbs.forEach(cb => { try{ cb(s); }catch(e){} });
}

/* ───────────────────────── 3. OPS API(立即可用,DOM 无关) ───────────────────────── */
const API = {
  fileMode: location.protocol === "file:",
  device: urlDevice, skin: () => skin, controlState: null, heldBySelf: false,
  _cbs: [], _skinCbs: skinCbs,
  onControl(cb){ this._cbs.push(cb); },
  onSkin(cb){ skinCbs.push(cb); },
  setSkin,
  fmtPad: pad,
  async fetchJSON(url, opts){
    const ac = new AbortController();
    const t = setTimeout(() => ac.abort(), (opts && opts.timeout) || 6000);
    try{
      const r = await fetch(url, Object.assign({ cache:"no-store", signal: ac.signal }, opts));
      clearTimeout(t);
      const payload = await r.json().catch(() => null);
      if (!r.ok) throw new Error((payload && (payload.error || payload.message)) || ("HTTP " + r.status));
      return payload;
    } finally { clearTimeout(t); }
  },
  fmtBytes(n){ if(n==null) return "--";
    const u=["B","KB","MB","GB","TB"]; let i=0; n=+n;
    while(n>=1024&&i<u.length-1){n/=1024;i++;}
    return n.toFixed(n>=100?0:1)+" "+u[i]; },
  fmtAgo(sec){ if(sec==null) return "--";
    sec=Math.max(0,Math.round(sec));
    if(sec<60) return sec+" 秒前";
    if(sec<3600) return Math.floor(sec/60)+" 分钟前";
    if(sec<86400) return Math.floor(sec/3600)+" 小时前";
    return Math.floor(sec/86400)+" 天前"; },
};
window.OPS = API;
/* ── 访问门禁与会话(/api/status 异步刷新;默认放行避免误锁) ── */
API.access = { read_only:false, actions_allowed:true, scan_allowed:true };
API.actionsAllowed = () => !(API.access.read_only || API.access.actions_allowed === false);
API.scanAllowed = () => API.access.scan_allowed !== false;
API.actionEntry = (page) => `${location.origin}/${page}${API.device?`?device=${encodeURIComponent(API.device)}`:""}`;
API.hidden = () => document.hidden;

/* ───────────────────────── 3. 顶栏注入 ───────────────────────── */
function initDom(){

const NAV = [["总览","/monitor-minterm"],["相机","/camera"],["赤道仪","/mount"],["素材库","/materials"]];
const here = location.pathname;
const isActive = (p) => {
  if (p === "/monitor-minterm") return here === "/" || here.startsWith("/monitor-minterm");
  if (p === "/camera") return here.startsWith("/camera") || here.startsWith("/preview");
  if (p === "/materials") return here.startsWith("/materials") || here.startsWith("/library");
  return here.startsWith(p);
};

const top = document.createElement("header");
top.className = "ops-top";
top.innerHTML = `
  <div class="ops-brand"><b>清华天协远程天文台</b><span>THU ASTRO REMOTE OBSERVATORY</span></div>
  <nav class="ops-nav">${NAV.map(([t,p]) =>
    `<a href="${p}${urlDevice?`?device=${encodeURIComponent(urlDevice)}`:""}" class="${isActive(p)?"active":""}">${t}</a>`).join("")}</nav>
  <span class="ops-spacer"></span>
  <div class="ops-acts">
    <span id="ops-clock">--:--:--</span>
    <select id="ops-device" class="ops-sel" aria-label="设备"></select>
    <select id="ops-role" class="ops-sel" aria-label="协作模式">
      <option value="monitor">监控</option><option value="controller">主控</option>
    </select>
    <span id="ops-control" class="ops-lamp"><i></i><span id="ops-control-text">主控空闲</span></span>
  </div>`;
document.body.prepend(top);

/* 时钟 */
const clk = top.querySelector("#ops-clock");
const tick = () => { const d = new Date();
  clk.textContent = `${d.getFullYear()}/${pad(d.getMonth()+1)}/${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`; };
tick(); setInterval(tick, 1000);


const devSel = top.querySelector("#ops-device");
fetch("/api/devices").then(r=>r.json()).then(d=>{
  (d.devices||[]).forEach(dv=>{
    const o=document.createElement("option"); o.value=o.textContent=dv.name; devSel.appendChild(o); });
  const cur = urlDevice || d.default_device || "";
  if (cur) devSel.value = cur;
  API.device = cur;
  syncNavDevice();
  pollRole();  /* 接入修订:设备就绪后立即补一次主控轮询,消除首轮竞态空窗 */
}).catch(()=>{ /* 设备列表不可达时保持空,页面自处理 */ });

devSel.addEventListener("change", () => {
  const u = new URL(location.href);
  u.searchParams.set("device", devSel.value);
  location.href = u.toString();
});
function syncNavDevice(){
  const v = devSel.value || API.device || "";
  if (!v) return;
  top.querySelectorAll(".ops-nav a").forEach(a=>{
    const u = new URL(a.getAttribute("href"), location.origin);
    u.searchParams.set("device", v);
    a.setAttribute("href", u.pathname + u.search);
  });
}

/* 主控角色:沿用旧版协议 */
const sid = (() => {
  const key = "asiair-ops:session-id";
  let s = store.get(key, "");
  if (!s){
    s = (window.crypto && crypto.randomUUID) ? crypto.randomUUID()
      : `session-${Date.now()}-${Math.random().toString(16).slice(2,10)}`;
    store.set(key, s);
  }
  return s;
})();
const ind = top.querySelector("#ops-control");
const indText = top.querySelector("#ops-control-text");
const roleSel = top.querySelector("#ops-role");
function renderRole(p){
  API.controlState = p;
  const holder = p && p.controller;
  const self = !!(p && p.held_by_self);
  API.heldBySelf = self;
  ind.className = "ops-lamp" + (!holder ? "" : self ? " self" : " busy");
  indText.textContent = !holder ? "主控空闲" : self ? "当前主控" : `主控中 · ${holder.display_name || holder.client_ip || "其他会话"}`;
  roleSel.value = self ? "controller" : "monitor";
  API._cbs.forEach(cb => { try{ cb(p); }catch(e){} });
}
async function pollRole(){
  if (!API.device) return;  /* 接入修订:设备未就绪时后端会拒绝 device 空参,跳过本轮 */
  try{
    const q = new URLSearchParams({ device: API.device || "", session_id: sid });
    const r = await fetch(`/api/control-role?${q}`, { cache:"no-store" });
    if (r.ok) renderRole(await r.json());
  }catch(e){}
}
roleSel.addEventListener("change", async () => {
  try{
    const r = await fetch("/api/control-role", { method:"POST",
      headers:{ "Content-Type":"application/json" },
      body: JSON.stringify({ device: API.device || "", session_id: sid, session_label:"web", role: roleSel.value }) });
    if (r.ok) renderRole(await r.json());
  }catch(e){}
});

/* ── /api/status 探测:只读入口 → 顶栏铭牌 + 广播 ops:access ── */
API.sessionId = sid;
(async () => {
  try {
    const st = await Promise.race([
      API.fetchJSON("/api/status"),
      new Promise((_, rj) => setTimeout(() => rj(new Error("status timeout")), 3000)),
    ]);
    API.access = Object.assign(API.access, (st && st.web) || {});
  } catch (e) {}
  if (!API.actionsAllowed()) {
    const page = here.startsWith("/camera") ? "camera" : here.startsWith("/mount") ? "mount"
      : here.startsWith("/materials") ? "materials" : "monitor-minterm";
    const tag = document.createElement("a");
    tag.className = "ops-lamp busy";
    tag.style.textDecoration = "none";
    tag.href = API.actionEntry(page);
    tag.title = "当前入口只读;可在本机 loopback 或以 --allow-remote-actions 启动可操作入口";
    tag.innerHTML = "<i></i><span>只读入口</span>";
    top.querySelector(".ops-acts").prepend(tag);
  }
  document.dispatchEvent(new CustomEvent("ops:access"));
})();
setTimeout(pollRole, 400);
setInterval(pollRole, 10000);

}
if (document.body) initDom();
else document.addEventListener("DOMContentLoaded", initDom);
})();
