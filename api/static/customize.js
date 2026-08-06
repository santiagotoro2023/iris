/* The Customize tab (SPEC.md 55).

   Settings configures the machine. Customize configures what IRiS *does*: the
   briefings it writes, the automations that make it speak first, the tool library it
   pulls from, the composer's quick commands, and the character it plays.

   Its own file rather than more of index.html, which was already 2,100 lines. Nothing
   here is loaded by the chat path; the tab renders the first time it is opened. */
(function () {
"use strict";

const {$, el, api, json, send, Combo, Stepper, Toggle} = window.IRiS;

const SECTIONS = [
  {id: "briefings", label: "Briefings", render: renderBriefings},
  {id: "automations", label: "Automations", render: renderAutomations},
  {id: "tools", label: "Tools", render: renderTools},
  {id: "commands", label: "Quick commands", render: renderCommands},
  {id: "persona", label: "Persona", render: renderPersona},
];
const DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
const HOURS = Array.from({length: 24}, (_, h) => String(h).padStart(2, "0") + ":00");
const RECURRENCE = {daily: "Every day", weekdays: "Weekdays", weekly: "Certain days",
                    monthly: "Once a month", never: "Only when asked"};

let CURRENT = "";
let WIDGETS = [];          // the catalogue, from the server

/* ------------------------------------------------------------- plumbing -- */

function panel(title) {
  const box = el("div", "panel");
  if (title) box.append(el("h2", null, title));
  return box;
}

function bar(...kids) {
  const row = el("div", "panel-bar");
  row.append(...kids);
  return row;
}

function button(cls, text, onClick) {
  const b = el("button", cls, text);
  b.type = "button";
  b.addEventListener("click", onClick);
  return b;
}

function field(label, control, help) {
  const wrap = el("div", "thing-field");
  const id = "cz-" + Math.random().toString(36).slice(2, 9);
  const tag = el("label", null, label);
  tag.id = id;
  wrap.append(tag);
  if (help) wrap.append(el("div", "desc", help));
  if (control.setAttribute) control.setAttribute("aria-labelledby", id);
  wrap.append(control);
  return wrap;
}

/* Two buttons, unlike the one-button modal in index.html. Used where a change can
   break something and the person should be told what, not just that. */
function confirmModal({title, message, confirmText = "Save anyway", danger = false}) {
  return new Promise(resolve => {
    const back = el("div", "modal-back");
    const box = el("div", "modal");
    box.append(el("h3", null, title), el("p", null, message));
    const done = answer => {
      back.remove();
      document.removeEventListener("keydown", esc);
      resolve(answer);
    };
    function esc(e) { if (e.key === "Escape") done(false); }
    const go = button(danger ? "btn danger" : "btn", confirmText, () => done(true));
    const row = el("div", "thing-actions");
    row.append(go, button("btn secondary", "Cancel", () => done(false)));
    box.append(row);
    back.append(box);
    back.addEventListener("click", e => { if (e.target === back) done(false); });
    document.addEventListener("keydown", esc);
    document.body.append(back);
    go.focus();
  });
}

function status(node, text, bad = false) {
  node.textContent = text;
  node.className = "status " + (bad ? "err" : "ok");
}

/* Renders one server-declared field spec, the same shape Devices and Integrations
   already use, so a widget or trigger gaining a field needs no work here. */
function control(spec, value) {
  if (spec.type === "boolean") {
    let on = value === undefined ? !!spec.default : !!value;
    const node = Toggle({checked: on, onChange: v => { on = v; }});
    node.read = () => on;
    return node;
  }
  if (spec.type === "number") {
    let n = Number(value === undefined || value === "" ? spec.default : value) || 0;
    const node = Stepper({value: n, min: 0, max: 100000,
                          onChange: v => { n = v; }});
    node.read = () => n;
    return node;
  }
  if (spec.choices && spec.choices.length && !spec.free) {
    let picked = value === undefined || value === "" ? spec.default : value;
    const node = Combo({options: spec.choices, value: picked,
                        onChange: v => { picked = v; }});
    node.read = () => picked;
    return node;
  }
  const input = el("input", "input");
  input.type = "text";
  input.value = value === undefined ? (spec.default ?? "") : value;
  if (spec.choices && spec.choices.length) {
    input.placeholder = spec.choices.slice(0, 4).join(", ") + "…";
  }
  input.read = () => input.value.trim();
  return input;
}

function form(fields, config) {
  const host = el("div", "cz-fields");
  const controls = {};
  for (const spec of fields || []) {
    const node = control(spec, (config || {})[spec.name]);
    controls[spec.name] = node;
    host.append(field(spec.label, node, spec.help));
  }
  host.read = () => {
    const out = {};
    for (const [name, node] of Object.entries(controls)) out[name] = node.read();
    return out;
  };
  return host;
}

/* ------------------------------------------------------------ briefings -- */

function scheduleEditor(schedule) {
  const host = el("div", "cz-schedule");
  const state = {recurrence: "daily", at: "07:00", days: [], day_of_month: 1,
                 enabled: true, ...schedule};
  const extra = el("div");

  function paintExtra() {
    extra.textContent = "";
    if (state.recurrence === "weekly") {
      const row = el("div", "cz-days");
      DAYS.forEach((label, i) => {
        const on = (state.days || []).includes(i);
        const b = button("cz-day" + (on ? " on" : ""), label, () => {
          const days = new Set(state.days || []);
          if (days.has(i)) days.delete(i); else days.add(i);
          state.days = [...days].sort((a, b) => a - b);
          paintExtra();
        });
        b.setAttribute("aria-pressed", String(on));
        row.append(b);
      });
      extra.append(field("Which days", row));
    } else if (state.recurrence === "monthly") {
      extra.append(field("Day of the month",
        Stepper({value: state.day_of_month || 1, min: 1, max: 28,
                 onChange: v => { state.day_of_month = v; }})));
    }
  }

  const time = field("At", Combo({options: HOURS, value: state.at || "07:00",
                                  onChange: v => { state.at = v; }}));
  const how = Combo({
    options: Object.values(RECURRENCE),
    value: RECURRENCE[state.recurrence] || RECURRENCE.daily,
    onChange: label => {
      state.recurrence =
        Object.keys(RECURRENCE).find(k => RECURRENCE[k] === label) || "daily";
      time.hidden = state.recurrence === "never";
      paintExtra();
    },
  });
  time.hidden = state.recurrence === "never";

  host.append(field("How often", how), time, extra);
  paintExtra();
  host.read = () => state;
  return host;
}

function widgetRow(entry, spec, editor) {
  const row = el("div", "cz-widget");
  row.draggable = true;
  row.dataset.id = entry.id;

  const head = el("div", "cz-widget-head");
  const grip = el("span", "cz-grip", "::");
  grip.setAttribute("aria-hidden", "true");
  head.append(grip, el("span", "cz-widget-name", spec.label));

  if (!spec.wordless) {
    const count = Stepper({
      value: entry.sentences ?? spec.default_sentences, min: 1, max: 8,
      onChange: v => { entry.sentences = v; }});
    const wrap = el("span", "cz-count");
    wrap.append(el("span", "desc", "sentences"), count);
    head.append(wrap);
  }

  /* Dragging is unreachable from a keyboard, so the same move is also two buttons.
     Without them the whole editor is mouse-only. */
  const up = button("ghost", "↑", () => editor.move(entry.id, -1));
  up.setAttribute("aria-label", `Move ${spec.label} earlier`);
  const down = button("ghost", "↓", () => editor.move(entry.id, 1));
  down.setAttribute("aria-label", `Move ${spec.label} later`);
  head.append(el("span", "spacer"), up, down);

  const body = el("div", "cz-widget-body");
  body.hidden = true;
  if (spec.fields.length) {
    const fields = form(spec.fields, entry.config);
    body.append(fields);
    row.readConfig = () => fields.read();
    const open = button("ghost", "settings", () => {
      body.hidden = !body.hidden;
      open.textContent = body.hidden ? "settings" : "hide";
      open.setAttribute("aria-expanded", String(!body.hidden));
    });
    open.setAttribute("aria-expanded", "false");
    head.append(open);
  } else {
    row.readConfig = () => ({});
  }
  head.append(button("ghost", "remove", () => editor.remove(entry.id)));
  row.append(head, body);
  return row;
}

function widgetEditor(widgets) {
  const host = el("div");
  const list = el("div", "cz-widget-list");
  let entries = (widgets || []).map((w, i) => ({...w, id: w.id || "w" + i}));
  const rows = new Map();

  /* The open forms are the truth while the editor is open, so any reorder has to read
     them back before the DOM is thrown away and rebuilt. */
  function collect() {
    for (const entry of entries) {
      const row = rows.get(entry.id);
      if (row) entry.config = row.readConfig();
    }
  }

  const editor = {
    move(id, delta) {
      const at = entries.findIndex(e => e.id === id);
      const to = at + delta;
      if (at < 0 || to < 0 || to >= entries.length) return;
      collect();
      [entries[at], entries[to]] = [entries[to], entries[at]];
      paint();
      list.querySelector(`[data-id="${id}"] .ghost`)?.focus();
    },
    remove(id) {
      collect();
      entries = entries.filter(e => e.id !== id);
      paint();
    },
    add(type) {
      const spec = WIDGETS.find(w => w.name === type);
      collect();
      entries.push({id: "w" + Date.now().toString(36) + entries.length, type,
                    sentences: spec.default_sentences, config: {}});
      paint();
    },
  };

  function paint() {
    list.textContent = "";
    rows.clear();
    if (!entries.length) {
      list.append(el("div", "empty", "No widgets yet. Add one below."));
      return;
    }
    for (const entry of entries) {
      const spec = WIDGETS.find(w => w.name === entry.type);
      if (!spec) continue;
      const row = widgetRow(entry, spec, editor);
      rows.set(entry.id, row);
      list.append(row);
    }
  }

  let dragging = null;
  list.addEventListener("dragstart", e => {
    const row = e.target.closest(".cz-widget");
    if (!row) return;
    dragging = row;
    row.classList.add("dragging");
    e.dataTransfer.effectAllowed = "move";
    // Firefox refuses to start a drag unless the transfer carries something.
    e.dataTransfer.setData("text/plain", row.dataset.id);
  });
  list.addEventListener("dragend", () => {
    dragging?.classList.remove("dragging");
    list.querySelectorAll(".over").forEach(n => n.classList.remove("over"));
    dragging = null;
  });
  list.addEventListener("dragover", e => {
    if (!dragging) return;
    e.preventDefault();
    const over = e.target.closest(".cz-widget");
    list.querySelectorAll(".over").forEach(n => n.classList.remove("over"));
    if (over && over !== dragging) over.classList.add("over");
  });
  list.addEventListener("drop", e => {
    if (!dragging) return;
    e.preventDefault();
    const over = e.target.closest(".cz-widget");
    if (!over || over === dragging) return;
    collect();
    const from = entries.findIndex(x => x.id === dragging.dataset.id);
    const to = entries.findIndex(x => x.id === over.dataset.id);
    const [moved] = entries.splice(from, 1);
    entries.splice(to, 0, moved);
    paint();
  });

  const palette = el("div", "type-grid");
  for (const spec of WIDGETS) {
    const card = button("type-card", "", () => editor.add(spec.name));
    card.append(el("strong", null, spec.label), el("span", null, spec.description));
    palette.append(card);
  }

  host.append(list, el("div", "cz-subhead", "Add a widget"), palette);
  paint();
  host.read = () => {
    collect();
    return entries.map(e => ({id: e.id, type: e.type, sentences: e.sentences,
                              config: e.config || {}}));
  };
  return host;
}

function briefingEditor(briefing, onDone) {
  const box = el("div", "thing-form");
  const name = el("input", "input");
  name.type = "text";
  name.value = briefing.name || "";
  const schedule = scheduleEditor(briefing.schedule || {});
  const widgets = widgetEditor(briefing.widgets || []);

  const options = briefing.options || {};
  let greeting = options.greeting !== false;
  let cap = Number(options.max_sentences || 0);

  const note = el("div", "status");
  const save = button("btn", briefing.id ? "Save" : "Create", async () => {
    save.disabled = true;
    const body = {name: name.value.trim(), schedule: schedule.read(),
                  options: {greeting, max_sentences: cap}, widgets: widgets.read()};
    try {
      if (briefing.id) await send("briefings/" + briefing.id, body, "PATCH");
      else await send("briefings", body, "POST");
      onDone();
    } catch (e) {
      status(note, e.message, true);
      save.disabled = false;
    }
  });

  const actions = el("div", "thing-actions");
  actions.append(save, button("btn secondary", "Cancel", onDone), note);
  box.append(
    field("Name", name),
    schedule,
    el("div", "cz-subhead", "What it says, in order"),
    widgets,
    field("Open with a greeting",
          Toggle({checked: greeting, onChange: v => { greeting = v; }})),
    field("Cap the whole briefing at (sentences)",
          Stepper({value: cap, min: 0, max: 40, onChange: v => { cap = v; }}),
          "0 leaves it to the per-widget budgets above."),
    actions);
  return box;
}

async function renderBriefings(host) {
  const data = await json("briefings");
  WIDGETS = data.widgets || [];
  const box = panel("Briefings");
  const editing = el("div");

  const startNew = () => {
    editing.textContent = "";
    editing.append(briefingEditor({name: "", schedule: {}, options: {}, widgets: []},
                                  () => { editing.textContent = ""; show("briefings"); }));
    editing.scrollIntoView({block: "nearest"});
  };
  box.append(bar(button("btn", "New briefing", startNew),
                 el("span", "desc", "Each briefing has its own schedule and its own "
                                    + "widgets. The default is what you get when you "
                                    + "just say 'brief me'.")));
  box.append(editing);

  if (!data.briefings.length) box.append(el("div", "empty", "No briefings yet."));
  for (const b of data.briefings) {
    const row = el("div", "mem-row");
    const head = el("div", "thing-head");
    head.append(el("strong", null, b.name));
    if (b.is_default) head.append(el("span", "cz-badge", "default"));
    head.append(el("span", "spacer"), el("span", "mem-meta", b.schedule_text));
    row.append(head);
    row.append(el("div", "desc", (b.widgets || []).map(w => {
      const spec = WIDGETS.find(x => x.name === w.type);
      return spec ? spec.label : w.type;
    }).join(" · ") || "no widgets"));

    const note = el("span", "status");
    const actions = el("div", "mem-meta");
    actions.append(button("ghost", "edit", () => {
      editing.textContent = "";
      editing.append(briefingEditor(b, () => {
        editing.textContent = "";
        show("briefings");
      }));
      editing.scrollIntoView({block: "nearest"});
    }));
    const run = button("ghost", "run now", async () => {
      run.disabled = true;
      status(note, "writing…");
      try {
        const r = await send(`briefings/${b.id}/run`, {}, "POST");
        status(note, `delivered, ${r.text.length} characters`);
      } catch (e) { status(note, e.message, true); }
      run.disabled = false;
    });
    actions.append(run);
    if (!b.is_default) {
      actions.append(button("ghost", "make default", async () => {
        await send("briefings/" + b.id, {is_default: true}, "PATCH");
        show("briefings");
      }));
    }
    actions.append(Toggle({checked: b.enabled, onChange: v =>
      send("briefings/" + b.id, {enabled: v}, "PATCH")}));
    actions.append(button("ghost", "remove", async () => {
      if (!await confirmModal({
        title: "Remove this briefing?",
        message: `"${b.name}" and its widgets will be deleted. This cannot be undone.`,
        confirmText: "Remove", danger: true})) return;
      try {
        await json("briefings/" + b.id, {method: "DELETE"});
        show("briefings");
      } catch (e) { status(note, e.message, true); }
    }), note);
    row.append(actions);
    box.append(row);
  }
  host.append(box);
}

/* ---------------------------------------------------------- automations -- */

const DELIVERIES = [["chat", "In the chat"], ["push", "To my phone"],
                    ["webhook", "To a webhook"]];

function ruleEditor(rule, triggers, briefings, onDone) {
  const box = el("div", "thing-form");
  const spec = triggers.find(t => t.name === rule.trigger);
  const name = el("input", "input");
  name.type = "text";
  name.value = rule.name || "";
  const config = form(spec ? spec.fields : [], rule.config);

  const action = rule.action || {};
  const deliver = new Set(action.deliver || ["chat"]);
  const where = el("div", "cz-days");
  for (const [key, label] of DELIVERIES) {
    const b = button("cz-day" + (deliver.has(key) ? " on" : ""), label, () => {
      if (deliver.has(key)) deliver.delete(key); else deliver.add(key);
      b.classList.toggle("on", deliver.has(key));
      b.setAttribute("aria-pressed", String(deliver.has(key)));
    });
    b.setAttribute("aria-pressed", String(deliver.has(key)));
    where.append(b);
  }

  const message = el("textarea", "input multiline");
  message.rows = 3;
  message.value = action.message || "";
  message.placeholder = "Leave blank and IRiS words it from what happened.";

  let briefingName = action.briefing || "";
  const note = el("div", "status");
  const save = button("btn", rule.id ? "Save" : "Create", async () => {
    save.disabled = true;
    const body = {name: name.value.trim(), trigger: rule.trigger,
                  config: config.read(),
                  action: {deliver: [...deliver], message: message.value.trim(),
                           briefing: briefingName}};
    try {
      if (rule.id) await send("rules/" + rule.id, body, "PATCH");
      else await send("rules", body, "POST");
      onDone();
    } catch (e) { status(note, e.message, true); save.disabled = false; }
  });

  box.append(field("Name", name));
  if (spec) box.append(el("div", "desc", spec.description));
  const actions = el("div", "thing-actions");
  actions.append(save, button("btn secondary", "Cancel", onDone), note);
  box.append(config,
             field("Tell me", where),
             field("What to say", message),
             field("Or run a briefing instead",
                   Combo({options: ["", ...briefings.map(b => b.name)],
                          value: briefingName,
                          onChange: v => { briefingName = v; }}),
                   "Chosen here, the message above is ignored."),
             actions);
  return box;
}

async function renderAutomations(host) {
  const [data, briefingData] = await Promise.all([
    json("rules"), json("briefings").catch(() => ({briefings: []}))]);
  const triggers = data.triggers || [];
  const box = panel("Automations");
  const editing = el("div");

  box.append(bar(button("btn", "New automation", () => {
    editing.textContent = "";
    const pick = el("div", "thing-form");
    const grid = el("div", "type-grid");
    for (const t of triggers) {
      const card = button("type-card", "", () => {
        editing.textContent = "";
        editing.append(ruleEditor({trigger: t.name, config: {}, action: {}}, triggers,
                                  briefingData.briefings,
                                  () => { editing.textContent = ""; show("automations"); }));
      });
      card.append(el("strong", null, t.label), el("span", null, t.description));
      grid.append(card);
    }
    pick.append(el("div", "cz-subhead", "What should set it off?"), grid,
                button("btn secondary", "Cancel", () => { editing.textContent = ""; }));
    editing.append(pick);
    editing.scrollIntoView({block: "nearest"});
  }), el("span", "desc", "A briefing runs at a time. An automation runs when "
                         + "something happens.")));
  box.append(editing);

  if (!data.rules.length) box.append(el("div", "empty", "Nothing automated yet."));
  for (const r of data.rules) {
    const row = el("div", "mem-row");
    const head = el("div", "thing-head");
    head.append(el("strong", null, r.name), el("span", "cz-badge", r.label),
                el("span", "spacer"),
                Toggle({checked: r.enabled,
                        onChange: v => send("rules/" + r.id, {enabled: v}, "PATCH")}));
    row.append(head);
    row.append(el("div", "desc",
      (r.action.briefing ? `runs the "${r.action.briefing}" briefing`
                         : `tells you ${(r.action.deliver || []).join(", ") || "nowhere"}`)
      + (r.fired ? ` · last fired ${new Date(r.fired * 1000).toLocaleString()}`
                 : " · never fired")));

    const note = el("span", "status");
    const actions = el("div", "mem-meta");
    actions.append(button("ghost", "edit", () => {
      editing.textContent = "";
      editing.append(ruleEditor(r, triggers, briefingData.briefings,
                                () => { editing.textContent = ""; show("automations"); }));
      editing.scrollIntoView({block: "nearest"});
    }));
    actions.append(button("ghost", "test", async () => {
      status(note, "checking…");
      try {
        const out = await send(`rules/${r.id}/test`, {}, "POST");
        if (out.error) status(note, out.error, true);
        else if (!out.fired) status(note, out.detail);
        else status(note, (out.already_sent ? "already sent: " : "") + out.text);
      } catch (e) { status(note, e.message, true); }
    }));
    actions.append(button("ghost", "remove", async () => {
      if (!await confirmModal({title: "Remove this automation?",
                               message: `"${r.name}" will stop watching.`,
                               confirmText: "Remove", danger: true})) return;
      await json("rules/" + r.id, {method: "DELETE"});
      show("automations");
    }), note);
    row.append(actions);
    box.append(row);
  }
  host.append(box);
}

/* ---------------------------------------------------------------- tools -- */

const BREAK_WARNING =
  "What a tool tells the model is how the model decides when to use it and what to "
  + "put in its arguments. A change here can stop the tool being chosen at all, or "
  + "make it be called with the wrong values. Reset puts the original wording back at "
  + "any time, so nothing here is permanent.";

function toolDetail(tool, refresh) {
  const body = el("div", "cz-tool-body");
  const description = el("textarea", "input multiline");
  description.rows = 5;
  description.value = tool.description;

  const triggers = el("input", "input");
  triggers.type = "text";
  triggers.value = (tool.triggers || []).join(", ");

  const args = el("div");
  const argInputs = {};
  const declared = (tool.declared_parameters || {}).properties || {};
  const live = (tool.parameters || {}).properties || {};
  for (const [key, spec] of Object.entries(declared)) {
    const input = el("textarea", "input multiline");
    input.rows = 2;
    input.value = (live[key] || {}).description || "";
    argInputs[key] = input;
    args.append(field(`${key} (${spec.type || "string"})`, input));
  }

  const declaredTriggers = (tool.declared_triggers || []).join(", ");
  const note = el("div", "status");
  const save = button("btn", "Save", async () => {
    const parameters = {};
    for (const [key, input] of Object.entries(argInputs)) {
      const original = (declared[key] || {}).description || "";
      if (input.value.trim() && input.value.trim() !== original) {
        parameters[key] = input.value.trim();
      }
    }
    const changed = description.value.trim() !== tool.declared_description
      || triggers.value.trim() !== declaredTriggers
      || Object.keys(parameters).length > 0;
    if (changed && !await confirmModal({title: "This might break the tool",
                                        message: BREAK_WARNING})) return;
    save.disabled = true;
    try {
      await send("tools/" + tool.name, {
        description: description.value.trim() === tool.declared_description
          ? "" : description.value.trim(),
        triggers: triggers.value.trim() === declaredTriggers
          ? "" : triggers.value.trim(),
        parameters,
      }, "PATCH");
      refresh();
    } catch (e) { status(note, e.message, true); save.disabled = false; }
  });

  const actions = el("div", "thing-actions");
  actions.append(save);
  if (tool.overridden) {
    actions.append(button("btn secondary", "Reset to default", async () => {
      await send(`tools/${tool.name}/reset`, {}, "POST");
      refresh();
    }));
  }
  actions.append(note);

  body.append(
    field("What the model is told this tool does", description),
    field("Words that pull it in", triggers,
          "Comma separated, matched against your message. A word match beats "
          + "similarity, so these decide the routing more than the wording above."),
    Object.keys(declared).length
      ? el("div", "cz-subhead", "What each argument means") : el("span"),
    args,
    el("div", "desc", `Banner while it runs: "${tool.activity}". Result shown as `
                      + `${tool.display}.`),
    actions);
  return body;
}

async function renderTools(host) {
  const data = await json("tools");
  const box = panel("Tools");
  box.append(el("div", "cz-explain",
    "IRiS is not handed every tool on every message. Trigger words settle it first, "
    + "and when none match, the tools whose descriptions are closest to what you said "
    + `are offered instead, up to ${data.budget} of them alongside the ones it always `
    + "has. That is why it stays quick and accurate as tools are added. The number is "
    + "under Settings, 'Tools offered per message'."));

  const edited = data.tools.filter(t => t.overridden);
  box.append(bar(
    button("btn secondary", "Reset every tool", async () => {
      if (!edited.length) return;
      if (!await confirmModal({
        title: "Reset every tool?",
        message: "All edited descriptions, trigger words and argument wording go back "
               + "to what the code declares, and any tool you switched off is "
               + "switched on again.",
        confirmText: "Reset all", danger: true})) return;
      for (const t of edited) await send(`tools/${t.name}/reset`, {}, "POST");
      show("tools");
    }),
    el("span", "desc", edited.length
      ? `${edited.length} tool${edited.length === 1 ? " has" : "s have"} been edited.`
      : "Nothing has been changed yet.")));

  const groups = {};
  for (const t of data.tools) (groups[t.group] = groups[t.group] || []).push(t);
  for (const [group, tools] of Object.entries(groups)) {
    box.append(el("div", "cz-group", group));
    for (const tool of tools) {
      const row = el("div", "mem-row");
      const head = el("div", "thing-head");
      const detail = el("div");
      detail.hidden = true;
      const open = button("cz-tool-name", tool.label, () => {
        detail.hidden = !detail.hidden;
        open.setAttribute("aria-expanded", String(!detail.hidden));
        if (!detail.hidden && !detail.childElementCount) {
          detail.append(toolDetail(tool, () => show("tools")));
        }
      });
      open.setAttribute("aria-expanded", "false");
      head.append(open);
      if (tool.core) head.append(el("span", "cz-badge", "always on"));
      if (tool.overridden) head.append(el("span", "cz-badge edited", "edited"));
      const warn = el("div", "status");
      head.append(el("span", "spacer"),
                  Toggle({checked: tool.enabled, onChange: async v => {
                    const r = await send("tools/" + tool.name, {enabled: v}, "PATCH");
                    if (r.warning) status(warn, r.warning, true);
                    else warn.textContent = "";
                  }}));
      row.append(head,
                 el("div", "desc", tool.description.split("\n")[0].slice(0, 170)),
                 warn, detail);
      box.append(row);
    }
  }
  host.append(box);
}

/* ------------------------------------------------------------- commands -- */

async function renderCommands(host) {
  const data = await json("tools/commands");
  const box = panel("Quick commands");
  box.append(el("div", "cz-explain",
    "The menu beside the composer. Choosing one prepends an instruction to your "
    + "message, which is enough to aim the model at the right tool without taking the "
    + "decision away from it. The instruction is never stored, so the transcript "
    + "reads as what you actually typed."));

  const editing = el("div");
  box.append(bar(button("btn", "New command", () => {
    editing.textContent = "";
    const box2 = el("div", "thing-form");
    const label = el("input", "input");
    const hint = el("input", "input");
    const directive = el("textarea", "input multiline");
    directive.rows = 3;
    const note = el("div", "status");
    const actions = el("div", "thing-actions");
    actions.append(button("btn", "Create", async () => {
      try {
        await send("tools/commands", {label: label.value.trim(),
                                      hint: hint.value.trim(),
                                      directive: directive.value.trim()}, "POST");
        editing.textContent = "";
        show("commands");
      } catch (e) { status(note, e.message, true); }
    }), button("btn secondary", "Cancel", () => { editing.textContent = ""; }), note);
    box2.append(field("Label", label, "What you see in the menu."),
                field("Hint", hint, "What to type after choosing it."),
                field("Instruction", directive,
                      "Prepended to your message, e.g. 'Use the weather tool and give "
                      + "the numbers in a sentence.'"),
                actions);
    editing.append(box2);
  })));
  box.append(editing);

  for (const c of data.commands) {
    const row = el("div", "mem-row");
    const head = el("div", "thing-head");
    head.append(el("strong", null, c.label));
    if (!c.custom) head.append(el("span", "cz-badge", "built in"));
    head.append(el("span", "spacer"),
                Toggle({checked: c.enabled !== false, onChange: v =>
                  send("tools/commands/" + c.name, {enabled: v}, "PATCH")}));
    row.append(head, el("div", "desc", c.directive));
    if (c.custom) {
      const actions = el("div", "mem-meta");
      actions.append(button("ghost", "remove", async () => {
        await json("tools/commands/" + c.name, {method: "DELETE"});
        show("commands");
      }));
      row.append(actions);
    }
    box.append(row);
  }
  host.append(box);
}

/* -------------------------------------------------------------- persona -- */

async function renderPersona(host) {
  const [schema, values] = await Promise.all([
    json("settings/schema"), json("settings/values")]);
  const props = schema.properties || {};
  const box = panel("Persona");
  box.append(el("div", "cz-explain",
    "The character IRiS plays. It is sent with every message and kept out of the "
    + "stored transcript, so an edit takes effect on conversations that already exist "
    + "rather than being frozen in when they were created."));

  const note = el("div", "status");
  const commit = async (key, title, value) => {
    try {
      await send("settings/values", {[key]: value}, "PUT");
      status(note, `${title} saved`);
    } catch (e) { status(note, e.message, true); }
  };

  for (const [key, spec] of Object.entries(props)) {
    if (!key.startsWith("persona.")) continue;
    const row = el("div", "row");
    const id = "pz-" + key.replace(/\./g, "-");
    const label = el("label", null, spec.title || key);
    label.id = id;
    row.append(label);
    if (spec.description) row.append(el("div", "desc", spec.description));

    let node;
    if (spec.type === "boolean") {
      node = Toggle({checked: !!values[key], labelledBy: id,
                     onChange: v => commit(key, spec.title, v)});
    } else {
      node = spec.format === "multiline"
        ? el("textarea", "input multiline") : el("input", "input");
      if (node.tagName === "TEXTAREA") node.rows = 18; else node.type = "text";
      node.value = values[key] ?? "";
      node.setAttribute("aria-labelledby", id);
      node.addEventListener("change", () => commit(key, spec.title, node.value));
    }
    row.append(node);
    box.append(row);
  }

  box.append(bar(button("btn secondary", "Reset to the default persona", async () => {
    if (!await confirmModal({
      title: "Reset the persona?",
      message: "Your edits to the persona prompt are replaced by the one IRiS ships "
             + "with. This cannot be undone.",
      confirmText: "Reset", danger: true})) return;
    await send("settings/values",
               {"persona.prompt": props["persona.prompt"].default}, "PUT");
    show("persona");
  }), note));
  host.append(box);
}

/* ----------------------------------------------------------------- rail -- */

async function renderInto(host, section) {
  const spec = SECTIONS.find(s => s.id === section) || SECTIONS[0];
  host.textContent = "";
  host.append(el("div", "empty", "loading…"));
  try {
    const built = el("div");
    await spec.render(built);
    host.textContent = "";
    host.append(...built.children);
  } catch (e) {
    host.textContent = "";
    host.append(el("div", "empty", e.message || "that could not be loaded"));
  }
}

function show(section) {
  CURRENT = section;
  for (const b of document.querySelectorAll("#cz-rail button")) {
    b.setAttribute("aria-current", String(b.dataset.section === section));
  }
  if (location.hash.startsWith("#customize")) {
    history.replaceState(null, "", "#customize/" + section);
  }
  renderInto($("#cz-pane"), section);
}

/* index.html owns the tab switching; this only needs to know it became visible. */
window.IRiS.openCustomize = function (section) {
  const rail = $("#cz-rail");
  if (!rail.childElementCount) {
    for (const s of SECTIONS) {
      const b = button("cz-rail-item", s.label, () => show(s.id));
      b.dataset.section = s.id;
      rail.append(b);
    }
  }
  const fromHash = location.hash.startsWith("#customize/")
    ? location.hash.split("/")[1] : "";
  const wanted = section || fromHash || CURRENT || "briefings";
  show(SECTIONS.some(s => s.id === wanted) ? wanted : "briefings");
};
})();
