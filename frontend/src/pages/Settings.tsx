import { useQuery } from '@tanstack/react-query'
import { api, endpoints } from '../lib/api'
import type { HealthOut, SystemConfigOut } from '../lib/types'
import { Badge, Card, Spinner } from '../components/ui'

export default function Settings() {
  const { data: health, isLoading: healthLoading } = useQuery({
    queryKey: ['health'],
    queryFn: () => api<HealthOut>(endpoints.health),
  })
  const { data: config } = useQuery({
    queryKey: ['config'],
    queryFn: () => api<SystemConfigOut>(endpoints.config),
  })
  const token = useAppToken()

  return (
    <div className="mx-auto max-w-3xl px-6 py-8">
      <h1 className="mb-6 text-2xl font-semibold text-white">设置</h1>

      <Card className="mb-4">
        <h2 className="mb-3 text-sm font-semibold text-slate-200">运行状态</h2>
        {healthLoading ? (
          <Spinner />
        ) : (
          <div className="space-y-1.5 text-sm">
            <div className="flex justify-between">
              <span className="text-slate-500">版本</span>
              <span className="text-slate-200">{health?.version}</span>
            </div>
            <div className="flex justify-between">
              <span className="text-slate-500">FFmpeg</span>
              <Badge tone={health?.ffmpeg_available ? 'green' : 'red'}>
                {health?.ffmpeg_available ? health?.ffmpeg : '未找到'}
              </Badge>
            </div>
            <div className="flex justify-between">
              <span className="text-slate-500">FFprobe</span>
              <span className="text-slate-400">{health?.ffprobe ?? '未找到'}</span>
            </div>
          </div>
        )}
      </Card>

      <Card className="mb-4">
        <h2 className="mb-3 text-sm font-semibold text-slate-200">服务配置</h2>
        <div className="space-y-1.5 text-sm">
          <div className="flex justify-between"><span className="text-slate-500">绑定地址</span><span className="text-slate-200">{config?.bind_host}:{config?.bind_port}</span></div>
          <div className="flex justify-between"><span className="text-slate-500">数据目录</span><span className="text-slate-200">{config?.data_dir}</span></div>
          <div className="flex justify-between"><span className="text-slate-500">并发数</span><span className="text-slate-200">{config?.max_concurrency}</span></div>
          <div className="flex justify-between"><span className="text-slate-500">缩略图宽度</span><span className="text-slate-200">{config?.thumb_width}px</span></div>
          <div className="flex justify-between">
            <span className="text-slate-500">AI 模型</span>
            <Badge tone={config?.model_configured ? 'green' : 'slate'}>
              {config?.model_configured ? config?.model_name : '未配置'}
            </Badge>
          </div>
        </div>
      </Card>

      <Card>
        <h2 className="mb-2 text-sm font-semibold text-slate-200">本地访问令牌</h2>
        <p className="mb-2 text-xs text-slate-500">
          所有 API 请求需携带此令牌。服务仅绑定 127.0.0.1，请勿对外暴露。
        </p>
        <code className="block break-all rounded bg-ink-800 px-3 py-2 text-xs text-slate-300">
          {token ?? '—'}
        </code>
      </Card>
    </div>
  )
}

function useAppToken() {
  return useQuery({ queryKey: ['token'], queryFn: () => api<{ token: string }>(endpoints.token) }).data
    ?.token
}
