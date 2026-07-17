from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.core.config import Settings
from app.core.tenant_access import settings_for_tenant, tenant_storage_key


class TenantAccessTests(unittest.TestCase):
    def test_tenant_storage_key_is_path_safe(self) -> None:
        self.assertEqual(tenant_storage_key("../Tenant A/../../secret"), "Tenant_A_.._.._secret")
        self.assertEqual(tenant_storage_key("   "), "default")
        self.assertEqual(tenant_storage_key("기관-01"), "01")

    def test_settings_for_tenant_preserves_default_when_isolation_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp), tenant_storage_isolation=False)

            scoped = settings_for_tenant(settings, "tenant-a")

        self.assertIs(scoped, settings)

    def test_settings_for_tenant_uses_tenant_subdirectory_when_enabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp), tenant_storage_isolation=True)

            scoped = settings_for_tenant(settings, "../tenant-a")

        self.assertEqual(scoped.data_dir, Path(tmp) / "tenants" / "tenant-a")
        self.assertEqual(scoped.api_auth_token, settings.api_auth_token)


if __name__ == "__main__":
    unittest.main()
