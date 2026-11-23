// Общий JSON-ответ (отдельный модуль, чтобы роутер и обработчики не импортировали друг друга).
export const json = (obj, status = 200, headers = {}) =>
  new Response(JSON.stringify(obj), { status, headers: { "content-type": "application/json", ...headers } });
