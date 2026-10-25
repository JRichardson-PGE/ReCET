// ReCET web: small page behaviours (no framework). The Edit inputs page has its own edit.js.
(function () {
  "use strict";

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
      document.querySelectorAll(".mode-workbook").forEach(function (x) { x.classList.toggle("hidden", v !== "workbook"); });
      document.querySelectorAll(".mode-files").forEach(function (x) { x.classList.toggle("hidden", v !== "files"); });
    };
    mode.addEventListener("change", sync);
    sync();
  }
  var runForm = document.getElementById("run-form");
  if (runForm) runForm.addEventListener("submit", function () {
    var b = runForm.querySelector("button.primary");
    if (b) { b.disabled = true; b.textContent = "Uploading…"; }
  });

  // Results: selects submit their form; the column picker sends the ticked columns.
  document.querySelectorAll("select.autosubmit").forEach(function (s) {
    s.addEventListener("change", function () {
      if (s.name === "view") { var c = s.form.querySelector("input[name=columns]"); if (c) c.remove(); }
      s.form.submit();
    });
  });
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
          var st = document.getElementById("run-status");
          st.textContent = d.status_text;
          st.className = "status big s-" + d.status;
          var pos = document.getElementById("run-position");
          pos.textContent = d.position ? "Waiting in the queue: number " + d.position + "." : "";
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
          if (live) { window.location.reload(); }
        })
        .catch(function () { setTimeout(poll, 5000); });
    };
    poll();
  }
  // Runs list: while a run is queued or running, reload every 10 s to move its progress bar and status.
  var runsTable = document.getElementById("runs-table");
  if (runsTable && runsTable.dataset.live === "1") { setTimeout(function () { window.location.reload(); }, 10000); }

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
      prev.disabled = pageNo === 0;
      next.disabled = pageNo >= pages - 1;
    };
    var restart = function () { pageNo = 0; apply(); };
    find.addEventListener("input", restart);
    showRetired.addEventListener("change", restart);
    size.addEventListener("change", restart);
    prev.addEventListener("click", function () { pageNo -= 1; apply(); vlTable.scrollIntoView({ block: "start" }); });
    next.addEventListener("click", function () { pageNo += 1; apply(); vlTable.scrollIntoView({ block: "start" }); });
    apply();
  }
})();
