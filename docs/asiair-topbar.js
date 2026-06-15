/* ASIAIR OPS shared topbar — the single source of truth for the bar on every page.
 *
 * Usage:  <header id="app-topbar"></header><script src="/topbar.js"></script>
 * A page that keeps OWNERSHIP of role/device wiring declares, BEFORE this script:
 *   <script>window.OPS_TOPBAR_DELEGATE = { role: true, device: true };</script>
 * The DOM/ids/styles are identical either way: #ops-clock, #ops-control,
 * #ops-role, #ops-device. Everything is hard-coded here on purpose — no page
 * CSS variables, no page fonts, no page sizes can leak in.
 */
(() => {
  if (window.__opsTopbarLoaded) return;
  window.__opsTopbarLoaded = true;
  const DELEG = window.OPS_TOPBAR_DELEGATE || {};

  const CSS = `
.ops-topbar{box-sizing:border-box;height:54px;display:grid;grid-template-columns:minmax(160px,220px) 1fr auto;align-items:center;background:#020303;border-bottom:1px solid #2b3030;position:sticky;top:0;z-index:60;min-width:0;font-family:"Cascadia Mono","SFMono-Regular",Consolas,Menlo,monospace;}
.ops-topbar *{box-sizing:border-box;font-family:inherit;}
.ops-brand{padding:0 14px;color:#00f5ff;font-weight:800;font-size:18px;letter-spacing:2px;white-space:nowrap;}
.ops-nav{height:100%;display:flex;align-items:center;gap:8px;min-width:0;overflow:hidden;}
.ops-nav a{color:#8c9693;text-decoration:none;padding:10px 12px;border:1px solid transparent;font-size:14px;font-weight:400;white-space:nowrap;}
.ops-nav a:hover{color:#f2f6f4;}
.ops-nav a.active{color:#f2f6f4;border-color:#2b3030;background:#070909;}
.ops-actions{display:flex;align-items:center;gap:10px;padding-right:12px;color:#8c9693;font-size:13px;white-space:nowrap;min-width:0;}
#ops-clock{color:#8c9693;font-size:13px;font-weight:400;}
.ops-indicator{font-size:12px;font-weight:700;padding:4px 10px;border:1px solid #2b3030;border-radius:999px;color:#8c9693;}
.ops-indicator.free{color:#8c9693;border-color:#2b3030;}
.ops-indicator.self{color:#35ff4f;border-color:#35ff4f;}
.ops-indicator.busy{color:#ffb300;border-color:#ffb300;}
select.ops-select{background:#050707;color:#f2f6f4;border:1px solid #2b3030;border-radius:0;font-weight:700;font-size:13px;padding:5px 8px;min-width:96px;}
`;
  const style = document.createElement("style");
  style.id = "ops-topbar-style";
  style.textContent = CSS;
  document.head.appendChild(style);

  const urlDevice = new URLSearchParams(location.search).get("device") || "";
  const NAV = [["总览", "/monitor-minterm"], ["相机", "/camera"], ["赤道仪", "/mount"], ["素材库", "/materials"]];
  const here = location.pathname;
  const isActive = (p) => {
    if (p === "/monitor-minterm") return here === "/" || here.startsWith("/monitor-minterm");
    if (p === "/camera") return here.startsWith("/camera") || here.startsWith("/preview");
    if (p === "/materials") return here.startsWith("/materials") || here.startsWith("/library");
    return here.startsWith(p);
  };

  const host = document.getElementById("app-topbar");
  if (!host) return;
  host.className = "ops-topbar";
  host.innerHTML = `
    <div class="ops-brand">ASIAIR OPS</div>
    <nav class="ops-nav">${NAV.map(([t, p]) =>
      `<a href="${p}${urlDevice ? `?device=${encodeURIComponent(urlDevice)}` : ""}" class="${isActive(p) ? "active" : ""}">${t}</a>`).join("")}</nav>
    <div class="ops-actions">
      <span id="ops-clock">--:--:--</span>
      <span id="ops-control" class="ops-indicator free">主控空闲</span>
      <select id="ops-role" class="ops-select" aria-label="协作模式">
        <option value="monitor">监控</option>
        <option value="controller">主控</option>
      </select>
      <select id="ops-device" class="ops-select" aria-label="设备"></select>
    </div>`;

  const clk = document.getElementById("ops-clock");
  const pad = (n) => String(n).padStart(2, "0");
  const tick = () => {
    const d = new Date();
    clk.textContent = `${d.getFullYear()}/${d.getMonth() + 1}/${d.getDate()} ${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
  };
  tick();
  setInterval(tick, 1000);

  const API = {
    device: urlDevice,
    controlState: null,
    heldBySelf: false,
    _cbs: [],
    onControl(cb) { this._cbs.push(cb); },
  };
  window.OpsTopbar = API;

  // Keep nav links + API.device synced with whatever the device select shows,
  // whether the component or the page drives it.
  const devSel = document.getElementById("ops-device");
  const syncNav = () => {
    const v = devSel.value || "";
    if (!v) return;  // not populated yet — never sync an empty value
    if (v === API.device && host.dataset.navDev === v) return;
    API.device = v;
    host.dataset.navDev = v;
    host.querySelectorAll(".ops-nav a").forEach((a) => {
      const u = new URL(a.getAttribute("href"), location.origin);
      if (v) u.searchParams.set("device", v); else u.searchParams.delete("device");
      a.setAttribute("href", u.pathname + (u.search || ""));
    });
  };
  setInterval(syncNav, 1000);
  devSel.addEventListener("change", syncNav);

  if (!DELEG.device) {
    fetch("/api/devices").then((r) => r.json()).then((d) => {
      const sel = document.getElementById("ops-device");
      (d.devices || []).forEach((dv) => {
        const o = document.createElement("option");
        o.value = o.textContent = dv.name;
        sel.appendChild(o);
      });
      const cur = urlDevice || d.default_device || "";
      if (cur) sel.value = cur;
      API.device = cur;
      sel.addEventListener("change", () => {
        const u = new URL(location.href);
        u.searchParams.set("device", sel.value);
        location.href = u.toString();
      });
    }).catch(() => {});
  }

  if (!DELEG.role) {
    const sid = (() => {
      // Same identity key as the camera/overview pages, so "当前主控" is
      // recognized as the same session on every page.
      const key = "asiair-ops:session-id";
      let s = null;
      try { s = localStorage.getItem(key); } catch (e) {}
      if (!s) {
        s = (window.crypto && crypto.randomUUID)
          ? crypto.randomUUID()
          : `session-${Date.now()}-${Math.random().toString(16).slice(2, 10)}`;
        try { localStorage.setItem(key, s); } catch (e) {}
      }
      return s;
    })();
    const ind = document.getElementById("ops-control");
    const sel = document.getElementById("ops-role");
    const renderState = (p) => {
      API.controlState = p;
      const holder = p && p.controller;
      const self = !!(p && p.held_by_self);
      API.heldBySelf = self;
      ind.className = "ops-indicator";
      if (!holder) { ind.textContent = "主控空闲"; ind.classList.add("free"); }
      else if (self) { ind.textContent = "当前主控"; ind.classList.add("self"); }
      else { ind.textContent = `主控中 · ${holder.display_name || holder.client_ip || "其他会话"}`; ind.classList.add("busy"); }
      sel.value = self ? "controller" : "monitor";
      API._cbs.forEach((cb) => { try { cb(p); } catch (e) {} });
    };
    const poll = async () => {
      try {
        const q = new URLSearchParams({ device: API.device || "", session_id: sid });
        const r = await fetch(`/api/control-role?${q}`, { cache: "no-store" });
        if (r.ok) renderState(await r.json());
      } catch (e) {}
    };
    sel.addEventListener("change", async () => {
      try {
        const r = await fetch("/api/control-role", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ device: API.device || "", session_id: sid, session_label: "web", role: sel.value }),
        });
        if (r.ok) renderState(await r.json());
      } catch (e) {}
    });
    setTimeout(poll, 400);
    setInterval(poll, 10000);
  }
})();
