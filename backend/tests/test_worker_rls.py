"""Tests for worker RLS tenant context enforcement.

Verifies that Celery workers set SET LOCAL app.current_tenant_id
before executing any database queries, preventing cross-tenant data leaks.
"""




class TestTenantSessionSource:
    """Verify tenant_session sets RLS context by inspecting source code."""

    def _get_source(self, path: str) -> str:
        """Read a Python source file."""
        with open(path) as f:
            return f.read()

    def test_tenant_session_calls_set_local(self):
        """tenant_session must call SET LOCAL app.current_tenant_id."""
        src = self._get_source("app/workers/base_task.py")
        assert "SET LOCAL app.current_tenant_id" in src
        assert "def tenant_session" in src

    def test_tenant_session_uses_context_manager(self):
        """tenant_session must be a context manager."""
        src = self._get_source("app/workers/base_task.py")
        assert "@contextmanager" in src

    def test_tenant_session_accepts_tenant_id(self):
        """tenant_session must accept a tenant_id parameter."""
        src = self._get_source("app/workers/base_task.py")
        assert "def tenant_session(tenant_id" in src


class TestSyncTasksUseTenantSession:
    """Verify sync tasks use tenant_session, not raw Session."""

    def _get_source(self, path: str) -> str:
        with open(path) as f:
            return f.read()

    def test_stripe_sync_uses_tenant_session(self):
        """stripe_sync task must use tenant_session, not raw Session."""
        src = self._get_source("app/workers/tasks/stripe_sync.py")
        assert "tenant_session" in src, "stripe_sync must use tenant_session"
        # Must not use raw Session(sync_engine) for data queries
        assert "Session(sync_engine)" not in src, "stripe_sync must not use raw Session"

    def test_shopify_sync_uses_tenant_session(self):
        """shopify_sync task must use tenant_session, not raw Session."""
        src = self._get_source("app/workers/tasks/shopify_sync.py")
        assert "tenant_session" in src, "shopify_sync must use tenant_session"
        assert "Session(sync_engine)" not in src, "shopify_sync must not use raw Session"

    def test_stripe_sync_passes_tenant_id(self):
        """stripe_sync must pass tenant_id to tenant_session."""
        src = self._get_source("app/workers/tasks/stripe_sync.py")
        assert "tenant_session(tenant_id)" in src

    def test_shopify_sync_passes_tenant_id(self):
        """shopify_sync must pass tenant_id to tenant_session."""
        src = self._get_source("app/workers/tasks/shopify_sync.py")
        assert "tenant_session(tenant_id)" in src


class TestBaseTaskUseTenantSession:
    """Verify InstrumentedTask uses tenant_session in lifecycle hooks."""

    def _get_source(self, path: str) -> str:
        with open(path) as f:
            return f.read()

    def _extract_method(self, full_source: str, method_name: str) -> str:
        """Extract a method body from the source using indentation-based parsing."""
        lines = full_source.split("\n")
        in_method = False
        method_lines = []
        for line in lines:
            if f"def {method_name}(" in line:
                in_method = True
                method_lines.append(line)
                continue
            if in_method:
                # End of method: non-empty line at same or lower indent level
                if line.strip() and not line.startswith("    ") and not line.startswith("\t"):
                    break
                if line.strip() and (line.startswith("    def ") or line.startswith("\tdef ")):
                    break
                method_lines.append(line)
        return "\n".join(method_lines)

    def test_before_start_uses_tenant_session(self):
        """InstrumentedTask.before_start must use tenant_session."""
        src = self._get_source("app/workers/base_task.py")
        method = self._extract_method(src, "before_start")
        assert "tenant_session" in method, "before_start must use tenant_session"
        assert "Session(sync_engine)" not in method, "before_start must not use raw Session"

    def test_on_success_uses_tenant_session(self):
        """InstrumentedTask.on_success must use tenant_session."""
        src = self._get_source("app/workers/base_task.py")
        method = self._extract_method(src, "on_success")
        assert "tenant_session" in method, "on_success must use tenant_session"

    def test_on_failure_uses_tenant_session(self):
        """InstrumentedTask.on_failure must use tenant_session."""
        src = self._get_source("app/workers/base_task.py")
        method = self._extract_method(src, "on_failure")
        assert "tenant_session" in method, "on_failure must use tenant_session"

    def test_no_raw_session_in_lifecycle_hooks(self):
        """No lifecycle method should use raw Session(sync_engine)."""
        src = self._get_source("app/workers/base_task.py")
        for method_name in ["before_start", "on_success", "on_failure"]:
            method = self._extract_method(src, method_name)
            assert "Session(sync_engine)" not in method, (
                f"{method_name} must not use raw Session(sync_engine)"
            )
