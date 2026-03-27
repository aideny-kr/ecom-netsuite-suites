"""Task file storage — upload/download for agent task files."""
import os
import uuid
from pathlib import Path
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from app.models.task_file import TaskFile

TASK_FILE_ROOT = Path(os.getenv("TASK_FILE_ROOT", "/data/task_files"))
MAX_FILE_SIZE = 10 * 1024 * 1024  # 10MB
ALLOWED_EXTENSIONS = {".xlsx", ".csv", ".xls"}


class TaskFileService:
    async def save_upload(self, db: AsyncSession, tenant_id: uuid.UUID, user_id: uuid.UUID, filename: str, content: bytes) -> TaskFile:
        """Validate, store on disk, create DB record. direction='input'."""
        self._validate_upload(filename, content)
        ext = Path(filename).suffix.lower()
        file_id = uuid.uuid4()
        tenant_dir = TASK_FILE_ROOT / str(tenant_id)
        tenant_dir.mkdir(parents=True, exist_ok=True)
        storage_path = tenant_dir / f"{file_id}{ext}"
        storage_path.write_bytes(content)
        task_file = TaskFile(
            id=file_id,
            tenant_id=tenant_id,
            user_id=user_id,
            filename=filename,
            file_type=ext.lstrip("."),
            file_size=len(content),
            storage_path=str(storage_path),
            direction="input",
        )
        db.add(task_file)
        return task_file

    async def save_output(
        self,
        db: AsyncSession,
        tenant_id: uuid.UUID,
        user_id: uuid.UUID,
        filename: str,
        content: bytes,
        related_message_id: uuid.UUID | None = None,
    ) -> TaskFile:
        """Store generated output. direction='output'."""
        ext = Path(filename).suffix.lower() or ".xlsx"
        file_id = uuid.uuid4()
        tenant_dir = TASK_FILE_ROOT / str(tenant_id)
        tenant_dir.mkdir(parents=True, exist_ok=True)
        storage_path = tenant_dir / f"{file_id}{ext}"
        storage_path.write_bytes(content)
        task_file = TaskFile(
            id=file_id,
            tenant_id=tenant_id,
            user_id=user_id,
            filename=filename,
            file_type=ext.lstrip("."),
            file_size=len(content),
            storage_path=str(storage_path),
            direction="output",
            related_message_id=related_message_id,
        )
        db.add(task_file)
        return task_file

    async def get_file(self, db: AsyncSession, tenant_id: uuid.UUID, file_id: uuid.UUID) -> tuple[TaskFile, bytes]:
        """Get file record + contents. Raises ValueError if not found."""
        result = await db.execute(
            select(TaskFile).where(TaskFile.id == file_id, TaskFile.tenant_id == tenant_id)
        )
        task_file = result.scalar_one_or_none()
        if not task_file:
            raise ValueError("File not found")
        content = Path(task_file.storage_path).read_bytes()
        return task_file, content

    def _validate_upload(self, filename: str, content: bytes) -> None:
        ext = Path(filename).suffix.lower()
        if ext not in ALLOWED_EXTENSIONS:
            raise ValueError(f"File type '{ext}' not allowed. Accepted: {ALLOWED_EXTENSIONS}")
        if len(content) > MAX_FILE_SIZE:
            raise ValueError(f"File size {len(content)} exceeds {MAX_FILE_SIZE} byte limit")
