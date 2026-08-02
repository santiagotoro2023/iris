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

window.IRiS = {$, el, api, json, send, Combo, Stepper, Toggle};
})();
