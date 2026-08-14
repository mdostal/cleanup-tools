/*
 * Client-side wiring for every "Plan: Reclaim" link on the page.
 *
 * GET /plan/reclaim no longer does the (potentially ~2-minute) reclaim
 * planning/staging work synchronously and redirect back to the dashboard --
 * it now returns `{"job_id": "..."}` immediately and does the work on a
 * background thread (see cleanup_tools/ui/jobs.py), pollable via
 * GET /status/<job_id>. A plain `<a href="/plan/reclaim">` link, left alone,
 * would just navigate a real browser to that bare JSON blob with no
 * progress feedback and no way back -- this file is what makes clicking
 * the link behave like a real (if async) page action again:
 *
 *   1. Intercept the click, prevent normal navigation.
 *   2. Fetch the href (same GET request the plain link would have made).
 *   3. Poll /status/<job_id> every ~400ms, updating a visible status area.
 *   4. On "done", navigate to the dashboard with ?staged=N (mirroring the
 *      OLD synchronous route's end state -- landing on the dashboard with a
 *      "Staged N entries" flash).
 *   5. On "error" (or a network failure at any step), show the error in the
 *      status area instead of leaving a dead JSON page or a silent hang.
 *
 * Every link this applies to is tagged `class="plan-reclaim-link"` and
 * carries two data attributes rendered server-side (see base.html,
 * dashboard.html, queue.html):
 *
 *   data-status-url-template  -- url_for('ui.job_status', job_id='__JOB_ID__'),
 *                                 with the literal "__JOB_ID__" placeholder
 *                                 swapped for the real job id at poll time.
 *   data-dashboard-url        -- url_for('ui.dashboard'), the redirect
 *                                 target once the job finishes.
 *
 * Deliberately vanilla JS with no build step or dependency, consistent with
 * keyboard.js.
 */
(function () {
  "use strict";

  var POLL_INTERVAL_MS = 400;

  // Only one reclaim job may be in flight at a time from this page --
  // guards against a double-click (or clicking a second "Plan: Reclaim"
  // link, e.g. the nav one and an empty-state one) firing two concurrent
  // /plan/reclaim requests.
  var jobInFlight = false;

  function statusEl() {
    return document.getElementById("plan-reclaim-status");
  }

  function setStatusText(text, isError) {
    var el = statusEl();
    if (!el) {
      return;
    }
    el.textContent = text;
    el.classList.toggle("plan-reclaim-status-error", !!isError);
  }

  function fail(message) {
    setStatusText("Could not plan: " + message, true);
    jobInFlight = false;
  }

  function poll(link, jobId) {
    var statusUrl = link.dataset.statusUrlTemplate.replace("__JOB_ID__", jobId);

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
            setStatusText("Staging reclaim plan... (" + payload.current + "/" + payload.total + ")");
            window.setTimeout(tick, POLL_INTERVAL_MS);
            return;
          }
          if (payload.status === "done") {
            var count = (payload.result && payload.result.count) || 0;
            setStatusText("Staged " + count + " entr" + (count === 1 ? "y" : "ies") + " for review. Reloading...");
            window.location.href = link.dataset.dashboardUrl + "?staged=" + encodeURIComponent(count);
            return;
          }
          // payload.status === "error"
          fail(payload.error || "unknown error");
        })
        .catch(function (err) {
          fail(err.message);
        });
    }

    tick();
  }

  function startReclaimJob(link) {
    if (jobInFlight) {
      return;
    }
    jobInFlight = true;
    setStatusText("Staging reclaim plan...");

    fetch(link.getAttribute("href"), { headers: { Accept: "application/json" } })
      .then(function (resp) {
        if (!resp.ok) {
          throw new Error("could not start reclaim plan (HTTP " + resp.status + ")");
        }
        return resp.json();
      })
      .then(function (payload) {
        poll(link, payload.job_id);
      })
      .catch(function (err) {
        fail(err.message);
      });
  }

  function init() {
    var links = Array.prototype.slice.call(document.querySelectorAll(".plan-reclaim-link"));
    links.forEach(function (link) {
      link.addEventListener("click", function (evt) {
        evt.preventDefault();
        startReclaimJob(link);
      });
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
