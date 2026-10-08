"""验证模拟器测试服务的补丁优先、官方回源和断点下载。"""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
import unittest
import json

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

    def test_patch_priority_fallback_head_and_traversal(self):
        """真实 HTTP 请求应命中补丁、转发缺失资源，并拒绝目录越界。"""
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
            old = Path(directory) / "old"
            old_patch = old / "AssetBundles/Android/removed.bundle"
            old_patch.parent.mkdir(parents=True)
            old_patch.write_bytes(b"old-patch-must-not-be-used")
            with ThreadingHTTPServer(("127.0.0.1", 0), OfficialHandler) as official:
                official_thread = Thread(target=official.serve_forever, daemon=True)
                official_thread.start()
                upstream = f"http://127.0.0.1:{official.server_port}/prodm39"
                try:
                    with ResourceServer(("127.0.0.1", 0), Path(directory), upstream) as server:
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
