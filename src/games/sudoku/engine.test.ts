import { describe, expect, it } from 'vitest';
import { countSolutions, createPuzzle, createRandom, SudokuGame } from './engine.ts';

const newGame = (seed = 7) => {
  const { puzzle, solution } = createPuzzle(createRandom(seed));
  return { game: new SudokuGame(puzzle, solution), puzzle, solution };
};

const emptyCells = (puzzle: number[][]) => {
  const cells: { row: number; col: number }[] = [];
  puzzle.forEach((line, row) => line.forEach((value, col) => value === 0 && cells.push({ row, col })));
  return cells;
};

describe('数独引擎', () => {
  it('同一个种子生成同一局', () => {
    expect(createPuzzle(createRandom(42)).puzzle).toEqual(createPuzzle(createRandom(42)).puzzle);
  });

  it('生成的题目恰好 36 个给定数，且只有唯一解', () => {
    const { puzzle, solution } = createPuzzle(createRandom(7));

    expect(puzzle.flat().filter((value) => value !== 0)).toHaveLength(36);
    expect(countSolutions(puzzle.map((row) => [...row]), 2)).toBe(1);
    puzzle.forEach((line, row) =>
      line.forEach((value, col) => value !== 0 && expect(value).toBe(solution[row][col])),
    );
  });

  it('不能修改题目给定的格', () => {
    const { game, puzzle } = newGame();
    const given = puzzle.flatMap((line, row) => line.map((value, col) => ({ value, row, col }))).find((cell) => cell.value !== 0)!;

    const result = game.place(given.row + 1, given.col + 1, 5);

    expect(result.ok).toBe(false);
    expect(result.message).toContain('题目给定');
  });

  it('可以在空格落子，并可以覆盖自己填过的数字', () => {
    const { game, puzzle, solution } = newGame();
    const [cell] = emptyCells(puzzle);
    const correct = solution[cell.row][cell.col];
    const wrong = correct === 9 ? 1 : correct + 1;

    expect(game.place(cell.row + 1, cell.col + 1, correct).message).toBe(
      `已填入第 ${cell.row + 1} 行第 ${cell.col + 1} 列 = ${correct}`,
    );
    expect(game.place(cell.row + 1, cell.col + 1, wrong).message).toContain(
      `已把第 ${cell.row + 1} 行第 ${cell.col + 1} 列从 ${correct} 改为 ${wrong}`,
    );
  });

  it('错误的数字允许落盘，并在与同行同列同宫重复时给出提示', () => {
    const { game, puzzle } = newGame();
    const givenInRow = puzzle[0].findIndex((value) => value !== 0);
    const target = emptyCells(puzzle).find((cell) => cell.row === 0)!;
    const value = puzzle[0][givenInRow];

    const result = game.place(1, target.col + 1, value);

    expect(result.ok).toBe(true);
    expect(result.message).toContain('重复');
    expect(game.conflicts()).toContainEqual({ row: 1, col: target.col + 1 });
  });

  it('擦除只能擦自己填的格', () => {
    const { game, puzzle } = newGame();
    const [cell] = emptyCells(puzzle);
    const given = puzzle.flatMap((line, row) => line.map((value, col) => ({ value, row, col }))).find((c) => c.value !== 0)!;

    expect(game.erase(cell.row + 1, cell.col + 1).message).toContain('本来就是空的');
    game.place(cell.row + 1, cell.col + 1, 2);
    expect(game.erase(cell.row + 1, cell.col + 1).ok).toBe(true);
    expect(game.erase(given.row + 1, given.col + 1).message).toContain('不能擦');
  });

  it('参数超出 1 到 9 时拒绝', () => {
    const { game } = newGame();

    expect(game.place(0, 1, 1).ok).toBe(false);
    expect(game.place(1, 10, 1).ok).toBe(false);
    expect(game.place(1, 1, 1.5).ok).toBe(false);
  });

  it('全部填入正确答案即完成', () => {
    const { game, puzzle, solution } = newGame();
    let last = { ok: false, message: '' };
    for (const cell of emptyCells(puzzle)) {
      last = game.place(cell.row + 1, cell.col + 1, solution[cell.row][cell.col]);
    }

    expect(game.solved).toBe(true);
    expect(last.message).toContain('完成');
    expect(game.place(1, 1, 1).message).toContain('已经完成');
  });

  it('填满但有错误时只报告错误数量，不报告位置', () => {
    const { game, puzzle, solution } = newGame();
    const cells = emptyCells(puzzle);
    for (const cell of cells.slice(0, -1)) {
      game.place(cell.row + 1, cell.col + 1, solution[cell.row][cell.col]);
    }
    const last = cells[cells.length - 1];
    const wrong = solution[last.row][last.col] === 9 ? 1 : solution[last.row][last.col] + 1;

    const result = game.place(last.row + 1, last.col + 1, wrong);

    expect(game.solved).toBe(false);
    expect(result.message.split('。').at(-1)).toBe('盘面已填满，但有 1 个格子与答案不一致，需要找出并擦掉错误的数字');
  });

  it('look 显示行列编号与空格，候选数可选', () => {
    const { game } = newGame();

    expect(game.look(false).split('\n')[0]).toBe('列     1 2 3 | 4 5 6 | 7 8 9');
    expect(game.look(false)).toContain('第1行');
    expect(game.look(false)).toContain('.');
    expect(game.look(false)).not.toContain('可能是');
    expect(game.look(true)).toContain('可能是');
  });
});
