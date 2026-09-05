const fs = require('node:fs');
const path = require('node:path');
const {execFileSync} = require('node:child_process');

test('the installable guard project contains its own disabled deployment and exact guard source', () => {
    let directory;
    try {
        directory = execFileSync(process.execPath, [path.join(__dirname, '../scripts/build-transaction-guard-project.js')],
            {encoding: 'utf8'}).trim();
        expect(path.basename(directory)).toMatch(/^framework-transaction-guard-/);
        expect(fs.readdirSync(path.join(directory, 'Objects'))).toEqual(['customscript_ecom_tx_ops_guard.xml']);
        expect(fs.readdirSync(path.join(directory, 'FileCabinet/SuiteScripts'))).toEqual(['ecom_tx_ops_guard.js']);
        expect(fs.readFileSync(path.join(directory, 'FileCabinet/SuiteScripts/ecom_tx_ops_guard.js'), 'utf8')).toBe(
            fs.readFileSync(path.join(__dirname, '../src/FileCabinet/SuiteScripts/ecom_tx_ops_guard.js'), 'utf8'));
        const definition = fs.readFileSync(path.join(directory, 'Objects/customscript_ecom_tx_ops_guard.xml'), 'utf8');
        expect(definition).toContain('<defaultchecked>F</defaultchecked>');
        expect(definition).toContain('<status>TESTING</status>');
        expect(definition).toContain('<allroles>F</allroles>');
        expect(fs.readFileSync(path.join(directory, 'deploy.xml'), 'utf8')).toContain('<objects>');
    } finally {
        if (directory && path.basename(directory).startsWith('framework-transaction-guard-'))
            fs.rmSync(directory, {recursive: true, force: true});
    }
});
