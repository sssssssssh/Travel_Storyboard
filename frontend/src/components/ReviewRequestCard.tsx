import { useState } from 'react'
import { Link } from 'react-router-dom'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { api, endpoints, mediaUrl, reviewProxyUrl } from '../lib/api'
import type { ReviewRequestOut } from '../lib/types'
import { Badge, Button, inputClass } from './ui'

/** Backend supplies current candidates and validation: this view never maps IDs. */
export default function ReviewRequestCard({ projectId, request: r }: { projectId: string; request: ReviewRequestOut }) {
  const qc = useQueryClient()
  const [selectedId, setSelectedId] = useState('')
  const [start, setStart] = useState('')
  const [end, setEnd] = useState('')
  const [notice, setNotice] = useState('')
  const candidates = r.current_segments ?? []
  const selected = candidates.find((g) => g.id === selectedId)
  const closed = r.status === 'superseded' || r.status === 'dismissed'
  const needsAction = !!r.validation_error || r.status === 'needs_review' || r.status === 'failed'
  const old = r.segment_context
  const dispose = useMutation({
    mutationFn: (action: 'replace' | 'defer' | 'dismiss' | 'retry') => api<ReviewRequestOut>(
      endpoints.reviewDisposition(projectId, r.id), { method: 'POST', body: JSON.stringify({
        action, ...(action === 'replace' && selected ? { asset_id: selected.asset_id,
          segment_id: selected.id, start_sec: Number(start), end_sec: Number(end) } : {}),
      }) }),
    onSuccess: (result) => {
      setNotice(result.supersedes_request_id ? '已创建新请求，旧请求与预览保留为历史。请生成低清预览。' : '处置已保存。')
      qc.invalidateQueries({ queryKey: ['review-requests', projectId] })
      qc.invalidateQueries({ queryKey: ['segments', projectId] })
    },
  })
  function choose(id: string) {
    setSelectedId(id)
    const g = candidates.find((s) => s.id === id)
    setStart(g ? String(g.start_sec) : '')
    setEnd(g ? String(g.end_sec) : '')
  }
  return <article className="space-y-3 rounded border border-ink-700 p-3 text-sm">
    <div className="flex flex-wrap items-center gap-2">
      <Badge tone={r.priority === 'high' ? 'red' : 'slate'}>{r.priority}</Badge>
      <span className="text-slate-200">原片 {old?.display_name || r.asset_id} · {r.start_sec.toFixed(3)}–{r.end_sec.toFixed(3)}s</span>
      <Badge tone={needsAction ? 'yellow' : r.status === 'proxied' ? 'green' : 'slate'}>{r.status}</Badge>
    </div>
    <p className="break-all text-xs text-slate-500">请求 {r.id} · 镜头 {r.segment_id} · {r.reason}</p>
    {r.validation_error && <p className="text-amber-300">[{r.validation_error.error_code}] {r.validation_error.message}</p>}
    {old && <div className="space-y-1 text-xs text-slate-400">
      <p>旧选择区间 {old.start_sec}–{old.end_sec}s · 旧检测区间 {old.detected_start_sec}–{old.detected_end_sec}s</p>
      <p>评分 {old.user_rating} · 用途 {old.story_role || '未填写'} · 备注 {old.note || '未填写'}</p>
      {old.change_reason && <p className="text-amber-300">变化原因：{old.change_reason}</p>}
    </div>}
    {r.proxy_ref && <div>
      <a href={reviewProxyUrl(projectId, r.id)} target="_blank" rel="noreferrer" className="text-accent hover:underline">查看已有预览（历史对照）</a>
      <p className="text-xs text-slate-500">预览 0 秒对应原片 {r.start_sec}s；失效请求的预览仅供历史对照。</p>
    </div>}
    {(r.plan_shot_ids?.length ?? 0) > 0 && <p className="text-amber-300">
      此镜头仍被剪辑计划引用（{r.plan_shot_ids!.length} 处）；失效镜头会阻断新粗剪。
      <Link className="ml-1 text-accent underline" to={`/project/${projectId}/export`}>人工更新剪辑计划</Link>
    </p>}
    {r.superseded_by_request_id && <p className="break-all text-xs text-slate-400">已替代为请求 {r.superseded_by_request_id}</p>}
    {r.supersedes_request_id && <p className="break-all text-xs text-slate-400">来自历史请求 {r.supersedes_request_id}</p>}
    {needsAction && !closed && <div className="space-y-2">
      <label className="block text-xs text-slate-300">选择当前有效镜头
        <select className={inputClass} value={selected ? selectedId : ''} onChange={(e) => choose(e.target.value)}>
          <option value="">请选择镜头，不自动映射</option>
          {candidates.map((g) => <option key={g.id} value={g.id}>{g.display_name} · {g.id.slice(0, 8)} · {g.start_sec}–{g.end_sec}s</option>)}
        </select>
      </label>
      {candidates.length === 0 && <p className="text-xs text-amber-300">没有当前有效镜头，请先重新扫描/检测，再返回处理。</p>}
      {selected && <div className="space-y-2 text-xs text-slate-400">
        <p>最新检测区间 {selected.detected_start_sec}–{selected.detected_end_sec}s · 当前可用区间 {selected.start_sec}–{selected.end_sec}s</p>
        {selected.thumbnail && <img className="max-h-32 rounded" alt="新镜头代表帧" src={mediaUrl(projectId, selected.thumbnail)} />}
        <div className="flex gap-2">
          <label>新请求原片入点（秒）<input className={inputClass} value={start} onChange={(e) => setStart(e.target.value)} /></label>
          <label>新请求原片出点（秒）<input className={inputClass} value={end} onChange={(e) => setEnd(e.target.value)} /></label>
        </div>
      </div>}
      <div className="flex flex-wrap gap-2">
        <Button disabled={!selected || dispose.isPending} onClick={() => dispose.mutate('replace')}>创建新请求并替代旧请求</Button>
        <Button variant="ghost" disabled={dispose.isPending} onClick={() => dispose.mutate('defer')}>暂不处理</Button>
        <Button variant="ghost" disabled={dispose.isPending} onClick={() => dispose.mutate('dismiss')}>放弃旧请求</Button>
        {!r.validation_error && <Button variant="ghost" disabled={dispose.isPending} onClick={() => dispose.mutate('retry')}>重新校验并重试原请求</Button>}
      </div>
    </div>}
    {dispose.error && <p role="alert" className="text-red-400">{dispose.error.message}</p>}
    {notice && <p role="status" className="text-emerald-300">{notice}</p>}
  </article>
}
