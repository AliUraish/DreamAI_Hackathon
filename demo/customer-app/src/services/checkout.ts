import { createOrder } from "../lib/orders";
import { formatDollars } from "../utils/money";

/** Places an order and returns the receipt line shown to the customer. */
export async function placeOrder(customer: string, priceDollars: number): Promise<string> {
  const order = await createOrder({ name: customer, price: priceDollars });
  return `${order.name} - ${formatDollars(order.price)} (${order.status})`;
}
