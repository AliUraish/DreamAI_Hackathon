import { complete } from "../lib/assistant";

/** A short, friendly status note for the customer. */
export async function orderStatusNote(customer: string, status: string): Promise<string> {
  const note = await complete(`Write one friendly sentence telling ${customer} their order is ${status}.`);
  return note.trim();
}
