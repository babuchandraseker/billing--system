/* ═══════════════════════════════════════════════════════════
   Thermal PDF page size — 80mm wide × the receipt's own height.

   CSS cannot express "80mm × content height": `size: 80mm auto` is
   invalid and browsers drop it (falling back to Letter/A4), while
   `size: 80mm` means an 80×80mm square page. So the receipt height is
   measured right before printing and injected as a valid @page size.
   thermal_receipt.css keeps `size: 80mm 297mm` as the no-JS fallback.

   Layout, widths and font sizes are NOT touched — only the page box.
   ═══════════════════════════════════════════════════════════ */
(function () {
  var MM_PER_PX = 25.4 / 96;
  // Screen padding is >= print padding, so the measured height already
  // over-estimates slightly; the slack covers rounding. Never under-shoot:
  // a page shorter than the receipt would split it across pages.
  var SLACK_MM = 4;

  function setThermalPageSize() {
    var bill = document.querySelector('.thermal-bill');
    if (!bill) return;
    var heightMm = Math.ceil(bill.getBoundingClientRect().height * MM_PER_PX) + SLACK_MM;
    var style = document.getElementById('thermal-page-size');
    if (!style) {
      style = document.createElement('style');
      style.id = 'thermal-page-size';
      document.head.appendChild(style);
    }
    style.textContent = '@page { size: 80mm ' + heightMm + 'mm; margin: 0; }';
  }

  window.addEventListener('beforeprint', setThermalPageSize);
  window.addEventListener('load', function () {
    if (document.fonts && document.fonts.ready) {
      document.fonts.ready.then(setThermalPageSize);
    } else {
      setThermalPageSize();
    }
  });
})();
