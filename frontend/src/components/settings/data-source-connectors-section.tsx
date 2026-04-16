"use client";

import { StripeConnectorCard } from "./stripe-connector-card";
import { NetSuiteDepositSyncCard } from "./netsuite-deposit-sync-card";
import { SheetsConnectorCard } from "./sheets-connector-card";
import { Database } from "lucide-react";

export function DataSourceConnectorsSection() {
  return (
    <div className="space-y-4">
      <div className="flex items-center gap-2">
        <Database className="h-5 w-5 text-muted-foreground" />
        <h2 className="text-lg font-semibold">Data Source Connectors</h2>
      </div>
      <p className="text-[13px] text-muted-foreground">
        Connect data sources for reconciliation and export. These connectors are separate from
        the AI/chat connectors above.
      </p>
      <div className="space-y-3">
        <StripeConnectorCard />
        <NetSuiteDepositSyncCard />
        <SheetsConnectorCard />
      </div>
    </div>
  );
}
