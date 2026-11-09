// BraXhn Verkstad – progressiv förbättring. Allt fungerar utan JS; med JS blir det snabbare och snyggare.
(function () {
  'use strict';
  var root = document.documentElement;
  root.classList.add('js');

  // ---------- Allmänt: utskrift, bekräftelser, kopiera ----------
  document.addEventListener('click', function (e) {
    var p = e.target.closest('[data-print]');
    if (p) { e.preventDefault(); window.print(); }
    var c = e.target.closest('[data-confirm]');
    if (c && !window.confirm(c.getAttribute('data-confirm'))) { e.preventDefault(); e.stopPropagation(); }
    var cp = e.target.closest('[data-copy]');
    if (cp) {
      e.preventDefault();
      copyText(cp.getAttribute('data-copy')).then(function (ok) {
        if (!ok) { window.prompt('Kopiera länken:', cp.getAttribute('data-copy')); return; }
        if (cp.dataset.busy) { return; }
        var old = cp.innerHTML; cp.dataset.busy = '1'; cp.textContent = 'Kopierat!';
        setTimeout(function () { cp.innerHTML = old; delete cp.dataset.busy; }, 1500);
      });
    }
  });
  document.addEventListener('change', function (e) {
    if (e.target.matches('select[data-price-target]')) {
      var opt = e.target.selectedOptions[0];
      var target = document.getElementById(e.target.getAttribute('data-price-target'));
      if (target && opt) { target.placeholder = opt.getAttribute('data-price') || ''; }
    }
    if (e.target.matches('[data-autosubmit]')) { e.target.form.submit(); }
  });

  // Kopiera text. Clipboard-API:t finns bara över https/localhost; på hemmanätet (http://ip:8080)
  // används en dold textruta och execCommand, som fungerar i alla webbläsare.
  function copyText(text) {
    if (navigator.clipboard && window.isSecureContext) {
      return navigator.clipboard.writeText(text).then(function () { return true; }, function () { return legacyCopy(text); });
    }
    return Promise.resolve(legacyCopy(text));
  }
  function legacyCopy(text) {
    var ta = document.createElement('textarea');
    ta.value = text;
    ta.setAttribute('readonly', '');
    ta.style.position = 'fixed'; ta.style.top = '0'; ta.style.left = '-9999px'; ta.style.fontSize = '16px';
    document.body.appendChild(ta);
    ta.focus(); ta.select(); ta.setSelectionRange(0, text.length);
    var ok = false;
    try { ok = document.execCommand('copy'); } catch (err) { ok = false; }
    document.body.removeChild(ta);
    return ok;
  }

  // ---------- Snabbtangent: / fokuserar sökningen ----------
  var search = document.getElementById('global-search');
  document.addEventListener('keydown', function (e) {
    if (e.key === '/' && search && !/INPUT|TEXTAREA|SELECT/.test(document.activeElement.tagName)) {
      e.preventDefault(); search.focus(); search.select();
    }
  });

  // ---------- Timers som tickar live ----------
  function pad(n) { return n < 10 ? '0' + n : '' + n; }
  function tick() {
    document.querySelectorAll('[data-since]').forEach(function (el) {
      var start = new Date(el.getAttribute('data-since').replace(' ', 'T'));
      var s = Math.max(0, Math.floor((Date.now() - start.getTime()) / 1000));
      var h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
      el.textContent = (h ? h + ':' + pad(m) : m) + ':' + pad(sec);
    });
  }
  if (document.querySelector('[data-since]')) { tick(); setInterval(tick, 1000); }

  // Mottagningen: öppna utskriften direkt
  if (document.querySelector('[data-autoprint]')) { setTimeout(function () { window.print(); }, 500); }

  // ---------- Bokningsformuläret ----------
  var form = document.getElementById('booking');
  if (!form) { return; }
  function setSum(key, text) { form.querySelectorAll('[data-sum="' + key + '"]').forEach(function (el) { el.textContent = text; }); }

  function checked(name) { return form.querySelector('input[name="' + name + '"]:checked'); }
  function labelText(input) {
    if (!input) { return ''; }
    var l = form.querySelector('label[for="' + input.id + '"]');
    return l ? (l.getAttribute('data-label') || l.textContent.trim()) : '';
  }

  function filterServices() {
    var cat = checked('category');
    form.querySelectorAll('[data-svc-cat]').forEach(function (g) {
      var show = cat && g.getAttribute('data-svc-cat') === cat.value;
      g.hidden = !show;
      if (!show) { g.querySelectorAll('input').forEach(function (i) { i.checked = false; }); }
    });
    var hint = document.getElementById('svc-hint');
    if (hint) { hint.classList.toggle('hide', !!cat); }
  }

  function showDay() {
    var d = checked('day');
    form.querySelectorAll('[data-day]').forEach(function (g) {
      var show = d && g.getAttribute('data-day') === d.value;
      g.classList.toggle('hide', !show);
      if (!show) { g.querySelectorAll('input').forEach(function (i) { i.checked = false; }); }
    });
  }

  function mode() { var m = checked('dropoff_mode'); return m ? m.value : 'tid'; }

  function showMode() {
    var box = form.querySelector('[data-dropoff="tid"]');
    if (box) { box.classList.toggle('hide', mode() !== 'tid'); }
  }

  function pickReady(input) {
    var fee = input ? parseInt(input.getAttribute('data-fee') || '0', 10) : 0;
    var ack = document.getElementById('fee_ack');
    if (ack) { ack.value = String(fee); }
    setSum('ready', input ? labelText(input) : '–');
    setSum('fee', fee ? ('+' + fee + ' kr') : 'Ingen kostnad');
  }

  // Hämtar klardatum och pris för vald tjänst och inlämningsdag
  var readyTimer = null;
  function refreshReady() {
    clearTimeout(readyTimer);
    readyTimer = setTimeout(function () {
      var url = form.getAttribute('data-ready-url');
      var grid = document.getElementById('ready-options');
      if (!url || !grid || !window.fetch) { return; }
      var svc = checked('service_id'), slot = checked('slot');
      var drop = (mode() === 'tid' && slot) ? slot.value.slice(0, 10) : '';
      fetch(url + '?service_id=' + encodeURIComponent(svc ? svc.value : '') + '&dropoff=' + encodeURIComponent(drop))
        .then(function (r) { return r.ok ? r.json() : null; })
        .then(function (data) {
          if (!data || !data.options) { return; }
          var prev = checked('ready_date'), keep = prev ? prev.value : null, found = false;
          grid.textContent = '';
          data.options.forEach(function (o) {
            var wrap = document.createElement('div'); wrap.className = 'slot ready';
            var inp = document.createElement('input');
            inp.type = 'radio'; inp.name = 'ready_date'; inp.id = 'r-' + o.date; inp.value = o.date;
            inp.setAttribute('data-fee', String(Math.round(o.fee)));
            var lab = document.createElement('label'); lab.htmlFor = inp.id; lab.setAttribute('data-label', o.label);
            var t = document.createElement('span'); t.textContent = o.label;
            var sm = document.createElement('small'); sm.textContent = o.fee ? ('+' + Math.round(o.fee) + ' kr') : (o.standard ? 'Standard' : 'Ingen kostnad');
            lab.appendChild(t); lab.appendChild(sm); wrap.appendChild(inp); wrap.appendChild(lab); grid.appendChild(wrap);
            if (o.date === keep) { inp.checked = true; found = true; }
          });
          if (!found) {
            var std = grid.querySelector('input[value="' + data.standard + '"]') || grid.querySelector('input');
            if (std) { std.checked = true; }
          }
          form.querySelectorAll('[data-ready="standard"]').forEach(function (el) { el.textContent = data.standard_label; });
          pickReady(checked('ready_date'));
          updateSummary();
        })
        .catch(function () {});
    }, 120);
  }

  function updateSummary() {
    var cat = checked('category'), svc = checked('service_id'), slot = checked('slot');
    var item = (form.querySelector('[name=item]') || {}).value || '';
    var what = [labelText(svc) || (cat ? cat.value : ''), item].filter(Boolean).join(', ');
    setSum('what', what || 'Välj vad det gäller');
    setSum('when', mode() === 'overens' ? 'Enligt överenskommelse' : (slot ? labelText(slot) : 'Välj inlämningstid'));
    form.querySelectorAll('[data-step]').forEach(function (st) {
      var key = st.getAttribute('data-step'), done = false;
      if (key === 'what') { done = !!cat && (!!svc || item.length > 1); }
      if (key === 'when') { done = mode() === 'overens' || !!slot; }
      if (key === 'ready') { done = !!checked('ready_date'); }
      if (key === 'who') { done = !!(form.querySelector('[name=name]').value && form.querySelector('[name=email]').value); }
      st.classList.toggle('done', done);
    });
  }

  form.addEventListener('change', function (e) {
    var n = e.target.name;
    if (n === 'category') { filterServices(); }
    if (n === 'day') { showDay(); }
    if (n === 'dropoff_mode') { showMode(); }
    if (n === 'ready_date') { pickReady(e.target); }
    if (n === 'service_id' || n === 'slot' || n === 'dropoff_mode' || n === 'category') { refreshReady(); }
    updateSummary();
  });
  form.addEventListener('input', updateSummary);

  // Initialt: välj första dag (eller dagen för vald tid efter valideringsfel)
  var preSlot = checked('slot');
  if (preSlot) {
    var dayInput = form.querySelector('input[name="day"][value="' + preSlot.value.slice(0, 10) + '"]');
    if (dayInput) { dayInput.checked = true; }
  } else if (!checked('day')) {
    var first = form.querySelector('input[name="day"]');
    if (first) { first.checked = true; }
  }
  filterServices();
  if (preSlot) { preSlot.checked = true; }
  showDay();
  if (preSlot) { preSlot.checked = true; }
  showMode();
  pickReady(checked('ready_date'));
  updateSummary();
  if (checked('service_id') || preSlot) { refreshReady(); }
})();
