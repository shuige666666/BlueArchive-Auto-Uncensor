"""资源服务：优先返回补丁，未修改资源转发至国服 CDN，并定时更新。"""

import argparse
import json
import logging
from logging.handlers import RotatingFileHandler
import mimetypes
import re
import sys
import subprocess
from threading import Event, Lock, Thread
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit

import requests

from sync_character_resources import workspace_path
from resource_sources import ROOT_DIR, save_json


class ResourceServer(ThreadingHTTPServer):
    """请求使用固定快照，更新成功后切换，新旧请求不共享可变路径。"""

    daemon_threads = True

    def __init__(self, address, directory: Path, upstream: str, proxy=None, active_file=None):
        self.directory = directory.resolve()
        self.upstream = upstream.rstrip("/")
        self.proxy = proxy
        self.lock = Lock()
        self.history = []
        self.in_use = {}
        self.active_file = active_file
        self.update_status = "disabled"
        self.update_interval = None
        self.bundle_names = {self.directory: self.read_bundle_names(self.directory)}
        super().__init__(address, ResourceHandler)

    @staticmethod
    def read_bundle_names(directory):
        """记录当前清单中的文件名，避免补丁取消后误回退到旧补丁。"""
        names = set()
        for manifest in directory.glob("AssetBundles/Catalog/*/Android/bundleDownloadInfo.json"):
            names.update(entry["Name"] for entry in json.loads(manifest.read_text(encoding="utf-8"))["BundleFiles"])
        return names

    def protected_outputs(self):
        """保留当前、上一版和尚有请求使用的快照目录。"""
        return {self.directory, *(path for path, _ in self.history), *self.in_use}

    def persist_active(self):
        """保存清理保护目录，使独立清理命令也不会删除正在使用的版本。"""
        if self.active_file:
            save_json(self.active_file, {"protected_outputs": [path.relative_to(ROOT_DIR).as_posix()
                for path in sorted(self.protected_outputs())]})

    @contextmanager
    def lease(self):
        """在整个下载请求内固定资源版本，直到正文传输结束才释放。"""
        with self.lock:
            snapshots = [(self.directory, self.upstream), *self.history]
            for path, _ in snapshots:
                self.in_use[path] = self.in_use.get(path, 0) + 1
        try:
            yield snapshots
        finally:
            with self.lock:
                for path, _ in snapshots:
                    self.in_use[path] -= 1
                    if not self.in_use[path]:
                        del self.in_use[path]

    def activate(self, report):
        """只接收已校验的完整构建，保留上一版用于旧资源路径和回退。"""
        directory = workspace_path(report["output_dir"])
        if not report.get("structural_validation_passed") or not directory.is_dir():
            raise ValueError("更新产物没有通过结构校验，继续提供旧版。")
        names = self.read_bundle_names(directory)
        with self.lock:
            if directory != self.directory:
                self.history = [(self.directory, self.upstream)]
                self.directory, self.upstream = directory, report["cn_source"]["root_url"].rstrip("/")
                self.bundle_names[directory] = names
                logging.info("[SWITCH] %s", report["run_id"])
            self.persist_active()


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
        """固定请求快照后再处理路径和正文。"""
        with self.server.lease() as snapshots:
            self.snapshots = snapshots
            self.directory, self.upstream = snapshots[0]
            self.current_bundle_names = self.server.bundle_names[self.directory]
            self.handle_resource(head)

    def handle_resource(self, head: bool):
        """验证路径后优先读取补丁，缺失时才访问官方 CDN。"""
        self.response_started = False
        parsed = urlsplit(self.path)
        if parsed.path == "/health":
            content = json.dumps({"run_id": self.directory.parent.name,
                "update_status": self.server.update_status,
                "update_interval_seconds": self.server.update_interval}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            if not head:
                self.wfile.write(content)
            return
        path = unquote(parsed.path)
        if not path.startswith("/prodm39/"):
            self.send_error(404, "Expected /prodm39/")
            return
        relative = path[len("/prodm39/"):]
        # 保护 Windows 的反斜杠、盘符和父目录路径，不允许请求逃出补丁目录。
        if any(part in (".", "..") for part in relative.split("/")) or any(char in relative for char in ("\\", ":", "\x00")):
            self.send_error(400, "Invalid resource path")
            return
        target = (self.directory / relative).resolve()
        if not target.is_relative_to(self.directory):
            self.send_error(400, "Invalid resource path")
            return
        try:
            current_official = relative.startswith("AssetBundles/Android/") and Path(relative).name in self.current_bundle_names
            if not target.is_file() and not current_official:
                for directory, upstream in self.snapshots[1:]:
                    previous = (directory / relative).resolve()
                    if previous.is_relative_to(directory) and previous.is_file():
                        target = previous
                        break
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
        url = self.upstream + suffix + ("?" + parsed.query if parsed.query else "")
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

    def log_message(self, message, *args):
        """请求日志使用轮转文件，避免长时间运行无限增长。"""
        logging.info("%s %s", self.client_address[0], message % args)


def update_loop(server, args, stop):
    """独立工作进程完成更新，HTTP 服务持续响应；失败时保持当前版本。"""
    from resource_maintenance import cleanup, pipeline_lock
    log = ROOT_DIR / ".cache/local-server/update.log"
    while not stop.is_set():
        try:
            server.update_status = "checking"
            # 工作进程日志只在两轮更新之间轮转，不影响正在写日志的子进程。
            if log.exists() and log.stat().st_size > 5 * 1024 * 1024:
                for index in range(2, 0, -1):
                    source = log.with_name(log.name + f".{index}")
                    if source.exists():
                        source.replace(log.with_name(log.name + f".{index + 1}"))
                log.replace(log.with_name(log.name + ".1"))
            command = [sys.executable, "-u", str(ROOT_DIR / "scripts/resource_maintenance.py"),
                       "--update", "--workers", str(args.workers)]
            if args.proxy:
                command.extend(["--proxy", args.proxy])
            with log.open("a", encoding="utf-8") as stream:
                result = subprocess.run(command, cwd=ROOT_DIR, stdout=stream, stderr=subprocess.STDOUT)
            if result.returncode:
                raise RuntimeError(f"更新进程退出 {result.returncode}，详见 {log}")
            report = json.loads((ROOT_DIR / "reports/character-resources.json").read_text(encoding="utf-8"))
            server.activate(report)
            if not args.no_cleanup:
                with pipeline_lock(), server.lock:
                    cleaned = cleanup(protect=server.protected_outputs(), keep_previews=args.keep_previews, apply=True)
                    logging.info("[CLEANUP] 释放 %.1f MiB", cleaned["bytes"] / 2**20)
            server.update_status = "idle"
            logging.info("[UPDATE] 完成，本轮版本 %s", report["run_id"])
        except Exception:
            server.update_status = "failed"
            logging.exception("[UPDATE] 更新或清理失败；当前服务继续运行")
        if stop.wait(args.update_interval):
            break


def main() -> int:
    """读取最近成功构建，启动资源服务及可选的自动更新任务。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", default="reports/character-resources.json")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=18888)
    parser.add_argument("--proxy")
    parser.add_argument("--update-interval", type=float, default=86400, help="自动更新间隔（秒），默认每天")
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--no-update", action="store_true", help="仅提供资源，不启动自动更新")
    parser.add_argument("--no-cleanup", action="store_true", help="保留所有生成文件")
    parser.add_argument("--keep-previews", action="store_true", help="保留当前扫描和最近构建的预览")
    args = parser.parse_args()
    if args.update_interval <= 0 or args.workers < 1:
        parser.error("更新间隔和进程数必须大于 0。")
    log_dir = ROOT_DIR / ".cache/local-server"
    log_dir.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
        handlers=[RotatingFileHandler(log_dir / "service.log", maxBytes=5*1024*1024,
                  backupCount=3, encoding="utf-8"), logging.StreamHandler()])
    report_path = workspace_path(args.report)
    if not report_path.exists() and not args.no_update:
        from resource_maintenance import pipeline_lock, update_resources
        logging.info("[BOOTSTRAP] 尚无成功构建，先获取资源并初始化")
        with pipeline_lock():
            update_resources(args.proxy, args.workers)
        report_path = ROOT_DIR / "reports/character-resources.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    directory = workspace_path(report["output_dir"])
    if not directory.is_dir() or not report.get("structural_validation_passed"):
        raise ValueError("没有可提供的成功补丁构建，请先执行扫描构建。")
    stop = Event()
    with ResourceServer((args.host, args.port), directory, report["cn_source"]["root_url"], args.proxy,
                        log_dir / "active.json") as server:
        # 重启也保留上一份成功输出，避免旧版本资源路径立即失效。
        for candidate in sorted(directory.parent.parent.glob("*/report.json"), reverse=True):
            previous = json.loads(candidate.read_text(encoding="utf-8"))
            previous_dir = workspace_path(previous["output_dir"])
            if previous_dir != directory and previous.get("structural_validation_passed") and previous_dir.is_dir():
                server.history = [(previous_dir, previous["cn_source"]["root_url"].rstrip("/"))]
                break
        server.activate(report)
        server.update_interval = None if args.no_update else args.update_interval
        print(f"[READY] http://{args.host}:{args.port}/prodm39", flush=True)
        print(f"[BUILD] {report['run_id']}；国服资源版本 {report['cn_source']['resource_version']}", flush=True)
        print("[INFO] PATCH 为本地补丁，OFFICIAL 为官方回源；Ctrl+C 停止服务。", flush=True)
        if not args.no_update:
            Thread(target=update_loop, args=(server, args, stop), daemon=True).start()
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            stop.set()
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    raise SystemExit(main())
