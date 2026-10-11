import { Context } from '@deepseek-ai/cordis';
import { describe, expect, it } from 'vitest';
import { apply } from './plugin.ts';

describe('sudoku plugin', () => {
  it('提供 game 服务：工具、玩法提示词与目录介绍；dispose 时关闭观察页', async () => {
    const root = new Context();
    const fiber = await root.plugin(apply, { viewerPort: 0 });

    const game = root.get('game')!;
    expect(game.tools.map((tool) => tool.function.name)).toContain('sudoku_new');
    expect(game.prompt).toContain('sudoku_place');
    // 菜单与开局工具分开：未开局只提"有得玩"，规则书要等开局后才注入
    expect(game.menu).toContain('本场可以玩的游戏');
    expect(game.menu).toContain('不玩也没关系');
    expect(game.openers).toEqual(['sudoku_new']);
    expect(game.isActive()).toBe(false);

    await expect(fiber.dispose()).resolves.toBeUndefined();
  });
});
