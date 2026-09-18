import { useCallback, useEffect, useMemo, useState } from 'react'
import { useNavigate } from '@tanstack/react-router'
import { Volume2, RefreshCw, Save, ArrowLeft } from 'lucide-react'

import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent } from '@/components/ui/card'
import { DynamicConfigForm } from '@/components/dynamic-form'
import { useToast } from '@/hooks/use-toast'
import {
  getBotConfig,
  getBotConfigSchema,
  updateBotConfigSection,
} from '@/lib/config-api'
import type { ConfigSchema } from '@/types/config-schema'

type ConfigSectionData = Record<string, unknown>

/** 语音朗读（TTS）配置在后端 schema 中的挂载位置：chat 分区下的 tts_read 子表 */
const HOST_SECTION = 'chat'
const TTS_SECTION = 'tts_read'
/** 找不到 schema 时的兜底字段定义（与后端 chat.tts_read 保持一致） */
const FALLBACK_TTS_SCHEMA: ConfigSchema = {
  className: 'tts_read',
  classDoc: 'AI 语音朗读配置',
  fields: [],
  uiLabel: '语音朗读',
}

/**
 * 语音朗读（TTS）独立配置页：
 * 由「插件管理 → 内置功能」进入，配置数据存放在 bot_config 的 chat.tts_read，
 * 不再显示在麦麦设置的聊天分区中。
 */
export function VoiceTtsPage() {
  const { toast } = useToast()
  const navigate = useNavigate()
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)
  const [schema, setSchema] = useState<ConfigSchema | null>(null)
  const [draft, setDraft] = useState<ConfigSectionData>({})
  // 保存时需要携带完整 chat 分区数据，避免覆盖同分区其他配置
  const [hostSectionData, setHostSectionData] = useState<ConfigSectionData>({})

  const loadConfig = useCallback(async () => {
    setLoading(true)
    try {
      const [schemaResult, configResult] = await Promise.allSettled([
        getBotConfigSchema(),
        getBotConfig(),
      ])

      if (schemaResult.status === 'fulfilled') {
        const ttsSchema =
          (schemaResult.value as unknown as Record<string, unknown>).schema as ConfigSchema
        setSchema(
          ttsSchema?.nested?.[HOST_SECTION]?.nested?.[TTS_SECTION] ?? FALLBACK_TTS_SCHEMA
        )
      } else {
        setSchema(FALLBACK_TTS_SCHEMA)
      }

      if (configResult.status === 'fulfilled') {
        const config = configResult.value as Record<string, unknown>
        const chatSection = (config?.[HOST_SECTION] as ConfigSectionData | undefined) ?? {}
        setHostSectionData(chatSection)
        setDraft((chatSection[TTS_SECTION] as ConfigSectionData | undefined) ?? {})
      } else {
        toast({
          title: '加载失败',
          description:
            configResult.reason instanceof Error
              ? configResult.reason.message
              : '无法加载语音朗读配置',
          variant: 'destructive',
        })
      }
    } finally {
      setLoading(false)
    }
  }, [toast])

  useEffect(() => {
    void loadConfig()
  }, [loadConfig])

  const handleChange = useCallback((fieldPath: string, value: unknown) => {
    // fieldPath 形如 "tts_read.enable_tts_read"，剥掉分区前缀
    const [, ...restPath] = fieldPath.split('.')
    if (restPath.length === 0) {
      return
    }
    setDraft((current) => {
      const next = { ...current }
      let target: ConfigSectionData = next
      for (let i = 0; i < restPath.length - 1; i += 1) {
        const key = restPath[i]
        const child = (target[key] as ConfigSectionData | undefined) ?? {}
        target[key] = child
        target = child
      }
      target[restPath[restPath.length - 1]] = value
      return next
    })
  }, [])

  const handleSave = useCallback(async () => {
    setSaving(true)
    try {
      await updateBotConfigSection(HOST_SECTION, {
        ...hostSectionData,
        [TTS_SECTION]: draft,
      })
      toast({ title: '保存成功' })
    } catch (error) {
      toast({
        title: '保存失败',
        description: error instanceof Error ? error.message : '保存语音朗读配置失败',
        variant: 'destructive',
      })
    } finally {
      setSaving(false)
    }
  }, [draft, hostSectionData, toast])

  const formSchema: ConfigSchema = useMemo(
    () => schema ?? FALLBACK_TTS_SCHEMA,
    [schema]
  )

  return (
    <div className="mx-auto w-full max-w-4xl space-y-4 p-4 lg:p-6">
      <Button
        type="button"
        variant="ghost"
        size="sm"
        className="-ml-2 h-8 text-muted-foreground hover:text-foreground"
        onClick={() => navigate({ to: '/plugin-config' })}
      >
        <ArrowLeft className="mr-2 h-4 w-4" />
        返回插件管理
      </Button>
      <div className="flex items-start justify-between gap-3">
        <div className="space-y-1">
          <div className="flex items-center gap-2">
            <Volume2 className="text-primary h-6 w-6" />
            <h1 className="text-2xl font-bold tracking-tight">语音朗读 (TTS)</h1>
            <Badge variant="outline">内置功能</Badge>
          </div>
          <p className="text-muted-foreground text-sm">
            配置 AI 回复的语音朗读：朗读开关、输出设备与音色参考音频。修改后需保存并等待后端热加载生效。
          </p>
        </div>
      </div>

      <Card>
        <CardContent className="pt-6">
          {loading ? (
            <div className="text-muted-foreground flex items-center justify-center gap-2 py-12 text-sm">
              <RefreshCw className="h-4 w-4 animate-spin" />
              正在加载语音朗读配置…
            </div>
          ) : (
            <DynamicConfigForm
              schema={formSchema}
              values={{ [TTS_SECTION]: draft }}
              onChange={handleChange}
              basePath={TTS_SECTION}
              sectionColumns={1}
            />
          )}
        </CardContent>
      </Card>

      <div className="flex justify-end">
        <Button type="button" onClick={() => void handleSave()} disabled={saving || loading}>
          <Save className="mr-2 h-4 w-4" />
          {saving ? '保存中…' : '保存配置'}
        </Button>
      </div>
    </div>
  )
}
