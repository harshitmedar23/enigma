(function () {
  "use strict";
  const TOKEN_KEY = "morrow_token";
  const USER_KEY = "morrow_user";

  window.Morrow = {
    token: () => localStorage.getItem(TOKEN_KEY),
    user: () => { try { return JSON.parse(localStorage.getItem(USER_KEY)); } catch (_) { return null; } },
    saveAuth(data) {
      if (data.token) localStorage.setItem(TOKEN_KEY, data.token);
      if (data.user) localStorage.setItem(USER_KEY, JSON.stringify(data.user));
    },
    logout() { localStorage.removeItem(TOKEN_KEY); localStorage.removeItem(USER_KEY); location.href = "/login.html"; },
    async api(path, options = {}) {
      const headers = Object.assign({ "Accept": "application/json" }, options.headers || {});
      if (this.token()) headers.Authorization = "Bearer " + this.token();
      if (options.body && !(options.body instanceof FormData) && !headers["Content-Type"]) headers["Content-Type"] = "application/json";
      const response = await fetch(path, Object.assign({}, options, { headers }));
      const text = await response.text();
      let data = null;
      try { data = text ? JSON.parse(text) : null; } catch (_) { data = text ? { detail: text } : null; }
      if (!response.ok) throw new Error((data && (data.detail || data.error || data.message)) || "Request failed (" + response.status + ")");
      return data;
    },
    escape(value) {
      const div = document.createElement("div");
      div.textContent = value == null ? "" : String(value);
      return div.innerHTML;
    },
    money(value) {
      const n = Number(value);
      return Number.isFinite(n) ? new Intl.NumberFormat("en-US", { style: "currency", currency: "USD" }).format(n) : "$0.00";
    },
    setBusy(button, busy, busyText) {
      if (!button) return;
      if (busy) { button.dataset.label = button.innerHTML; button.disabled = true; button.innerHTML = busyText || "Working…"; }
      else { button.disabled = false; if (button.dataset.label) button.innerHTML = button.dataset.label; }
    },
    requireRole(role) {
      const user = this.user();
      if (!this.token() || !user) { location.replace("/login.html"); return false; }
      if (role && user.role !== role) { location.replace(user.role === "admin" ? "/admin.html" : "/seller.html"); return false; }
      return true;
    },
    renderError(el, message) {
      if (!el) return;
      el.hidden = false;
      el.innerHTML = '<span class="error-dot"></span><span>' + this.escape(message) + "</span>";
    }
  };

  document.addEventListener("DOMContentLoaded", () => {
    document.querySelectorAll("[data-logout]").forEach(button => button.addEventListener("click", () => Morrow.logout()));
    document.querySelectorAll("a[href]").forEach(link => {
      if (link.getAttribute("href") === "#") link.addEventListener("click", event => event.preventDefault());
    });
  });
})();
