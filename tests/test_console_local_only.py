"""控制台仅本机：同热点/局域网不可操作管理台，/v1 仍可用。"""
import unittest
from unittest.mock import patch

from admin import server
from admin.config import settings


def _scope(path: str, client: str):
    return {
        "type": "http",
        "path": path,
        "headers": [],
        "client": (client, 54321),
        "method": "GET",
    }


class ConsoleGateTests(unittest.IsolatedAsyncioTestCase):
    async def _run(self, path, client):
        hits = {"n": 0}

        async def app(scope, receive, send):
            hits["n"] += 1
            from starlette.responses import PlainTextResponse
            await PlainTextResponse("ok")(scope, receive, send)

        mw = server.ConsoleLocalOnlyMiddleware(app)
        sent = []

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message):
            sent.append(message)

        with patch.object(settings, "CONSOLE_LOCAL_ONLY", True), \
             patch.object(settings, "CONSOLE_ALLOWED_IPS", ""):
            await mw(_scope(path, client), receive, send)
        return hits["n"], sent

    async def test_lan_cannot_open_admin(self):
        n, sent = await self._run("/admin", "192.168.137.50")
        self.assertEqual(n, 0)
        start = next(m for m in sent if m.get("type") == "http.response.start")
        self.assertEqual(start["status"], 403)

    async def test_lan_cannot_call_admin_api(self):
        for path in ("/api/keys", "/api/login", "/api/sync/receive", "/join", "/static/x.js"):
            n, sent = await self._run(path, "10.0.0.9")
            self.assertEqual(n, 0, path)
            start = next(m for m in sent if m.get("type") == "http.response.start")
            self.assertEqual(start["status"], 403, path)

    async def test_spoofed_xff_still_blocked(self):
        scope = _scope("/admin", "192.168.1.9")
        scope["headers"] = [(b"x-real-ip", b"127.0.0.1"), (b"x-forwarded-for", b"127.0.0.1")]
        hits = {"n": 0}

        async def app(scope, receive, send):
            hits["n"] += 1

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        sent = []

        async def send(message):
            sent.append(message)

        mw = server.ConsoleLocalOnlyMiddleware(app)
        with patch.object(settings, "CONSOLE_LOCAL_ONLY", True):
            await mw(scope, receive, send)
        self.assertEqual(hits["n"], 0)
        self.assertEqual(sent[0]["status"], 403)

    async def test_localhost_ok(self):
        n, sent = await self._run("/admin", "127.0.0.1")
        self.assertEqual(n, 1)

    async def test_v1_still_open_for_lan(self):
        n, _ = await self._run("/v1/messages", "192.168.137.50")
        self.assertEqual(n, 1)

    async def test_allowed_ip_override(self):
        async def app(scope, receive, send):
            from starlette.responses import PlainTextResponse
            await PlainTextResponse("ok")(scope, receive, send)

        mw = server.ConsoleLocalOnlyMiddleware(app)
        sent = []

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message):
            sent.append(message)

        with patch.object(settings, "CONSOLE_LOCAL_ONLY", True), \
             patch.object(settings, "CONSOLE_ALLOWED_IPS", "192.168.137.50"):
            await mw(_scope("/admin", "192.168.137.50"), receive, send)
        self.assertTrue(any(m.get("type") == "http.response.start" and m["status"] == 200 for m in sent))


if __name__ == "__main__":
    unittest.main()
