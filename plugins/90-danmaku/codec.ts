import { brotliDecompressSync, inflateSync } from 'node:zlib';

export const Operation = {
  HEARTBEAT: 2,
  HEARTBEAT_REPLY: 3,
  SEND_MSG_REPLY: 5,
  AUTH: 7,
  AUTH_REPLY: 8,
} as const;

const HEADER_SIZE = 16;

export type Command = Record<string, unknown>;

export function encodePacket(operation: number, body: unknown): Buffer {
  const payload = Buffer.from(JSON.stringify(body), 'utf8');
  const header = Buffer.alloc(HEADER_SIZE);
  header.writeUInt32BE(HEADER_SIZE + payload.length, 0);
  header.writeUInt16BE(HEADER_SIZE, 4);
  header.writeUInt16BE(1, 6);
  header.writeUInt32BE(operation, 8);
  header.writeUInt32BE(1, 12);
  return Buffer.concat([header, payload]);
}

export function decodeCommands(data: Buffer): Command[] {
  const commands: Command[] = [];
  let offset = 0;
  while (offset < data.length) {
    const packLen = data.readUInt32BE(offset);
    const headerLen = data.readUInt16BE(offset + 4);
    const ver = data.readUInt16BE(offset + 6);
    const operation = data.readUInt32BE(offset + 8);
    const body = data.subarray(offset + headerLen, offset + packLen);
    if (operation === Operation.SEND_MSG_REPLY) {
      if (ver === 3) {
        commands.push(...decodeCommands(brotliDecompressSync(body)));
      } else if (ver === 2) {
        commands.push(...decodeCommands(inflateSync(body)));
      } else if (ver === 0 && body.length > 0) {
        commands.push(JSON.parse(body.toString('utf8')) as Command);
      }
    } else if (operation === Operation.HEARTBEAT_REPLY) {
      const text = body.toString('utf8').trim();
      const count = Number(text);
      commands.push({ cmd: 'HEARTBEAT_REPLY', count: text !== '' && Number.isFinite(count) ? count : null });
    }
    offset += packLen;
  }
  return commands;
}
