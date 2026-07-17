from __future__ import annotations

import unittest

from app.api import routes_documents, routes_jobs


class APIJobContractTests(unittest.TestCase):
    def test_process_route_documents_synchronous_contract(self):
        route = next(
            route
            for route in routes_documents.router.routes
            if getattr(route, "path", "") == "/api/documents/{document_id}/process"
        )

        self.assertIn("Synchronously process", route.summary)
        self.assertIn("inline before returning", route.description)
        self.assertIn("does not enqueue background work", route.description)

    def test_jobs_route_documents_lookup_contract(self):
        route = next(
            route
            for route in routes_jobs.router.routes
            if getattr(route, "path", "") == "/api/jobs/{job_id}"
        )

        self.assertIn("Read a stored processing job", route.summary)
        self.assertIn("status lookup", route.description)
        self.assertIn("queue progress stream", route.description)


if __name__ == "__main__":
    unittest.main()
