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
  var WIDTHS = { CEInputID: 120, MeasCode: 100, MeasDescription: 230, PrgID: 180, E3MeaElecEndUseShape: 240, E3GasSavProfile: 200 };
  var st = {
    rev: null, layout: null, columns: {}, choices: {}, issues: { Measure: {}, ProgramCost: {} }, grids: {}, current: "Measure",
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
  function formatter(t, col, numeric) {
    return function (cell) {
      var e = cell.getElement();
      var issue = (st.issues[t][cell.getRow().getData()._rid] || {})[col];
      e.classList.remove("issue-error", "issue-warning", "issue-info");
      if (issue) {
        e.classList.add("issue-" + issue.severity);
        e.title = issue.messages.join("\n");
      } else {
        e.removeAttribute("title");
      }
      var v = cell.getValue();
      return el("span", null, numeric ? displayNumber(v) : (v == null ? "" : String(v)));
    };
  }

  function endUseLookup(cell) {
    var d = cell.getRow().getData();
    var iou = d.IOU_AC_Territory || "", sector = d.E3TargetSector || "", key = iou + "|" + sector;
    if (st.endUses[key]) return st.endUses[key];
    return fetch(END_USES + "?iou=" + encodeURIComponent(iou) + "&sector=" + encodeURIComponent(sector), { credentials: "same-origin" })
      .then(function (r) { return r.json(); })
      .then(function (j) { st.endUses[key] = j.values || [""]; return st.endUses[key]; })
      .catch(function () { return [""]; });
  }

  function columnDefs(t) {
    var L = st.layout[t], noEffect = new Set(L.no_effect), numeric = new Set(L.numeric);
    return L.pinned.concat(L.scroll).map(function (col) {
      var d = {
        title: col, field: col, frozen: L.pinned.indexOf(col) >= 0, cssClass: noEffect.has(col) ? "noeff" : "calc",
        headerTooltip: L.tooltips[col] || "", formatter: formatter(t, col, numeric.has(col)), hozAlign: numeric.has(col) ? "right" : "left",
        width: WIDTHS[col] || Math.min(240, Math.max(84, col.length * 7 + 26)), editor: "input"
      };
      if (t === "Measure" && col === "E3MeaElecEndUseShape") {
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
    g.on("cellClick", function (_e, cell) { showHelp(t, cell.getField()); });
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
    TABLES.forEach(function (t) {
      var live = new Map(rowsOf(t).map(function (r) { return [String(r.getData()._rid), r]; }));   // rows deleted since the save have no row to redraw
      touched[t].forEach(function (rid) {
        var row = live.get(rid);
        if (row) row.reformat();
      });
    });
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
    if (n.error) {
      sum.textContent = n.error + " problem" + (n.error > 1 ? "s" : "") + " would stop the run" + (n.warning ? "; " + n.warning + " to check" : "") + ".";
      sum.className = "issue-line error";
    } else if (n.warning) {
      sum.textContent = "Ready to run. " + n.warning + " cell" + (n.warning > 1 ? "s" : "") + " to check (the run goes ahead, but CET would reject the value or it is not priced).";
      sum.className = "issue-line warning";
    } else {
      sum.textContent = "No problems found.";
      sum.className = "issue-line";
    }
    list.slice(0, 400).forEach(function (i) {
      var li = el("li", i.severity, i.table + " row " + pos[i.table][i.rid] + ", " + i.column + ": " + i.message);
      li.addEventListener("click", function () { goTo(i.table, i.rid, i.column); });
      ul.appendChild(li);
    });
    if (list.length > 400) ul.appendChild(el("li", "info", "… and " + (list.length - 400) + " more"));
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
      document.querySelector('.tab[data-table="' + x + '"]').classList.toggle("active", x === t);
    });
    st.grids[t].redraw(true);
  }

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

  // Run: save first, then open the New run page for this draft
  document.getElementById("run-link").addEventListener("click", function (e) {
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
          st.dirty = false;
          st.loaded = true;
          setStatus("Saved", "ok");
        });
      });
    })
    .catch(function (e) { setStatus("Could not load the draft: " + e.message, "bad"); });
})();
