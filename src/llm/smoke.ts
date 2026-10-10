import { Context } from '@deepseek-ai/cordis';
import { Operation } from '../../plugins/90-danmaku/codec.ts';
import { apply as danmakuApply, type DanmakuPluginConfig } from '../../plugins/90-danmaku/plugin.ts';
import { createLlmClient } from './client.ts';
import { apply as llmApply } from './plugin.ts';

function requireEnv(name: string): string {
  const value = process.env[name];
  if (!value) throw new Error(`missing environment variable ${name}`);
  return value;
}

function danmakuFrame(text: string): Buffer {
  const body = Buffer.from(
    JSON.stringify({
      cmd: 'DANMU_MSG',
      info: [
        [0, 1, 25, 16777215, Date.now(), 0, 0, '', 0, 0, 0, '', 0, '{}', '{}', {}],
        text,
        [123456, '测试观众', 0, 0, 0, 0, 0, ''],
      ],
    }),
  );
  const header = Buffer.alloc(16);
  header.writeUInt32BE(16 + body.length, 0);
  header.writeUInt16BE(16, 4);
  header.writeUInt16BE(0, 6);
  header.writeUInt32BE(Operation.SEND_MSG_REPLY, 8);
  header.writeUInt32BE(1, 12);
  return Buffer.concat([header, body]);
}

const llm = createLlmClient({
  baseUrl: requireEnv('LLM_BASE_URL'),
  apiKey: requireEnv('LLM_API_KEY'),
  model: requireEnv('LLM_MODEL'),
});

let handlers: { onMessage(data: Buffer): void } | undefined;
const socket = { send() {}, close() {} };
const danmakuConfig: DanmakuPluginConfig = {
  roomId: 22625027,
  uid: 0,
  buvid: 'SMOKE',
  connect: (h: { onOpen(): void; onMessage(data: Buffer): void; onClose(): void }) => {
    handlers = h;
    return socket;
  },
};

const root = new Context();
const reply = new Promise<string>((resolve) => root.on('agent/reply', (r) => resolve(r.text)));
root.provide('llm', llm);
await root.plugin(llmApply, {});
const fiber = await root.plugin(danmakuApply, danmakuConfig);
handlers?.onMessage(danmakuFrame('今天唱什么歌'));

console.log(await reply);
await fiber.dispose();
