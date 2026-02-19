const SuiteCloudJestStubs = require('@oracle/suitecloud-unit-testing/SuiteCloudJestStubs');

describe('ecom_mock_data_restlet', () => {
    let restlet;

    beforeAll(() => {
        SuiteCloudJestStubs.install();
    });

    beforeEach(() => {
        jest.resetModules();
        restlet = require('../src/FileCabinet/SuiteScripts/ecom_mock_data_restlet');
    });

    test('masks PII fields by default', () => {
        const query = require('N/query');
        query.runSuiteQL.mockReturnValue({
            columns: [
                { label: 'id', index: 0 },
                { label: 'email', index: 1 },
                { label: 'companyname', index: 2 },
            ],
            asMappedResults: () => [
                { id: 1, email: 'real@company.com', companyname: 'Acme Corp' },
                { id: 2, email: 'other@company.com', companyname: 'Beta Inc' },
            ],
        });

        const result = restlet.post({ query: 'SELECT id, email, companyname FROM customer' });

        expect(result.success).toBe(true);
        expect(result.masked).toBe(true);
        expect(result.data[0].email).toBe('test_0@example.com');
        expect(result.data[0].companyname).toBe('Test_Entity_0');
        expect(result.data[0].id).toBe(1); // Non-PII preserved
    });

    test('preserves real data when maskPII=false', () => {
        const query = require('N/query');
        query.runSuiteQL.mockReturnValue({
            columns: [{ label: 'id', index: 0 }, { label: 'email', index: 1 }],
            asMappedResults: () => [{ id: 1, email: 'real@company.com' }],
        });

        const result = restlet.post({
            query: 'SELECT id, email FROM customer',
            maskPII: false,
        });

        expect(result.data[0].email).toBe('real@company.com');
        expect(result.masked).toBe(false);
    });

    test('returns error when query missing', () => {
        const result = restlet.post({});
        expect(result.success).toBe(false);
    });
});
