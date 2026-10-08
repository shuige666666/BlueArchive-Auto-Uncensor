"""本地测试资源服务：优先返回补丁，未修改资源转发至国服 CDN。"""

import argparse
import json
import mimetypes
import re
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit

import requests

from sync_character_resources import workspace_path


class ResourceServer(ThreadingHTTPServer):
    """保存当前构建快照和官方来源，运行期间不切换资源版本。"""

    daemon_threads = True

    def __init__(self, address, directory: Path, upstream: str, proxy=None):
        self.directory = directory.resolve()
        self.upstream = upstream.rstrip("/")
        self.proxy = proxy
        super().__init__(address, ResourceHandler)


def byte_range(header: str | None, size: int) -> tuple[int, int, bool]:
    """解析单段下载范围，支持客户端断点续传与后缀范围。"""
    if not header:
        return 0, size - 1, False
    match = re.fullmatch(r"bytes=(\d*)-(\d*)", header)
    if not match or not any(match.groups()):
        raise ValueError("不支持的 Range")
    first, last = match.groups()
    if first:
        start, end = int(first), min(int(last), size - 1) if last else size - 1
    else:
        start, end = max(0, size - int(last)), size - 1
    if start > end or start >= size:
        raise ValueError("Range 超出文件大小")
    return start, end, True


class ResourceHandler(BaseHTTPRequestHandler):
    """仅暴露 /prodm39 资源路径，避免提供项目源码和凭据文件。"""

    def do_GET(self):
        """下载补丁或转发官方资源。"""
        self.serve(head=False)

    def do_HEAD(self):
        """返回下载元数据，不传输文件正文。"""
        self.serve(head=True)

    def serve(self, head: bool):
        """验证路径后优先读取补丁，缺失时才访问官方 CDN。"""
        self.response_started = False
        parsed = urlsplit(self.path)
        path = unquote(parsed.path)
        if not path.startswith("/prodm39/"):
            self.send_error(404, "Expected /prodm39/")
            return
        relative = path[len("/prodm39/"):]
        # 保护 Windows 的反斜杠、盘符和父目录路径，不允许请求逃出补丁目录。
        if any(part in (".", "..") for part in relative.split("/")) or any(char in relative for char in ("\\", ":", "\x00")):
            self.send_error(400, "Invalid resource path")
            return
        target = (self.server.directory / relative).resolve()
        if not target.is_relative_to(self.server.directory):
            self.send_error(400, "Invalid resource path")
            return
        try:
            if target.is_file():
                self.local_file(target, head)
            else:
                self.forward(parsed, head)
        except requests.RequestException as exc:
            self.log_message("[UPSTREAM ERROR] %s", exc)
            if self.response_started:
                # 已开始下载后回源中断，应关闭连接，让客户端识别不完整文件并重试。
                self.close_connection = True
            else:
                self.send_error(502, "Official CDN unavailable")
        except (BrokenPipeError, ConnectionResetError):
            # 客户端取消下载时仅结束该请求，保持资源服务继续运行。
            self.log_message("[DISCONNECT] %s", self.path)

    def local_file(self, target: Path, head: bool):
        """流式返回已校验补丁，并为断点下载提供正确的 206 响应。"""
        size = target.stat().st_size
        try:
            start, end, partial = byte_range(self.headers.get("Range"), size)
        except ValueError:
            self.send_response(416)
            self.send_header("Content-Range", f"bytes */{size}")
            self.end_headers()
            return
        self.send_response(206 if partial else 200)
        self.send_header("Content-Type", mimetypes.guess_type(target.name)[0] or "application/octet-stream")
        self.send_header("Content-Length", str(end - start + 1))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Resource-Source", "patch")
        if partial:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        self.log_message("[PATCH] %s", self.path)
        if not head:
            with target.open("rb") as stream:
                stream.seek(start)
                remaining = end - start + 1
                while remaining > 0:
                    chunk = stream.read(min(1024 * 1024, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)

    def forward(self, parsed, head: bool):
        """保留原请求的范围与查询参数，将未修改资源流式转发给游戏。"""
        suffix = parsed.path[len("/prodm39"):]
        url = self.server.upstream + suffix + ("?" + parsed.query if parsed.query else "")
        headers = {"Accept-Encoding": "identity"}
        if self.headers.get("Range"):
            headers["Range"] = self.headers["Range"]
        with requests.Session() as session:
            # 测试服务使用指定代理，避免进程环境中的代理影响官方回源。
            session.trust_env = False
            if self.server.proxy:
                session.proxies.update(http=self.server.proxy, https=self.server.proxy)
            with session.request("HEAD" if head else "GET", url, headers=headers, stream=True, timeout=(15, 120)) as response:
                self.send_response(response.status_code)
                for name in ("Content-Type", "Content-Length", "Content-Range", "Accept-Ranges", "Content-Encoding", "Last-Modified", "ETag"):
                    if name in response.headers:
                        self.send_header(name, response.headers[name])
                self.send_header("Cache-Control", "no-cache")
                self.send_header("X-Resource-Source", "official")
                self.end_headers()
                self.response_started = True
                self.log_message("[OFFICIAL %s] %s", response.status_code, self.path)
                if not head:
                    for chunk in response.raw.stream(1024 * 1024, decode_content=False):
                        self.wfile.write(chunk)


def main() -> int:
    """读取最近成功构建，启动仅供本地模拟器测试的资源服务。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", default="reports/character-resources.json")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=18888)
    parser.add_argument("--proxy")
    args = parser.parse_args()
    report = json.loads(workspace_path(args.report).read_text(encoding="utf-8"))
    directory = workspace_path(report["output_dir"])
    if not directory.is_dir() or not report.get("structural_validation_passed"):
        raise ValueError("没有可提供的成功补丁构建，请先执行扫描构建。")
    with ResourceServer((args.host, args.port), directory, report["cn_source"]["root_url"], args.proxy) as server:
        print(f"[READY] http://{args.host}:{args.port}/prodm39", flush=True)
        print(f"[BUILD] {report['run_id']}；国服资源版本 {report['cn_source']['resource_version']}", flush=True)
        print("[INFO] PATCH 为本地补丁，OFFICIAL 为官方回源；Ctrl+C 停止服务。", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    raise SystemExit(main())
