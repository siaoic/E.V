# 直播：事件驱动的 AI 主播 Agent Harness

基于 TypeScript / Node.js 与 Cordis 插件内核的「一切皆插件」Agent Harness，面向直播场景的事件驱动主动对话。设计细节见 [docs/统一架构文档.md](<docs/统一架构文档.md>)。

## 当前状态

仅处于架构设计阶段。仓库中还没有可运行的应用入口（无根目录 `package.json`、无依赖清单），Agent Loop、事件总线、Persona 等业务模块尚未实现。落地顺序见统一架构文档第 9 节。

## 目录

| 路径 | 内容 |
| --- | --- |
| `docs/` | 架构文档、框架图、架构决策记录（ADR） |
| `docs/adr/` | 架构决策记录 |
| `docs/agents/` | 智能体协作约定（议题与领域文档的存放方式） |
| `CONTEXT.md` | 领域术语表 |
| `src/cordis/` | Cordis 插件框架（第三方源码） |
| `src/A_memorix/` | A_Memorix 长期记忆子系统（第三方源码） |

文档索引见 [docs/README.md](<docs/README.md>)。

## 第三方源码

- `src/cordis/`：包名 `@deepseek-ai/cordis`，MIT 许可证（见 `src/cordis/LICENSE`）。
- `src/A_memorix/`：来自 MaiBot 项目的长期记忆子系统，许可证为 AGPL v3（见 `src/A_memorix/LICENSE` 与 `LICENSE-MAIBOT-GPL.md`）。跨进程部署与分发的许可证义务尚未评估，见 [ADR-0005](<docs/adr/0005-a-memorix-license.md>)。

## 约定

- 议题：以本地 Markdown 存放于 `.scratch/<feature>/`，见 [docs/agents/issue-tracker.md](<docs/agents/issue-tracker.md>)。
- 领域与决策：单上下文仓库，术语见 `CONTEXT.md`，决策见 `docs/adr/`，见 [docs/agents/domain.md](<docs/agents/domain.md>)。
