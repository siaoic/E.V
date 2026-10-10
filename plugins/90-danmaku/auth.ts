import { encodePacket, Operation } from './codec.ts';

export type AuthParams = { roomId: number; uid: number; buvid: string; token?: string };

export function buildAuthPacket({ roomId, uid, buvid, token }: AuthParams): Buffer {
  const body: Record<string, unknown> = {
    uid,
    roomid: roomId,
    protover: 3,
    platform: 'web',
    type: 2,
    buvid,
  };
  if (token !== undefined) body.key = token;
  return encodePacket(Operation.AUTH, body);
}
