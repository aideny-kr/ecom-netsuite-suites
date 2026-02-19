/**
 * @NApiVersion 2.1
 * @NScriptType Restlet
 * @NModuleScope SameAccount
 */
define(['N/file', 'N/search', 'N/log', 'N/runtime', 'N/error'], (file, search, log, runtime, error) => {

    /**
     * GET: Read file content by internal ID or path.
     * Query params: fileId (number) OR filePath (string)
     * Returns: { success, fileId, name, folder, content, size, fileType, lastModified }
     */
    const get = (requestParams) => {
        try {
            const script = runtime.getCurrentScript();
            log.debug('FileCabinet GET', JSON.stringify(requestParams));

            let fileObj;
            if (requestParams.fileId) {
                fileObj = file.load({ id: parseInt(requestParams.fileId, 10) });
            } else if (requestParams.filePath) {
                fileObj = file.load({ id: requestParams.filePath });
            } else {
                throw error.create({
                    name: 'MISSING_PARAM',
                    message: 'Provide fileId or filePath',
                });
            }

            return {
                success: true,
                fileId: fileObj.id,
                name: fileObj.name,
                folder: fileObj.folder,
                content: fileObj.getContents(),
                size: fileObj.size,
                fileType: fileObj.fileType,
                lastModified: fileObj.dateCreated?.toISOString() || null,
                remainingUsage: script.getRemainingUsage(),
            };
        } catch (e) {
            log.error('FileCabinet GET Error', e.message);
            return { success: false, error: e.name, message: e.message };
        }
    };

    /**
     * POST: Create a new file in the File Cabinet.
     * Body: { name, folder (ID), content, fileType? (default JAVASCRIPT), description? }
     * Returns: { success, fileId, name }
     */
    const post = (requestBody) => {
        try {
            log.debug('FileCabinet POST', `name=${requestBody.name}, folder=${requestBody.folder}`);

            if (!requestBody.name || !requestBody.folder || requestBody.content === undefined) {
                throw error.create({
                    name: 'MISSING_FIELDS',
                    message: 'name, folder, and content are required',
                });
            }

            const fileObj = file.create({
                name: requestBody.name,
                fileType: requestBody.fileType || file.Type.JAVASCRIPT,
                contents: requestBody.content,
                folder: parseInt(requestBody.folder, 10),
                description: requestBody.description || '',
            });

            const fileId = fileObj.save();
            log.audit('FileCabinet CREATE', `Created file ${requestBody.name} with ID ${fileId}`);

            return { success: true, fileId: fileId, name: requestBody.name };
        } catch (e) {
            log.error('FileCabinet POST Error', e.message);
            return { success: false, error: e.name, message: e.message };
        }
    };

    /**
     * PUT: Update existing file content by internal ID.
     * Body: { fileId, content, description? }
     * Returns: { success, fileId, name, size }
     *
     * N/file does not support in-place update — we delete + re-create with same name/folder.
     */
    const put = (requestBody) => {
        try {
            log.debug('FileCabinet PUT', `fileId=${requestBody.fileId}`);

            if (!requestBody.fileId || requestBody.content === undefined) {
                throw error.create({
                    name: 'MISSING_FIELDS',
                    message: 'fileId and content are required',
                });
            }

            // Load existing to preserve metadata
            const existing = file.load({ id: parseInt(requestBody.fileId, 10) });
            const meta = {
                name: existing.name,
                folder: existing.folder,
                fileType: existing.fileType,
                description: requestBody.description || existing.description || '',
            };

            // Delete and re-create (N/file update strategy)
            file.delete({ id: parseInt(requestBody.fileId, 10) });

            const newFile = file.create({
                name: meta.name,
                fileType: meta.fileType,
                contents: requestBody.content,
                folder: meta.folder,
                description: meta.description,
            });

            const newFileId = newFile.save();
            log.audit('FileCabinet UPDATE', `Updated ${meta.name}: old=${requestBody.fileId} new=${newFileId}`);

            return {
                success: true,
                fileId: newFileId,
                name: meta.name,
                size: requestBody.content.length,
                previousFileId: parseInt(requestBody.fileId, 10),
            };
        } catch (e) {
            log.error('FileCabinet PUT Error', e.message);
            return { success: false, error: e.name, message: e.message };
        }
    };

    /**
     * DELETE: Delete a file from the File Cabinet.
     * Query params: fileId (number)
     */
    const doDelete = (requestParams) => {
        try {
            if (!requestParams.fileId) {
                throw error.create({ name: 'MISSING_PARAM', message: 'fileId is required' });
            }
            const fid = parseInt(requestParams.fileId, 10);
            // Load first to get name for audit
            const existing = file.load({ id: fid });
            file.delete({ id: fid });
            log.audit('FileCabinet DELETE', `Deleted ${existing.name} (${fid})`);
            return { success: true, fileId: fid, name: existing.name };
        } catch (e) {
            log.error('FileCabinet DELETE Error', e.message);
            return { success: false, error: e.name, message: e.message };
        }
    };

    return { get, post, put, 'delete': doDelete };
});
