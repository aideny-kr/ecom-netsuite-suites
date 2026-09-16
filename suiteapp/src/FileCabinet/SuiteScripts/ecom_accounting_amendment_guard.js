/**
 * Conditional accounting amendment transport. Disabled unless explicitly enabled.
 * Application callers must claim the exact human-approved operation before POST.
 * @NApiVersion 2.1
 * @NScriptType Restlet
 * @NModuleScope SameAccount
 */
define(['./ecom_accounting_amendment_core', 'N/record', 'N/runtime', 'N/log'], (core, record, runtime, log) => {
    const WORK_KEY = 'custbody_ecom_tx_ops_work_key';
    const fail = code => { throw new Error(code); };
    const canonical = value => {
        if (Array.isArray(value)) return '[' + value.map(canonical).join(',') + ']';
        if (value && typeof value === 'object') return '{' + Object.keys(value).sort().map(k =>
            JSON.stringify(k) + ':' + canonical(value[k])).join(',') + '}';
        return JSON.stringify(value);
    };
    const same = (left, right) => canonical(left) === canonical(right);
    const expiry = value => {
        const expires = typeof value === 'string' ? Date.parse(value) : NaN;
        if (!Number.isFinite(expires) || expires <= Date.now() || expires > Date.now() + 300000) fail('dispatch_expired');
    };
    const preservation = core.preservation;
    const openPeriod = snapshot => {
        const identifier = snapshot.body.postingperiod;
        if (!identifier || !/^[1-9][0-9]*$/.test(identifier)) fail('posting_period_unverified');
        const period = record.load({type: 'accountingperiod', id: identifier, isDynamic: false});
        if (['closed', 'arlocked', 'alllocked'].some(fieldId => period.getValue({fieldId}) !== false)) {
            fail('posting_period_not_open');
        }
    };
    const totalsAgree = (snapshot, expected) => ['subtotal', 'taxtotal', 'total'].every(key =>
        core.decimal(snapshot.body[key]) === core.decimal(expected[key]));
    const post = input => {
        let submitted = false, saved = false;
        try {
            if (!input || typeof input !== 'object' || JSON.stringify(input).length > 200000) fail('bounded_request_required');
            if (input.schema_version !== 1 || !['preview', 'apply'].includes(input.action)) fail('unsupported_action');
            const allowed = new Set(['schema_version', 'action', 'request', 'expected_before', 'work_key',
                'approval_expires_at', 'approval_audit_id']);
            if (Object.keys(input).some(key => !allowed.has(key))) fail('unsupported_request_field');
            if (input.action === 'preview') {
                return {success: true, schema_version: 1, ...core.prepare(input.request).receipt};
            }
            if (runtime.getCurrentScript().getParameter({name: 'custscript_ecom_acct_amend_enabled'}) !== true) {
                fail('accounting_amendment_disabled');
            }
            if (!['creditmemo', 'salesorder'].includes(input.request.recordType)) fail('unsupported_posting_treatment');
            if (typeof input.work_key !== 'string' || !/^[a-f0-9]{64}$/.test(input.work_key)) fail('operation_key_required');
            if (typeof input.approval_audit_id !== 'string' ||
                !/^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/.test(input.approval_audit_id)) {
                fail('approval_audit_required');
            }
            expiry(input.approval_expires_at);
            const draft = core.prepare(input.request), receipt = draft.receipt, rec = draft.record;
            const current = receipt.beforeSnapshot;
            const expected = JSON.parse(input.request.expectedJson);
            if (!rec.getFields().includes(WORK_KEY)) fail('operation_attribution_field_unavailable');
            if (current.body[WORK_KEY] === input.work_key) {
                // Read-only duplicate acknowledgement. Never invoke save again.
                return {success: true, schema_version: 1, status: 'prior_submission_requires_verification',
                    record_type: input.request.recordType, record_id: input.request.recordId, work_key: input.work_key,
                    financial_writes: 0, native_snapshot: current, retry_allowed: false,
                    amounts_match_requested: totalsAgree(current, expected),
                    note: 'Work-key presence is attribution, not proof that this requested amendment was applied.'};
            }
            if (!same(current, input.expected_before)) fail('approved_native_evidence_changed');
            if (!receipt.matches) fail('native_calculated_amounts_mismatch');
            preservation(current, receipt.afterSnapshot, receipt.amendment);
            if (input.request.recordType === 'creditmemo') {
                const before = current.body, after = receipt.afterSnapshot.body;
                if (core.decimal(before.total) !== core.decimal(after.total) ||
                    core.decimal(before.subtotal) !== core.decimal(before.total) ||
                    core.decimal(before.applied) !== core.decimal(before.total) ||
                    core.decimal(before.unapplied) !== '0' || core.decimal(before.taxtotal) !== '0' ||
                    before.istaxable !== false || after.istaxable !== true ||
                    Number(after.taxtotal) <= 0 || Number(after.subtotal) < 0 || current.lines.length !== 1 ||
                    current.lines[0].itemtype !== 'NonInvtPart' || core.decimal(current.lines[0].quantity) !== '1' ||
                    core.decimal(current.lines[0].amount) !== core.decimal(before.total)) {
                    fail('credit_reallocation_scope_changed');
                }
                openPeriod(current);
            }
            expiry(input.approval_expires_at);
            if (runtime.getCurrentScript().getRemainingUsage() < 100) fail('insufficient_governance');
            rec.setValue({fieldId: WORK_KEY, value: input.work_key});
            const staged = core.snapshot(rec);
            preservation(current, staged, receipt.amendment, true);
            core.declaredValues(staged, receipt.amendment);
            core.amountIdentity(current.body, staged.body);
            if (!totalsAgree(staged, expected)) fail('native_calculation_changed_before_save');
            submitted = true;
            const savedId = String(rec.save({enableSourcing: true, ignoreMandatoryFields: false}));
            saved = true;
            log.audit({title: 'Approved accounting amendment', details: {work_key: input.work_key,
                approval_audit_id: input.approval_audit_id, record_type: input.request.recordType, record_id: savedId}});
            if (savedId !== input.request.recordId) fail('saved_record_identity_mismatch');
            const after = core.snapshot(core.load(input.request));
            preservation(current, after, receipt.amendment, true);
            core.declaredValues(after, receipt.amendment);
            core.amountIdentity(current.body, after.body);
            const matches = totalsAgree(after, expected) && after.body[WORK_KEY] === input.work_key;
            return {success: true, schema_version: 1, status: matches ? 'posted_pending_independent_verification' : 'needs_review',
                record_type: input.request.recordType, record_id: savedId, work_key: input.work_key,
                financial_writes: 1, native_snapshot: after, retry_allowed: false,
                verification_required: ['GL allocation', 'unchanged related posting documents and applications',
                    'current source revision', 'dependent sales-order alignment and final reconciliation']};
        } catch (error) {
            log.error({title: 'Accounting amendment outcome', details: {submitted, error: error.name || 'amendment_error'}});
            return {success: false, schema_version: 1,
                status: saved ? 'posted_needs_review' : submitted ? 'outcome_unconfirmed' : 'not_submitted',
                financial_writes: saved ? 1 : submitted ? null : 0, retry_allowed: false,
                error: String(error.message || error).slice(0, 240)};
        }
    };
    const get = input => {
        try {
            if (input && input.action === 'capabilities' && String(input.schema_version) === '1') {
                if (Object.keys(input).some(key => !['action', 'schema_version', 'subsidiaryId'].includes(key))) fail('unsupported_request_field');
                const profile = core.configuration(undefined, input.subsidiaryId);
                return {success: true, schema_version: 1, profile, financial_writes: 0, execution_authorized: false,
                    apply_enabled: runtime.getCurrentScript().getParameter({name: 'custscript_ecom_acct_amend_enabled'}) === true,
                    treatments: ['credit_tax_reallocation', 'sales_order_line_alignment'],
                    suitetax: runtime.isFeatureInEffect({feature: 'SUITETAXENGINE'})};
            }
            if (!input || input.action !== 'snapshot' || String(input.schema_version) !== '1') fail('unsupported_action');
            const allowed = new Set(['action', 'schema_version', 'accountId', 'recordType', 'recordId', 'subsidiaryId', 'currencyId']);
            if (Object.keys(input).some(key => !allowed.has(key))) fail('unsupported_request_field');
            const snapshot = core.snapshot(core.load(input));
            return {success: true, schema_version: 1, record_type: input.recordType, record_id: input.recordId,
                profile: core.configuration(undefined, input.subsidiaryId), native_snapshot: snapshot, financial_writes: 0, execution_authorized: false};
        } catch (error) {
            return {success: false, schema_version: 1, financial_writes: 0, error: String(error.message || error).slice(0, 240)};
        }
    };
    return {get, post};
});
