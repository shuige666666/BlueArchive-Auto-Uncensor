"""协调资源更新和缓存清理，失败时保留上一份成功构建。"""

import argparse
from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import shutil
import sys
from datetime import datetime, timezone
import zlib

from resource_sources import ROOT_DIR, save_json, sha256_file

RUN_NAME = re.compile(r"\d{8}T\d{12}Z")


@contextmanager
def pipeline_lock(root=ROOT_DIR):
    """用操作系统文件锁互斥扫描、构建和清理，进程退出后自动释放。"""
    path = root / ".cache/resource-pipeline.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as stream:
        if path.stat().st_size == 0:
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise RuntimeError("已有扫描、构建或清理任务运行，本轮跳过。") from exc
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == "nt":
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream, fcntl.LOCK_UN)


def cleanup(root=ROOT_DIR, protect=(), keep=2, keep_previews=False, apply=False):
    """保留成功版本及使用中的目录，只清理明确的生成文件，不触碰源码和覆盖备份。"""
    root = root.resolve()
    build_root = root / "build/character-resources"
    protected = {Path(path).resolve() for path in protect}
    active_path = root / ".cache/local-server/active.json"
    if active_path.exists():
        active = json.loads(active_path.read_text(encoding="utf-8"))
        protected.update((root / path).resolve() for path in active.get("protected_outputs", []))
    candidates = []
    successful = []
    if build_root.exists():
        for directory in build_root.iterdir():
            if not RUN_NAME.fullmatch(directory.name) or not directory.is_dir():
                continue
            report_path = directory / "report.json"
            if report_path.exists():
                report = json.loads(report_path.read_text(encoding="utf-8"))
                if report.get("structural_validation_passed"):
                    successful.append(directory)
        retained = set(sorted(successful, reverse=True)[:keep])
        retained.update(directory for directory in build_root.iterdir()
                        if any(path.is_relative_to(directory.resolve()) for path in protected))
        for directory in build_root.iterdir():
            if not RUN_NAME.fullmatch(directory.name) or not directory.is_dir():
                continue
            if directory not in retained:
                candidates.append(directory)
            elif not keep_previews and (directory / "previews").exists():
                candidates.append(directory / "previews")
    cache = root / ".cache/character-resources"
    staged = cache / "staged"
    if staged.exists():
        candidates.extend(path for path in staged.iterdir() if RUN_NAME.fullmatch(path.name))
    previews = cache / "scan-previews"
    if previews.exists():
        if keep_previews:
            state = json.loads((cache / "scan-state.json").read_text(encoding="utf-8"))
            referenced = {asset["preview_dir"] for pair in state["pairs"].values()
                          for asset in pair["result"]["assets"] if "preview_dir" in asset}
            candidates.extend(path for path in previews.iterdir()
                              if path.relative_to(root).as_posix() not in referenced)
        else:
            candidates.append(previews)
    # 这些目录是早期探测和重复工具缓存，正式下载工具及 replacement 备份始终保留。
    candidates.extend(path for path in (root / ".cache/probe", root / ".cache/tools", cache / "probe-shun") if path.exists())
    # 国服保留最近两个资源版本；日服保留当前清单仍引用的原包，淘汰历史条目。
    cn_source = cache / "cn-source.json"
    cn_root = cache / "cn"
    if cn_source.exists() and cn_root.exists():
        current = json.loads(cn_source.read_text(encoding="utf-8"))["resource_version"]
        versions = sorted((path for path in cn_root.iterdir() if path.is_dir()), reverse=True)
        retained_cn = {cn_root / current, *versions[:2]}
        candidates.extend(path for path in versions if path not in retained_cn)
    index_path = cache / "jp-file-index.json"
    catalog_path = cache / "jp-catalog.bytes"
    jp_root = cache / "jp"
    updated_index = None
    if index_path.exists() and catalog_path.exists() and jp_root.exists():
        from jp_catalog import read_jp_catalog
        catalog = {entry["Name"]: [entry["Size"], entry["Crc"]] for entry in read_jp_catalog(catalog_path)}
        index = json.loads(index_path.read_text(encoding="utf-8"))
        updated_index = {name: entry for name, entry in index.items() if catalog.get(name) == entry["fingerprint"]}
        retained_jp = {(root / entry["file"]).resolve() for entry in updated_index.values()}
        if any(not path.is_relative_to(jp_root.resolve()) for path in retained_jp):
            raise ValueError("日服缓存索引指向原包目录之外。")
        candidates.extend(path for path in jp_root.rglob("*.bundle") if path.resolve() not in retained_jp)
    removed, total = [], 0
    for path in candidates:
        resolved = path.resolve()
        if not resolved.is_relative_to(root) or resolved == root or resolved != path.absolute() or path.is_symlink():
            raise ValueError(f"清理目标越界或是符号链接: {path}")
        if any(locked.is_relative_to(resolved) for locked in protected):
            continue
        # 删除前逐项确认链接没有指向工作区外；仅针对上面列出的生成目录。
        files = list(path.rglob("*")) if path.is_dir() else [path]
        if any(item.is_symlink() or not item.resolve().is_relative_to(resolved) for item in files):
            raise ValueError(f"生成目录含符号链接，停止清理: {path}")
        total += sum(item.stat().st_size for item in files if item.is_file())
        removed.append(path.relative_to(root).as_posix())
        if apply:
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink()
    if apply and updated_index is not None:
        save_json(index_path, updated_index)
    return {"applied": apply, "removed_paths": removed, "bytes": total}


def update_resources(proxy=None, workers=4, offline=False):
    """先扫描；仅构建输入变化时重新构建，成功后才更新最近成功报告。"""
    from scan_character_resources import scan
    from sync_character_resources import build_fingerprint, run
    args = argparse.Namespace(cache_dir=".cache/character-resources", proxy=proxy,
        offline=offline, group=[], rescan=False, pixel_threshold=12, min_changed_ratio=.01,
        report="reports/resource-diff.json", config_output="reports/resource-diff-characters.json",
        build=False, workers=workers)
    scan(args)
    config_path = ROOT_DIR / args.config_output
    config = json.loads(config_path.read_text(encoding="utf-8"))
    groups = {bundle["key"] for character in config["characters"].values() for bundle in character["bundles"]}
    fingerprint = build_fingerprint(config_path, ROOT_DIR / args.cache_dir, groups)
    latest = ROOT_DIR / "reports/character-resources.json"
    previous = json.loads(latest.read_text(encoding="utf-8")) if latest.exists() else {}
    if previous.get("input_fingerprint") == fingerprint and (ROOT_DIR / previous["output_dir"]).is_dir():
        print("[UPDATE] 输入未变化，跳过构建。", flush=True)
        return previous
    if not config["characters"]:
        # 新版本没有差异候选时也必须更新清单，不能继续提供过期补丁。
        cache = ROOT_DIR / args.cache_dir
        cn_source = json.loads((cache / "cn-source.json").read_text(encoding="utf-8"))
        jp_source = json.loads((cache / "jp-source.json").read_text(encoding="utf-8"))
        catalog = json.loads((cache / "cn-catalog.json").read_text(encoding="utf-8"))
        run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        output = ROOT_DIR / "build/character-resources" / run_id / "modified"
        manifest = output / "AssetBundles/Catalog" / cn_source["resource_version"] / "Android/bundleDownloadInfo.json"
        save_json(manifest, catalog)
        manifest.with_suffix(".hash").write_text(str(zlib.crc32(manifest.read_bytes())), encoding="utf-8")
        report = {"run_id": run_id, "generated_at": datetime.now(timezone.utc).isoformat(),
            "input_fingerprint": fingerprint, "config_sha256": sha256_file(config_path),
            "output_dir": output.relative_to(ROOT_DIR).as_posix(), "cn_source": cn_source,
            "jp_source": jp_source, "characters": [], "bundles": [], "source_assets": [],
            "structural_validation_passed": True, "applied": False, "game_client_verified": False}
        save_json(output.parent / "report.json", report)
        save_json(latest, report)
        return report
    return run(argparse.Namespace(config=args.config_output, character=[], cache_dir=args.cache_dir,
        proxy=proxy, output_dir=None, report="reports/character-resources.json", offline=True,
        apply=False, workers=workers))


def main():
    """作为资源服务的工作进程，或独立预览、执行生成文件清理。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--update", action="store_true")
    parser.add_argument("--proxy")
    parser.add_argument("--offline", action="store_true")
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--apply", action="store_true", help="实际执行清理；默认只展示计划")
    parser.add_argument("--keep-previews", action="store_true")
    parser.add_argument("--protect", action="append", default=[])
    args = parser.parse_args()
    try:
        with pipeline_lock():
            if args.update:
                update_resources(args.proxy, args.workers, args.offline)
            else:
                result = cleanup(protect=args.protect, keep_previews=args.keep_previews, apply=args.apply)
                print(json.dumps(result, ensure_ascii=False, indent=2))
    except (OSError, ValueError, RuntimeError, KeyError) as exc:
        print(f"[MAINTENANCE ERROR] {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    raise SystemExit(main())
