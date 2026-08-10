(() => {
  "use strict";
  const statusUrl = document.body.dataset.statusUrl;
  const state = document.querySelector("#waiting-state");
  if (!statusUrl || !state) return;

  async function poll() {
    try {
      const response = await fetch(statusUrl, { cache: "no-store" });
      if (response.ok) {
        const payload = await response.json();
        if (payload.ready) {
          window.location.reload();
          return;
        }
        const labels = {
          queued: "Le restaurant est en file d’attente.",
          refreshing: "Le catalogue public est en cours de traitement.",
          backoff: "Nouvelle tentative différée après une erreur publique.",
          rate_limited: "Quota horaire atteint, aucun nouvel appel n’est envoyé.",
        };
        state.textContent = labels[payload.state] || "En attente d’un snapshot valide.";
      }
    } catch (_) {
      state.textContent = "Le serveur reste actif ; nouvel essai dans quelques secondes.";
    }
    window.setTimeout(poll, 2000);
  }

  window.setTimeout(poll, 1000);
})();
