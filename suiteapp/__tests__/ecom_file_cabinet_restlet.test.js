const SuiteCloudJestStubs = require('@oracle/suitecloud-unit-testing/SuiteCloudJestStubs');

describe('ecom_file_cabinet_restlet', () => {
    let restlet;

    beforeAll(() => {
        SuiteCloudJestStubs.install();
    });

    beforeEach(() => {
        jest.resetModules();
        restlet = require('../src/FileCabinet/SuiteScripts/ecom_file_cabinet_restlet');
    });

    describe('GET', () => {
        test('returns file content when fileId provided', () => {
            const file = require('N/file');
            file.load.mockReturnValue({
                id: 42,
                name: 'test_script.js',
                folder: 100,
                getContents: () => 'console.log("hello");',
                size: 22,
                fileType: 'JAVASCRIPT',
                dateCreated: new Date('2024-01-01'),
            });

            const result = restlet.get({ fileId: '42' });

            expect(result.success).toBe(true);
            expect(result.fileId).toBe(42);
            expect(result.content).toBe('console.log("hello");');
            expect(file.load).toHaveBeenCalledWith({ id: 42 });
        });

        test('returns error when no params provided', () => {
            const result = restlet.get({});
            expect(result.success).toBe(false);
            expect(result.error).toBe('MISSING_PARAM');
        });
    });

    describe('POST', () => {
        test('creates file and returns new ID', () => {
            const file = require('N/file');
            const mockFile = { save: jest.fn().mockReturnValue(99) };
            file.create.mockReturnValue(mockFile);
            file.Type = { JAVASCRIPT: 'JAVASCRIPT' };

            const result = restlet.post({
                name: 'new_script.js',
                folder: 100,
                content: '// new file',
            });

            expect(result.success).toBe(true);
            expect(result.fileId).toBe(99);
            expect(file.create).toHaveBeenCalledWith(
                expect.objectContaining({ name: 'new_script.js', folder: 100 })
            );
        });

        test('returns error when fields missing', () => {
            const result = restlet.post({ name: 'test.js' });
            expect(result.success).toBe(false);
        });
    });

    describe('PUT', () => {
        test('deletes and recreates file with new content', () => {
            const file = require('N/file');
            file.load.mockReturnValue({
                name: 'existing.js',
                folder: 100,
                fileType: 'JAVASCRIPT',
                description: 'test',
            });
            file.delete = jest.fn();
            const mockFile = { save: jest.fn().mockReturnValue(101) };
            file.create.mockReturnValue(mockFile);

            const result = restlet.put({ fileId: 42, content: '// updated' });

            expect(result.success).toBe(true);
            expect(result.fileId).toBe(101);
            expect(result.previousFileId).toBe(42);
            expect(file.delete).toHaveBeenCalledWith({ id: 42 });
        });
    });
});
