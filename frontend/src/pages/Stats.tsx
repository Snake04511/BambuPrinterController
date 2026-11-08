// STATS: statistics (all time + this month) and the print history with Obico scores
// (panel src/ui/pages/StatsPage.cpp, StatsView.cpp).
import { ChevronLeft, ChevronRight, Eye, Film, ImageOff, Loader2, RefreshCw, Trash2 } from 'lucide-react'
import { useCallback, useEffect, useState, type ReactNode } from 'react'
import { api, type ApiError } from '../api'
import { C } from '../format'
import type { Snapshot } from '../types'
import { TimelapseButton, TimelapsePlayer } from '../timelapse'
import { UsageCard, useUsage } from '../usage'
import { Modal, Swatch, useDialogs } from '../ui'

interface AllTime {
  total: number
  ok: number
  failed: number
  cancelled: number
  hours: number | null
  grams: number | null
  cost: number | null
  types: { type: string; prints: number }[]
}
interface Month {
  month: string
  days: number
  runs: number
  ok: number
  failed: number
  cancelled: number
  other: number
  seconds: number
  cost?: number
  timed: number
  grams: number
  weighed: number
  day_hours: number[]
  mixed_grams: number
  mixed_runs: number
  truncated: boolean
  materials: { type: string; grams: number; runs: number }[]
  colors: { color: string; type: string; grams: number; runs: number }[]
}
interface StatsData {
  ok: boolean
  error: string
  all: AllTime | null
  month: Month | null
  age: number
}
interface HistItem {
  id: number
  name: string
  status: string
  started: string
  actual_sec: number
  est_sec: number
  grams: number | null
  cost: number | null
  cost_filament?: number | null
  cost_energy?: number | null
  energy_kwh?: number | null
  type: string
  colors: string[]
  layers: number
  layer_height: number | null
  failure: string
  has_obico: boolean
  timelapse: string
  from_sd: boolean
  filaments?: { color: string; type: string; grams: number }[]
}
interface HistPage {
  ok: boolean
  error: string
  items: HistItem[]
  offset: number
  more: boolean
}
export interface ObicoRec {
  job: string
  samples: number
  warn: number
  fail: number
  dark: number
  max: number
  sum: number
  low: number
  high: number
  stride: number
  points: number[]
  start: number
  end: number
  result: string
}

const fmtHours = (h: number | null) => (h === null ? 'n/a' : h >= 100 ? `${Math.round(h)} h` : `${h.toFixed(1)} h`)
const fmtGrams = (g: number | null) => (g === null ? 'n/a' : g >= 1000 ? `${(g / 1000).toFixed(2)} kg` : `${Math.round(g)} g`)
const hm = (sec: number) => (sec < 0 ? '--' : `${Math.floor(sec / 3600)}h ${String(Math.floor((sec % 3600) / 60)).padStart(2, '0')}m`)

function statusColor(st: string): string {
  if (st === 'completed') return C.accent
  if (st === 'failed') return C.error
  if (st === 'printing') return C.info
  return C.warn
}

/** The print log's naive timestamps are UTC. */
function localTime(iso: string): string {
  if (!iso) return '--'
  const d = new Date(/[zZ]|[+-]\d\d:?\d\d$/.test(iso) ? iso : `${iso}Z`)
  if (isNaN(d.getTime())) return iso.slice(0, 16).replace('T', ' ')
  return d.toLocaleString('en-GB', { year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' }).replace(',', '')
}

/** "1 234 Ft" in the currency of Settings > Costs. */
export function money(v: number, snap: Snapshot | null): string {
  const cur = snap?.settings?.currency || 'Ft'
  return `${Math.round(v).toLocaleString('hu-HU')} ${cur}`
}

export default function Stats({ snap }: { snap: Snapshot | null }) {
  const [view, setView] = useState<'stats' | 'history' | 'timelapse'>(() => (location.hash.includes('history') ? 'history' : 'stats'))
  return (
    <div className="mx-auto flex max-w-[1300px] flex-col gap-4 p-3 md:p-4">
      <div className="flex gap-1 self-start rounded-xl bg-card p-1">
        {(['stats', 'history', 'timelapse'] as const).map((v) => (
          <button
            key={v}
            onClick={() => setView(v)}
            className={`rounded-lg px-4 py-2 text-sm font-bold ${view === v ? 'bg-card2 text-accent shadow-[0_0_14px_-6px_var(--color-accent)]' : 'text-muted hover:text-text'}`}
          >
            {v === 'stats' ? 'Statistics' : v === 'history' ? 'History' : 'Timelapses'}
          </button>
        ))}
      </div>
      {!snap ? (
        <div className="card p-8 text-center text-muted">Loading...</div>
      ) : view === 'stats' ? (
        <StatsView snap={snap} />
      ) : view === 'history' ? (
        <HistoryView snap={snap!} />
      ) : (
        <TimelapseList snap={snap!} />
      )}
    </div>
  )
}

// ---------------------------------------------------------------- statistics

function Card({ value, label, sub, color }: { value: string; label: string; sub?: ReactNode; color?: string }) {
  return (
    <div className="card min-w-0 p-4">
      <div className="text-[11px] font-bold tracking-wider text-muted uppercase">{label}</div>
      <div className="mt-1 text-3xl font-extrabold tabular-nums" style={{ color: color ?? C.text }}>
        {value}
      </div>
      {sub && <div className="mt-1 text-xs text-muted">{sub}</div>}
    </div>
  )
}

function Outcome({ ok, failed, cancelled }: { ok: number; failed: number; cancelled: number }) {
  return (
    <span className="flex flex-wrap gap-x-2">
      <span style={{ color: C.accent }}>{ok} ok</span>
      <span style={{ color: C.error }}>{failed} failed</span>
      <span>{cancelled} cancelled</span>
    </span>
  )
}

function Section({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section className="flex flex-col gap-3">
      <h2 className="section-title">{title}</h2>
      {children}
    </section>
  )
}

function StatsView({ snap }: { snap: Snapshot | null }) {
  const [d, setD] = useState<StatsData | null>(null)
  const [loading, setLoading] = useState(false)
  const load = useCallback((refresh: boolean) => {
    setLoading(true)
    api<StatsData>(`/api/stats${refresh ? '?refresh=1' : ''}`)
      .then(setD)
      .catch((e: ApiError) => setD({ ok: false, error: e.message, all: null, month: null, age: 0 }))
      .finally(() => setLoading(false))
  }, [])
  useEffect(() => load(false), [load])

  const a = d?.all
  const m = d?.month
  const decided = a ? a.ok + a.failed + a.cancelled : 0
  let status = 'Loading statistics...'
  if (d && !loading) status = d.error || (d.age >= 60 ? `Updated ${Math.round(d.age / 60)} min ago` : 'Up to date')
  return (
    <div className="flex flex-col gap-6">
      <div className="flex items-center gap-2 text-sm text-muted">
        {loading && <Loader2 size={14} className="animate-spin text-accent" />}
        <span className={d?.error ? 'text-error' : ''}>{status}</span>
        <button className="btn ml-auto px-3" onClick={() => load(true)} disabled={loading} title="Refresh">
          <RefreshCw size={18} className={loading ? 'animate-spin' : ''} />
        </button>
      </div>

      {a && (
        <Section title="All time">
          <div className="grid grid-cols-2 gap-3 lg:grid-cols-5">
            <Card value={String(a.total)} label="Prints" sub={<Outcome ok={a.ok} failed={a.failed} cancelled={a.cancelled} />} />
            <Card
              value={decided > 0 ? `${Math.round((100 * a.ok) / decided)}%` : 'n/a'}
              label="Success rate"
              sub="completed / finished runs"
              color={C.accent}
            />
            <Card
              value={fmtHours(a.hours)}
              label="Print time"
              sub={a.hours !== null && a.total > 0 ? `avg ${hm(((a.hours * 3600) / a.total) | 0)} / print` : 'avg n/a'}
            />
            <Card
              value={fmtGrams(a.grams)}
              label="Filament"
              sub={a.grams !== null && a.total > 0 ? `avg ${Math.round(a.grams / a.total)} g / print` : 'avg n/a'}
            />
            <Card
              value={a.cost ? money(a.cost, snap) : 'n/a'}
              label="Cost"
              sub={a.cost && a.total > 0 ? `avg ${money(a.cost / a.total, snap)} / print` : 'filament + power'}
            />
          </div>
          {a.types.length > 0 && (
            <div className="text-sm">
              <span className="text-muted">Prints by material: </span>
              {a.types.map((t) => (
                <span key={t.type} className="mr-4 font-semibold">
                  {t.type} {t.prints}
                </span>
              ))}
            </div>
          )}
        </Section>
      )}

      {d && (
        <Section title={`This month · ${m?.month ?? '--'}`}>
          {!m ? (
            <p className="text-muted">Not available</p>
          ) : (
            <>
              <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
                <Card value={String(m.runs)} label="Prints" sub={<Outcome ok={m.ok} failed={m.failed} cancelled={m.cancelled} />} />
                <Card
                  value={m.timed ? fmtHours(m.seconds / 3600) : m.runs ? 'n/a' : '0 h'}
                  label="Print time"
                  sub={m.timed ? `${m.timed} of ${m.runs} runs timed` : m.runs ? 'no run durations' : undefined}
                />
                <Card
                  value={m.weighed ? fmtGrams(m.grams) : m.runs ? 'n/a' : '0 g'}
                  label="Filament"
                  sub={m.weighed ? `${m.weighed} of ${m.runs} runs weighed` : m.runs ? 'no filament data' : undefined}
                />
                <Card value={m.cost ? money(m.cost, snap) : m.runs ? 'n/a' : '0'} label="Cost" sub="filament + power" />
              </div>
              <DayChart m={m} />
              <div className="grid gap-4 lg:grid-cols-2">
                <div className="card flex flex-col gap-2 p-4">
                  <div className="text-[11px] font-bold tracking-wider text-muted uppercase">Filament by material</div>
                  {!m.materials.length && !m.mixed_runs && <p className="text-sm text-muted">No filament data this month.</p>}
                  {m.materials.map((r) => (
                    <BarRow key={r.type} label={r.type} grams={r.grams} runs={r.runs} max={m.materials[0].grams} color={C.accent} />
                  ))}
                  {m.mixed_runs > 0 && (
                    <p className="text-xs text-muted">
                      Multi-material prints: {fmtGrams(m.mixed_grams)} in {m.mixed_runs} run(s) - one total per print, no split.
                    </p>
                  )}
                </div>
                <div className="card flex flex-col gap-2 p-4">
                  <div className="text-[11px] font-bold tracking-wider text-muted uppercase">Filament by colour · single-colour prints</div>
                  {!m.colors.length && <p className="text-sm text-muted">No single-colour prints this month.</p>}
                  {m.colors.map((r) => (
                    <BarRow
                      key={r.color + r.type}
                      label={
                        <span className="flex items-center gap-2">
                          <Swatch color={r.color} size={16} /> {r.type || '?'}
                        </span>
                      }
                      grams={r.grams}
                      runs={r.runs}
                      max={m.colors[0].grams}
                      color={r.color}
                    />
                  ))}
                </div>
              </div>
              {m.truncated && <p className="text-sm text-warn">Only the first 1000 runs of this month are counted.</p>}
            </>
          )}
        </Section>
      )}
    </div>
  )
}

function BarRow({ label, grams, runs, max, color }: { label: ReactNode; grams: number; runs: number; max: number; color: string }) {
  return (
    <div className="grid grid-cols-[minmax(0,7rem)_1fr_auto] items-center gap-3 text-sm">
      <span className="truncate font-semibold">{label}</span>
      <div className="h-2.5 overflow-hidden rounded-full bg-card2">
        <div className="h-full rounded-full" style={{ width: `${max > 0 ? Math.max(2, (100 * grams) / max) : 0}%`, background: color, boxShadow: `0 0 8px -2px ${color}` }} />
      </div>
      <span className="text-right text-muted tabular-nums">
        {fmtGrams(grams)} · {runs}×
      </span>
    </div>
  )
}

function DayChart({ m }: { m: Month }) {
  const max = Math.max(1, ...m.day_hours)
  const today = new Date().getDate()
  const [hover, setHover] = useState(-1)
  return (
    <div className="card p-4">
      <div className="mb-3 flex items-baseline justify-between gap-2 text-[11px] font-bold tracking-wider text-muted uppercase">
        <span>Print hours per day</span>
        <span className="normal-case tracking-normal">
          {hover >= 0 ? `${hover + 1}.: ${m.day_hours[hover].toFixed(1)} h` : `max ${Math.max(...m.day_hours).toFixed(1)} h`}
        </span>
      </div>
      <div className="flex h-32 items-end gap-[2px] sm:gap-1" onMouseLeave={() => setHover(-1)}>
        {m.day_hours.map((h, i) => (
          <div key={i} className="flex h-full flex-1 flex-col justify-end" onMouseEnter={() => setHover(i)} onClick={() => setHover(i)}>
            <div
              className="rounded-t-sm"
              style={{
                height: `${(h / max) * 100}%`,
                minHeight: h > 0 ? 3 : 0,
                background: i + 1 === today ? C.info : C.accent,
                opacity: hover === -1 || hover === i ? 1 : 0.5,
              }}
            />
          </div>
        ))}
      </div>
      <div className="mt-1 flex justify-between text-[11px] text-muted">
        <span>1</span>
        <span>{Math.ceil(m.days / 2)}</span>
        <span>{m.days}</span>
      </div>
    </div>
  )
}

// ---------------------------------------------------------------- timelapses (recorded by this app)

interface Lapse {
  id: string
  job: string
  start: number
  end: number
  result: string
  frames: number
  active: boolean
}

function TimelapseList({ snap }: { snap: Snapshot }) {
  const [items, setItems] = useState<Lapse[] | null>(null)
  const [open, setOpen] = useState('')
  useEffect(() => {
    api<{ items: Lapse[] }>('/api/timelapse')
      .then((d) => setItems(d.items))
      .catch(() => setItems([]))
  }, [snap.timelapse_version])
  if (!items)
    return (
      <p className="flex items-center gap-2 text-muted">
        <Loader2 size={16} className="animate-spin" /> Loading...
      </p>
    )
  return (
    <div className="flex flex-col gap-2">
      {!items.length && (
        <div className="card p-8 text-center text-muted">
          No recordings yet. {snap.settings.record_timelapse ? 'The next print is recorded (a camera frame with its Obico score about every 10 s).' : 'Recording is off: Settings > Features.'}
        </div>
      )}
      {items.map((t) => (
        <button
          key={t.id}
          onClick={() => setOpen(t.id)}
          className="card flex items-center gap-3 border-l-4 px-4 py-3 text-left transition hover:bg-card2/60"
          style={{ borderLeftColor: t.active ? C.info : t.result === 'FINISH' ? C.accent : t.result === 'FAILED' ? C.error : C.warn }}
        >
          <Film size={20} className="shrink-0 text-muted" />
          <div className="min-w-0 flex-1">
            <div className="truncate font-semibold">{t.job || 'Print'}</div>
            <div className="truncate text-sm text-muted">
              {new Date(t.start * 1000).toLocaleString('en-GB', { day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit' })} ·{' '}
              {hm((t.end || Date.now() / 1000) - t.start)} · {t.frames} frame{t.frames === 1 ? '' : 's'}
            </div>
          </div>
          {t.active && <span className="rounded-md bg-error/20 px-1.5 py-0.5 text-[11px] font-bold text-error">REC</span>}
        </button>
      ))}
      {open && <TimelapsePlayer id={open} onClose={() => setOpen('')} />}
    </div>
  )
}

// ---------------------------------------------------------------- history

function HistoryView({ snap }: { snap: Snapshot }) {
  const [reload, setReload] = useState(0)
  const [offset, setOffset] = useState(0)
  const [page, setPage] = useState<HistPage | null>(null)
  const [loading, setLoading] = useState(false)
  const [open, setOpen] = useState<HistItem | null>(null)
  useEffect(() => {
    let alive = true
    setLoading(true)
    api<HistPage>(`/api/history?offset=${offset}`)
      .then((p) => alive && setPage(p))
      .catch((e: ApiError) => alive && setPage({ ok: false, error: e.message, items: [], offset, more: false }))
      .finally(() => alive && setLoading(false))
    return () => {
      alive = false
    }
  }, [offset, reload])
  const items = page?.items ?? []
  return (
    <div className="flex flex-col gap-3">
      <div className="flex items-center gap-2 text-sm text-muted">
        {loading && <Loader2 size={14} className="animate-spin text-accent" />}
        <span className={page?.error ? 'text-error' : ''}>
          {page?.error || (loading && !page ? 'Loading history...' : items.length ? `Prints ${offset + 1} - ${offset + items.length}` : '')}
        </span>
      </div>
      {page?.ok && !items.length && <div className="card p-8 text-center text-muted">No prints in the archive yet.</div>}
      <div className="flex flex-col gap-2">
        {items.map((e) => (
          <button
            key={e.id}
            onClick={() => setOpen(e)}
            className="card flex items-center gap-3 border-l-4 px-4 py-3 text-left transition hover:bg-card2/60"
            style={{ borderLeftColor: statusColor(e.status) }}
          >
            <div className="min-w-0 flex-1">
              <div className="truncate font-semibold">{e.name || 'Print'}</div>
              <div className="truncate text-sm text-muted">
                {localTime(e.started)} · {hm(e.actual_sec >= 0 ? e.actual_sec : e.est_sec)}
                {e.grams !== null && ` · ${Math.round(e.grams)} g ${e.type}`}
              </div>
            </div>
            <div className="flex shrink-0 items-center gap-1">
              {e.colors.slice(0, 3).map((c, i) => (
                <Swatch key={i} color={c} size={16} />
              ))}
              {e.has_obico && <Eye size={18} className="ml-1 text-info" />}
            </div>
          </button>
        ))}
      </div>
      {(offset > 0 || page?.more) && (
        <div className="flex justify-between gap-2">
          <button className="btn" disabled={offset === 0 || loading} onClick={() => setOffset(Math.max(0, offset - 20))}>
            <ChevronLeft size={18} /> Newer
          </button>
          <button className="btn" disabled={!page?.more || loading} onClick={() => setOffset(offset + 20)}>
            Older <ChevronRight size={18} />
          </button>
        </div>
      )}
      {open && (
        <HistoryDetail
          e={open}
          snap={snap}
          onClose={() => setOpen(null)}
          onDeleted={() => {
            setOpen(null)
            setReload((x) => x + 1)
          }}
        />
      )}
    </div>
  )
}

function HistoryDetail({ e, snap, onClose, onDeleted }: { e: HistItem; snap: Snapshot; onClose: () => void; onDeleted: () => void }) {
  const dlg = useDialogs()
  const usage = useUsage(snap)?.filter((u) => u.archive_id === e.id) ?? []
  const del = async () => {
    const ok = await dlg.confirm({
      title: 'Delete this print?',
      text: `Deletes "${e.name}" (#${e.id}) from the print log. This cannot be undone. Its Obico scores / timelapse move to another entry of the same print, if there is one.`,
      ok: 'Delete',
      tone: 'danger',
    })
    if (!ok) return
    try {
      const r = await api<{ ok: boolean; message: string }>(`/api/history/${e.id}`, { method: 'DELETE' })
      if (r.ok) onDeleted()
      else dlg.toast(false, r.message)
    } catch (err) {
      dlg.toast(false, (err as ApiError).message)
    }
  }
  const [imgFailed, setImgFailed] = useState(false)
  const [lapse, setLapse] = useState(false)
  const [lapseId, setLapseId] = useState(e.timelapse)
  const [rec, setRec] = useState<ObicoRec | null | undefined>(e.has_obico ? undefined : null)
  useEffect(() => {
    if (!e.has_obico) return
    api<ObicoRec>(`/api/obico/record/${e.id}`)
      .then(setRec)
      .catch(() => setRec(null))
  }, [e.id, e.has_obico])
  const rows: [string, ReactNode, string?][] = [
    ['Status', e.status || '--', statusColor(e.status)],
    ['Started', localTime(e.started)],
    ['Duration', `${hm(e.actual_sec)}   (estimate ${hm(e.est_sec)})`],
  ]
  if (e.grams !== null) rows.push(['Filament', `${e.grams.toFixed(1)} g  ${e.type}`])
  if (e.filaments?.length)
    rows.push([
      'Per filament',
      <span className="flex flex-col gap-1">
        {e.filaments.map((f, i) => (
          <span key={i} className="flex items-center gap-2">
            <Swatch color={f.color} size={16} /> {f.type} {f.grams.toFixed(1)} g
          </span>
        ))}
      </span>,
    ])
  if (e.from_sd)
    rows.push(['Source', <span className="font-normal text-muted">Filament and picture from the SD file</span>])
  if (e.colors.length && !e.filaments?.length)
    rows.push([
      'Colours',
      <span className="flex gap-1.5">
        {e.colors.map((c, i) => (
          <Swatch key={i} color={c} size={22} />
        ))}
      </span>,
    ])
  if (e.layers > 0) rows.push(['Layers', `${e.layers} × ${(e.layer_height ?? 0).toFixed(2)} mm`])
  if (e.cost !== null)
    rows.push([
      'Cost',
      <span>
        {money(e.cost, snap)}
        {e.cost_filament != null && (
          <span className="font-normal text-muted">
            {' '}
            (filament {money(e.cost_filament, snap)} + power {money(e.cost_energy ?? 0, snap)}
            {e.energy_kwh ? `, ${e.energy_kwh.toFixed(2)} kWh` : ''})
          </span>
        )}
      </span>,
    ])
  if (e.failure) rows.push(['Failure', e.failure, C.error])
  return (
    <Modal
      title={e.name || 'Print'}
      onClose={onClose}
      wide
      footer={
        <button className="btn mr-auto text-error" onClick={del} title="Delete this entry from the print log">
          <Trash2 size={18} /> Delete
        </button>
      }
    >
      <div className="flex flex-col gap-5">
        <div className="flex flex-col gap-4 sm:flex-row">
          <div className="flex aspect-square w-full shrink-0 items-center justify-center overflow-hidden rounded-xl bg-card sm:w-60">
            {imgFailed ? (
              <span className="flex flex-col items-center gap-2 text-sm text-muted">
                <ImageOff size={36} /> No preview
              </span>
            ) : (
              <img src={`/api/history/${e.id}/thumb`} alt="" className="h-full w-full object-contain" onError={() => setImgFailed(true)} />
            )}
          </div>
          <dl className="grid h-fit grid-cols-[auto_1fr] gap-x-4 gap-y-2">
            {rows.map(([k, v, col]) => (
              <div key={k} className="contents">
                <dt className="text-sm text-muted">{k}</dt>
                <dd className="text-sm font-semibold [overflow-wrap:anywhere]" style={col ? { color: col } : undefined}>
                  {v}
                </dd>
              </div>
            ))}
          </dl>
        </div>
        {lapseId ? (
          <div className="flex items-center gap-3">
            <TimelapseButton id={lapseId} onOpen={() => setLapse(true)} className="btn-accent" />
            <span className="text-sm text-muted">Timelapse with the Obico score of every frame.</span>
          </div>
        ) : (
          <ImportLapse archiveId={e.id} onDone={(id) => {
            setLapseId(id)
            setLapse(true)
          }} />
        )}
        {usage.map((u) => (
          <UsageCard key={u.id} r={u} />
        ))}
        <ObicoSection rec={rec} />
        {lapse && lapseId && <TimelapsePlayer id={lapseId} onClose={() => setLapse(false)} />}
      </div>
    </Modal>
  )
}

/** The printer's own timelapse (.avi saved from Bambu Studio / the SD card) for this print. */
function ImportLapse({ archiveId, onDone }: { archiveId: number; onDone: (id: string) => void }) {
  const [pct, setPct] = useState(-1)
  const [err, setErr] = useState('')
  const upload = (f: File) => {
    setErr('')
    setPct(0)
    const x = new XMLHttpRequest()
    x.open('POST', `/api/timelapse/import?archive_id=${archiveId}`)
    x.upload.onprogress = (ev) => ev.lengthComputable && setPct(Math.round((100 * ev.loaded) / ev.total))
    x.onload = () => {
      try {
        const d = JSON.parse(x.responseText)
        if (x.status === 200 && d.id) {
          setPct(-1)
          onDone(d.id)
          return
        }
        setErr(d.detail || `Import failed (HTTP ${x.status})`)
      } catch {
        setErr(`Import failed (HTTP ${x.status})`)
      }
      setPct(-1)
    }
    x.onerror = () => {
      setErr('Upload failed')
      setPct(-1)
    }
    x.send(f)
  }
  return (
    <div className="flex flex-wrap items-center gap-3 rounded-xl bg-card px-4 py-3">
      <label className={`btn cursor-pointer ${pct >= 0 ? 'pointer-events-none opacity-60' : ''}`}>
        {pct >= 0 ? <Loader2 size={18} className="animate-spin" /> : <Film size={18} />}
        {pct >= 0 ? (pct < 100 ? `Uploading ${pct}%` : 'Processing...') : 'Import printer timelapse (.avi)'}
        <input type="file" accept=".avi,video/x-msvideo,video/avi" className="hidden" onChange={(ev) => ev.target.files?.[0] && upload(ev.target.files[0])} />
      </label>
      <span className="min-w-0 flex-1 text-xs text-muted">
        Save the print's video from Bambu Studio (Device &gt; Timelapse &gt; Download) and pick it here; the Obico scores of this print are matched to it.
      </span>
      {err && <span className="w-full text-sm text-error">{err}</span>}
    </div>
  )
}

/** Obico verdict of a record: the colour and a word for its worst moment (panel obicoColor). */
function verdict(r: ObicoRec): [string, string] {
  if (r.fail || (r.high > 0 && r.max >= r.high)) return [C.error, 'Failure seen']
  if (r.warn || (r.low > 0 && r.max >= r.low)) return [C.warn, 'Suspicious moments']
  return [C.accent, 'Looked good']
}

function ObicoSection({ rec }: { rec: ObicoRec | null | undefined }) {
  return (
    <div className="flex flex-col gap-2 rounded-xl bg-card p-4">
      <div className="flex items-center gap-2 font-bold text-muted">
        <Eye size={18} /> Obico AI detection
      </div>
      {rec === undefined && (
        <p className="flex items-center gap-2 text-sm text-muted">
          <Loader2 size={14} className="animate-spin" /> Loading...
        </p>
      )}
      {rec === null && (
        <p className="text-sm text-muted">
          No Obico scores for this print. The server records them while a print runs (with Obico switched on in Settings).
        </p>
      )}
      {rec && <ObicoBody r={rec} />}
    </div>
  )
}

export function ObicoBody({ r }: { r: ObicoRec }) {
  const [col, word] = verdict(r)
  const pts = r.points
  const top = Math.max(200, ...pts)
  const W = 600
  const H = 150
  const x = (i: number) => (i / Math.max(1, pts.length - 1)) * W
  const y = (v: number) => H - (v / top) * H
  const path = pts.map((v, i) => `${i ? 'L' : 'M'}${x(i).toFixed(1)},${y(v).toFixed(1)}`).join('')
  const lines: [number, string][] = [
    [r.low * 200, C.warn],
    [r.high * 200, C.error],
  ]
  return (
    <>
      <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1.5 text-sm">
        <dt className="text-muted">Result</dt>
        <dd className="font-semibold" style={{ color: col }}>
          {word} - highest score {r.max.toFixed(2)} (warning {r.low.toFixed(2)}, failure {r.high.toFixed(2)})
        </dd>
        <dt className="text-muted">Frames</dt>
        <dd>
          average {(r.samples ? r.sum / r.samples : 0).toFixed(2)} over {r.samples} frames; {r.warn} suspicious, {r.fail} failure
        </dd>
        {r.dark > 0 && (
          <>
            <dt className="text-muted">Light</dt>
            <dd className="text-warn">light off for {Math.round((r.dark * 100) / Math.max(1, r.samples))}% of the frames (the camera sees little)</dd>
          </>
        )}
      </dl>
      {pts.length >= 2 && (
        <svg viewBox={`0 0 ${W} ${H + 4}`} className="mt-2 w-full">
          <line x1="0" x2={W} y1={H} y2={H} stroke={C.card2} />
          {lines.map(([v, c]) =>
            v > 0 && v <= top ? <line key={c} x1="0" x2={W} y1={y(v)} y2={y(v)} stroke={c} strokeDasharray="6 5" strokeWidth="1.5" /> : null,
          )}
          <path d={`${path}L${W},${H}L0,${H}Z`} fill={C.info} opacity="0.12" />
          <path d={path} fill="none" stroke={C.info} strokeWidth="2.5" strokeLinejoin="round" style={{ filter: `drop-shadow(0 0 3px ${C.info})` }} />
        </svg>
      )}
      {pts.length >= 2 && <p className="text-xs text-muted">Score over the print (highest per span). Dashed: warning (yellow) and failure (red) thresholds.</p>}
    </>
  )
}
