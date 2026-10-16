"""仅监听本机的经验管理页面；人工操作复用现有服务门槛，不调用模型。"""
from __future__ import annotations

import argparse
import hmac
import ipaddress
import json
from pathlib import Path
import re
import secrets
import socket
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qsl, unquote, urlsplit


MAX_BODY_BYTES = 3 * 1024 * 1024
STATIC_FILES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/evolution.js": ("evolution.js", "text/javascript; charset=utf-8"),
    "/styles.css": ("styles.css", "text/css; charset=utf-8"),
    "/favicon.svg": ("favicon.svg", "image/svg+xml"),
}
CSP = ("default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; "
       "img-src 'self'; font-src 'self'; base-uri 'none'; form-action 'self'; "
       "frame-ancestors 'none'; object-src 'none'")


class RequestError(ValueError):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def _loopback_host(host: str) -> str:
    if not isinstance(host, str) or host != host.strip():
        raise ValueError("监听地址必须是本机 loopback 地址。")
    if host.lower() == "localhost":
        return "127.0.0.1"
    try:
        address = ipaddress.ip_address(host)
    except ValueError as error:
        raise ValueError("只允许 localhost、127.0.0.1 或其他明确的 loopback IP。") from error
    if not address.is_loopback:
        raise ValueError("管理页面只允许监听本机 loopback，不能监听外网地址。")
    return str(address)


def _json_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise RequestError(f"JSON 字段重复：{key}")
        result[key] = value
    return result


def _invalid_number(value):
    raise RequestError(f"JSON 不允许非有限数字：{value}")


class MemoryHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    block_on_close = False

    def __init__(self, address, project_root: Path, read_only: bool):
        from memory_dashboard import DashboardService

        self.project_root = project_root
        self.read_only = read_only
        self.csrf_token = secrets.token_urlsafe(32)
        self.service = DashboardService(project_root)
        self.web_root = project_root / "web" / "memory"
        if ":" in address[0]:
            self.address_family = socket.AF_INET6
        super().__init__(address, MemoryHandler)
        host, port = self.server_address[:2]
        literal = f"[{host}]" if ":" in host else host
        self.allowed_hosts = {f"{literal}:{port}".lower(), f"localhost:{port}"}
        if port == 80:
            self.allowed_hosts.update((literal.lower(), "localhost"))

    def get_request(self):
        connection, address = super().get_request()
        connection.settimeout(10)
        return connection, address

    def verify_request(self, request, client_address):
        return ipaddress.ip_address(client_address[0]).is_loopback


class MemoryHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "TermMemoryLocal/1"
    sys_version = ""

    def log_message(self, format, *args):
        # 查询可能含私有文件路径，不把请求内容写到终端。
        pass

    def _send(self, status: int, body: bytes, content_type: str):
        self.close_connection = True
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy", CSP)
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        self.send_header("Connection", "close")
        self.end_headers()
        if self.command != "HEAD":
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

    def _json(self, status: int, value):
        body = json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
        self._send(status, body, "application/json; charset=utf-8")

    def _error(self, status: int, message: str):
        self._json(status, {"error": message})

    def send_error(self, code, message=None, explain=None):
        self._error(code, message or "HTTP 请求不合法。")

    def _header(self, name: str, *, required: bool = False) -> str | None:
        values = self.headers.get_all(name, [])
        if len(values) > 1 or (required and not values):
            raise RequestError(f"{name} 请求头缺失或重复。")
        return values[0] if values else None

    def _validate_origin(self, *, write: bool):
        host = self._header("Host", required=True)
        if host.lower() not in self.server.allowed_hosts:
            raise RequestError("Host 不属于此本机管理服务。", 403)
        origin = self._header("Origin")
        if write and origin is None:
            raise RequestError("写入请求缺少同源 Origin。", 403)
        if origin is not None and origin.lower() != "http://" + host.lower():
            raise RequestError("Origin 与本机页面不同源。", 403)

    def _target(self) -> tuple[str, dict[str, str]]:
        try:
            target = urlsplit(self.path)
            if target.scheme or target.netloc or target.fragment or not target.path.startswith("/"):
                raise RequestError("只接受本机相对请求路径。")
            if re.search(r"%(?![0-9a-fA-F]{2})", target.path + target.query):
                raise RequestError("请求路径的百分号编码不合法。")
            path = unquote(target.path, encoding="utf-8", errors="strict")
            if ("\\" in path or "%" in path or "//" in path
                    or any(ord(char) < 32 for char in path)
                    or any(part in (".", "..") for part in path.split("/"))):
                raise RequestError("请求路径不允许穿越目录或嵌套编码。", 404)
            pairs = parse_qsl(target.query, keep_blank_values=True, strict_parsing=True,
                              encoding="utf-8", errors="strict", max_num_fields=50)
            query = {}
            for key, value in pairs:
                if key in query:
                    raise RequestError(f"查询参数重复：{key}")
                query[key] = value
            return path, query
        except (UnicodeError, ValueError) as error:
            if isinstance(error, RequestError):
                raise
            raise RequestError(f"请求路径或查询参数不合法：{error}") from error

    def _body(self) -> dict:
        if self._header("Transfer-Encoding") is not None:
            raise RequestError("不支持 chunked 或其他 Transfer-Encoding。")
        length = self._header("Content-Length", required=True)
        if not re.fullmatch(r"[0-9]+", length):
            raise RequestError("Content-Length 必须是非负整数。")
        size = int(length)
        if size > MAX_BODY_BYTES:
            raise RequestError("JSON 请求超过 3 MiB。", 413)
        content_type = self._header("Content-Type", required=True)
        media_type = content_type.split(";", 1)[0].strip().lower()
        if media_type != "application/json":
            raise RequestError("POST 仅接受 application/json。", 415)
        raw = self.rfile.read(size)
        if len(raw) != size:
            raise RequestError("请求体长度与 Content-Length 不一致。")
        try:
            value = json.loads(raw.decode("utf-8"), object_pairs_hook=_json_pairs,
                               parse_constant=_invalid_number)
        except (UnicodeError, json.JSONDecodeError, RecursionError) as error:
            raise RequestError("请求体不是合法 UTF-8 JSON。") from error
        if not isinstance(value, dict):
            raise RequestError("JSON 请求体必须是对象。", 422)
        return value

    def _dispatch(self, *, write: bool):
        try:
            self._validate_origin(write=write)
            path, query = self._target()
            if write:
                if self.server.read_only:
                    raise RequestError("当前页面处于只读模式。", 403)
                token = self._header("X-CSRF-Token")
                if token is None or not hmac.compare_digest(token, self.server.csrf_token):
                    raise RequestError("CSRF Token 不匹配，请刷新页面。", 403)
                if not path.startswith("/api/") or path == "/api/session":
                    raise RequestError("不存在此写入接口。", 404)
                if query:
                    raise RequestError("POST 参数必须放在 JSON 请求体。")
                self._json(200, self.server.service.post(path, self._body()))
            elif path == "/api/session":
                self._json(200, {"csrf_token": self.server.csrf_token, "read_only": self.server.read_only})
            elif path == "/api" or path.startswith("/api/"):
                self._json(200, self.server.service.get(path, query))
            elif path in STATIC_FILES:
                name, content_type = STATIC_FILES[path]
                web_root = self.server.web_root.resolve()
                file = (web_root / name).resolve()
                if not file.is_relative_to(web_root) or not file.is_file():
                    raise RequestError("静态页面文件不存在。", 404)
                self._send(200, file.read_bytes(), content_type)
            else:
                raise RequestError("页面或接口不存在。", 404)
        except RequestError as error:
            self._error(error.status, str(error))
        except FileNotFoundError:
            self._error(404, "请求的数据不存在。")
        except (ValueError, TypeError, KeyError) as error:
            status = getattr(error, "status_code", 400)
            self._error(status if type(status) is int and status in (400, 404, 409, 422) else 400, str(error))
        except TimeoutError:
            self._error(408, "读取请求超时。")
        except OSError:
            self._error(500, "读取本机数据失败，请检查文件权限和服务状态。")

    def do_GET(self):
        self._dispatch(write=False)

    def do_POST(self):
        self._dispatch(write=True)

    def _unsupported(self):
        self._error(405, "仅支持 GET 和 POST。")

    do_HEAD = _unsupported
    do_OPTIONS = _unsupported
    do_PUT = _unsupported
    do_DELETE = _unsupported
    do_PATCH = _unsupported

    def handle_expect_100(self):
        self._error(417, "不支持 Expect 请求头。")
        return False


def create_server(project_root: Path, host: str = "127.0.0.1", port: int = 8765,
                  read_only: bool = False) -> MemoryHTTPServer:
    host = _loopback_host(host)
    if type(port) is not int or not 0 <= port <= 65535:
        raise ValueError("port 必须为 0 到 65535 的整数。")
    if type(read_only) is not bool:
        raise ValueError("read_only 必须是布尔值。")
    return MemoryHTTPServer((host, port), Path(project_root).resolve(), read_only)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="启动本机经验管理工作台；抽取和学习由 OpenCode agent 执行。")
    parser.add_argument("--project-root", type=Path, default=Path(__file__).resolve().parents[4])
    parser.add_argument("--host", default="127.0.0.1", help="只允许明确的 loopback 地址")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--read-only", action="store_true", help="只读查看，禁用全部 POST")
    parser.add_argument("--no-maintenance", action="store_true", help="不启用服务存活期间的经验冲突维护")
    args = parser.parse_args(argv)
    try:
        server = create_server(args.project_root, args.host, args.port, args.read_only)
    except (ValueError, OSError) as error:
        parser.error(str(error))
    host, port = server.server_address[:2]
    shown = f"[{host}]" if ":" in host else host
    print(f"本机经验管理页面：http://{shown}:{port}/（{'只读' if args.read_only else '人工操作可用'}）", flush=True)
    maintenance = None
    if not args.read_only and not args.no_maintenance:
        from memory_maintenance import start
        maintenance = start(args.project_root.resolve())
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        if maintenance is not None:
            maintenance.set()
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
