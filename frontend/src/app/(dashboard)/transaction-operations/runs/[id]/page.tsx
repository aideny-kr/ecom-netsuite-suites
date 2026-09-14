import { TransactionRunPage } from "@/components/transaction-ops/run-page";
export default function Page({ params }: { params: { id: string } }) {
  return <TransactionRunPage id={params.id} />;
}
