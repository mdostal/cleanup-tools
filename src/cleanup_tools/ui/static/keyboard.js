/*
 * Keyboard shortcuts for the /queue review page.
 *
 * Operates against a single "focused" entry card (tracked purely client
 * side, via a `.focused` CSS class on one `.entry-card` at a time):
 *
 *   y            approve the focused entry (submits its existing approve
 *                form -- a real POST to /queue/<id>/approve, same route
 *                the "Approve" button already uses, so approving via
 *                keyboard and via mouse click behave identically, including
 *                the page reload/redirect afterwards).
 *   n            reject the focused entry (same idea, /queue/<id>/reject).
 *   space        toggle the focused entry's bulk-select checkbox, without
 *                submitting anything or leaving the page.
 *   ArrowDown/Right   move focus to the next entry card.
 *   ArrowUp/Left      move focus to the previous entry card.
 *
 * Deliberately vanilla JS with no build step or dependency, consistent
 * with the rest of this UI (plain Jinja templates, no frontend framework).
 */
(function () {
  "use strict";

  function init() {
    var cards = Array.prototype.slice.call(document.querySelectorAll(".entry-card"));
    if (cards.length === 0) {
      return;
    }

    var focusedIndex = -1;

    function focusedCard() {
      return focusedIndex >= 0 ? cards[focusedIndex] : null;
    }

    function setFocus(index) {
      if (index < 0 || index >= cards.length) {
        return;
      }
      var previous = focusedCard();
      if (previous) {
        previous.classList.remove("focused");
      }
      focusedIndex = index;
      cards[focusedIndex].classList.add("focused");
      cards[focusedIndex].scrollIntoView({ block: "nearest" });
    }

    // Start with the first entry on the page focused, so a keyboard-only
    // user has something to act on immediately without first clicking.
    setFocus(0);

    function submitEntryForm(card, action) {
      if (!card) {
        return;
      }
      var form = card.querySelector('form[data-action="' + action + '"]');
      if (!form) {
        return;
      }
      if (typeof form.requestSubmit === "function") {
        form.requestSubmit();
      } else {
        form.submit();
      }
    }

    function toggleSelect(card) {
      if (!card) {
        return;
      }
      var checkbox = card.querySelector(".entry-select");
      if (!checkbox || checkbox.disabled) {
        return;
      }
      checkbox.checked = !checkbox.checked;
      // Let any listener relying on native change events (e.g. the
      // selected-count updater below) react the same way it would to a
      // real mouse click.
      checkbox.dispatchEvent(new Event("change", { bubbles: true }));
    }

    document.addEventListener("keydown", function (evt) {
      // Don't hijack typing in a real text field. Checkboxes are exempt --
      // pressing space while a checkbox itself has native focus should
      // still go through our toggle (and the browser's own default
      // checkbox-toggle-on-space is equivalent anyway).
      var target = evt.target;
      var tag = target && target.tagName ? target.tagName : "";
      if ((tag === "INPUT" && target.type !== "checkbox") || tag === "TEXTAREA" || tag === "SELECT") {
        return;
      }

      switch (evt.key) {
        case "y":
        case "Y":
          evt.preventDefault();
          submitEntryForm(focusedCard(), "approve");
          break;
        case "n":
        case "N":
          evt.preventDefault();
          submitEntryForm(focusedCard(), "reject");
          break;
        case " ":
        case "Spacebar":
          evt.preventDefault();
          toggleSelect(focusedCard());
          break;
        case "ArrowDown":
        case "ArrowRight":
          evt.preventDefault();
          setFocus(Math.min(focusedIndex + 1, cards.length - 1));
          break;
        case "ArrowUp":
        case "ArrowLeft":
          evt.preventDefault();
          setFocus(Math.max(focusedIndex - 1, 0));
          break;
        default:
          break;
      }
    });

    // Keep the bulk-action bar's "N selected" counter in sync, whether a
    // checkbox was toggled by mouse click or by the space-bar shortcut.
    var countEl = document.getElementById("bulk-selected-count");
    function updateSelectedCount() {
      if (!countEl) {
        return;
      }
      var checked = document.querySelectorAll(".entry-select:checked").length;
      countEl.textContent = checked + " selected";
    }
    document.addEventListener("change", function (evt) {
      var t = evt.target;
      if (t && t.classList && t.classList.contains("entry-select")) {
        updateSelectedCount();
      }
    });
    updateSelectedCount();

    // Small hook exposed for debugging / automated verification (e.g. a
    // Playwright session can read window.__cleanupQueueKeyboard.getFocusedIndex()
    // to assert focus moved without depending on CSS class internals).
    window.__cleanupQueueKeyboard = {
      getFocusedIndex: function () {
        return focusedIndex;
      },
      setFocus: setFocus,
      cardCount: cards.length,
    };
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
