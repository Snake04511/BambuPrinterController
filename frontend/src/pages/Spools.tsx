// SPOOLS: every spool of the own inventory - in the AMS or on the shelf. Add (also several
// identical ones), edit, duplicate, archive; recent bookings below.
import { Archive, ArchiveRestore, Copy, Droplets, Loader2, MapPin, Package, Pencil, Plus, Save, Search } from 'lucide-react'
import { useEffect, useMemo, useState, type ReactNode } from 'react'
import { api, cmd } from '../api'
import type { Snapshot } from '../types'
import { Modal, Swatch, useDialogs } from '../ui'
import { ColorInput } from './Ams'

export interface InvSpool {
  id: number
  material: string
  subtype: string
  brand: string
  color_name: string
  color: string
  label_weight: number
  weight_used: number
  core_weight: number
  remaining: number
  temp_min: number
  temp_max: number
  cost_per_kg: number | null
  note: string
  location: string
  last_used: number | null
  archived: boolean
  tray: string | null
  printing_used?: number
  wet_hours?: number
  dried_at?: number | null
  needs_drying?: boolean
}

interface Booking {
  id: number
  spool_id: number
  grams: number
  job: string
  booked_at: number
  spool_name: string
  color: string
}

const name = (s: { brand: string; material: string; subtype: string }) =>
  [s.brand, s.material, s.subtype].filter(Boolean).join(' ') || 'Spool'
const kg = (g: number) => (g >= 1000 ? `${(g / 1000).toFixed(2)} kg` : `${Math.round(g)} g`)

export default function Spools({ snap }: { snap: Snapshot | null }) {
  const [data, setData] = useState<{ own: boolean; spools: InvSpool[]; usage: Booking[] } | null>(null)
  const [archived, setArchived] = useState(false)
  const [q, setQ] = useState('')
  const [mat, setMat] = useState('')
  const [edit, setEdit] = useState<InvSpool | 'new' | null>(null)
  const [copyOf, setCopyOf] = useState<InvSpool | null>(null)
  const reload = () =>
    api<{ own: boolean; spools: InvSpool[]; usage: Booking[] }>(`/api/spools?archived=${archived}`)
      .then(setData)
      .catch(() => setData({ own: false, spools: [], usage: [] }))
  // the inventory changes with every booking / tray change: follow the assignments + usage
  // while printing: follow the live use (filament_use changes with the progress)
  useEffect(() => {
    reload()
  }, [archived, Math.floor((snap?.filament_use?.total ?? 0) * (snap?.filament_use?.progress ?? 0) / 500), snap?.usage_version, snap?.assignments?.length, JSON.stringify(snap?.assignments?.map((a) => [a.spool.id, a.spool.weight_used]))])

  const spools = data?.spools ?? []
  const materials = useMemo(() => [...new Set(spools.filter((s) => !s.archived).map((s) => s.material || '?'))].sort(), [spools])
  const shown = spools.filter((s) => {
    if (!archived && s.archived) return false
    if (mat && (s.material || '?') !== mat) return false
    const t = `${name(s)} ${s.color_name} ${s.location} ${s.note} ${s.tray ?? ''}`.toLowerCase()
    return !q || q.toLowerCase().split(/\s+/).every((w) => t.includes(w))
  })
  // In the AMS first, then by material / colour.
  shown.sort((a, b) => Number(!!b.tray) - Number(!!a.tray) || Number(a.archived) - Number(b.archived) || name(a).localeCompare(name(b)) || a.color_name.localeCompare(b.color_name))
  const active = spools.filter((s) => !s.archived)
  const total = active.reduce((n, s) => n + s.remaining, 0)

  if (!data) return <div className="p-6 text-muted">Loading...</div>

  return (
    <div className="mx-auto flex max-w-6xl flex-col gap-4 p-3 md:p-4">
      <section className="card flex flex-col gap-3 p-4">
        <div className="flex flex-wrap items-center gap-3">
          <h2 className="section-title mb-0!">Spools</h2>
          <span className="text-sm text-muted">
            {active.length} spools · {kg(total)} left
          </span>
          <span className="flex-1" />
          <button className="btn-accent h-10" onClick={() => setEdit('new')}>
            <Plus size={18} /> Add spool
          </button>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <div className="relative min-w-48 flex-1">
            <Search size={16} className="absolute top-1/2 left-3 -translate-y-1/2 text-muted" />
            <input className="input pl-9" placeholder="Search brand, colour, place..." value={q} onChange={(e) => setQ(e.target.value)} />
          </div>
          <div className="flex flex-wrap gap-1">
            <Chip on={!mat} onClick={() => setMat('')}>
              All
            </Chip>
            {materials.map((m) => (
              <Chip key={m} on={mat === m} onClick={() => setMat(mat === m ? '' : m)}>
                {m}
              </Chip>
            ))}
          </div>
          <Chip on={archived} onClick={() => setArchived(!archived)}>
            <Archive size={13} /> Archived
          </Chip>
        </div>
      </section>

      {shown.length === 0 ? (
        <p className="p-4 text-center text-muted">{spools.length ? 'Nothing matches.' : 'No spools yet - add one.'}</p>
      ) : (
        <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
          {shown.map((s) => (
            <SpoolCard key={s.id} s={s} onEdit={() => setEdit(s)} onCopy={() => setCopyOf(s)} onChanged={reload} />
          ))}
        </div>
      )}

      {data.usage.length > 0 && (
        <section className="card p-4">
          <h2 className="section-title">Recent bookings</h2>
          <div className="flex flex-col divide-y divide-border">
            {data.usage.slice(0, 20).map((u) => (
              <div key={u.id} className="flex items-center gap-2.5 py-1.5 text-sm">
                <Swatch color={u.color || '#888888'} size={14} />
                <span className="min-w-0 flex-1 truncate">
                  <span className="font-semibold">{u.spool_name || 'Spool'}</span> <span className="text-muted">#{u.spool_id}</span>
                  {u.job && <span className="text-muted"> · {u.job}</span>}
                </span>
                <span className="font-semibold tabular-nums">{u.grams.toFixed(1)} g</span>
                <span className="w-24 text-right text-xs text-muted">
                  {new Date(u.booked_at * 1000).toLocaleString('en-GB', { day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit' })}
                </span>
              </div>
            ))}
          </div>
        </section>
      )}

      {edit && <SpoolEditor spool={edit === 'new' ? null : edit} onClose={() => setEdit(null)} onSaved={reload} />}
      {copyOf && <SpoolEditor spool={null} template={copyOf} onClose={() => setCopyOf(null)} onSaved={reload} />}
    </div>
  )
}

function Chip({ on, onClick, children }: { on: boolean; onClick: () => void; children: ReactNode }) {
  return (
    <button
      onClick={onClick}
      className={`flex h-8 items-center gap-1 rounded-lg border px-2.5 text-xs font-bold ${on ? 'border-accent bg-card2 text-accent' : 'border-border text-muted hover:text-text'}`}
    >
      {children}
    </button>
  )
}

function SpoolCard({ s, onEdit, onCopy, onChanged }: { s: InvSpool; onEdit: () => void; onCopy: () => void; onChanged: () => void }) {
  const dlg = useDialogs()
  const pct = s.label_weight > 0 ? Math.max(0, Math.min(100, (100 * s.remaining) / s.label_weight)) : 0
  const low = s.remaining < 100
  const archive = async () => {
    if (!s.archived && !(await dlg.confirm({ title: 'Archive spool?', text: `${name(s)} ${s.color_name} (#${s.id}) is hidden from the list${s.tray ? ' and taken off its tray' : ''}. Its history stays.`, ok: 'Archive' })))
      return
    const r = await cmd('spool_archive', { id: s.id, archived: !s.archived })
    if (r.ok) onChanged()
  }
  return (
    <div className={`card flex flex-col gap-3 p-4 ${s.archived ? 'opacity-60' : ''}`}>
      <div className="flex items-start gap-3">
        <span
          className="mt-0.5 block h-10 w-10 shrink-0 rounded-full border border-white/20"
          style={{ background: s.color || '#888888', boxShadow: `0 0 14px -2px ${s.color || '#888888'}` }}
        />
        <div className="min-w-0 flex-1">
          <div className="truncate font-semibold">{name(s)}</div>
          <div className="truncate text-sm text-muted">{s.color_name || s.color || '-'}</div>
        </div>
        <span className="text-xs text-muted">#{s.id}</span>
      </div>
      <div>
        <div className="mb-1 flex items-baseline justify-between text-sm">
          <span className={`font-bold tabular-nums ${low ? 'text-warn' : ''}`}>
            {s.printing_used ? '~' : ''}
            {Math.round(s.remaining)} g
            {!!s.printing_used && <span className="ml-1.5 text-xs font-semibold text-info">-{s.printing_used.toFixed(1)} g now</span>}
          </span>
          <span className="text-xs text-muted">of {s.label_weight} g</span>
        </div>
        <div className="h-2 overflow-hidden rounded-full bg-card2">
          <div className="h-full rounded-full" style={{ width: `${pct}%`, background: low ? 'var(--color-warn)' : 'var(--color-accent)' }} />
        </div>
      </div>
      <div className="flex flex-wrap items-center gap-1.5 text-xs">
        {s.tray ? (
          <span className="flex items-center gap-1 rounded-md bg-accent/15 px-1.5 py-0.5 font-bold text-accent">
            <Package size={12} /> {s.tray}
          </span>
        ) : (
          <span className="flex items-center gap-1 rounded-md bg-card2 px-1.5 py-0.5 font-semibold text-muted">
            <MapPin size={12} /> {s.location || 'Not loaded'}
          </span>
        )}
        {s.tray && s.location && <span className="rounded-md bg-card2 px-1.5 py-0.5 text-muted">{s.location}</span>}
        {s.temp_min > 0 && (
          <span className="rounded-md bg-card2 px-1.5 py-0.5 text-muted">
            {s.temp_min}-{s.temp_max}°C
          </span>
        )}
        {s.archived && <span className="rounded-md bg-card2 px-1.5 py-0.5 text-muted">archived</span>}
        {s.needs_drying ? (
          <span className="flex items-center gap-1 rounded-md bg-info/15 px-1.5 py-0.5 font-bold text-info" title="Hours in humid air since the last drying">
            <Droplets size={12} /> needs drying ({Math.round(s.wet_hours ?? 0)} h humid)
          </span>
        ) : (s.wet_hours ?? 0) >= 1 ? (
          <span className="flex items-center gap-1 rounded-md bg-card2 px-1.5 py-0.5 text-muted" title="Hours in humid air since the last drying">
            <Droplets size={12} /> {Math.round(s.wet_hours ?? 0)} h humid
          </span>
        ) : null}
      </div>
      {s.note && <p className="line-clamp-2 text-xs text-muted">{s.note}</p>}
      <div className="mt-auto flex gap-2">
        <button className="btn h-9 flex-1 text-sm" onClick={onEdit}>
          <Pencil size={15} /> Edit
        </button>
        {(s.wet_hours ?? 0) >= 1 && (
          <button
            className={`h-9 text-sm ${s.needs_drying ? 'btn-accent' : 'btn'}`}
            onClick={async () => (await cmd('spool_dried', { id: s.id })).ok && onChanged()}
            title="I dried it: start counting again"
          >
            <Droplets size={15} /> Dried
          </button>
        )}
        <button className="btn h-9 text-sm" onClick={onCopy} title="Add another spool like this one">
          <Copy size={15} />
        </button>
        <button className="btn h-9 text-sm" onClick={archive} title={s.archived ? 'Restore' : 'Archive (empty / gone)'}>
          {s.archived ? <ArchiveRestore size={15} /> : <Archive size={15} />}
        </button>
      </div>
    </div>
  )
}

function Field({ label, children, wide }: { label: string; children: ReactNode; wide?: boolean }) {
  return (
    <label className={`flex flex-col gap-1 ${wide ? 'col-span-2' : ''}`}>
      <span className="label">{label}</span>
      {children}
    </label>
  )
}

const MATERIALS = ['PLA', 'PETG', 'ABS', 'ASA', 'TPU', 'PA', 'PC', 'PVA']

/** New spool (optionally a copy of `template`) or edit of an existing one. */
function SpoolEditor({ spool, template, onClose, onSaved }: { spool: InvSpool | null; template?: InvSpool; onClose: () => void; onSaved: () => void }) {
  const dlg = useDialogs()
  const src = spool ?? template
  const [f, setF] = useState({
    material: src?.material ?? 'PLA',
    subtype: src?.subtype ?? '',
    brand: src?.brand ?? '',
    color_name: src?.color_name ?? '',
    color: src?.color || '#FFFFFF',
    label_weight: src?.label_weight ?? 1000,
    remaining: spool ? Math.round(spool.remaining) : (src?.label_weight ?? 1000),
    core_weight: src?.core_weight ?? 250,
    temp_min: src?.temp_min || 0,
    temp_max: src?.temp_max || 0,
    cost_per_kg: src?.cost_per_kg ?? '',
    location: src?.location ?? '',
    note: spool?.note ?? '',
    count: 1,
  })
  const [busy, setBusy] = useState(false)
  const set = (k: keyof typeof f, v: string | number) => setF((o) => ({ ...o, [k]: v }))
  const save = async () => {
    const body: Record<string, unknown> = {
      material: f.material.trim(),
      subtype: f.subtype.trim(),
      brand: f.brand.trim(),
      color_name: f.color_name.trim(),
      color: f.color,
      label_weight: f.label_weight,
      weight_used: Math.max(0, f.label_weight - f.remaining),
      core_weight: f.core_weight,
      temp_min: f.temp_min || null,
      temp_max: f.temp_max || null,
      cost_per_kg: f.cost_per_kg === '' ? null : Number(f.cost_per_kg),
      location: f.location.trim(),
      note: f.note.trim(),
    }
    setBusy(true)
    let ok = true
    if (spool) {
      ok = (await cmd('spool_patch', { id: spool.id, ...body })).ok
    } else {
      for (let i = 0; i < Math.max(1, Math.min(20, f.count)) && ok; i++) ok = (await cmd('spool_create', body)).ok
    }
    setBusy(false)
    if (ok) {
      if (!spool && f.count > 1) dlg.toast(true, `${f.count} spools added`)
      onSaved()
      onClose()
    }
  }
  return (
    <Modal
      title={spool ? `Edit spool #${spool.id}` : template ? 'Add a spool like this' : 'Add spool'}
      onClose={onClose}
      footer={
        <button className="btn-accent" disabled={busy || !f.material.trim()} onClick={save}>
          {busy ? <Loader2 size={18} className="animate-spin" /> : <Save size={18} />} {spool ? 'Save' : f.count > 1 ? `Add ${f.count} spools` : 'Add spool'}
        </button>
      }
    >
      <div className="grid grid-cols-2 gap-3">
        <Field label="Material">
          <input className="input" list="materials" value={f.material} onChange={(e) => set('material', e.target.value)} />
          <datalist id="materials">
            {MATERIALS.map((m) => (
              <option key={m} value={m} />
            ))}
          </datalist>
        </Field>
        <Field label="Type / subtype">
          <input className="input" placeholder="Basic, Matte, HF..." value={f.subtype} onChange={(e) => set('subtype', e.target.value)} />
        </Field>
        <Field label="Brand">
          <input className="input" placeholder="Bambu Lab, eSun..." value={f.brand} onChange={(e) => set('brand', e.target.value)} />
        </Field>
        <Field label="Colour name">
          <input className="input" value={f.color_name} onChange={(e) => set('color_name', e.target.value)} />
        </Field>
        <Field label="Colour" wide>
          <ColorInput value={f.color} onChange={(v) => set('color', v)} />
        </Field>
        <Field label="Label weight (g)">
          <input className="input" type="number" min={0} max={10000} value={f.label_weight} onChange={(e) => set('label_weight', Number(e.target.value))} />
        </Field>
        <Field label="Remaining (g)">
          <input className="input" type="number" min={0} max={10000} value={f.remaining} onChange={(e) => set('remaining', Number(e.target.value))} />
        </Field>
        <Field label="Nozzle min °C">
          <input className="input" type="number" min={0} max={400} value={f.temp_min || ''} placeholder="-" onChange={(e) => set('temp_min', Number(e.target.value))} />
        </Field>
        <Field label="Nozzle max °C">
          <input className="input" type="number" min={0} max={400} value={f.temp_max || ''} placeholder="-" onChange={(e) => set('temp_max', Number(e.target.value))} />
        </Field>
        <Field label="Empty spool weight (g)">
          <input className="input" type="number" min={0} max={1000} value={f.core_weight} onChange={(e) => set('core_weight', Number(e.target.value))} />
        </Field>
        <Field label="Price / kg">
          <input className="input" type="number" min={0} step="0.01" value={f.cost_per_kg} placeholder="-" onChange={(e) => set('cost_per_kg', e.target.value)} />
        </Field>
        <Field label="Where is it" wide>
          <input className="input" list="places" placeholder="Shelf, cabinet, dryer box..." value={f.location} onChange={(e) => set('location', e.target.value)} />
          <datalist id="places">
            {['Shelf', 'Cabinet', 'Dry box', 'Dryer'].map((m) => (
              <option key={m} value={m} />
            ))}
          </datalist>
        </Field>
        <Field label="Note" wide>
          <textarea className="input min-h-16 py-2" value={f.note} onChange={(e) => set('note', e.target.value)} />
        </Field>
        {!spool && (
          <Field label="How many (identical spools)">
            <input className="input" type="number" min={1} max={20} value={f.count} onChange={(e) => set('count', Math.max(1, Math.min(20, Number(e.target.value) || 1)))} />
          </Field>
        )}
      </div>
    </Modal>
  )
}
