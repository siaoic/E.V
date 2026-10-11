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

/** 上舰（开通/续费大航海）。这是全房间唯一原先完全落在感知之外的付费行为 */
export type GuardEvent = {
  type: 'guard';
  uid: number;
  uname: string;
  /** B 站 guard_level 口径：1 总督 / 2 提督 / 3 舰长 */
  guardLevel: number;
  /** 开通月数（一次买几个月） */
  num: number;
  /** 平台给的金瓜子价格，缺失为 0 */
  price: number;
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

// GUARD_BUY 的 data 字段平台给的类型不稳（数字/字符串都见过），统一 Number() 兜一遍
type GuardData = {
  uid?: number | string;
  username?: string;
  guard_level?: number | string;
  num?: number | string;
  price?: number | string;
  start_time?: number | string;
};

export function normalizeCommand(
  command: Command,
): DanmakuEvent | SuperChatEvent | GuardEvent | HeartbeatEvent | null {
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
  if (cmd === 'SUPER_CHAT_MESSAGE') {
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
  if (cmd === 'GUARD_BUY') {
    const data = (command.data ?? {}) as GuardData;
    return {
      type: 'guard',
      uid: Number(data.uid ?? 0),
      uname: String(data.username ?? ''),
      guardLevel: Number(data.guard_level ?? 0),
      num: Number(data.num ?? 1),
      price: Number(data.price ?? 0),
      timestamp: Number(data.start_time ?? 0) * 1000,
    };
  }
  return null;
}
