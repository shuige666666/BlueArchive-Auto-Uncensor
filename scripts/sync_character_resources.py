"""自动获取所选角色的日服原始资源，生成并验证国服角色补丁。"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import shutil
import sys
import zlib
from concurrent.futures import ProcessPoolExecutor
from contextlib import nullcontext
from datetime import datetime, timezone
from pathlib import Path

import UnityPy
from PIL import __version__ as pillow_version

from character_patch import export_assets, find_assets, group_assets_by_bundle, patch_bundle
from replaceTexture2D import normalize_bundle_name
from resource_sources import ROOT_DIR, create_session, download_file, download_jp, fetch_cn_catalog, save_json, sha256_file


def workspace_path(value: str | Path) -> Path:
    """将写入位置限制在工作区内，保护配置中意外填写的外部路径。"""
    path = Path(value)
    resolved = (ROOT_DIR / path).resolve() if not path.is_absolute() else path.resolve()
    if not resolved.is_relative_to(ROOT_DIR):
        raise ValueError(f"输出或缓存位置必须位于项目内: {value}")
    return resolved


def read_plan(
    config: Path, characters: list[str]
) -> tuple[list[str], dict[str, set[tuple[str, str]]]]:
    """读取维护者指定的角色和皮肤范围，不推断完整的和谐名单。"""
    settings = json.loads(config.read_text(encoding="utf-8"))
    if settings.get("schema_version") != 1:
        raise ValueError("不支持的 characters.json schema_version。")
    available = settings["characters"]
    selected = characters or list(available)
    unknown = set(selected) - available.keys()
    if unknown:
        raise ValueError(f"角色未配置: {sorted(unknown)}；可选: {sorted(available)}")
    groups = {}
    for character in selected:
        for bundle in available[character]["bundles"]:
            group = bundle["key"]
            if (
                not group
                or Path(group).name != group
                or any(char in group for char in ("/", "\\", ":"))
                or group in (".", "..")
            ):
                raise ValueError(f"资源组名称不能包含路径: {group}")
            keys = groups.setdefault(bundle["key"], set())
            keys.update(("Texture2D", name) for name in bundle.get("textures", []))
            keys.update(("TextAsset", name) for name in bundle.get("text_assets", []))
    if not groups or any(not keys for keys in groups.values()):
        raise ValueError("角色配置中存在空的资源组。")
    return selected, groups


def apply_sources(staged: Path, records: list[dict], report: dict, cache: Path) -> None:
    """将已验证来源的资源写入 replacement，覆盖前保存备份并更新来源台账。"""
    backup = cache / "backups" / report["run_id"]
    ledger_path = ROOT_DIR / "resources/provenance.json"
    ledger = (
        json.loads(ledger_path.read_text(encoding="utf-8"))
        if ledger_path.exists()
        else {"schema_version": 1, "assets": {}}
    )
    for record in records:
        relative = Path(record["replacement_file"])
        target = workspace_path(relative)
        source = staged / relative.relative_to("replacement")
        if target.exists() and sha256_file(target) != sha256_file(source):
            backup_file = backup / relative
            backup_file.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(target, backup_file)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        ledger["assets"][relative.as_posix()] = {
            **record,
            "source_region": "jp",
            "jp_version": report["jp_source"]["version"],
            "jp_catalog_url": report["jp_source"]["catalog_url"],
            "synced_at": report["generated_at"],
        }
    save_json(ledger_path, ledger)
    report["applied"] = True
    report["backup_dir"] = backup.relative_to(ROOT_DIR).as_posix() if backup.exists() else None


def build_bundle_job(job: tuple) -> dict:
    """独立加载并构建一个国服包，使贴图编码可在多个进程间并行。"""
    original, source_files, keys, output, previews = job
    source_assets = find_assets(source_files, keys)
    print(f"[BUILD] {original.name}", flush=True)
    result = patch_bundle(original, source_assets, keys, output, previews)
    with output.open("rb") as stream:
        result["md5"] = hashlib.file_digest(stream, "md5").hexdigest()
    return result


def run(args) -> dict:
    """执行获取、匹配、重打包和校验，成功后才允许更新正式资源。"""
    selected, groups = read_plan(workspace_path(args.config), args.character)
    cache = workspace_path(args.cache_dir)
    cache.mkdir(parents=True, exist_ok=True)
    session = create_session()
    if args.proxy:
        # 显式代理优先，避免 requests 用环境变量覆盖维护者指定的下载地址。
        session.trust_env = False
        session.proxies.update({"http": args.proxy, "https": args.proxy})
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    output = (
        workspace_path(args.output_dir)
        if args.output_dir
        else ROOT_DIR / "build/character-resources" / run_id / "modified"
    )
    if output.exists() and any(output.iterdir()):
        raise ValueError("输出目录已有文件；请指定新的空目录，避免混入上次构建的资源。")
    stage = cache / "staged" / run_id
    report = {
        "schema_version": 1,
        "run_id": run_id,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "characters": selected,
        "config_sha256": sha256_file(workspace_path(args.config)),
        "runtime": {
            "python": sys.version.split()[0],
            "unitypy": UnityPy.__version__,
            "pillow": pillow_version,
        },
        "applied": False,
        "game_client_verified": False,
        "output_dir": output.relative_to(ROOT_DIR).as_posix(),
        "bundles": [],
        "source_assets": [],
    }

    # 1. 获取版本一致的日服快照和国服官方清单；离线模式只重放缓存。
    if args.offline:
        jp_metadata = json.loads((cache / "jp-source.json").read_text(encoding="utf-8"))
        jp_dir = workspace_path(jp_metadata["bundle_dir"])
        cn_catalog = json.loads((cache / "cn-catalog.json").read_text(encoding="utf-8"))
        cn_metadata = json.loads((cache / "cn-source.json").read_text(encoding="utf-8"))
    else:
        jp_dir, jp_metadata = download_jp(session, cache, list(groups), proxy=args.proxy)
        cn_catalog, cn_metadata = fetch_cn_catalog(session, cache)
    report.update(jp_source=jp_metadata, cn_source=cn_metadata)
    catalog = copy.deepcopy(cn_catalog)
    entries = {item["Name"]: item for item in catalog["BundleFiles"]}
    cn_cache = cache / "cn" / cn_metadata["resource_version"]
    source_assets_by_group = {}
    source_files_by_group = {}
    targets = {}
    source_records = []

    # 2. 按资源组、对象名称和皮肤配置建立对应关系，缺项时停止。
    for group, required in groups.items():
        print(f"[MATCH] {group}", flush=True)
        jp_files = [path for path in jp_dir.glob("*.bundle") if normalize_bundle_name(path) == group]
        source_files_by_group[group] = jp_files
        source_assets = find_assets(jp_files, required)
        source_assets_by_group[group] = source_assets
        cn_entries = [item for item in catalog["BundleFiles"] if normalize_bundle_name(Path(item["Name"])) == group]
        if not cn_entries:
            raise RuntimeError(f"国服尚无对应资源组: {group}")
        cn_files = []
        for entry in cn_entries:
            name = entry["Name"]
            if Path(name).name != name or "/" in name or "\\" in name:
                raise RuntimeError(f"国服清单中的包名包含路径: {name}")
            target = cn_cache / name
            if args.offline:
                if not target.exists() or target.stat().st_size != entry["Size"]:
                    raise RuntimeError(f"离线缓存不存在或尺寸不符: {target}")
                with target.open("rb") as stream:
                    if hashlib.file_digest(stream, "md5").hexdigest() != entry["Crc"].lower():
                        raise RuntimeError(f"离线缓存摘要不符: {target}")
            else:
                download_file(
                    session,
                    f"{cn_metadata['root_url']}/AssetBundles/Android/{name}",
                    target,
                    md5=entry["Crc"],
                    size=entry["Size"],
                )
            cn_files.append(target)
        targets[group] = find_assets(cn_files, required)
        records = export_assets(source_assets, stage / group)
        for record in records:
            record["replacement_file"] = f"replacement/{group}/{record['filename']}"
        source_records.extend(records)

    # 3. 在实际国服包中写入对应资源，独立校验后更新大小和 MD5。
    jobs = []
    for group in source_assets_by_group:
        for original, keys in group_assets_by_bundle(targets[group]).items():
            generated = output / "AssetBundles/Android" / original.name
            jobs.append((original, source_files_by_group[group], keys, generated, output.parent / "previews"))
    workers = getattr(args, "workers", 4)
    # 工作进程只写各自的资源包，主进程收齐校验结果后才生成清单。
    with ProcessPoolExecutor(max_workers=workers) if workers > 1 else nullcontext(None) as pool:
        results = pool.map(build_bundle_job, jobs) if pool else map(build_bundle_job, jobs)
        for result in results:
            entries[result["name"]].update(Crc=result["md5"], Size=result["size"])
            report["bundles"].append(result)
    catalog_dir = output / "AssetBundles/Catalog" / cn_metadata["resource_version"] / "Android"
    catalog_path = catalog_dir / "bundleDownloadInfo.json"
    save_json(catalog_path, catalog)
    # 沿用原项目的数字更新标记格式，用确定性值表示本次清单；它不是安全校验值。
    (catalog_dir / "bundleDownloadInfo.hash").write_text(str(zlib.crc32(catalog_path.read_bytes())), encoding="utf-8")
    for result in report["bundles"]:
        entry = entries[result["name"]]
        if entry["Crc"] != result["md5"] or entry["Size"] != result["size"]:
            raise RuntimeError("输出清单与补丁文件不一致。")
    report.update(
        source_assets=source_records,
        structural_validation_passed=True,
        preview_dir=(output.parent / "previews").relative_to(ROOT_DIR).as_posix(),
        staged_replacement_dir=stage.relative_to(ROOT_DIR).as_posix(),
    )
    if args.apply:
        apply_sources(stage, source_records, report, cache)
    save_json(workspace_path(args.report), report)
    save_json(output.parent / "report.json", report)
    print(f"[DONE] 校验通过，生成 {len(report['bundles'])} 个国服补丁包；游戏客户端验证尚未执行。", flush=True)
    print(f"[OUTPUT] {output}")
    print(f"[REPORT] {workspace_path(args.report)}")
    return report


def main() -> int:
    """解析命令行并报告实际失败，不把下载或匹配失败当作成功。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="config/characters.json")
    parser.add_argument("--character", action="append", default=[], help="角色配置 ID；可重复指定，默认使用配置中全部角色")
    parser.add_argument("--cache-dir", default=".cache/character-resources")
    parser.add_argument("--output-dir", help="新的空输出目录；默认 build/character-resources/<时间>/modified")
    parser.add_argument("--report", default="reports/character-resources.json")
    parser.add_argument("--proxy", help="可选下载代理地址；不会写入来源报告")
    parser.add_argument("--offline", action="store_true", help="使用已有官方清单和原包缓存重放构建")
    parser.add_argument("--apply", action="store_true", help="校验后将提取资源写入 replacement，并维护来源台账")
    parser.add_argument("--workers", type=int, default=4, help="补丁构建进程数；内存较小时设置 1")
    args = parser.parse_args()
    if args.workers < 1:
        parser.error("构建进程数必须大于 0。")
    try:
        run(args)
    except (OSError, ValueError, KeyError, RuntimeError) as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    # Windows 重定向到日志时仍输出 UTF-8，避免中文日志被按本地代码页写出。
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    raise SystemExit(main())
