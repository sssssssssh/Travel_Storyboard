import type { JobOut } from '../lib/types'

export default function CleanupNotice({ cleanup }: { cleanup?: JobOut['stats']['cleanup'] }) {
  if (!cleanup) return <span className="text-xs text-slate-500">任务已停止；尚无本次清理统计</span>
  if (cleanup.ok) return <span className="text-xs text-slate-500">已清理：{cleanup.removed_files ?? 0} 个文件，{cleanup.removed_bytes ?? 0} 字节</span>
  const summaries = (cleanup.residual_paths ?? []).slice(0, 3).map((path) => path.replace(/\\/g, '/').split('/').slice(-3).join('/').slice(0, 100))
  return <span className="text-xs text-amber-300">有暂存残留，需安全重试（{cleanup.residual_count ?? cleanup.residual_paths?.length ?? 0} 项，{cleanup.residual_bytes ?? 0} 字节；{cleanup.error_code || 'CLEANUP_FAILED'}）{summaries.length > 0 && `：${summaries.join('、')}`}</span>
}
