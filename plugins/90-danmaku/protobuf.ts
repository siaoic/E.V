export type ProtoField = { field: number; value: number | Buffer };

function readVarint(buf: Buffer, start: number): [number, number] {
  let result = 0;
  let multiplier = 1;
  let pos = start;
  for (;;) {
    const byte = buf[pos++];
    result += (byte & 0x7f) * multiplier;
    if ((byte & 0x80) === 0) return [result, pos];
    multiplier *= 0x80;
  }
}

export function readFields(buf: Buffer): ProtoField[] {
  const fields: ProtoField[] = [];
  let pos = 0;
  while (pos < buf.length) {
    const [key, afterKey] = readVarint(buf, pos);
    pos = afterKey;
    const field = Math.floor(key / 8);
    const wireType = key % 8;
    if (wireType === 0) {
      const [value, next] = readVarint(buf, pos);
      fields.push({ field, value });
      pos = next;
    } else if (wireType === 2) {
      const [length, next] = readVarint(buf, pos);
      fields.push({ field, value: buf.subarray(next, next + length) });
      pos = next + length;
    } else if (wireType === 1) {
      pos += 8;
    } else if (wireType === 5) {
      pos += 4;
    } else {
      throw new Error(`unsupported protobuf wire type ${wireType}`);
    }
  }
  return fields;
}

export function getVarint(fields: ProtoField[], field: number): number {
  const found = fields.find((f) => f.field === field);
  return found === undefined ? 0 : (found.value as number);
}

export function getString(fields: ProtoField[], field: number): string {
  const found = fields.find((f) => f.field === field);
  return found === undefined ? '' : (found.value as Buffer).toString('utf8');
}

export function getMessages(fields: ProtoField[], field: number): Buffer[] {
  return fields.filter((f) => f.field === field).map((f) => f.value as Buffer);
}
