"""临时端口/临时目录验证本机页面传输边界，不审批真实经验。"""
import http.client
import json
import os
from pathlib import Path
import socket
import tempfile
import threading
import unittest
from unittest import mock

import memory_store
import memory_ui


class EchoService:
    def __init__(self):
        self.writes = []

    def get(self, path, query):
        return {"path": path, "query": query}

    def post(self, path, body):
        self.writes.append((path, body))
        return {"saved": True, "body": body}


class MemoryUITests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.private = self.root / "private"
        self.environment = mock.patch.dict(os.environ, {"TERM_EXTRACT_MEMORY_DIR": str(self.private)})
        self.environment.start()
        web = self.root / "web" / "memory"
        web.mkdir(parents=True)
        for name, content in (("index.html", "<!doctype html><title>合成页面</title>"),
                              ("app.js", "console.log('fixture');"), ("styles.css", "body {color: black}")):
            (web / name).write_text(content, encoding="utf-8")
        self.server = memory_ui.create_server(self.root, port=0)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        self.thread.start()
        self.authority = f"127.0.0.1:{self.server.server_address[1]}"
        self.origin = "http://" + self.authority

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)
        self.environment.stop()
        self.temporary.cleanup()

    def request(self, method="GET", path="/api/session", body=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=3)
        values = {"Host": self.authority}
        if headers:
            values.update(headers)
        if isinstance(body, dict):
            body = json.dumps(body, ensure_ascii=False).encode("utf-8")
        try:
            connection.request(method, path, body=body, headers=values)
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def post(self, path="/api/echo", body=None, headers=None):
        values = {"Origin": self.origin, "X-CSRF-Token": self.server.csrf_token,
                  "Content-Type": "application/json; charset=utf-8"}
        values.update(headers or {})
        return self.request("POST", path, {} if body is None else body, values)

    def test_session_and_empty_overview_do_not_create_database(self):
        status, headers, raw = self.request()
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(raw), {"csrf_token": self.server.csrf_token, "read_only": False})
        self.assertEqual(headers["Cache-Control"], "no-store")
        status, _, raw = self.request(path="/api/overview")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(raw)["counts"]["published"], 0)
        self.assertFalse(self.private.exists())

    def test_security_headers_and_static_allowlist(self):
        for route, mime in (("/", "text/html"), ("/app.js", "text/javascript"), ("/styles.css", "text/css")):
            with self.subTest(route=route):
                status, headers, _ = self.request(path=route)
                self.assertEqual(status, 200)
                self.assertTrue(headers["Content-Type"].startswith(mime))
                self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
                self.assertEqual(headers["X-Frame-Options"], "DENY")
                self.assertIn("script-src 'self'", headers["Content-Security-Policy"])
                self.assertNotIn("unsafe-inline", headers["Content-Security-Policy"])
                self.assertFalse(any(key.lower().startswith("access-control-") for key in headers))
        self.assertEqual(self.request(path="/favicon.svg")[0], 404)
        self.assertEqual(self.request(path="/memory.sqlite3")[0], 404)

    def test_evaluation_example_is_exact_read_only_public_file(self):
        example = self.root / "examples" / "evaluation" / "manifest.example.json"
        example.parent.mkdir(parents=True)
        value = {"schema_version": 1, "cases": [], "说明": "合成示例"}
        example.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        (example.parent / "private.json").write_text('{"secret":true}', encoding="utf-8")
        route = "/examples/evaluation/manifest.example.json"
        self.server.read_only = True
        status, headers, raw = self.request(path=route)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(raw), value)
        self.assertTrue(headers["Content-Type"].startswith("application/json"))
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
        for path in ("/examples/evaluation/private.json", "/examples/evaluation/prepare_demo.py",
                     "/examples/evaluation/", "/examples/evaluation/../private.json"):
            with self.subTest(path=path):
                self.assertIn(self.request(path=path)[0], (400, 404))
        self.assertEqual(self.post(path=route)[0], 403)
        self.server.read_only = False
        self.assertEqual(self.post(path=route)[0], 404)
        example.unlink()
        self.assertEqual(self.request(path=route)[0], 404)
        self.assertFalse(self.private.exists())

    def test_host_and_origin_are_checked_before_dispatch(self):
        self.server.service = EchoService()
        for host in ("evil.example", "127.0.0.1:1", self.authority + ".evil.example", "localhost"):
            with self.subTest(host=host):
                self.assertEqual(self.request(headers={"Host": host})[0], 403)
        for origin in ("https://" + self.authority, "http://evil.example", "null", self.origin + "/"):
            with self.subTest(origin=origin):
                self.assertEqual(self.request(headers={"Origin": origin})[0], 403)
                self.assertEqual(self.post(headers={"Origin": origin})[0], 403)
        self.assertEqual(self.server.service.writes, [])
        self.assertEqual(self.request(headers={"Host": f"localhost:{self.server.server_address[1]}"})[0], 200)

    def test_post_requires_origin_and_csrf_and_read_only_blocks_writes(self):
        service = EchoService()
        self.server.service = service
        self.assertEqual(self.request("POST", "/api/echo", {}, {"Content-Type": "application/json"})[0], 403)
        self.assertEqual(self.request("POST", "/api/echo", {}, {"Content-Type": "application/json", "Origin": self.origin})[0], 403)
        self.assertEqual(self.post(headers={"X-CSRF-Token": "wrong"})[0], 403)
        self.server.read_only = True
        self.assertEqual(json.loads(self.request()[2])["read_only"], True)
        self.assertEqual(self.post()[0], 403)
        self.assertEqual(service.writes, [])

    def test_factory_read_only_is_preserved(self):
        other = memory_ui.create_server(self.root, port=0, read_only=True)
        try:
            self.assertIs(other.read_only, True)
            self.assertNotEqual(other.csrf_token, self.server.csrf_token)
        finally:
            other.server_close()

    def test_reviewed_publish_keeps_csrf_read_only_and_explicit_human_ack(self):
        body = {"memory_id": "missing", "revision": 1, "content_sha256": "a" * 64,
                "reviewer": "合成审核人", "note": "仅合成请求", "acknowledged": True}
        self.assertEqual(self.request("POST", "/api/publish-reviewed", body,
                         {"Content-Type": "application/json", "Origin": self.origin})[0], 403)
        for value in (False, 1, "true", None):
            status, _, raw = self.post(path="/api/publish-reviewed", body={**body, "acknowledged": value})
            self.assertEqual(status, 400)
            self.assertIn("acknowledged", json.loads(raw)["error"])
        self.server.read_only = True
        self.assertEqual(self.post(path="/api/publish-reviewed", body=body)[0], 403)
        self.assertFalse(self.private.exists())

    def test_queries_are_plain_strings_and_post_is_direct_json(self):
        service = EchoService()
        self.server.service = service
        status, _, raw = self.request(path="/api/memories?limit=2&q=%E4%B8%AD%E6%96%87&blank=")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(raw)["query"], {"limit": "2", "q": "中文", "blank": ""})
        status, _, raw = self.post(body={"acknowledged": True, "note": "合成请求"})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(raw), {"saved": True, "body": {"acknowledged": True, "note": "合成请求"}})
        self.assertEqual(service.writes[0][0], "/api/echo")

    def test_json_validation_size_type_and_chunked_are_rejected(self):
        service = EchoService()
        self.server.service = service
        for body in (b"not json", b'{"a":1,"a":2}', b'{"a":NaN}', b"\xff"):
            with self.subTest(body=body):
                status, _, raw = self.post(body=body)
                self.assertEqual(status, 400)
                self.assertIn("error", json.loads(raw))
        self.assertEqual(self.post(body=b"[]")[0], 422)
        self.assertEqual(self.post(headers={"Content-Type": "text/plain"})[0], 415)
        self.assertEqual(self.post(body=b"", headers={"Content-Length": str(memory_ui.MAX_BODY_BYTES + 1)})[0], 413)
        self.assertEqual(self.post(body=b"", headers={"Content-Length": "-1"})[0], 400)
        self.assertEqual(self.post(body=b"0\r\n\r\n", headers={"Transfer-Encoding": "chunked"})[0], 400)
        self.assertEqual(service.writes, [])

    def test_paths_queries_and_non_api_posts_cannot_escape_routes(self):
        self.server.service = EchoService()
        for route in ("/../secret", "/%2e%2e/secret", "/app.js/../secret", "/%255csecret", "/%2fapp.js", "/%00", "/%GG"):
            with self.subTest(route=route):
                self.assertIn(self.request(path=route)[0], (400, 404))
        self.assertEqual(self.request(path="http://evil.example/api/session")[0], 400)
        self.assertEqual(self.request(path="/api/echo?q=1&q=2")[0], 400)
        self.assertEqual(self.post(path="/app.js")[0], 404)
        self.assertEqual(self.post(path="/api/session")[0], 404)
        self.assertEqual(self.post(path="/api/echo?a=1")[0], 400)

    def test_duplicate_host_and_smuggled_lengths_are_rejected(self):
        for extra in (f"Host: {self.authority}\r\n", "Content-Length: 2\r\nContent-Length: 3\r\n"):
            with self.subTest(extra=extra):
                raw = (f"POST /api/echo HTTP/1.1\r\nHost: {self.authority}\r\nOrigin: {self.origin}\r\n"
                       f"X-CSRF-Token: {self.server.csrf_token}\r\nContent-Type: application/json\r\n{extra}\r\n{{}}").encode()
                with socket.create_connection(("127.0.0.1", self.server.server_address[1]), timeout=3) as stream:
                    stream.sendall(raw)
                    response = stream.recv(2048)
                self.assertIn(b"400", response.split(b"\r\n", 1)[0])

    def test_real_service_does_not_accept_truthy_acknowledgement(self):
        for acknowledged in (1, "true", None, False):
            with self.subTest(acknowledged=acknowledged):
                status, _, raw = self.post(path="/api/withdraw", body={"acknowledged": acknowledged,
                    "reviewer": "合成测试", "note": "不触及真实记录", "memory_id": "missing", "expected_revision": 1})
                self.assertEqual(status, 400)
                self.assertIn("acknowledged", json.loads(raw)["error"])
        self.assertFalse(self.private.exists())

    def test_errors_are_json_and_can_preserve_service_status(self):
        class Conflict(memory_store.MemoryError):
            status_code = 409

        class BadService(EchoService):
            def get(self, path, query):
                raise Conflict("记录已变化")

        self.server.service = BadService()
        status, _, raw = self.request(path="/api/anything")
        self.assertEqual(status, 409)
        self.assertEqual(json.loads(raw), {"error": "记录已变化"})
        self.assertEqual(self.request("OPTIONS")[0], 405)

    def test_server_never_binds_external_or_ambiguous_addresses(self):
        for host in ("0.0.0.0", "::", "192.168.1.2", "evil.example", "localhost.evil", " 127.0.0.1"):
            with self.subTest(host=host):
                with self.assertRaises(ValueError):
                    memory_ui.create_server(self.root, host=host, port=0)
        for value in (True, -1, 65536):
            with self.assertRaises(ValueError):
                memory_ui.create_server(self.root, port=value)
        with self.assertRaises(ValueError):
            memory_ui.create_server(self.root, port=0, read_only=1)


if __name__ == "__main__":
    unittest.main()
