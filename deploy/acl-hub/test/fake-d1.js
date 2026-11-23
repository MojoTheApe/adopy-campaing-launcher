// Поддельная D1 для тестов поверх встроенного node:sqlite (Node 22.5+).
// Повторяет ту часть API D1, которой пользуется Worker: prepare/bind/run/all/first, batch, exec.
import { DatabaseSync } from "node:sqlite";
import { readdirSync, readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { join } from "node:path";

class FakeStatement {
  constructor(db, sql, args = []) { this.db = db; this.sql = sql; this.args = args; }
  bind(...args) { return new FakeStatement(this.db, this.sql, args.map((a) => (a === undefined ? null : a))); }
  _stmt() { return this.db.raw.prepare(this.sql); }
  _run() {
    const info = this._stmt().run(...this.args);
    return { success: true, meta: { changes: Number(info.changes), last_row_id: Number(info.lastInsertRowid) } };
  }
  async run() { return this._run(); }
  async all() { return { success: true, results: this._stmt().all(...this.args).map((r) => ({ ...r })) }; }
  async first(column) {
    const row = this._stmt().get(...this.args);
    if (!row) return null;
    return column ? row[column] : { ...row };
  }
}

export class FakeD1 {
  constructor() { this.raw = new DatabaseSync(":memory:"); }
  applyMigrations(dirUrl) {
    const dir = fileURLToPath(dirUrl);
    for (const name of readdirSync(dir).filter((n) => n.endsWith(".sql")).sort()) {
      this.raw.exec(readFileSync(join(dir, name), "utf8"));
    }
    return this;
  }
  prepare(sql) { return new FakeStatement(this, sql); }
  async exec(sql) { this.raw.exec(sql); return { count: 1 }; }
  // D1 выполняет batch одной транзакцией: ошибка откатывает всё.
  async batch(statements) {
    this.raw.exec("BEGIN");
    try {
      const out = statements.map((s) => s._run());
      this.raw.exec("COMMIT");
      return out;
    } catch (err) {
      this.raw.exec("ROLLBACK");
      throw err;
    }
  }
}
