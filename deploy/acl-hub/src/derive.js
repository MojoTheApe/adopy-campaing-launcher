// Выводы из событий: машины, задания, прогоны кампаний. Каждое событие апсертит только свои
// колонки, поэтому итог не зависит от порядка событий внутри и между пачками.

// Любое событие с machine_id: машина, её последняя подпись и время. null — если машины нет.
export function machineStatement(db, ev) {
  if (!ev.machine_id) return null;
  return db.prepare(
    "INSERT INTO machines (machine_id, machine, manager, flow_version, first_seen_at, last_event_at) "
    + "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(machine_id) DO UPDATE SET "
    + "machine = CASE WHEN excluded.last_event_at >= coalesce(machines.last_event_at, '') "
    + "  THEN coalesce(excluded.machine, machines.machine) ELSE machines.machine END, "
    + "manager = CASE WHEN excluded.last_event_at >= coalesce(machines.last_event_at, '') "
    + "  THEN coalesce(excluded.manager, machines.manager) ELSE machines.manager END, "
    + "flow_version = CASE WHEN excluded.last_event_at >= coalesce(machines.last_event_at, '') "
    + "  THEN coalesce(excluded.flow_version, machines.flow_version) ELSE machines.flow_version END, "
    + "first_seen_at = min(coalesce(machines.first_seen_at, excluded.first_seen_at), excluded.first_seen_at), "
    + "last_event_at = max(coalesce(machines.last_event_at, ''), excluded.last_event_at)",
  ).bind(ev.machine_id, ev.machine ?? null, ev.manager ?? null, ev.flow_version ?? null, ev.ts, ev.ts);
}

// Task 4 наполняет.
export async function deriveStatements(db, ev) {
  return [];
}
