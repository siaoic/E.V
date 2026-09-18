import { Globe, RefreshCw } from 'lucide-react'
import { useQuery } from '@tanstack/react-query'

import { Badge } from '@/components/ui/badge'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import {
  getVtuberStatus,
  getWorldsEvents,
  getWorldsOverview,
  getWorldsState,
  getWorldsTools,
} from '@/lib/worlds-api'

const REFRESH_INTERVAL_MS = 5000

function StatusBadge({ ok, okText, badText }: { ok: boolean; okText: string; badText: string }) {
  return <Badge variant={ok ? 'default' : 'secondary'}>{ok ? okText : badText}</Badge>
}

export function WorldsPage() {
  // 世界状态是直播现场数据：5 秒轮询保持新鲜
  const { data: overview, isLoading: overviewLoading } = useQuery({
    queryKey: ['worlds-overview'],
    queryFn: () => getWorldsOverview(),
    refetchInterval: REFRESH_INTERVAL_MS,
  })
  const { data: stateData } = useQuery({
    queryKey: ['worlds-state'],
    queryFn: () => getWorldsState(),
    refetchInterval: REFRESH_INTERVAL_MS,
  })
  const { data: eventsData } = useQuery({
    queryKey: ['worlds-events'],
    queryFn: () => getWorldsEvents(),
    refetchInterval: REFRESH_INTERVAL_MS,
  })
  const { data: toolsData } = useQuery({
    queryKey: ['worlds-tools'],
    queryFn: () => getWorldsTools(),
    refetchInterval: 30000,
  })
  const { data: vtuber } = useQuery({
    queryKey: ['worlds-vtuber'],
    queryFn: () => getVtuberStatus(),
    refetchInterval: REFRESH_INTERVAL_MS,
  })

  const worlds = overview?.worlds ?? []

  return (
    <div className="mx-auto w-full max-w-4xl space-y-6 p-6">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="flex items-center gap-2 text-2xl font-bold">
            <Globe className="h-6 w-6" />
            世界状态
          </h1>
          <p className="mt-1 text-sm text-muted-foreground">
            事件驱动多 World 框架的实时只读视图：已接入世界、状态注入文本、最近事件流、世界工具调用与 VTuber
            口型桥连接状态。数据 5 秒自动刷新。
          </p>
        </div>
        <Badge variant={overview?.enabled ? 'default' : 'secondary'}>
          {overview?.enabled ? '世界框架已开启' : '世界框架未开启（worlds.enabled = false）'}
        </Badge>
      </div>

      <Card>
        <CardHeader>
          <CardTitle>已接入世界</CardTitle>
          <CardDescription>
            世界由插件注册（如 world-bilibili / world-minecraft / world-pvz / world-asr）；
            注册表里有描述符即视为在线。绑定聊天流：{overview?.bound_session_id || '未绑定'}
          </CardDescription>
        </CardHeader>
        <CardContent>
          {overviewLoading ? (
            <p className="text-sm text-muted-foreground">加载中...</p>
          ) : worlds.length === 0 ? (
            <p className="text-sm text-muted-foreground">当前没有接入任何世界。</p>
          ) : (
            <div className="overflow-hidden rounded-md border">
              <table className="w-full text-sm">
                <thead className="bg-muted text-left text-xs uppercase tracking-wide text-muted-foreground">
                  <tr>
                    <th className="px-3 py-2">世界</th>
                    <th className="px-3 py-2">world_name</th>
                    <th className="px-3 py-2">来源插件</th>
                    <th className="px-3 py-2">变更轮询</th>
                    <th className="px-3 py-2">延迟视图</th>
                  </tr>
                </thead>
                <tbody>
                  {worlds.map((world) => (
                    <tr key={world.name} className="border-t">
                      <td className="px-3 py-2 font-medium">{world.display_name}</td>
                      <td className="px-3 py-2 font-mono text-xs">{world.name}</td>
                      <td className="px-3 py-2 font-mono text-xs">{world.plugin_id}</td>
                      <td className="px-3 py-2">
                        {world.polls_changes ? `每 ${overview?.change_poll_seconds ?? 2}s` : '事件推送'}
                      </td>
                      <td className="px-3 py-2">
                        {world.delayed_active ? (
                          <StatusBadge ok okText={`延迟 ${overview?.delayed_seconds ?? 8}s`} badText="" />
                        ) : world.supports_delayed ? (
                          <Badge variant="outline">已声明未启用</Badge>
                        ) : (
                          <span className="text-muted-foreground">—</span>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>当前状态注入文本</CardTitle>
          <CardDescription>
            Planner 请求时按需注入的快照（无变化不注入，最长 {overview?.stale_after_seconds ?? 45}s
            补一次）；「观众此刻看到的画面」是延迟视图，只有防穿帮来源才有。
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-3">
          {(stateData?.worlds ?? []).length === 0 ? (
            <p className="text-sm text-muted-foreground">还没有任何世界状态被注入过。</p>
          ) : (
            stateData?.worlds.map((world) => (
              <div key={world.name} className="rounded-md border p-3">
                <div className="mb-1 font-medium">{world.display_name}</div>
                {world.realtime ? (
                  <div className="whitespace-pre-wrap text-sm">
                    <span className="mr-1 rounded bg-muted px-1.5 py-0.5 text-xs">实时</span>
                    {world.realtime}
                  </div>
                ) : null}
                {world.delayed ? (
                  <div className="mt-1 whitespace-pre-wrap text-sm">
                    <span className="mr-1 rounded bg-muted px-1.5 py-0.5 text-xs">观众此刻看到的画面</span>
                    {world.delayed}
                  </div>
                ) : null}
                {!world.realtime && !world.delayed ? (
                  <p className="text-sm text-muted-foreground">暂无快照</p>
                ) : null}
              </div>
            ))
          )}
        </CardContent>
      </Card>

      <div className="grid gap-6 lg:grid-cols-2">
        <Card>
          <CardHeader>
            <CardTitle className="flex items-center justify-between">
              最近世界事件
              <span className="text-xs font-normal text-muted-foreground">
                待投递 {eventsData?.pending_count ?? 0} 条
              </span>
            </CardTitle>
            <CardDescription>环形缓冲最近 200 条，含被节流丢弃的事件。</CardDescription>
          </CardHeader>
          <CardContent>
            {(eventsData?.events ?? []).length === 0 ? (
              <p className="text-sm text-muted-foreground">还没有事件。</p>
            ) : (
              <ul className="max-h-72 space-y-1.5 overflow-y-auto text-sm">
                {[...(eventsData?.events ?? [])].reverse().map((event, index) => (
                  <li key={`${event.at}-${index}`} className="rounded border px-2 py-1.5">
                    <div className="flex items-center gap-2 text-xs text-muted-foreground">
                      <Badge variant="outline">{event.trigger}</Badge>
                      <span className="font-mono">{event.world}</span>
                      <span>{event.at}</span>
                      {!event.delivered && <Badge variant="secondary">{event.reason || '未投递'}</Badge>}
                    </div>
                    <div className="mt-1">{event.text}</div>
                  </li>
                ))}
              </ul>
            )}
          </CardContent>
        </Card>

        <div className="space-y-6">
          <Card>
            <CardHeader>
              <CardTitle>VTuber 口型桥</CardTitle>
              <CardDescription>VTube Studio 注入方状态（W4）。</CardDescription>
            </CardHeader>
            <CardContent>
              {vtuber?.enabled ? (
                <dl className="space-y-1.5 text-sm">
                  <div className="flex items-center justify-between">
                    <dt className="text-muted-foreground">连接状态</dt>
                    <dd>
                      <StatusBadge ok={vtuber.connected} okText="已连接" badText="未连接" />
                    </dd>
                  </div>
                  <div className="flex items-center justify-between">
                    <dt className="text-muted-foreground">注入参数</dt>
                    <dd className="font-mono text-xs">
                      {vtuber.mouth_param} = {vtuber.mouth_value.toFixed(2)}
                    </dd>
                  </div>
                  {vtuber.last_error ? (
                    <div className="rounded bg-destructive/10 p-2 text-xs text-destructive">
                      {vtuber.last_error}
                    </div>
                  ) : null}
                </dl>
              ) : (
                <p className="text-sm text-muted-foreground">
                  未启用（config/bilibili_live.toml 的 [vtuber].lipsync_enabled = false）。
                </p>
              )}
            </CardContent>
          </Card>

          <Card>
            <CardHeader>
              <CardTitle>近期世界工具调用</CardTitle>
              <CardDescription>world_observe / minecraft_* / pvz_* / vtuber_* 等最近 50 条。</CardDescription>
            </CardHeader>
            <CardContent>
              {toolsData?.error ? (
                <p className="text-xs text-destructive">{toolsData.error}</p>
              ) : (toolsData?.tools ?? []).length === 0 ? (
                <p className="text-sm text-muted-foreground">还没有世界工具调用记录。</p>
              ) : (
                <ul className="max-h-40 space-y-1 overflow-y-auto text-sm">
                  {toolsData?.tools.map((tool, index) => (
                    <li key={index} className="flex items-center justify-between gap-2">
                      <span className="font-mono text-xs">{tool.tool_name}</span>
                      <span className="text-xs text-muted-foreground">{tool.timestamp}</span>
                    </li>
                  ))}
                </ul>
              )}
            </CardContent>
          </Card>
        </div>
      </div>

      <div className="flex items-center gap-2 text-xs text-muted-foreground">
        <RefreshCw className="h-3 w-3" />
        数据为只读视图；世界的启停请在「插件管理」中操作，框架开关在「麦麦设置 → 世界」。
      </div>
    </div>
  )
}

export default WorldsPage
