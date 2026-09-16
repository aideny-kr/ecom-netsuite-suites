/**
 * Native accounting amendment preview. Never saves or submits a record.
 * @NApiVersion 2.1
 * @NScriptType CustomTool
 * @NModuleScope SameAccount
 */
define(['./ecom_accounting_amendment_core', 'N/runtime', 'N/log'], (core, runtime, log) => {
    const previewAccountingAmendment = args => {
        try {
            // This unsaved CustomTool has no script deployment parameters.
            // Its explicit field map is evidence only. The apply RESTlet never
            // accepts this override and always uses its installation profile.
            let profile;
            if (args.fieldMapJson !== undefined) {
                if (typeof args.fieldMapJson !== 'string' || args.fieldMapJson.length > 2000) {
                    throw new Error('bounded_field_map_required');
                }
                profile = {schema_version: 1, account_id: runtime.accountId, role_id: String(runtime.getCurrentUser().role),
                    subsidiary_id: args.subsidiaryId, tax_regime: 'legacy', fields: JSON.parse(args.fieldMapJson)};
            }
            const draft = core.prepare(args, profile);
            return {success: true, result: JSON.stringify(draft.receipt),
                remainingUsage: runtime.getCurrentScript().getRemainingUsage()};
        } catch (error) {
            log.error({title: 'Accounting preview failed', details: error.name || 'preview_error'});
            return {success: false, result: JSON.stringify({saved: false, financialWrites: 0,
                error: String(error.message || error).slice(0, 240)}),
                remainingUsage: runtime.getCurrentScript().getRemainingUsage()};
        }
    };
    return {previewAccountingAmendment};
});
