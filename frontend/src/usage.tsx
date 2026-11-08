// Filament used by every print (kept by the server), booked on the spools when the print
// ends; "Book" books a line that could not be booked then, also days later.
import { Check, CloudUpload, EyeOff, Loader2, TriangleAlert } from 'lucide-react'
import { useEffect, useState } from 'react'
import { api, type ApiError } from './api'
import type { Snapshot } from './types'
import { Swatch, useDialogs } from './ui'

export interface UsageItem {
  tray: number
  label: string
  grams: number
  spool_id: number | null
  spool_name: string
  color: string
  state: 'checking' | 'booked' | 'missing' | 'synced' | 'local' | 'no_spool'
}
export interface UsageRec {
  id: string
  job: string
  result: string
  ended_at: number
  archive_id: number
  items: UsageItem[]
  ignored: boolean
}

const BADGE: Record<UsageItem['state'], [string, string]> = {
  checking: ['Not booked yet', 'text-muted'],
  booked: ['Booked', 'text-accent'],
  missing: ['NOT booked', 'text-error'],
  synced: ['Booked', 'text-accent'],
  local: ['Booked', 'text-accent'],
  no_spool: ['No spool assigned', 'text-muted'],
}

export function useUsage(snap: Snapshot): UsageRec[] | null {
  const [items, setItems] = useState<UsageRec[] | null>(null)
  useEffect(() => {
    api<{ items: UsageRec[] }>('/api/usage')
      .then((d) => setItems(d.items))
      .catch(() => setItems([]))
  }, [snap.usage_version])
  return items
}

export function UsageCard({ r }: { r: UsageRec }) {
  const dlg = useDialogs()
  const [busy, setBusy] = useState('')
  const missing = r.items.filter((i) => i.state === 'missing')
  const checking = r.items.some((i) => i.state === 'checking')
  const act = async (action: string) => {
    if (action === 'sync') {
      const ok = await dlg.confirm({
        title: 'Book it now?',
        text: `Adds the filament this print used to the spools: ${missing.map((i) => `${i.label} ${i.grams} g`).join(', ')}.`,
        ok: 'Book it',
      })
      if (!ok) return
    }
    setBusy(action)
    try {
      const res = await api<{ ok: boolean; message: string }>(`/api/usage/${r.id}/${action}`, { body: {} })
      if (!res.ok && res.message) dlg.toast(false, res.message)
    } catch (e) {
      dlg.toast(false, (e as ApiError).message)
    }
    setBusy('')
  }
  return (
    <div className={`rounded-xl border-l-4 bg-card px-4 py-3 ${r.ignored ? 'opacity-60' : ''}`} style={{ borderColor: missing.length && !r.ignored ? 'var(--color-error)' : 'var(--color-border)' }}>
      <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
        <span className="min-w-0 flex-1 truncate font-semibold">{r.job}</span>
        <span className="text-xs text-muted">
          {new Date(r.ended_at * 1000).toLocaleString('en-GB', { day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit' })}
          {r.result && r.result !== 'FINISH' && ` · ${r.result.toLowerCase()} (used until then)`}
        </span>
      </div>
      <div className="mt-2 flex flex-col gap-1.5">
        {r.items.map((i) => (
          <div key={i.tray} className="flex items-center gap-2.5 text-sm">
            <Swatch color={i.color} size={18} />
            <span className="w-9 font-bold">{i.label}</span>
            <span className="min-w-0 flex-1 truncate text-muted">{i.spool_name || '-'}</span>
            <span className="font-semibold tabular-nums">{i.grams.toFixed(1)} g</span>
            <span className={`w-36 text-right text-xs font-bold ${BADGE[i.state][1]}`}>
              {i.state === 'booked' || i.state === 'synced' || i.state === 'local' ? <Check size={12} className="mr-1 inline" /> : null}
              {i.state === 'missing' ? <TriangleAlert size={12} className="mr-1 inline" /> : null}
              {BADGE[i.state][0]}
            </span>
          </div>
        ))}
      </div>
      {(missing.length > 0 || checking) && (
        <div className="mt-3 flex flex-wrap gap-2">
          {!r.ignored && (
            <button className="btn-accent h-9 text-sm" disabled={!!busy} onClick={() => act('sync')}>
              {busy === 'sync' ? <Loader2 size={16} className="animate-spin" /> : <CloudUpload size={16} />} Book
            </button>
          )}
          {missing.length > 0 && (
            <button className="btn h-9 text-sm" disabled={!!busy} onClick={() => act(r.ignored ? 'unignore' : 'ignore')}>
              <EyeOff size={16} /> {r.ignored ? 'Show again' : 'Ignore'}
            </button>
          )}
        </div>
      )}
    </div>
  )
}

/** AMS page: the recent prints' filament use, unbooked ones first. */
export function UsageList({ snap }: { snap: Snapshot }) {
  const items = useUsage(snap)
  const [all, setAll] = useState(false)
  if (!items || !items.length) return null
  const visible = items.filter((r) => all || !r.ignored).slice(0, all ? 50 : 6)
  return (
    <section className="flex flex-col gap-2">
      <div className="flex items-center gap-2">
        <h2 className="section-title mb-0!">Print usage</h2>
        {snap.usage_pending > 0 && <span className="rounded-md bg-error/20 px-1.5 py-0.5 text-[11px] font-bold text-error">{snap.usage_pending} not booked</span>}
        <span className="flex-1" />
        <button className="text-xs font-bold text-muted hover:text-text" onClick={() => setAll(!all)}>
          {all ? 'Fewer' : 'All / ignored'}
        </button>
      </div>
      <p className="text-xs text-muted">
        Filament each print used, per spool. It is booked on the spool when the print ends; if that failed, book it from here.
      </p>
      {visible.map((r) => (
        <UsageCard key={r.id} r={r} />
      ))}
    </section>
  )
}
