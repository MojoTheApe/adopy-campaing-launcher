/**
 * ACL — приём телеметрии прогонов в эту таблицу.
 *
 * Зачем так, а не сервисным аккаунтом: политика Workspace в Adopy запрещает
 * делиться документами с доменами вне белого списка, а у сервисного аккаунта домен
 * внешний (…iam.gserviceaccount.com) — расшарить на него таблицу нельзя.
 * Этот скрипт живёт ВНУТРИ таблицы и выполняется от имени её владельца,
 * поэтому ничего никому расшаривать не нужно, а на машинах байеров не нужны
 * ключи — только адрес развёртывания.
 *
 * УСТАНОВКА (один раз, в самой таблице):
 *   1. Расширения → Apps Script
 *   2. Удалить содержимое файла Code.gs, вставить этот текст
 *   3. Заменить PASTE_TOKEN_HERE на токен, который лежит у вас в .env
 *      в переменной ACL_TELEMETRY_WEBHOOK_TOKEN
 *   4. Сохранить, затем: Развернуть → Новое развёртывание → тип «Веб-приложение»
 *        Выполнять от имени : Я
 *        У кого есть доступ : Все / Anyone
 *      (машины байеров не авторизуются в Google, поэтому доступ «только для
 *       Adopy» тут не сработает — их запрос анонимный)
 *   5. Скопировать URL развёртывания (…/exec) и отдать его агенту —
 *      он впишет его в .env как ACL_TELEMETRY_WEBHOOK_URL
 *
 * Токен — единственная защита этого адреса: сам по себе URL даёт право
 * дописывать строки. Он не секрет уровня пароля от аккаунта, но и публиковать
 * его не нужно.
 */

var TOKEN = 'PASTE_TOKEN_HERE';

function doPost(e) {
  try {
    var body = JSON.parse(e.postData.contents);

    if (TOKEN && body.token !== TOKEN) {
      return _json({ ok: false, error: 'bad token' });
    }

    var rows = body.rows || [];
    if (!rows.length) {
      return _json({ ok: true, written: 0 });
    }

    var ss = SpreadsheetApp.getActiveSpreadsheet();
    var name = body.tab || 'rpa_telemetry';
    var sh = ss.getSheetByName(name);
    if (!sh) {
      sh = ss.insertSheet(name);
      if (body.header && body.header.length) {
        sh.appendRow(body.header);
        sh.setFrozenRows(1);
      }
    }

    // setValues одним блоком, а не appendRow в цикле: на первой выгрузке с
    // машины приходят тысячи строк, и построчная запись упирается в лимит
    // времени выполнения Apps Script.
    sh.getRange(sh.getLastRow() + 1, 1, rows.length, rows[0].length).setValues(rows);

    return _json({ ok: true, written: rows.length });
  } catch (err) {
    return _json({ ok: false, error: String(err) });
  }
}

function doGet() {
  // Чтобы открытие URL в браузере давало понятный ответ, а не ошибку.
  return _json({ ok: true, service: 'ACL telemetry sink', hint: 'POST only' });
}

function _json(obj) {
  return ContentService
    .createTextOutput(JSON.stringify(obj))
    .setMimeType(ContentService.MimeType.JSON);
}
