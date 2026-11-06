import { useState } from 'react'
import type { SetStateAction } from 'react'
import { Link, useParams } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api, downloadUrl, endpoints, fetchAllSegments, mediaUrl, roughcutUrl } from '../lib/api'
import type { BuildPlanResult, EditPlanOut, JobOut, SegmentListItem, StoryboardVersionOut } from '../lib/types'
import { formatTimeCode } from '../lib/format'
import { useJob } from '../lib/useJob'
import { Badge, Button, EmptyState, Spinner, inputClass } from '../components/ui'
import { OperationPanel } from '../components/OperationPanel'

interface DraftShot {
  /**
   * TSB-366-R1 (P0-02): the shot's stable identity, as returned by the server.
   *
   * It must survive load → edit → save, otherwise an ordinary edit (changing one
   * duration) constructs a *new* shot and loses the link to history. A shot the
   * user has just added has no id yet; the server mints one on save and the page
   * picks it up from the response.
   */
  shot_id?: string
  asset_id: string
  segment_id: string
  source_in_sec: number
  source_out_sec: number
  target_duration_sec: number
  story_role: string
  transition_hint: string
  music_cue: string
  audio_hint: string
  color_hint: string
  review_status: string
  evidence: string
  confidence: number
}

const ROLES = ['', '开场', '铺垫', '过渡', '高潮', '结尾']

export default function Export() {
  const { id = '' } = useParams()
  return <ExportEditor key={id} id={id} />
}

function ExportEditor({ id }: { id: string }) {
  const qc = useQueryClient()
  const [editor, setEditor] = useState<{
    draft: DraftShot[]
    dirty: boolean
    server: EditPlanOut | null
    //: TSB-366: the version this draft was loaded from. Sent back untouched on
    //: save so the server can reject a write based on stale data.
    baseVersionId: string | null
  }>({ draft: [], dirty: false, server: null, baseVersionId: null })
  const { draft, dirty, baseVersionId } = editor
  //: TSB-366: the storyboard head (current version, history trigger).
  const [showHistory, setShowHistory] = useState(false)
  const [conflict, setConflict] = useState<string | null>(null)
  const [restoredNotice, setRestoredNotice] = useState<string | null>(null)
  //: P1-03: set after a successful restore so the sync effect reloads the editor
  //: even though `dirty` was true (the user had unsaved work before restoring).
  const [forceReload, setForceReload] = useState(false)
  //: TSB-367-R1: the shot the user explicitly selected; operations target THIS.
  const [selectedShotId, setSelectedShotId] = useState<string | null>(null)
  //: Which structured operation the user wants to try, if any.
  const [pendingOps, setPendingOps] = useState<Record<string, unknown>[]>([])
  //: The selected shot and its index, derived from the stable id (never the array
  //: position, which is what "always the first shot" got wrong).
  const selectedIndex = selectedShotId
    ? draft.findIndex((s) => s.shot_id === selectedShotId)
    : null
  //: Operations need a stable identity; a draft row without a shot_id (a brand
  //: new, unsaved shot) is not a valid target.
  const selectedShot: (DraftShot & { shot_id: string }) | null = (() => {
    const row = selectedIndex !== null && selectedIndex >= 0 ? draft[selectedIndex] : null
    return row && typeof row.shot_id === 'string' && row.shot_id
      ? { ...row, shot_id: row.shot_id }
      : null
  })()
  function setDraft(value: SetStateAction<DraftShot[]>) {
    setEditor((current) => ({ ...current, dirty: true,
      draft: typeof value === 'function' ? value(current.draft) : value }))
  }
  const [result, setResult] = useState<BuildPlanResult | null>(null)
  const [roughcutJobId, setRoughcutJobId] = useState<string | null>(null)
  const roughcutJob = useJob(roughcutJobId)

  const segments = useQuery({
    queryKey: ['segments', id, 'export'],
    queryFn: () => fetchAllSegments(id),
  })

  const plan = useQuery({
    queryKey: ['edit-plan', id],
    queryFn: () => api<EditPlanOut>(endpoints.editPlan(id)),
  })

  // TSB-366: the storyboard head tells the page which version is current.
  const storyboard = useQuery({
    queryKey: ['storyboard', id],
    queryFn: () => api<StoryboardVersionOut>(endpoints.storyboard(id)),
  })
  const versions = useQuery({
    queryKey: ['storyboard-versions', id],
    queryFn: () => api<StoryboardVersionOut[]>(endpoints.storyboardVersions(id)),
    enabled: showHistory,
  })

  function loadServer(data: EditPlanOut) {
    // Keep every persisted edit field; array position remains authoritative.
    return [...data.shots].sort((a, b) => a.order - b.order).map((shot) => ({
      // P0-02: carry the identity through, never re-derive it from the index.
      ...(shot.shot_id ? { shot_id: shot.shot_id } : {}),
      asset_id: shot.asset_id, segment_id: shot.segment_id,
      source_in_sec: shot.source_in_sec, source_out_sec: shot.source_out_sec,
      target_duration_sec: shot.target_duration_sec, story_role: shot.story_role,
      transition_hint: shot.transition_hint, music_cue: shot.music_cue,
      audio_hint: shot.audio_hint, color_hint: shot.color_hint,
      evidence: shot.evidence, confidence: shot.confidence, review_status: shot.review_status,
    }))
  }
  if (plan.data && plan.data !== editor.server && (!dirty || forceReload)) {
    setEditor({
      draft: loadServer(plan.data),
      dirty: false,
      server: plan.data,
      baseVersionId: plan.data.version_id ?? null,
    })
    if (forceReload) setForceReload(false)
  }

  const save = useMutation({
    mutationFn: (shots: DraftShot[]) =>
      api<BuildPlanResult>(endpoints.editPlan(id), {
        method: 'POST',
        body: JSON.stringify({
          // Array position is the order; base_version_id makes the write safe.
          shots: shots.map((s, i) => ({ order: i, ...s })),
          base_version_id: baseVersionId,
        }),
      }),
    onSuccess: async (r, submitted) => {
      setResult(r)
      setConflict(null)
      if ((r.errors ?? []).length) return
      await qc.invalidateQueries({ queryKey: ['edit-plan', id] })
      await qc.invalidateQueries({ queryKey: ['storyboard', id] })
      await qc.invalidateQueries({ queryKey: ['storyboard-versions', id] })
      // The draft is only marked saved if it is what was actually submitted.
      setEditor((current) =>
        current.draft === submitted
          ? { ...current, dirty: false, baseVersionId: r.version_id ?? current.baseVersionId }
          : current,
      )
    },
    onError: (error) => {
      // §5.2: on a conflict the draft is KEPT and the user is told what to do.
      // Nothing here clears `draft`; only an explicit reload does.
      const status = (error as { status?: number }).status
      if (status === 409 || status === 428) {
        setConflict(
          status === 409
            ? '该方案已被其他地方修改（版本冲突）。你的草稿已保留，请先「收录服务端版本」再合并。'
            : '保存请求缺少版本号，请先刷新页面。',
        )
      }
    },
  })

  const restore = useMutation({
    mutationFn: (targetVersionId: string) =>
      api<StoryboardVersionOut>(endpoints.storyboardRestore(id), {
        method: 'POST',
        body: JSON.stringify({
          target_version_id: targetVersionId,
          base_version_id: storyboard.data?.current_version_id ?? null,
        }),
      }),
    onSuccess: async (v) => {
      // P1-03: the editor must end up showing the *restored* content, and may
      // only be marked clean once it actually does.
      //
      // The strategy: clear the "server" marker so the existing sync effect
      // reloads the editor from the freshly fetched plan, and only mark the draft
      // as clean once that new plan has arrived. Marking `dirty = false` here
      // unconditionally was the defect — it declared the old draft saved while
      // the user's edits silently vanished.
      setRestoredNotice(`已恢复版本 v${v.version_no}（新建版本，历史未被改写）`)
      setShowHistory(false)
      // Force the next fetched plan to become the editor content, whatever the
      // draft's dirty flag says; `dirty` is cleared only by that sync.
      setForceReload(true)
      await qc.invalidateQueries({ queryKey: ['edit-plan', id] })
      await qc.invalidateQueries({ queryKey: ['storyboard', id] })
      await qc.invalidateQueries({ queryKey: ['storyboard-versions', id] })
      // The sync effect below takes over: it sets draft + baseVersionId + dirty.
    },
    onError: (error) => {
      // P1-03 requirement 9: on failure the draft, baseVersionId and dirty flag
      // are left exactly as they were — only a message is shown.
      const status = (error as { status?: number }).status
      setRestoredNotice(
        status === 409
          ? '恢复失败：方案已被修改，你的草稿保持不变，请刷新后再试。'
          : `恢复失败：${error.message}（草稿保持不变）`,
      )
    },
  })

  /**
   * P1-03: restoring replaces the editor content, so it is only sent after the
   * user has seen what it will do to their unsaved work.
   *
   * - clean editor: no confirmation needed (nothing can be lost);
   * - dirty editor: an explicit confirm is required, and cancel sends **no**
   *   request at all.
   */
  function requestRestore(targetVersionId: string) {
    if (!dirty) {
      restore.mutate(targetVersionId)
      return
    }
    const confirmed = window.confirm(
      '恢复历史版本会替换当前编辑内容，你尚未保存的修改将会丢失。\n\n确定要恢复吗？',
    )
    if (!confirmed) return
    restore.mutate(targetVersionId)
  }

  const roughcut = useMutation({
    mutationFn: () => api<JobOut>(endpoints.createJob(id, 'roughcut'), { method: 'POST' }),
    onSuccess: (j) => setRoughcutJobId(j.id),
  })

  function addToPlan(s: SegmentListItem) {
    setDraft((d) => [
      ...d,
      {
        asset_id: s.asset_id,
        segment_id: s.id,
        source_in_sec: s.start_sec,
        source_out_sec: s.end_sec,
        target_duration_sec: Math.round((s.end_sec - s.start_sec) * 10) / 10,
        story_role: '',
        transition_hint: '',
        music_cue: '', audio_hint: '', color_hint: '', review_status: 'pending',
        evidence: 'static',
        confidence: 0.8,
      },
    ])
  }

  function move(from: number, to: number) {
    setDraft((d) => {
      const next = [...d]
      const [item] = next.splice(from, 1)
      next.splice(to, 0, item)
      return next
    })
  }

  const total = draft.reduce((acc, s) => acc + s.target_duration_sec, 0)
  const rcPath = roughcutJob.data?.resume_token?.path as string | undefined
  const rcIncomplete = Boolean(roughcutJob.data?.resume_token?.incomplete)

  return (
    <div className="flex h-full flex-col">
      <div className="flex items-center justify-between border-b border-ink-800 px-6 py-4">
        <div className="flex items-center gap-3">
          <Link to={`/project/${id}`} className="text-sm text-slate-400 hover:text-slate-200">← 返回项目</Link>
          <h1 className="text-lg font-semibold text-white">故事板 / 导出</h1>
          <span className="text-sm text-slate-500">计划 {draft.length} 个镜头 · 约 {total.toFixed(1)}s</span>
          {/* TSB-366: which version the editor is showing. */}
          {storyboard.data && (
            <span className="rounded-md border border-ink-700 px-2 py-0.5 text-xs text-slate-300" data-testid="current-version">
              当前 v{storyboard.data.version_no}
            </span>
          )}
          <Button variant="ghost" onClick={() => setShowHistory((v) => !v)} data-testid="toggle-history">
            版本历史
          </Button>
        </div>
        <div className="flex items-center gap-2">
          <a className="rounded-md border border-ink-700 px-3 py-1.5 text-sm text-slate-300 hover:bg-ink-800" href={downloadUrl(endpoints.exportCsv(id))}>CSV</a>
          <a className="rounded-md border border-ink-700 px-3 py-1.5 text-sm text-slate-300 hover:bg-ink-800" href={downloadUrl(endpoints.exportJson(id))}>JSON</a>
          <a className="rounded-md border border-ink-700 px-3 py-1.5 text-sm text-slate-300 hover:bg-ink-800" href={downloadUrl(endpoints.exportOtio(id))}>OTIO</a>
          <Button variant="ghost" disabled={!plan.data || save.isPending} onClick={() => {
            if (plan.data) {
              setEditor({
                draft: loadServer(plan.data),
                dirty: false,
                server: plan.data,
                baseVersionId: plan.data.version_id ?? null,
              })
            }
          }}>重新加载服务端版本</Button>
          <Button disabled={plan.isLoading || save.isPending} onClick={() => save.mutate(draft)}>
            保存剪辑计划
          </Button>
          {/* TSB-261: a `cancelling` job still occupies the project, so the
              button stays disabled until the worker has really stopped. */}
          <Button variant="ghost" disabled={roughcut.isPending || ['running', 'pending', 'cancelling'].includes(roughcutJob.data?.status ?? '')} onClick={() => roughcut.mutate()}>
            生成粗剪
          </Button>
        </div>
      </div>

      {roughcutJob.data && (
        <div className="border-b border-ink-800 px-6 py-2 text-sm">
          <Badge
            tone={
              roughcutJob.data.status === 'succeeded'
                ? 'green'
                : roughcutJob.data.status === 'partial_failed'
                  ? 'yellow'
                  : roughcutJob.data.status === 'failed'
                    ? 'red'
                    : 'blue'
            }
          >
            {roughcutJob.data.status}
          </Badge>
          {' '}粗剪 {roughcutJob.data.stats.done ?? 0}/{roughcutJob.data.stats.total ?? 0}
          {rcIncomplete && <span className="ml-2 text-amber-400">⚠ 缺镜头，仅为不完整预览</span>}
          {roughcutJob.data.error && <span className="ml-2 text-red-400">{roughcutJob.data.error}</span>}
          {rcPath && (
            <a
              className="ml-2 text-accent hover:underline"
              href={roughcutUrl(id)}
              target="_blank"
              rel="noreferrer"
            >
              预览粗剪
            </a>
          )}
        </div>
      )}

      {save.error && (
        <div className="px-6 py-2 text-sm text-red-400" data-testid="save-error">
          保存失败，草稿已保留：{save.error.message}
        </div>
      )}
      {conflict && (
        <div className="flex items-center gap-3 border-b border-amber-700 bg-amber-950/40 px-6 py-2 text-sm text-amber-200" data-testid="version-conflict">
          <span>{conflict}</span>
          <Button
            variant="ghost"
            onClick={() => {
              // The only action that discards the local draft, and it is explicit.
              if (plan.data) {
                setEditor({ draft: loadServer(plan.data), dirty: false, server: plan.data, baseVersionId: plan.data.version_id ?? null })
                setConflict(null)
              }
            }}
          >
            收录服务端版本（放弃草稿）
          </Button>
        </div>
      )}
      {restoredNotice && (
        <div className="border-b border-ink-800 px-6 py-2 text-sm text-emerald-300" data-testid="restore-notice">
          {restoredNotice}
        </div>
      )}
      {dirty && (
        <div className="px-6 py-2 text-xs text-amber-300" data-testid="draft-dirty">
          草稿有未保存修改（尚未写入任何版本）
        </div>
      )}
      {result && (
        <div className="border-b border-ink-800 px-6 py-2 text-sm">
          {(result.errors ?? []).length === 0 ? (
            <span className="text-emerald-300">已保存 {result.created} 个镜头，总时长 {result.total_duration_sec}s</span>
          ) : (
            <span className="text-red-400">校验失败（未保存）：{(result.errors ?? []).map((e) => e.message).join('；')}</span>
          )}
        </div>
      )}

      {showHistory && (
        <div className="max-h-64 overflow-auto border-b border-ink-800 px-6 py-3" data-testid="version-history">
          <h2 className="mb-2 text-sm font-semibold text-slate-300">版本历史</h2>
          {versions.isLoading ? (
            <Spinner />
          ) : (
            <ul className="space-y-1 text-xs">
              {(versions.data ?? []).map((v) => {
                const isCurrent = v.version_id === storyboard.data?.current_version_id
                return (
                  <li key={v.version_id} className="flex items-center gap-3 rounded border border-ink-700 px-2 py-1">
                    <span className="font-mono text-slate-200">v{v.version_no}</span>
                    <span className="text-slate-400">{v.reason}</span>
                    <span className="text-slate-500">{v.actor}</span>
                    <span className="text-slate-500">{v.created_at?.replace('T', ' ').slice(0, 19)}</span>
                    <span className="text-slate-400">{v.shots.length} 镜头</span>
                    {v.validation_summary?.state !== 'ok' && (
                      <Badge tone="yellow">{v.validation_summary?.state}</Badge>
                    )}
                    {isCurrent ? (
                      <Badge tone="green">当前</Badge>
                    ) : (
                      <Button
                        variant="ghost"
                        disabled={restore.isPending}
                        onClick={() => requestRestore(v.version_id)}
                      >
                        恢复此版本
                      </Button>
                    )}
                  </li>
                )
              })}
            </ul>
          )}
        </div>
      )}

      {/* TSB-367-R1 R1-05: minimal structured-operation workspace.
          The target is whatever shot the user selected - never an implicit
          "first shot" - and every action builds an explicit operation for it. */}
      {storyboard.data && (
        <div className="border-b border-ink-800">
          <div className="flex flex-wrap items-center gap-2 px-6 py-2 text-xs">
            <span className="text-slate-400" data-testid="selected-shot">
              {selectedShot
                ? `已选镜头 ${selectedShot.shot_id.slice(0, 8)}`
                : '先在下方剪辑列表点选一个镜头'}
            </span>

            <Button
              variant="ghost"
              disabled={!selectedShot || selectedIndex === null}
              onClick={() => {
                if (!selectedShot || selectedIndex === null) return
                const target = selectedIndex > 0 ? draft[selectedIndex - 1] : draft[selectedIndex + 1]
                if (!target) return
                setPendingOps([{
                  type: 'move_shot',
                  shot_id: selectedShot.shot_id,
                  [selectedIndex > 0 ? 'after_shot_id' : 'before_shot_id']: target.shot_id,
                }])
              }}
            >
              上移
            </Button>

            <Button
              variant="ghost"
              disabled={!selectedShot || selectedIndex === null}
              onClick={() => {
                if (!selectedShot || selectedIndex === null) return
                const target = selectedIndex < draft.length - 1
                  ? draft[selectedIndex + 1]
                  : draft[selectedIndex - 1]
                if (!target) return
                setPendingOps([{
                  type: 'move_shot',
                  shot_id: selectedShot.shot_id,
                  [selectedIndex < draft.length - 1 ? 'after_shot_id' : 'before_shot_id']: target.shot_id,
                }])
              }}
            >
              下移
            </Button>

            <Button
              variant="ghost"
              disabled={!selectedShot}
              onClick={() =>
                selectedShot &&
                setPendingOps([{
                  type: 'trim_shot',
                  shot_id: selectedShot.shot_id,
                  source_in_sec: selectedShot.source_in_sec,
                  source_out_sec: Math.max(
                    selectedShot.source_in_sec + 0.5,
                    selectedShot.source_out_sec - 0.5,
                  ),
                }])
              }
            >
              裁剪（缩短 0.5s）
            </Button>

            <Button
              variant="ghost"
              disabled={!selectedShot}
              onClick={() =>
                selectedShot &&
                setPendingOps([{ type: 'remove_shot', shot_id: selectedShot.shot_id }])
              }
            >
              移除
            </Button>

            <Button
              variant="ghost"
              disabled={!selectedShot}
              onClick={() =>
                selectedShot &&
                setPendingOps([{
                  type: 'lock_shot',
                  shot_id: selectedShot.shot_id,
                  locked: true,
                  dimensions: ['range'],
                }])
              }
            >
              锁定裁剪区间
            </Button>

            <Button
              variant="ghost"
              disabled={!selectedShot}
              onClick={() =>
                selectedShot &&
                setPendingOps([{
                  type: 'lock_shot',
                  shot_id: selectedShot.shot_id,
                  locked: true,
                  dimensions: ['all'],
                }])
              }
            >
              全部锁定
            </Button>

            <Button
              variant="ghost"
              disabled={!selectedShot}
              onClick={() =>
                selectedShot &&
                setPendingOps([{
                  type: 'lock_shot',
                  shot_id: selectedShot.shot_id,
                  locked: false,
                  dimensions: ['range'],
                }])
              }
            >
              解除裁剪锁定
            </Button>

            <Button
              variant="ghost"
              disabled={!selectedShot}
              onClick={() =>
                selectedShot &&
                setPendingOps([{
                  type: 'lock_shot',
                  shot_id: selectedShot.shot_id,
                  locked: false,
                  dimensions: ['all'],
                }])
              }
            >
              全部解锁
            </Button>

            <Button variant="ghost" onClick={() => setPendingOps([])}>
              清空
            </Button>
          </div>

          <OperationPanel
            projectId={id}
            boardId={storyboard.data.board_id}
            baseVersionId={storyboard.data.current_version_id ?? storyboard.data.version_id}
            pendingOperations={pendingOps}
            targetLabel={
              selectedShot ? `镜头 ${selectedShot.shot_id.slice(0, 8)}` : undefined
            }
          />
        </div>
      )}

      <div className="flex flex-1 overflow-hidden">
        {/* Segment picker */}
        <div className="w-1/2 overflow-auto border-r border-ink-800 px-4 py-4">
          <h2 className="mb-3 text-sm font-semibold text-slate-300">候选镜头</h2>
          {segments.isLoading ? (
            <Spinner />
          ) : !segments.data || segments.data.length === 0 ? (
            <EmptyState title="暂无片段" hint="先运行「检测片段」" />
          ) : (
            <ul className="space-y-2">
              {segments.data.map((s) => (
                <li key={s.id} className="flex items-center gap-3 rounded-md border border-ink-700 bg-ink-900 p-2">
                  <div className="h-10 w-16 shrink-0 overflow-hidden rounded bg-ink-800">
                    {s.thumbnail ? <img src={mediaUrl(id, s.thumbnail)} alt="" className="h-full w-full object-cover" /> : null}
                  </div>
                  <div className="min-w-0 flex-1 text-xs text-slate-400">
                    <div className="truncate text-slate-300">{s.asset_display_name}</div>
                    <div>{formatTimeCode(s.start_sec)} – {formatTimeCode(s.end_sec)}</div>
                  </div>
                  <Button variant="ghost" disabled={s.status === 'needs_review'} onClick={() => addToPlan(s)}>{s.status === 'needs_review' ? '需要复核' : '+'}</Button>
                </li>
              ))}
            </ul>
          )}
        </div>

        {/* Draft plan */}
        <div className="w-1/2 overflow-auto px-4 py-4">
          <h2 className="mb-3 text-sm font-semibold text-slate-300">剪辑计划（按顺序）</h2>
          {draft.length === 0 ? (
            <EmptyState title="还没有镜头" hint="从左侧点击 + 加入计划" />
          ) : (
            <ul className="space-y-2">
              {draft.map((s, i) => (
                <li
                  key={s.shot_id ?? i}
                  data-testid={`draft-shot-${i}`}
                  onClick={() => setSelectedShotId(s.shot_id ?? null)}
                  className={
                    'cursor-pointer rounded-md border p-2 ' +
                    (s.shot_id && s.shot_id === selectedShotId
                      ? 'border-emerald-600 bg-ink-800'
                      : 'border-ink-700 bg-ink-900')
                  }
                >
                  <div className="mb-1 flex items-center justify-between text-xs text-slate-400">
                    <span className="font-medium text-slate-300">#{i + 1}</span>
                    <span>{s.source_in_sec.toFixed(1)}s – {s.source_out_sec.toFixed(1)}s</span>
                    <div className="flex gap-1">
                      <button className="px-1 text-slate-400 hover:text-white" onClick={() => move(i, i - 1)} disabled={i === 0}>↑</button>
                      <button className="px-1 text-slate-400 hover:text-white" onClick={() => move(i, i + 1)} disabled={i === draft.length - 1}>↓</button>
                      <button className="px-1 text-red-400 hover:text-red-300" onClick={() => setDraft((d) => d.filter((_, x) => x !== i))}>✕</button>
                    </div>
                  </div>
                  <div className="mb-2 flex gap-2 text-xs text-slate-400">
                    {(['source_in_sec', 'source_out_sec', 'target_duration_sec'] as const).map((field) => (
                      <label key={field}>{field === 'source_in_sec' ? '入点秒' : field === 'source_out_sec' ? '出点秒' : '目标时长秒'}
                        <input className={inputClass} type="number" min="0" step="0.01" value={s[field]}
                          onChange={(e) => setDraft((d) => d.map((x, xi) => xi === i ? { ...x, [field]: Number(e.target.value) } : x))} />
                      </label>
                    ))}
                  </div>
                  <div className="flex items-center gap-2">
                    <select
                      className={inputClass + ' max-w-[120px]'}
                      value={s.story_role}
                      onChange={(e) => setDraft((d) => d.map((x, xi) => (xi === i ? { ...x, story_role: e.target.value } : x)))}
                    >
                      {ROLES.map((r) => <option key={r} value={r}>{r || '用途…'}</option>)}
                    </select>
                    <input
                      className={inputClass}
                      placeholder="转场提示（可选）"
                      value={s.transition_hint}
                      onChange={(e) => setDraft((d) => d.map((x, xi) => (xi === i ? { ...x, transition_hint: e.target.value } : x)))}
                    />
                  </div>
                  <div className="mt-2 flex gap-2">
                    {([['music_cue', '配乐提示'], ['audio_hint', '音频提示'], ['color_hint', '调色提示']] as const).map(([field, label]) => (
                      <input key={field} className={inputClass} placeholder={label} value={s[field]}
                        onChange={(e) => setDraft((d) => d.map((x, xi) => xi === i ? { ...x, [field]: e.target.value } : x))} />
                    ))}
                  </div>
                </li>
              ))}
            </ul>
          )}
          {plan.data && plan.data.shots.length > 0 && (
            <div className="mt-4">
              <h3 className="mb-2 text-xs font-medium uppercase text-slate-500">已保存的计划（{plan.data.shots.length} 镜头）</h3>
              <ol className="space-y-1 text-xs text-slate-400">
                {plan.data.shots.map((s) => (
                  <li key={s.order}>
                    <Badge tone="blue">#{s.order}</Badge> {s.source_name} {s.source_in_sec}s–{s.source_out_sec}s · {s.story_role || '未设用途'}
                  </li>
                ))}
              </ol>
            </div>
          )}
        </div>
      </div>
    </div>
  )
}
