import { createElement } from 'react'
import {
  Activity,
  Box,
  Brain,
  Database,
  FileText,
  Globe,
  HardDrive,
  Hash,
  Home,
  MessageSquare,
  Puzzle,
  Settings,
  Store,
  Wifi,
} from 'lucide-react'

import { createStreamlineIcon } from '@/components/ui/streamline-menu-icon'

import type { MenuIcon, MenuSection } from './types'

const HomeIcon = createStreamlineIcon('allergens-fish-remix', Home)
const ChatManagementIcon = createStreamlineIcon('chat-two-bubbles-oval-remix', MessageSquare)
const BotConfigIcon = createStreamlineIcon('page-setting-remix', Settings)
const ModelIcon = createStreamlineIcon('module-remix', Box)
const PromptIcon = createStreamlineIcon('script-1-remix', FileText)
const ExpressionIcon = createStreamlineIcon('chat-bubble-square-write-remix', MessageSquare)
const JargonIcon = createStreamlineIcon('sign-hashtag-solid', Hash)
const BehaviorIcon = createStreamlineIcon('cyborg-solid', Brain)
const KnowledgeIcon = createStreamlineIcon('user-sticker-square-remix', Database)
const PluginConfigIcon = createStreamlineIcon('application-add-remix', Puzzle)
const AdapterManagementIcon = createStreamlineIcon('router-wifi-network-solid', Wifi)
const PluginMarketIcon = createStreamlineIcon('store-2-solid', Store)
const McpIcon = createStreamlineIcon('router-wifi-network-solid', Wifi)
const DataTransferIcon: MenuIcon = (props) => createElement(HardDrive, props)
const ReplyEffectsIcon: MenuIcon = (props) => createElement(Activity, props)
const GlobeIcon: MenuIcon = (props) => createElement(Globe, props)

// Cortico 式主导航组：终端类入口（聊天室 / 日志）不显示分组标题
export const primaryNavSection: MenuSection = {
  title: '',
  items: [
    {
      icon: ChatManagementIcon,
      label: 'workspace.chat',
      path: '/chat',
    },
    {
      icon: DataTransferIcon,
      label: 'workspace.logs',
      path: '/logs',
    },
  ],
}

export const menuSections: MenuSection[] = [
  {
    title: 'sidebar.groups.overview',
    items: [
      {
        icon: HomeIcon,
        label: 'sidebar.menu.home',
        path: '/',
        searchDescription: 'search.items.homeDesc',
      },
      { icon: ChatManagementIcon, label: 'sidebar.menu.chatManagement', path: '/chat-management' },
      {
        icon: GlobeIcon,
        label: 'sidebar.menu.worldStatus',
        path: '/worlds',
      },
    ],
  },
  {
    title: 'sidebar.groups.botConfig',
    items: [
      {
        icon: BotConfigIcon,
        label: 'sidebar.menu.botMainConfig',
        path: '/config/bot',
        searchDescription: 'search.items.botConfigDesc',
      },
      {
        icon: ModelIcon,
        label: 'sidebar.menu.modelManagement',
        path: '/config/model',
        searchDescription: 'search.items.modelDesc',
        tourId: 'sidebar-model-management',
      },
      {
        icon: AdapterManagementIcon,
        label: 'sidebar.menu.adapterManagement',
        path: '/adapter-management',
      },
    ],
  },
  {
    title: 'sidebar.groups.botResources',
    items: [
      {
        icon: ExpressionIcon,
        label: 'sidebar.menu.expressionManagement',
        path: '/resource/expression',
        searchDescription: 'search.items.expressionDesc',
      },
      {
        icon: JargonIcon,
        label: 'sidebar.menu.slangManagement',
        path: '/resource/jargon',
        searchDescription: 'search.items.jargonDesc',
      },
      {
        icon: BehaviorIcon,
        label: 'sidebar.menu.behaviorLearning',
        path: '/resource/behavior',
        searchDescription: 'search.items.behaviorLearningDesc',
        featureFlag: 'behaviorLearning',
      },
      {
        icon: KnowledgeIcon,
        label: 'sidebar.menu.knowledgeBase',
        path: '/resource/knowledge-base',
      },
    ],
  },
  {
    title: 'sidebar.groups.extensionsMonitor',
    items: [
      { icon: PluginConfigIcon, label: 'sidebar.menu.pluginConfig', path: '/plugin-config' },
      {
        icon: PluginMarketIcon,
        label: 'sidebar.menu.pluginMarket',
        path: '/plugins',
        searchDescription: 'search.items.pluginsDesc',
      },
      { icon: McpIcon, label: 'sidebar.menu.mcpSettings', path: '/mcp-settings' },
    ],
  },
  {
    title: 'sidebar.groups.advancedTools',
    items: [
      { icon: PromptIcon, label: 'sidebar.menu.promptManagement', path: '/config/prompts' },
      {
        icon: ReplyEffectsIcon,
        label: 'sidebar.menu.replyEffects',
        path: '/reply-effects',
        featureFlag: 'replyEffects',
      },
      {
        icon: DataTransferIcon,
        label: 'sidebar.menu.dataTransfer',
        path: '/data-transfer',
        searchDescription: 'search.items.dataTransferDesc',
      },
    ],
  },
]
