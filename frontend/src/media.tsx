// The HOME picture: the print's preview (cover), the latest camera frame, or the live camera.
import { Box, Camera, Image as ImageIcon, Maximize2, Video, VideoOff, X } from 'lucide-react'
import { Progress3D } from './progress3d'
import { createPortal } from 'react-dom'
import { useEffect, useRef, useState, useSyncExternalStore } from 'react'
import type { Snapshot } from './types'

export type MediaMode = 'preview' | 'snapshot' | 'live' | '3d'

const KEY = 'home-media'
const listeners = new Set<() => void>()
function read(): MediaMode {
  try {
    const v = localStorage.getItem(KEY)
    return v === 'snapshot' || v === 'live' || v === '3d' ? v : 'preview'
  } catch {
    return 'preview'
  }
}
let current: MediaMode = read()

/** Shared by every picture on the page (phone hero, tablet / desktop card). */
export function useMediaMode(): [MediaMode, (m: MediaMode) => void] {
  const mode = useSyncExternalStore(
    (fn) => {
      listeners.add(fn)
      return () => listeners.delete(fn)
    },
    () => current,
  )
  const set = (m: MediaMode) => {
    current = m
    try {
      localStorage.setItem(KEY, m)
    } catch {
      /* private mode */
    }
    listeners.forEach((fn) => fn())
  }
  return [mode, set]
}

const MODES: { id: MediaMode; label: string; icon: typeof ImageIcon }[] = [
  { id: 'preview', label: 'Preview', icon: ImageIcon },
  { id: 'snapshot', label: 'Camera', icon: Camera },
  { id: 'live', label: 'Live', icon: Video },
  { id: '3d', label: '3D', icon: Box },
]

export function MediaSwitch({ compact }: { compact?: boolean }) {
  const [mode, setMode] = useMediaMode()
  return (
    <div className="flex gap-0.5 rounded-lg bg-black/55 p-0.5 backdrop-blur-sm" onClick={(e) => e.stopPropagation()}>
      {MODES.map(({ id, label, icon: Icon }) => (
        <button
          key={id}
          title={id === 'snapshot' ? 'Latest camera frame (every 10 s)' : id === 'live' ? 'Live camera' : id === '3d' ? 'Where the print is, layer by layer' : 'Print preview'}
          onClick={() => setMode(id)}
          className={`flex items-center gap-1.5 rounded-md px-2 py-1 text-xs font-bold transition ${
            mode === id ? 'bg-card2 text-accent' : 'text-dim hover:text-text'
          }`}
        >
          <Icon size={14} />
          {!compact && label}
          {id === 'live' && mode === 'live' && <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-error" />}
        </button>
      ))}
    </div>
  )
}

const SNAPSHOT_EVERY_MS = 10000

/** Re-fetches the camera frame every 10 s while the page is visible. */
function useSnapshotUrl(active: boolean): string {
  const [t, setT] = useState(() => Date.now())
  useEffect(() => {
    if (!active) return
    setT(Date.now())
    const tick = () => document.visibilityState === 'visible' && setT(Date.now())
    const id = window.setInterval(tick, SNAPSHOT_EVERY_MS)
    document.addEventListener('visibilitychange', tick)
    return () => {
      window.clearInterval(id)
      document.removeEventListener('visibilitychange', tick)
    }
  }, [active])
  return `/api/camera/snapshot?t=${t}`
}

/** Live MJPEG only while the tab is visible (a hidden tab drops the stream). */
function useVisible(): boolean {
  const [v, setV] = useState(() => document.visibilityState === 'visible')
  useEffect(() => {
    const on = () => setV(document.visibilityState === 'visible')
    document.addEventListener('visibilitychange', on)
    return () => document.removeEventListener('visibilitychange', on)
  }, [])
  return v
}

export function MediaView({
  snap,
  className = '',
  onClick,
  switcher = true,
  compactSwitch,
}: {
  snap: Snapshot
  className?: string
  onClick?: () => void
  switcher?: boolean
  compactSwitch?: boolean
}) {
  const [mode] = useMediaMode()
  const visible = useVisible()
  const snapUrl = useSnapshotUrl(mode === 'snapshot')
  const [err, setErr] = useState('')
  // Keep the last good camera frame on screen while the next one loads (no flicker).
  const [shown, setShown] = useState('')
  const [full, setFull] = useState(false)
  useEffect(() => setErr(''), [mode])

  const click = mode === 'preview' ? onClick : undefined

  let content
  if (mode === '3d') {
    const printing = !!snap.status && ['PREPARE', 'SLICING', 'RUNNING', 'PAUSE'].includes(snap.status.state)
    content = printing ? <Progress3D snap={snap} /> : <Placeholder icon={<Box className="h-1/4 w-1/4" />} text="Shown while a print runs" />
  } else if (mode === 'preview') {
    content = snap.cover_job ? (
      <img src={`/api/cover?v=${snap.cover_version}`} alt="" className="h-full w-full object-contain" />
    ) : (
      <Placeholder icon={<ImageIcon className="h-1/4 w-1/4" />} text="No print preview" />
    )
  } else if (err) {
    content = (
      <Placeholder icon={<VideoOff className="h-1/5 w-1/5" />} text={err}>
        <CameraRestart onDone={() => setErr('')} />
      </Placeholder>
    )
  } else if (mode === 'snapshot') {
    content = (
      <>
        {shown && <img src={shown} alt="" className="absolute inset-0 h-full w-full object-contain" />}
        <img
          key={snapUrl}
          src={snapUrl}
          alt=""
          className="absolute inset-0 h-full w-full object-contain opacity-0"
          onLoad={() => setShown(snapUrl)}
          onError={() => !shown && setErr('Camera not available')}
        />
      </>
    )
  } else {
    content = visible ? (
      <img src="/api/camera/stream?fps=5" alt="" className="h-full w-full object-contain" onError={() => setErr('Live camera not available')} />
    ) : (
      <Placeholder icon={<Video className="h-1/5 w-1/5" />} text="Paused while the page is hidden" />
    )
  }

  return (
    <div
      onClick={click}
      className={`relative overflow-hidden rounded-xl ${mode === 'preview' ? 'bg-panel' : 'bg-black'} ${click ? 'cursor-zoom-in' : ''} ${className}`}
    >
      {full && mode !== 'preview' ? <Placeholder icon={<Maximize2 className="h-1/6 w-1/6" />} text="Shown full screen" /> : content}
      {full && <FullView onClose={() => setFull(false)}>{content}</FullView>}
      {switcher && (
        <div className="absolute top-2 right-2 flex items-center gap-1.5">
          <MediaSwitch compact={compactSwitch} />
          {mode !== 'preview' && (
            <button
              title="Full screen"
              onClick={(e) => {
                e.stopPropagation()
                setFull(true)
              }}
              className="rounded-lg bg-black/55 p-1.5 text-dim backdrop-blur-sm hover:text-text"
            >
              <Maximize2 size={14} />
            </button>
          )}
        </div>
      )}
    </div>
  )
}

/** Full-screen camera: our own layer over the whole page (iPhone Safari has no element
 * fullscreen for images), plus the browser's real fullscreen where it exists. */
function FullView({ children, onClose }: { children: React.ReactNode; onClose: () => void }) {
  const ref = useRef<HTMLDivElement>(null)
  const close = useRef(onClose)
  close.current = onClose
  useEffect(() => {
    const el = ref.current
    el?.requestFullscreen?.().catch(() => {})
    const onKey = (e: KeyboardEvent) => e.key === 'Escape' && close.current()
    // Leaving the browser's fullscreen (Esc, back gesture) closes the layer too.
    const onFs = () => !document.fullscreenElement && close.current()
    window.addEventListener('keydown', onKey)
    document.addEventListener('fullscreenchange', onFs)
    return () => {
      window.removeEventListener('keydown', onKey)
      document.removeEventListener('fullscreenchange', onFs)
      if (document.fullscreenElement) document.exitFullscreen?.().catch(() => {})
    }
  }, [])
  return createPortal(
    <div ref={ref} className="fixed inset-0 z-[90] bg-black" onClick={(e) => e.stopPropagation()}>
      <div className="absolute inset-0">{children}</div>
      <div className="safe-top safe-x absolute top-0 right-0 flex items-center gap-2 p-3">
        <MediaSwitch />
        <button title="Close" onClick={onClose} className="rounded-xl bg-black/60 p-2.5 text-text backdrop-blur-sm">
          <X size={22} />
        </button>
      </div>
    </div>,
    document.body,
  )
}

function Placeholder({ icon, text, children }: { icon: React.ReactNode; text: string; children?: React.ReactNode }) {
  return (
    <div className="flex h-full w-full flex-col items-center justify-center gap-2 p-4 text-center text-sm text-muted">
      <span className="flex w-full justify-center text-card2">{icon}</span>
      {text}
      {children}
    </div>
  )
}

/** The printer's camera hangs now and then: turn it off and on over MQTT. */
export function CameraRestart({ onDone }: { onDone?: () => void }) {
  const [busy, setBusy] = useState(false)
  const [msg, setMsg] = useState('')
  return (
    <div className="flex flex-col items-center gap-1" onClick={(e) => e.stopPropagation()}>
      <button
        className="btn h-9 text-sm"
        disabled={busy}
        onClick={async () => {
          setBusy(true)
          setMsg('Turning the camera off and on... (~15 s)')
          try {
            const r = await fetch('/api/camera/restart', { method: 'POST', credentials: 'same-origin' }).then((x) => x.json())
            setMsg(r.message || '')
            if (r.ok) onDone?.()
          } catch {
            setMsg('Restart failed')
          }
          setBusy(false)
        }}
      >
        {busy ? 'Restarting...' : 'Restart camera'}
      </button>
      {msg && <span className="text-xs text-muted">{msg}</span>}
    </div>
  )
}
