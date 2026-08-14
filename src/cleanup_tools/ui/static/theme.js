/*
 * Theme switching (Ledger / Sonar / Tide) + the dashboard's canvas chart.
 *
 * The FOUC-avoiding part of theme switching -- reading localStorage and
 * stamping `data-theme` onto <html> before first paint -- runs as an
 * inline <script> at the very top of base.html's <head>, so it executes
 * before this file (and before any CSS) even loads. This file only needs
 * to handle what can safely wait until after the page is interactive:
 *
 *   1. Sync the nav's #theme-select <select> to whatever theme is
 *      currently active (so the dropdown reflects reality on first load,
 *      including the "ledger" default before any explicit user choice).
 *   2. On change, apply the new theme (data-theme attribute) and persist
 *      it to localStorage under "cleanup-tools-theme", so it survives
 *      the next page navigation (every route re-renders base.html fresh,
 *      and the inline head script picks the stored value back up).
 *   3. Draw the dashboard's "size by group" donut chart onto
 *      #dashboard-chart from the JSON data dashboard.html renders inline
 *      into #dashboard-chart-data -- and redraw it whenever the theme
 *      changes, since its colors are read live from the active theme's
 *      CSS custom properties. This is a no-op on any page that doesn't
 *      have that canvas (i.e. every page except the dashboard).
 *
 * Deliberately vanilla JS with no build step or dependency, consistent
 * with keyboard.js and plan-reclaim.js.
 */
(function () {
  "use strict";

  var STORAGE_KEY = "cleanup-tools-theme";
  var VALID_THEMES = ["ledger", "sonar", "tide"];

  function currentTheme() {
    var attr = document.documentElement.getAttribute("data-theme");
    return VALID_THEMES.indexOf(attr) !== -1 ? attr : "ledger";
  }

  function applyTheme(theme, persist) {
    if (VALID_THEMES.indexOf(theme) === -1) {
      theme = "ledger";
    }
    document.documentElement.setAttribute("data-theme", theme);
    if (persist) {
      try {
        window.localStorage.setItem(STORAGE_KEY, theme);
      } catch (e) {
        // localStorage may be unavailable (private browsing, storage
        // disabled, ...) -- the theme still applies for this page view,
        // it just won't be remembered on the next navigation.
      }
    }
    drawDashboardChart();
  }

  function initSwitcher() {
    var select = document.getElementById("theme-select");
    if (!select) {
      return;
    }
    select.value = currentTheme();
    select.addEventListener("change", function () {
      applyTheme(select.value, true);
    });
  }

  // -------------------------------------------------------------------
  // Dashboard chart.
  // -------------------------------------------------------------------

  function readChartData() {
    var el = document.getElementById("dashboard-chart-data");
    if (!el) {
      return null;
    }
    try {
      var data = JSON.parse(el.textContent);
      return Array.isArray(data) ? data : null;
    } catch (e) {
      return null;
    }
  }

  function cssVar(name) {
    return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  }

  function drawDashboardChart() {
    var canvas = document.getElementById("dashboard-chart");
    if (!canvas || !canvas.getContext) {
      return;
    }
    var ctx = canvas.getContext("2d");
    var size = canvas.width;
    var cx = size / 2;
    var cy = size / 2;
    var radius = size / 2 - 10;
    var thickness = Math.max(10, radius * 0.34);

    ctx.clearRect(0, 0, size, size);

    var data = readChartData();
    var legend = document.getElementById("dashboard-chart-legend");
    if (legend) {
      legend.innerHTML = "";
    }
    if (!data || data.length === 0) {
      return;
    }

    var total = data.reduce(function (sum, d) {
      return sum + (typeof d.value === "number" ? d.value : 0);
    }, 0);

    var palette = [
      cssVar("--accent"),
      cssVar("--good"),
      cssVar("--pending"),
      cssVar("--ink-soft"),
      cssVar("--line-strong")
    ].filter(function (c) {
      return !!c;
    });
    if (palette.length === 0) {
      palette = ["#8a3b2b"];
    }

    ctx.lineWidth = thickness;
    ctx.lineCap = "butt";

    if (total <= 0) {
      ctx.strokeStyle = cssVar("--line") || "#ccc";
      ctx.beginPath();
      ctx.arc(cx, cy, radius, 0, Math.PI * 2);
      ctx.stroke();
    } else {
      // Small angular gaps between slices (rather than one continuous
      // ring) so adjacent same-color-cycled slices stay visually
      // distinct -- a purely cosmetic touch, not load-bearing.
      var gap = data.length > 1 ? 0.02 : 0;
      var start = -Math.PI / 2;
      data.forEach(function (d, i) {
        var value = typeof d.value === "number" ? d.value : 0;
        var slice = (value / total) * (Math.PI * 2);
        if (slice <= 0) {
          return;
        }
        var end = start + Math.max(slice - gap, slice * 0.5);
        ctx.strokeStyle = palette[i % palette.length];
        ctx.beginPath();
        ctx.arc(cx, cy, radius, start, end);
        ctx.stroke();
        start += slice;
      });
    }

    if (legend) {
      data.forEach(function (d, i) {
        var row = document.createElement("div");

        var swatch = document.createElement("span");
        swatch.className = "swatch";
        swatch.style.background = palette[i % palette.length];
        row.appendChild(swatch);

        var label = document.createElement("span");
        label.className = "label";
        var pct = total > 0 ? Math.round(((d.value || 0) / total) * 100) : 0;
        label.textContent = (d.label || "ungrouped") + " — " + pct + "%";
        row.appendChild(label);

        legend.appendChild(row);
      });
    }
  }

  function init() {
    initSwitcher();
    drawDashboardChart();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
