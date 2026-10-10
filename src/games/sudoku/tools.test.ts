import { describe, expect, it } from 'vitest';
import { createPuzzle, createRandom } from './engine.ts';
import { createSudokuToolkit } from './tools.ts';

const toolkit = (seed = 7) => createSudokuToolkit({ nextSeed: () => seed });

describe('数独工具', () => {
  it('prompt 提供数独玩法说明', () => {
    expect(toolkit().prompt).toContain('sudoku_place');
  });

  it('没有开局时提示开局，且不能查看或落子', () => {
    const game = toolkit();

    expect(game.isActive()).toBe(false);
    expect(game.describe()).toContain('还没有开局');
    expect(game.run('sudoku_look', '{}')).toContain('还没有开局');
    expect(game.run('sudoku_place', '{"row":1,"col":1,"value":1}')).toContain('还没有开局');
  });

  it('sudoku_new 开局后可以查看盘面', () => {
    const game = toolkit();

    expect(game.run('sudoku_new', '{}')).toContain('新的一局开始了');
    expect(game.isActive()).toBe(true);
    expect(game.describe()).toContain('数独进行中');
    expect(game.run('sudoku_look', '{"candidates":true}')).toContain('可能是');
  });

  it('局中再次开局会被拒绝', () => {
    const game = toolkit();
    game.run('sudoku_new', '{}');

    expect(game.run('sudoku_new', '{}')).toContain('还没有完成');
  });

  it('落子与擦除的参数从工具参数里读取', () => {
    const game = toolkit();
    game.run('sudoku_new', '{}');

    expect(game.run('sudoku_place', '{"row":1,"col":1,"value":9}')).toMatch(/已填入|题目给定/);
    expect(game.run('sudoku_erase', '{"row":1,"col":1}')).toMatch(/已擦掉|不能擦|本来就是空的/);
  });

  it('参数缺失或非法时返回提示而不是抛错', () => {
    const game = toolkit();
    game.run('sudoku_new', '{}');

    expect(game.run('sudoku_place', '{"row":"a"}')).toContain('必须是 1 到 9');
    expect(game.run('sudoku_place', 'not json')).toContain('必须是 1 到 9');
  });

  it('sudoku_check 报告冲突格子，没有冲突时说明没有发现', () => {
    const game = toolkit();
    game.run('sudoku_new', '{}');

    expect(game.run('sudoku_check', '{}')).toBe('没有发现规则冲突');
  });

  it('未知工具名返回未知工具', () => {
    const game = toolkit();
    game.run('sudoku_new', '{}');

    expect(game.run('sudoku_dance', '{}')).toBe('（未知工具）');
  });

  it('snapshot 报告状态、盘面、给定格与冲突', () => {
    const game = toolkit();

    expect(game.snapshot()).toEqual({ status: 'none', filled: 0, board: [], givens: [], conflicts: [] });

    game.run('sudoku_new', '{}');
    const playing = game.snapshot();
    expect(playing.status).toBe('playing');
    expect(playing.filled).toBe(36);
    expect(playing.board).toHaveLength(9);
    expect(playing.board.every((row) => row.length === 9)).toBe(true);
    expect(playing.givens.flat().filter(Boolean)).toHaveLength(36);
  });

  it('onChange 在每次数独工具调用后触发', () => {
    let changes = 0;
    const game = createSudokuToolkit({ nextSeed: () => 7, onChange: () => (changes += 1) });

    game.run('sudoku_new', '{}');
    game.run('sudoku_look', '{}');
    game.run('sudoku_place', '{"row":1,"col":1,"value":5}');

    expect(changes).toBe(3);
  });

  it('填入正确答案完成一局后不再算进行中，可以开新局', () => {
    const { puzzle, solution } = createPuzzle(createRandom(7));
    const game = toolkit(7);
    game.run('sudoku_new', '{}');

    puzzle.forEach((line, row) =>
      line.forEach((value, col) => {
        if (value === 0) game.run('sudoku_place', JSON.stringify({ row: row + 1, col: col + 1, value: solution[row][col] }));
      }),
    );

    expect(game.isActive()).toBe(false);
    expect(game.describe()).toContain('上一局已完成');
    expect(game.run('sudoku_new', '{}')).toContain('新的一局开始了');
  });
});
