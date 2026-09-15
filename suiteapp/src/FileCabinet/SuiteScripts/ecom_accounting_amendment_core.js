/**
 * Native accounting draft preparation. Never saves or submits a record.
 * @NApiVersion 2.1
 * @NModuleScope SameAccount
 */
define(['N/record', 'N/runtime'], (record, runtime) => {
    const BODY = new Set(['taxitem', 'taxrate', 'istaxable', 'taxtotal']);
    const TYPES = new Set(['creditmemo', 'salesorder', 'invoice']);
    const AMOUNTS = ['subtotal', 'taxtotal', 'total'];
    const account = value => String(value).replace(/_/g, '-').toLowerCase();
    const id = value => typeof value === 'string' && /^[1-9][0-9]*$/.test(value);
    const fail = code => { throw new Error(code); };
    // Installation-owned configuration, never model/request-supplied write fields.
    // No customer mapping is a default for another account.
    const configuration = (previewProfile, subsidiaryId) => {
        const raw = previewProfile === undefined ?
            runtime.getCurrentScript().getParameter({name: 'custscript_ecom_acct_amend_profile'}) : JSON.stringify(previewProfile);
        if (typeof raw !== 'string' || raw.length > 8000) fail('native_profile_required');
        const configured = JSON.parse(raw);
        const profiles = Array.isArray(configured) ? configured : [configured];
        if (!profiles.length || profiles.length > 20) fail('native_profile_scope_mismatch');
        const matches = profiles.filter(p => p && (subsidiaryId === undefined || p.subsidiary_id === subsidiaryId));
        if (matches.length !== 1) fail('native_profile_scope_mismatch');
        const profile = matches[0];
        if (!profile || profile.schema_version !== 1 || profile.tax_regime !== 'legacy' ||
            account(profile.account_id) !== account(runtime.accountId) || !id(profile.subsidiary_id) ||
            !id(profile.role_id) || profile.role_id !== String(runtime.getCurrentUser().role)) fail('native_profile_scope_mismatch');
        const fields = profile.fields;
        const names = ['order_reference', 'source_line_id', 'original_sku', 'vat_amount'];
        if (!fields || Object.keys(fields).length !== names.length || names.some(name =>
            typeof fields[name] !== 'string' || !(name === 'order_reference' ?
                /^custbody_[a-z0-9_]{1,100}$/ : /^custcol_[a-z0-9_]{1,100}$/).test(fields[name])) ||
            new Set(Object.values(fields)).size !== names.length) fail('native_profile_fields_invalid');
        return {schema_version: 1, account_id: account(runtime.accountId), subsidiary_id: profile.subsidiary_id,
            role_id: profile.role_id, tax_regime: 'legacy', fields};
    };
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
    const cents = value => {
        const text = decimal(value);
        const [whole, fraction = ''] = text.replace(/^-/, '').split('.');
        if (fraction.length > 2) fail('monetary_precision_not_supported');
        const amount = Number(whole) * 100 + Number(fraction.padEnd(2, '0'));
        if (!Number.isSafeInteger(amount)) fail('monetary_range_not_supported');
        return text.startsWith('-') ? -amount : amount;
    };
    const amountIdentity = (before, after) => {
        // Shipping, handling and existing discounts are preserved, not assumed zero.
        // These two treatments may change only item subtotal and tax, in currencies
        // with at most two decimal places (also enforced by the intent builders).
        if (cents(after.total) - cents(before.total) !==
            cents(after.subtotal) - cents(before.subtotal) + cents(after.taxtotal) - cents(before.taxtotal)) {
            fail('native_amount_identity_mismatch');
        }
    };
    // Preserve native state needed by a subsequent conditional amendment.
    // Missing fields remain null; a receipt is never authority to assume zero.
    const protectedSnapshot = (rec, previewProfile) => {
        const fields = configuration(previewProfile, String(rec.getValue({fieldId: 'subsidiary'}))).fields;
        const scalar = value => {
            if (value === undefined || value === null || value === '') return null;
            if (Object.prototype.toString.call(value) === '[object Date]') return value.toISOString();
            if (typeof value === 'boolean') return value;
            if (typeof value !== 'string' && typeof value !== 'number') fail('unsupported_native_snapshot_value');
            if (typeof value === 'number' && !Number.isFinite(value)) fail('nonfinite_native_snapshot_value');
            if (String(value).length > 1000) fail('native_snapshot_value_limit');
            return String(value);
        };
        const bodyFields = ['lastmodifieddate', 'entity', 'subsidiary', 'currency', 'exchangerate',
            'account', 'postingperiod', 'createdfrom', 'status', 'trandate', 'department', 'class', 'location',
            'taxitem', 'taxrate', 'istaxable', 'subtotal', 'taxtotal', 'total', 'applied', 'unapplied',
            'discountitem', 'discountrate', 'shippingcost', 'handlingcost', 'shippingtax1rate',
            'shippingtaxcode', 'shipmethod', 'shipaddresslist', 'custbody_ecom_tx_ops_work_key', fields.order_reference];
        const lineFields = ['line', 'lineuniquekey', 'item', 'itemtype', 'quantity', 'quantityfulfilled',
            'quantitybilled', 'isclosed', 'rate', 'amount', 'istaxable', 'department', 'class', 'location',
            fields.source_line_id, fields.original_sku, fields.vat_amount];
        const count = rec.getLineCount({sublistId: 'item'});
        if (count > 500) fail('native_line_limit');
        return {body: Object.fromEntries(bodyFields.map(fieldId => [fieldId, scalar(rec.getValue({fieldId}))])),
            lines: Array.from({length: count}, (_, line) => Object.fromEntries(lineFields.map(fieldId =>
                [fieldId, scalar(rec.getSublistValue({sublistId: 'item', fieldId, line}))])))};
    };
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
    const preservation = (before, after, amendment, saved = false) => {
        const bodyAllowed = new Set(['subtotal', 'taxtotal', 'total', ...Object.keys(amendment.body || {})]);
        if (saved) { bodyAllowed.add('lastmodifieddate'); bodyAllowed.add('custbody_ecom_tx_ops_work_key'); }
        for (const key of Object.keys(before.body)) {
            if (!bodyAllowed.has(key) && before.body[key] !== after.body[key]) fail('protected_body_changed');
        }
        if (before.lines.length !== after.lines.length) fail('native_line_count_changed');
        const changes = new Map((amendment.lines || []).map(line =>
            [String(line.lineUniqueKey), new Set(Object.keys(line.fields))]));
        for (let i = 0; i < before.lines.length; i++) {
            const original = before.lines[i], current = after.lines[i];
            const allowed = changes.get(original.lineuniquekey) || new Set();
            for (const key of Object.keys(original)) {
                if (!allowed.has(key) && original[key] !== current[key]) fail('protected_line_changed');
            }
        }
    };
    const declaredValues = (snapshot, amendment) => {
        const agrees = (field, actual, expected) => field === 'istaxable' ? actual === expected :
            field === 'taxitem' ? actual === expected : decimal(actual) === decimal(expected);
        for (const [field, value] of Object.entries(amendment.body || {})) {
            if (!agrees(field, snapshot.body[field], value)) fail('declared_body_value_mismatch');
        }
        for (const change of amendment.lines || []) {
            const line = snapshot.lines.find(item => item.lineuniquekey === String(change.lineUniqueKey));
            if (!line || String(change.line) !== line.line) fail('native_line_identity_changed');
            for (const [field, value] of Object.entries(change.fields)) {
                if (!agrees(field, line[field], value)) fail('declared_line_value_mismatch');
            }
        }
    };
    const load = (args, previewProfile) => {
        const profile = configuration(previewProfile, args.subsidiaryId);
        if (args.subsidiaryId !== profile.subsidiary_id) fail('native_profile_subsidiary_mismatch');
        if (account(args.accountId) !== account(runtime.accountId)) fail('account_scope_mismatch');
        if (!TYPES.has(args.recordType) || !id(args.recordId) || !id(args.subsidiaryId) || !id(args.currencyId)) {
            fail('verified_record_scope_required');
        }
        if (runtime.getCurrentScript().getRemainingUsage() < 100) fail('insufficient_governance');
        const rec = record.load({type: args.recordType, id: args.recordId, isDynamic: true});
        if (String(rec.getValue({fieldId: 'subsidiary'})) !== args.subsidiaryId ||
            String(rec.getValue({fieldId: 'currency'})) !== args.currencyId) fail('record_scope_mismatch');
        return rec;
    };
    const prepare = (args, previewProfile) => {
            const profile = configuration(previewProfile, args.subsidiaryId);
            if (args.fieldMapJson !== undefined) {
                const requested = parse(args.fieldMapJson, 2000);
                if (Object.keys(requested).length !== Object.keys(profile.fields).length ||
                    Object.keys(profile.fields).some(key => requested[key] !== profile.fields[key])) fail('native_field_map_mismatch');
            }
            const LINE = new Set(['rate', 'amount', 'istaxable', profile.fields.vat_amount]);
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
            const rec = load(args, previewProfile);
            const before = readAmounts(rec);
            const beforeSnapshot = protectedSnapshot(rec, previewProfile);
            const count = rec.getLineCount({sublistId: 'item'});
            if (count > 500) fail('native_line_limit');
            const indices = new Map();
            for (let index = 0; index < count; index++) {
                const key = String(rec.getSublistValue({sublistId: 'item', fieldId: 'lineuniquekey', line: index}));
                if (indices.has(key)) fail('duplicate_native_line_identity');
                indices.set(key, index);
            }
            const taxOverride = Object.prototype.hasOwnProperty.call(body, 'taxtotal');
            // Dynamic sourcing must not depend on JSON serialization order.
            for (const field of ['istaxable', 'taxitem']) {
                if (Object.prototype.hasOwnProperty.call(body, field)) {
                    rec.setValue({fieldId: field, value: nativeValue(field, body[field])});
                }
            }
            for (const line of lines) {
                const index = indices.get(String(line.lineUniqueKey));
                if (index === undefined || String(rec.getSublistValue({sublistId: 'item', fieldId: 'line', line: index})) !== String(line.line)) {
                    fail('native_line_identity_changed');
                }
                rec.selectLine({sublistId: 'item', line: index});
                for (const field of ['istaxable', 'rate', 'amount', profile.fields.vat_amount]) {
                    if (Object.prototype.hasOwnProperty.call(line.fields, field)) {
                        rec.setCurrentSublistValue({sublistId: 'item', fieldId: field, value: nativeValue(field, line.fields[field])});
                    }
                }
                rec.commitLine({sublistId: 'item'});
            }
            if (Object.prototype.hasOwnProperty.call(body, 'taxrate')) {
                rec.setValue({fieldId: 'taxrate', value: nativeValue('taxrate', body.taxrate)});
            }
            if (taxOverride) rec.setValue({fieldId: 'taxtotal', value: nativeValue('taxtotal', body.taxtotal)});
            const after = readAmounts(rec);
            const afterSnapshot = protectedSnapshot(rec, previewProfile);
            preservation(beforeSnapshot, afterSnapshot, amendment);
            declaredValues(afterSnapshot, amendment);
            amountIdentity(before, after);
            const matches = AMOUNTS.every(field => after[field] === decimal(expected[field]));
            // A preview does not execute user events/workflows triggered on save,
            // establish legal tax authority, reserve a record or authorize posting.
            return {record: rec, receipt: {
                accountId: account(runtime.accountId), roleId: String(runtime.getCurrentUser().role),
                recordType: args.recordType, recordId: args.recordId,
                subsidiaryId: args.subsidiaryId, currencyId: args.currencyId,
                taxRegime: 'legacy', profile, amendment, before, after, expected, matches,
                beforeSnapshot, afterSnapshot,
                saved: false, financialWrites: 0, executionAuthorized: false,
                limitations: ['Save-time scripts and GL effects are not simulated.',
                    'Fresh preflight, exact approval and independent post-save verification remain required.'],
            }};
    };
    return {prepare, load, snapshot: protectedSnapshot, preservation, declaredValues, amountIdentity, decimal, configuration};
});
