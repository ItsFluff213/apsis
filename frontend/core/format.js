// Number and unit formatting shared across every view.
//
// Part of core/, which is deliberately dependency-free: the standalone
// satellites page imports from here too, and the whole point of that page
// being separable is that taking core/ with it is enough.

export function fmt(n, digits = 0) {
  if (n === undefined || n === null || Number.isNaN(n)) return "-";
  return Number(n).toLocaleString(undefined, { maximumFractionDigits: digits });
}

export function fmtKm(metres, digits = 0) {
  if (metres === undefined || metres === null || Number.isNaN(metres)) return "-";
  return `${fmt(metres / 1000, digits)} km`;
}

// Durations here are game time, where a Kerbin day is 6 hours and a year
// is 426 days -- so "3 days" from an interplanetary transfer estimate means
// 18 hours of game clock, not 72. Showing raw seconds for a 300-day
// transfer is useless, hence the unit ladder.
const KERBIN_DAY_S = 21600;
const KERBIN_YEAR_S = KERBIN_DAY_S * 426;

export function fmtDuration(seconds) {
  if (seconds === undefined || seconds === null || Number.isNaN(seconds)) return "-";
  const s = Math.max(0, seconds);
  if (s < 60) return `${Math.round(s)}s`;
  if (s < 3600) return `${Math.floor(s / 60)}m ${Math.round(s % 60)}s`;
  if (s < KERBIN_DAY_S) return `${Math.floor(s / 3600)}h ${Math.round((s % 3600) / 60)}m`;
  if (s < KERBIN_YEAR_S) {
    const days = Math.floor(s / KERBIN_DAY_S);
    return `${days}d ${Math.round((s % KERBIN_DAY_S) / 3600)}h`;
  }
  const years = Math.floor(s / KERBIN_YEAR_S);
  return `${years}y ${Math.floor((s % KERBIN_YEAR_S) / KERBIN_DAY_S)}d`;
}

export function fmtSpeed(ms, digits = 0) {
  return `${fmt(ms, digits)} m/s`;
}
