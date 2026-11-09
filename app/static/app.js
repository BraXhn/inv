// Minimal JS: utskrift, bekräftelser och förifyllt pris vid val av lagerdel. Allt fungerar även utan JS.
document.addEventListener('click', function (e) {
  var p = e.target.closest('[data-print]');
  if (p) { e.preventDefault(); window.print(); }
  var c = e.target.closest('[data-confirm]');
  if (c && !window.confirm(c.getAttribute('data-confirm'))) { e.preventDefault(); }
  var cp = e.target.closest('[data-copy]');
  if (cp && navigator.clipboard) {
    e.preventDefault();
    navigator.clipboard.writeText(cp.getAttribute('data-copy')).then(function () {
      var old = cp.textContent; cp.textContent = 'Kopierat!';
      setTimeout(function () { cp.textContent = old; }, 1500);
    });
  }
});
document.addEventListener('change', function (e) {
  if (e.target.matches('select[data-price-target]')) {
    var opt = e.target.selectedOptions[0];
    var target = document.getElementById(e.target.getAttribute('data-price-target'));
    if (target && opt) { target.placeholder = opt.getAttribute('data-price') || ''; }
  }
});
