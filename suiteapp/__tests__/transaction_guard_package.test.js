const fs = require('node:fs');
const path = require('node:path');
const {execFileSync} = require('node:child_process');

test('the installable guard project contains its own disabled deployment and exact guard source', () => {
    let directory;
    try {
        directory = execFileSync(process.execPath, [path.join(__dirname, '../scripts/build-transaction-guard-project.js')],
            {encoding: 'utf8'}).trim();
        expect(path.basename(directory)).toMatch(/^framework-transaction-guard-/);
        expect(fs.readdirSync(path.join(directory, 'Objects')).sort()).toEqual(['custbody_ecom_tx_ops_work_key.xml','customscript_ecom_tx_ops_guard.xml']);
        expect(fs.readdirSync(path.join(directory, 'FileCabinet/SuiteScripts')).sort()).toEqual(['ecom_tx_ops_create.js','ecom_tx_ops_guard.js']);
        expect(fs.readFileSync(path.join(directory, 'FileCabinet/SuiteScripts/ecom_tx_ops_guard.js'), 'utf8')).toBe(
            fs.readFileSync(path.join(__dirname, '../src/FileCabinet/SuiteScripts/ecom_tx_ops_guard.js'), 'utf8'));
        expect(fs.readFileSync(path.join(directory, 'FileCabinet/SuiteScripts/ecom_tx_ops_create.js'), 'utf8')).toBe(
            fs.readFileSync(path.join(__dirname, '../src/FileCabinet/SuiteScripts/ecom_tx_ops_create.js'), 'utf8'));
        const definition = fs.readFileSync(path.join(directory, 'Objects/customscript_ecom_tx_ops_guard.xml'), 'utf8');
        expect(definition).toContain('<defaultchecked>F</defaultchecked>');
        expect(definition).toContain('<status>TESTING</status>');
        expect(definition).toContain('<allroles>F</allroles>');
        expect(definition).toContain('custscript_ecom_tx_create_enabled');
        expect(definition.match(/<defaultchecked>F<\/defaultchecked>/g)).toHaveLength(2);
        const attribution = fs.readFileSync(path.join(directory,'Objects/custbody_ecom_tx_ops_work_key.xml'),'utf8');
        expect(attribution).toContain('<transactionbodycustomfield scriptid="custbody_ecom_tx_ops_work_key">');
        expect(attribution).toContain('<fieldtype>TEXT</fieldtype>');
        expect(attribution).toContain('<storevalue>T</storevalue>');
        expect(attribution).toContain('<bodysale>T</bodysale>');
        expect(attribution).toContain('<maxlength>64</maxlength>');
        expect(fs.readFileSync(path.join(directory, 'deploy.xml'), 'utf8')).toContain('<objects>');
    } finally {
        if (directory && path.basename(directory).startsWith('framework-transaction-guard-'))
            fs.rmSync(directory, {recursive: true, force: true});
    }
});
