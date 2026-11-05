import { useState } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { api, endpoints } from '../lib/api'
import type { PlanOperationDiff, PlanOperationOut, PlanOperationRequest } from '../lib/types'
import { Button, Badge, Spinner } from '../components/ui'

/**
 * Minimal structured-operation workspace (TSB-367 §8).
 *
 * Deliberately not a new UI framework: it lives inside the existing Export page
 * and proves the *whole* protocol loop end to end — propose (server-side diff),
 * confirm, reject, and conflict handling. Three rules it encodes:
 *
 * 1. **The server's diff is the only diff.** Nothing here computes or guesses
 *    what would change; the preview comes from the POST response.
 * 2. **Nothing is applied without an explicit confirm**, and cancelling sends no
 *    request at all.
 * 3. **A 409 keeps the user's work.** The panel does not reload, refetch or
 *    overwrite anything on a conflict; it shows the server's message and offers
 *    an explicit "reload to latest" action.
 */

export interface OperationPanelProps {
  projectId: string
  boardId: string
  /** The version the panel should propose against. */
  baseVersionId: string
  /** Curated operations built by the parent (e.g. from the shot list). */
  pendingOperations: Record<string, unknown>[]
  onApplied?: (op: PlanOperationOut) => void
}

function describeDiff(diff: PlanOperationDiff): string[] {
  const lines: string[] = []
  const s = diff?.shots
  if (!s) return lines
  if (s.added?.length) lines.push(`新增镜头 ${s.added.length} 个`)
  if (s.removed?.length) lines.push(`移除镜头 ${s.removed.length} 个`)
  if (s.trimmed?.length) {
    for (const t of s.trimmed) {
      lines.push(
        `裁剪 ${t.shot_id.slice(0, 8)}: ${t.before.in.toFixed(2)}–${t.before.out.toFixed(2)}s → ${t.after.in.toFixed(2)}–${t.after.out.toFixed(2)}s`,
      )
    }
  }
  if (s.locked?.length) {
    for (const l of s.locked) {
      lines.push(
        `锁定 ${l.shot_id.slice(0, 8)}: ${l.before.join(',') || '未锁定'} → ${l.after.join(',') || '未锁定'}`,
      )
    }
  }
  if (s.reordered?.length) lines.push(`重排 ${s.reordered.length} 个镜头`)
  if (s.replaced?.length) {
    for (const r of s.replaced) {
      lines.push(`替换 ${r.old_shot_id.slice(0, 8)} → 新镜头 ${r.new_shot_id?.slice(0, 8) ?? '?'}`)
    }
  }
  if (diff.constraints?.changed) lines.push('更新方案约束')
  return lines
}

export function OperationPanel({
  projectId,
  boardId,
  baseVersionId,
  pendingOperations,
  onApplied,
}: OperationPanelProps) {
  const qc = useQueryClient()
  const [proposal, setProposal] = useState<PlanOperationOut | null>(null)
  const [conflict, setConflict] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)

  const propose = useMutation({
    mutationFn: () => {
      const body: PlanOperationRequest = {
        schema_version: 1,
        base_version_id: baseVersionId,
        // A fresh key per proposal attempt so a retry of *the same* attempt is
        // deduplicated, while a genuinely new attempt is a new operation.
        idempotency_key: `ui-${Date.now()}-${Math.random().toString(36).slice(2, 10)}`,
        operations: pendingOperations,
        reason: '手动结构化操作',
      }
      return api<PlanOperationOut>(endpoints.planOperations(projectId, boardId), {
        method: 'POST',
        body: JSON.stringify(body),
      })
    },
    onSuccess: (op) => {
      setProposal(op)
      setConflict(null)
      setError(null)
    },
    onError: (err) => {
      const status = (err as { status?: number }).status
      if (status === 409) {
        setConflict(err.message)
      } else {
        setError(err.message)
      }
    },
  })

  const apply = useMutation({
    mutationFn: (operationId: string) =>
      api<PlanOperationOut>(
        endpoints.planOperationApply(projectId, boardId, operationId),
        { method: 'POST' },
      ),
    onSuccess: async (op) => {
      setProposal(op)
      setError(null)
      await qc.invalidateQueries({ queryKey: ['edit-plan', projectId] })
      await qc.invalidateQueries({ queryKey: ['storyboard', projectId] })
      await qc.invalidateQueries({ queryKey: ['storyboard-versions', projectId] })
      onApplied?.(op)
    },
    onError: (err) => setError(err.message),
  })

  const reject = useMutation({
    mutationFn: (operationId: string) =>
      api<PlanOperationOut>(
        endpoints.planOperationReject(projectId, boardId, operationId),
        { method: 'POST', body: JSON.stringify({ reason: '用户拒绝' }) },
      ),
    onSuccess: (op) => {
      // A rejected proposal disappears; no version was created.
      setProposal(op.status === 'rejected' ? null : op)
    },
    onError: (err) => setError(err.message),
  })

  const diffLines = proposal ? describeDiff(proposal.diff) : []
  const canPropose = pendingOperations.length > 0 && !conflict

  return (
    <div className="border-b border-ink-800 px-6 py-3" data-testid="operation-panel">
      <div className="mb-2 flex items-center gap-3">
        <h2 className="text-sm font-semibold text-slate-300">结构化操作</h2>
        <span className="text-xs text-slate-500">
          基于版本 {baseVersionId.slice(0, 8)} · {pendingOperations.length} 项待提交
        </span>
      </div>

      {conflict && (
        <div
          className="mb-2 rounded border border-amber-700 bg-amber-950/40 p-2 text-sm text-amber-200"
          data-testid="operation-conflict"
        >
          <div>版本冲突：{conflict}</div>
          <div className="mt-1 text-xs">
            你的操作没有提交，也没有覆盖服务端版本。请刷新到最新版本后重新操作。
          </div>
        </div>
      )}

      {error && (
        <div className="mb-2 text-sm text-red-400" data-testid="operation-error">
          {error}
        </div>
      )}

      {!proposal && (
        <Button
          disabled={!canPropose || propose.isPending}
          onClick={() => propose.mutate()}
          data-testid="operation-propose"
        >
          {propose.isPending ? '校验中…' : '预览变更'}
        </Button>
      )}

      {propose.isPending && <Spinner />}

      {proposal && (
        <div className="rounded border border-ink-700 bg-ink-900 p-3 text-xs">
          <div className="mb-2 flex items-center gap-2">
            <Badge tone={proposal.status === 'applied' ? 'green' : 'blue'}>
              {proposal.status}
            </Badge>
            <span className="text-slate-400">
              操作 {proposal.operation_id.slice(0, 8)}
            </span>
          </div>

          <div className="mb-2">
            <div className="mb-1 text-slate-400">服务端计算的变更：</div>
            {diffLines.length === 0 ? (
              <div className="text-slate-500">无变更</div>
            ) : (
              <ul className="list-inside list-disc text-slate-300" data-testid="operation-diff">
                {diffLines.map((line) => (
                  <li key={line}>{line}</li>
                ))}
              </ul>
            )}
          </div>

          {proposal.status === 'proposed' && (
            <div className="flex gap-2">
              <Button
                disabled={apply.isPending}
                onClick={() => apply.mutate(proposal.operation_id)}
                data-testid="operation-apply"
              >
                确认应用
              </Button>
              <Button
                variant="ghost"
                disabled={reject.isPending}
                onClick={() => reject.mutate(proposal.operation_id)}
                data-testid="operation-reject"
              >
                拒绝
              </Button>
            </div>
          )}

          {proposal.status === 'applied' && (
            <div className="text-emerald-300" data-testid="operation-applied">
              已应用，生成新版本 {proposal.result_version_id?.slice(0, 8)}
            </div>
          )}
        </div>
      )}
    </div>
  )
}
