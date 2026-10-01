// Live P&L page. Reads a guard's /public/pnl.json (opt-in via public_pnl; CORS: *) and
// renders totals only — the endpoint never exposes entries, notes, or recipients.
//
// Guard URL resolution, in order:
//   1. ?guard=https://... in this page's address (and saved to localStorage)
//   2. the last URL saved in localStorage
//   3. window.CASHMAXX_GUARD_URL, if a deploy bakes one in before this script loads
(function () {
  "use strict";

  var STORAGE_KEY = "cashma…dUrl";
  var REFRESH_MS = 60000;

  function $(id) { return document.getElementById(id); }

  var form = $("guard-form");
  var input = $("guard-url");
  var hint = $("guard-hint");
  var reset = $("guard-reset");
  var stateEmpty = $("state-empty");
  var stateError = $("state-error");
  var stateLoading = $("state-loading");
  var data = $("pnl-data");
  var timer = null;

  function show(name) {
    stateEmpty.hidden = name !== "empty";
    stateError.hidden = name !== "error";
    stateLoading.hidden = name !== "loading";
    data.hidden = name !== "data";
  }

  function saved() {
    try { return localStorage.getItem(STORAGE_KEY) || ""; } catch (e) { return ""; }
  }
  function remember(url) {
    try { localStorage.setItem(STORAGE_KEY, url); } catch (e) { /* private mode */ }
  }
  function forget() {
    try { localStorage.removeItem(STORAGE_KEY); } catch (e) { /* ignore */ }
  }

  function guardUrl() {
    return (input.value || "").trim().replace(/\/+$/, "");
  }

  function money(amount) {
    if (typeof amount !== "string" || amount === "") return "–";
    return amount.charAt(0) === "-" ? "-$" + amount.slice(1) : "$" + amount;
  }
  function text(el, value) { el.textContent = value; }

  function render(doc) {
    var windows = doc.windows || {};
    var total = windows.all || { income: "0", verified_income: "0", costs: "0", net: "0", by_category: {} };

    text($("v-income"), money(total.income));
    text($("v-costs"), money(total.costs));
    var netEl = $("v-net");
    netEl.textContent = money(total.net);
    netEl.className = String(total.net).charAt(0) === "-" ? "neg" : "pos";
    text($("v-verified"), "verified " + money(total.verified_income));

    // net cells get their colour class through the DOM, never string interpolation
    $("window-rows").innerHTML = "";
    ["7d", "30d", "all"].forEach(function (w) {
      var v = windows[w];
      if (!v) return;
      var tr = document.createElement("tr");
      [w === "all" ? "All time" : w, money(v.income), money(v.verified_income), money(v.costs)]
        .forEach(function (cell, i) {
          var td = document.createElement("td");
          if (i > 0) td.className = "num";
          td.textContent = cell;
          tr.appendChild(td);
        });
      var net = document.createElement("td");
      net.className = "num " + (String(v.net).charAt(0) === "-" ? "neg" : "pos");
      net.textContent = money(v.net);
      tr.appendChild(net);
      $("window-rows").appendChild(tr);
    });

    var cats = total.by_category || {};
    var cbody = $("category-rows");
    cbody.innerHTML = "";
    var names = Object.keys(cats).sort();
    if (!names.length) {
      var none = document.createElement("tr");
      none.innerHTML = "<td colspan=2 class=muted>No entries yet.</td>";
      cbody.appendChild(none);
    }
    names.forEach(function (cat) {
      var tr = document.createElement("tr");
      var th = document.createElement("th");
      th.scope = "row";
      th.textContent = cat.replace(/_/g, " ");
      var td = document.createElement("td");
      td.className = "num";
      td.textContent = money(cats[cat]);
      tr.appendChild(th); tr.appendChild(td);
      cbody.appendChild(tr);
    });

    var meta = [];
    if (doc.network) meta.push(doc.network);
    meta.push(doc.frozen ? "frozen" : "active");
    if (doc.generated_at) meta.push("ledger at " + doc.generated_at.replace("T", " ").replace(/\+\d\d:\d\d$/, " UTC"));
    text($("pnl-meta"), meta.join(" · "));

    var wallet = $("pnl-wallet");
    wallet.innerHTML = "";
    if (doc.address) {
      wallet.append("Wallet: ");
      var addr = String(doc.address);
      if (doc.basescan_url) {
        var a = document.createElement("a");
        a.href = doc.basescan_url;
        a.rel = "noopener";
        a.textContent = addr.slice(0, 6) + "…" + addr.slice(-4);
        wallet.appendChild(a);
      } else {
        wallet.append(addr);
      }
    }
    text($("pnl-refresh"), "Auto-refreshes every 60 seconds, straight from the guard at " +
      guardUrl() + ".");
    show("data");
  }

  function fail(title, body) {
    if (title) text($("error-title"), title);
    if (body) text($("error-body"), body);
    show("error");
  }

  function load() {
    var base = guardUrl();
    if (!base) { show("empty"); return; }
    show("loading");
    var ctrl = new AbortController();
    var timeout = setTimeout(function () { ctrl.abort(); }, 15000);
    fetch(base + "/public/pnl.json", { signal: ctrl.signal, cache: "no-store" })
      .then(function (resp) {
        if (resp.status === 404) {
          return fail("This guard has no public P&L",
            "The guard answered, but /public/pnl.json returned 404 — public_pnl is off on this guard.");
        }
        if (!resp.ok) {
          return fail("The guard returned " + resp.status, "Try again in a moment.");
        }
        return resp.json().then(render);
      })
      .catch(function (err) {
        fail("Couldn't reach the guard",
          "No answer from " + base + ". The guard may be offline, the URL may be wrong, or " +
          "the guard may not allow this page to read it (CORS).");
      })
      .finally(function () { clearTimeout(timeout); });
  }

  function apply(url, persist) {
    input.value = url || "";
    if (persist && url) remember(url);
    reset.hidden = !saved();
    hint.hidden = Boolean(saved() || url);
    if (timer) clearInterval(timer);
    if (guardUrl()) {
      load();
      timer = setInterval(load, REFRESH_MS);
    } else {
      show("empty");
    }
  }

  form.addEventListener("submit", function (ev) {
    ev.preventDefault();
    var url = guardUrl();
    if (!/^https?:\/\//i.test(url)) { input.focus(); return; }
    apply(url, true);
  });
  reset.addEventListener("click", function () { forget(); apply("", false); });
  $("retry").addEventListener("click", load);

  var params = new URLSearchParams(location.search).get("guard");
  apply(params || saved() || window.CASHMAXX_GUARD_URL || "", Boolean(params));
})();
