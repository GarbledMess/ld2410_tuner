// Stored times to the millisecond, as the server's CSV writes them (presentation/recorded_csv.py).
export function rawTimestamp(value) {
  return new Date(Math.round(value * 1000)).toISOString();
}
