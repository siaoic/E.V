export const apply = (ctx: { emit(name: string, value: unknown): void }) => {
  ctx.emit('plugin-loaded', 'custom');
};
export const config = {};
