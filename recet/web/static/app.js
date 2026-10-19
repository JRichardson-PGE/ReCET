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
          if (more) { setTimeout(poll, 50); return; }
          if (d.status === "queued" || d.status === "running") { setTimeout(poll, 1500); return; }
          if (live) { window.location.reload(); }
        })
        .catch(function () { setTimeout(poll, 5000); });
    };
    poll();
  }
  // Docs > Value lists: find box and the "Show retired codes" switch.
  var vlTable = document.getElementById("vl-table");
  if (vlTable) {
    var find = document.getElementById("vl-filter"), showRetired = document.getElementById("vl-retired"), count = document.getElementById("vl-count");
    var rows = Array.prototype.slice.call(vlTable.tBodies[0].rows);
    var apply = function () {
      var q = find.value.trim().toLowerCase(), n = 0;
      rows.forEach(function (r) {
        var show = (showRetired.checked || r.dataset.retired !== "1") && (!q || r.textContent.toLowerCase().indexOf(q) >= 0);
        r.classList.toggle("hidden", !show);
        if (show) n += 1;
      });
      count.textContent = n.toLocaleString() + " of " + rows.length.toLocaleString() + " codes shown";
    };
    find.addEventListener("input", apply);
    showRetired.addEventListener("change", apply);
    apply();
  }
})();
