// FILES: the printer's SD card (panel src/ui/pages/FilesPage.cpp).
import { Download, FileBox, LayoutGrid, List, Loader2, RefreshCw, TriangleAlert } from 'lucide-react'
import { useEffect, useState } from 'react'
import { api, type ApiError } from '../api'
import { isPrinting, online } from '../format'
import { fmtSec, fmtSize, SdFileDialog, sdThumbUrl, type SdFile } from '../print'
import type { Snapshot } from '../types'
import { useDialogs, useLongPress } from '../ui'

interface Listing {
  files: SdFile[]
  error: string
  listed: boolean
  loading: boolean
  busy: boolean
  waiting?: { name: string; left: number }[]
}

function loadView(): 'grid' | 'list' {
  try {
    return localStorage.getItem('files-view') === 'list' ? 'list' : 'grid'
  } catch {
    return 'grid'
  }
}

export default function Files({ snap, goHome }: { snap: Snapshot | null; goHome: () => void }) {
  const dlg = useDialogs()
  const [list, setList] = useState<Listing | null>(null)
  const [view, setView] = useState<'grid' | 'list'>(loadView)
  const [open, setOpen] = useState<SdFile | null>(null)
  const version = snap?.sd_version ?? 0

  // Opening FILES lists the card again when the last list is older than a minute (the server
  // lists by itself only every 10 min: each list is an FTP connection on the printer).
  useEffect(() => {
    api('/api/sd/refresh?soft=1', { body: {} }).catch(() => {})
  }, [])
  useEffect(() => {
    api<Listing>('/api/sd/files')
      .then(setList)
      .catch(() => {})
  }, [version])

  const toggleView = () => {
    const v = view === 'grid' ? 'list' : 'grid'
    setView(v)
    try {
      localStorage.setItem('files-view', v)
    } catch {
      /* private mode */
    }
  }
  const refresh = async () => {
    try {
      await api('/api/sd/refresh', { body: {} })
    } catch (e) {
      dlg.toast(false, (e as ApiError).message)
    }
  }
  const fetchNew = async () => {
    if (isPrinting(snap?.status)) {
      const ok = await dlg.confirm({
        title: 'Load new files now?',
        text: "A print is running: reading the files uses the printer's FTP meanwhile.",
        ok: 'Load now',
        tone: 'warn',
      })
      if (!ok) return
    }
    try {
      await api('/api/sd/fetch-new', { body: {} })
      setList((l) => (l ? { ...l, waiting: [], busy: true } : l))
    } catch (e) {
      dlg.toast(false, (e as ApiError).message)
    }
  }
  const del = async (f: SdFile) => {
    const ok = await dlg.confirm({
      title: 'Delete file?',
      text: `Delete "${f.name}" from the printer's SD card? This cannot be undone.`,
      ok: 'Delete',
      tone: 'danger',
    })
    if (ok) await api('/api/sd/delete', { body: { name: f.name } }).catch(() => {})
  }

  if (!snap || !online(snap)) {
    return (
      <div className="flex h-full min-h-[50vh] flex-col items-center justify-center gap-3 p-6 text-center">
        <TriangleAlert size={48} className="text-warn" />
        <div className="text-lg font-bold">Not connected</div>
        <div className="text-muted">{snap?.link_text || 'Connecting to the server...'}</div>
      </div>
    )
  }

  let status = 'Tap refresh to load'
  if (list?.loading && !list.listed) status = 'Reading the SD card...'
  else if (list?.error) status = `⚠ ${list.error}`
  else if (list?.listed) status = `${list.files.length} file${list.files.length === 1 ? '' : 's'}${snap.cloud.logged_in ? '' : ' - log in to Bambu Cloud to print'}`

  return (
    <div className="mx-auto flex max-w-[1500px] flex-col gap-3 p-3 md:p-4">
      <div className="flex items-center gap-2">
        <div className="min-w-0 flex-1">
          <h1 className="text-lg font-extrabold">Printer SD card</h1>
          <div className={`flex items-center gap-2 truncate text-sm ${list?.error ? 'text-error' : 'text-muted'}`}>
            {(list?.loading || list?.busy) && <Loader2 size={14} className="shrink-0 animate-spin text-accent" />}
            <span className="truncate">{status}</span>
          </div>
        </div>
        <button className="btn px-3" onClick={toggleView} title={view === 'grid' ? 'List view' : 'Grid view'}>
          {view === 'grid' ? <List size={20} /> : <LayoutGrid size={20} />}
        </button>
        <button className="btn px-3" onClick={refresh} title="Refresh (also retries failed files)">
          <RefreshCw size={20} className={list?.loading ? 'animate-spin' : ''} />
        </button>
      </div>

      {!!list?.waiting?.length && (
        <div className="card flex flex-wrap items-center gap-3 border-info/40 px-4 py-3">
          <Download size={20} className="shrink-0 text-info" />
          <div className="min-w-0 flex-1 text-sm">
            <div className="font-semibold">
              {list.waiting.length} new file{list.waiting.length === 1 ? '' : 's'} - details load in ~{Math.max(1, Math.ceil(Math.max(...list.waiting.map((w) => w.left)) / 60))} min
            </div>
            <div className="truncate text-muted">
              Waiting a little after an upload keeps the printer's FTP free if you print it right away. {list.waiting.map((w) => w.name).join(', ')}
            </div>
          </div>
          <button className="btn-accent h-10 shrink-0" onClick={fetchNew}>
            <Download size={18} /> Load now
          </button>
        </div>
      )}

      {list?.listed && list.files.length === 0 && (
        <div className="card p-8 text-center text-muted">No printable files on the SD card.</div>
      )}
      {!list?.listed && !list?.loading && !list?.error && (
        <div className="card p-8 text-center text-muted">Files on the printer's SD card. Tap refresh to read them from the printer.</div>
      )}

      {view === 'grid' ? (
        <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-4 xl:grid-cols-5 2xl:grid-cols-6">
          {list?.files.map((f) => (
            <GridCard key={f.name + f.size} f={f} previews={snap.settings.file_previews} onOpen={() => setOpen(f)} onDelete={() => del(f)} />
          ))}
        </div>
      ) : (
        <div className="card divide-y divide-border/50 overflow-hidden">
          {list?.files.map((f) => (
            <ListRow key={f.name + f.size} f={f} previews={snap.settings.file_previews} onOpen={() => setOpen(f)} onDelete={() => del(f)} />
          ))}
        </div>
      )}

      {open && (
        <SdFileDialog
          snap={snap}
          file={open}
          onClose={() => setOpen(null)}
          onStarted={() => {
            setOpen(null)
            goHome()
          }}
        />
      )}
    </div>
  )
}

function subText(f: SdFile): string {
  if (f.print_sec > 0 || f.grams > 0) {
    const parts = []
    if (f.print_sec > 0) parts.push(`⟳ ${fmtSec(f.print_sec)}`)
    if (f.grams > 0) parts.push(`${Math.round(f.grams)} g`)
    return parts.join('  •  ')
  }
  return f.failed ? 'details not available' : f.date.slice(0, 10)
}

function Thumb({ f, previews, className }: { f: SdFile; previews: boolean; className: string }) {
  const [failed, setFailed] = useState(false)
  const bust = `${f.thumb ? 1 : 0}${f.plates}`
  useEffect(() => setFailed(false), [bust])
  if (!previews || !f.thumb || failed)
    return (
      <div className={`flex items-center justify-center bg-panel text-card2 ${className}`}>
        <FileBox className="h-2/5 w-2/5" />
      </div>
    )
  return (
    <img
      src={sdThumbUrl(f.name, f.size, 1, bust)}
      alt=""
      loading="lazy"
      onError={() => setFailed(true)}
      className={`bg-panel object-contain ${className}`}
    />
  )
}

function GridCard({ f, previews, onOpen, onDelete }: { f: SdFile; previews: boolean; onOpen: () => void; onDelete: () => void }) {
  const press = useLongPress(onDelete, onOpen)
  return (
    <button {...press} className="card group flex flex-col overflow-hidden text-left transition hover:border-accent/50 hover:shadow-[0_0_20px_-8px_var(--color-accent)]">
      <div className="relative">
        <Thumb f={f} previews={previews} className="aspect-square w-full" />
        {f.plates > 1 && (
          <span className="absolute top-2 right-2 rounded-md bg-black/70 px-1.5 py-0.5 text-[11px] font-bold text-accent backdrop-blur-sm">
            {f.plates} plates
          </span>
        )}
      </div>
      <div className="flex flex-1 flex-col gap-1 p-3">
        <div className="line-clamp-2 text-sm leading-snug font-semibold [overflow-wrap:anywhere]">{f.name}</div>
        <div className="mt-auto text-xs text-muted">{subText(f)}</div>
      </div>
    </button>
  )
}

function ListRow({ f, previews, onOpen, onDelete }: { f: SdFile; previews: boolean; onOpen: () => void; onDelete: () => void }) {
  const press = useLongPress(onDelete, onOpen)
  return (
    <button {...press} className="flex w-full items-center gap-3 px-3 py-2.5 text-left transition hover:bg-card2/50">
      <Thumb f={f} previews={previews} className="h-14 w-14 shrink-0 rounded-lg" />
      <div className="min-w-0 flex-1">
        <div className="flex min-w-0 items-center gap-2">
          <span className="truncate font-semibold">{f.name}</span>
          {f.plates > 1 && (
            <span className="shrink-0 rounded-md bg-accent/15 px-1.5 py-0.5 text-[11px] font-bold text-accent">{f.plates} plates</span>
          )}
        </div>
        <div className="truncate text-xs text-muted">
          {f.date} · {fmtSize(f.size)}
          {(f.print_sec > 0 || f.grams > 0) && <> · {subText(f)}</>}
        </div>
      </div>
    </button>
  )
}
