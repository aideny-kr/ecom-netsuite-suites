/** Screen-only refinements. Source-defined print layouts, chart geometry, table
 * emphasis and favorable/unfavorable colors remain owned by the frozen report. */
export const REPORT_FAMILY_CSS = `
@media screen {
  .orbital-report { --ia-muted:#536677; --ia-line-soft:#dce4eb; --ia-paper-2:#edf2f6; --fs-soft:#edf2f6; }
  .orbital-report :is(.chart,.tblcard,.ia-kpis .kpi,.fs-witem,.ia-chip,.fs-chip) { border:1px solid #d6dfe6; border-radius:10px; box-shadow:none; min-width:0; }
  .orbital-report :is(.chart,.tblcard) { padding:20px; margin:16px 0; }
  .orbital-report :is(.chart,.tblcard) h3 { font-size:15px; font-weight:600; line-height:1.5; margin-bottom:16px; }
  .orbital-report .ia-head { border-bottom:1px solid #d6dfe6; padding-bottom:20px; }
  .orbital-report :is(.ia-sub,.ia-meta,.fs-meta) { color:#536677; line-height:1.6; }
  .orbital-report .ia-section { margin:30px 0; }
  .orbital-report .ia-section h2 { font-size:18px; letter-spacing:-.01em; text-transform:none; font-weight:600; margin:0 0 14px; }
  .orbital-report :is(.ia-watch,.fs-watch) { gap:8px; }
  .orbital-report :is(.ia-chip,.fs-chip,.fs-witem) { padding:7px 11px; line-height:1.5; }
  .orbital-report :is(.ia-chip .dot,.fs-dot) { border-radius:50%; }
  .orbital-report :is(.ia-kpis,.fs-kpis) { grid-template-columns:repeat(2,minmax(0,1fr)); gap:14px; }
  .orbital-report :is(.ia-kpis .kpi,.fs-kpi) { padding:18px; margin:0; }
  .orbital-report :is(.ia-kpis .kpi .v,.fs-kpi-val) { font-size:27px; font-weight:650; line-height:1.3; overflow-wrap:anywhere; font-variant-numeric:tabular-nums; }
  .orbital-report :is(.ia-kpis .kpi .l,.fs-kpi-lbl) { color:#536677; font-size:11px; letter-spacing:.06em; }
  .orbital-report :is(.ia-kpis .kpi .s,.fs-kpi-sub) { color:#536677; line-height:1.6; }
  .orbital-report .fs-spark { position:static; display:block; margin-top:10px; }
  .orbital-report :is(.ia-mid,.fs-mid) { grid-template-columns:minmax(0,1fr); gap:8px; }
  .orbital-report :is(.ia-mid,.fs-mid) > * { min-width:0; }
  .orbital-chart-scroll { max-width:100%; overflow-x:auto; scrollbar-width:thin; }
  .orbital-chart-scroll > svg { display:block; width:100%; min-width:var(--plot-width); height:auto; max-height:400px; }
  .orbital-table-scroll { max-width:100%; overflow:auto; scrollbar-width:thin; }
  .orbital-report :is(.orbital-chart-scroll,.orbital-table-scroll,summary):focus-visible { outline:2px solid #537e9c; outline-offset:3px; }
  .orbital-report .tblcard :is(th,td) { padding:9px 10px; }
  .orbital-report .tblcard th { color:#536677; background:#edf2f6; border-bottom:1px solid #c7d4de; font-size:10px; line-height:1.5; white-space:normal; }
  .orbital-report .tblcard :is(td.lbl,td.desc,td:first-child) { white-space:normal; overflow-wrap:anywhere; }
  .orbital-report .tblcard .desc { min-width:180px; }
  .orbital-report :is(.fs-stmt,.fs-quad) :is(th,td) { padding:9px 10px; }
  .orbital-report .fs-stmt tr.fs-net td { background:#e6eff5; color:#233442; border-bottom:3px double #7a93a6; }
  .orbital-report .fs-stmt tr.fs-net td.fs-good { color:var(--fs-good); }
  .orbital-report .fs-stmt tr.fs-net td.fs-bad { color:var(--fs-bad); }
  .orbital-report .fs-chip.fs-good { border-color:var(--fs-good); }
  .orbital-report .fs-chip.fs-bad { border-color:var(--fs-bad); }
  .orbital-report .ia-section summary { cursor:pointer; padding:12px; border:1px solid #d6dfe6; border-radius:7px; background:#f4f7fa; list-style:revert; }
  .orbital-report .ia-section summary::before { content:none; }
  .orbital-report :is(.ia-section summary, .fs-sec-lbl):hover { background:#e6eff5; }
  .orbital-report .narr { border-left:3px solid #8eb1c8; padding-left:18px; }
  .orbital-report :is(.ia-prov,.prov) { color:#536677; overflow-wrap:anywhere; }
  .orbital-report .hl { grid-template-columns:repeat(2,minmax(0,1fr)); }
  .orbital-scroll-hint { display:none; font-size:11px; color:#536677; margin:10px 0 0; }
  @media(min-width:1100px) { .orbital-report :is(.ia-kpis,.fs-kpis) { grid-template-columns:repeat(4,minmax(0,1fr)); } }
  @media(max-width:900px) { .orbital-scroll-hint { display:block; } }
  @media(max-width:640px) {
    .orbital-report :is(.ia-kpis,.fs-kpis,.hl) { grid-template-columns:1fr; }
    .orbital-report :is(.chart,.tblcard,.ia-kpis .kpi,.fs-kpi) { padding:16px; }
    .orbital-report .ia-head { grid-template-columns:1fr; }
    .orbital-report :is(.ia-chip,.fs-witem) { max-width:100%; overflow-wrap:anywhere; }
    .orbital-report .fs-watch { flex-wrap:wrap; }
  }
}
@media print { .orbital-scroll-hint { display:none; } }
`;
