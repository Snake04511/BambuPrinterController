// Shared UI: modal, confirm, number pad, choice list, toasts (panel src/ui/Widgets.cpp).
import { X } from 'lucide-react'
import { createContext, useCallback, useContext, useEffect, useRef, useState, type ReactNode } from 'react'
import { createPortal } from 'react-dom'
import type { Notice } from './types'

// ---------------------------------------------------------------- Modal

export function Modal({
  title,
  onClose,
  children,
  wide = false,
  footer,
}: {
  title: ReactNode
  onClose: () => void
  children: ReactNode
  wide?: boolean
  footer?: ReactNode
}) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === 'Escape' && onClose()
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])
  // Portal to <body>: an ancestor with backdrop-filter / transform (e.g. the top bar)
  // would otherwise become the containing block of this fixed overlay.
  return createPortal(
    <div
      className="fixed inset-0 z-50 flex items-end justify-center bg-black/70 backdrop-blur-[2px] sm:items-center sm:p-4"
      onMouseDown={(e) => e.target === e.currentTarget && onClose()}
    >
      <div
        className={`safe-bottom flex max-h-[92dvh] w-full flex-col overflow-hidden rounded-t-3xl border border-border bg-panel shadow-2xl sm:rounded-3xl ${wide ? 'sm:max-w-3xl' : 'sm:max-w-lg'}`}
      >
        <div className="flex items-center gap-3 border-b border-border/60 px-5 py-3.5">
          <div className="min-w-0 flex-1 truncate text-lg font-bold">{title}</div>
          <button className="btn min-h-10 px-2.5" onClick={onClose} aria-label="Close">
            <X size={20} />
          </button>
        </div>
        <div className="min-h-0 flex-1 overflow-y-auto px-5 py-4">{children}</div>
        {footer && <div className="flex flex-wrap justify-end gap-2 border-t border-border/60 px-5 py-3">{footer}</div>}
      </div>
    </div>,
    document.body,
  )
}

// ---------------------------------------------------------------- dialog manager

interface ConfirmOpts {
  title: string
  text: string
  ok: string
  tone?: 'danger' | 'accent' | 'warn'
}
interface NumOpts {
  title: string
  min: number
  max: number
  value: number
  unit?: string
  presets?: number[]
}
export interface Choice<T> {
  label: ReactNode
  value: T
  selected?: boolean
}
interface ChooseOpts<T> {
  title: string
  choices: Choice<T>[]
}

type Dialog =
  | { kind: 'confirm'; opts: ConfirmOpts; resolve: (v: boolean) => void }
  | { kind: 'num'; opts: NumOpts; resolve: (v: number | null) => void }
  | { kind: 'choose'; opts: ChooseOpts<unknown>; resolve: (v: unknown) => void }

interface DialogApi {
  confirm: (o: ConfirmOpts) => Promise<boolean>
  numpad: (o: NumOpts) => Promise<number | null>
  choose: <T>(o: ChooseOpts<T>) => Promise<T | null>
  toast: (ok: boolean, text: string) => void
}

const Ctx = createContext<DialogApi | null>(null)

export function useDialogs(): DialogApi {
  const c = useContext(Ctx)
  if (!c) throw new Error('DialogProvider missing')
  return c
}

export function DialogProvider({ children }: { children: ReactNode }) {
  const [dialog, setDialog] = useState<Dialog | null>(null)
  const [toasts, setToasts] = useState<{ id: number; ok: boolean; text: string }[]>([])
  const seq = useRef(1_000_000)

  const toast = useCallback((ok: boolean, text: string) => {
    const id = ++seq.current
    setToasts((t) => [...t.slice(-2), { id, ok, text }])
    window.setTimeout(() => setToasts((t) => t.filter((x) => x.id !== id)), 3200)
  }, [])

  const api: DialogApi = {
    confirm: (opts) => new Promise((resolve) => setDialog({ kind: 'confirm', opts, resolve })),
    numpad: (opts) => new Promise((resolve) => setDialog({ kind: 'num', opts, resolve })),
    choose: <T,>(opts: ChooseOpts<T>) =>
      new Promise<T | null>((resolve) =>
        setDialog({ kind: 'choose', opts: opts as ChooseOpts<unknown>, resolve: resolve as (v: unknown) => void }),
      ),
    toast,
  }

  const close = (v: unknown) => {
    if (!dialog) return
    ;(dialog.resolve as (x: unknown) => void)(v)
    setDialog(null)
  }

  return (
    <Ctx.Provider value={api}>
      {children}
      {dialog?.kind === 'confirm' && <ConfirmDialog opts={dialog.opts} close={close} />}
      {dialog?.kind === 'num' && <NumPad opts={dialog.opts} close={close} />}
      {dialog?.kind === 'choose' && <ChooseDialog opts={dialog.opts} close={close} />}
      <div className="pointer-events-none fixed inset-x-0 bottom-[calc(5.5rem+env(safe-area-inset-bottom))] z-[70] flex flex-col items-center gap-2 px-4 wide:bottom-6">
        {toasts.map((t) => (
          <div
            key={t.id}
            className={`toast-in max-w-md rounded-xl px-4 py-2.5 text-sm font-semibold text-white shadow-xl ${t.ok ? 'bg-[#1E7D45]' : 'bg-[#B3261E]'}`}
          >
            {t.text}
          </div>
        ))}
      </div>
    </Ctx.Provider>
  )
}

/** Shows backend notices (command results, automations) as toasts, each once. */
export function useNoticeToasts(notices: Notice[] | undefined) {
  const { toast } = useDialogs()
  const seen = useRef<number | null>(null)
  useEffect(() => {
    if (!notices) return
    const newest = notices.reduce((m, n) => Math.max(m, n.id), 0)
    if (seen.current === null) {
      seen.current = newest // don't replay old ones after (re)connecting
      return
    }
    for (const n of notices) if (n.id > seen.current) toast(n.ok, n.text)
    seen.current = Math.max(seen.current, newest)
  }, [notices, toast])
}

function ConfirmDialog({ opts, close }: { opts: ConfirmOpts; close: (v: boolean) => void }) {
  const cls = opts.tone === 'danger' ? 'btn-danger' : opts.tone === 'warn' ? 'btn-warn' : 'btn-accent'
  return (
    <Modal
      title={opts.title}
      onClose={() => close(false)}
      footer={
        <>
          <button className="btn" onClick={() => close(false)}>
            Cancel
          </button>
          <button className={cls} onClick={() => close(true)} autoFocus>
            {opts.ok}
          </button>
        </>
      }
    >
      <p className="text-base leading-relaxed text-dim">{opts.text}</p>
    </Modal>
  )
}

function NumPad({ opts, close }: { opts: NumOpts; close: (v: number | null) => void }) {
  const [text, setText] = useState(String(opts.value))
  const [fresh, setFresh] = useState(true) // first digit replaces the initial value
  const press = (d: string) => {
    setText((t) => {
      const next = fresh || t === '0' ? d : t + d
      return Number(next) > 99999 ? t : next
    })
    setFresh(false)
  }
  const back = () => {
    setFresh(false)
    setText((t) => (t.length > 1 ? t.slice(0, -1) : '0'))
  }
  const ok = () => close(Math.min(opts.max, Math.max(opts.min, Number(text) || 0)))
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (/^\d$/.test(e.key)) press(e.key)
      else if (e.key === 'Backspace') back()
      else if (e.key === 'Enter') ok()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  })
  return (
    <Modal title={opts.title} onClose={() => close(null)}>
      <div className="mb-1 text-center text-5xl font-bold tabular-nums neon-text">
        {text}
        {opts.unit && <span className="ml-1 text-2xl text-muted">{opts.unit}</span>}
      </div>
      <div className="mb-4 text-center text-sm text-muted">
        Range {opts.min} - {opts.max}
      </div>
      {opts.presets && (
        <div className="mb-3 flex flex-wrap justify-center gap-2">
          {opts.presets.map((p) => (
            <button key={p} className="btn min-w-16" onClick={() => close(p)}>
              {p === 0 ? 'Off' : p}
            </button>
          ))}
        </div>
      )}
      <div className="grid grid-cols-3 gap-2">
        {['1', '2', '3', '4', '5', '6', '7', '8', '9'].map((d) => (
          <button key={d} className="btn h-14 text-xl" onClick={() => press(d)}>
            {d}
          </button>
        ))}
        <button className="btn h-14 text-xl" onClick={back} aria-label="Backspace">
          ⌫
        </button>
        <button className="btn h-14 text-xl" onClick={() => press('0')}>
          0
        </button>
        <button className="btn-accent h-14 text-xl" onClick={ok}>
          OK
        </button>
      </div>
    </Modal>
  )
}

function ChooseDialog({ opts, close }: { opts: ChooseOpts<unknown>; close: (v: unknown) => void }) {
  return (
    <Modal title={opts.title} onClose={() => close(null)}>
      <div className="flex flex-col gap-2">
        {opts.choices.map((c, i) => (
          <button
            key={i}
            className={`btn h-14 justify-start text-left text-base ${c.selected ? 'bg-accent-dark text-accent ring-1 ring-accent' : ''}`}
            onClick={() => close(c.value)}
          >
            {c.label}
          </button>
        ))}
      </div>
    </Modal>
  )
}

// ---------------------------------------------------------------- small pieces

export function Swatch({ color, size = 18 }: { color: string; size?: number }) {
  return (
    <span
      className="inline-block shrink-0 rounded-full border border-white/20"
      style={{ width: size, height: size, background: color || 'transparent' }}
    />
  )
}

export function Toggle({ checked, onChange, disabled }: { checked: boolean; onChange: (v: boolean) => void; disabled?: boolean }) {
  return (
    <button
      role="switch"
      aria-checked={checked}
      disabled={disabled}
      onClick={() => onChange(!checked)}
      className={`relative h-7 w-12 shrink-0 rounded-full transition disabled:opacity-40 ${checked ? 'bg-accent shadow-[0_0_12px_-2px_var(--color-accent)]' : 'bg-card2'}`}
    >
      <span
        className={`absolute top-1 h-5 w-5 rounded-full bg-white shadow transition-all ${checked ? 'left-6' : 'left-1'}`}
      />
    </button>
  )
}

/** Long press (hold 550 ms) + normal click on one element. */
export function useLongPress(onLong: () => void, onClick: () => void) {
  const timer = useRef<number | undefined>(undefined)
  const fired = useRef(false)
  const start = () => {
    fired.current = false
    timer.current = window.setTimeout(() => {
      fired.current = true
      onLong()
    }, 550)
  }
  const cancel = () => window.clearTimeout(timer.current)
  return {
    onPointerDown: start,
    onPointerUp: cancel,
    onPointerLeave: cancel,
    onContextMenu: (e: React.MouseEvent) => e.preventDefault(),
    onClick: () => {
      if (!fired.current) onClick()
    },
  }
}
