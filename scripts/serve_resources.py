"""资源服务：优先返回补丁，未修改资源跳转至国服 CDN，并定时更新。"""

import argparse
import json
import logging
from logging.handlers import RotatingFileHandler
import mimetypes
import re
import sys
import subprocess
from datetime import datetime, timedelta, timezone
from threading import Event, Lock, Thread
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit

import requests

from sync_character_resources import workspace_path
from resource_sources import ROOT_DIR, save_json
from patch_reuse import prepare_compatible_patch


class ResourceServer(ThreadingHTTPServer):
    """请求使用固定快照，更新成功后切换，新旧请求不共享可变路径。"""

    daemon_threads = True

    def __init__(self, address, directory: Path, upstream: str, proxy=None, active_file=None, stream_official=False):
        self.directory = directory.resolve()
        self.upstream = upstream.rstrip("/")
        self.proxy = proxy
        self.stream_official = stream_official
        self.lock = Lock()
        self.prepare_lock = Lock()
        self.pending_report = None
        self.history = []
        self.in_use = {}
        self.active_file = active_file
        self.update_status = "disabled"
        self.update_interval = None
        self.catalog_versions = {self.directory: self.read_catalog_versions(self.directory)}
        # 新版清单回源后，后续无版本号的 Bundle 请求也必须使用官方资源。
        self.requested_version = None
        self.official_mode = False
        if active_file and active_file.exists():
            active = json.loads(active_file.read_text(encoding="utf-8"))
            self.requested_version = active.get("requested_version")
            self.official_mode = active.get("official_mode", False)
        super().__init__(address, ResourceHandler)

    @staticmethod
    def read_catalog_versions(directory):
        """只把清单与更新标记均已生成的版本视为可提供补丁。"""
        return {path.parent.parent.name
                for path in directory.glob("AssetBundles/Catalog/*/Android/bundleDownloadInfo.json")
                if path.with_suffix(".hash").is_file()}

    def allow_patch(self, directory, relative):
        """发现未适配的清单版本后全局回源，直到对应补丁构建完成。"""
        match = re.fullmatch(r"AssetBundles/Catalog/([^/]+)/Android/[^/]+", relative)
        with self.lock:
            versions = self.catalog_versions[directory]
            if match:
                # 数字版本按发布时间递增，迟到的旧版请求不覆盖已发现的新版本。
                version = match[1]
                if self.requested_version is None or version >= self.requested_version:
                    changed = version != self.requested_version
                    self.requested_version = version
                    # 构建完成后也先保持回源，直到客户端重新请求配套清单/标记。
                    if Path(relative).name in ("bundleDownloadInfo.json", "bundleDownloadInfo.hash"):
                        self.official_mode = version not in versions
                    elif version not in versions:
                        self.official_mode = True
                    self.persist_active()
                    if changed:
                        logging.info("[CATALOG] 请求版本 %s", version)
            allowed = bool(versions) and not self.official_mode and (self.requested_version is None or self.requested_version in versions)
            # 旧版清单不能回源后再配上当前补丁；不支持的旧版本提示客户端重试。
            if match and allowed and match[1] not in versions:
                return None
            return allowed

    def protected_outputs(self):
        """保留当前、上一版和尚有请求使用的快照目录。"""
        pending = {workspace_path(self.pending_report["output_dir"])} if self.pending_report else set()
        return {self.directory, *(path for path, _ in self.history), *self.in_use, *pending}

    def persist_active(self):
        """保存清理保护目录，使独立清理命令也不会删除正在使用的版本。"""
        if self.active_file:
            save_json(self.active_file, {"requested_version": self.requested_version,
                "official_mode": self.official_mode,
                "output_dir": self.directory.relative_to(ROOT_DIR).as_posix(),
                "protected_outputs": [path.relative_to(ROOT_DIR).as_posix()
                for path in sorted(self.protected_outputs())]})

    def stage_update(self, report):
        """后台产物先待发布，客户端重新读取配套清单时才切换文件。"""
        directory = workspace_path(report["output_dir"])
        if not report.get("structural_validation_passed") or not directory.is_dir():
            raise ValueError("更新产物未通过校验")
        with self.lock:
            if directory != self.directory and (self.requested_version is None or
                    report["cn_source"]["resource_version"] >= self.requested_version):
                self.pending_report = report
                self.persist_active()

    def prepare_catalog(self, version, primary):
        """首次访问新版本时只生成兼容清单，不等待纹理重新编码。"""
        with self.prepare_lock:
            with self.lock:
                if self.requested_version and version < self.requested_version:
                    return
                pending = self.pending_report
            if primary and pending and version in self.read_catalog_versions(workspace_path(pending["output_dir"])):
                self.activate(pending)
                with self.lock:
                    self.pending_report = None
                    self.persist_active()
            if version in self.catalog_versions[self.directory]:
                return
            # 没有可追溯报告时维持原有官方回退，不能凭文件名猜测兼容性。
            with self.lease() as snapshots:
                directory, upstream = snapshots[0]
                path = directory.parent / "report.json"
                if not path.is_file():
                    return
                previous = json.loads(path.read_text(encoding="utf-8"))
                if not previous.get("structural_validation_passed") or workspace_path(previous["output_dir"]) != directory:
                    return
                url = f"{upstream}/AssetBundles/Catalog/{version}/Android/bundleDownloadInfo.json"
                with requests.Session() as session:
                    session.trust_env = False
                    if self.proxy:
                        session.proxies.update(http=self.proxy, https=self.proxy)
                    response = session.get(url, timeout=(15, 60))
                    response.raise_for_status()
                    report = prepare_compatible_patch(previous, response.json(), version, upstream, root=ROOT_DIR)
                self.activate(report)
                logging.info("[COMPATIBLE] 版本 %s，复用 %s 个包，%s 个包暂用官方", version,
                             report["build_stats"]["reused_bundles"], report["build_stats"]["official_bundles"])

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
        versions = self.read_catalog_versions(directory)
        with self.lock:
            if directory != self.directory:
                self.history = [(self.directory, self.upstream)]
                self.directory, self.upstream = directory, report["cn_source"]["root_url"].rstrip("/")
                self.catalog_versions[directory] = versions
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


def normalize_resource_path(path: str) -> str:
    """清理配置 URL 末尾带入的 ASCII 空白和控制字符，不修改资源文件名。"""
    prefix = "/prodm39"
    if not path.startswith(prefix):
        return path
    # 只清理固定前缀与资源分隔符之间的字符，NUL 仍交给路径校验拒绝。
    suffix = re.sub(r"^(?:%0[1-9a-f]|%1[0-9a-f]|%20|%7f|[\x01-\x20\x7f])+(?=/)",
                    "", path[len(prefix):], flags=re.IGNORECASE)
    return prefix + suffix


class ResourceHandler(BaseHTTPRequestHandler):
    """仅暴露 /prodm39 资源路径，避免提供项目源码和凭据文件。"""

    def do_GET(self):
        """下载补丁，或引导客户端下载官方资源。"""
        self.serve(head=False)

    def do_HEAD(self):
        """返回下载元数据，不传输文件正文。"""
        self.serve(head=True)

    def serve(self, head: bool):
        """固定请求快照后再处理路径和正文。"""
        path = normalize_resource_path(urlsplit(self.path).path)
        catalog = re.fullmatch(r"/prodm39/AssetBundles/Catalog/(\d{12})/Android/([^/]+)", path)
        if catalog:
            try:
                # 只在标记请求发布待更新快照，避免 hash 与随后 JSON 请求之间切换。
                self.server.prepare_catalog(catalog[1], catalog[2] == "bundleDownloadInfo.hash")
            except (requests.RequestException, OSError, ValueError, KeyError):
                # 新清单取不到时让客户端重试，不发布无法证明配套的旧补丁。
                logging.exception("[CATALOG ERROR] 无法准备版本 %s", catalog[1])
                self.send_error(503, "Resource catalog preparation failed; retry later")
                return
        with self.server.lease() as snapshots:
            self.snapshots = snapshots
            self.directory, self.upstream = snapshots[0]
            self.handle_resource(head)

    def handle_resource(self, head: bool):
        """验证路径后优先读取补丁，缺失时才访问官方 CDN。"""
        self.response_started = False
        parsed = urlsplit(self.path)
        normalized = normalize_resource_path(parsed.path)
        if normalized != parsed.path:
            self.log_message("[NORMALIZE] %s -> %s", parsed.path, normalized)
            # 本地匹配与 CDN 跳转必须使用同一条修正后的路径。
            parsed = parsed._replace(path=normalized)
        if parsed.path == "/health":
            with self.server.lock:
                requested_version = self.server.requested_version
                official_mode = self.server.official_mode
                pending = self.server.pending_report
            versions = self.server.catalog_versions[self.directory]
            report_file = self.directory.parent / "report.json"
            snapshot = json.loads(report_file.read_text(encoding="utf-8")) if report_file.is_file() else {}
            prepared_mode = "mixed" if snapshot.get("partial") else "patch"
            if snapshot.get("partial") and not snapshot.get("bundles"):
                prepared_mode = "official"
            content = json.dumps({"run_id": self.directory.parent.name,
                "update_status": self.server.update_status,
                "update_interval_seconds": self.server.update_interval,
                "requested_resource_version": requested_version,
                "patch_resource_versions": sorted(versions),
                "ready_resource_versions": sorted(self.server.read_catalog_versions(workspace_path(pending["output_dir"])))
                    if pending else [],
                "resource_mode": prepared_mode if versions and not official_mode and (requested_version is None or requested_version in versions)
                    else "official"}).encode()
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
            allowed = self.server.allow_patch(self.directory, relative)
            if allowed is None:
                self.send_error(409, "Resource catalog superseded; restart the game to refresh")
                return
            if not allowed:
                self.forward(parsed, head)
                return
            # 历史快照只保护已开始的下载，不再用于新请求的补丁兜底。
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
        """未修改资源默认由客户端下载，兼容模式才经本服务转发。"""
        suffix = parsed.path[len("/prodm39"):]
        url = self.upstream + suffix + ("?" + parsed.query if parsed.query else "")
        if not self.server.stream_official:
            # 官方大包直接走 CDN，避免服务器带宽使客户端下载超时后从头重试。
            self.send_response(302)
            self.send_header("Location", url)
            self.send_header("Content-Length", "0")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("X-Resource-Source", "official")
            self.end_headers()
            self.log_message("[REDIRECT] %s", self.path)
            return
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


def next_weekly_check(now):
    """计算北京时间下一个周四 16 点或 18 点，不依赖宿主机时区。"""
    beijing = now.astimezone(timezone(timedelta(hours=8)))
    thursday = beijing.date() + timedelta(days=(3 - beijing.weekday()) % 7)
    for days in (0, 7):
        for hour in (16, 18):
            candidate = datetime.combine(thursday + timedelta(days=days), datetime.min.time(), beijing.tzinfo).replace(hour=hour)
            if candidate > beijing:
                return candidate.astimezone(timezone.utc)


def restore_active_report(latest, active_path):
    """重启恢复最后实际发布的快照，避免待发布构建中途替换客户端文件。"""
    if active_path.exists():
        active = json.loads(active_path.read_text(encoding="utf-8"))
        if active.get("output_dir"):
            directory = workspace_path(active["output_dir"])
            path = directory.parent / "report.json"
            if path.is_file():
                report = json.loads(path.read_text(encoding="utf-8"))
                if report.get("structural_validation_passed") and workspace_path(report["output_dir"]) == directory:
                    return report
    return latest


def update_wait_seconds(interval, started, finished):
    """兼顾日常间隔和周四检查，运行期间错过的检查只补一次。"""
    return min(interval, max(0, (next_weekly_check(started) - finished).total_seconds()))


def update_loop(server, args, stop):
    """独立工作进程完成更新，HTTP 服务持续响应；失败时保持当前版本。"""
    from resource_maintenance import cleanup, pipeline_lock
    log = ROOT_DIR / ".cache/local-server/update.log"
    while not stop.is_set():
        started = datetime.now(timezone.utc)
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
            server.stage_update(report)
            if not args.no_cleanup:
                with pipeline_lock(), server.lock:
                    cleaned = cleanup(protect=server.protected_outputs(), keep_previews=args.keep_previews, apply=True)
                    logging.info("[CLEANUP] 释放 %.1f MiB", cleaned["bytes"] / 2**20)
            server.update_status = "idle"
            logging.info("[UPDATE] 完成，本轮版本 %s", report["run_id"])
        except Exception:
            server.update_status = "failed"
            logging.exception("[UPDATE] 更新或清理失败；当前服务继续运行")
        # 日常每 24 小时兜底；构建跨过周四检查时刻时，结束后补查一次。
        delay = update_wait_seconds(args.update_interval, started, datetime.now(timezone.utc))
        if stop.wait(delay):
            break


def main() -> int:
    """读取最近成功构建，启动资源服务及可选的自动更新任务。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", default="reports/character-resources.json")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=18888)
    parser.add_argument("--proxy")
    parser.add_argument("--stream-official", action="store_true", help="兼容模式：经服务器转发官方资源，默认跳转 CDN")
    parser.add_argument("--update-interval", type=float, default=86400, help="日常更新间隔（秒），另在北京时间周四 16/18 点检查")
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
    latest = json.loads(report_path.read_text(encoding="utf-8"))
    report = restore_active_report(latest, log_dir / "active.json")
    directory = workspace_path(report["output_dir"])
    if not directory.is_dir() or not report.get("structural_validation_passed"):
        raise ValueError("没有可提供的成功补丁构建，请先执行扫描构建。")
    stop = Event()
    with ResourceServer((args.host, args.port), directory, report["cn_source"]["root_url"], args.proxy,
                        log_dir / "active.json", stream_official=args.stream_official) as server:
        # 重启也保留上一份成功输出，避免旧版本资源路径立即失效。
        for candidate in sorted(directory.parent.parent.glob("*/report.json"), reverse=True):
            previous = json.loads(candidate.read_text(encoding="utf-8"))
            previous_dir = workspace_path(previous["output_dir"])
            if previous_dir != directory and previous.get("structural_validation_passed") and previous_dir.is_dir():
                server.history = [(previous_dir, previous["cn_source"]["root_url"].rstrip("/"))]
                break
        server.activate(report)
        server.stage_update(latest)
        server.update_interval = None if args.no_update else args.update_interval
        print(f"[READY] http://{args.host}:{args.port}/prodm39", flush=True)
        print(f"[BUILD] {report['run_id']}；国服资源版本 {report['cn_source']['resource_version']}", flush=True)
        print("[INFO] PATCH 为本地补丁，REDIRECT 为官方 CDN 跳转，OFFICIAL 为兼容模式回源；Ctrl+C 停止服务。", flush=True)
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
