const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');

function setup({suiteTax = false, wrongCurrency = false, recalculate = true} = {}) {
  const body = {subsidiary: '1', currency: wrongCurrency ? '2' : '1', subtotal: 440, taxtotal: 0, total: 440};
  const lines = [{line: '7', lineuniquekey: '1007', rate: 440, amount: 440, istaxable: false}];
  const recalc = () => {
    if (!recalculate) return;
    body.subtotal = lines.reduce((sum, line) => sum + line.amount, 0);
    body.taxtotal = body.istaxable ? Math.round(body.subtotal * (body.taxrate || 0)) / 100 : 0;
    body.total = body.subtotal + body.taxtotal;
  };
  const rec = {
    getValue: jest.fn(({fieldId}) => body[fieldId]),
    setValue: jest.fn(({fieldId, value}) => {
      body[fieldId] = value;
      if (fieldId === 'taxtotal') body.total = body.subtotal + body.taxtotal;
      else recalc();
    }),
    getLineCount: jest.fn(() => lines.length),
    getSublistValue: jest.fn(({fieldId,line}) => lines[line][fieldId]),
    selectLine: jest.fn(),
    setCurrentSublistValue: jest.fn(({fieldId,value}) => {lines[0][fieldId] = value;}),
    commitLine: jest.fn(recalc),
    save: jest.fn(() => {throw new Error('Saving is forbidden in preview');}),
  };
  const record = {load: jest.fn(() => rec), submitFields: jest.fn(), create: jest.fn(), delete: jest.fn()};
  const runtime = {accountId: 'TEST_SB1', isFeatureInEffect: () => suiteTax,
    getCurrentUser: () => ({role: '7'}), getCurrentScript: () => ({getRemainingUsage: () => 500})};
  let tool;
  vm.runInNewContext(fs.readFileSync(path.join(__dirname,'../src/FileCabinet/SuiteScripts/ecom_accounting_preview.js'),'utf8'), {
    define: (names,factory) => {tool = factory(record,runtime,{error: jest.fn()});},
  });
  return {tool, record, rec};
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
