// The LLM is stubbed at the network boundary: the test double speaks the provider's wire format.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { orderStatusNote } from "../src/services/support";

const fetchMock = vi.fn();

beforeEach(() => {
  process.env.ANTHROPIC_API_KEY = "test-key";
  fetchMock.mockResolvedValue(
    new Response(JSON.stringify({ content: [{ type: "text", text: "  Hi Ada, your order is on its way!  " }] }), { status: 200 }),
  );
  vi.stubGlobal("fetch", fetchMock);
});

afterEach(() => {
  vi.unstubAllGlobals();
  fetchMock.mockReset();
});

describe("support notes", () => {
  it("returns the model's sentence, trimmed", async () => {
    expect(await orderStatusNote("Ada", "shipped")).toBe("Hi Ada, your order is on its way!");
  });

  it("authenticates with the configured key and sends the prompt", async () => {
    await orderStatusNote("Ada", "shipped");
    const [, init] = fetchMock.mock.calls[0];
    expect(JSON.stringify(init.headers)).toContain("test-key");
    expect(init.body).toContain("Ada");
  });
});
