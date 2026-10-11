import type { GameHooks } from '../llm/reply.ts';
import { createSudokuToolkit, type SudokuToolkit } from './sudoku/tools.ts';

export type GameEntry = { id: string; name: string; description: string };

// 游戏目录：告诉 AI 本场有哪些游戏可玩；想加新游戏，在这里登记并接一个工厂分支
export const GAME_CATALOG: GameEntry[] = [
  { id: 'sudoku', name: '数独', description: '9x9 数独，sudoku_new 开局，一次填一格，边玩边嘀咕' },
];

export function createGame<T extends GameHooks = GameHooks>(
  id = process.env.GAME ?? 'sudoku',
  options: { onChange?: () => void } = {},
): { hooks: T; entry: GameEntry } {
  const entry = GAME_CATALOG.find((game) => game.id === id);
  if (!entry) throw new Error(`未知的游戏：${id}`);
  const hooks = (entry.id === 'sudoku' ? createSudokuToolkit(options) : createSudokuToolkit(options)) as unknown as T;
  return { hooks, entry };
}

export function catalogLine(id: string): string {
  const entry = GAME_CATALOG.find((game) => game.id === id);
  // 措辞刻意保持"可选"：这是菜单，不是任务；想玩才开，不玩也没关系
  return entry ? `（本场可以玩的游戏：${entry.name}——${entry.description}；想玩的时候自己开一局就行，不玩也没关系）` : '';
}
