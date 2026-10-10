// 没有 package.json 的文件夹按默认 index.ts 入口处理
export const apply = (ctx: { emit(name: string, value: unknown): void }) => {
  ctx.emit('plugin-loaded', 'two');
};
export const config = {};
