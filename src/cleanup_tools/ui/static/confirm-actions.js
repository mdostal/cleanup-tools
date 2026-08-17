/*
 * Global confirm-before-submit for any <form data-confirm="..."> on the
 * page -- one listener, not a per-template onsubmit handler. Deliberately
 * NOT built as inline onsubmit="return confirm('...')" with server-side
 * string interpolation: a bucket name or location nickname is
 * user-editable free text (Settings > Bucket Rules), and splicing it
 * into a JS string literal in markup would break (or, worse, be a real
 * injection surface) the moment that text contains a quote character.
 * data-confirm is just an HTML attribute -- Jinja's normal
 * attribute-escaping handles arbitrary text safely, no JS-string
 * quoting involved at all.
 *
 * Direct answer to this project's UI design review: every bulk action
 * (approve/reject/undo a whole bucket, branch, or selection) must show
 * the real count and target before it fires, at any scale.
 *
 * The queue page's "Approve selected" / "Reject selected" buttons are a
 * second, dynamic case this same listener handles: their target count
 * only exists client-side (however many checkboxes are currently ticked)
 * so it can't be baked into a server-rendered data-confirm string. Those
 * two buttons carry data-bulk-selected-action="Approve"/"Reject" instead,
 * and `evt.submitter` (the actual button that triggered this submit, not
 * just the form) is enough to build the message from the live
 * #bulk-selected-count text at the moment of submission.
 *
 * Deliberately vanilla JS with no build step or dependency, consistent
 * with the rest of this UI's static/*.js files.
 */
(function () {
  "use strict";

  function confirmBulkSelected(evt, submitter) {
    var countEl = document.getElementById("bulk-selected-count");
    var n = countEl ? parseInt(countEl.textContent, 10) || 0 : 0;
    if (!n) {
      // Nothing selected -- block here with a clear reason rather than
      // letting an empty bulk request round-trip to the server only to
      // report back "touched nothing".
      window.alert("Select at least one entry first.");
      evt.preventDefault();
      return;
    }
    var verb = submitter.dataset.bulkSelectedAction;
    var message = verb + " " + n + " selected entr" + (n === 1 ? "y" : "ies") + "?";
    if (!window.confirm(message)) {
      evt.preventDefault();
    }
  }

  document.addEventListener("submit", function (evt) {
    var form = evt.target;
    var submitter = evt.submitter;

    if (submitter && submitter.dataset && submitter.dataset.bulkSelectedAction) {
      confirmBulkSelected(evt, submitter);
      return;
    }

    if (form && form.dataset && form.dataset.confirm) {
      if (!window.confirm(form.dataset.confirm)) {
        evt.preventDefault();
      }
    }
  });
})();
