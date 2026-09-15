// Build a local, disabled installation artifact. This script never contacts NetSuite.
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'suitestudio-accounting-amendment-'));
try {
    for (const relative of [
        'Objects/customscript_ecom_acct_amend.xml',
        'Objects/custbody_ecom_tx_ops_work_key.xml',
        'FileCabinet/SuiteScripts/ecom_accounting_amendment_core.js',
        'FileCabinet/SuiteScripts/ecom_accounting_amendment_guard.js',
    ]) {
        const target = path.join(directory, relative);
        fs.mkdirSync(path.dirname(target), {recursive: true});
        fs.copyFileSync(path.join(__dirname, '../src', relative), target);
    }
    fs.writeFileSync(path.join(directory, 'manifest.xml'), `<?xml version="1.0" encoding="UTF-8"?>
<manifest projecttype="ACCOUNTCUSTOMIZATION">
    <projectname>Suite Studio Accounting Amendments</projectname>
    <frameworkversion>1.0</frameworkversion>
    <dependencies><features><feature required="true">SERVERSIDESCRIPTING</feature></features></dependencies>
</manifest>
`);
    fs.writeFileSync(path.join(directory, 'deploy.xml'), `<?xml version="1.0" encoding="UTF-8"?>
<deploy>
    <objects><path>~/Objects/*</path></objects>
    <files><path>~/FileCabinet/SuiteScripts/*</path></files>
</deploy>
`);
    process.stdout.write(directory + '\n');
} catch (error) {
    fs.rmSync(directory, {recursive: true, force: true});
    throw error;
}
