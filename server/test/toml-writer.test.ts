// R12 契约：TOML 手术式写入——注释与其余行字节级保留

import { describe, expect, it } from "vitest";
import { parse } from "smol-toml";

import { serializeTomlValue, setTomlKey } from "../src/config/toml-writer.js";

const SAMPLE = `# MaiBot 配置
# 第二行注释

[webui]
enabled = true
port = 8001 # WebUI 访问端口。
host = ["127.0.0.1", "::1"]
name = "a#b" # 值里带井号

[chat]
# 聊天配置
max_context = 20
`;

describe("setTomlKey", () => {
  it("更新已存在的键：保留行内注释与其余行字节不变", () => {
    const result = setTomlKey(SAMPLE, "webui", "port", 18099);
    expect(result.action).toBe("updated");
    expect(result.changed).toBe(true);

    const lines = result.text.split("\n");
    const portLine = lines.find((line) => line.trim().startsWith("port"))!;
    expect(portLine).toBe("port = 18099 # WebUI 访问端口。");
    // 其余内容逐字节保留
    expect(result.text).toContain("# MaiBot 配置");
    expect(result.text).toContain('name = "a#b" # 值里带井号');
    expect(result.text).toContain("max_context = 20");
    // 解析回语义：只动了目标键
    const parsed = parse(result.text) as { webui: { port: number; name: string } };
    expect(parsed.webui.port).toBe(18099);
    expect(parsed.webui.name).toBe("a#b");
  });

  it("值没变时返回 unchanged", () => {
    const result = setTomlKey(SAMPLE, "webui", "port", 8001);
    expect(result.action).toBe("unchanged");
    expect(result.changed).toBe(false);
    expect(result.text).toBe(SAMPLE);
  });

  it("键不存在、节存在：插入节头之后", () => {
    const result = setTomlKey(SAMPLE, "webui", "secure_cookie", false);
    expect(result.action).toBe("inserted");
    expect(result.text).toMatch(/\[webui\]\nsecure_cookie = false\n/);
    const parsed = parse(result.text) as { webui: { secure_cookie: boolean; enabled: boolean } };
    expect(parsed.webui.secure_cookie).toBe(false);
    expect(parsed.webui.enabled).toBe(true);
  });

  it("节不存在：文件末尾新建节", () => {
    const result = setTomlKey(SAMPLE, "vtuber", "lipsync_enabled", true);
    expect(result.action).toBe("section_added");
    const parsed = parse(result.text) as { vtuber: { lipsync_enabled: boolean } };
    expect(parsed.vtuber.lipsync_enabled).toBe(true);
    expect(result.text).toContain("[vtuber]");
  });

  it("更新带引号井号的字符串键：注释切分不误伤值", () => {
    const result = setTomlKey(SAMPLE, "webui", "name", "x#y");
    expect(result.action).toBe("updated");
    const lines = result.text.split("\n");
    const nameLine = lines.find((line) => line.trim().startsWith("name"))!;
    expect(nameLine).toBe('name = "x#y" # 值里带井号');
    const parsed = parse(result.text) as { webui: { name: string } };
    expect(parsed.webui.name).toBe("x#y");
  });

  it("幂等：反复设置同一值不再变化", () => {
    const once = setTomlKey(SAMPLE, "chat", "max_context", 32);
    expect(once.action).toBe("updated");
    const twice = setTomlKey(once.text, "chat", "max_context", 32);
    expect(twice.action).toBe("unchanged");
  });

  it("数组值序列化", () => {
    expect(serializeTomlValue(["127.0.0.1", "::1"])).toBe('["127.0.0.1", "::1"]');
    expect(serializeTomlValue(true)).toBe("true");
    expect(serializeTomlValue("中文")).toBe('"中文"');
  });
});
