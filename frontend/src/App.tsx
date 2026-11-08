import { BarChart3, Boxes, Disc3, FolderOpen, Gauge, Home as HomeIcon, Settings as SettingsIcon } from 'lucide-react'
import { useCallback, useEffect, useState, type ComponentType } from 'react'
import { api, setUnauthorizedHandler, type ApiError } from './api'
import { startLive, stopLive, useSnapshot, useSocketConnected } from './live'
import Ams from './pages/Ams'
import Control from './pages/Control'
import Files from './pages/Files'
import Home from './pages/Home'
import Settings from './pages/Settings'
import Stats from './pages/Stats'
import Spools from './pages/Spools'
import { DownloadsTray } from './downloads'
import { PlateSeqBar } from './plateseq'
import { PrintOverlays } from './PrintScreens'
import { TopBar } from './TopBar'
import { DialogProvider, useNoticeToasts } from './ui'

type Page = 'home' | 'control' | 'files' | 'ams' | 'spools' | 'stats' | 'settings'

const NAV: { id: Page; label: string; icon: ComponentType<{ size?: number }> }[] = [
  { id: 'home', label: 'Home', icon: HomeIcon },
  { id: 'control', label: 'Control', icon: Gauge },
  { id: 'files', label: 'Files', icon: FolderOpen },
  { id: 'ams', label: 'AMS', icon: Boxes },
  { id: 'spools', label: 'Spools', icon: Disc3 },
  { id: 'stats', label: 'Stats', icon: BarChart3 },
  { id: 'settings', label: 'Settings', icon: SettingsIcon },
]

function pageFromHash(): Page {
  const h = location.hash.replace('#/', '') as Page
  return NAV.some((n) => n.id === h) ? h : 'home'
}

export default function App() {
  const [auth, setAuth] = useState<'loading' | 'setup' | 'login' | 'ok'>('loading')

  const check = useCallback(() => {
    api<{ setup_needed: boolean; logged_in: boolean }>('/api/auth/state')
      .then((s) => setAuth(s.setup_needed ? 'setup' : s.logged_in ? 'ok' : 'login'))
      .catch(() => window.setTimeout(check, 3000))
  }, [])

  useEffect(() => {
    check()
    setUnauthorizedHandler(() => setAuth('login'))
  }, [check])

  if (auth === 'loading') return <div className="flex h-full items-center justify-center text-muted">Loading...</div>
  if (auth !== 'ok') return <Login setup={auth === 'setup'} onDone={() => setAuth('ok')} />
  return (
    <DialogProvider>
      <Shell onLoggedOut={() => setAuth('login')} />
    </DialogProvider>
  )
}

function Shell({ onLoggedOut }: { onLoggedOut: () => void }) {
  const [page, setPage] = useState<Page>(pageFromHash)
  const snap = useSnapshot()
  const socket = useSocketConnected()
  useNoticeToasts(snap?.notices)

  useEffect(() => {
    startLive(onLoggedOut)
    return () => stopLive()
  }, [onLoggedOut])

  useEffect(() => {
    const onHash = () => setPage(pageFromHash())
    window.addEventListener('hashchange', onHash)
    return () => window.removeEventListener('hashchange', onHash)
  }, [])

  const go = (p: Page) => {
    location.hash = `#/${p}`
    setPage(p)
  }

  return (
    <div className="flex h-full">
      {/* desktop / tablet: side rail */}
      <nav className="hidden w-24 shrink-0 flex-col gap-1 overflow-y-auto border-r border-border/60 bg-panel p-2 wide:flex">
        <div className="mb-3" />
        {NAV.map(({ id, label, icon: Icon }) => (
          <button
            key={id}
            onClick={() => go(id)}
            className={`flex flex-col items-center gap-1 rounded-xl border-l-4 py-3 text-xs font-bold transition ${
              page === id ? 'border-accent bg-card2 text-accent' : 'border-transparent text-muted hover:bg-card hover:text-text'
            }`}
          >
            <span className="relative">
              <Icon size={24} />
              {id === 'ams' && !!snap?.usage_pending && <span className="absolute -top-1 -right-1.5 h-2.5 w-2.5 rounded-full bg-error" title="Filament use not booked yet" />}
            </span>
            {label.toUpperCase()}
          </button>
        ))}
      </nav>

      <div className="flex min-w-0 flex-1 flex-col">
        <TopBar snap={snap} />
        <PlateSeqBar snap={snap} />
        {!socket && (
          <div className="bg-warn/15 px-4 py-1.5 text-center text-xs font-semibold text-warn">Reconnecting to the server...</div>
        )}
        <main className="safe-x min-h-0 flex-1 overflow-y-auto">
          {page === 'home' && <Home snap={snap} goSettings={() => go('settings')} goPage={go} />}
          {page === 'settings' && <Settings snap={snap} onLogout={onLoggedOut} />}
          {page === 'control' && <Control snap={snap} />}
          {page === 'files' && <Files snap={snap} goHome={() => go('home')} />}
          {page === 'ams' && <Ams snap={snap} />}
          {page === 'spools' && <Spools snap={snap} />}
          {page === 'stats' && <Stats snap={snap} />}
        </main>
        {/* phone: bottom tab bar */}
        <nav className="safe-bottom grid shrink-0 grid-cols-7 border-t border-border/60 bg-panel/95 backdrop-blur wide:hidden">
          {NAV.map(({ id, label, icon: Icon }) => (
            <button
              key={id}
              onClick={() => go(id)}
              className={`flex flex-col items-center gap-0.5 py-2 text-[10px] font-bold ${page === id ? 'text-accent' : 'text-muted'}`}
            >
              <span className="relative">
                <Icon size={22} />
                {id === 'ams' && !!snap?.usage_pending && <span className="absolute -top-1 -right-1.5 h-2.5 w-2.5 rounded-full bg-error" />}
              </span>
              {label}
            </button>
          ))}
        </nav>
      </div>
      <PrintOverlays snap={snap} />
      <DownloadsTray />
    </div>
  )
}

function Login({ setup, onDone }: { setup: boolean; onDone: () => void }) {
  const [pw, setPw] = useState('')
  const [pw2, setPw2] = useState('')
  const [err, setErr] = useState('')
  const [busy, setBusy] = useState(false)
  const submit = async (e: React.FormEvent) => {
    e.preventDefault()
    setErr('')
    if (setup && pw !== pw2) {
      setErr('The two passwords differ.')
      return
    }
    setBusy(true)
    try {
      await api(setup ? '/api/auth/setup' : '/api/auth/login', { body: { password: pw } })
      onDone()
    } catch (ex) {
      setErr((ex as ApiError).message)
    } finally {
      setBusy(false)
    }
  }
  return (
    <div className="safe-top flex h-full items-center justify-center p-6">
      <form onSubmit={submit} className="card flex w-full max-w-sm flex-col gap-4 p-6">
        <img src="/icon-192.png" alt="" className="mx-auto h-20 w-20 rounded-2xl" />
        <h1 className="text-center text-xl font-extrabold tracking-wide">
          <span className="neon-text">PRINTER</span>DASH
        </h1>
        {setup && <p className="text-center text-sm text-muted">First start: choose a password or PIN for this app.</p>}
        <input
          className="input text-center"
          type="password"
          autoComplete={setup ? 'new-password' : 'current-password'}
          placeholder={setup ? 'New password / PIN' : 'Password / PIN'}
          value={pw}
          onChange={(e) => setPw(e.target.value)}
          autoFocus
        />
        {setup && (
          <input
            className="input text-center"
            type="password"
            autoComplete="new-password"
            placeholder="Repeat"
            value={pw2}
            onChange={(e) => setPw2(e.target.value)}
          />
        )}
        {err && <p className="text-center text-sm font-semibold text-error">{err}</p>}
        <button className="btn-accent h-12" disabled={busy || pw.length < (setup ? 4 : 1)}>
          {setup ? 'Save & log in' : 'Log in'}
        </button>
      </form>
    </div>
  )
}
