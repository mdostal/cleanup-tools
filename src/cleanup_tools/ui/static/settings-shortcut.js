/*
 * Cmd+, (macOS convention) navigates to /settings from anywhere in the
 * app -- matches the gear icon nav link (base.html's #settings-nav-link).
 * Works whether or not this page is running inside the Tauri desktop
 * shell or a plain browser tab; it's just a keyboard shortcut, no
 * Tauri-specific API involved.
 *
 * Deliberately vanilla JS with no build step or dependency, consistent
 * with the rest of this UI's static/*.js files.
 */
(function () {
  "use strict";

  document.addEventListener("keydown", function (evt) {
    if (evt.key !== "," || !(evt.metaKey || evt.ctrlKey)) {
      return;
    }
    var link = document.getElementById("settings-nav-link");
    if (!link) {
      return;
    }
    evt.preventDefault();
    window.location.href = link.getAttribute("href");
  });
})();
