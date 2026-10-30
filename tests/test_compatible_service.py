"""通过真实 HTTP 验证兼容清单、成组回源及待发布快照的切换。"""

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
from threading import Thread
import unittest
from unittest.mock import patch

import requests

from test_patch_reuse import make_snapshot, TEXTURE, TEXT, COLLECTION
from test_character_resources import test_directory
from patch_reuse import write_catalog
from resource_sources import save_json
from resource_maintenance import cleanup
from serve_resources import ResourceServer, restore_active_report


@contextmanager
def compatible_service(root, changed=False):
    """启动独立资源服务与官方 CDN 替身，所有产物限制在测试目录。"""
    old, catalog = make_snapshot(root)
    state = {"catalog_calls": 0, "fail": False}
    originals = {name: ("original-" + name).encode() for name in (TEXTURE, TEXT, COLLECTION)}
    if changed:
        originals[TEXTURE] = b"new-texture"
        catalog["BundleFiles"][0].update(Crc=hashlib.md5(originals[TEXTURE]).hexdigest(), Size=len(originals[TEXTURE]))

    class OfficialHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            """提供真实新版清单与相符的官方资源字节。"""
            if "/Catalog/" in self.path and self.path.endswith(".json"):
                state["catalog_calls"] += 1
                if state["fail"]:
                    self.send_error(503)
                    return
                content = json.dumps(catalog).encode()
            else:
                content = originals[Path(self.path).name]
            self.send_response(200)
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)

        def log_message(self, *args):
            """测试不输出官方替身的请求日志。"""
            pass

    with ThreadingHTTPServer(("127.0.0.1", 0), OfficialHandler) as official:
        official_thread = Thread(target=official.serve_forever, daemon=True)
        official_thread.start()
        upstream = f"http://127.0.0.1:{official.server_port}/prodm39"
        with patch("serve_resources.ROOT_DIR", root), patch("sync_character_resources.ROOT_DIR", root):
            with ResourceServer(("127.0.0.1", 0), root / old["output_dir"], upstream,
                                active_file=root / ".cache/local-server/active.json") as server:
                thread = Thread(target=server.serve_forever, daemon=True)
                thread.start()
                session = requests.Session()
                session.trust_env = False
                base = f"http://127.0.0.1:{server.server_port}"
                try:
                    yield server, session, base, state, old
                finally:
                    session.close()
                    server.shutdown()
                    thread.join()
        official.shutdown()
        official_thread.join()


class CompatibleServiceTests(unittest.TestCase):
    """直接验证游戏会收到哪些清单和包，而不只验证内部函数返回值。"""

    def test_new_version_retains_good_patches_and_falls_back_only_changed_family(self):
        """首次新版标记即提供混合清单，每个包的正文与清单校验一致。"""
        with test_directory() as temporary, compatible_service(Path(temporary), changed=True) as (server, session, base, state, old):
            catalog_url = base + "/prodm39/AssetBundles/Catalog/202610221737/Android/bundleDownloadInfo.json"
            self.assertEqual(session.get(catalog_url.replace(".json", ".hash"), timeout=5).status_code, 200)
            entries = session.get(catalog_url, timeout=5).json()["BundleFiles"]
            for entry in entries:
                url = base + "/prodm39/AssetBundles/Android/" + entry["Name"]
                response = session.get(url, timeout=5)
                self.assertEqual(hashlib.md5(response.content).hexdigest(), entry["Crc"])
                self.assertEqual(len(response.content), entry["Size"])
                expected_source = "patch" if entry["Name"] == COLLECTION else None
                self.assertEqual(response.headers.get("X-Resource-Source"), expected_source)
            health = session.get(base + "/health", timeout=5).json()
            self.assertEqual(health["resource_mode"], "mixed")
            self.assertEqual(state["catalog_calls"], 1)
            response = session.get(base + "/prodm39/AssetBundles/Catalog/202610131404/Android/bundleDownloadInfo.hash", timeout=5)
            self.assertEqual(response.status_code, 409)

    def test_pending_build_does_not_replace_manifest_or_files_until_next_hash(self):
        """构建完成后继续服务旧配套快照，下一次标记请求才统一发布新快照。"""
        with test_directory() as temporary, compatible_service(Path(temporary)) as (server, session, base, state, old):
            root = Path(temporary)
            catalog_url = base + "/prodm39/AssetBundles/Catalog/202610221737/Android/bundleDownloadInfo.json"
            hash_url = catalog_url.replace(".json", ".hash")
            session.get(hash_url, timeout=5).raise_for_status()
            before = session.get(catalog_url, timeout=5).content
            bundle_url = base + "/prodm39/AssetBundles/Android/" + COLLECTION
            previous_bytes = session.get(bundle_url, timeout=5).content
            full, _ = make_snapshot(root, version="202610221737", stamp="20261030T120000000000Z")
            output = root / full["output_dir"]
            newer = b"fresh-jp-patch"
            (output / "AssetBundles/Android" / COLLECTION).write_bytes(newer)
            record = next(r for r in full["bundles"] if r["name"] == COLLECTION)
            record.update(size=len(newer), md5=hashlib.md5(newer).hexdigest(), output_sha256=hashlib.sha256(newer).hexdigest())
            catalog = json.loads(next(output.glob("AssetBundles/Catalog/*/Android/*.json")).read_text())
            next(r for r in catalog["BundleFiles"] if r["Name"] == COLLECTION).update(Size=len(newer), Crc=record["md5"])
            write_catalog(output, "202610221737", catalog)
            save_json(output.parent / "report.json", full)
            server.stage_update(full)
            self.assertEqual(session.get(catalog_url, timeout=5).content, before)
            self.assertEqual(session.get(bundle_url, timeout=5).content, previous_bytes)
            # 重启仍恢复已发布快照，而不是直接加载后台最新报告。
            active = root / ".cache/local-server/active.json"
            restored = restore_active_report(full, active)
            self.assertEqual(restored["output_dir"], server.directory.relative_to(root).as_posix())
            cleanup(root, protect=server.protected_outputs(), apply=True)
            self.assertTrue(output.exists())
            session.get(hash_url, timeout=5).raise_for_status()
            self.assertEqual(session.get(bundle_url, timeout=5).content, newer)
            info = next(r for r in session.get(catalog_url, timeout=5).json()["BundleFiles"] if r["Name"] == COLLECTION)
            self.assertEqual(info["Crc"], hashlib.md5(newer).hexdigest())
            partial = session.get(bundle_url, headers={"Range": "bytes=1-4"}, timeout=5)
            self.assertEqual(partial.status_code, 206)
            self.assertEqual(partial.content, newer[1:5])

    def test_catalog_failure_retries_without_corrupting_old_snapshot(self):
        """官方清单失败时不猜测发布，也不改变已有客户端的文件。"""
        with test_directory() as temporary, compatible_service(Path(temporary)) as (server, session, base, state, old):
            state["fail"] = True
            catalog_url = base + "/prodm39/AssetBundles/Catalog/202610221737/Android/bundleDownloadInfo.hash"
            with self.assertLogs(level="ERROR"):
                self.assertEqual(session.get(catalog_url, timeout=5).status_code, 503)
            self.assertEqual(server.directory, Path(temporary) / old["output_dir"])
            state["fail"] = False
            self.assertEqual(session.get(catalog_url, timeout=5).status_code, 200)
            self.assertEqual(session.get(base + "/health", timeout=5).json()["resource_mode"], "mixed")

    def test_concurrent_catalog_requests_prepare_once(self):
        """多下载线程同时看到新版本时，只生成并发布一份兼容快照。"""
        with test_directory() as temporary, compatible_service(Path(temporary)) as (server, session, base, state, old):
            url = base + "/prodm39/AssetBundles/Catalog/202610221737/Android/bundleDownloadInfo.hash"
            def fetch(_):
                """每个客户端使用独立会话，模拟游戏并发检查。"""
                with requests.Session() as client:
                    client.trust_env = False
                    return client.get(url, timeout=5).content
            with ThreadPoolExecutor(4) as pool:
                responses = list(pool.map(fetch, range(4)))
            self.assertEqual(len(set(responses)), 1)
            self.assertEqual(state["catalog_calls"], 1)


if __name__ == "__main__":
    unittest.main()
