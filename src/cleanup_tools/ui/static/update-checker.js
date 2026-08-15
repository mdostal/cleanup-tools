// Update-available banner. Only does anything inside the Tauri desktop
// shell (window.__TAURI__ present) -- a plain browser tab running
// `cleanup approve` has no app to update, so this is a silent no-op there.
//
// The actual check happens Rust-side (src-tauri's spawn_update_check_loop:
// once on launch, then every 6h) -- this file never initiates a check
// itself. It only:
//   1. asks for whatever the most recent check already found
//      (get_pending_update), so a page opened after a background check
//      fired still shows the banner, and
//   2. listens for the update-available event a check fires while this
//      page is open.
// Both paths render the same banner; "Update now" is the only thing that
// ever triggers a download/install.
(function () {
  if (!window.__TAURI__) return;

  const banner = document.getElementById("update-banner");
  if (!banner) return;

  const textEl = document.getElementById("update-banner-text");
  const installBtn = document.getElementById("update-banner-install");
  const dismissBtn = document.getElementById("update-banner-dismiss");
  const DISMISSED_KEY = "cleanup-tools-dismissed-update-version";

  function showBanner(info) {
    if (!info) return;
    if (sessionStorage.getItem(DISMISSED_KEY) === info.version) return;

    textEl.textContent = "Update available: v" + info.version + (info.notes ? " -- " + info.notes : "");
    banner.dataset.version = info.version;
    banner.hidden = false;
  }

  dismissBtn.addEventListener("click", () => {
    sessionStorage.setItem(DISMISSED_KEY, banner.dataset.version || "");
    banner.hidden = true;
  });

  installBtn.addEventListener("click", async () => {
    installBtn.disabled = true;
    dismissBtn.disabled = true;
    installBtn.textContent = "Installing…";
    try {
      await window.__TAURI__.core.invoke("download_and_install_update");
      // On success the app restarts itself (see download_and_install_update
      // in lib.rs) -- nothing left to do here.
    } catch (err) {
      installBtn.disabled = false;
      dismissBtn.disabled = false;
      installBtn.textContent = "Update now";
      textEl.textContent = "Update failed: " + err;
    }
  });

  window.__TAURI__.core
    .invoke("get_pending_update")
    .then(showBanner)
    .catch(() => {});

  window.__TAURI__.event.listen("update-available", (event) => showBanner(event.payload));
})();
