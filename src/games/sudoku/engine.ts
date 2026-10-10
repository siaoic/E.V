export type Grid = number[][];

export type PlaceResult = { ok: boolean; message: string };

export type Coordinate = { row: number; col: number };

export function createRandom(seed: number): () => number {
  let state = seed >>> 0;
  return () => {
    state = (state + 0x6d2b79f5) >>> 0;
    let t = state;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

function shuffle<T>(items: T[], random: () => number): T[] {
  const result = [...items];
  for (let i = result.length - 1; i > 0; i -= 1) {
    const j = Math.floor(random() * (i + 1));
    [result[i], result[j]] = [result[j], result[i]];
  }
  return result;
}

function canPlace(grid: Grid, row: number, col: number, value: number): boolean {
  for (let i = 0; i < 9; i += 1) {
    if (grid[row][i] === value || grid[i][col] === value) return false;
  }
  const top = row - (row % 3);
  const left = col - (col % 3);
  for (let r = top; r < top + 3; r += 1) {
    for (let c = left; c < left + 3; c += 1) {
      if (grid[r][c] === value) return false;
    }
  }
  return true;
}

function findEmpty(grid: Grid): Coordinate | undefined {
  for (let row = 0; row < 9; row += 1) {
    for (let col = 0; col < 9; col += 1) {
      if (grid[row][col] === 0) return { row, col };
    }
  }
  return undefined;
}

function fillSolution(grid: Grid, random: () => number): boolean {
  const empty = findEmpty(grid);
  if (!empty) return true;
  for (const value of shuffle([1, 2, 3, 4, 5, 6, 7, 8, 9], random)) {
    if (!canPlace(grid, empty.row, empty.col, value)) continue;
    grid[empty.row][empty.col] = value;
    if (fillSolution(grid, random)) return true;
    grid[empty.row][empty.col] = 0;
  }
  return false;
}

export function countSolutions(grid: Grid, limit = 2): number {
  const empty = findEmpty(grid);
  if (!empty) return 1;
  let count = 0;
  for (let value = 1; value <= 9 && count < limit; value += 1) {
    if (!canPlace(grid, empty.row, empty.col, value)) continue;
    grid[empty.row][empty.col] = value;
    count += countSolutions(grid, limit - count);
    grid[empty.row][empty.col] = 0;
  }
  return count;
}

export function createPuzzle(random: () => number, clues = 36): { puzzle: Grid; solution: Grid } {
  const solution: Grid = Array.from({ length: 9 }, () => Array(9).fill(0));
  fillSolution(solution, random);
  const puzzle = solution.map((row) => [...row]);
  const positions = shuffle(
    Array.from({ length: 81 }, (_, index) => ({ row: Math.floor(index / 9), col: index % 9 })),
    random,
  );
  let removals = 81 - clues;
  for (const { row, col } of positions) {
    if (removals <= 0) break;
    const value = puzzle[row][col];
    puzzle[row][col] = 0;
    if (countSolutions(puzzle, 2) === 1) {
      removals -= 1;
    } else {
      puzzle[row][col] = value;
    }
  }
  return { puzzle, solution };
}

function inRange(value: number): boolean {
  return Number.isInteger(value) && value >= 1 && value <= 9;
}

function peersOf({ row, col }: Coordinate): Coordinate[] {
  const peers: Coordinate[] = [];
  const top = row - (row % 3);
  const left = col - (col % 3);
  for (let i = 0; i < 9; i += 1) {
    if (i !== col) peers.push({ row, col: i });
    if (i !== row) peers.push({ row: i, col });
  }
  for (let r = top; r < top + 3; r += 1) {
    for (let c = left; c < left + 3; c += 1) {
      if (r !== row && c !== col) peers.push({ row: r, col: c });
    }
  }
  return peers;
}

export class SudokuGame {
  readonly givens: boolean[][];
  readonly board: Grid;
  private readonly solution: Grid;
  private finished = false;

  constructor(puzzle: Grid, solution: Grid) {
    this.givens = puzzle.map((row) => row.map((value) => value !== 0));
    this.board = puzzle.map((row) => [...row]);
    this.solution = solution;
  }

  get solved(): boolean {
    return this.finished;
  }

  filledCount(): number {
    return this.board.flat().filter((value) => value !== 0).length;
  }

  place(row: number, col: number, value: number): PlaceResult {
    if (this.finished) return { ok: false, message: '这一局已经完成，可以调用 sudoku_new 开新一局' };
    if (!inRange(row) || !inRange(col) || !inRange(value)) {
      return { ok: false, message: '行、列、数字都必须是 1 到 9 的整数' };
    }
    const at = { row: row - 1, col: col - 1 };
    if (this.givens[at.row][at.col]) {
      return { ok: false, message: `第 ${row} 行第 ${col} 列是题目给定的，不能修改` };
    }
    const previous = this.board[at.row][at.col];
    this.board[at.row][at.col] = value;
    let message =
      previous === 0
        ? `已填入第 ${row} 行第 ${col} 列 = ${value}`
        : `已把第 ${row} 行第 ${col} 列从 ${previous} 改为 ${value}`;
    const clashes = peersOf(at).filter((peer) => this.board[peer.row][peer.col] === value).length;
    if (clashes > 0) message += `（与同行、同列或同宫的 ${clashes} 个格子重复）`;
    if (this.filledCount() === 81) message += this.finishBoard();
    return { ok: true, message };
  }

  erase(row: number, col: number): PlaceResult {
    if (this.finished) return { ok: false, message: '这一局已经完成，可以调用 sudoku_new 开新一局' };
    if (!inRange(row) || !inRange(col)) return { ok: false, message: '行和列必须是 1 到 9 的整数' };
    const at = { row: row - 1, col: col - 1 };
    if (this.givens[at.row][at.col]) return { ok: false, message: '题目给定的格不能擦' };
    if (this.board[at.row][at.col] === 0) return { ok: false, message: `第 ${row} 行第 ${col} 列本来就是空的` };
    this.board[at.row][at.col] = 0;
    return { ok: true, message: `已擦掉第 ${row} 行第 ${col} 列` };
  }

  conflicts(): Coordinate[] {
    const found: Coordinate[] = [];
    for (let row = 0; row < 9; row += 1) {
      for (let col = 0; col < 9; col += 1) {
        const value = this.board[row][col];
        if (value === 0) continue;
        if (peersOf({ row, col }).some((peer) => this.board[peer.row][peer.col] === value)) {
          found.push({ row: row + 1, col: col + 1 });
        }
      }
    }
    return found;
  }

  look(withCandidates: boolean): string {
    // 表头用与「第N行」等宽的前缀（CJK 按双宽计），保证列号与格子对齐
    const lines = ['列     1 2 3 | 4 5 6 | 7 8 9'];
    for (let row = 0; row < 9; row += 1) {
      const groups = [0, 3, 6].map((start) =>
        this.board[row]
          .slice(start, start + 3)
          .map((value) => (value === 0 ? '.' : String(value)))
          .join(' '),
      );
      lines.push(`第${row + 1}行  ${groups.join(' | ')}`);
    }
    lines.push(`已填 ${this.filledCount()}/81`);
    if (withCandidates) {
      for (let row = 0; row < 9; row += 1) {
        for (let col = 0; col < 9; col += 1) {
          if (this.board[row][col] !== 0) continue;
          const used = new Set(peersOf({ row, col }).map((peer) => this.board[peer.row][peer.col]));
          const candidates = [1, 2, 3, 4, 5, 6, 7, 8, 9].filter((value) => !used.has(value));
          lines.push(`第 ${row + 1} 行第 ${col + 1} 列可能是：${candidates.join(' ')}`);
        }
      }
    }
    return lines.join('\n');
  }

  private finishBoard(): string {
    let mismatches = 0;
    for (let row = 0; row < 9; row += 1) {
      for (let col = 0; col < 9; col += 1) {
        if (this.board[row][col] !== this.solution[row][col]) mismatches += 1;
      }
    }
    if (mismatches === 0) {
      this.finished = true;
      return '。恭喜，整盘正确，这一局完成！';
    }
    return `。盘面已填满，但有 ${mismatches} 个格子与答案不一致，需要找出并擦掉错误的数字`;
  }
}

export function createSudokuGame(random: () => number): SudokuGame {
  const { puzzle, solution } = createPuzzle(random);
  return new SudokuGame(puzzle, solution);
}
