/*
 * Client-side wiring for every "Plan: X" async job on the page -- both the
 * individual "Plan: Sort" / "Plan: Reclaim" / "Plan: Corral Screenshots"
 * links, and the dashboard's "kickoff bar" that launches several of them at
 * once. This is the generalized successor to plan-reclaim.js: all three
 * GET /plan/<kind> routes now kick off their (potentially slow -- see
 * cleanup_tools/ui/jobs.py and routes.py's per-entry build_plan_snapshot
 * loop) planning/staging work on a background thread and return
 * `{"job_id": "..."}` immediately instead of blocking the request, pollable
 * via GET /status/<job_id>. A plain `<a href="/plan/sort">` link, left
 * alone, would just navigate a real browser to that bare JSON blob with no
 * progress feedback and no way back -- this file is what makes clicking any
 * of them behave like a real (if async) page action again.
 *
 * Two independent pieces share the same low-level poll() helper:
 *
 * 1. Individual trigger links -- any element tagged `class="plan-trigger-link"`
 *    (see base.html's nav, dashboard.html's and queue.html's empty states).
 *    Each carries:
 *      data-plan-kind             -- "sort" | "reclaim" | "corral-screenshots",
 *                                     used only for status text.
 *      data-status-url-template   -- url_for('ui.job_status', job_id='__JOB_ID__'),
 *                                     with the literal "__JOB_ID__" placeholder
 *                                     swapped for the real job id at poll time.
 *      data-dashboard-url         -- url_for('ui.dashboard'), the redirect
 *                                     target once the job finishes.
 *    Clicking one: intercept the click, GET the href, poll until
 *    done/error, update the single shared #plan-status live region, and on
 *    success navigate to the dashboard with ?staged=N (mirroring the OLD
 *    synchronous routes' end state). Only one such job may be in flight at
 *    a time from this page (guards against a double-click, or clicking two
 *    different individual links back to back).
 *
 * 2. The kickoff bar -- `#kickoff-form` (dashboard.html only), with
 *    `.kickoff-checkbox` inputs (each carrying data-plan-kind/data-plan-url)
 *    and a `#kickoff-panel` results area. Submitting it fires ALL checked
 *    plans' routes in parallel (independent fetch() calls, not sequential
 *    awaits), tracks each job_id's poll independently, and renders each
 *    one's live progress/result as its own row in the panel -- so the user
 *    can watch e.g. sort + reclaim + corral-screenshots all complete
 *    without re-clicking each "Plan: X" link individually, and without the
 *    page navigating away mid-run. Once every selected job reaches a
 *    terminal state, the panel shows a combined summary and the page
 *    refreshes so the status summary/group breakdown reflects the final
 *    state (matching the individual-link flow's own "reload once done"
 *    convention, just after N jobs instead of 1).
 *
 * Deliberately vanilla JS with no build step or dependency, consistent with
 * keyboard.js/theme.js.
 */
(function () {
  "use strict";

  var POLL_INTERVAL_MS = 400;

  // ---------------------------------------------------------------------
  // Shared low-level poller. Neither caller needs to know about fetch()
  // plumbing or the __JOB_ID__ substitution more than once.
  // ---------------------------------------------------------------------

  function pollJob(statusUrlTemplate, jobId, handlers) {
    var statusUrl = statusUrlTemplate.replace("__JOB_ID__", jobId);

    function tick() {
      fetch(statusUrl, { headers: { Accept: "application/json" } })
        .then(function (resp) {
          if (!resp.ok) {
            throw new Error("status check failed (HTTP " + resp.status + ")");
          }
          return resp.json();
        })
        .then(function (payload) {
          if (payload.status === "running") {
            if (handlers.onProgress) {
              handlers.onProgress(payload.current, payload.total);
            }
            window.setTimeout(tick, POLL_INTERVAL_MS);
            return;
          }
          if (payload.status === "done") {
            if (handlers.onDone) {
              handlers.onDone(payload.result || {});
            }
            return;
          }
          // payload.status === "error"
          if (handlers.onError) {
            handlers.onError(payload.error || "unknown error");
          }
        })
        .catch(function (err) {
          if (handlers.onError) {
            handlers.onError(err.message);
          }
        });
    }

    tick();
  }

  // ---------------------------------------------------------------------
  // 1. Individual "Plan: X" trigger links.
  // ---------------------------------------------------------------------

  var triggerJobInFlight = false;

  function planStatusEl() {
    return document.getElementById("plan-status");
  }

  function setPlanStatusText(text, isError) {
    var el = planStatusEl();
    if (!el) {
      return;
    }
    el.textContent = text;
    el.classList.toggle("plan-status-error", !!isError);
  }

  function triggerFail(message) {
    setPlanStatusText("Could not plan: " + message, true);
    triggerJobInFlight = false;
  }

  function startTriggerJob(link) {
    if (triggerJobInFlight) {
      return;
    }
    triggerJobInFlight = true;
    var kind = link.dataset.planKind || "plan";
    setPlanStatusText("Staging " + kind + " plan...");

    fetch(link.getAttribute("href"), { headers: { Accept: "application/json" } })
      .then(function (resp) {
        if (!resp.ok) {
          throw new Error("could not start " + kind + " plan (HTTP " + resp.status + ")");
        }
        return resp.json();
      })
      .then(function (payload) {
        pollJob(link.dataset.statusUrlTemplate, payload.job_id, {
          onProgress: function (current, total) {
            setPlanStatusText("Staging " + kind + " plan... (" + current + "/" + total + ")");
          },
          onDone: function (result) {
            var count = result.count || 0;
            setPlanStatusText(
              "Staged " + count + " entr" + (count === 1 ? "y" : "ies") + " for review. Reloading..."
            );
            triggerJobInFlight = false;
            window.location.href = link.dataset.dashboardUrl + "?staged=" + encodeURIComponent(count);
          },
          onError: function (message) {
            triggerFail(message);
          },
        });
      })
      .catch(function (err) {
        triggerFail(err.message);
      });
  }

  function initTriggerLinks() {
    var links = Array.prototype.slice.call(document.querySelectorAll(".plan-trigger-link"));
    links.forEach(function (link) {
      link.addEventListener("click", function (evt) {
        evt.preventDefault();
        startTriggerJob(link);
      });
    });
  }

  // ---------------------------------------------------------------------
  // 2. The kickoff bar: launch several plans at once, track each
  //    independently, report a combined result.
  // ---------------------------------------------------------------------

  function initKickoffBar() {
    var form = document.getElementById("kickoff-form");
    if (!form) {
      return;
    }
    var panel = document.getElementById("kickoff-panel");
    var runButton = document.getElementById("kickoff-run-button");
    var statusUrlTemplate = form.dataset.statusUrlTemplate;
    var dashboardUrl = form.dataset.dashboardUrl;

    form.addEventListener("submit", function (evt) {
      evt.preventDefault();

      var checked = Array.prototype.slice.call(
        form.querySelectorAll(".kickoff-checkbox:checked")
      );
      if (!checked.length) {
        panel.textContent = "Select at least one plan to run.";
        return;
      }

      panel.innerHTML = "";
      runButton.disabled = true;
      var remaining = checked.length;

      function oneJobSettled() {
        remaining -= 1;
        if (remaining > 0) {
          return;
        }
        runButton.disabled = false;
        var summary = document.createElement("div");
        summary.className = "kickoff-row kickoff-summary";
        summary.textContent = "All selected plans finished. Refreshing...";
        panel.appendChild(summary);
        window.setTimeout(function () {
          window.location.href = dashboardUrl;
        }, 700);
      }

      // Fire every selected plan's route in parallel -- independent
      // fetch() calls started in the same tick, not awaited one after
      // another -- so all of them start their background jobs together
      // instead of queueing up behind each other on the client side.
      checked.forEach(function (checkbox) {
        var kind = checkbox.dataset.planKind;
        var row = document.createElement("div");
        row.className = "kickoff-row";
        row.dataset.planKind = kind;
        row.textContent = kind + ": starting...";
        panel.appendChild(row);

        fetch(checkbox.dataset.planUrl, { headers: { Accept: "application/json" } })
          .then(function (resp) {
            if (!resp.ok) {
              throw new Error("HTTP " + resp.status);
            }
            return resp.json();
          })
          .then(function (payload) {
            pollJob(statusUrlTemplate, payload.job_id, {
              onProgress: function (current, total) {
                row.textContent = kind + ": staging... (" + current + "/" + total + ")";
              },
              onDone: function (result) {
                var count = result.count || 0;
                row.textContent =
                  kind + ": staged " + count + " entr" + (count === 1 ? "y" : "ies") + ".";
                row.classList.add("kickoff-row-done");
                oneJobSettled();
              },
              onError: function (message) {
                row.textContent = kind + ": failed (" + message + ")";
                row.classList.add("kickoff-row-error");
                oneJobSettled();
              },
            });
          })
          .catch(function (err) {
            row.textContent = kind + ": failed to start (" + err.message + ")";
            row.classList.add("kickoff-row-error");
            oneJobSettled();
          });
      });
    });
  }

  function init() {
    initTriggerLinks();
    initKickoffBar();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
