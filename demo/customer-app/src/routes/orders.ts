import { placeOrder } from "../services/checkout";
import { invoiceLines, revenueTotal } from "../services/billing";

export async function postOrder(body: { customer: string; price: number }) {
  return { receipt: await placeOrder(body.customer, body.price) };
}

export async function getInvoice() {
  return { lines: await invoiceLines(), revenue: await revenueTotal() };
}
