// Emit an isolated SDF project containing the transaction guard's deployment.
// No credentials, account mutations, installation, or license acceptance.
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'framework-transaction-guard-'));
try {
    for (const relative of ['Objects/customscript_ecom_tx_ops_guard.xml', 'FileCabinet/SuiteScripts/ecom_tx_ops_guard.js']) {
        const target = path.join(directory, relative);
        fs.mkdirSync(path.dirname(target), {recursive: true});
        fs.copyFileSync(path.join(__dirname, '../src', relative), target);
    }
    fs.writeFileSync(path.join(directory, 'manifest.xml'), `<?xml version="1.0" encoding="UTF-8"?>
<manifest projecttype="ACCOUNTCUSTOMIZATION">
    <projectname>Framework Transaction Guard</projectname>
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
