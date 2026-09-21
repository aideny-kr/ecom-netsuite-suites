import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { AccountingConfirmationCard } from "../accounting-confirmation-card";
import { AccountingGroupCard } from "../accounting-group-card";
import type { WriteConfirmationData } from "@/lib/types";

const data = {
  type: "write_confirmation", mutation_type: "update", record_type: "creditmemo", record_id: "30",
  tool_name: "transaction_ops_accounting_amendment_apply", tool_input: {}, confirmation_token: "signed", status: "pending",
  target_account: "123456-sb1", target_environment: "SANDBOX", current_record: null, proposed_fields: { taxRate: "10" },
  accounting_review: {
    kind: "credit_tax_reallocation", order_reference: "R123", record_id: "30", case_id: "case", invoice_id: "20",
    source: {currency:"USD"}, scope: {netsuite_account_id:"123456-sb1",subsidiary_id:"1"},
    before: {total:"440",subtotal:"440",taxTotal:"0"}, expected_after:{total:"440",subtotal:"400",taxTotal:"40"},
    period:{id:"90",closed:false,arLocked:false,allLocked:false}, accounting_book:"1",ar_account:"11",
    sales_adjustment_account:"12",tax_account:"13",approval_basis:"Review the source tax allocation and retain the existing open period.",
    proposed_fields:{taxRate:"10",item:{items:[{line:7,amount:"400"}]}},
  },
} satisfies WriteConfirmationData;

describe("native accounting approval",()=>{
  it("shows exact audited lines and requires confirmation of the refund association",()=>{
    const approve=vi.fn();
    const audited={...data,accounting_review:{...data.accounting_review,refund_allocation:{
      status:"ready_for_finance_review" as const,source_refund_id:"500",payment_number:"PAY100",currency:"USD",
      net:"400",tax:"40",gross:"440",order_version_id:"1",
      authority:"Audit history has no explicit refund-to-line link. Finance must confirm the association.",
      lines:[{source_line_id:"101",sku:"MEM64",quantity:"1",price_before:"1600",price_after:"1200",
        tax_before:"160",tax_after:"120",net:"400",tax:"40",gross:"440",version_ids:["2","3"]}],
    }}};
    const {unmount}=render(<AccountingConfirmationCard data={audited} onConfirm={approve} onReject={vi.fn()}/>);
    expect(screen.getByRole("table",{name:"Refunded lines"})).toHaveTextContent("MEM64");
    expect(screen.getByText(/Solidus refund #500/)).toHaveTextContent("PAY100");
    expect(screen.getByText(/no explicit refund-to-line link/)).toBeInTheDocument();
    const button=screen.getByRole("button",{name:"Approve correction"});
    expect(button).toBeDisabled();
    fireEvent.click(screen.getByRole("checkbox",{name:/I confirm these line changes belong to this refund/}));
    fireEvent.click(button);
    expect(approve).toHaveBeenCalledTimes(1);
    unmount();
    const grouped={...data,accounting_review:null,accounting_group:{group_id:"group",concurrency:1,members:[{
      case_id:"case",order_reference:"R123",confirmation_id:"child",card:audited,
    }]}};
    render(<AccountingGroupCard data={grouped} onConfirm={approve} onReject={vi.fn()}/>);
    expect(screen.getByText(/Audit history does not contain an explicit refund-to-line link/)).toBeInTheDocument();
    const groupButton=screen.getByRole("button",{name:/Approve/});
    expect(groupButton).toBeDisabled();
    fireEvent.click(screen.getByRole("checkbox",{name:/I confirm the audited line changes belong to each displayed refund/}));
    fireEvent.click(groupButton);
    expect(approve).toHaveBeenCalledTimes(2);
  });
  it("shows credit allocation accurately and requires explicit accounting acknowledgement",()=>{
    const approve=vi.fn(); render(<AccountingConfirmationCard data={data} onConfirm={approve} onReject={vi.fn()}/>);
    expect(screen.getByRole("heading",{name:"Correct existing credit tax allocation"})).toBeInTheDocument();
    expect(screen.queryByText("Correct invoice tax")).not.toBeInTheDocument();
    expect(screen.getByText("Gross credit")).toBeInTheDocument();
    expect(screen.getByText("$40.00")).toBeInTheDocument();
    const button=screen.getByRole("button",{name:"Approve correction"}); expect(button).toBeDisabled();
    fireEvent.click(screen.getByRole("checkbox"));fireEvent.click(button);expect(approve).toHaveBeenCalledTimes(1);
  });
  it("does not claim verified on approval alone and reads actual native after amounts",()=>{
    const {rerender}=render(<AccountingConfirmationCard data={{...data,status:"approved"}} onConfirm={vi.fn()} onReject={vi.fn()}/>);
    expect(screen.getByRole("status")).toHaveTextContent("verification needed");
    rerender(<AccountingConfirmationCard data={{...data,status:"approved",accounting_verification:{status:"verified",after:{body:{subtotal:"400",taxtotal:"40",total:"440"},lines:[]}}}} onConfirm={vi.fn()} onReject={vi.fn()}/>);
    expect(screen.getByRole("status")).toHaveTextContent("Executed · verified");
    expect(screen.getByText("$40.00")).toBeInTheDocument();
  });
  it("renders the dependent non-posting sales order treatment distinctly",()=>{
    const order={...data,record_type:"salesorder",accounting_review:{...data.accounting_review!,kind:"sales_order_line_alignment" as const}};
    render(<AccountingConfirmationCard data={order} onConfirm={vi.fn()} onReject={vi.fn()}/>);
    expect(screen.getByRole("heading",{name:"Align sales-order lines and tax"})).toBeInTheDocument();
    expect(screen.getByText(/related credit correction has been verified/i)).toBeInTheDocument();
  });
  it("prevents approval with an unverified target and prevents child approval while grouped",()=>{
    const {rerender}=render(<AccountingConfirmationCard data={{...data,target_account:"another"}} onConfirm={vi.fn()} onReject={vi.fn()}/>);
    expect(screen.getByRole("button",{name:"Approve correction"})).toBeDisabled();
    rerender(<AccountingConfirmationCard data={data} onConfirm={vi.fn()} onReject={vi.fn()} readOnly groupState="executing"/>);
    expect(screen.getByRole("status")).toHaveTextContent("Awaiting result");
    expect(screen.queryByRole("button",{name:"Approve correction"})).not.toBeInTheDocument();
  });
});
