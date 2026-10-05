// Reading-page behaviour: local times, copy buttons, long-code folding, TOC state.
(function () {
  'use strict';
  var doc = document;

  // "Oct 5, 14:32" in the reader's own time zone
  var dateFormat = new Intl.DateTimeFormat('en', {
    month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit', hourCycle: 'h23'
  });
  doc.querySelectorAll('time[datetime]').forEach(function (el) {
    var date = new Date(el.getAttribute('datetime'));
    if (!isNaN(date.getTime())) el.textContent = dateFormat.format(date);
  });

  function icon(name) {
    var span = doc.createElement('span');
    span.className = 'rp-icon rp-icon-' + name;
    span.setAttribute('aria-hidden', 'true');
    return span;
  }

  function copyText(text) {
    if (navigator.clipboard && window.isSecureContext) return navigator.clipboard.writeText(text);
    return new Promise(function (resolve, reject) {
      var area = doc.createElement('textarea');
      area.value = text;
      area.setAttribute('readonly', '');
      area.style.position = 'fixed';
      area.style.opacity = '0';
      doc.body.appendChild(area);
      area.select();
      var ok = false;
      try { ok = doc.execCommand('copy'); } catch (e) { ok = false; }
      area.remove();
      if (ok) { resolve(); } else { reject(new Error('copy failed')); }
    });
  }

  // Button.md: on success .is-done + check icon + "Copied" for 1.5 s
  function copyButton(button, iconName, label, getText) {
    var timer = null;
    function show(done) {
      button.textContent = '';
      if (iconName) button.appendChild(icon(done ? 'check' : iconName));
      button.appendChild(doc.createTextNode(done ? 'Copied' : label));
      button.classList.toggle('is-done', done);
    }
    show(false);
    button.addEventListener('click', function () {
      copyText(getText()).then(function () {
        show(true);
        clearTimeout(timer);
        timer = setTimeout(function () { show(false); }, 1500);
      }).catch(function () {});
    });
  }

  doc.querySelectorAll('.rp-code').forEach(function (block) {
    var bar = block.querySelector('.rp-code-bar');
    var code = block.querySelector('pre code');
    if (!bar || !code) return;
    var button = doc.createElement('button');
    button.type = 'button';
    button.className = 'rp-btn rp-btn-sm';
    button.setAttribute('aria-label', 'Copy code');
    copyButton(button, 'copy', 'Copy', function () { return code.textContent; });
    bar.appendChild(button);

    // CodeBlock.md: more than 40 lines fold to 24 with "Show all"
    if (parseInt(block.getAttribute('data-lines') || '0', 10) > 40) {
      block.classList.add('is-folded');
      var more = doc.createElement('div');
      more.className = 'rp-code-more';
      var expand = doc.createElement('button');
      expand.type = 'button';
      expand.className = 'rp-btn rp-btn-sm';
      expand.textContent = 'Show all';
      expand.addEventListener('click', function () {
        block.classList.remove('is-folded');
        more.remove();
      });
      more.appendChild(expand);
      block.appendChild(more);
    }
  });

  var copyAll = doc.querySelector('[data-copy-all]');
  var raw = doc.querySelector('.rp-raw');
  if (copyAll && raw) copyButton(copyAll, null, 'Copy all', function () { return raw.value; });

  // Toc.md: collapsed on phones; the section being read is highlighted
  var toc = doc.querySelector('.rp-toc');
  if (!toc) return;
  if (window.matchMedia('(max-width: 640px)').matches) toc.removeAttribute('open');
  var links = {};
  toc.querySelectorAll('a[href^="#"]').forEach(function (link) {
    links[decodeURIComponent(link.getAttribute('href').slice(1))] = link;
  });
  var headings = Object.keys(links).map(function (id) { return doc.getElementById(id); }).filter(Boolean);
  if (!headings.length || !('IntersectionObserver' in window)) return;
  var current = null;
  function update() {
    var active = headings[0];
    for (var i = 0; i < headings.length; i++) {
      if (headings[i].getBoundingClientRect().top <= 96) active = headings[i];
    }
    if (active === current) return;
    if (current) links[current.id].classList.remove('is-active');
    links[active.id].classList.add('is-active');
    current = active;
  }
  var observer = new IntersectionObserver(update, { rootMargin: '0px 0px -60% 0px', threshold: [0, 1] });
  headings.forEach(function (heading) { observer.observe(heading); });
  // Jumps (Home key, links) can skip every heading's trigger band; re-check on scroll too.
  var queued = false;
  window.addEventListener('scroll', function () {
    if (queued) return;
    queued = true;
    requestAnimationFrame(function () { queued = false; update(); });
  }, { passive: true });
  update();
})();
