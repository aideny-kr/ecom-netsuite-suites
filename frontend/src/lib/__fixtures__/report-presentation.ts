/** Synthetic labels/values only. Mirrors the generic frozen bar-chart markup. */
export const reportReadingFixture = `<!doctype html><html lang="en"><head><title>Report layout fixture</title><style>
* { box-sizing:border-box; } body { margin:0; font-family:system-ui,sans-serif; line-height:1.5; }
.metric { display:flex; flex-direction:column; gap:4px; } table { width:100%; border-collapse:collapse; } th { text-align:left; }
</style></head><body>
<div class="report"><h1>Report layout fixture</h1>
<div class="nb-card metric"><span class="label">Missing source value</span><span class="value"> <small></small></span></div>
<div class="nb-card metric"><span class="label">Existing value</span><span class="value">12,345.67</span></div>
<div class="nb-card svg-wrap"><svg viewBox="0 0 720 380" width="720" height="380">
<rect x="2" y="2" width="716" height="376"/><text x="20" y="30">Selected drivers</text>
<g class="ser-0"><title>10001 - A very long receivables category with an unabridged name — amount: -1,234.567891</title><rect/><rect/></g>
<text>10001 - A very…<title>10001 - A very long receivables category with an unabridged name</title></text>
<g class="ser-0"><title>10002 - Inventory &amp; equipment — amount: 2,500</title><rect/><rect/></g>
<g class="ser-0"><title>10003 - Zero balance — amount: 0</title><rect/><rect/></g>
</svg></div><div class="nb-card"><p>The complete original narrative stays here.</p><table><tbody><tr><td>Evidence</td><td>12,345.67</td></tr></tbody></table></div>
</div></body></html>`;
