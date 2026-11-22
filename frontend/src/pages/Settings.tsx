import React, { useCallback, useEffect, useState } from 'react'
import { RotateCcw, RefreshCw, Save } from 'lucide-react'
import { apiClient, HealthResponse, TuningSnapshot } from '../api'
import { useWebSocket } from '../hooks/useWebSocket'

export const Settings: React.FC = () => {
  const [cur, setCur] = useState<TuningSnapshot | null>(null)
  const [minEdgeDraft, setMinEdgeDraft] = useState('')
  const [minAiWinDraft, setMinAiWinDraft] = useState('')
  const [savingStrategy, setSavingStrategy] = useState(false)
  const [resettingConfigDefaults, setResettingConfigDefaults] = useState(false)
  const [resettingHistory, setResettingHistory] = useState(false)
  const [health, setHealth] = useState<HealthResponse | null>(null)
  const [kalshiReconciling, setKalshiReconciling] = useState(false)
  const [kalshiReconcileMsg, setKalshiReconcileMsg] = useState<string | null>(null)

  const { data: wsData } = useWebSocket('')

  const load = useCallback(async () => {
    try {
      const [c, h] = await Promise.all([apiClient.getTuningState(), apiClient.getHealth()])
      setCur(c)
      setHealth(h)
    } catch {
      /* ignore */
    }
  }, [])

  useEffect(() => {
    void load()
  }, [load])

  useEffect(() => {
    if (!cur) return
    setMinEdgeDraft(String(Math.round(Number(cur.min_edge_to_buy_pct ?? 4))))
    setMinAiWinDraft(String(Math.round(Number(cur.min_ai_win_prob_buy_side_pct ?? 70))))
  }, [cur])

  useEffect(() => {
    if (!wsData) return
    if (wsData.type === 'tuning_update') {
      const patch = wsData.data
      if (!patch || typeof patch !== 'object') return
      setCur((p) =>
        p ? { ...p, ...(patch as Partial<TuningSnapshot>) } : (patch as TuningSnapshot),
      )
    }
  }, [wsData])

  const saveStrategyKnobs = async () => {
    const me = Number(minEdgeDraft)
    const ma = Number(minAiWinDraft)
    if (!Number.isFinite(me) || !Number.isFinite(ma)) {
      alert('Enter whole numbers for the minimum edge and the model confidence.')
      return
    }
    setSavingStrategy(true)
    try {
      const next = await apiClient.saveStrategyKnobs({
        min_edge_to_buy_pct: Math.round(me),
        min_ai_win_prob_buy_side_pct: Math.round(ma),
      })
      setCur(next)
    } catch (e: unknown) {
      const msg =
        e && typeof e === 'object' && 'response' in e
          ? (e as { response?: { data?: { detail?: string } } }).response?.data?.detail
          : null
      alert(typeof msg === 'string' ? msg : 'Failed to save strategy settings.')
    } finally {
      setSavingStrategy(false)
    }
  }

  const runKalshiReconcile = async () => {
    setKalshiReconciling(true)
    setKalshiReconcileMsg(null)
    try {
      const r = await apiClient.reconcileKalshiPortfolio()
      setKalshiReconcileMsg(
        `Open field updates: ${r.open_updates} · New opens imported from Kalshi: ${r.open_positions_imported} · ` +
          `Buy-order entry refreshes: ${r.open_entry_order_refreshes ?? 0} · Unrealized/mark refreshes: ${r.open_unrealized_refreshes ?? 0} · ` +
          `Portfolio settlement closes: ${r.settlement_portfolio_closes} · Settlement API closes: ${r.settlement_history_closes} · ` +
          `Flat-row reconciliations: ${r.flat_row_reconciliations} · ` +
          `Closure finalizations: ${r.closure_finalizations ?? 0}`,
      )
    } catch (e: unknown) {
      const msg = e && typeof e === 'object' && 'response' in e ? (e as { response?: { data?: { detail?: string } } }).response?.data?.detail : null
      setKalshiReconcileMsg(typeof msg === 'string' ? msg : 'Reconcile failed (check backend logs).')
    } finally {
      setKalshiReconciling(false)
    }
  }

  const restoreConfigurationDefaults = async () => {
    if (!cur) return
    const ok = window.confirm(
      'Restore strategy settings from your configuration file (.env) and built-in defaults?\n\n' +
        'The running bot picks these up immediately.',
    )
    if (!ok) return
    setResettingConfigDefaults(true)
    try {
      const next = await apiClient.resetTuningToConfigDefaults()
      setCur(next)
    } catch (e) {
      console.error('Restore configuration defaults error:', e)
      alert('Failed to restore defaults.')
    } finally {
      setResettingConfigDefaults(false)
    }
  }

  const resetHistory = async () => {
    const ok = window.confirm(
      'Reset history?\n\nThis deletes ALL closed positions and resets realized P&L + closed-position stats. ' +
        'Open positions and unrealized P&L are NOT affected.\n\nStrategy settings are also reset to configuration defaults.',
    )
    if (!ok) return
    setResettingHistory(true)
    try {
      await apiClient.resetHistory()
      await load()
    } catch (e) {
      console.error('Reset history error:', e)
      alert('Failed to reset history.')
    } finally {
      setResettingHistory(false)
    }
  }

  return (
    <div className="space-y-8">
      <div>
        <h1 className="text-3xl font-bold text-white tracking-tight">Settings</h1>
        <p className="text-white mt-1 text-sm">
          Live Kalshi trading · strategy knobs · reconcile · history
        </p>
      </div>

      <section className="ui-surface-sm p-5">
        <div className="flex flex-wrap items-start justify-between gap-4">
          <div className="min-w-0">
            <h2 className="text-base font-semibold text-white">Reconcile with Kalshi</h2>
            <p className="text-xs text-white mt-2 max-w-2xl">
              Syncs this live account with Kalshi (same one-shot path as the bot and positions API): portfolio
              positions, settlement reconciliation, and flat-row alignment for closed rows. API host is{' '}
              <code className="text-cyan-200/90">KALSHI_BASE_URL</code> in{' '}
              <code className="text-cyan-200/90">backend/.env</code> (default{' '}
              <code className="text-cyan-200/90">https://external-api.kalshi.com</code>).
            </p>
            {kalshiReconcileMsg && (
              <p className={`text-xs mt-2 ${kalshiReconcileMsg.includes('failed') ? 'text-red-400' : 'text-white'}`}>
                {kalshiReconcileMsg}
              </p>
            )}
          </div>
          <button
            type="button"
            onClick={() => void runKalshiReconcile()}
            disabled={kalshiReconciling || health?.status !== 'healthy'}
            className={`shrink-0 inline-flex items-center gap-2 px-4 py-2.5 rounded-lg text-sm font-semibold border transition ${
              !kalshiReconciling && health?.status === 'healthy'
                ? 'bg-cyan-600/20 text-cyan-200 border-cyan-500/40 hover:bg-cyan-600/30'
                : 'bg-brand-muted/12 text-white border-brand-muted/30 cursor-not-allowed'
            }`}
          >
            <RefreshCw className={`w-4 h-4 ${kalshiReconciling ? 'animate-spin' : ''}`} aria-hidden />
            {kalshiReconciling ? 'Reconciling…' : 'Reconcile now'}
          </button>
        </div>
      </section>

      <section className="ui-surface-sm p-5 space-y-2">
        <h2 className="text-base font-semibold text-white">AI market analysis</h2>
        <p className="text-xs text-white/90 max-w-3xl">
          Market analysis uses <strong className="text-white">xAI Grok</strong>. Set{' '}
          <code className="text-cyan-200/90">XAI_API_KEY</code> and optional{' '}
          <code className="text-cyan-200/90">XAI_MODEL</code> in{' '}
          <code className="text-cyan-200/90">backend/.env</code>
          {cur?.xai_model ? (
            <>
              {' '}
              (running model: <code className="text-cyan-200/90">{cur.xai_model}</code>).
            </>
          ) : (
            <> (default <code className="text-cyan-200/90">grok-4.3</code>).</>
          )}{' '}
          Default <code className="text-cyan-200/90">grok-4.3</code> is the cost-efficient scan model; use{' '}
          <code className="text-cyan-200/90">grok-4.7</code> for flagship quality. Restart the backend after
          changing model or keys. Optional <code className="text-cyan-200/90">XAI_TEAM_ID</code> and{' '}
          <code className="text-cyan-200/90">XAI_MANAGEMENT_API_KEY</code> enable the prepaid balance tile on the
          dashboard.
        </p>
      </section>

      <section className="grid grid-cols-1 lg:grid-cols-2 gap-6">
        <div className="ui-surface-sm p-5 space-y-4">
          <h2 className="text-base font-semibold text-white">Trading strategy</h2>
          <p className="text-xs text-white/90">
            Each buy spends the edge, in points, as a percent of available cash (a 10-point edge uses 10% of cash), rounded down to whole contracts. Set <code className="text-cyan-200/90">FLAT_STAKE_DOLLARS</code> above 0 to use a fixed dollar amount instead. If that amount cannot buy one contract, the bot skips.
            Buys need your minimum edge and at least 70% model confidence. The price must be between 50¢ and 85¢. Edges above <code className="text-cyan-200/90">MAX_EDGE_TO_BUY_PCT</code> (22, or 0 to turn that cap off) are skipped.
            The bot holds until Kalshi settles the market. Live web search is off unless <code className="text-cyan-200/90">VIBE_LIVE_SEARCH_ENABLED</code> is true. Analysis stops for the Denver day when recorded AI spend hits <code className="text-cyan-200/90">DAILY_XAI_BUDGET_USD</code> (0 means no cap).
          </p>
          <p className="text-xs text-amber-200/90 border border-amber-500/25 rounded-lg px-3 py-2 bg-amber-500/10">
            Numbers here are stored in the <strong className="text-white">database</strong>, not read
            live from <code className="text-cyan-200/90">backend/.env</code>. After editing <code className="text-cyan-200/90">.env</code>, use{' '}
            <strong className="text-white">Restore configuration defaults</strong> below so SQLite matches your file, or type the numbers and <strong className="text-white">Save strategy settings</strong>.
          </p>

          {!cur ? (
            <p className="text-sm text-white">Loading…</p>
          ) : (
            <>
              <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-3 items-stretch">
                <label className="flex min-h-0 flex-col gap-1">
                  <span className="block min-h-[3rem] text-xs leading-snug text-white/80">
                    Minimum edge to buy (percentage points)
                  </span>
                  <input
                    type="text"
                    inputMode="numeric"
                    value={minEdgeDraft}
                    onChange={(e) => setMinEdgeDraft(e.target.value)}
                    className="w-full bg-primary/50 border border-brand-muted/30 rounded-lg px-3 py-2 text-sm text-white focus:outline-none focus:ring-2 focus:ring-blue-500/40"
                  />
                </label>
                <label className="flex min-h-0 flex-col gap-1">
                  <span className="block min-h-[3rem] text-xs leading-snug text-white/80">
                    Minimum model confidence on the bought side (51–99)
                  </span>
                  <input
                    type="text"
                    inputMode="numeric"
                    value={minAiWinDraft}
                    onChange={(e) => setMinAiWinDraft(e.target.value)}
                    className="w-full bg-primary/50 border border-brand-muted/30 rounded-lg px-3 py-2 text-sm text-white focus:outline-none focus:ring-2 focus:ring-blue-500/40"
                  />
                </label>
              </div>
              <button
                type="button"
                disabled={savingStrategy}
                onClick={() => void saveStrategyKnobs()}
                className="inline-flex items-center gap-2 px-4 py-2 rounded-lg text-sm font-semibold border bg-blue-600/20 text-blue-100 border-blue-500/35 hover:bg-blue-600/30 disabled:opacity-50"
              >
                <Save className="w-4 h-4" />
                {savingStrategy ? 'Saving…' : 'Save strategy settings'}
              </button>
              <p className="text-[11px] text-white/70">
                Saved values apply immediately. Restore configuration defaults reloads the edge and the 70% confidence floor from <code className="text-cyan-200/90">backend/.env</code>. Editing the file alone does not overwrite the database.
              </p>
              <p className="text-[11px] text-white/70">
                {cur.config_source_note ||
                  'The numbers on this screen come from the database. The .env file applies when that row is missing, or after Restore configuration defaults.'}
              </p>

              <button
                type="button"
                disabled={resettingConfigDefaults}
                onClick={() => void restoreConfigurationDefaults()}
                className="inline-flex items-center gap-2 px-4 py-2 rounded-lg text-sm font-semibold border bg-sky-500/15 text-sky-200 border-sky-500/35 hover:bg-sky-500/25 disabled:opacity-50"
              >
                <RefreshCw className={`w-4 h-4 ${resettingConfigDefaults ? 'animate-spin' : ''}`} />
                {resettingConfigDefaults ? 'Restoring…' : 'Restore configuration defaults'}
              </button>
            </>
          )}
        </div>

        <div className="ui-surface-sm p-5 space-y-4">
          <h2 className="text-base font-semibold text-white">History</h2>
          <p className="text-xs text-white/90">
            Deletes all closed live positions. Open legs are untouched. Strategy settings reset to config
            defaults.
          </p>
          <button
            type="button"
            disabled={resettingHistory}
            onClick={() => void resetHistory()}
            className="w-full flex items-center justify-center gap-2 px-4 py-2.5 rounded-lg text-sm font-semibold border bg-red-500/15 text-red-300 border-red-500/30 hover:bg-red-500/20 disabled:opacity-50"
          >
            <RotateCcw className={`w-4 h-4 ${resettingHistory ? 'animate-spin' : ''}`} />
            {resettingHistory ? 'Resetting…' : 'Reset closed-position history'}
          </button>
          <button
            type="button"
            onClick={() => void load()}
            className="w-full px-4 py-2.5 bg-secondary hover:bg-brand-muted/30 border border-brand-muted/25 text-white rounded-lg text-sm"
          >
            Refresh settings
          </button>
        </div>
      </section>
    </div>
  )
}

export default Settings
