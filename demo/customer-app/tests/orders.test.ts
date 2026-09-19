// Integration tests: these hit the live Orders API (ORDERS_API_URL), not a mock,
// so they go red the moment the provider's contract changes.
import { beforeEach, describe, expect, it } from "vitest";
import { placeOrder } from "../src/services/checkout";
import { invoiceLines, revenueTotal } from "../src/services/billing";

const BASE_URL = process.env.ORDERS_API_URL ?? "http://localhost:4010";

beforeEach(async () => {
  await fetch(`${BASE_URL}/admin/reset-orders`, { method: "POST" });
});

describe("orders integration", () => {
  it("places an order and prints a receipt in dollars", async () => {
    expect(await placeOrder("Ada", 19.99)).toBe("Ada - $19.99 (pending)");
  });

  it("sums revenue in dollars", async () => {
    expect(await revenueTotal()).toBeCloseTo(35.5, 2);
  });

  it("renders invoice lines with customer names", async () => {
    expect(await invoiceLines()).toEqual(["Grace: $25.50", "Linus: $10.00"]);
  });
});
