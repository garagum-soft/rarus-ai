// The account gateway's strip on an engine's page: who is signed in, the balance, and the way out.
(function () {
  "use strict";
  if (window.__ravenHubBar) { return; }
  window.__ravenHubBar = true;
  var ru = (navigator.language || "").toLowerCase().indexOf("ru") === 0;
  var W = ru
    ? { credits: "кр.", account: "Аккаунт", admin: "Админка", signout: "Выйти", blocked: "Новая работа приостановлена" }
    : { credits: "cr", account: "Account", admin: "Admin", signout: "Sign out", blocked: "New work is paused" };

  var root = document.createElement("div");
  root.setAttribute("data-raven-hub", "");
  root.style.cssText = [
    "position:fixed", "top:0", "left:50%", "transform:translateX(-50%)", "z-index:2147483000",
    "font:12px/1.2 system-ui,-apple-system,'Segoe UI',sans-serif", "display:flex", "gap:8px", "align-items:center",
    "padding:3px 10px", "border-radius:0 0 8px 8px", "background:rgba(30,30,28,.82)", "color:#f2f2ee",
    "backdrop-filter:blur(4px)", "user-select:none"
  ].join(";");
  var label = document.createElement("span");
  var menu = document.createElement("span");
  menu.style.cssText = "display:none;gap:10px";
  function link(text, href) {
    var a = document.createElement("a");
    a.textContent = text; a.href = href; a.style.cssText = "color:#bcd6f0;text-decoration:none";
    return a;
  }
  var adminLink = link(W.admin, "/_hub/admin");
  var out = link(W.signout, "#");
  out.onclick = function (event) {
    event.preventDefault();
    fetch("/_hub/api/logout", { method: "POST", credentials: "same-origin", headers: { "Content-Type": "application/json" }, body: "{}" })
      .then(function () { window.location.href = "/_hub/login"; });
  };
  menu.appendChild(link(W.account, "/_hub/account"));
  menu.appendChild(adminLink);
  menu.appendChild(out);
  root.appendChild(label);
  root.appendChild(menu);
  root.onclick = function (event) {
    if (event.target.tagName === "A") { return; }
    menu.style.display = menu.style.display === "none" ? "inline-flex" : "none";
  };

  function refresh() {
    fetch("/_hub/api/me", { credentials: "same-origin" }).then(function (r) { return r.ok ? r.json() : null; }).then(function (data) {
      if (!data) { return; }
      var me = data.me;
      var balance = Math.round(Number(me.balance || 0) * 100) / 100;
      label.textContent = (me.display_name || me.username) + " · " + balance.toLocaleString() + " " + W.credits;
      adminLink.style.display = me.role === "superadmin" || me.role === "admin" ? "" : "none";
      root.style.background = data.blocked ? "rgba(150,40,30,.9)" : "rgba(30,30,28,.82)";
      root.title = data.blocked ? W.blocked + ": " + data.blocked : "";
    }).catch(function () {});
  }
  function mount() { document.body.appendChild(root); refresh(); setInterval(refresh, 60000); }
  if (document.body) { mount(); } else { document.addEventListener("DOMContentLoaded", mount); }
})();
