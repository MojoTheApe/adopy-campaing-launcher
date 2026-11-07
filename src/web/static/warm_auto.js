/* ACL-2268: автопрогрев профиля из билдера. Когда ЧЕЛОВЕК добавил карточку кампании, продублировал её
   или правит её в форме аккаунта, просим сервер поднять профиль этого аккаунта в фоне
   (POST /warm/ensure), чтобы проверка плана и Publish пошли на готовой вкладке.

   Правила:
   - только по настоящему действию человека (`event.isTrusted`) в этой сессии страницы; загрузка
     страницы и восстановленный черновик профиль не открывают;
   - только ACL_FB_BACKEND=api; один запрос на аккаунт за сессию страницы; нужна хотя бы одна карточка;
   - никакого setInterval: статус читается из GET /warm/state (локально, ни Meta, ни MLX) короткой
     цепочкой таймеров, и только пока профиль стоит в очереди или открывается;
   - отказ молчаливый: строка гаснет, без красного текста и кнопок; причина — в подсказке чипа аккаунта;
   - профиль мы никогда не закрываем и вне Multilogin не открываем: открытие делает сервер. */
(function () {
  'use strict';
  if (window.__aclWarmAuto) return;
  window.__aclWarmAuto = true;

  var POLL_MS = 3000;
  var POLL_MAX = 200;           // ~10 минут, дальше строка гаснет
  var asked = {};               // account -> true (в этой сессии страницы уже просили)

  function isApi() { return window.ACL_FB_BACKEND === 'api'; }

  function labelOf(form) {
    var f = form.querySelector('[name="account_label"]');
    return f ? String(f.value || '').trim() : '';
  }

  function lineOf(form) {
    var el = form.querySelector('.warm-auto-line');
    if (el) return el;
    el = document.createElement('div');
    el.className = 'warm-auto-line';
    el.style.cssText = 'font-size:.78rem;margin:.2rem 0 .5rem;color:var(--muted,#888)';
    var anchor = form.querySelector('.cb-empty-hint') || form.querySelector('.cb-slots');
    if (anchor && anchor.parentNode) anchor.parentNode.insertBefore(el, anchor);
    else form.appendChild(el);
    return el;
  }

  function say(form, text, ok) {
    var el = lineOf(form);
    el.textContent = text || '';
    el.style.color = ok ? 'var(--ok,#2a9d4b)' : 'var(--muted,#888)';
  }

  function post(url, body) {
    return fetch(url, { method: 'POST', credentials: 'same-origin',
                        headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) })
      .then(function (r) { return r.json().then(function (j) { return { status: r.status, body: j }; }); });
  }

  function poll(form, label, n) {
    fetch('/warm/state?account=' + encodeURIComponent(label), { credentials: 'same-origin' })
      .then(function (r) { return r.json(); })
      .then(function (data) {
        var info = data && data.accounts && data.accounts[label];
        if (!info) { say(form, ''); return; }
        if (info.auto === 'queued' || info.auto === 'opening') {
          say(form, 'Opening profile…');
          if (n < POLL_MAX) setTimeout(function () { poll(form, label, n + 1); }, POLL_MS);
          else say(form, '');
          return;
        }
        if (info.state === 'warm') { say(form, 'Profile ready', true); return; }
        say(form, '');      // любой отказ: строка гаснет, причина — в подсказке чипа
      })
      .catch(function () { say(form, ''); });
  }

  /* Вызывается ТОЛЬКО из обработчиков настоящих действий человека. */
  function userAction(form, trigger) {
    if (!isApi() || !form) return;
    var label = labelOf(form);
    var cards = form.querySelectorAll('.cb-slot').length;
    if (!label || asked[label] || cards < 1) return;
    asked[label] = true;
    post('/warm/ensure', { accounts: [label], trigger: trigger, campaigns: cards }).then(function (res) {
      if (res.status !== 202) return;       // выключено, лимит, отказ: ничего не показываем
      say(form, 'Opening profile…');
      poll(form, label, 0);
    }).catch(function () { /* нет связи: ничего не показываем */ });
  }

  /* Страница аккаунта: «+ Add Campaign», дубликат карточки, правка внутри карточки. Карточка при
     добавлении появляется после обработчика клика, поэтому проверку откладываем. Программные события
     (восстановление черновика, автозаполнение) isTrusted=false и не считаются. */
  function touched(ev) {
    if (!ev || !ev.isTrusted || !isApi() || !ev.target || !ev.target.closest) return;
    var t = ev.target;
    var trigger = '';
    if (ev.type === 'click') {
      if (t.closest('.cb-dup-slot-btn')) trigger = 'duplicate';
      else if (t.closest('.cb-add-btn')) trigger = 'add';
    } else if (t.closest('.cb-slot')) {
      trigger = 'edit';
    }
    if (!trigger) return;
    var form = t.closest('form.open-account-form');
    if (!form) return;
    if (trigger === 'edit') userAction(form, trigger);
    else setTimeout(function () { userAction(form, trigger); }, 0);
  }
  ['click', 'input', 'change'].forEach(function (type) { document.addEventListener(type, touched, true); });

  /* Ящик билдера: его «+ Campaign» и «+» у аккаунта лежат вне формы, форму отдаёт сам обработчик. */
  window.aclWarmAutoUser = userAction;
})();
