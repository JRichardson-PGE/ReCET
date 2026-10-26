/* The Edit inputs page: two Tabulator grids (Measure, ProgramCost) for one draft.

   Every change is saved (PUT, debounced) and the server answers with the cell checks (editor.check_tables) and the dropdown values for the claim years now
   in the table. Each row carries a private _rid (never sent) so a check result, which names rows by position at the time of the save, still lands on the
   right row after later inserts. No sorting or filtering, so the grid order is the table order. Cell text is always set as text, never as HTML. */
(function () {
  "use strict";
  var root = document.getElementById("editor");
  if (!root || typeof Tabulator === "undefined") return;

  var API = root.dataset.api, END_USES = root.dataset.endUses, CSRF = root.dataset.csrf, RUN = root.dataset.run;
  var TABLES = ["Measure", "ProgramCost"];
  var WIDTHS = { CEInputID: 120, MeasCode: 100, MeasDescription: 230, PrgID: 180, AC_Elec_Impact_Profile: 240, AC_Elec_Impact_Profile_2nd_Baseline: 240,
                 AC_Elec_Impact_Profile_Water: 240, AC_Gas_Impact_Profile: 200 };
  var END_USE_COLUMNS = ["AC_Elec_Impact_Profile", "AC_Elec_Impact_Profile_2nd_Baseline", "AC_Elec_Impact_Profile_Water"];   // per-row dropdowns (IOU and target sector)
  var st = {
    rev: null, layout: null, columns: {}, choices: {}, issues: { Measure: {}, ProgramCost: {} }, checkMarks: { Measure: {}, ProgramCost: {} }, grids: {}, current: "Measure",
    timer: null, saving: false, pending: false, dirty: false, conflict: false, nextRid: 1, endUses: {}, maxRows: 500, newMeasure: null, pcLike: []
  };

  // ------------------------------------------------------------------------------------------------------------ helpers
  function el(tag, cls, text) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text != null) e.textContent = text;
    return e;
  }

  function setStatus(text, kind) {
    var s = document.getElementById("save-status");
    s.textContent = text;
    s.className = "save-status small " + (kind || "muted");
  }

  // editor.display_number: at most 6 decimals, trailing zeros dropped; display only
  var NUM = /^[+-]?(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?$/;
  function displayNumber(v) {
    var s = v == null ? "" : String(v).trim();
    if (!s || !NUM.test(s)) return s;
    var f = Number(s);
    if (!isFinite(f)) return s;
    var out = f.toFixed(6).replace(/0+$/, "").replace(/\.$/, "");
    if ((out === "0" || out === "-0") && f !== 0) out = Number(f.toPrecision(3)).toExponential();
    return out === "-0" ? "0" : out;
  }

  function uniqueId(base, taken) {
    var stem = String(base || "NEW").replace(/_(copy|\d+)(_\d+)?$/, "") || "NEW";
    for (var i = 1; i < 100000; i++) {
      var cand = stem + "_" + i;
      if (!taken.has(cand)) return cand;
    }
    return stem + "_" + Date.now();
  }

  function rowsOf(t) { return st.grids[t].getRows("active"); }

  function withRid(obj) { obj._rid = st.nextRid++; return obj; }

  function toObjects(t, table) {
    return table.rows.map(function (r) {
      var o = {};
      table.columns.forEach(function (c, i) { o[c] = r[i] == null ? "" : String(r[i]); });
      return withRid(o);
    });
  }

  function blankRow(t) {
    var o = {};
    st.columns[t].forEach(function (c) { o[c] = ""; });
    return o;
  }

  // ------------------------------------------------------------------------------------------------------------ columns
  // colours a cell by its check results (the cell checks of the last save, and the last Validate inputs check); touches only the cell's class and title,
  // so an open editor in it survives
  var RANK = { error: 3, warning: 2, info: 1 };
  function markIssue(t, cell) {
    var e = cell.getElement(), rid = cell.getRow().getData()._rid, f = cell.getField();
    var found = [(st.issues[t][rid] || {})[f], (st.checkMarks[t][rid] || {})[f]].filter(Boolean);
    e.classList.remove("issue-error", "issue-warning", "issue-info");
    if (found.length) {
      var sev = found.reduce(function (s, x) { return RANK[x.severity] > RANK[s] ? x.severity : s; }, "info");
      e.classList.add("issue-" + sev);
      e.title = found.map(function (x) { return x.messages.join("\n"); }).join("\n");
    } else {
      e.removeAttribute("title");
    }
  }

  // redraws the marks of these rows ({table: Set of rid strings}); rows deleted since have nothing to redraw
  function remark(touched) {
    TABLES.forEach(function (t) {
      if (!touched[t] || !touched[t].size) return;
      var live = new Map(rowsOf(t).map(function (r) { return [String(r.getData()._rid), r]; }));
      touched[t].forEach(function (rid) {
        var row = live.get(String(rid));
        if (row) row.getCells().forEach(function (c) { markIssue(t, c); });   // not reformat(): that would close an open editor
      });
    });
  }

  function formatter(t, col, numeric, list) {
    return function (cell) {
      markIssue(t, cell);
      var v = cell.getValue(), text = numeric ? displayNumber(v) : (v == null ? "" : String(v));
      if (!list) return el("span", null, text);
      // a value-list column shows an arrow, like a dropdown, so it reads as "pick from a list"
      var w = el("span", "list-cell");
      w.appendChild(el("span", "list-val", text));
      var a = el("span", "list-arrow", "\u25BE");
      a.setAttribute("aria-hidden", "true");
      w.appendChild(a);
      return w;
    };
  }

  function endUseLookup(cell) {
    var d = cell.getRow().getData();
    var iou = d.IOU_AC_Territory || "", sector = d.AC_Elec_Target_Sector || "", key = iou + "|" + sector;
    if (st.endUses[key]) return st.endUses[key];
    return fetch(END_USES + "?iou=" + encodeURIComponent(iou) + "&sector=" + encodeURIComponent(sector), { credentials: "same-origin" })
      .then(function (r) { return r.json(); })
      .then(function (j) { st.endUses[key] = j.values || [""]; return st.endUses[key]; })
      .catch(function () { return [""]; });
  }

  function columnDefs(t) {
    var L = st.layout[t], noEffect = new Set(L.no_effect), numeric = new Set(L.numeric);
    return L.pinned.concat(L.scroll).map(function (col) {
      var endUse = t === "Measure" && END_USE_COLUMNS.indexOf(col) >= 0, list = endUse || !!(st.choices[t] || {})[col];
      var d = {
        title: col, field: col, frozen: L.pinned.indexOf(col) >= 0, cssClass: noEffect.has(col) ? "noeff" : "calc",
        headerTooltip: L.tooltips[col] || "", formatter: formatter(t, col, numeric.has(col), list), hozAlign: numeric.has(col) ? "right" : "left",
        width: WIDTHS[col] || Math.min(240, Math.max(84, col.length * 7 + 26)), editor: "input", editorParams: { selectContents: true }
      };
      if (endUse) {
        d.editor = "list";
        d.editorParams = { valuesLookup: endUseLookup, autocomplete: true, freetext: true, allowEmpty: true, listOnEmpty: true, filterDelay: 150 };
      } else if ((st.choices[t] || {})[col]) {
        d.editor = "list";
        d.editorParams = function () {
          return { values: (st.choices[t] || {})[col] || [""], autocomplete: true, freetext: true, allowEmpty: true, listOnEmpty: true };
        };
      }
      return d;
    });
  }

  function buildGrid(t, data) {
    var g = new Tabulator("#grid-" + t, {
      data: toObjects(t, data), index: "_rid", height: "62vh", layout: "fitData", placeholder: "No rows",
      columns: columnDefs(t),
      rowHeader: { formatter: "rownum", headerSort: false, hozAlign: "center", resizable: false, frozen: true, width: 46 },
      columnDefaults: { headerSort: false, resizable: "header" },
      selectableRange: 1, selectableRangeColumns: true, selectableRangeRows: true,
      selectableRangeClearCells: true, selectableRangeClearCellsValue: "",
      editTriggerEvent: "dblclick",
      clipboard: true, clipboardCopyStyled: false, clipboardCopyConfig: { rowHeaders: false, columnHeaders: false, formatCells: false },
      clipboardCopyRowRange: "range", clipboardPasteParser: "range", clipboardPasteAction: "range",
      history: true
    });
    ["cellEdited", "clipboardPasted", "rowAdded", "rowDeleted", "historyUndo", "historyRedo", "dataChanged"].forEach(function (ev) {
      g.on(ev, function () { changed(t); });
    });
    // a cell changed since the last Validate inputs check loses that check's mark (the next check says whether it is fixed)
    g.on("cellEdited", function (cell) { clearCheckMark(t, cell.getRow().getData()._rid, [cell.getField()]); });
    g.on("clipboardPasted", function (_text, rowData, rows) {
      var fields = Object.keys((rowData && rowData[0]) || {});
      (rows || []).forEach(function (r) { clearCheckMark(t, r.getData()._rid, fields); });
    });
    // A block pasted (Excel copy) while a cell's editor is open goes to the grid's paste, not into the one input.
    g.element.addEventListener("paste", function (e) {
      var text = e.clipboardData && e.clipboardData.getData("text/plain");
      if (!text || e.target.tagName !== "INPUT" || !/[\t\n]/.test(text.replace(/\r?\n$/, ""))) return;
      e.preventDefault();
      e.stopPropagation();
      var editing = g.element.querySelector(".tabulator-cell.tabulator-editing");
      var cell = editing && g.getRows("active").reduce(function (hit, r) {
        return hit || r.getCells().find(function (c) { return c.getElement() === editing; });
      }, null);
      if (cell) cell.cancelEdit();
      var dt = new DataTransfer();
      dt.setData("text/plain", text);
      g.element.querySelector(".tabulator-tableholder").dispatchEvent(new ClipboardEvent("paste", { clipboardData: dt, bubbles: true, cancelable: true }));
    }, true);
    // One click opens the editor. Shift / Ctrl / Cmd clicks are left to the range selection, and a click inside a cell that is
    // already being edited (placing the caret) is left alone. Double-click still works too. The edit waits a tick because the
    // range selection moves focus to the grid on the same click, which would close an editor opened straight away.
    g.on("cellClick", function (e, cell) {
      var col = cell.getField(), elm = cell.getElement();
      showHelp(t, col);
      if (!col || e.shiftKey || e.ctrlKey || e.metaKey || elm.classList.contains("tabulator-editing")) return;
      setTimeout(function () {
        if (elm.classList.contains("tabulator-editing") || !elm.isConnected) return;
        elm.focus({ preventScroll: true });
        cell.edit();
      }, 0);
    });
    st.grids[t] = g;
  }

  function showHelp(t, col) {
    var help = document.getElementById("help");
    var L = st.layout[t];
    if (!col || !L) return;
    help.textContent = col + ": " + (L.help[col] || "no description") + (L.tooltips[col] ? "  |  " + L.tooltips[col] : "");
  }

  function updateCounts() {
    TABLES.forEach(function (t) {
      var c = document.querySelector('[data-count="' + t + '"]');
      if (c && st.grids[t]) c.textContent = "(" + rowsOf(t).length + ")";
    });
  }

  // ------------------------------------------------------------------------------------------------------------ saving and checks
  function changed(t) {
    if (st.conflict || !st.loaded) return;
    st.dirty = true;
    updateCounts();
    setStatus("Unsaved changes…");
    clearTimeout(st.timer);
    st.timer = setTimeout(save, 700);
  }

  function collect(t) {
    var cols = st.columns[t], rows = rowsOf(t).map(function (r) { return r.getData(); });
    return {
      rids: rows.map(function (d) { return d._rid; }),
      table: { columns: cols, rows: rows.map(function (d) { return cols.map(function (c) { return d[c] == null ? "" : String(d[c]); }); }) }
    };
  }

  function save() {
    clearTimeout(st.timer);
    if (st.conflict) return Promise.resolve(false);
    if (st.saving) { st.pending = true; return st.saving; }
    var snap = {}, body = { rev: st.rev, tables: {} };
    TABLES.forEach(function (t) { var c = collect(t); snap[t] = c.rids; body.tables[t] = c.table; });
    st.dirty = false;
    setStatus("Saving…");
    st.saving = fetch(API, {
      method: "PUT", credentials: "same-origin", body: JSON.stringify(body),
      headers: { "Content-Type": "application/json", "X-CSRF-Token": CSRF }
    }).then(function (r) {
      return r.json().catch(function () { return {}; }).then(function (j) { return { ok: r.ok, status: r.status, j: j }; });
    }).then(function (res) {
      if (!res.ok) {
        st.dirty = true;
        if (res.status === 409) st.conflict = true;
        var msg = res.j.error || ("Not saved (" + res.status + ")");
        setStatus(res.status === 401 ? "Signed out: reload the page and sign in again. Your last changes were not saved." : msg, "bad");
        return false;
      }
      st.rev = res.j.rev;
      st.choices = res.j.choices || st.choices;
      applyIssues(res.j.issues || [], snap);
      if (chk.info) staleNote();
      setStatus(st.dirty ? "Unsaved changes…" : "Saved " + new Date().toLocaleTimeString(), st.dirty ? "muted" : "ok");
      return true;
    }).catch(function (e) {
      st.dirty = true;
      setStatus("Not saved: " + e.message + ". Your changes are still here; they will be saved with the next change.", "bad");
      return false;
    }).then(function (ok) {
      var again = st.pending;
      st.saving = false;
      st.pending = false;
      if (again || (ok && st.dirty)) return save();
      return ok;
    });
    return st.saving;
  }

  function applyIssues(list, snap) {
    var touched = { Measure: new Set(Object.keys(st.issues.Measure)), ProgramCost: new Set(Object.keys(st.issues.ProgramCost)) };
    var rank = { error: 3, warning: 2, info: 1 };
    st.issues = { Measure: {}, ProgramCost: {} };
    st.issueList = [];
    list.forEach(function (i) {
      var rid = snap[i.table] && snap[i.table][i.row];
      if (rid == null) return;
      var byCol = st.issues[i.table][rid] = st.issues[i.table][rid] || {};
      var cur = byCol[i.column] = byCol[i.column] || { severity: i.severity, messages: [] };
      if (rank[i.severity] > rank[cur.severity]) cur.severity = i.severity;
      cur.messages.push(i.message);
      touched[i.table].add(String(rid));
      st.issueList.push({ table: i.table, rid: rid, column: i.column, severity: i.severity, message: i.message });
    });
    remark(touched);
    renderIssues();
  }

  function renderIssues() {
    var ul = document.getElementById("issues"), sum = document.getElementById("issue-summary");
    ul.textContent = "";
    var pos = {};
    TABLES.forEach(function (t) {
      pos[t] = {};
      rowsOf(t).forEach(function (r, i) { pos[t][r.getData()._rid] = i + 1; });
    });
    var list = (st.issueList || []).filter(function (i) { return pos[i.table][i.rid]; });
    var order = { error: 0, warning: 1, info: 2 };
    list.sort(function (a, b) {
      return order[a.severity] - order[b.severity] || (a.table < b.table ? -1 : a.table > b.table ? 1 : 0) || pos[a.table][a.rid] - pos[b.table][b.rid];
    });
    var n = { error: 0, warning: 0, info: 0 };
    list.forEach(function (i) { n[i.severity]++; });
    st.cellCounts = n;
    st.firstError = list.find(function (i) { return i.severity === "error"; }) || null;
    list.slice(0, 400).forEach(function (i) {
      var li = el("li", i.severity);
      var b = el("button", "link", i.table + " row " + pos[i.table][i.rid] + ", " + i.column);
      b.type = "button";
      b.title = "Show this cell";
      b.addEventListener("click", function () { goTo(i.table, i.rid, i.column); });
      li.appendChild(b);
      li.appendChild(document.createTextNode(": " + i.message));
      ul.appendChild(li);
    });
    if (list.length > 400) ul.appendChild(el("li", "info", "… and " + (list.length - 400) + " more"));
    var toggle = document.getElementById("issues-toggle");
    toggle.hidden = !list.length;
    if (!list.length) { ul.hidden = true; toggle.setAttribute("aria-expanded", "false"); }
    toggle.textContent = (ul.hidden ? "Show" : "Hide") + " the " + (list.length === 1 ? "cell" : list.length.toLocaleString() + " cells");
    renderSummary();
  }

  // One verdict for the page, from the cell checks (every save) and the latest Validate inputs check while it is for the saved tables. A Validate result
  // for an older version of the tables never says the run can start (it said so above the grid while the cell checks below it found problems).
  function renderSummary() {
    var sum = document.getElementById("issue-summary"), n = st.cellCounts || { error: 0, warning: 0 };
    var rep = chk.report, current = rep && chk.info && chk.info.rev === st.rev, v = current ? chk.reportCounts : null;
    var plural = function (k, one, many) { return k.toLocaleString() + " " + (k === 1 ? one : many); };
    var text, kind;
    if (n.error) {
      text = plural(n.error, "problem", "problems") + " in the cells would stop the run (outlined in red)" + (n.warning ? "; " + plural(n.warning, "cell", "cells") + " to check" : "") + ".";
      kind = "error";
    } else if (v && v.error) {
      text = "Validate inputs found " + plural(v.error, "problem that stops", "problems that stop") + " the run: see below.";
      kind = "error";
    } else if (n.warning || (v && v.warning)) {
      text = "Ready to run; " + (n.warning ? plural(n.warning, "cell", "cells") + " to check" : "")
        + (n.warning && v && v.warning ? " and " : "") + (v && v.warning ? plural(v.warning, "warning", "warnings") + " from Validate inputs" : "")
        + " (the run goes ahead, but CET would reject a value or some measures are not priced).";
      kind = "warning";
    } else if (v) {
      text = "No problems found: the run can start (" + chk.info.acc_version + " ACC check of these tables).";
      kind = "ok";
    } else {
      text = "No problems in the cells. Validate inputs runs the full check a run makes" + (rep ? " (the last one is for an older version of the tables)." : ".");
      kind = "";
    }
    sum.textContent = text;
    sum.className = "issue-line " + kind;
  }

  function goTo(t, rid, col) {
    show(t);
    var g = st.grids[t];
    g.scrollToRow(rid, "center", false).then(function () {
      return g.scrollToColumn(col, "middle", false);
    }).catch(function () {}).then(function () {
      var row = g.getRow(rid);
      var cell = row && row.getCell(col);
      if (cell) {
        var e = cell.getElement();
        e.classList.add("flash");
        setTimeout(function () { e.classList.remove("flash"); }, 1200);
      }
      showHelp(t, col);
    });
  }

  // ------------------------------------------------------------------------------------------------------------ tabs and row buttons
  function show(t) {
    st.current = t;
    TABLES.forEach(function (x) {
      document.getElementById("grid-" + x).hidden = x !== t;
      var tab = document.querySelector('.tab[data-table="' + x + '"]');
      tab.classList.toggle("active", x === t);
      tab.setAttribute("aria-selected", x === t ? "true" : "false");
    });
    st.grids[t].redraw(true);
    fillColumnNames();
  }

  // ------------------------------------------------------------------------------------------------------------ finding columns
  function fillColumnNames() {
    var dl = document.getElementById("col-names"), L = st.layout && st.layout[st.current];
    if (!dl || !L) return;
    dl.textContent = "";
    L.pinned.concat(L.scroll).forEach(function (c) { var o = el("option"); o.value = c; dl.appendChild(o); });
  }

  var colFind = document.getElementById("col-find");
  colFind.addEventListener("change", function () {
    var t = st.current, g = st.grids[t], q = colFind.value.trim().toLowerCase();
    if (!g || !q) return;
    var L = st.layout[t], cols = L.pinned.concat(L.scroll);
    var hit = cols.find(function (c) { return c.toLowerCase() === q; }) || cols.find(function (c) { return c.toLowerCase().indexOf(q) >= 0; });
    if (!hit) { colFind.setCustomValidity("No column named like that"); colFind.reportValidity(); return; }
    colFind.setCustomValidity("");
    var col = g.getColumn(hit);
    if (!col.isVisible()) col.show();
    g.scrollToColumn(hit, "middle", false).catch(function () {}).then(function () {
      var h = col.getElement();
      h.classList.add("flash");
      setTimeout(function () { h.classList.remove("flash"); }, 1200);
      showHelp(t, hit);
    });
  });

  document.getElementById("hide-noeff").addEventListener("change", function (e) {
    TABLES.forEach(function (t) {
      var g = st.grids[t];
      if (!g) return;
      (st.layout[t].no_effect || []).forEach(function (c) {
        var col = g.getColumn(c);
        if (col && st.layout[t].pinned.indexOf(c) < 0) { if (e.target.checked) col.hide(); else col.show(); }
      });
    });
  });

  document.getElementById("issues-toggle").addEventListener("click", function () {
    var ul = document.getElementById("issues"), b = this;
    ul.hidden = !ul.hidden;
    b.setAttribute("aria-expanded", ul.hidden ? "false" : "true");
    b.textContent = b.textContent.replace(/^(Show|Hide)/, ul.hidden ? "Show" : "Hide");
  });

  function selectedRows(t) {
    var seen = new Set(), out = [];
    st.grids[t].getRanges().forEach(function (range) {
      range.getRows().forEach(function (r) {
        var rid = r.getData()._rid;
        if (!seen.has(rid)) { seen.add(rid); out.push(r); }
      });
    });
    var pos = new Map(rowsOf(t).map(function (r, i) { return [r.getData()._rid, i]; }));
    out.sort(function (a, b) { return pos.get(a.getData()._rid) - pos.get(b.getData()._rid); });
    return out;
  }

  function addRow() {
    var t = st.current, g = st.grids[t], sel = selectedRows(t), all = rowsOf(t);
    var after = sel.length ? sel[sel.length - 1] : all[all.length - 1];
    var like = after ? after.getData() : null;
    var row = blankRow(t);
    if (t === "Measure") {
      if (all.length >= st.maxRows) { window.alert("The editor is for jobs of up to " + st.maxRows + " measures."); return; }
      Object.assign(row, st.newMeasure.defaults);
      st.newMeasure.like.forEach(function (c) { if (like && like[c]) row[c] = like[c]; });
      row.CEInputID = uniqueId("NEW", new Set(all.map(function (r) { return r.getData().CEInputID; })));
    } else {
      st.pcLike.forEach(function (c) { if (like && like[c]) row[c] = like[c]; });
    }
    g.addRow(withRid(row), false, after || undefined).then(function (r) { g.scrollToRow(r, "nearest", false); });
  }

  function duplicateRows() {
    var t = st.current, g = st.grids[t], sel = selectedRows(t), all = rowsOf(t);
    if (!sel.length) { window.alert("Select one or more rows (or cells in them) first."); return; }
    if (t === "Measure" && all.length + sel.length > st.maxRows) { window.alert("The editor is for jobs of up to " + st.maxRows + " measures."); return; }
    var taken = new Set(all.map(function (r) { return r.getData().CEInputID; }));
    var copies = sel.map(function (r) {
      var d = Object.assign({}, r.getData());
      delete d._rid;
      if (t === "Measure") { d.CEInputID = uniqueId(d.CEInputID || "NEW", taken); taken.add(d.CEInputID); }
      return withRid(d);
    });
    var at = sel[sel.length - 1];
    copies.reduce(function (p, d) {
      return p.then(function (prev) { return g.addRow(d, false, prev); });
    }, Promise.resolve(at));
  }

  function deleteRows() {
    var t = st.current, sel = selectedRows(t);
    if (!sel.length) { window.alert("Select one or more rows (or cells in them) first."); return; }
    if (!window.confirm("Delete " + sel.length + " row(s) from " + t + "?")) return;
    sel.forEach(function (r) { r.delete(); });
  }

  document.querySelectorAll(".tab[data-table]").forEach(function (b) {
    b.addEventListener("click", function () { show(b.dataset.table); });
  });
  document.querySelectorAll("[data-action]").forEach(function (b) {
    b.addEventListener("click", function () {
      if (!st.layout) return;
      var a = b.dataset.action, g = st.grids[st.current];
      if (a === "add") addRow();
      else if (a === "duplicate") duplicateRows();
      else if (a === "delete") deleteRows();
      else if (a === "undo") g.undo();
      else if (a === "redo") g.redo();
    });
  });

  // ------------------------------------------------------------------------------------------------------------ Validate inputs
  // Saves the draft, queues a preflight-only run of it (POST API/check), then polls the run's /check until it ends, lists what the check found and marks
  // the cells it names. The report names rows of the checked files; chk.info.row_map turns those into grid positions at the checked revision, and chk.snap
  // (the rows' _rids at that revision, taken when the check started or when the page loaded that revision) into rows, so the marks follow later edits.
  var chk = { info: null, timer: null, started: 0, busy: false, snap: null, snapRun: null, report: null, reportCounts: null };
  var chkBtn = document.getElementById("check-btn"), chkAcc = document.getElementById("check-acc"), chkSw = document.getElementById("check-sw");
  var chkFiling = document.getElementById("check-filing");
  var chkOut = document.getElementById("check-result");

  function checkDefaults(d) {
    var def = d.check_defaults || { acc_versions: [], acc_version: "" };
    var c = d.check || {};
    chkAcc.textContent = "";
    def.acc_versions.forEach(function (a) { var o = el("option", null, a); o.value = a; chkAcc.appendChild(o); });
    chkAcc.value = c.acc_version || def.acc_version;
    chkSw.checked = c.run ? !!c.allocate_statewide : !!def.allocate_statewide;
    chkFiling.value = (c.run ? c.filing_validation : def.filing_validation) || "";
    chkBtn.disabled = false;
    if (c.run) {
      chk.info = c;
      if (c.rev === d.rev) { chk.snap = ridSnapshot(); chk.snapRun = c.run; }   // the page holds the checked revision: its rows are the checked rows
      poll(true);
    }
  }

  function ridSnapshot() {
    var snap = {};
    TABLES.forEach(function (t) { snap[t] = rowsOf(t).map(function (r) { return r.getData()._rid; }); });
    return snap;
  }

  // marks the cells the report names (rep null: clears them); each issue gets ._at, its cells as [table, rid, column]
  function setCheckMarks(rep) {
    var touched = {};
    TABLES.forEach(function (t) { touched[t] = new Set(Object.keys(st.checkMarks[t])); });
    st.checkMarks = { Measure: {}, ProgramCost: {} };
    var rids = rep && chk.info && chk.snap && chk.snapRun === chk.info.run ? chk.snap : null;
    if (rep) rep.issues.forEach(function (i) {
      i._at = [];
      if (!rids) return;
      (i.cells || []).forEach(function (c) {
        var t = c[0], map = (chk.info.row_map || {})[t], pos = map ? map[c[1]] : c[1];
        var rid = rids[t] && rids[t][pos];
        if (rid == null || !st.checkMarks[t]) return;
        var byCol = st.checkMarks[t][rid] = st.checkMarks[t][rid] || {};
        var cur = byCol[c[2]] = byCol[c[2]] || { severity: i.severity, messages: [] };
        if (RANK[i.severity] > RANK[cur.severity]) cur.severity = i.severity;
        cur.messages.push("Validate inputs: " + i.message);
        touched[t].add(String(rid));
        i._at.push([t, rid, c[2]]);
      });
    });
    remark(touched);
  }

  function clearCheckMark(t, rid, fields) {
    var byCol = st.checkMarks[t][rid];
    if (!byCol) return;
    fields.forEach(function (f) { delete byCol[f]; });
    if (!Object.keys(byCol).length) delete st.checkMarks[t][rid];
    var touched = {};
    touched[t] = new Set([String(rid)]);
    remark(touched);
  }

  function checkHead(text, kind) {
    var head = el("div", "check-head");
    head.appendChild(el("span", "check-title " + (kind || ""), text));
    if (chk.info && chk.info.run_url) {
      var a = el("a", "small", "Run page and log");
      a.href = chk.info.run_url;
      head.appendChild(a);
    }
    return head;
  }

  function staleNote() {
    var old = chkOut.querySelector(".check-stale");
    if (old) old.remove();
    var stale = !!(chk.info && chk.info.rev != null && chk.info.rev !== st.rev);
    chkOut.classList.toggle("stale", stale);
    renderSummary();
    if (!stale) return;
    chkOut.insertBefore(el("p", "check-stale", "Out of date: these results are for the tables before your latest edits. Validate again to check the current version."),
                        chkOut.children[1] || null);
  }

  function exampleTarget(v) {
    var hit = null;
    rowsOf("Measure").some(function (r) { if (r.getData().CEInputID === v) { hit = ["Measure", r.getData()._rid, "CEInputID"]; } return hit; });
    if (!hit) rowsOf("ProgramCost").some(function (r) { if (r.getData().PrgID === v) { hit = ["ProgramCost", r.getData()._rid, "PrgID"]; } return hit; });
    if (!hit) rowsOf("Measure").some(function (r) { if (r.getData().PrgID === v) { hit = ["Measure", r.getData()._rid, "PrgID"]; } return hit; });
    return hit;
  }

  function renderCheck(j) {
    chkOut.hidden = false;
    chkOut.textContent = "";
    if (j.status === "queued" || j.status === "running") {
      var wait = j.status === "running" ? "Checking the inputs…"
        : (j.position && j.position > 1 ? "Waiting to start (" + (j.position - 1) + " ahead in the queue)…" : "Waiting for the run in progress to finish…");
      chkOut.appendChild(checkHead(wait, "muted"));
      return;
    }
    var rep = j.report;
    setCheckMarks(rep);
    chk.report = rep;
    chk.reportCounts = null;
    if (!rep) {
      chkOut.appendChild(checkHead(j.status === "cancelled" ? "The check was stopped." : "The check could not run.", "bad"));
      if (j.message) chkOut.appendChild(el("p", "small", j.message));
      if (j.log_tail) chkOut.appendChild(el("pre", "check-log", j.log_tail));
      staleNote();
      return;
    }
    var n = { error: 0, warning: 0, info: 0 };
    rep.issues.forEach(function (i) { n[i.severity] = (n[i.severity] || 0) + 1; });
    chk.reportCounts = n;
    // what this check found, never a verdict on the run: the line above combines it with the cell checks
    var title = "Validate inputs, " + j.acc_version + " ACC" + (j.created_at ? ", " + new Date(j.created_at * 1000).toLocaleTimeString() : "") + ": "
      + (n.error ? n.error + (n.error === 1 ? " problem stops" : " problems stop") + " the run"
        : (n.warning ? n.warning + (n.warning === 1 ? " warning" : " warnings") + " to look at" : "no problems found"));
    chkOut.appendChild(checkHead(title, n.error ? "bad" : (n.warning ? "warn" : "ok")));
    var stats = Object.keys(rep.stats || {}).map(function (k) { return k + ": " + rep.stats[k]; });
    if (stats.length) chkOut.appendChild(el("p", "check-stats", stats.join(" · ")));
    if (rep.issues.length) {
      var ul = el("ul", "check-issues"), rank = { error: 0, warning: 1, info: 2 };
      rep.issues.slice().sort(function (a, b) { return rank[a.severity] - rank[b.severity]; }).forEach(function (i) {
        var li = el("li", i.severity);
        li.appendChild(el("span", "sev", i.severity === "error" ? "stops run" : i.severity));
        li.appendChild(document.createTextNode(i.message + (i.count ? " (" + i.count.toLocaleString() + ")" : "") + " "));
        li.appendChild(el("span", "code", i.code));
        if (i._at.length) {                                              // steps through the cells it marked
          var k = 0, show = el("button", "link check-show", "Show in table" + (i._at.length > 1 ? " (" + i._at.length + " cells)" : ""));
          show.type = "button";
          show.addEventListener("click", function () {
            var live = i._at.filter(function (a) { return st.grids[a[0]].getRow(a[1]); });
            if (!live.length) return;
            var a = live[k % live.length];
            if (live.length > 1) show.textContent = "Show next (" + (k % live.length + 1) + " of " + live.length + ")";
            k++;
            goTo(a[0], a[1], a[2]);
          });
          li.appendChild(document.createTextNode(" "));
          li.appendChild(show);
        }
        if (i.examples && i.examples.length) {
          var eg = el("span", "eg", " e.g. ");
          i.examples.forEach(function (v, k) {
            if (k) eg.appendChild(document.createTextNode(", "));
            var target = exampleTarget(v);
            if (target) {
              var b = el("button", "link", v);
              b.type = "button";
              b.title = "Show this row";
              b.addEventListener("click", function () { goTo(target[0], target[1], target[2]); });
              eg.appendChild(b);
            } else {
              eg.appendChild(document.createTextNode(v));
            }
          });
          li.appendChild(eg);
        }
        ul.appendChild(li);
      });
      chkOut.appendChild(ul);
    }
    staleNote();
  }

  function poll(first) {
    clearTimeout(chk.timer);
    if (!chk.info) return;
    if (first) chk.started = Date.now();
    fetch(chk.info.status_url, { credentials: "same-origin", headers: { Accept: "application/json" } })
      .then(function (r) { if (!r.ok) throw new Error("status " + r.status); return r.json(); })
      .then(function (j) {
        renderCheck(j);
        if (j.status === "queued" || j.status === "running") {
          chk.timer = setTimeout(poll, Date.now() - chk.started > 60000 ? 4000 : 1500);
        } else {
          chk.busy = false;
          chkBtn.disabled = false;
        }
      })
      .catch(function () {
        if (Date.now() - chk.started < 600000) chk.timer = setTimeout(poll, 5000);   // a dropped connection: keep trying for a while
        else { chk.busy = false; chkBtn.disabled = false; }
      });
  }

  function runCheck() {
    if (chk.busy || !st.loaded) return;
    chk.busy = true;
    chkBtn.disabled = true;
    chkOut.hidden = false;
    chkOut.textContent = "";
    chkOut.appendChild(checkHead("Saving and starting the check…", "muted"));
    setCheckMarks(null);
    chk.report = null;
    chk.reportCounts = null;
    renderSummary();
    var ready = (st.dirty || st.saving) ? Promise.resolve(save()) : Promise.resolve(true), snap = null;
    ready.then(function (ok) {
      if (!ok) throw new Error("the draft could not be saved (see the message at the top)");
      snap = ridSnapshot();                                // the rows as saved: the check reads this revision
      return fetch(API + "/check", {
        method: "POST", credentials: "same-origin",
        headers: { "Content-Type": "application/json", "X-CSRF-Token": CSRF },
        body: JSON.stringify({ rev: st.rev, acc_version: chkAcc.value, allocate_statewide: chkSw.checked, filing_validation: chkFiling.value })
      });
    }).then(function (r) {
      return r.json().catch(function () { return {}; }).then(function (j) { return { ok: r.ok, status: r.status, j: j }; });
    }).then(function (res) {
      if (!res.ok) {
        chk.busy = false;
        chkBtn.disabled = false;
        chkOut.textContent = "";
        chk.info = null;
        chkOut.appendChild(checkHead(res.status === 401 ? "Signed out: reload the page and sign in again." : "The check did not start.", "bad"));
        chkOut.appendChild(el("p", "small", res.j.error || res.j.detail || ("Error " + res.status)));
        return;
      }
      chk.info = res.j;
      chk.snap = snap;
      chk.snapRun = res.j.run;
      poll(true);
    }).catch(function (e) {
      chk.busy = false;
      chkBtn.disabled = false;
      chkOut.textContent = "";
      chkOut.appendChild(checkHead("The check did not start: " + e.message, "bad"));
    });
  }
  chkBtn.addEventListener("click", runCheck);

  // Run: save first, then open the New run page for this draft. A draft with a cell problem that would stop the run is refused by the New run page,
  // so say so here and show the first such cell instead.
  document.getElementById("run-link").addEventListener("click", function (e) {
    if (st.cellCounts && st.cellCounts.error && st.firstError) {
      e.preventDefault();
      var sum = document.getElementById("issue-summary");
      sum.textContent = "Fix the " + (st.cellCounts.error === 1 ? "problem" : st.cellCounts.error + " problems") + " outlined in red first: the run would be refused.";
      sum.className = "issue-line error";
      goTo(st.firstError.table, st.firstError.rid, st.firstError.column);
      return;
    }
    if (!st.dirty && !st.saving) return;
    e.preventDefault();
    Promise.resolve(save()).then(function (ok) { if (ok) window.location.href = RUN; });
  });
  window.addEventListener("beforeunload", function (e) {
    if (st.dirty || st.saving) { e.preventDefault(); e.returnValue = ""; }
  });

  // ------------------------------------------------------------------------------------------------------------ load
  fetch(API, { credentials: "same-origin", headers: { Accept: "application/json" } })
    .then(function (r) { return r.json().then(function (j) { return { ok: r.ok, j: j }; }); })
    .then(function (res) {
      if (!res.ok) { setStatus(res.j.error || "Could not load the draft.", "bad"); return; }
      var d = res.j;
      st.rev = d.rev;
      st.layout = d.layout;
      st.choices = d.choices || {};
      st.maxRows = d.max_rows;
      st.newMeasure = d.new_measure;
      st.pcLike = d.new_programcost_like || [];
      TABLES.forEach(function (t) { st.columns[t] = d.tables[t].columns; buildGrid(t, d.tables[t]); });
      document.getElementById("profiles-note").textContent = d.profiles_note ? "End uses: " + d.profiles_note + "." : "";
      var ready = 0;
      TABLES.forEach(function (t) {
        st.grids[t].on("tableBuilt", function () {
          if (++ready < TABLES.length) return;
          var snap = {};
          TABLES.forEach(function (x) { snap[x] = rowsOf(x).map(function (r) { return r.getData()._rid; }); });
          applyIssues(d.issues || [], snap);
          updateCounts();
          fillColumnNames();
          st.dirty = false;
          st.loaded = true;
          setStatus("Saved", "ok");
          checkDefaults(d);
        });
      });
    })
    .catch(function (e) { setStatus("Could not load the draft: " + e.message, "bad"); });
})();
