"""HTTP regression for the packaged static page, authentication and retrieval."""
import os
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from building_kb import paths
from building_kb.search.retrieve import Retriever
from building_kb.web import main


class WebTests(unittest.TestCase):
    def setUp(self):
        self.environment = patch.dict(os.environ, {"KB_PASSCODE": "layout-test-only", "KB_HTTPS": ""})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        fixture = Retriever(str(paths.ROOT / "examples/demo/chunks"), chroma_dir="/does-not-exist")
        self.retriever = patch.object(main, "_r", fixture)
        self.retriever.start()
        self.addCleanup(self.retriever.stop)
        self.client = TestClient(main.app)
        self.addCleanup(self.client.close)

    def test_page_login_cookie_and_logout(self):
        self.assertEqual(self.client.get("/").status_code, 200)
        self.assertEqual(self.client.get("/api/books").status_code, 401)
        self.assertEqual(self.client.post("/api/login", json={"passcode": "incorrect"}).status_code, 401)
        response = self.client.post("/api/login", json={"passcode": "layout-test-only"})
        self.assertEqual(response.status_code, 200)
        self.assertIn("HttpOnly", response.headers["set-cookie"])
        self.assertIn("SameSite=lax", response.headers["set-cookie"])
        self.assertEqual(self.client.get("/api/books").status_code, 200)
        self.assertEqual(self.client.post("/api/logout").status_code, 200)
        self.assertEqual(self.client.get("/api/books").status_code, 401)

    def test_packaged_app_streams_exact_clause_without_model(self):
        self.client.post("/api/login", json={"passcode": "layout-test-only"})
        with patch.object(main.providers, "get_llm", side_effect=AssertionError("model must not be called")):
            response = self.client.get("/api/ask", params={"q": "GB 00000 第1.0.1条", "llm": 0})
        self.assertEqual(response.status_code, 200)
        self.assertIn("event: hits", response.text)
        self.assertIn("GB 00000-2099", response.text)
        self.assertIn("event: done", response.text)

    def test_pdf_and_clause_interfaces_require_authentication(self):
        for endpoint in ["/api/chunk/unknown", "/api/pdf?code=GB+00000", "/api/page?code=GB+00000&page=1"]:
            with self.subTest(endpoint=endpoint):
                self.assertEqual(self.client.get(endpoint).status_code, 401)
