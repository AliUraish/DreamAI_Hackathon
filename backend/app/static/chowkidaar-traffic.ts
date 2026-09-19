// Chowkidaar traffic reporter. Drop this file into a TypeScript service (Cloudflare Workers, Node 18+, Bun, Deno).
//
//   const traffic = createTraffic({ url: env.CHOWKIDAAR_URL, apiKey: env.CHOWKIDAAR_API_KEY, repoId: "repo_..." });
//   const callOpenAI = traffic.watch("callOpenAI", "src/services/explanation.ts", rawCallOpenAI);
//   ...
//   ctx.waitUntil(traffic.flush());          // Workers: send the batch after the response has gone out
//
// It records how long each watched function ran, whether it threw, and which watched function called it.
// No arguments, return values or request data are ever read or sent.

export interface TrafficOptions { url: string; apiKey: string; repoId: string; maxBatch?: number }
interface Span { function: string; file: string; parent?: string; ms: number; ok: boolean; ts: number }

export function createTraffic(options: TrafficOptions) {
  let batch: Span[] = [];
  const stack: string[] = [];

  async function flush(): Promise<void> {
    if (batch.length === 0) return;
    const spans = batch;
    batch = [];
    try {
      await fetch(`${options.url.replace(/\/$/, "")}/api/v1/traffic`, {
        method: "POST",
        headers: { "content-type": "application/json", authorization: `Bearer ${options.apiKey}` },
        body: JSON.stringify({ repo_id: options.repoId, spans }),
      });
    } catch {
      // Reporting must never break the service it observes.
    }
  }

  function watch<A extends unknown[], R>(name: string, file: string, fn: (...args: A) => Promise<R>): (...args: A) => Promise<R> {
    return async (...args: A): Promise<R> => {
      const parent = stack[stack.length - 1];
      const started = Date.now();
      stack.push(name);
      let ok = true;
      try {
        return await fn(...args);
      } catch (error) {
        ok = false;
        throw error;
      } finally {
        stack.pop();
        batch.push({ function: name, file, parent, ms: Date.now() - started, ok, ts: started / 1000 });
        if (batch.length >= (options.maxBatch ?? 50)) void flush();
      }
    };
  }

  return { watch, flush };
}
