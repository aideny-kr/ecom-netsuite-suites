/**
 * @NApiVersion 2.1
 * @NScriptType BundleInstallationScript
 * @NModuleScope SameAccount
 */
define(['N/log', 'N/runtime', 'N/email', 'N/record'], (log, runtime, email, record) => {

    const afterInstall = (params) => {
        log.audit('Bundle Install', `Ecom NetSuite Suite v${params.toversion} installed`);
        try {
            // Log the RESTlet deployment info for the tenant admin
            const user = runtime.getCurrentUser();
            log.audit('Bundle Install', `Installed by: ${user.name} (${user.email})`);
            log.audit('Bundle Install',
                'FileCabinet RESTlet is now available. ' +
                'Script: customscript_ecom_filecabinet_rl, Deploy: customdeploy_ecom_filecabinet_rl'
            );
        } catch (e) {
            log.error('Bundle Install Error', e.message);
        }
    };

    const beforeUpdate = (params) => {
        log.audit('Bundle Update', `Updating from v${params.fromversion} to v${params.toversion}`);
    };

    const afterUpdate = (params) => {
        log.audit('Bundle Updated', `Ecom NetSuite Suite updated to v${params.toversion}`);
    };

    const beforeUninstall = (params) => {
        log.audit('Bundle Uninstall', 'Ecom NetSuite Suite is being uninstalled');
    };

    return { afterInstall, beforeUpdate, afterUpdate, beforeUninstall };
});
