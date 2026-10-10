// llama.cpp llama-server 的 OpenAI 兼容嵌入端点客户端。
// 任何失败（服务未启动、超时、非 2xx）都返回 null，调用方回退到朴素检索——记忆系统不因嵌入服务缺席而不可用。
export type Embedder = { embed(texts: string[]): Promise<number[][] | null> };

export type EmbedderOptions = {
  url: string;
  model: string;
  fetch?: typeof globalThis.fetch;
  timeoutMs?: number;
};

export function createEmbedder({ url, model, fetch: doFetch = globalThis.fetch, timeoutMs = 7000 }: EmbedderOptions): Embedder {
  return {
    async embed(texts: string[]): Promise<number[][] | null> {
      if (texts.length === 0) return [];
      const controller = new AbortController();
      const timer = setTimeout(() => controller.abort(), timeoutMs);
      try {
        const response = await doFetch(`${url}/v1/embeddings`, {
          method: 'POST',
          headers: { 'content-type': 'application/json' },
          body: JSON.stringify({ model, input: texts }),
          signal: controller.signal,
        });
        if (!response.ok) return null;
        const data = (await response.json()) as { data: { embedding: number[] }[] };
        const vectors = data.data.map((entry) => entry.embedding);
        return vectors.length === texts.length ? vectors : null;
      } catch {
        return null;
      } finally {
        clearTimeout(timer);
      }
    },
  };
}
