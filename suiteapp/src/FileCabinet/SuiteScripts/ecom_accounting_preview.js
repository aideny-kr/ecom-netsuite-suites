/**
 * Native accounting amendment preview. Never saves or submits a record.
 * @NApiVersion 2.1
 * @NScriptType CustomTool
 * @NModuleScope SameAccount
 */
define(['./ecom_accounting_amendment_core', 'N/runtime', 'N/log'], (core, runtime, log) => {
    const previewAccountingAmendment = args => {
        try {
            const draft = core.prepare(args);
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
