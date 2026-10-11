// 覆写前的破坏性检测：不拦截写入，只把「比上一版短了很多、哪些小节会消失」说清楚。
// 口径照搬 Cortico 0.1.6 `bots/cortiv/persona/persona.ts:90-170`：
// 比例 0.5、上一版短于 200 字符就不提示（小文件改几行就过半，提示会变成噪音）、
// 小节差集最多点 8 个。Cortico 里这条是「防一次坏写入」唯一真正落地的一环——
// 写入照常完成，回执点名要消失的小节，旧版靠 git_log/git_show 取回。

/** 缩水提示的触发线：新正文短于上一版的这个比例 */
export const SHRINK_RATIO = 0.5;
/** 上一版短于这个长度就不提示 */
export const SHRINK_MIN_CHARS = 200;
/** 最多点名几个将要消失的小节 */
export const SHRINK_MAX_SECTIONS = 8;

const headings = (text: string): string[] =>
  text
    .split('\n')
    .map((line) => line.trim())
    .filter((line) => /^#{1,6}\s+\S/.test(line));

/** 返回要追加到回执里的提示；不该提示时返回 null */
export function shrinkNote(prev: string, next: string): string | null {
  if (prev.length < SHRINK_MIN_CHARS) return null;
  if (next.length >= prev.length * SHRINK_RATIO) return null;
  const kept = new Set(headings(next));
  const gone = [...new Set(headings(prev))].filter((heading) => !kept.has(heading));
  const head = `[缩水提示] 这一版 ${next.length} 字，上一版 ${prev.length} 字`;
  const tail = '有意精简就不用管；不是的话，用 git_log 查这个文件的历史、git_show 取回上一版正文，自己挑要留的写回去。';
  if (gone.length === 0) return `${head}。${tail}`;
  const shown = gone.slice(0, SHRINK_MAX_SECTIONS);
  const more = gone.length > shown.length ? `，另有 ${gone.length - shown.length} 节` : '';
  return `${head}；将要消失的小节：${shown.join('、')}${more}。${tail}`;
}
