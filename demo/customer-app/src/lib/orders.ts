// Client for the Acme Orders API (v1).
const BASE_URL = process.env.ORDERS_API_URL ?? "http://localhost:4010";

export interface Order {
  id: string;
  name: string;
  /** Order value in dollars, e.g. 19.99 */
  price: number;
  status: string;
}

export interface NewOrder {
  name: string;
  price: number;
}

export async function listOrders(): Promise<Order[]> {
  const res = await fetch(`${BASE_URL}/v1/orders`);
  if (!res.ok) throw new Error(`Orders API error: ${res.status}`);
  const body = await res.json();
  return body.orders as Order[];
}

export async function createOrder(input: NewOrder): Promise<Order> {
  const res = await fetch(`${BASE_URL}/v1/orders`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name: input.name, price: input.price }),
  });
  if (!res.ok) throw new Error(`Orders API error: ${res.status}`);
  return (await res.json()) as Order;
}
