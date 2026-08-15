/*
 * Settings page's sidebar-of-sections shell (settings.html). Every pane is
 * fully server-rendered in the SAME response -- this file only toggles
 * which `.settings-pane` is visible, it never fetches anything. That
 * keeps every CRUD form's plain POST-then-redirect flow (add/edit/remove/
 * reorder bucket rules, search roots, master paths) working exactly like
 * every other form in this app: a real page load, no client-side state to
 * keep in sync with the server. The redirect target for each of those
 * forms carries the right `#section` hash back (see routes.py), so
 * landing back on the same open pane after a submit is just this file
 * reading the hash on load -- no server-side "which pane was open" state
 * needed either.
 *
 * Deliberately vanilla JS with no build step or dependency, consistent
 * with the rest of this UI's static/*.js files.
 */
(function () {
  "use strict";

  function init() {
    var sidebar = document.getElementById("settings-sidebar");
    if (!sidebar) {
      return;
    }
    var panes = Array.prototype.slice.call(document.querySelectorAll(".settings-pane"));
    var links = Array.prototype.slice.call(sidebar.querySelectorAll("a[data-pane]"));
    if (!panes.length || !links.length) {
      return;
    }

    function show(paneId) {
      var matched = false;
      panes.forEach(function (pane) {
        var isTarget = pane.id === paneId;
        pane.hidden = !isTarget;
        matched = matched || isTarget;
      });
      if (!matched) {
        // Unknown/stale hash -- fall back to the first pane rather than
        // leaving every pane hidden.
        panes[0].hidden = false;
        paneId = panes[0].id;
      }
      links.forEach(function (link) {
        var isCurrent = link.dataset.pane === paneId;
        link.classList.toggle("active", isCurrent);
        if (isCurrent) {
          link.setAttribute("aria-current", "true");
        } else {
          link.removeAttribute("aria-current");
        }
      });
    }

    // Sidebar links are plain `href="#pane-id"` anchors -- clicking one is
    // a native, real hash navigation (no preventDefault/JS needed for the
    // click itself), which fires `hashchange` just like typing a #hash
    // URL directly or landing on one from a CRUD form's redirect. Routing
    // every "which pane is active" update through that single event
    // (rather than duplicating the same logic in a click handler) means a
    // fragment-only navigation -- e.g. Back/Forward between two panes, or
    // jumping straight to a #hash URL -- is handled the exact same way a
    // click is, with no separate code path to keep in sync.
    function showFromHash() {
      show(window.location.hash ? window.location.hash.slice(1) : panes[0].id);
    }

    window.addEventListener("hashchange", showFromHash);
    showFromHash();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
