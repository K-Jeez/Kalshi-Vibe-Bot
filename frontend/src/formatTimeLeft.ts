import type { Position } from './api'

/** Trading-closes clock. Contractual ``close_time`` wins. Expected expiration is only a hint. */
export function positionEndsAtIso(
  p: Pick<Position, 'ends_at' | 'expected_expiration_time' | 'close_time' | 'kalshi_market_status'>,
  nowMs: number = Date.now(),
): string | undefined {
  void nowMs
  const ct = (p.close_time ?? '').trim()
  if (ct) return ct
  const exp = (p.expected_expiration_time ?? '').trim()
  if (exp) return exp
  return (p.ends_at ?? '').trim() || undefined
}

export function tradingClosesSource(
  p: Pick<Position, 'close_time' | 'expected_expiration_time'>,
): 'close_time' | 'expected_expiration_time' | 'unknown' {
  if ((p.close_time ?? '').trim()) return 'close_time'
  if ((p.expected_expiration_time ?? '').trim()) return 'expected_expiration_time'
  return 'unknown'
}

/** Tradeable row whose Kalshi ``expected_expiration_time`` peg is already in the past (schedule slip / live play). */
export function tradeableExpectedExpirationPassed(
  p: Pick<Position, 'kalshi_market_status' | 'expected_expiration_time'>,
  nowMs: number,
): boolean {
  const st = (p.kalshi_market_status ?? '').trim().toLowerCase()
  if (st !== 'active' && st !== 'open') return false
  const raw = (p.expected_expiration_time ?? '').trim()
  if (!raw) return false
  const peg = Date.parse(raw)
  if (Number.isNaN(peg)) return false
  return peg <= nowMs
}

/** Sort key uses the same trading-closes clock as the label. */
export function positionSortEndsAtIso(
  p: Pick<Position, 'ends_at' | 'expected_expiration_time' | 'close_time' | 'kalshi_market_status'>,
  nowMs: number = Date.now(),
): string | undefined {
  return positionEndsAtIso(p, nowMs)
}

/** True when ``closeTimeIso`` parses and is not after ``nowMs``. */
export function isCloseTimePassed(closeTimeIso: string | undefined, nowMs: number): boolean {
  if (!closeTimeIso?.trim()) return false
  const end = Date.parse(closeTimeIso)
  if (Number.isNaN(end)) return false
  return end - nowMs <= 0
}

/** Compact label for table columns (e.g. ``10.6h``, ``1.2d``, ``45m``). */
export function formatTimeLeft(closeTimeIso: string | undefined, nowMs: number): string {
  if (!closeTimeIso?.trim()) return '—'
  const end = Date.parse(closeTimeIso)
  if (Number.isNaN(end)) return '—'
  const ms = end - nowMs
  if (ms <= 0) return 'Ended'
  const days = ms / 86_400_000
  if (days >= 1) return `${days.toFixed(1)}d`
  const hours = ms / 3_600_000
  if (hours >= 1) return `${hours.toFixed(1)}h`
  const mins = Math.max(1, Math.round(ms / 60_000))
  return `${mins}m`
}
