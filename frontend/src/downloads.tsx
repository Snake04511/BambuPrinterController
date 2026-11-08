// SD file downloads to this device: they keep running when the file dialog closes, and a
// small tray (bottom right) shows them; the browser download starts when the file is ready.
import { Check, Download, Loader2, TriangleAlert, X } from 'lucide-react'
import { useSyncExternalStore } from 'react'
import { api, type ApiError } from './api'

export interface Dl {
  key: number
  name: string
  state: 'starting' | 'waiting' | 'preparing' | 'ready' | 'failed'
  message: string
  url?: string
  cancelled?: boolean
}

let items: Dl[] = []
let seq = 0
const listeners = new Set<() => void>()
const emit = () => {
  items = [...items]
  listeners.forEach((fn) => fn())
}
const update = (key: number, patch: Partial<Dl>) => {
  const d = items.find((x) => x.key === key)
  if (d) Object.assign(d, patch)
  emit()
}
const remove = (key: number) => {
  items = items.filter((x) => x.key !== key)
  emit()
}

export function useDownloads(): Dl[] {
  return useSyncExternalStore(
    (fn) => {
      listeners.add(fn)
      return () => listeners.delete(fn)
    },
    () => items,
  )
}

/** Starts (or joins) the download of an SD file; runs until done, whatever is open. */
export async function startDownload(name: string, size: number): Promise<void> {
  if (items.some((d) => d.name === name && d.state !== 'failed' && d.state !== 'ready')) return
  const key = ++seq
  items = [...items, { key, name, state: 'starting', message: '' }]
  emit()
  try {
    const { id } = await api<{ id: string }>('/api/sd/download', { body: { name, size } })
    for (;;) {
      await new Promise((r) => window.setTimeout(r, 2000))
      const me = items.find((x) => x.key === key)
      if (!me || me.cancelled) return
      const st = await api<{ state: Dl['state']; message: string }>(`/api/sd/download/${id}`)
      if (st.state === 'ready') {
        // Browsers may block a download that does not follow a click (Safari does): try, and
        // keep a "Save" button in the tray for the click.
        const url = `/api/sd/download/${id}/file`
        update(key, { state: 'ready', message: '', url })
        const a = document.createElement('a')
        a.href = url
        a.download = name
        document.body.appendChild(a)
        a.click()
        a.remove()
        return
      }
      if (st.state === 'failed') {
        update(key, { state: 'failed', message: st.message })
        return
      }
      update(key, { state: st.state })
    }
  } catch (e) {
    update(key, { state: 'failed', message: (e as ApiError).message })
  }
}

const TEXT: Record<Dl['state'], string> = {
  starting: 'Starting...',
  waiting: 'Waiting for the printer (another transfer runs)...',
  preparing: 'Copying from the printer...',
  ready: 'Ready - if it did not start, press Save',
  failed: 'Failed',
}

/** The tray, shown in the app shell. */
export function DownloadsTray() {
  const list = useDownloads()
  if (!list.length) return null
  return (
    <div className="fixed right-3 bottom-[calc(5.5rem+env(safe-area-inset-bottom))] z-[60] flex w-[min(92vw,340px)] flex-col gap-2 wide:right-5 wide:bottom-5">
      {list.map((d) => (
        <div key={d.key} className="flex items-start gap-3 rounded-xl border border-border bg-panel/95 p-3 shadow-[0_12px_40px_-12px_#000] backdrop-blur">
          <span className="mt-0.5 shrink-0">
            {d.state === 'ready' ? (
              <Check size={18} className="text-accent" />
            ) : d.state === 'failed' ? (
              <TriangleAlert size={18} className="text-error" />
            ) : (
              <Loader2 size={18} className="animate-spin text-info" />
            )}
          </span>
          <div className="min-w-0 flex-1">
            <div className="flex items-center gap-1.5 truncate text-sm font-semibold">
              <Download size={13} className="shrink-0 text-muted" />
              <span className="truncate">{d.name}</span>
            </div>
            <div className={`text-xs ${d.state === 'failed' ? 'text-error' : 'text-muted'}`}>
              {TEXT[d.state]}
              {d.message && `: ${d.message}`}
            </div>
            {d.state === 'preparing' && <div className="mt-1 text-[11px] text-muted">Big files take a few minutes; you can close the file and keep using the app.</div>}
          </div>
          {d.state === 'ready' && d.url && (
            <a href={d.url} download={d.name} className="btn-accent h-8 shrink-0 px-3 text-xs" onClick={() => window.setTimeout(() => remove(d.key), 1500)}>
              <Download size={14} /> Save
            </a>
          )}
          <button
            className="shrink-0 text-muted hover:text-text"
            title={d.state === 'failed' || d.state === 'ready' ? 'Dismiss' : 'Stop watching (the printer copy may still finish)'}
            onClick={() => {
              update(d.key, { cancelled: true })
              remove(d.key)
            }}
          >
            <X size={16} />
          </button>
        </div>
      ))}
    </div>
  )
}
