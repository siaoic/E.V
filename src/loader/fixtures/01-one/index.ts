export const apply = (ctx: { emit(name: string, value: unknown): void }) => {
  ctx.emit('plugin-loaded', 'one');
};
export const config = {};
