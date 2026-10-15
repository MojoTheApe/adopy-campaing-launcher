"""ACL-141: окна дешборда — свои <dialog class="acl-dlg">, а не alert/confirm/
prompt браузера, и Playwright-событие 'dialog' о них не знает.

capture_dialogs(page, sink) — то же, что раньше давал
`page.on('dialog', lambda d: (sink.append(d.message), d.accept()))`:
каждое окно страницы попадает в sink текстом «заголовок + пояснение» и
закрывается основной кнопкой (OK / «да»). answer(text) — что ответить:
False — Cancel; для окна ввода строка — что вписать, None — Cancel.
Вызывать до page.goto.
"""

_SCRIPT = """
(() => {
  const seen = new WeakSet();
  const handle = (dlg) => {
    if (seen.has(dlg)) return;
    seen.add(dlg);
    const title = (dlg.querySelector('.acl-dlg-title') || {}).textContent || '';
    const body = Array.from(dlg.querySelectorAll('.acl-dlg-text')).map((p) => p.innerText).join('\\n\\n');
    const text = body ? title + '\\n\\n' + body : title;
    const field = dlg.querySelector('.acl-dlg-input');
    Promise.resolve(window.__aclDialogSeen(text)).then((answer) => {
      if (!dlg.isConnected) return;
      const quiet = dlg.querySelector('.acl-dlg-quiet');
      if (quiet && (answer === false || (field && answer === null))) { quiet.click(); return; }
      if (field && typeof answer === 'string') field.value = answer;
      dlg.querySelector('.acl-dlg-primary').click();
    });
  };
  new MutationObserver((records) => {
    for (const r of records) for (const n of r.addedNodes) {
      if (n.nodeType === 1 && n.matches && n.matches('dialog.acl-dlg')) handle(n);
    }
  }).observe(document, { childList: true, subtree: true });
})();
"""


def capture_dialogs(page, sink, answer=None, answer_js=None):
    """answer_js — то же, что answer, но функцией JS внутри страницы и без
    expose_function (sink тогда не пополняется): так в ручном скрипте
    browser_review_cards.py, где привязка Python-функции вешает загрузку."""
    if answer_js is not None:
        page.add_init_script('window.__aclDialogSeen = ' + answer_js + ';\n' + _SCRIPT)
        return

    def seen(text):
        sink.append(text)
        if answer is None:
            return True
        return answer(text)

    page.expose_function('__aclDialogSeen', seen)
    page.add_init_script(_SCRIPT)
