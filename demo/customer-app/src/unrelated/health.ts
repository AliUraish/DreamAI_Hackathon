export function health() {
  return { ok: true, uptime: process.uptime() };
}
