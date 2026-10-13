import { useEffect } from 'react'
import { HashRouter, NavLink, Route, Routes } from 'react-router-dom'
import { useApp } from './lib/store'
import { Spinner } from './components/ui'
import Home from './pages/Home'
import Project from './pages/Project'
import Review from './pages/Review'
import AIDirector from './pages/AIDirector'
import Export from './pages/Export'
import Settings from './pages/Settings'

const navClass = ({ isActive }: { isActive: boolean }) =>
  `block rounded-md px-3 py-2 text-sm transition-colors ${
    isActive ? 'bg-ink-700 text-white' : 'text-slate-400 hover:bg-ink-800 hover:text-slate-200'
  }`

function Shell() {
  return (
    <div className="flex h-screen overflow-hidden">
      <aside className="flex w-52 shrink-0 flex-col border-r border-ink-800 bg-ink-900">
        <div className="px-4 py-5">
          <div className="text-lg font-semibold text-white">Travel Storyboard</div>
          <div className="text-xs text-slate-500">AI 旅行影片导演助手</div>
        </div>
        <nav className="flex flex-col gap-1 px-2">
          <NavLink to="/" end className={navClass}>
            项目
          </NavLink>
          <NavLink to="/settings" className={navClass}>
            设置
          </NavLink>
        </nav>
        <div className="mt-auto px-4 py-4 text-xs text-slate-600">v2.0.3 · 本地运行</div>
      </aside>
      <main className="flex-1 overflow-auto">
        <Routes>
          <Route path="/" element={<Home />} />
          <Route path="/project/:id" element={<Project />} />
          <Route path="/project/:id/review" element={<Review />} />
          <Route path="/project/:id/ai" element={<AIDirector />} />
          <Route path="/project/:id/export" element={<Export />} />
          <Route path="/settings" element={<Settings />} />
        </Routes>
      </main>
    </div>
  )
}

export default function App() {
  const ready = useApp((s) => s.ready)
  const error = useApp((s) => s.error)
  const boot = useApp((s) => s.boot)

  useEffect(() => {
    void boot()
  }, [boot])

  if (!ready) {
    return (
      <div className="flex h-screen items-center justify-center">
        <Spinner label="正在初始化…" />
      </div>
    )
  }

  if (error) {
    return (
      <div className="flex h-screen items-center justify-center">
        <div className="text-red-400">无法连接后端：{error}</div>
      </div>
    )
  }

  return (
    <HashRouter>
      <Shell />
    </HashRouter>
  )
}
