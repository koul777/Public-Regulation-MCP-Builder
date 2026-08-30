from __future__ import annotations

import unittest
from uuid import uuid4

from app.schemas.authoring import OFFICIAL_BOUNDARY_NOTICE
from app.services.authoring_template_service import (
    AuthoringTemplateNotFoundError,
    AuthoringTemplateService,
)


class AuthoringTemplateServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = AuthoringTemplateService()

    def test_templates_are_generic_korean_beginner_guides(self) -> None:
        templates = self.service.list_templates()

        self.assertEqual(
            {"general-regulation", "committee-operation", "work-procedure"},
            {template.template_id for template in templates},
        )
        for template in templates:
            self.assertEqual(OFFICIAL_BOUNDARY_NOTICE, template.boundary_notice)
            self.assertIn("적으세요", template.first_action_ko)
            self.assertTrue(all(node.beginner_guidance for node in template.nodes))
            self.assertNotIn("기관-a", template.model_dump_json())

    def test_instantiation_uses_stable_uuid_nodes_and_parent_links(self) -> None:
        project_id = uuid4()

        first = self.service.instantiate_clauses("general-regulation", project_id=project_id)
        second = self.service.instantiate_clauses("general-regulation", project_id=project_id)

        self.assertEqual([item.clause_id for item in first], [item.clause_id for item in second])
        self.assertEqual(len(first), len({item.clause_id for item in first}))
        known_ids = {item.clause_id for item in first}
        self.assertTrue(all(item.parent_id is None or item.parent_id in known_ids for item in first))
        self.assertTrue(all(item.body == "" for item in first))

    def test_list_and_get_return_defensive_copies(self) -> None:
        listed = self.service.list_templates()
        listed[0].nodes[0].title = "변조"

        fresh = self.service.get_template("general-regulation")

        self.assertNotEqual("변조", fresh.nodes[0].title)

    def test_unknown_template_fails_closed(self) -> None:
        with self.assertRaises(AuthoringTemplateNotFoundError):
            self.service.get_template("institution-secret-template")


if __name__ == "__main__":
    unittest.main()
