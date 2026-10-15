import CleanupNotice from '../components/CleanupNotice'
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
  // TSB-261: a cancel has been requested but the worker is still stopping.
  cancelling: 'yellow',
  cancelled: 'slate',
  skipped: 'slate',
}

const statusLabel: Record<string, string> = {
  pending: '等待中',
  running: '运行中',
  cancelling: '正在取消 / 资源清理中',
  cancelled: '已取消',
  succeeded: '已完成',
  partial_failed: '部分失败',
  failed: '失败',
}

/** Statuses that still occupy the project: a `cancelling` job has not stopped. */
const OCCUPYING_STATUSES = ['pending', 'running', 'cancelling']

export default function Project() {
  const { id = '' } = useParams()
  const qc = useQueryClient()
  const [statusFilter, setStatusFilter] = useState('')
  const [search, setSearch] = useState('')
  const [force, setForce] = useState(false)
  const [fullHash, setFullHash] = useState(false)
  const [strictHash, setStrictHash] = useState(false)

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

  // TSB-261: `cancelling` still occupies the project, so the heavy buttons must
  // stay disabled until the worker has actually stopped and cleaned up.
  const hasActive = jobs?.some((j) => OCCUPYING_STATUSES.includes(j.status))

  useEffect(() => {
    if (hasActive) {
      qc.invalidateQueries({ queryKey: ['project', id] })
      qc.invalidateQueries({ queryKey: ['assets', id] })
    }
  }, [jobs, hasActive, id, qc])

  const startJob = useMutation({
    mutationFn: (type: 'scan' | 'detect') =>
      api<JobOut>(endpoints.createJob(id, type), { method: 'POST', body: JSON.stringify({ force, options: { full_hash: fullHash, strict_full_hash: strictHash } }) }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['jobs', id] }),
  })

  const retryCleanup = useMutation({
    mutationFn: (jid: string) => api<JobOut>(`/api/projects/${id}/jobs/${jid}/cleanup-retry`, { method: 'POST' }),
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
        <label><input type="checkbox" checked={force} onChange={(e) => setForce(e.target.checked)} /> 强制重新探测 / 重分析</label>
        <label><input type="checkbox" checked={fullHash} onChange={(e) => { setFullHash(e.target.checked); if (!e.target.checked) setStrictHash(false) }} /> 后台完整校验（每文件完整读取一遍）</label>
        <label><input type="checkbox" disabled={!fullHash} checked={strictHash} onChange={(e) => setStrictHash(e.target.checked)} /> 严格复核（处理后再完整读取一遍）</label>
        <span>默认头/中/尾快速校验，极端碰撞仍可能漏检；扫描强制选项重新 FFprobe，检测强制选项重新抽帧。</span>
      </div>
      <p className="mb-4 text-xs text-amber-300">完整哈希按每个任务计算读取成本：200GB 素材分别运行完整哈希扫描和检测，可能额外读取约 400GB；严格二次复核继续增加读取量。中断重读与 FFmpeg 解码 I/O 另计，不承诺固定完成时间。</p>
      {startJob.error && <p className="mb-4 text-sm text-red-400">{startJob.error.message}</p>}
      {retryCleanup.error && <p className="mb-4 text-sm text-red-400">{retryCleanup.error.message}</p>}
      {cancel.error && <p className="mb-4 text-sm text-red-400">{cancel.error.message}</p>}
      {jobs && jobs.length > 0 && (
        <Card className="mb-6">
          <h2 className="mb-2 text-sm font-semibold text-slate-200">任务</h2>
          <ul className="flex flex-col gap-2">
            {jobs.slice(0, 5).map((job) => (
              <li key={job.id} className="flex flex-wrap items-center gap-3 text-sm">
                <Badge tone={job.stats.cleanup?.ok === false || (job.status === 'succeeded' && ['vision_analyze', 'motion_review'].includes(job.job_type) && !job.stats.cleanup) ? 'yellow' : statusTone[job.status] ?? 'slate'}>{job.status === 'succeeded' && (job.stats.cleanup?.ok === false || (['vision_analyze', 'motion_review'].includes(job.job_type) && !job.stats.cleanup)) ? '业务成功 · 清理需核对' : statusLabel[job.status] ?? job.status}</Badge>
                {(job.status === 'pending' || job.status === 'running') && <Button variant="ghost" disabled={cancel.isPending} onClick={() => cancel.mutate(job.id)}>取消</Button>}
                {job.status === 'cancelling' && <span className="text-xs text-amber-300">已收到取消请求，正在停止编码并清理暂存；完成后才会释放项目锁。</span>}
                {!OCCUPYING_STATUSES.includes(job.status) && (job.status === 'cancelled' || job.stats.cleanup || ['vision_analyze', 'motion_review'].includes(job.job_type)) && <CleanupNotice cleanup={job.stats.cleanup} />}
                {!hasActive && !OCCUPYING_STATUSES.includes(job.status) && job.stats.cleanup?.ok === false && job.stats.cleanup.retryable && <Button variant="ghost" disabled={retryCleanup.isPending} onClick={() => retryCleanup.mutate(job.id)}>安全重试清理</Button>}
                <span className="text-slate-300">{{ scan: '素材建库', detect: '片段检测', package: '分析包', proxies: '二轮代理', roughcut: '粗剪' }[job.job_type] ?? job.job_type}</span>
                <span className="text-xs text-slate-500" title={job.id}>编号 {job.id.slice(0, 8)}</span>
                <span className="text-slate-500">
                  {job.stats.done ?? 0}/{job.stats.total ?? 0}
                  {job.stats.failed ? ` · 失败 ${job.stats.failed}` : ''}
                </span>
                {job.stats.signature_mode && <span className="text-xs text-slate-400">
                  {job.stats.signature_mode === 'full' ? '完整 SHA-256' : '快速采样'} ·
                  {{ queued: '等待后台处理', enumerating: '后台建立清单', hashing: '读取校验', hash_recheck: '严格复核', processing: '处理素材', complete: '处理结束', cancelled: '已取消', source_changed: '源文件已变化', needs_review: '需要人工复核' }[job.stats.phase ?? 'queued'] ?? job.stats.phase}
                  {job.stats.current_file && ` · ${job.stats.current_file}`}
                  {job.stats.signature_mode === 'full' && ` · 读盘 ${formatSize(job.stats.bytes_read ?? 0)} / ${formatSize(job.stats.total_bytes ?? 0)} · 校验 ${job.stats.processed_files ?? 0}/${job.stats.total_files ?? 0}`}
                  {(job.stats.strict_extra_bytes ?? 0) > 0 && ` · 复核额外读取 ${formatSize(job.stats.strict_extra_bytes!)}`}
                  {(job.stats.hash_restarts ?? 0) > 0 && ` · 重读未完成文件 ${job.stats.hash_restarts} 次，实际读盘量可能超过计划`}
                </span>}
                {(job.stats.source_changed ?? 0) > 0 && <span className="text-xs text-red-300">源文件已变化 {job.stats.source_changed} 项，请重新扫描/检测</span>}
                {(job.stats.needs_review ?? 0) > 0 && <Link to={`/project/${id}/review`} className="text-xs text-amber-300 underline">需要人工复核 {job.stats.needs_review} 个镜头</Link>}
                {job.error && <span className="truncate text-red-400">{job.error}</span>}
                {(job.failures?.items ?? []).slice(0, 3).map((failure) => <span key={`${failure.kind}:${failure.ref}`} className="text-xs text-red-300">{failure.message}</span>)}
                {typeof job.resume_token.warning === 'string' && <span className="text-xs text-amber-300">{job.resume_token.warning}</span>}
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
