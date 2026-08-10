(() => {
  "use strict";
  const container = document.querySelector("[data-search-url]");
  const input = document.querySelector("#restaurant-search");
  const results = document.querySelector("#restaurant-results");
  const state = document.querySelector("#search-state");
  if (!container || !input || !results || !state) return;

  let timer = 0;
  let controller = null;

  function clearResults() {
    while (results.firstChild) results.firstChild.remove();
  }

  function appendResult(restaurant) {
    const item = document.createElement("li");
    const link = document.createElement("a");
    const title = document.createElement("strong");
    const detail = document.createElement("span");
    link.href = restaurant.url;
    title.textContent = restaurant.name;
    detail.textContent = `${restaurant.postal_code} ${restaurant.city} · ${restaurant.address}`;
    link.append(title, detail);
    item.append(link);
    results.append(item);
  }

  async function search() {
    const query = input.value.trim();
    controller?.abort();
    clearResults();
    if (query.length < 2) {
      state.textContent = "Saisissez au moins deux caractères.";
      return;
    }
    controller = new AbortController();
    const parameters = new URLSearchParams({ q: query });
    try {
      const response = await fetch(`${container.dataset.searchUrl}?${parameters}`, {
        cache: "no-store",
        headers: { Accept: "application/json" },
        signal: controller.signal,
      });
      if (!response.ok) throw new Error("search unavailable");
      const payload = await response.json();
      payload.restaurants.forEach(appendResult);
      state.textContent = payload.restaurants.length
        ? `${payload.restaurants.length} restaurant(s) proposé(s).`
        : "Aucun restaurant correspondant.";
    } catch (error) {
      if (error.name !== "AbortError") state.textContent = "Recherche momentanément indisponible.";
    }
  }

  input.addEventListener("input", () => {
    window.clearTimeout(timer);
    timer = window.setTimeout(search, 250);
  });
})();
