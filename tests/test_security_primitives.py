from __future__ import annotations

import unittest

from app.core import security, security_primitives


class SecurityPrimitivesTests(unittest.TestCase):
    def test_security_module_reexports_shared_primitives(self) -> None:
        self.assertIs(security.AuthContext, security_primitives.AuthContext)
        self.assertEqual(security.API_ROLE_ADMIN, security_primitives.API_ROLE_ADMIN)
        self.assertEqual(security.API_ROLE_OPERATOR, security_primitives.API_ROLE_OPERATOR)
        self.assertEqual(security.API_READ_ROLES, security_primitives.API_READ_ROLES)
        self.assertEqual(security.ROLE_SECURITY_LEVELS, security_primitives.ROLE_SECURITY_LEVELS)
