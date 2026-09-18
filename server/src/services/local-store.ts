/**
 * data/local_store.json 的 TS 侧读写（对应 Python LocalStoreManager）。
 *
 * 文件是「扁平键值」JSON（{key: value}），与 Python 侧互换读写；
 * 写入是全量覆盖（Python __setitem__ 也是每次全量 save）。
 */

import { readFileSync, writeFileSync, existsSync } from "node:fs";

import type { FastifyBaseLogger } from "fastify";

export class LocalStore {
  private store: Record<string, unknown>;

  private constructor(
    readonly filePath: string,
    private readonly logger: FastifyBaseLogger,
  ) {
    if (existsSync(filePath)) {
      try {
        this.store = JSON.parse(readFileSync(filePath, "utf8")) as Record<string, unknown>;
      } catch (error) {
        this.logger.error({ error, filePath }, "本地存储文件损坏，按空存储启动");
        this.store = {};
      }
    } else {
      this.store = {};
    }
  }

  static open(filePath: string, logger: FastifyBaseLogger): LocalStore {
    return new LocalStore(filePath, logger);
  }

  get<T = unknown>(key: string): T | undefined {
    return this.store[key] as T | undefined;
  }

  set(key: string, value: unknown): void {
    this.store[key] = value;
    this.save();
  }

  private save(): void {
    try {
      writeFileSync(this.filePath, `${JSON.stringify(this.store, null, 2)}\n`, "utf8");
    } catch (error) {
      this.logger.error({ error, filePath: this.filePath }, "本地存储写入失败");
    }
  }
}
