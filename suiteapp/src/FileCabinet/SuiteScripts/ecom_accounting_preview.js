/**
 * Native accounting amendment preview. Never saves or submits a record.
 * @NApiVersion 2.1
 * @NScriptType CustomTool
 * @NModuleScope SameAccount
 */
define(['N/record', 'N/runtime', 'N/log'], (record, runtime, log) => {
    const BODY = new Set(['taxitem', 'taxrate', 'istaxable', 'taxtotal']);
    const LINE = new Set(['rate', 'amount', 'istaxable', 'custcol_fw_vat_amount']);
    const TYPES = new Set(['creditmemo', 'salesorder', 'invoice']);
    const AMOUNTS = ['subtotal', 'taxtotal', 'total'];
    const account = value => String(value).replace(/_/g, '-').toLowerCase();
    const id = value => typeof value === 'string' && /^[1-9][0-9]*$/.test(value);
    const fail = code => { throw new Error(code); };
    const decimal = value => {
        if (typeof value !== 'string' && typeof value !== 'number') fail('decimal_required');
        const text = String(value);
        if (!/^-?\d{1,12}(\.\d{1,7})?$/.test(text)) fail('bounded_decimal_required');
        const normalized = text.replace(/(\.\d*?)0+$/, '$1').replace(/\.$/, '');
        return Number(normalized) === 0 ? '0' : normalized;
    };
    const nativeValue = (field, value) => {
        if (field === 'istaxable') {
            if (typeof value !== 'boolean') fail('boolean_required');
            return value;
        }
        if (field === 'taxitem') {
            if (!id(value)) fail('tax_item_id_required');
            return value;
        }
        return Number(decimal(value));
    };
    const readAmounts = rec => Object.fromEntries(AMOUNTS.map(field => [field, decimal(rec.getValue({fieldId: field}))]));
    const parse = (text, max) => {
        if (typeof text !== 'string' || text.length > max) fail('bounded_json_required');
        const value = JSON.parse(text);
        if (!value || Array.isArray(value) || typeof value !== 'object') fail('object_required');
        return value;
    };
    const keys = (value, allowed) => {
        if (!value || Array.isArray(value) || typeof value !== 'object' ||
            Object.keys(value).some(key => !allowed.has(key))) fail('unsupported_amendment_field');
    };
    const previewAccountingAmendment = args => {
        try {
            if (account(args.accountId) !== account(runtime.accountId)) fail('account_scope_mismatch');
            if (!TYPES.has(args.recordType) || !id(args.recordId) || !id(args.subsidiaryId) || !id(args.currencyId)) {
                fail('verified_record_scope_required');
            }
            if (runtime.isFeatureInEffect({feature: 'SUITETAXENGINE'})) fail('suitetax_preview_not_implemented');
            const amendment = parse(args.amendmentJson, 32000);
            const expected = parse(args.expectedJson, 2000);
            keys(amendment, new Set(['body', 'lines']));
            keys(expected, new Set(AMOUNTS));
            if (AMOUNTS.some(field => expected[field] === undefined)) fail('complete_expected_amounts_required');
            const body = amendment.body || {};
            const lines = amendment.lines || [];
            keys(body, BODY);
            if (!Array.isArray(lines) || lines.length > 30 || (!lines.length && !Object.keys(body).length)) {
                fail('bounded_amendment_required');
            }
            // Validate every value before touching even an unsaved in-memory record.
            for (const [field, value] of Object.entries(body)) nativeValue(field, value);
            const seen = new Set();
            for (const line of lines) {
                keys(line, new Set(['line', 'lineUniqueKey', 'fields']));
                if (!id(String(line.line)) || !id(String(line.lineUniqueKey)) || seen.has(String(line.lineUniqueKey))) {
                    fail('unique_native_line_identity_required');
                }
                seen.add(String(line.lineUniqueKey));
                keys(line.fields, LINE);
                if (!Object.keys(line.fields).length) fail('line_changes_required');
                for (const [field, value] of Object.entries(line.fields)) nativeValue(field, value);
            }
            if (runtime.getCurrentScript().getRemainingUsage() < 100) fail('insufficient_governance');
            const rec = record.load({type: args.recordType, id: args.recordId, isDynamic: true});
            if (String(rec.getValue({fieldId: 'subsidiary'})) !== args.subsidiaryId ||
                String(rec.getValue({fieldId: 'currency'})) !== args.currencyId) fail('record_scope_mismatch');
            const before = readAmounts(rec);
            const count = rec.getLineCount({sublistId: 'item'});
            if (count > 500) fail('native_line_limit');
            const indices = new Map();
            for (let index = 0; index < count; index++) {
                const key = String(rec.getSublistValue({sublistId: 'item', fieldId: 'lineuniquekey', line: index}));
                if (indices.has(key)) fail('duplicate_native_line_identity');
                indices.set(key, index);
            }
            const taxOverride = Object.prototype.hasOwnProperty.call(body, 'taxtotal');
            for (const [field, value] of Object.entries(body)) {
                if (field !== 'taxtotal') rec.setValue({fieldId: field, value: nativeValue(field, value)});
            }
            for (const line of lines) {
                const index = indices.get(String(line.lineUniqueKey));
                if (index === undefined || String(rec.getSublistValue({sublistId: 'item', fieldId: 'line', line: index})) !== String(line.line)) {
                    fail('native_line_identity_changed');
                }
                rec.selectLine({sublistId: 'item', line: index});
                for (const [field, value] of Object.entries(line.fields)) {
                    rec.setCurrentSublistValue({sublistId: 'item', fieldId: field, value: nativeValue(field, value)});
                }
                rec.commitLine({sublistId: 'item'});
            }
            if (taxOverride) rec.setValue({fieldId: 'taxtotal', value: nativeValue('taxtotal', body.taxtotal)});
            const after = readAmounts(rec);
            const matches = AMOUNTS.every(field => after[field] === decimal(expected[field]));
            // A preview does not execute user events/workflows triggered on save,
            // establish legal tax authority, reserve a record or authorize posting.
            return {success: true, result: JSON.stringify({
                accountId: account(runtime.accountId), roleId: String(runtime.getCurrentUser().role),
                recordType: args.recordType, recordId: args.recordId,
                subsidiaryId: args.subsidiaryId, currencyId: args.currencyId,
                taxRegime: 'legacy', amendment, before, after, expected, matches,
                saved: false, financialWrites: 0, executionAuthorized: false,
                limitations: ['Save-time scripts and GL effects are not simulated.',
                    'Fresh preflight, exact approval and independent post-save verification remain required.'],
            }), remainingUsage: runtime.getCurrentScript().getRemainingUsage()};
        } catch (error) {
            log.error({title: 'Accounting preview failed', details: error.name || 'preview_error'});
            return {success: false, result: JSON.stringify({saved: false, financialWrites: 0,
                error: String(error.message || error).slice(0, 240)}),
                remainingUsage: runtime.getCurrentScript().getRemainingUsage()};
        }
    };
    return {previewAccountingAmendment};
});
