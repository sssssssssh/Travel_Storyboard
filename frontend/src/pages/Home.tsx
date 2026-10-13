import { useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api, endpoints } from '../lib/api'
import type { ProjectDetail } from '../lib/types'
import { formatDuration, formatSize } from '../lib/format'
import { Button, Card, EmptyState, Field, Spinner, inputClass } from '../components/ui'

export default function Home() {
  const navigate = useNavigate()
  const qc = useQueryClient()
  const [name, setName] = useState('')
  const [roots, setRoots] = useState('')
  const [outputDir, setOutputDir] = useState('')
  const [formError, setFormError] = useState<string | null>(null)

  const { data: projects, isLoading } = useQuery({
    queryKey: ['projects'],
    queryFn: () => api<ProjectDetail[]>(endpoints.projects),
  })

  const create = useMutation({
    mutationFn: () =>
      api<ProjectDetail>(endpoints.projects, {
        method: 'POST',
        body: JSON.stringify({
          name,
          source_roots: roots.split('\n').map((s) => s.trim()).filter(Boolean),
          output_dir: outputDir.trim(),
          settings: {},
        }),
      }),
    onSuccess: (p) => {
      qc.invalidateQueries({ queryKey: ['projects'] })
      navigate(`/project/${p.id}`)
    },
    onError: (e) => setFormError(e instanceof Error ? e.message : String(e)),
  })

  return (
    <div className="mx-auto max-w-5xl px-6 py-8">
      <h1 className="mb-6 text-2xl font-semibold text-white">项目</h1>

      <Card className="mb-8">
        <h2 className="mb-4 text-sm font-semibold text-slate-200">新建项目</h2>
        <div className="grid gap-4">
          <Field label="项目名称">
            <input
              className={inputClass}
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="例如：土耳其之旅 2026"
            />
          </Field>
          <Field label="素材根目录（每行一个，可递归添加多个）">
            <textarea
              className={`${inputClass} min-h-[72px]`}
              value={roots}
              onChange={(e) => setRoots(e.target.value)}
              placeholder={'D:\\TurkeyTrip\\原片\nD:\\TurkeyTrip\\原片2'}
            />
          </Field>
          <Field label="输出工作区（留空使用默认，产物写入 <该目录>\<项目ID>）">
            <input
              className={inputClass}
              value={outputDir}
              onChange={(e) => setOutputDir(e.target.value)}
              placeholder="D:\\TurkeyTrip_Workspace"
            />
          </Field>
          <p className="text-xs text-slate-500">
            输出工作区不能与素材目录重叠，否则会拒绝创建 —— 这是为了避免产物覆盖原片。
            分镜图、代理、分析包、粗剪都会写到这里，原片始终只读。
          </p>
          {formError && <div className="text-sm text-red-400">{formError}</div>}
          <div>
            <Button disabled={!name.trim() || create.isPending} onClick={() => create.mutate()}>
              {create.isPending ? '创建中…' : '创建项目'}
            </Button>
          </div>
        </div>
      </Card>

      <h2 className="mb-3 text-sm font-semibold text-slate-300">最近项目</h2>
      {isLoading ? (
        <Spinner label="加载项目…" />
      ) : !projects || projects.length === 0 ? (
        <EmptyState title="还没有项目" hint="新建一个项目开始整理你的旅行素材" />
      ) : (
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
          {projects.map((p) => (
            <Link key={p.id} to={`/project/${p.id}`}>
              <Card className="h-full transition-colors hover:border-accent/60">
                <div className="mb-2 flex items-center justify-between">
                  <span className="font-medium text-white">{p.name}</span>
                  <span className="text-xs text-slate-500">
                    {new Date(p.created_at).toLocaleDateString('zh-CN')}
                  </span>
                </div>
                <div className="grid grid-cols-2 gap-x-4 gap-y-1 text-sm text-slate-400">
                  <span>素材 {p.stats.asset_count} 段</span>
                  <span>片段 {p.stats.segment_count} 个</span>
                  <span>时长 {formatDuration(p.stats.total_duration_sec)}</span>
                  <span>体积 {formatSize(p.stats.total_size_bytes)}</span>
                </div>
                {p.cache_dir && (
                  <div className="mt-2 truncate text-xs text-slate-600" title={`产物工作区：${p.cache_dir}`}>
                    产物：{p.cache_dir}
                  </div>
                )}
              </Card>
            </Link>
          ))}
        </div>
      )}
    </div>
  )
}
