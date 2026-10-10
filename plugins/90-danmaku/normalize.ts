import type { Command } from './codec.ts';

export type DanmakuEvent = {
  type: 'danmaku';
  uid: number;
  uname: string;
  text: string;
  timestamp: number;
};

export type SuperChatEvent = {
  type: 'super-chat';
  uid: number;
  uname: string;
  text: string;
  price: number;
  timestamp: number;
};

export type HeartbeatEvent = {
  type: 'heartbeat';
  count: number | null;
  timestamp: number;
};

type DanmuInfo = [
  [unknown, unknown, unknown, unknown, number],
  string,
  [number, string, ...unknown[]],
  ...unknown[],
];

type SuperChatData = {
  uid: number;
  price: number;
  message: string;
  start_time: number;
  user_info: { uname: string };
};

export function normalizeCommand(command: Command): DanmakuEvent | SuperChatEvent | HeartbeatEvent | null {
  const cmd = String(command.cmd).split(':')[0];
  if (cmd === 'HEARTBEAT_REPLY') {
    return {
      type: 'heartbeat',
      count: typeof command.count === 'number' ? command.count : null,
      timestamp: Date.now(),
    };
  }
  if (cmd === 'DANMU_MSG') {
    const info = command.info as DanmuInfo;
    return {
      type: 'danmaku',
      uid: info[2][0],
      uname: info[2][1],
      text: info[1],
      timestamp: info[0][4],
    };
  }
  if (command.cmd === 'SUPER_CHAT_MESSAGE') {
    const data = command.data as SuperChatData;
    return {
      type: 'super-chat',
      uid: data.uid,
      uname: data.user_info.uname,
      text: data.message,
      price: data.price,
      timestamp: data.start_time * 1000,
    };
  }
  return null;
}
