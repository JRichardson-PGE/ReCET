// ReCET web: small page behaviours (no framework). The Edit inputs page has its own edit.js.
(function () {
  "use strict";

  // One place to tell a screen reader something that happened without moving the focus (a row count, a check's result). The text is cleared and
  // set again a moment later, so a repeated or quickly replaced message is still spoken; calls within that moment keep only the last.
  var announcer = document.getElementById("announcer"), announceTimer = null;
  window.recetAnnounce = function (text) {
    if (!announcer) return;
    clearTimeout(announceTimer);
    announcer.textContent = "";
    announceTimer = setTimeout(function () { announcer.textContent = text; }, 150);
  };

  // A page that comes back with an error message (a form the server refused): move the focus to the message, so a keyboard or screen-reader user
  // hears it (WCAG 3.3.1). Not the run page's live message, which updates while the page is open.
  var alertBox = document.querySelector("main p.alert[role=alert]:not(.hidden):not(#run-message)");
  if (alertBox) { alertBox.tabIndex = -1; alertBox.focus(); }

  // A live page (run page, runs list) reloads itself when a run ends, to show its files; it waits while the person is typing or has the focus in
  // a control, and says so instead (WCAG 2.4.3: a reload would drop the focus and anything typed).
  var reloadWhenIdle = function (say) {
    var a = document.activeElement;
    if (!a || a === document.body || a.id === "main") { window.location.reload(); return; }
    say("The run ended. Reload the page to see its results and files.");
  };

  // Confirm before destructive form posts.
  document.querySelectorAll("form.confirm").forEach(function (f) {
    f.addEventListener("submit", function (e) { if (!window.confirm(f.dataset.confirm || "Are you sure?")) e.preventDefault(); });
  });

  // Copy buttons (the admin page's setup link).
  document.querySelectorAll("[data-copy]").forEach(function (b) {
    b.addEventListener("click", function () {
      var el = document.getElementById(b.dataset.copy);
      el.select();
      (navigator.clipboard ? navigator.clipboard.writeText(el.value) : Promise.reject()).then(function () { b.textContent = "Copied"; },
        function () { document.execCommand("copy"); b.textContent = "Copied"; });
    });
  });

  // New run: show the inputs of the chosen input type.
  var mode = document.getElementById("input-mode");
  if (mode) {
    var sync = function () {
      var v = (mode.querySelector("input:checked") || {}).value || "workbook";
      ["workbook", "files", "from_run"].forEach(function (m) {
        document.querySelectorAll(".mode-" + m).forEach(function (x) { x.classList.toggle("hidden", v !== m); });
      });
    };
    mode.addEventListener("change", sync);
    sync();
  }
  // New run: "Compare with" cannot be the ACC already chosen; the filing year matters only with filing checks.
  var accSel = document.getElementById("acc-version"), cmpSel = document.getElementById("compare-with");
  if (accSel && cmpSel) {
    var syncCompare = function () {
      Array.prototype.forEach.call(cmpSel.options, function (o) { o.disabled = !!o.value && o.value === accSel.value; o.hidden = o.disabled; });
      if (cmpSel.value === accSel.value) cmpSel.value = "";
    };
    accSel.addEventListener("change", syncCompare);
    syncCompare();
  }
  var filingSel = document.getElementById("filing-validation");
  if (filingSel) {
    var syncFiling = function () {
      document.querySelectorAll(".filing-year").forEach(function (x) { x.classList.toggle("hidden", !filingSel.value); });
    };
    filingSel.addEventListener("change", syncFiling);
    syncFiling();
  }

  // New run: send the form with a visible upload percentage (a job zip can be 150 MB). The server answers with a redirect to the new run's page, or with
  // the New run page again saying what was wrong; that page replaces this one.
  var runForm = document.getElementById("run-form");
  if (runForm) runForm.addEventListener("submit", function (e) {
    var by = e.submitter;
    if (by && by.name) {                    // the button pressed (Start run / Check inputs only) is sent as a field: a disabled button would not be
      var keep = document.createElement("input");
      keep.type = "hidden"; keep.name = by.name; keep.value = by.value;
      runForm.appendChild(keep);
    }
    runForm.querySelectorAll("button").forEach(function (b) { b.disabled = true; });
    if (by) by.textContent = "Uploading…";
    var hasFile = Array.prototype.some.call(runForm.querySelectorAll("input[type=file]"), function (i) { return i.files && i.files.length; });
    if (!hasFile || !window.XMLHttpRequest || !window.FormData) return;          // no file to watch: the browser's own submit
    e.preventDefault();
    var box = document.getElementById("upload-progress"), bar = box.querySelector("progress"), text = box.querySelector(".text");
    box.hidden = false;
    var xhr = new XMLHttpRequest();
    xhr.open("POST", runForm.action);
    xhr.upload.addEventListener("progress", function (p) {
      if (!p.lengthComputable) return;
      bar.value = p.loaded / p.total;
      text.textContent = p.loaded < p.total
        ? "Uploading " + Math.round(100 * p.loaded / p.total) + "% (" + (p.loaded / 1048576).toFixed(0) + " of " + (p.total / 1048576).toFixed(0) + " MB)"
        : "Uploaded; unpacking and queueing the run…";
    });
    xhr.addEventListener("load", function () {
      var done = xhr.responseURL && xhr.responseURL.indexOf("/runs/new") < 0 && xhr.responseURL !== runForm.action;
      if (done && xhr.status < 400) { window.location.href = xhr.responseURL; return; }
      document.open();                       // the New run page again, with the error and the values given
      document.write(xhr.responseText);
      document.close();
    });
    xhr.addEventListener("error", function () {
      text.textContent = "The upload failed (connection lost). Check the connection and try again.";
      runForm.querySelectorAll("button").forEach(function (b) { b.disabled = false; });
      if (by) by.textContent = by.value === "on" ? "Check inputs only" : "Start run";
    });
    xhr.send(new FormData(runForm));
  });

  // Results and Compare: the filters are sent by their Apply button, never on change (WCAG 3.2.2). A different saved view replaces the unsaved columns.
  document.querySelectorAll("select[data-view]").forEach(function (s) {
    var was = s.value;
    s.form.addEventListener("submit", function () {
      if (s.value !== was) { var c = s.form.querySelector("input[name=columns]"); if (c) c.remove(); }
    });
  });
  // Results: the column picker sends the ticked columns.
  var pick = document.getElementById("pick");
  if (pick) {
    var ticked = function () { return Array.prototype.map.call(pick.querySelectorAll(".pick input:checked"), function (x) { return x.value; }); };
    pick.addEventListener("submit", function () { document.getElementById("pick-columns").value = ticked().join(","); });
    var save = document.getElementById("save-view");
    save.addEventListener("submit", function (e) {
      var keys = ticked();
      if (!keys.length) { e.preventDefault(); window.alert("Tick at least one column."); return; }
      var box = document.getElementById("save-keys");
      box.innerHTML = "";
      keys.forEach(function (k) { var i = document.createElement("input"); i.type = "hidden"; i.name = "keys"; i.value = k; box.appendChild(i); });
    });
  }

  // Run page: follow the log while the run is queued or running, then reload once to show the files.
  var log = document.getElementById("log");
  if (log) {
    var offset = 0, live = log.dataset.live === "1";
    var poll = function () {
      fetch(log.dataset.url + "?offset=" + offset, { headers: { "Accept": "application/json" }, credentials: "same-origin" })
        .then(function (r) { if (!r.ok) throw new Error(r.status); return r.json(); })
        .then(function (d) {
          if (d.text) {
            var atEnd = log.scrollTop + log.clientHeight >= log.scrollHeight - 30;
            log.textContent += d.text;
            if (atEnd || offset === 0) log.scrollTop = log.scrollHeight;
          }
          var more = d.offset > offset;
          offset = d.offset;
          var st = document.getElementById("run-status"), say = document.getElementById("run-announce");
          if (say && st.textContent !== d.status_text) say.textContent = "Run status: " + d.status_text;      // announced only when it changes
          st.textContent = d.status_text;
          st.className = "status big s-" + d.status;
          var pos = document.getElementById("run-position");
          pos.textContent = d.position ? "Waiting in the queue: number " + d.position + (d.wait ? "; " + d.wait : "") + "." : "";
          var bar = document.getElementById("run-progress");
          if (bar) {
            var p = d.progress;
            bar.classList.toggle("hidden", !p);
            if (p) {
              bar.querySelector("progress").value = p.fraction;
              bar.querySelector(".pct").textContent = Math.round(p.fraction * 100) + "%";
              bar.querySelector(".text").textContent = p.text || "";
            }
          }
          if (more) { setTimeout(poll, 50); return; }
          if (d.status === "queued" || d.status === "running") { setTimeout(poll, 1500); return; }
          if (live) reloadWhenIdle(function (m) { pos.textContent = m; if (say) say.textContent = m; });
        })
        .catch(function () { setTimeout(poll, 5000); });
    };
    poll();
  }
  // Runs list: while a run is queued or running, update its status, wait and progress cells every few seconds (no page reload, so the page keeps its
  // scroll and selection); reload once when one of them finishes, to show its links and times.
  var runsTable = document.getElementById("runs-table");
  if (runsTable && runsTable.dataset.live === "1") {
    var liveIds = runsTable.dataset.ids;
    var tick = function () {
      fetch(runsTable.dataset.url + "?ids=" + encodeURIComponent(liveIds), { credentials: "same-origin", headers: { Accept: "application/json" } })
        .then(function (r) { if (!r.ok) throw new Error(r.status); return r.json(); })
        .then(function (d) {
          var ended = false;
          Object.keys(d).forEach(function (id) {
            var x = d[id], row = runsTable.querySelector('tr[data-id="' + id + '"]');
            if (!row) return;
            if (x.status !== "queued" && x.status !== "running") ended = true;
            var st = row.querySelector(".status"), say = document.getElementById("runs-announce");
            if (say && st.textContent !== x.status_text) {
              var name = row.cells[1] ? row.cells[1].textContent.trim().split("\n")[0] : "A run";
              say.textContent = name + ": " + x.status_text;
            }
            st.textContent = x.status_text;
            st.className = "status s-" + x.status;
            var wait = row.querySelector(".live-wait"), box = row.querySelector(".progress");
            if (wait) wait.textContent = x.wait || "";
            if (!box) return;
            box.classList.toggle("hidden", !x.progress);
            if (x.progress) {
              box.querySelector("progress").value = x.progress.fraction;
              box.title = x.progress.text;
              var left = x.progress.text.split(" · ");
              box.querySelector(".live-text").textContent = Math.round(x.progress.fraction * 100) + "%" + (left.length > 1 ? " · " + left[left.length - 1] : "");
            }
          });
          if (ended) { reloadWhenIdle(function (m) { var s = document.getElementById("runs-announce"); if (s) s.textContent = m; }); return; }
          setTimeout(tick, 4000);
        })
        .catch(function () { setTimeout(tick, 15000); });
    };
    setTimeout(tick, 4000);
  }

  // Results: sort a table by a column (click its header) and filter its rows by text. Numbers sort by the value in data-v, not the formatted text.
  document.querySelectorAll("table.sortable").forEach(function (table) {
    var body = table.tBodies[0];
    if (!body) return;
    var heads = table.tHead ? table.tHead.rows[0].cells : [];
    Array.prototype.forEach.call(heads, function (th, col) {
      th.classList.add("sort-head");
      th.setAttribute("aria-sort", "none");
      var btn = document.createElement("button");             // the heading's text becomes a button: reachable by Tab and announced as one
      btn.type = "button";
      btn.className = "sort-btn";
      while (th.firstChild) btn.appendChild(th.firstChild);
      th.appendChild(btn);
      var sort = function () {
        var dir = th.getAttribute("aria-sort") === "descending" ? "ascending" : "descending";
        Array.prototype.forEach.call(heads, function (h) { h.setAttribute("aria-sort", "none"); });
        th.setAttribute("aria-sort", dir);
        var rows = Array.prototype.slice.call(body.rows), sign = dir === "ascending" ? 1 : -1;
        var key = function (tr) {
          var td = tr.cells[col];
          if (!td) return "";
          var v = td.dataset.v;
          return v != null && v !== "" && !isNaN(Number(v)) ? Number(v) : td.textContent.trim().toLowerCase();
        };
        rows.sort(function (a, b) {
          var x = key(a), y = key(b);
          if (typeof x === "number" && typeof y === "number") return sign * (x - y);
          if (typeof x === "number") return -1;
          if (typeof y === "number") return 1;
          return sign * (x < y ? -1 : x > y ? 1 : 0);
        });
        rows.forEach(function (tr) { body.appendChild(tr); });
      };
      th.addEventListener("click", sort);
    });
  });
  document.querySelectorAll("input[data-filter]").forEach(function (box) {
    var table = document.getElementById(box.dataset.filter), count = document.getElementById(box.dataset.count);
    if (!table) return;
    var timer = null;
    box.addEventListener("input", function () {
      var q = box.value.trim().toLowerCase(), shown = 0, rows = table.tBodies[0].rows;
      Array.prototype.forEach.call(rows, function (tr) {
        var hit = !q || tr.textContent.toLowerCase().indexOf(q) >= 0;
        tr.hidden = !hit;
        if (hit) shown++;
      });
      var said = q ? shown.toLocaleString() + " of " + rows.length.toLocaleString() + " rows shown" : "";
      if (count) count.textContent = said;
      // spoken once typing pauses, not over each typed letter (NVDA did not read the count as a role=status line beside the box)
      clearTimeout(timer);
      timer = setTimeout(function () { if (said) window.recetAnnounce(said); }, 500);
    });
  });

  // Results: the column picker's search box, groups and Defaults / Clear buttons.
  var pickFind = document.getElementById("pick-find");
  if (pickFind) {
    var labels = document.querySelectorAll(".pick label");
    pickFind.addEventListener("input", function () {
      var q = pickFind.value.trim().toLowerCase();
      labels.forEach(function (l) { l.hidden = !!q && (l.textContent + " " + (l.title || "")).toLowerCase().indexOf(q) < 0; });
      document.querySelectorAll(".pick-group").forEach(function (g) { g.hidden = !g.querySelector("label:not([hidden])"); });
    });
    document.querySelectorAll("[data-pick]").forEach(function (b) {
      b.addEventListener("click", function () {
        var defaults = (b.dataset.defaults || "").split(",");
        document.querySelectorAll(".pick input[type=checkbox]").forEach(function (c) {
          c.checked = b.dataset.pick === "defaults" ? defaults.indexOf(c.value) >= 0 : false;
        });
      });
    });
  }
  // Results: the Columns button opens the picker above the table.
  var colBtn = document.getElementById("columns-btn"), colPanel = document.getElementById("columns");
  if (colBtn && colPanel) colBtn.addEventListener("click", function () {
    colPanel.hidden = !colPanel.hidden;
    colBtn.setAttribute("aria-expanded", colPanel.hidden ? "false" : "true");
    if (!colPanel.hidden && pickFind) pickFind.focus();
  });

  // Docs > Value lists: find box, the "Show retired codes" switch and pages of codes (100 a page by default).
  var vlTable = document.getElementById("vl-table");
  if (vlTable) {
    var find = document.getElementById("vl-filter"), showRetired = document.getElementById("vl-retired"), count = document.getElementById("vl-count");
    var size = document.getElementById("vl-size"), pager = document.getElementById("vl-pager"), pageLabel = document.getElementById("vl-page");
    var prev = document.getElementById("vl-prev"), next = document.getElementById("vl-next");
    var rows = Array.prototype.slice.call(vlTable.tBodies[0].rows), pageNo = 0;
    var nRetired = rows.filter(function (r) { return r.dataset.retired === "1"; }).length;
    var apply = function () {
      var q = find.value.trim().toLowerCase();
      var matches = rows.filter(function (r) {
        return (showRetired.checked || r.dataset.retired !== "1") && (!q || r.textContent.toLowerCase().indexOf(q) >= 0);
      });
      var per = parseInt(size.value, 10) || matches.length || 1, pages = Math.max(1, Math.ceil(matches.length / per));
      pageNo = Math.min(pageNo, pages - 1);
      var first = pageNo * per, onPage = matches.slice(first, first + per);
      rows.forEach(function (r) { r.classList.add("hidden"); });
      onPage.forEach(function (r) { r.classList.remove("hidden"); });
      var hidden = showRetired.checked ? 0 : nRetired;
      count.textContent = matches.length.toLocaleString() + " of " + rows.length.toLocaleString() + " codes match" +
        (hidden ? " (" + hidden.toLocaleString() + " retired " + (hidden === 1 ? "code" : "codes") + " hidden)" : "");
      pager.classList.toggle("hidden", pages < 2);
      pageLabel.textContent = "Page " + (pageNo + 1) + " of " + pages + ", codes " + (matches.length ? first + 1 : 0).toLocaleString() +
        " to " + (first + onPage.length).toLocaleString();
      var had = document.activeElement;
      prev.disabled = pageNo === 0;
      next.disabled = pageNo >= pages - 1;
      if (had === prev && prev.disabled) next.focus();          // a disabled button drops the keyboard focus to the page: hand it to the other
      else if (had === next && next.disabled) prev.focus();
    };
    var restart = function () { pageNo = 0; apply(); };
    find.addEventListener("input", restart);
    showRetired.addEventListener("change", restart);
    size.addEventListener("change", restart);
    prev.addEventListener("click", function () { pageNo -= 1; apply(); vlTable.scrollIntoView({ block: "start" }); });
    next.addEventListener("click", function () { pageNo += 1; apply(); vlTable.scrollIntoView({ block: "start" }); });
    apply();
  }

  // Output templates page: a two-list field picker. The left list shows the fields of the chosen category not yet in the template, filtered by the search
  // box; the right list is the template in file order. The textarea (sent with the form) always holds the right list, one column per line, and typing or
  // pasting there rebuilds the right list.
  var tmplCols = document.getElementById("template-columns");
  var picker = document.getElementById("field-picker");
  var catalogEl = document.getElementById("field-catalog");
  if (tmplCols && picker && catalogEl) {
    var fields = JSON.parse(catalogEl.textContent), byName = {};
    fields.forEach(function (f) { byName[f.c] = f; });
    var avail = document.getElementById("pick-available"), sel = document.getElementById("pick-selected");
    var kindBox = document.getElementById("pick-kind"), find = document.getElementById("pick-find"), findSel = document.getElementById("pick-find-sel");
    var defBox = document.getElementById("pick-def");
    var chosen = [];
    var parse = function (text) {
      var out = [];
      text.split(/\r?\n/).forEach(function (line) {
        line.split("#")[0].split(",").forEach(function (c) { c = c.trim(); if (c && out.indexOf(c) < 0) out.push(c); });
      });
      return out;
    };
    var matches = function (f, q) { return !q || f.c.toLowerCase().indexOf(q) >= 0 || (f.d || "").toLowerCase().indexOf(q) >= 0; };
    var option = function (name) {
      var f = byName[name], o = document.createElement("option");
      o.value = name; o.textContent = name;
      o.title = f ? (f.g + (f.d ? ": " + f.d : "")) : "Not in the field list (a per-component column, or a name ReCET does not write)";
      if (!f && !/^(Elec|Gas)Value_/.test(name)) { o.className = "unknown"; o.textContent = name + " (not in the field list)"; }   // not by colour alone
      return o;
    };
    var renderAvail = function () {
      var k = kindBox.value, q = find.value.trim().toLowerCase(), taken = {}, groups = {}, order = [], shown = 0;
      chosen.forEach(function (c) { taken[c] = 1; });
      avail.innerHTML = "";
      fields.forEach(function (f) {
        if (taken[f.c] || !matches(f, q)) return;
        if (k.slice(0, 2) === "k:" && f.k !== k.slice(2)) return;
        if (k.slice(0, 2) === "g:" && f.g !== k.slice(2)) return;
        if (!groups[f.g]) { groups[f.g] = []; order.push(f.g); }
        groups[f.g].push(f.c);
        shown += 1;
      });
      order.forEach(function (g) {
        var og = document.createElement("optgroup");
        og.label = g;
        groups[g].forEach(function (c) { og.appendChild(option(c)); });
        avail.appendChild(og);
      });
      document.getElementById("pick-count").textContent = shown + " field" + (shown === 1 ? "" : "s") + " shown";
    };
    var renderSel = function (keep, keepText) {
      var q = findSel.value.trim().toLowerCase();
      sel.innerHTML = "";
      chosen.forEach(function (c) {
        var o = option(c);
        if (keep && keep.indexOf(c) >= 0) o.selected = true;
        if (q && c.toLowerCase().indexOf(q) < 0) o.hidden = true;
        sel.appendChild(o);
      });
      document.getElementById("pick-sel-count").textContent = chosen.length + " column" + (chosen.length === 1 ? "" : "s");
      tmplCols.value = chosen.join("\n") + (chosen.length ? "\n" : "");
    };
    var picked = function (list) { return Array.prototype.filter.call(list.options, function (o) { return o.selected && !o.hidden; }).map(function (o) { return o.value; }); };
    var showDef = function (name) {
      var f = byName[name];
      defBox.innerHTML = "";
      var code = document.createElement(f && f.l ? "a" : "strong");
      if (f && f.l) code.href = f.l;
      code.textContent = name;
      defBox.appendChild(code);
      defBox.appendChild(document.createTextNode(f ? " (" + f.g + ") " + (f.d || "") : " is not in the field list."));
    };
    var add = function (names) { names.forEach(function (c) { if (chosen.indexOf(c) < 0) chosen.push(c); }); renderSel(names); renderAvail(); };
    var remove = function (names) { chosen = chosen.filter(function (c) { return names.indexOf(c) < 0; }); renderSel(); renderAvail(); };
    var move = function (how) {
      var names = picked(sel);
      if (!names.length) return;
      var rest = chosen.filter(function (c) { return names.indexOf(c) < 0; });
      if (how === "top") chosen = names.concat(rest);
      else if (how === "bottom") chosen = rest.concat(names);
      else {
        var step = how === "up" ? -1 : 1, idx = names.map(function (c) { return chosen.indexOf(c); });
        if (step > 0) idx.reverse();
        idx.forEach(function (i) {
          var j = i + step;
          if (j < 0 || j >= chosen.length || names.indexOf(chosen[j]) >= 0) return;
          var tmp = chosen[j]; chosen[j] = chosen[i]; chosen[i] = tmp;
        });
      }
      renderSel(names);
      var first = sel.querySelector("option:checked");
      if (first) first.scrollIntoView({ block: "nearest" });
    };
    document.getElementById("pick-add").addEventListener("click", function () { add(picked(avail)); });
    document.getElementById("pick-add-all").addEventListener("click", function () {
      add(Array.prototype.map.call(avail.options, function (o) { return o.value; }));
    });
    document.getElementById("pick-remove").addEventListener("click", function () { remove(picked(sel)); });
    document.getElementById("pick-clear").addEventListener("click", function () { if (!chosen.length || confirm("Remove every column from the template?")) remove(chosen.slice()); });
    ["top", "up", "down", "bottom"].forEach(function (h) { document.getElementById("pick-" + h).addEventListener("click", function () { move(h); }); });
    avail.addEventListener("dblclick", function (e) { if (e.target.tagName === "OPTION") add([e.target.value]); });
    sel.addEventListener("dblclick", function (e) { if (e.target.tagName === "OPTION") remove([e.target.value]); });
    avail.addEventListener("keydown", function (e) { if (e.key === "Enter") { e.preventDefault(); add(picked(avail)); } });
    sel.addEventListener("keydown", function (e) {
      if (e.key === "Delete" || e.key === "Backspace") { e.preventDefault(); remove(picked(sel)); }
      else if (e.altKey && (e.key === "ArrowUp" || e.key === "ArrowDown")) { e.preventDefault(); move(e.key === "ArrowUp" ? "up" : "down"); }
    });
    [avail, sel].forEach(function (list) {
      list.addEventListener("change", function () { var n = picked(list); if (n.length) showDef(n[n.length - 1]); });
    });
    kindBox.addEventListener("change", renderAvail);
    find.addEventListener("input", renderAvail);
    findSel.addEventListener("input", function () { renderSel(picked(sel)); });
    tmplCols.addEventListener("input", function () { chosen = parse(tmplCols.value); renderSel(null, true); renderAvail(); });   // typing: leave the text as typed
    tmplCols.addEventListener("change", function () { renderSel(); });
    chosen = parse(tmplCols.value);
    picker.hidden = false;
    document.getElementById("pick-text-box").open = false;
    renderSel();
    renderAvail();
  }
})();
