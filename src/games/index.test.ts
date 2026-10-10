import { describe, expect, it } from 'vitest';
import { GAME_CATALOG, catalogLine, createGame } from './index.ts';

describe('games index', () => {
  it('目录里登记了数独', () => {
    expect(GAME_CATALOG.map((game) => game.id)).toContain('sudoku');
  });

  it('createGame 默认返回数独工具集，带玩法提示词与工具', () => {
    const { hooks, entry } = createGame('sudoku');

    expect(entry.name).toBe('数独');
    expect(hooks.tools.map((tool) => tool.function.name)).toContain('sudoku_new');
    expect(hooks.prompt).toContain('sudoku_place');
    expect(hooks.isActive()).toBe(false);
  });

  it('未知游戏 id 抛错', () => {
    expect(() => createGame(' шахматы')).toThrow();
    expect(() => createGame('chess')).toThrow(/未知的游戏/);
  });

  it('catalogLine 生成给模型看的游戏介绍', () => {
    expect(catalogLine('sudoku')).toContain('本场可以玩的游戏：数独');
    expect(catalogLine('chess')).toBe('');
  });
});
