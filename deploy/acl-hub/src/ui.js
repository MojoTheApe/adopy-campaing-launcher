// Страницы владельца. Без внешних ресурсов: всё отдаёт Worker.
const esc = (s) => String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

export function LOGIN_HTML(error = "") {
  return `<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>ACL Control Center — sign in</title>
<style>body{font:15px system-ui,sans-serif;background:#f6f7f9;color:#1d2330;display:grid;place-items:center;min-height:100vh;margin:0}
form{background:#fff;border:1px solid #e3e6eb;border-radius:12px;padding:28px;width:min(360px,90vw)}h1{font-size:18px;margin:0 0 16px}
input{width:100%;box-sizing:border-box;padding:10px;border:1px solid #cdd3dc;border-radius:8px;font:inherit}
button{margin-top:12px;width:100%;padding:10px;border:0;border-radius:8px;background:#2d5bff;color:#fff;font:600 15px system-ui}
.err{color:#c62828;margin:0 0 10px}</style></head><body>
<form method="post" action="/login"><h1>ACL Control Center</h1>${error ? `<p class="err">${esc(error)}</p>` : ""}
<input type="password" name="key" placeholder="Owner key" autocomplete="current-password" autofocus required>
<button type="submit">Sign in</button></form></body></html>`;
}

// Task 3 наполняет.
export const PAGE_HTML = "<!doctype html><title>ACL Control Center</title>";
