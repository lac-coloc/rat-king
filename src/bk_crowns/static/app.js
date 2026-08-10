(() => {
  "use strict";
  const table = document.querySelector("#ranking-table");
  if (!table) return;
  const body = table.tBodies[0];
  const rows = [...body.rows];
  const search = document.querySelector("#search");
  const tier = document.querySelector("#tier-filter");
  const kind = document.querySelector("#kind-filter");
  const count = document.querySelector("#visible-count");
  const empty = document.querySelector("#empty-result");
  let sortKey = "epc";
  let sortDirection = -1;

  function applyFilters() {
    const needle = search.value.trim().toLocaleLowerCase("fr");
    let visible = 0;
    rows.forEach((row) => {
      const show = (!needle || row.textContent.toLocaleLowerCase("fr").includes(needle)) &&
        (!tier.value || row.dataset.tier === tier.value) &&
        (!kind.value || row.dataset.kind === kind.value);
      row.hidden = !show;
      if (show) visible += 1;
    });
    count.textContent = `${visible} récompense${visible > 1 ? "s" : ""}`;
    empty.hidden = visible !== 0;
  }

  function value(row, key) {
    if (key === "product") return row.dataset.product;
    return Number(row.dataset[key]);
  }

  function sortRows(key) {
    if (sortKey === key) sortDirection *= -1;
    else { sortKey = key; sortDirection = key === "product" ? 1 : -1; }
    rows.sort((left, right) => {
      const a = value(left, sortKey);
      const b = value(right, sortKey);
      return (typeof a === "string" ? a.localeCompare(b, "fr") : a - b) * sortDirection;
    }).forEach((row) => body.append(row));
  }

  [search, tier, kind].forEach((control) => control.addEventListener("input", applyFilters));
  document.querySelectorAll("[data-sort]").forEach((button) => {
    button.addEventListener("click", () => sortRows(button.dataset.sort));
  });

  async function refreshStatus() {
    const statusUrl = document.body.dataset.statusUrl;
    if (!statusUrl) return;
    try {
      const response = await fetch(statusUrl, { cache: "no-store" });
      if (!response.ok) return;
      const status = await response.json();
      const badge = document.querySelector("#freshness");
      const labels = { fresh: "Données fraîches", stale: "Données anciennes", error: "Dernier refresh en erreur" };
      badge.textContent = labels[status.state] || "Initialisation";
      badge.className = `status status--${status.state}`;
    } catch (_) { /* Le snapshot statique reste utilisable hors ligne. */ }
  }

  applyFilters();
  refreshStatus();
  window.setInterval(refreshStatus, 60000);
})();
