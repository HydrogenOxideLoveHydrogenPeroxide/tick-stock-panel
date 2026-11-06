"""Quant routes through real Host/Origin/auth middleware; no actual holdings or AI."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from fastapi.testclient import TestClient
from app.main import app
from app.api.auth import COOKIE_NAME
from app.config import settings
from app.services import auth

ORIGIN = {"Origin": "http://127.0.0.1:3018"}

class QuantIntegrationTests(unittest.TestCase):
    def test_auth_and_origin_before_quant_routes(self):
        c = TestClient(app, base_url="http://127.0.0.1:3018")
        with patch.object(auth, "is_configured", return_value=False):
            self.assertEqual(c.get("/api/quant-assistant/portfolio").status_code, 403)
        with patch.object(auth, "is_configured", return_value=True):
            self.assertEqual(c.get("/api/quant-assistant/portfolio").status_code, 401)
            self.assertEqual(c.get("/api/quant-assistant/trades").status_code, 401)
            for path in ("candidates", "explain", "value-factors/evaluate", "value-factors/sync"):
                self.assertEqual(c.post("/api/quant-assistant/"+path, headers=ORIGIN, json={}).status_code, 401)
                self.assertEqual(c.post("/api/quant-assistant/"+path, headers={"Origin":"https://evil.example"}, json={}).status_code, 403)
            self.assertEqual(c.post("/api/quant-assistant/trades", headers={"Origin":"https://evil.example"}, json={}).status_code, 403)
            self.assertEqual(c.put("/api/quant-assistant/portfolio", json={}).status_code, 403)
            self.assertEqual(c.post("/api/quant-assistant/suggest", headers={"Origin":"https://evil.example"}, json={}).status_code, 403)
            self.assertEqual(c.get("/api/quant-assistant/portfolio", headers={"Host":"evil.example"}).status_code, 403)

    def test_authenticated_save_reload_and_empty_draft_no_persistence(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(settings,"data_dir",Path(directory).resolve()), patch.object(auth,"is_configured",return_value=True), patch.object(auth,"is_valid_session",return_value=True), patch.object(app.state,"repo",object(),create=True):
            c = TestClient(app, base_url="http://127.0.0.1:3018")
            c.cookies.set(COOKIE_NAME,"isolated-test-only")
            initial = c.get("/api/quant-assistant/portfolio")
            self.assertEqual(initial.status_code,200)
            draft = initial.json()
            draft["cash"] = 5000
            saved = c.put("/api/quant-assistant/portfolio",headers=ORIGIN,json=draft)
            self.assertEqual(saved.status_code,200,saved.text)
            self.assertEqual(c.get("/api/quant-assistant/portfolio").json()["cash"],5000)
            draft["cash"] = 1234
            suggested = c.post("/api/quant-assistant/suggest",headers=ORIGIN,json={"portfolio":draft,"mode":"morning"})
            self.assertEqual(suggested.status_code,200,suggested.text)
            self.assertEqual(suggested.json()["suggestions"],[])
            self.assertEqual(suggested.json()["summary"]["cash"],1234)
            self.assertEqual(c.get("/api/quant-assistant/portfolio").json()["cash"],5000)
            bad = {**draft,"cash":-1}
            self.assertEqual(c.put("/api/quant-assistant/portfolio",headers=ORIGIN,json=bad).status_code,422)
            self.assertEqual(c.get("/api/quant-assistant/portfolio").json()["cash"],5000)

if __name__ == "__main__":
    unittest.main()
