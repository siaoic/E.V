// 观察者插件：控制台打印弹幕/礼物/SC、AI 的回复和播报进度。复制本文件夹改名即可叠加第二份日志
// 顺序规范：00-19 服务 provider、20-79 消费者/观察者、80+ 事件生产者；停用 = 文件夹改名加 .disabled
// 注意：插件文件运行时以 Node 类型剥离方式加载，不能使用参数属性等 TS-only 语法
import type { Context } from '@deepseek-ai/cordis';
import type { LiveEvent } from '../90-danmaku/plugin.ts';
import type { AgentReply } from '../../src/llm/plugin.ts';

export const apply = Object.assign(
  function consoleLog(ctx: Context) {
    ctx.on('world/live-event', (event: LiveEvent) => {
      if (event.type === 'danmaku') console.log(`[弹幕] ${event.uname}: ${event.text}`);
      else if (event.type === 'gift') console.log(`[礼物] ${event.uname} ${event.giftName}×${event.num}`);
      else if (event.type === 'super-chat') console.log(`[SC ¥${event.price}] ${event.uname}: ${event.text}`);
    });
    ctx.on('agent/reply', (reply: AgentReply) => {
      console.log(`  -> ${reply.text}`);
    });
    ctx.on('playback/started', ({ sentence }: { sentence: string }) => console.log(`  [播报] ${sentence}`));
    ctx.on('playback/completed', () => console.log('  [播报完成]'));
  },
  { inject: [] as string[] },
);
export const config = {};
