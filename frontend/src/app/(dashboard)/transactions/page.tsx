import { redirect } from "next/navigation";

export default function TransactionsPage() {
  redirect("/tables/orders?view=records");
}
