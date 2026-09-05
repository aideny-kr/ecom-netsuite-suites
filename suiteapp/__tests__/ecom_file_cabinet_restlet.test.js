jest.mock('N/file');
jest.mock('N/search');
jest.mock('N/log');
jest.mock('N/runtime');
jest.mock('N/error');

describe('ecom_file_cabinet_restlet', () => {
    let restlet;

    beforeEach(() => {
        jest.resetModules();
        require('N/runtime').getCurrentScript.mockReturnValue({getRemainingUsage: () => 1000});
        require('N/error').create.mockImplementation(({name, message}) => Object.assign(new Error(message), {name}));
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
        test('updates file contents in place, preserving the deployed file ID', () => {
            const file = require('N/file');
            const mockFile = {name: 'existing.js', contents: '// old', save: jest.fn().mockReturnValue(42)};
            file.load.mockReturnValue(mockFile);

            const result = restlet.put({ fileId: 42, content: '// updated' });

            expect(result.success).toBe(true);
            expect(result.fileId).toBe(42);
            expect(mockFile.contents).toBe('// updated');
            expect(mockFile.save).toHaveBeenCalledTimes(1);
            expect(file.delete).not.toHaveBeenCalled();
            expect(file.create).not.toHaveBeenCalled();
        });
    });
});
