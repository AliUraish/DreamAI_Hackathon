import { listOrders } from "../lib/orders";
import { formatDollars } from "../utils/money";

/** Total revenue across all orders, in dollars. */
export async function revenueTotal(): Promise<number> {
  const orders = await listOrders();
  return orders.reduce((sum, order) => sum + order.price, 0);
}

export async function invoiceLines(): Promise<string[]> {
  const orders = await listOrders();
  return orders.map((order) => `${order.name}: ${formatDollars(order.price)}`);
}
