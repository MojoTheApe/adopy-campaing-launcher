// ACL-141: свои окна вместо alert/confirm/prompt браузера.
//
// Браузерное окно подписано «127.0.0.1:9193 says» и выглядит как что-то, что
// случилось с дешбордом, а не вопрос дешборда. Здесь один <dialog> в стиле
// страницы: заголовок, текст, основная и тихая кнопки, Escape = отмена, фокус
// туда, где человек продолжит (поле ввода, основная кнопка или — для
// необратимого — «Cancel»).
//
// Окна асинхронные: aclConfirm/aclPrompt возвращают Promise. Два окна разом не
// открываются — следующее ждёт, пока закроют предыдущее.
//
// aclInlineError(anchor, text) — ошибка красной строкой рядом с тем, что не
// сработало (ACL-141b): страницу можно разглядывать, ничего не закрывая.
(function (root) {
  'use strict';
  var doc = root.document;
  var queue = Promise.resolve();

  function el(tag, cls, text) {
    var n = doc.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    return n;
  }

  // Текст старых окон: абзацы через пустую строку, переносы — через \n.
  // Если заголовка нет, первый абзац становится заголовком (обычно это сам
  // вопрос: «Retire DE?»), остальное — пояснением.
  function split(message, title) {
    var paras = String(message == null ? '' : message).split(/\n\s*\n/)
      .map(function (p) { return p.replace(/^\s+|\s+$/g, ''); })
      .filter(function (p) { return p; });
    if (title == null) title = paras.length ? paras.shift() : '';
    return { title: String(title), paras: paras };
  }

  function fillBody(box, paras) {
    paras.forEach(function (p) {
      var node = el('p', 'acl-dlg-text');
      p.split('\n').forEach(function (line, i) {
        if (i) node.appendChild(doc.createElement('br'));
        node.appendChild(doc.createTextNode(line));
      });
      box.appendChild(node);
    });
  }

  // kind: 'alert' | 'confirm' | 'prompt'. Отдаёт значение, с которым закрыли:
  // alert → undefined, confirm → true/false, prompt → строка или null.
  function open(kind, message, opts) {
    opts = opts || {};
    return new Promise(function (resolve) {
      var parts = split(message, opts.title);
      var dlg = el('dialog', 'acl-dlg' + (opts.danger ? ' acl-dlg-danger' : ''));
      dlg.setAttribute('aria-labelledby', 'acl-dlg-title');
      var h = el('h3', 'acl-dlg-title', parts.title);
      h.id = 'acl-dlg-title';
      dlg.appendChild(h);
      var body = el('div', 'acl-dlg-body');
      body.id = 'acl-dlg-body';
      fillBody(body, parts.paras);
      dlg.appendChild(body);
      if (parts.paras.length) dlg.setAttribute('aria-describedby', 'acl-dlg-body');

      var field = null;
      if (kind === 'prompt') {
        var label = el('label', 'acl-dlg-label', opts.label || 'Value');
        field = el('input', 'acl-dlg-input');
        field.type = 'text';
        field.id = 'acl-dlg-input';
        field.value = opts.value == null ? '' : String(opts.value);
        if (opts.placeholder) field.placeholder = opts.placeholder;
        field.autocomplete = 'off';
        label.htmlFor = field.id;
        dlg.appendChild(label);
        dlg.appendChild(field);
      }

      var bar = el('div', 'acl-dlg-actions');
      var cancel = null;
      if (kind !== 'alert') {
        cancel = el('button', 'acl-dlg-btn acl-dlg-quiet', opts.cancelLabel || 'Cancel');
        cancel.type = 'button';
        bar.appendChild(cancel);
      }
      var ok = el('button', 'acl-dlg-btn acl-dlg-primary',
        opts.okLabel || (kind === 'alert' ? 'OK' : kind === 'prompt' ? 'Save' : 'Continue'));
      ok.type = 'button';
      bar.appendChild(ok);
      dlg.appendChild(bar);

      var before = doc.activeElement;
      var finished = false;
      function done(value) {
        if (finished) return;
        finished = true;
        try { dlg.close(); } catch (e) { /* уже закрыт */ }
        if (dlg.parentNode) dlg.parentNode.removeChild(dlg);
        if (before && typeof before.focus === 'function' && doc.contains(before)) {
          try { before.focus(); } catch (e) { /* элемент мог исчезнуть */ }
        }
        resolve(value);
      }
      var dismissed = kind === 'confirm' ? false : kind === 'prompt' ? null : undefined;
      ok.addEventListener('click', function () {
        done(kind === 'confirm' ? true : kind === 'prompt' ? field.value : undefined);
      });
      if (cancel) cancel.addEventListener('click', function () { done(dismissed); });
      // Escape: <dialog> присылает cancel. Закрываем сами, чтобы снять из DOM.
      dlg.addEventListener('cancel', function (ev) { ev.preventDefault(); done(dismissed); });
      if (field) {
        field.addEventListener('keydown', function (ev) {
          if (ev.key === 'Enter' && !ev.isComposing) { ev.preventDefault(); ok.click(); }
        });
      }

      doc.body.appendChild(dlg);
      if (typeof dlg.showModal === 'function') dlg.showModal();
      else dlg.setAttribute('open', '');
      if (field) { field.focus(); field.select(); }
      else if (cancel && opts.danger) cancel.focus();
      else ok.focus();
    });
  }

  // Нативное окно блокировало страницу, и второй клик по той же кнопке был
  // невозможен. Очередь его допускает — поэтому то же самое окно, пока первое
  // открыто или ждёт, второй раз не ставится: повтор сразу получает «отмену»,
  // и действие не выполнится дважды.
  var pending = {};

  function enqueue(kind, message, opts) {
    var key = kind + '\u0000' + ((opts && opts.title) || '') + '\u0000' + String(message);
    if (pending[key]) {
      return Promise.resolve(kind === 'confirm' ? false : kind === 'prompt' ? null : undefined);
    }
    pending[key] = true;
    var run = function () { return open(kind, message, opts); };
    var p = queue.then(run, run);
    var release = function () { delete pending[key]; };
    p.then(release, release);
    queue = p.then(function () {}, function () {});
    return p;
  }

  root.aclAlert = function (message, opts) { return enqueue('alert', message, opts); };
  root.aclConfirm = function (message, opts) { return enqueue('confirm', message, opts); };
  root.aclPrompt = function (message, opts) { return enqueue('prompt', message, opts); };

  // Красная строка сразу за anchor (кнопкой, полем). Повторная ошибка там же
  // заменяет текст; aclInlineError(anchor, '') — убрать. Без anchor на странице
  // (кнопку уже перерисовали) — обычное окно, чтобы ошибка не потерялась.
  root.aclInlineError = function (anchor, message) {
    var key = anchor && anchor.getAttribute && (anchor.getAttribute('data-acl-err-id')
      || (function () {
        var id = 'acl-err-' + Math.random().toString(36).slice(2, 9);
        anchor.setAttribute('data-acl-err-id', id);
        return id;
      })());
    var line = key ? doc.querySelector('.acl-inline-err[data-acl-err-for="' + key + '"]') : null;
    if (!message) { if (line) line.remove(); return null; }
    if (!anchor || !doc.contains(anchor)) { root.aclAlert(message); return null; }
    if (!line) {
      line = el('span', 'acl-inline-err');
      line.setAttribute('role', 'alert');
      line.setAttribute('data-acl-err-for', key);
      anchor.insertAdjacentElement('afterend', line);
    }
    line.textContent = String(message);
    return line;
  };
})(window);
