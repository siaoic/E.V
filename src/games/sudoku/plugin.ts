import type { Context } from '@deepseek-ai/cordis';
import { catalogLine, createGame } from '../index.ts';
import { SUDOKU_PAGE } from './page.ts';
import type { SudokuToolkit } from './tools.ts';
import { startViewerServer } from '../../viewer/server.ts';

export type SudokuPluginConfig = {
  /** 观察页端口；0 = 随机端口（测试用） */
  viewerPort?: number | string;
};

declare module '@deepseek-ai/cordis' {
  interface Context {
    game: SudokuToolkit;
  }
}

export const apply = Object.assign(
  async function sudoku(ctx: Context, config: SudokuPluginConfig) {
    const port = config.viewerPort === undefined ? 8787 : Number(config.viewerPort);
    const viewer = await startViewerServer({
      page: SUDOKU_PAGE,
      port,
      onConnect: (client) => client.send('board', game.snapshot()),
    });
    console.log(`观察页面: ${viewer.url}`);
    const { hooks: game } = createGame<SudokuToolkit>(process.env.GAME ?? 'sudoku', {
      onChange: () => viewer.broadcaster.send('board', game.snapshot()),
    });
    game.prompt += catalogLine('sudoku');
    ctx.provide('game', game);
    ctx.effect(() => () => viewer.close());
  },
  { inject: [] as string[] },
);

export const config = {
  viewerPort: '${VIEWER_PORT:-8787}',
};
