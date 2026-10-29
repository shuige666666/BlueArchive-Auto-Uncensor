"""验证资源服务的补丁优先、CDN 跳转、兼容回源和断点下载。"""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
import unittest
import json
import hashlib

import requests

from test_character_resources import test_directory
from serve_resources import ResourceServer, byte_range


class ResourceServerTests(unittest.TestCase):
    """使用本机临时 HTTP 服务验证实际响应，不访问公网。"""

    def test_range_boundaries(self):
        """正确处理完整文件、后缀下载和无效范围。"""
        self.assertEqual(byte_range(None, 6), (0, 5, False))
        self.assertEqual(byte_range("bytes=-2", 6), (4, 5, True))
        self.assertEqual(byte_range("bytes=2-99", 6), (2, 5, True))
        with self.assertRaises(ValueError):
            byte_range("bytes=6-", 6)

    def test_new_catalog_uses_official_bundles_until_ready_catalog_is_requested(self):
        """复现同名包跨版本变化，验证回源、重启、失败更新及恢复后的清单一致性。"""
        name = "same.bundle"
        official_bytes = b"official-new"
        new_patch = b"new-patched-content"

        def manifest(content):
            """生成与实际响应一致的最小下载清单。"""
            return {"BundleFiles": [{"Name": name, "Size": len(content),
                "Crc": hashlib.md5(content).hexdigest()}]}

        class OfficialHandler(BaseHTTPRequestHandler):
            def do_GET(self):
                """模拟新版官方清单、更新标记和同名资源包。"""
                content = json.dumps(manifest(official_bytes)).encode() if self.path.endswith(".json") else (
                    b"official-hash" if self.path.endswith(".hash") else official_bytes)
                self.send_response(200)
                self.send_header("Content-Length", str(len(content)))
                self.end_headers()
                self.wfile.write(content)

            def log_message(self, *args):
                """隐藏测试上游的访问日志。"""
                pass

        with test_directory() as temporary:
            root = Path(temporary)
            old, new = root / "old", root / "new"
            for directory, version, content in ((old, "202610010000", b"old-patch"),
                                                (new, "202610220000", new_patch)):
                target = directory / "AssetBundles/Android" / name
                target.parent.mkdir(parents=True)
                target.write_bytes(content)
                info = directory / f"AssetBundles/Catalog/{version}/Android/bundleDownloadInfo.json"
                info.parent.mkdir(parents=True)
                info.write_text(json.dumps(manifest(content)))
                info.with_suffix(".hash").write_text("1")
            active = root / "active.json"
            with ThreadingHTTPServer(("127.0.0.1", 0), OfficialHandler) as official:
                upstream = f"http://127.0.0.1:{official.server_port}/prodm39"
                official_thread = Thread(target=official.serve_forever, daemon=True)
                official_thread.start()
                try:
                    with ResourceServer(("127.0.0.1", 0), old, upstream, active_file=active) as server:
                        thread = Thread(target=server.serve_forever, daemon=True)
                        thread.start()
                        try:
                            base = f"http://127.0.0.1:{server.server_port}"
                            bundle = base + "/prodm39/AssetBundles/Android/" + name
                            catalog = base + "/prodm39/AssetBundles/Catalog/202610220000/Android/bundleDownloadInfo.json"
                            with requests.Session() as session:
                                session.trust_env = False
                                self.assertEqual(session.get(bundle, timeout=5).content, b"old-patch")
                                # 游戏先请求新版标记，之后所有新下载必须跟随官方清单。
                                response = session.get(catalog.replace(".json", ".hash"), timeout=5)
                                self.assertEqual(response.content, b"official-hash")
                                info = session.get(catalog, timeout=5).json()["BundleFiles"][0]
                                response = session.get(bundle, timeout=5)
                                self.assertEqual(len(response.content), info["Size"])
                                self.assertEqual(hashlib.md5(response.content).hexdigest(), info["Crc"])
                                self.assertEqual(session.get(base + "/health", timeout=5).json()["resource_mode"], "official")
                                with ResourceServer(("127.0.0.1", 0), old, upstream, active_file=active) as restarted:
                                    self.assertFalse(restarted.allow_patch(old.resolve(), "AssetBundles/Android/" + name))
                                report = {"output_dir": str(new), "structural_validation_passed": False,
                                          "cn_source": {"root_url": upstream}, "run_id": "new"}
                                with self.assertRaises(ValueError):
                                    server.activate(report)
                                self.assertEqual(session.get(bundle, timeout=5).content, official_bytes)
                                report["structural_validation_passed"] = True
                                server.activate(report)
                                # 构建完成不立刻改变仍按官方清单下载的请求。
                                self.assertEqual(session.get(bundle, timeout=5).content, official_bytes)
                                info = session.get(catalog, timeout=5).json()["BundleFiles"][0]
                                response = session.get(bundle, timeout=5)
                                self.assertEqual(response.content, new_patch)
                                self.assertEqual(len(response.content), info["Size"])
                                self.assertEqual(hashlib.md5(response.content).hexdigest(), info["Crc"])
                                response = session.get(bundle, headers={"Range": "bytes=2-5"}, timeout=5)
                                self.assertEqual(response.status_code, 206)
                                self.assertEqual(response.content, new_patch[2:6])
                                # 迟到的旧版清单不应被回源后与新补丁混用。
                                response = session.get(catalog.replace("202610220000", "202610010000"), timeout=5)
                                self.assertEqual(response.status_code, 409)
                        finally:
                            server.shutdown()
                            thread.join()
                finally:
                    official.shutdown()
                    official_thread.join()

    def test_patch_priority_fallback_head_and_traversal(self):
        """真实 HTTP 请求验证补丁、回源与 CDN 跳转，并拒绝目录越界。"""
        calls = []

        class OfficialHandler(BaseHTTPRequestHandler):
            def do_GET(self):
                """记录回源路径与下载范围，并模拟官方 CDN。"""
                calls.append((self.path, self.headers.get("Range")))
                self.send_response(200)
                self.send_header("Content-Length", "8")
                self.end_headers()
                self.wfile.write(b"official")

            def log_message(self, *args):
                """测试不输出模拟上游的请求日志。"""
                pass

        with test_directory() as directory:
            patch_file = Path(directory) / "AssetBundles/test.bundle"
            patch_file.parent.mkdir()
            patch_file.write_bytes(b"patched")
            manifest = Path(directory) / "AssetBundles/Catalog/v/Android/bundleDownloadInfo.json"
            manifest.parent.mkdir(parents=True)
            manifest.write_text(json.dumps({"BundleFiles": [{"Name": "removed.bundle"}]}))
            manifest.with_suffix(".hash").write_text("1")
            old = Path(directory) / "old"
            old_patch = old / "AssetBundles/Android/removed.bundle"
            old_patch.parent.mkdir(parents=True)
            old_patch.write_bytes(b"old-patch-must-not-be-used")
            with ThreadingHTTPServer(("127.0.0.1", 0), OfficialHandler) as official:
                official_thread = Thread(target=official.serve_forever, daemon=True)
                official_thread.start()
                upstream = f"http://127.0.0.1:{official.server_port}/prodm39"
                try:
                    with ResourceServer(("127.0.0.1", 0), Path(directory), upstream, stream_official=True) as server:
                        server.history = [(old, upstream)]
                        thread = Thread(target=server.serve_forever, daemon=True)
                        thread.start()
                        try:
                            root = f"http://127.0.0.1:{server.server_port}/prodm39"
                            with requests.Session() as session:
                                session.trust_env = False
                                response = session.get(root + "/AssetBundles/test.bundle", timeout=5)
                                self.assertEqual(response.content, b"patched")
                                self.assertEqual(response.headers["X-Resource-Source"], "patch")
                                self.assertFalse(calls)
                                response = session.get(root + "/AssetBundles/test.bundle", headers={"Range": "bytes=2-4"}, timeout=5)
                                self.assertEqual(response.status_code, 206)
                                self.assertEqual(response.content, b"tch")
                                self.assertEqual(response.headers["Content-Range"], "bytes 2-4/7")
                                response = session.head(root + "/AssetBundles/test.bundle", timeout=5)
                                self.assertEqual(response.content, b"")
                                self.assertEqual(response.headers["Content-Length"], "7")
                                response = session.get(root + "/AssetBundles/other.bundle?q=1", headers={"Range": "bytes=0-"}, timeout=5)
                                self.assertEqual(response.content, b"official")
                                self.assertEqual(response.headers["X-Resource-Source"], "official")
                                self.assertEqual(calls, [("/prodm39/AssetBundles/other.bundle?q=1", "bytes=0-")])
                                response = session.get(root + "/AssetBundles/Android/removed.bundle", timeout=5)
                                self.assertEqual(response.content, b"official")
                                self.assertEqual(response.headers["X-Resource-Source"], "official")
                                # 默认跳转不经过服务回源，跟随跳转仍应保留断点和查询参数。
                                server.stream_official = False
                                calls.clear()
                                redirect_path = root + "/AssetBundles/other.bundle?q=1"
                                response = session.get(redirect_path, allow_redirects=False, timeout=5)
                                self.assertEqual(response.status_code, 302)
                                self.assertEqual(response.headers["Location"], upstream + "/AssetBundles/other.bundle?q=1")
                                self.assertEqual(response.content, b"")
                                self.assertFalse(calls)
                                response = session.head(redirect_path, allow_redirects=False, timeout=5)
                                self.assertEqual(response.status_code, 302)
                                self.assertEqual(response.headers["Content-Length"], "0")
                                response = session.get(redirect_path, headers={"Range": "bytes=2-"}, timeout=5)
                                self.assertEqual(response.content, b"official")
                                self.assertEqual(calls, [("/prodm39/AssetBundles/other.bundle?q=1", "bytes=2-")])
                                response = session.get(root + "/AssetBundles/test.bundle", timeout=5)
                                self.assertEqual(response.content, b"patched")
                                self.assertEqual(response.headers["X-Resource-Source"], "patch")
                                # 配置末尾混入隐藏字符时，补丁和官方跳转都应使用修正路径。
                                for trailing in ("%13", "%0D", "%0a", "%20", "%09", "%20%0D%0A%13"):
                                    with self.subTest(trailing=trailing):
                                        response = session.get(root + trailing + "/AssetBundles/test.bundle",
                                            headers={"Range": "bytes=2-4"}, timeout=5)
                                        self.assertEqual(response.status_code, 206)
                                        self.assertEqual(response.content, b"tch")
                                        response = session.head(root + trailing + "/AssetBundles/test.bundle", timeout=5)
                                        self.assertEqual(response.status_code, 200)
                                        self.assertEqual(response.headers["Content-Length"], "7")
                                        response = session.get(root + trailing + "/AssetBundles/other.bundle?q=x%20y",
                                            allow_redirects=False, timeout=5)
                                        self.assertEqual(response.status_code, 302)
                                        self.assertEqual(response.headers["Location"], upstream + "/AssetBundles/other.bundle?q=x%20y")
                                server.stream_official = True
                                calls.clear()
                                response = session.get(root + "%13%0D%20/AssetBundles/other.bundle?q=1", timeout=5)
                                self.assertEqual(response.content, b"official")
                                self.assertEqual(calls, [("/prodm39/AssetBundles/other.bundle?q=1", None)])
                                server.stream_official = False
                                # 仅清理前缀边界，文件名里的空格、错误前缀与越界路径不被改写。
                                response = session.get(root + "/AssetBundles/some%20file.bundle", allow_redirects=False, timeout=5)
                                self.assertEqual(response.headers["Location"], upstream + "/AssetBundles/some%20file.bundle")
                                response = session.get(root + "x%13/AssetBundles/test.bundle", timeout=5)
                                self.assertEqual(response.status_code, 404)
                                response = session.get(root + "%13/%2e%2e%2frequirements.txt", timeout=5)
                                self.assertEqual(response.status_code, 400)
                                response = session.get(root + "%00/AssetBundles/test.bundle", timeout=5)
                                self.assertEqual(response.status_code, 404)
                                response = session.get(root + "/%2e%2e%2frequirements.txt", timeout=5)
                                self.assertEqual(response.status_code, 400)
                        finally:
                            server.shutdown()
                            thread.join()
                finally:
                    official.shutdown()
                    official_thread.join()


if __name__ == "__main__":
    unittest.main()
