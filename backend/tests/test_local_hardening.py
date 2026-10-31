"""Real ASGI integration without network/data lifecycle; isolated auth storage."""
import os
import tempfile
import unittest
from pathlib import Path
from fastapi.testclient import TestClient
from app.main import app
from app.config import settings
from app.services import auth

class LocalHardeningTests(unittest.TestCase):
    def test_auth_origin_and_disabled_execution(self):
        old = settings.data_dir
        with tempfile.TemporaryDirectory() as directory:
            settings.data_dir = Path(directory)
            auth._sessions.clear()
            try:
                c = TestClient(app, base_url="http://127.0.0.1:3018", client=("127.0.0.1", 12345))
                self.assertEqual(c.get("/api/settings").status_code, 403)
                self.assertEqual(c.get("/", headers={"Host":"evil.example"}).status_code, 403)
                self.assertEqual(c.get("/", headers={"Origin":"https://evil.example"}).status_code, 403)
                self.assertEqual(c.post("/api/auth/setup", json={"password":"integration-test-only"}).status_code, 403)
                headers={"Origin":"http://127.0.0.1:3018"}
                self.assertEqual(c.post("/api/auth/setup", headers=headers,json={"password":"integration-test-only"}).status_code,200)
                self.assertEqual(c.get("/api/settings").status_code,401)
                self.assertEqual(c.post("/api/auth/login",headers=headers,json={"password":"integration-test-only"}).status_code,200)
                self.assertTrue(c.get("/api/auth/status").json()["authenticated"])
                self.assertEqual(c.post("/api/strategies/ai/save",headers=headers,json={"strategy_id":"test","code":"raise RuntimeError(123)"}).status_code,403)
                self.assertEqual((Path(directory)/"user_data"/"auth.json").stat().st_mode & 0o777,0o600)
            finally:
                auth._sessions.clear()
                settings.data_dir=old

if __name__=="__main__": unittest.main()
