import { useEffect, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api, endpoints } from '../lib/api'
import type { AssetListOut, JobOut, ProjectDetail } from '../lib/types'
import { formatDuration, formatSize } from '../lib/format'
import { Badge, Button, Card, EmptyState, Spinner, inputClass } from '../components/ui'

const statusTone: Record<string, string> = {
  succeeded: 'green',
  failed: 'red',
  pending: 'yellow',
  running: 'blue',
  skipped: 'slate',
}

export default function Project() {
  const { id = '' } = useParams()
  const qc = useQueryClient()
  const [statusFilter, setStatusFilter] = useState('')
  const [search, setSearch] = useState('')
  const [force, setForce] = useState(false)
  const [fullHash, setFullHash] = useState(false)

  const { data: project } = useQuery({
    queryKey: ['project', id],
    queryFn: () => api<ProjectDetail>(endpoints.project(id)),
  })

  const { data: assets } = useQuery({
    queryKey: ['assets', id, statusFilter, search],
    queryFn: () =>
      api<AssetListOut>(
        endpoints.assets(
          id,
          `?offset=0&limit=200${statusFilter ? `&status=${statusFilter}` : ''}${
            search ? `&q=${encodeURIComponent(search)}` : ''
          }`,
        ),
      ),
  })

  const { data: jobs } = useQuery({
    queryKey: ['jobs', id],
    queryFn: () => api<JobOut[]>(endpoints.jobs(id)),
    refetchInterval: 2000,
  })

  const hasActive = jobs?.some((j) => j.status === 'pending' || j.status === 'running')

  useEffect(() => {
    if (hasActive) {
      qc.invalidateQueries({ queryKey: ['project', id] })
      qc.invalidateQueries({ queryKey: ['assets', id] })
    }
  }, [jobs, hasActive, id, qc])

  const startJob = useMutation({
    mutationFn: (type: 'scan' | 'detect') =>
      api<JobOut>(endpoints.createJob(id, type), { method: 'POST', body: JSON.stringify({ force, options: { full_hash: fullHash } }) }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['jobs', id] }),
  })

  const cancel = useMutation({
    mutationFn: (jid: string) => api<JobOut>(endpoints.cancelJob(jid), { method: 'POST' }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['jobs', id] }),
  })

  if (!project) return <Spinner label="加载项目…" />

  const { stats } = project

  return (
    <div className="mx-auto max-w-6xl px-6 py-8">
      <div className="mb-6 flex flex-wrap items-center justify-between gap-4">
        <div>
          <h1 className="text-2xl font-semibold text-white">{project.name}</h1>
          <div className="mt-1 flex flex-wrap gap-4 text-sm text-slate-400">
            <span>素材 {stats.asset_count} 段</span>
            <span>片段 {stats.segment_count} 个</span>
            <span>时长 {formatDuration(stats.total_duration_sec)}</span>
            <span>体积 {formatSize(stats.total_size_bytes)}</span>
            {stats.asset_failed > 0 && <span className="text-red-400">失败 {stats.asset_failed}</span>}
          </div>
          {project.cache_dir && (
            <div
              className="mt-1 truncate text-xs text-slate-500"
              title={`产物工作区：${project.cache_dir}`}
            >
              产物工作区：{project.cache_dir}
              {project.output_dir ? '' : '（默认位置）'}
              <span className="ml-2">已有缓存不能直接修改目录，需要备份和迁移；未来版本将提供迁移向导。</span>
            </div>
          )}
        </div>
        <div className="flex items-center gap-2">
          <Link to={`/project/${id}/review`}>
            <Button variant="ghost">分镜审阅</Button>
          </Link>
          <Link to={`/project/${id}/ai`}>
            <Button variant="ghost">AI 导演</Button>
          </Link>
          <Link to={`/project/${id}/export`}>
            <Button variant="ghost">导出</Button>
          </Link>
          <Button disabled={hasActive} onClick={() => startJob.mutate('scan')}>
            扫描建库
          </Button>
          <Button
            disabled={hasActive || stats.asset_succeeded === 0}
            onClick={() => startJob.mutate('detect')}
          >
            检测片段
          </Button>
        </div>
      </div>

      <div className="mb-4 flex gap-4 text-xs text-slate-400">
        <label><input type="checkbox" checked={force} onChange={(e) => setForce(e.target.checked)} /> 强制重分析</label>
        <label><input type="checkbox" checked={fullHash} onChange={(e) => setFullHash(e.target.checked)} /> 全量内容校验（读取全部原片，增加 I/O）</label>
        <span>默认头/中/尾快速校验，极端碰撞仍可能漏检。</span>
      </div>
      {startJob.error && <p className="mb-4 text-sm text-red-400">{startJob.error.message}</p>}
      {cancel.error && <p className="mb-4 text-sm text-red-400">{cancel.error.message}</p>}
      {jobs && jobs.length > 0 && (
        <Card className="mb-6">
          <h2 className="mb-2 text-sm font-semibold text-slate-200">任务</h2>
          <ul className="flex flex-col gap-2">
            {jobs.slice(0, 5).map((job) => (
              <li key={job.id} className="flex items-center gap-3 text-sm">
                <Badge tone={statusTone[job.status] ?? 'slate'}>{job.status === 'cancelled' ? '正在停止 / 已取消' : job.status}</Badge>
                {(job.status === 'pending' || job.status === 'running') && <Button variant="ghost" disabled={cancel.isPending} onClick={() => cancel.mutate(job.id)}>取消</Button>}
                {job.status === 'cancelled' && <span className="text-xs text-slate-500">当前编码可能仍在结束，后续项目任务会等待它停止。</span>}
                <span className="text-slate-300">{job.job_type === 'scan' ? '素材建库' : '片段检测'}</span>
                <span className="text-slate-500">
                  {job.stats.done ?? 0}/{job.stats.total ?? 0}
                  {job.stats.failed ? ` · 失败 ${job.stats.failed}` : ''}
                </span>
                {job.error && <span className="truncate text-red-400">{job.error}</span>}
              </li>
            ))}
          </ul>
        </Card>
      )}

      <div className="mb-4 flex flex-wrap items-center gap-3">
        <input
          className={`${inputClass} max-w-xs`}
          placeholder="搜索素材名…"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
        />
        <select
          className={inputClass + ' max-w-[160px]'}
          value={statusFilter}
          onChange={(e) => setStatusFilter(e.target.value)}
        >
          <option value="">全部状态</option>
          <option value="succeeded">成功</option>
          <option value="failed">失败</option>
          <option value="pending">待处理</option>
        </select>
      </div>

      {!assets || assets.items.length === 0 ? (
        <EmptyState
          title="暂无素材"
          hint="点击「扫描建库」开始扫描素材根目录，或先确认根目录路径正确"
        />
      ) : (
        <div className="overflow-hidden rounded-lg border border-ink-700">
          <table className="w-full text-sm">
            <thead className="bg-ink-900 text-left text-slate-400">
              <tr>
                <th className="px-4 py-2 font-medium">素材</th>
                <th className="px-4 py-2 font-medium">状态</th>
                <th className="px-4 py-2 font-medium">分辨率</th>
                <th className="px-4 py-2 font-medium">时长</th>
                <th className="px-4 py-2 font-medium">大小</th>
                <th className="px-4 py-2 font-medium">片段</th>
              </tr>
            </thead>
            <tbody>
              {assets.items.map((a) => (
                <tr key={a.id} className="border-t border-ink-800 hover:bg-ink-900/50">
                  <td className="max-w-[360px] truncate px-4 py-2 text-slate-200" title={a.source_path}>
                    {a.display_name}
                  </td>
                  <td className="px-4 py-2">
                    <Badge tone={statusTone[a.scan_status] ?? 'slate'}>{a.scan_status}</Badge>
                    {a.error_code && <span className="ml-2 text-xs text-red-400">{a.error_code}</span>}
                  </td>
                  <td className="px-4 py-2 text-slate-400">
                    {a.width}×{a.height}
                  </td>
                  <td className="px-4 py-2 text-slate-400">{formatDuration(a.duration_sec)}</td>
                  <td className="px-4 py-2 text-slate-400">{formatSize(a.file_size_bytes)}</td>
                  <td className="px-4 py-2 text-slate-400">{a.segment_count}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}
