import type { Context } from '@deepseek-ai/cordis';
import path from 'node:path';
import type { LiveEvent } from '../../plugins/90-danmaku/plugin.ts';
import { createDream } from './dream.ts';
import { createEmbedder } from './embedding.ts';
import { createMemoryToolkit } from './hooks.ts';
import { openNoteWorkspace } from './notes.ts';
import { openPersonaStore } from './persona.ts';
import { MemoryWorkspace } from './workspace.ts';

export type MemoryPluginConfig = {
  file?: string;
};

declare module '@deepseek-ai/cordis' {
  interface Context {
    memory: ReturnType<typeof createMemoryToolkit>;
  }
}

export const apply = Object.assign(
  async function memory(ctx: Context, config: MemoryPluginConfig) {
    const memoryFile = config.file ?? 'workspace/memory.jsonl';
    const workspaceDir = path.dirname(memoryFile);
    const workspace = await MemoryWorkspace.open(memoryFile);
    const persona = await openPersonaStore(workspaceDir);
    const notes = await openNoteWorkspace(workspaceDir);
    workspace.setEmbedder(
      createEmbedder({
        url: process.env.EMBEDDING_URL ?? 'http://127.0.0.1:8761',
        model: process.env.EMBEDDING_MODEL ?? 'qwen3-vl-embedding',
      }),
    );
    const llm = ctx.llm;
    const dream = createDream({ llm, notes, persona, logger: (message) => console.log(`[dream] ${message}`) });
    // 零定时器：dream 由心跳计数驱动（约每 30 个心跳 ≈ 半小时一轮），退出前跑场次交接的最后一轮
    ctx.on('world/live-event', (event: LiveEvent) => {
      if (event.type === 'heartbeat') dream.tick();
    });
    ctx.provide(
      'memory',
      createMemoryToolkit(workspace, {
        describeImage: llm.describeImage?.bind(llm),
        persona,
        notes,
        dreamSummary: dream.summary,
      }),
    );
    ctx.effect(() => () => {
      void (async () => {
        await dream.flush().catch(() => {});
        await workspace.flush();
      })();
    });
  },
  { inject: ['llm'] },
);

export const config = {
  file: '${MEMORY_FILE:-workspace/memory.jsonl}',
};
