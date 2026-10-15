import type { JobOut } from '../lib/types'

export default function CleanupNotice({ cleanup }: { cleanup?: JobOut['stats']['cleanup'] }) {
  if (!cleanup) return <span className="text-xs text-amber-300">任务已停止；尚无本次清理统计</span>
  if (cleanup.ok) return <span className="text-xs text-slate-500">{cleanup.remediated ? '清理已补救（原异常审计保留）' : '已清理'}：{cleanup.removed_files ?? 0} 个文件，{cleanup.removed_bytes ?? 0} 字节</span>
  const summaries = (cleanup.residual_paths ?? []).slice(0, 3).map((path) => path.replace(/\\/g, '/').split('/').slice(-3).join('/').slice(0, 100))
  const residualBytes = cleanup.residual_bytes == null ? '残留大小待核对' : `${cleanup.residual_bytes} 字节`
  return <span className="text-xs text-amber-300">有暂存残留，{cleanup.retryable ? '需安全重试' : '需核对所有权后人工处置'}（{cleanup.residual_count ?? (cleanup.residual_paths?.length || '未知')} 项，{residualBytes}；{cleanup.error_code || 'CLEANUP_FAILED'}）{summaries.length > 0 && `：${summaries.join('、')}`}</span>
}
