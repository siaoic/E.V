import type { ToolDefinition } from '../../llm/client.ts';
import { createRandom, createSudokuGame, type SudokuGame } from './engine.ts';

export type BoardSnapshot = {
  status: 'none' | 'playing' | 'solved';
  filled: number;
  board: number[][];
  givens: boolean[][];
  conflicts: { row: number; col: number }[];
};

export type SudokuToolkit = {
  prompt: string;
  tools: ToolDefinition[];
  run(name: string, rawArguments: string): string;
  describe(): string;
  isActive(): boolean;
  snapshot(): BoardSnapshot;
  /** 未开局时唯一该暴露的工具：她只有想玩的时候才自己开一局 */
  openers?: string[];
  /** 未开局时注入的那一行菜单（由插件填目录文案） */
  menu?: string;
};

const GAME_PROMPT =
  '你正在直播里随手玩数独，就是边玩边自言自语的那种：会调用 sudoku_new 开局，sudoku_look 看盘面，sudoku_place 落子，sudoku_erase 擦掉自己填的数字，sudoku_check 查找冲突。像真人一样一次只做一个动作：每轮只能填或擦一个格子，动手前先把心里话说出来。说话是你平时的闲聊腔，不是解说员：别报坐标（不要说"第3行第5列"），别用"我们看到/各位观众"这种解说腔，就是嘀咕、犹豫、吐槽、得意、后悔（"这格我盯半天了""哎不对不对""稳了"）。错误的数字不会被自动指出，要自己发现。每句不超过 40 个字。';

const NO_GAME = '还没有开局，可以调用 sudoku_new 开一局';

const cellProperties = {
  row: { type: 'integer', minimum: 1, maximum: 9, description: '行，1 到 9，从上到下' },
  col: { type: 'integer', minimum: 1, maximum: 9, description: '列，1 到 9，从左到右' },
};

export const SUDOKU_TOOLS: ToolDefinition[] = [
  {
    type: 'function',
    function: {
      name: 'sudoku_new',
      description: '开一局新的数独。只有当前没有进行中的局时才有效。',
      parameters: { type: 'object', properties: {}, additionalProperties: false },
    },
  },
  {
    type: 'function',
    function: {
      name: 'sudoku_look',
      description: '查看当前盘面。candidates 为 true 时列出每个空格的可能数字。',
      parameters: {
        type: 'object',
        properties: { candidates: { type: 'boolean', description: '是否列出每个空格的可能数字' } },
        additionalProperties: false,
      },
    },
  },
  {
    type: 'function',
    function: {
      name: 'sudoku_place',
      description: '在空格或自己填过的格子里落一个数字。错误的数字也会落盘，需要自己发现并擦掉。',
      parameters: {
        type: 'object',
        properties: { ...cellProperties, value: { type: 'integer', minimum: 1, maximum: 9, description: '要填的数字' } },
        required: ['row', 'col', 'value'],
        additionalProperties: false,
      },
    },
  },
  {
    type: 'function',
    function: {
      name: 'sudoku_erase',
      description: '擦掉自己填过的格子。题目给定的格子不能擦。',
      parameters: {
        type: 'object',
        properties: cellProperties,
        required: ['row', 'col'],
        additionalProperties: false,
      },
    },
  },
  {
    type: 'function',
    function: {
      name: 'sudoku_check',
      description: '列出盘面上同行、同列或同宫重复的冲突格子。不会告诉你哪个数字是错的。',
      parameters: { type: 'object', properties: {}, additionalProperties: false },
    },
  },
];

function parseArguments(raw: string): Record<string, unknown> {
  try {
    const value: unknown = JSON.parse(raw || '{}');
    return typeof value === 'object' && value !== null ? (value as Record<string, unknown>) : {};
  } catch {
    return {};
  }
}

function integer(value: unknown): number {
  return typeof value === 'number' ? value : Number.NaN;
}

function describeConflicts(game: SudokuGame): string {
  const conflicts = game.conflicts();
  if (conflicts.length === 0) return '没有发现规则冲突';
  return `冲突的格子：${conflicts.map((cell) => `第${cell.row}行第${cell.col}列`).join('、')}`;
}

export function createSudokuToolkit(
  options: { nextSeed?: () => number; onChange?: () => void } = {},
): SudokuToolkit {
  const nextSeed = options.nextSeed ?? (() => Math.floor(Math.random() * 2 ** 32));
  let game: SudokuGame | undefined;

  const execute = (name: string, rawArguments: string): string => {
    const args = parseArguments(rawArguments);
    if (name === 'sudoku_new') {
      if (game && !game.solved) return '这一局还没有完成，不能开新局';
      game = createSudokuGame(createRandom(nextSeed()));
      return `新的一局开始了。\n${game.look(false)}`;
    }
    const current = game;
    if (!current) return NO_GAME;
    switch (name) {
      case 'sudoku_look':
        return current.look(args.candidates === true);
      case 'sudoku_place':
        return current.place(integer(args.row), integer(args.col), integer(args.value)).message;
      case 'sudoku_erase':
        return current.erase(integer(args.row), integer(args.col)).message;
      case 'sudoku_check':
        return describeConflicts(current);
      default:
        return '（未知工具）';
    }
  };

  return {
    prompt: GAME_PROMPT,
    tools: SUDOKU_TOOLS,
    // 没开局时只暴露这一件：想玩才开，没开就不把工具箱摆出来诱导她
    openers: ['sudoku_new'],
    isActive: () => game !== undefined && !game.solved,
    describe() {
      if (!game) return `（数独：${NO_GAME}）`;
      if (game.solved) return `（数独：上一局已完成，可以调用 sudoku_new 开新一局）\n${game.look(false)}`;
      return `（数独进行中）\n${game.look(false)}`;
    },
    snapshot(): BoardSnapshot {
      if (!game) return { status: 'none', filled: 0, board: [], givens: [], conflicts: [] };
      return {
        status: game.solved ? 'solved' : 'playing',
        filled: game.filledCount(),
        board: game.board.map((row) => [...row]),
        givens: game.givens.map((row) => [...row]),
        conflicts: game.conflicts(),
      };
    },
    run(name, rawArguments) {
      const result = execute(name, rawArguments);
      if (name.startsWith('sudoku_')) options.onChange?.();
      return result;
    },
  };
}
