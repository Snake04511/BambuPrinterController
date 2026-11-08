// Multi-plate run (one 3MF, plate after plate): a strip under the top bar while it lasts.
// The next plate never starts by itself - only with the button, after the plate is cleared.
import { Layers, Loader2, Play, SkipForward, X } from 'lucide-react'
import { useState } from 'react'
import { api, type ApiError } from './api'
import type { Snapshot } from './types'
import { useDialogs } from './ui'

export interface PlateSeq {
  file: string
  state: 'printing' | 'waiting' | 'paused' | 'complete'
  current: number | null
  next: number | null
  next_name: string
  done: number[]
  total: number
  message: string
}

export function PlateSeqBar({ snap }: { snap: Snapshot | null }) {
  const dlg = useDialogs()
  const [busy, setBusy] = useState('')
  const q = snap?.plate_seq
  if (!q) return null
  const st = snap?.status
  const printerFree = !!st?.connected && !['PREPARE', 'SLICING', 'RUNNING', 'PAUSE'].includes(st.state)
  const act = async (a: 'next' | 'skip' | 'stop') => {
    if (a === 'next') {
      const ok = await dlg.confirm({
        title: `Start plate ${q.next}?`,
        text: 'Is the build plate clear and back on the printer?',
        ok: 'Plate is clear - start',
      })
      if (!ok) return
    }
    setBusy(a)
    try {
      const r = await api<{ ok: boolean; message: string }>(`/api/plate-seq/${a}`, { body: {} })
      if (!r.ok) dlg.toast(false, r.message)
    } catch (e) {
      dlg.toast(false, (e as ApiError).message)
    }
    setBusy('')
  }
  const waiting = q.state === 'waiting' || q.state === 'paused'
  return (
    <div
      className={`flex flex-wrap items-center gap-x-3 gap-y-2 border-b px-4 py-2 text-sm ${waiting ? 'border-accent/40 bg-accent/10' : 'border-border/60 bg-card'}`}
    >
      <span className="flex min-w-0 basis-full items-center gap-2 sm:flex-1 sm:basis-auto">
        <Layers size={18} className={`shrink-0 ${waiting ? 'text-accent' : 'text-muted'}`} />
        <span className="min-w-0 flex-1 truncate">
          <span className="font-semibold">{q.file}</span>
          <span className="text-muted">
            {' '}
            · {q.done.length}/{q.total} plates done
            {q.state === 'printing' && q.current ? ` · printing plate ${q.current}` : ''}
            {q.state === 'complete' ? ' · all done' : ''}
          </span>
          {q.message && <span className={q.state === 'paused' ? ' text-warn' : ' text-muted'}> · {q.message}</span>}
        </span>
      </span>
      {waiting && q.next && (
        <button
          className="btn-accent h-9 text-sm"
          disabled={!!busy || !printerFree}
          onClick={() => act('next')}
          title={printerFree ? '' : 'The printer is busy'}
        >
          {busy === 'next' ? <Loader2 size={16} className="animate-spin" /> : <Play size={16} />} Start plate {q.next}
          {q.next_name ? ` · ${q.next_name}` : ''}
        </button>
      )}
      {q.next && q.state !== 'complete' && (
        <button className="btn h-9 text-sm" disabled={!!busy} onClick={() => act('skip')} title={`Leave plate ${q.next} out`}>
          <SkipForward size={16} /> Skip
        </button>
      )}
      <button
        className="btn h-9 text-sm"
        disabled={!!busy}
        onClick={() => act('stop')}
        title={q.state === 'complete' ? 'Close' : 'End the run (the running plate keeps printing)'}
      >
        <X size={16} /> {q.state === 'complete' ? 'Close' : 'End run'}
      </button>
    </div>
  )
}
