const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');

function setup({suiteTax = false, wrongCurrency = false, recalculate = true,
  allowApply = false, periodOpen = true, saveMode = 'forbidden', workField = true,
  onCommit = () => {}, onStamp = () => {}, onSave = () => {}} = {}) {
  const workKey = 'custbody_ecom_tx_ops_work_key';
  let storedBody = {subsidiary: '1', currency: wrongCurrency ? '2' : '1', entity: '8', account: '11',
    postingperiod: '90', exchangerate: 1, lastmodifieddate: '2026-01-01T00:00:00Z',
    subtotal: 440, taxtotal: 0, total: 440, applied: 440, unapplied: 0, istaxable: false, taxrate: 0};
  if (workField) storedBody[workKey] = null;
  let storedLines = [{line: '7', lineuniquekey: '1007', item: '4', itemtype: 'NonInvtPart',
    quantity: 1, rate: 440, amount: 440, istaxable: false}];
  const allSaves = jest.fn();
  function draft() {
    const body = {...storedBody}, lines = storedLines.map(line => ({...line}));
    const recalc = () => {
      if (!recalculate) return;
      body.subtotal = lines.reduce((sum, line) => sum + line.amount, 0);
      body.taxtotal = body.istaxable ? Math.round(body.subtotal * (body.taxrate || 0)) / 100 : 0;
      body.total = body.subtotal + body.taxtotal;
    };
    return {
      getValue: jest.fn(({fieldId}) => body[fieldId]),
      getFields: jest.fn(() => Object.keys(body)),
      setValue: jest.fn(({fieldId, value}) => {
        body[fieldId] = value;
        if (fieldId === workKey) onStamp(body, lines);
        if (fieldId === 'taxtotal') body.total = body.subtotal + body.taxtotal;
        else if (['taxrate','taxitem','istaxable'].includes(fieldId)) recalc();
      }),
      getLineCount: jest.fn(() => lines.length),
      getSublistValue: jest.fn(({fieldId,line}) => lines[line][fieldId]),
      selectLine: jest.fn(),
      setCurrentSublistValue: jest.fn(({fieldId,value}) => {lines[0][fieldId] = value;}),
      commitLine: jest.fn(() => { recalc(); onCommit(body, lines); }),
      save: jest.fn(() => {
        allSaves();
        if (saveMode === 'forbidden') throw new Error('Saving is forbidden in preview');
        storedBody = {...body, lastmodifieddate: '2026-01-01T00:01:00Z'};
        storedLines = lines.map(line => ({...line}));
        onSave(storedBody, storedLines);
        if (saveMode === 'uncertain') throw new Error('Response lost after native save');
        return '40';
      }),
    };
  }
  const rec = draft(); let loaded = false;
  const record = {load: jest.fn(({type}) => {
    if (type === 'accountingperiod') return {getValue: () => !periodOpen};
    if (!loaded) { loaded = true; return rec; }
    return draft();
  }), submitFields: jest.fn(), create: jest.fn(), delete: jest.fn()};
  const runtime = {accountId: 'TEST_SB1', isFeatureInEffect: () => suiteTax,
    getCurrentUser: () => ({role: '7'}), getCurrentScript: () => ({getRemainingUsage: () => 500,
      getParameter: () => allowApply})};
  const logger = {error: jest.fn(), audit: jest.fn()};
  let core, tool, guard;
  const scripts = path.join(__dirname,'../src/FileCabinet/SuiteScripts');
  vm.runInNewContext(fs.readFileSync(path.join(scripts,'ecom_accounting_amendment_core.js'),'utf8'), {
    define: (names,factory) => {core = factory(record,runtime,logger);},
  });
  vm.runInNewContext(fs.readFileSync(path.join(scripts,'ecom_accounting_preview.js'),'utf8'), {
    define: (names,factory) => {tool = factory(core,runtime,logger);},
  });
  vm.runInNewContext(fs.readFileSync(path.join(scripts,'ecom_accounting_amendment_guard.js'),'utf8'), {
    define: (names,factory) => {guard = factory(core,record,runtime,logger);},
  });
  return {tool, core, guard, record, rec, allSaves,
    mutate: (body, line = {}) => {Object.assign(storedBody, body); Object.assign(storedLines[0], line);}};
}
const request = () => ({accountId: 'test-sb1',recordType: 'creditmemo',recordId: '40',subsidiaryId: '1',currencyId: '1',
  amendmentJson: JSON.stringify({body: {taxitem: '60',taxrate: '10',istaxable: true},
    lines: [{line: '7',lineUniqueKey: '1007',fields: {rate: '400',amount: '400',istaxable: true}}]}),
  expectedJson: JSON.stringify({subtotal: '400.00',taxtotal: '40',total: '440'})});

test('native preview compares calculated amounts and never saves', () => {
  const {tool,rec,record} = setup();
  const result = tool.previewAccountingAmendment(request());
  expect(result.success).toBe(true);
  expect(JSON.parse(result.result)).toMatchObject({matches: true, saved: false,financialWrites: 0,executionAuthorized: false});
  expect(JSON.parse(result.result).beforeSnapshot.lines[0]).toMatchObject({line:'7',lineuniquekey:'1007',amount:'440'});
  expect(JSON.parse(result.result).afterSnapshot.lines[0]).toMatchObject({line:'7',lineuniquekey:'1007',amount:'400'});
  expect(rec.save).not.toHaveBeenCalled();
  expect(record.submitFields).not.toHaveBeenCalled();
  expect(record.create).not.toHaveBeenCalled();
});
test('failed native calculation is reported without a guessed match', () => {
  const {tool,rec} = setup({recalculate:false});
  expect(JSON.parse(tool.previewAccountingAmendment(request()).result).matches).toBe(false);
  expect(rec.save).not.toHaveBeenCalled();
});
test.each(['account','type','body','line','duplicate','suiteTax','currency'])(
  'rejects %s scope or unsupported operation without saving', scenario => {
    const {tool,rec} = setup({suiteTax:scenario==='suiteTax',wrongCurrency:scenario==='currency'});
    const args=request(); const amendment=JSON.parse(args.amendmentJson);
    if (scenario==='account') args.accountId='another-account';
    if (scenario==='type') args.recordType='role';
    if (scenario==='body') amendment.body.account='999';
    if (scenario==='line') amendment.lines[0].lineUniqueKey='999';
    if (scenario==='duplicate') amendment.lines.push(amendment.lines[0]);
    args.amendmentJson=JSON.stringify(amendment);
    expect(tool.previewAccountingAmendment(args).success).toBe(false);
    expect(rec.save).not.toHaveBeenCalled();
  });
test('tax-only preview uses native amount override rather than dividing by zero',()=>{
  const {tool,rec}=setup(); const args=request();
  args.amendmentJson=JSON.stringify({body:{taxitem:'60',istaxable:true,taxtotal:'440'},
    lines:[{line:'7',lineUniqueKey:'1007',fields:{rate:'0',amount:'0',istaxable:true}}]});
  args.expectedJson=JSON.stringify({subtotal:'0',taxtotal:'440',total:'440'});
  const output=JSON.parse(tool.previewAccountingAmendment(args).result);
  expect(output.matches).toBe(true); expect(output.saved).toBe(false);
  expect(rec.setValue).not.toHaveBeenCalledWith(expect.objectContaining({fieldId:'taxrate'}));
  expect(rec.save).not.toHaveBeenCalled();
});

function approval(guard, args = request()) {
  const preview = guard.post({schema_version: 1, action: 'preview', request: args});
  expect(preview.success).toBe(true);
  return {schema_version: 1, action: 'apply', request: args, expected_before: preview.beforeSnapshot,
    work_key: 'a'.repeat(64), approval_audit_id: 'aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee',
    approval_expires_at: new Date(Date.now() + 120000).toISOString()};
}

test('native application is disabled by default while preview stays read-only', () => {
  const {guard, allSaves} = setup();
  expect(guard.post(approval(guard))).toMatchObject({success: false, status: 'not_submitted', financial_writes: 0,
    error: 'accounting_amendment_disabled'});
  expect(allSaves).not.toHaveBeenCalled();
});

test.each(['snapshot', 'closed period', 'expired', 'no audit', 'no key', 'no attribution', 'invoice'])(
  'refuses %s before any financial write', scenario => {
    const {guard, allSaves} = setup({allowApply: true, periodOpen: scenario !== 'closed period',
      workField: scenario !== 'no attribution', saveMode: 'ok'});
    const input = approval(guard);
    if (scenario === 'snapshot') input.expected_before.body.entity = '999';
    if (scenario === 'expired') input.approval_expires_at = new Date(Date.now() - 1000).toISOString();
    if (scenario === 'no audit') delete input.approval_audit_id;
    if (scenario === 'no key') delete input.work_key;
    if (scenario === 'invoice') input.request.recordType = 'invoice';
    expect(guard.post(input)).toMatchObject({success: false, status: 'not_submitted', financial_writes: 0});
    expect(allSaves).not.toHaveBeenCalled();
  });

test('one save preserves gross and records attribution; duplicate only reads', () => {
  const {guard, allSaves} = setup({allowApply: true, saveMode: 'ok'});
  const input = approval(guard);
  expect(guard.post(input)).toMatchObject({success: true, status: 'posted_pending_independent_verification',
    financial_writes: 1, retry_allowed: false, native_snapshot: {body: {total: '440', subtotal: '400',
      taxtotal: '40', applied: '440', unapplied: '0', custbody_ecom_tx_ops_work_key: input.work_key}}});
  expect(guard.post(input)).toMatchObject({success: true, status: 'prior_submission_requires_verification', financial_writes: 0});
  expect(allSaves).toHaveBeenCalledTimes(1);
});

test('tax-only credit amendment preserves gross without a fictional rate', () => {
  const {guard, allSaves} = setup({allowApply: true, saveMode: 'ok'});
  const args = request();
  args.amendmentJson = JSON.stringify({body:{taxitem:'60',istaxable:true,taxtotal:'440'},
    lines:[{line:'7',lineUniqueKey:'1007',fields:{rate:'0',amount:'0',istaxable:true}}]});
  args.expectedJson = JSON.stringify({subtotal:'0',taxtotal:'440',total:'440'});
  expect(guard.post(approval(guard, args))).toMatchObject({success:true,financial_writes:1,
    native_snapshot:{body:{total:'440',taxtotal:'440',subtotal:'0'}}});
  expect(allSaves).toHaveBeenCalledTimes(1);
});

test('lost response stays unknown and recovery reads attribution without resubmission', () => {
  const {guard, allSaves} = setup({allowApply: true, saveMode: 'uncertain'});
  const input = approval(guard);
  expect(guard.post(input)).toMatchObject({success: false, status: 'outcome_unconfirmed',
    financial_writes: null, retry_allowed: false});
  const {accountId,recordType,recordId,subsidiaryId,currencyId} = input.request;
  expect(guard.get({action:'snapshot',schema_version:'1',accountId,recordType,recordId,subsidiaryId,currencyId}))
    .toMatchObject({success:true,financial_writes:0,execution_authorized:false,
      native_snapshot:{body:{total:'440',subtotal:'400',taxtotal:'40',custbody_ecom_tx_ops_work_key:input.work_key}}});
  expect(guard.post(input)).toMatchObject({status:'prior_submission_requires_verification',financial_writes:0});
  expect(allSaves).toHaveBeenCalledTimes(1);
});

test.each(['quantity','rate'])(
  'preview rejects native sourcing that silently changes %s', field => {
    const {tool, allSaves} = setup({onCommit: (body,lines) => {lines[0][field] = 777;}});
    expect(tool.previewAccountingAmendment(request()).success).toBe(false);
    expect(allSaves).not.toHaveBeenCalled();
  });

test('attribution-triggered recalculation is caught before save', () => {
  const {guard,allSaves} = setup({allowApply:true,saveMode:'ok',onStamp: body => {body.taxtotal=0;body.total=400;}});
  expect(guard.post(approval(guard))).toMatchObject({success:false,status:'not_submitted',financial_writes:0});
  expect(allSaves).not.toHaveBeenCalled();
});

test('known save with changed fields reports one write and needs review', () => {
  const {guard,allSaves} = setup({allowApply:true,saveMode:'ok',onSave: (body,lines) => {lines[0].quantity=2;}});
  expect(guard.post(approval(guard))).toMatchObject({success:false,status:'posted_needs_review',financial_writes:1});
  expect(allSaves).toHaveBeenCalledTimes(1);
});

test('fresh native changes invalidate approval even if the totals still agree', () => {
  const {guard,mutate,allSaves} = setup({allowApply:true,saveMode:'ok'});
  const input = approval(guard);
  mutate({entity:'99'});
  expect(guard.post(input)).toMatchObject({success:false,error:'approved_native_evidence_changed',financial_writes:0});
  expect(allSaves).not.toHaveBeenCalled();
});

test('sales-order amendment preserves billing and fulfillment quantities', () => {
  const {guard,mutate,allSaves} = setup({allowApply:true,saveMode:'ok'});
  const args = request(); args.recordType = 'salesorder';
  // Consume the initial fixture object, then set real existing native quantities.
  guard.post({schema_version:1,action:'preview',request:args});
  mutate({}, {quantitybilled:1, quantityfulfilled:1, custcol_fw_vat_amount:0});
  const input = approval(guard,args);
  expect(guard.post(input)).toMatchObject({success:true,financial_writes:1,
    native_snapshot:{lines:[{quantity:'1',quantitybilled:'1',quantityfulfilled:'1'}]}});
  expect(allSaves).toHaveBeenCalledTimes(1);
});

test.each(['foreign account','foreign currency','write action','extra field'])(
  'recovery refuses %s and cannot execute an amendment', scenario => {
    const {guard,allSaves} = setup({allowApply:true,saveMode:'ok'});
    const {accountId,recordType,recordId,subsidiaryId,currencyId} = request();
    const input = {schema_version:'1',action:'snapshot',accountId,recordType,recordId,subsidiaryId,currencyId};
    if (scenario === 'foreign account') input.accountId='another-account';
    if (scenario === 'foreign currency') input.currencyId='2';
    if (scenario === 'write action') input.action='apply';
    if (scenario === 'extra field') input.work_key='a'.repeat(64);
    expect(guard.get(input)).toMatchObject({success:false,financial_writes:0});
    expect(allSaves).not.toHaveBeenCalled();
  });


test('matching expected amounts cannot conceal an inconsistent native total', () => {
  const {tool,allSaves} = setup({recalculate:false,onCommit: body => {
    Object.assign(body,{subtotal:400,taxtotal:40,total:430});
  }});
  const args = request(); args.expectedJson = JSON.stringify({subtotal:'400',taxtotal:'40',total:'430'});
  expect(tool.previewAccountingAmendment(args)).toMatchObject({success:false});
  expect(allSaves).not.toHaveBeenCalled();
});

test('amount identity preserves existing shipping and rejects unsupported fractional pennies', () => {
  const {core} = setup();
  expect(() => core.amountIdentity({subtotal:'440',taxtotal:'0',total:'449'},
    {subtotal:'400',taxtotal:'40',total:'449'})).not.toThrow();
  expect(() => core.amountIdentity({subtotal:'440',taxtotal:'0',total:'449'},
    {subtotal:'400',taxtotal:'40',total:'450'})).toThrow('native_amount_identity_mismatch');
  expect(() => core.amountIdentity({subtotal:'440',taxtotal:'0',total:'440'},
    {subtotal:'400',taxtotal:'40.001',total:'440.001'})).toThrow('monetary_precision_not_supported');
});

test('native field application order is explicit even when JSON keys arrive reversed', () => {
  const {tool,rec} = setup();
  const args = request();
  args.amendmentJson = JSON.stringify({body:{taxrate:'10',taxitem:'60',istaxable:true},
    lines:[{line:'7',lineUniqueKey:'1007',fields:{amount:'400',rate:'400',istaxable:true}}]});
  expect(tool.previewAccountingAmendment(args).success).toBe(true);
  expect(rec.setValue.mock.calls.map(([arg]) => arg.fieldId)).toEqual(['istaxable','taxitem','taxrate']);
  expect(rec.setCurrentSublistValue.mock.calls.map(([arg]) => arg.fieldId)).toEqual(['istaxable','rate','amount']);
  expect(rec.setValue.mock.invocationCallOrder[2]).toBeGreaterThan(rec.commitLine.mock.invocationCallOrder[0]);
});

test('reused work key never proves a different amendment was applied', () => {
  const {guard,allSaves} = setup({allowApply:true,saveMode:'ok'});
  const input = approval(guard);
  expect(guard.post(input).financial_writes).toBe(1);
  const amendment = JSON.parse(input.request.amendmentJson);
  amendment.lines[0].fields.custcol_fw_vat_amount='99';
  input.request.amendmentJson=JSON.stringify(amendment);
  expect(guard.post(input)).toMatchObject({status:'prior_submission_requires_verification',
    amounts_match_requested:true,financial_writes:0,retry_allowed:false,
    native_snapshot:{lines:[{custcol_fw_vat_amount:null}]}});
  expect(allSaves).toHaveBeenCalledTimes(1);
});
