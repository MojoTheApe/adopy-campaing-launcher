/* ACL-2899: аккаунты юзера с хаба. Только по событиям: открытие страницы и возврат вкладки.
   Без таймеров. Ответ тихий; единственное, что видит человек, это один раз на машину
   предложение войти в Google заново (старый токен без прав на email). «Один раз» помнит
   сервер дешборда (j.prompt), а не вкладка. */
(function () {
  'use strict';
  if (window.__aclUserAccountsSync) return;  // сохранённая страница и свежая оба подключают файл
  window.__aclUserAccountsSync = true;
  var busy = false;

  // Ревью ACL-2899: сравнение периодов (compare= в адресе) — режим только для просмотра,
  // синхронизация пишет в карту, поэтому там не запускается. Обычное открытие догонит.
  function readOnlyView() {
    try { return new URLSearchParams(location.search).has('compare'); } catch (e) { return false; }
  }

  function sync() {
    if (busy || readOnlyView()) return;
    busy = true;
    fetch('/api/user-accounts/sync', { method: 'POST', credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' }, body: '{}' })
      .then(function (r) { return r.json(); })
      .then(function (j) {
        if (!j) return;
        if (j.status === 'signin_required' && j.prompt && window.aclConnections) window.aclConnections.open();
        // Карта изменилась: если открыта вкладка Accounts, пусть перечитает себя на месте, без перезагрузки страницы
        if (j.changed && location.hash === '#accounts') {
          document.dispatchEvent(new CustomEvent('acl:viewchange', { detail: { view: 'accounts' } }));
        }
      })
      .catch(function () { /* тихо: офлайн или хаб недоступен */ })
      .then(function () { busy = false; });
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', sync);
  else sync();
  document.addEventListener('visibilitychange', function () { if (document.visibilityState === 'visible') sync(); });
})();
