/* IRiS web client. Every control is app-drawn: no native <select>, no spinner
   arrows, no browser validation bubbles. Forms carry novalidate and validate here.

   Wrapped in an IIFE: pages destructure these names from window.IRiS, so leaking
   them into global scope would make every page a redeclaration SyntaxError. */
(function () {
"use strict";

const $ = (s, r = document) => r.querySelector(s);
const el = (tag, cls, text) => {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text !== undefined) n.textContent = text;
  return n;
};

/* --------------------------------------------------------------- fetch --- */
// 401/403 means the session is gone or a password change is owed.
async function api(path, opts) {
  const r = await fetch(path, opts);
  if (r.status === 401 || r.status === 403) {
    location.href = "login.html";
    throw new Error("unauthenticated");
  }
  return r;
}
/* fetch cannot report upload progress at all, so an upload that takes a minute
   looks identical to one that has stalled. XHR can, and that is the only reason it
   is here. Same-origin, so the session cookie rides along as usual. */
function upload(path, formData, onProgress, signal) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("POST", path);
    if (signal) {
      if (signal.aborted) { xhr.abort(); return reject(new DOMException("aborted", "AbortError")); }
      signal.addEventListener("abort", () => xhr.abort(), {once: true});
    }
    xhr.upload.addEventListener("progress", e => {
      if (e.lengthComputable) onProgress(e.loaded / e.total);
    });
    // The bytes are gone; what follows is the server reading them, which has no
    // progress to report.
    xhr.upload.addEventListener("load", () => onProgress(1));
    xhr.addEventListener("load", () => {
      if (xhr.status === 401 || xhr.status === 403) {
        location.href = "login.html";
        reject(new Error("unauthenticated"));
        return;
      }
      let data = {};
      try { data = JSON.parse(xhr.responseText); } catch { /* not json */ }
      if (xhr.status >= 400) reject(new Error(data.detail || `error ${xhr.status}`));
      else resolve(data);
    });
    xhr.addEventListener("error", () => reject(new Error("the upload failed")));
    // An AbortError so callers can tell a cancellation from a failure and stay quiet
    // about it.
    xhr.addEventListener("abort", () =>
      reject(new DOMException("the upload was cancelled", "AbortError")));
    xhr.send(formData);
  });
}

async function json(path, opts) {
  const r = await api(path, opts);
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.detail || `error ${r.status}`);
  return data;
}
const send = (path, body, method = "POST") => json(path, {
  method, headers: {"content-type": "application/json"}, body: JSON.stringify(body),
});

/* ----------------------------------------------------------- combobox --- */
/* Replaces <select>. Filterable, because some lists (timezones) are ~500 long. */
function Combo({options, value, onChange, labelledBy}) {
  const wrap = el("div", "combo");
  const btn = el("button", "combo-btn");
  btn.type = "button";
  btn.setAttribute("role", "combobox");
  btn.setAttribute("aria-expanded", "false");
  btn.setAttribute("aria-haspopup", "listbox");
  if (labelledBy) btn.setAttribute("aria-labelledby", labelledBy);
  const val = el("span", "combo-val", value ?? "");
  btn.append(val, el("span", "chev"));

  const pop = el("div", "combo-pop");
  const filter = el("input", "combo-filter");
  filter.type = "text";
  filter.placeholder = "filter…";
  filter.setAttribute("aria-label", "Filter options");
  const list = el("ul", "combo-list");
  list.setAttribute("role", "listbox");
  pop.append(filter, list);
  wrap.append(btn, pop);

  let current = value, cursor = 0, shown = options.slice();

  function paint() {
    list.textContent = "";
    if (!shown.length) {
      list.append(el("li", "combo-none", "no matches"));
      return;
    }
    shown.forEach((opt, i) => {
      const li = el("li", "combo-opt" + (i === cursor ? " cursor" : ""), opt);
      li.setAttribute("role", "option");
      li.setAttribute("aria-selected", String(opt === current));
      li.addEventListener("mousedown", e => { e.preventDefault(); pick(opt); });
      li.addEventListener("mousemove", () => {
        if (cursor !== i) { cursor = i; paint(); }
      });
      list.append(li);
    });
    const active = list.children[cursor];
    if (active && active.scrollIntoView) active.scrollIntoView({block: "nearest"});
  }

  function open() {
    wrap.dataset.open = "true";
    btn.setAttribute("aria-expanded", "true");
    filter.value = "";
    shown = options.slice();
    cursor = Math.max(0, shown.indexOf(current));
    paint();
    filter.focus();
  }
  function close() {
    delete wrap.dataset.open;
    btn.setAttribute("aria-expanded", "false");
  }
  function pick(opt) {
    current = opt;
    val.textContent = opt;
    close();
    btn.focus();
    onChange(opt);
  }

  btn.addEventListener("click", () => wrap.dataset.open ? close() : open());
  btn.addEventListener("keydown", e => {
    if (["ArrowDown", "ArrowUp", "Enter", " "].includes(e.key)) { e.preventDefault(); open(); }
  });
  filter.addEventListener("input", () => {
    const q = filter.value.toLowerCase();
    shown = options.filter(o => o.toLowerCase().includes(q));
    cursor = 0;
    paint();
  });
  filter.addEventListener("keydown", e => {
    if (e.key === "ArrowDown") { e.preventDefault(); cursor = Math.min(cursor + 1, shown.length - 1); paint(); }
    else if (e.key === "ArrowUp") { e.preventDefault(); cursor = Math.max(cursor - 1, 0); paint(); }
    else if (e.key === "Home") { e.preventDefault(); cursor = 0; paint(); }
    else if (e.key === "End") { e.preventDefault(); cursor = shown.length - 1; paint(); }
    else if (e.key === "Enter") { e.preventDefault(); if (shown[cursor]) pick(shown[cursor]); }
    else if (e.key === "Escape") { e.preventDefault(); close(); btn.focus(); }
    else if (e.key === "Tab") close();
  });
  document.addEventListener("mousedown", e => {
    if (wrap.dataset.open && !wrap.contains(e.target)) close();
  });

  wrap.setValue = v => { current = v; val.textContent = v ?? ""; };
  return wrap;
}

/* ------------------------------------------------------------ stepper --- */
/* Replaces the native number spinner, whose arrows cannot be styled. */
function Stepper({value, min, max, step = 1, onChange, id}) {
  const wrap = el("div", "stepper");
  const dec = el("button", null, "−");
  const inc = el("button", null, "+");
  dec.type = inc.type = "button";
  dec.setAttribute("aria-label", "Decrease");
  inc.setAttribute("aria-label", "Increase");
  const input = el("input");
  input.type = "text";
  input.inputMode = "numeric";
  if (id) input.id = id;
  input.value = value;

  const clamp = n => Math.min(max ?? Infinity, Math.max(min ?? -Infinity, n));
  function set(n, fire = true) {
    if (Number.isNaN(n)) return;
    n = clamp(n);
    input.value = n;
    dec.disabled = min !== undefined && n <= min;
    inc.disabled = max !== undefined && n >= max;
    if (fire) onChange(n);
  }
  dec.addEventListener("click", () => set(Number(input.value) - step));
  inc.addEventListener("click", () => set(Number(input.value) + step));
  input.addEventListener("keydown", e => {
    if (e.key === "ArrowUp") { e.preventDefault(); set(Number(input.value) + step); }
    if (e.key === "ArrowDown") { e.preventDefault(); set(Number(input.value) - step); }
  });
  let t;
  input.addEventListener("input", () => {
    input.value = input.value.replace(/[^\d-]/g, "");
    clearTimeout(t);
    t = setTimeout(() => set(Number(input.value)), 600);
  });
  input.addEventListener("blur", () => set(Number(input.value)));

  wrap.append(dec, input, inc);
  set(value, false);
  wrap.setValue = v => set(v, false);
  return wrap;
}

/* ------------------------------------------------------------- toggle --- */
function Toggle({checked, onChange, labelledBy}) {
  const b = el("button", "toggle");
  b.type = "button";
  b.setAttribute("role", "switch");
  b.setAttribute("aria-checked", String(!!checked));
  if (labelledBy) b.setAttribute("aria-labelledby", labelledBy);
  b.addEventListener("click", () => {
    const next = b.getAttribute("aria-checked") !== "true";
    b.setAttribute("aria-checked", String(next));
    onChange(next);
  });
  b.setValue = v => b.setAttribute("aria-checked", String(!!v));
  return b;
}

window.IRiS = {$, el, api, json, send, upload, Combo, Stepper, Toggle};
})();

/* ---------------------------------------------------------- backdrop --- */
/* A slow constellation behind the app: points drifting, lines drawn between the
   ones that happen to be near each other. Quiet enough to read over, and it gives
   the grid something to do besides sit there.

   Density follows the viewport so a phone draws a handful and a monitor draws a
   field, and the whole thing stops when the tab is hidden or when the machine has
   asked for less motion. */
(function backdrop() {
  const still = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  const canvas = document.createElement("canvas");
  canvas.className = "backdrop";
  canvas.setAttribute("aria-hidden", "true");
  document.body.prepend(canvas);
  const ctx = canvas.getContext("2d", {alpha: true});

  let points = [], w = 0, h = 0, dpr = 1, raf = null;
  const LINK = 140;                    // how close two points must be to connect

  function size() {
    dpr = Math.min(window.devicePixelRatio || 1, 2);
    w = canvas.clientWidth;
    h = canvas.clientHeight;
    canvas.width = Math.round(w * dpr);
    canvas.height = Math.round(h * dpr);
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    // One point per ~22k px², so the field looks the same on a phone and a monitor.
    const want = Math.max(14, Math.min(90, Math.round((w * h) / 22000)));
    points = Array.from({length: want}, () => ({
      x: Math.random() * w,
      y: Math.random() * h,
      vx: (Math.random() - 0.5) * 0.14,
      vy: (Math.random() - 0.5) * 0.14,
      warm: Math.random() < 0.16,      // a few carry the accent colour
    }));
  }

  function frame() {
    ctx.clearRect(0, 0, w, h);
    for (const p of points) {
      p.x += p.vx;
      p.y += p.vy;
      // Wrap rather than bounce: bouncing makes the edges look like walls.
      if (p.x < -20) p.x = w + 20;
      if (p.x > w + 20) p.x = -20;
      if (p.y < -20) p.y = h + 20;
      if (p.y > h + 20) p.y = -20;
    }
    for (let i = 0; i < points.length; i++) {
      for (let j = i + 1; j < points.length; j++) {
        const a = points[i], b = points[j];
        const dx = a.x - b.x, dy = a.y - b.y;
        const d2 = dx * dx + dy * dy;
        if (d2 > LINK * LINK) continue;
        const near = 1 - Math.sqrt(d2) / LINK;
        ctx.strokeStyle = (a.warm || b.warm)
          ? `rgba(255,122,26,${(near * 0.20).toFixed(3)})`
          : `rgba(140,152,170,${(near * 0.13).toFixed(3)})`;
        ctx.lineWidth = 1;
        ctx.beginPath();
        ctx.moveTo(a.x, a.y);
        ctx.lineTo(b.x, b.y);
        ctx.stroke();
      }
    }
    for (const p of points) {
      ctx.fillStyle = p.warm ? "rgba(255,122,26,.55)" : "rgba(160,172,190,.34)";
      ctx.beginPath();
      ctx.arc(p.x, p.y, p.warm ? 1.6 : 1.2, 0, Math.PI * 2);
      ctx.fill();
    }
    raf = requestAnimationFrame(frame);
  }

  function start() {
    if (raf || still) return;
    raf = requestAnimationFrame(frame);
  }
  function stop() {
    if (raf) cancelAnimationFrame(raf);
    raf = null;
  }

  size();
  if (still) frame(), stop();          // draw it once, then leave it alone
  else start();

  let resizeTimer;
  window.addEventListener("resize", () => {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(() => { size(); if (still) { frame(); stop(); } }, 150);
  });
  // Nothing to animate for a tab nobody is looking at.
  document.addEventListener("visibilitychange", () =>
    document.hidden ? stop() : start());
})();
