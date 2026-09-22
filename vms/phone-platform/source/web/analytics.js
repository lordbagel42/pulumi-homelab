// Derive analytics from the returned session inventory; never synthesize traffic.
export function sessionAnalytics(sessions, hours, now = Date.now()) {
  const count = hours === 24 ? 24 : hours === 168 ? 7 : 30;
  const width = hours * 3600000 / count;
  const end = Math.floor(now / width) * width + width;
  const start = end - count * width;
  const bins = Array.from({length: count}, (_, i) => ({start: start + i * width, end: start + (i + 1) * width, count: 0}));
  let previous = 0, missing = 0;
  for (const session of sessions) {
    const raw = session.created;
    const date = raw == null ? NaN : new Date(typeof raw === 'number' && raw < 1e12 ? raw * 1000 : raw).getTime();
    if (!Number.isFinite(date)) { missing++; continue; }
    if (date > now) continue;
    if (date >= start && date < end) bins[Math.floor((date-start)/width)].count++;
    else if (date >= start - count * width && date < start) previous++;
  }
  return {bins, total: bins.reduce((n,b) => n+b.count,0), previous, missing};
}
