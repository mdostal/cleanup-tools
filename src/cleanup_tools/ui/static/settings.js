// Settings page's icon picker. Loaded globally on every page (matching
// theme.js/plan-trigger.js) -- a defensive no-op anywhere #icon-choice-grid
// isn't present.
(function () {
  const grid = document.getElementById("icon-choice-grid");
  if (!grid) return;

  const statusEl = document.getElementById("icon-choice-status");
  const endpoint = grid.dataset.endpoint;

  function setStatus(text) {
    if (statusEl) statusEl.textContent = text;
  }

  function markSelected(card) {
    grid.querySelectorAll(".icon-choice-card").forEach((el) => {
      el.classList.toggle("selected", el === card);
    });
    grid.querySelectorAll(".icon-choice-current").forEach((el) => el.remove());
    const marker = document.createElement("span");
    marker.className = "icon-choice-current";
    marker.textContent = "Current";
    card.appendChild(marker);
  }

  grid.addEventListener("click", async (event) => {
    const card = event.target.closest(".icon-choice-card");
    if (!card || !grid.contains(card)) return;
    const choice = card.dataset.iconChoice;
    if (!choice) return;

    setStatus("Saving…");
    let response;
    let payload;
    try {
      response = await fetch(endpoint, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ choice: choice }),
      });
      payload = await response.json();
    } catch (err) {
      setStatus("Could not save icon choice: " + err);
      return;
    }

    if (!response.ok) {
      setStatus((payload && payload.error) || "Could not save icon choice.");
      return;
    }

    markSelected(card);

    // Live icon update only when actually running inside the Tauri
    // desktop shell -- this same page also serves the plain-browser
    // `cleanup approve` case, where window.__TAURI__ never exists and
    // there is no window/Dock icon of ours to update right now (the
    // saved choice above still applies next time the desktop app
    // launches).
    if (window.__TAURI__) {
      try {
        await window.__TAURI__.core.invoke("apply_icon_choice", { choice: choice });
        setStatus("Icon updated.");
      } catch (err) {
        setStatus("Saved, but couldn't update the live icon: " + err);
      }
    } else {
      setStatus("Saved. Applies next time the desktop app launches.");
    }
  });
})();
