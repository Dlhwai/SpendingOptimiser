"use strict";
// Keeps a From/To pair inside the three-calendar-month limit while dates are chosen.
// The server applies the same rule (monthly.date_range); without this script the
// browser still submits plain dates and the server rejects an invalid pair.
(function () {
  var DAY = /^(\d{4})-(\d{2})-(\d{2})$/;

  // Last allowed day for a range that starts on `start`: three calendar months later,
  // clamped to the end of a shorter month, minus one day. Returns "" for no valid date.
  function latestEnd(start) {
    var parts = DAY.exec(start);
    if (!parts) return "";
    var index = Number(parts[1]) * 12 + Number(parts[2]) - 1 + 3;
    var year = Math.floor(index / 12);
    var month = index % 12;
    var length = new Date(Date.UTC(year, month + 1, 0)).getUTCDate();
    var day = Math.min(Number(parts[3]), length);
    return new Date(Date.UTC(year, month, day - 1)).toISOString().slice(0, 10);
  }

  function wire(group) {
    var start = group.querySelector('input[name="start"]');
    var end = group.querySelector('input[name="end"]');
    if (!start || !end) return;
    var ceiling = start.max; // the supported last day; the server sets end.max from From
    function sync(moved) {
      if (!DAY.test(start.value)) return;
      if (moved && end.value && end.value < start.value) end.value = start.value;
      end.min = start.value;
      var limit = latestEnd(start.value);
      end.max = ceiling && ceiling < limit ? ceiling : limit;
    }
    start.addEventListener("change", function () {
      sync(true);
    });
    sync(false);
  }

  if (typeof module !== "undefined" && module.exports) module.exports = { latestEnd: latestEnd };
  if (typeof document !== "undefined") {
    document.querySelectorAll("[data-period-picker]").forEach(wire);
  }
})();
