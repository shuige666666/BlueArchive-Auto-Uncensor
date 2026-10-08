"""自动比较两服共有立绘资源，缓存结果并生成差异候选配置。"""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import sys
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from contextlib import nullcontext
from datetime import datetime, timezone
from pathlib import Path

import UnityPy
from PIL import ImageChops, ImageFilter, ImageStat

from character_patch import asset_digest, text_bytes
from spine_compare import compare_spine
from replaceTexture2D import normalize_bundle_name
from resource_sources import (
    ROOT_DIR, create_session, download_file, ensure_jp_bundles,
    fetch_cn_catalog, fetch_jp_catalog, save_json,
)
from sync_character_resources import workspace_path

COLLECTION = "uis-01_common-14_charactercollect-_mxload-textures"
SCAN_VERSION = 4


def catalog_groups(entries: list[dict]) -> dict[str, list[dict]]:
    """只索引 Spine 贴图/文本与收藏画像，排除其他游戏内容。"""
    result = {}
    for entry in entries:
        key = normalize_bundle_name(Path(entry["Name"]))
        is_spine = key.startswith("assets-_mx-spinecharacters-") and key.endswith(("-_mxdependency-textures", "-_mxdependency-textassets"))
        if key == COLLECTION or is_spine:
            result.setdefault(key, []).append(entry)
    return result


def pair_fingerprint(jp: list[dict], cn: list[dict], threshold: int, ratio: float) -> str:
    """用各服自己的包标记检测更新，不跨服比较不同算法的摘要。"""
    data = {"version": SCAN_VERSION, "threshold": threshold, "ratio": ratio}
    for region, entries in (("jp", jp), ("cn", cn)):
        data[region] = sorted((entry["Name"], entry["Size"], entry["Crc"]) for entry in entries)
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


def index_group(paths: list[Path]) -> tuple[dict, list]:
    """按对象名匹配；不同内容的同名对象记为歧义，不任意选版本。"""
    assets, ambiguous = {}, set()
    for path in sorted(paths):
        env = UnityPy.load(str(path))
        for obj in env.objects:
            if obj.type.name not in ("Texture2D", "TextAsset"):
                continue
            name = obj.peek_name()
            if not name:
                continue
            key = (obj.type.name, name)
            if key in ambiguous:
                continue
            if key in assets and asset_digest(assets[key]) != asset_digest(obj):
                del assets[key]
                ambiguous.add(key)
            else:
                assets[key] = obj
    return assets, [list(key) for key in sorted(ambiguous)]


def image_difference(cn, jp, threshold: int, min_ratio: float) -> dict:
    """忽略透明区域隐藏 RGB，以明显变化像素比例过滤轻微编码误差。"""
    cn, jp = cn.convert("RGBA"), jp.convert("RGBA")
    if cn.size != jp.size:
        return {"classification": "incompatible_size", "cn_size": list(cn.size), "jp_size": list(jp.size)}

    def visible(image):
        """把透明度作用到颜色上，避免不可见像素产生差异候选。"""
        rgb = ImageChops.multiply(image.convert("RGB"), image.getchannel("A").convert("RGB"))
        rgba = rgb.convert("RGBA")
        rgba.putalpha(image.getchannel("A"))
        return rgba

    left, right = visible(cn), visible(jp)
    difference = ImageChops.difference(left, right)
    channels = difference.split()
    tolerant = []
    # 裁剪和等比例缩放会产生约一像素的边缘偏差，用邻域色值范围消除该噪声。
    for a, b in zip(left.split(), right.split()):
        forward = ImageChops.lighter(ImageChops.subtract(a, b.filter(ImageFilter.MaxFilter(3))), ImageChops.subtract(b.filter(ImageFilter.MinFilter(3)), a))
        backward = ImageChops.lighter(ImageChops.subtract(b, a.filter(ImageFilter.MaxFilter(3))), ImageChops.subtract(a.filter(ImageFilter.MinFilter(3)), b))
        tolerant.append(ImageChops.lighter(forward, backward))
    maximum = tolerant[0]
    for channel in tolerant[1:]:
        maximum = ImageChops.lighter(maximum, channel)
    histogram = maximum.histogram()
    changed_ratio = sum(histogram[threshold + 1:]) / (cn.width * cn.height)
    return {
        "classification": "visual_difference" if changed_ratio >= min_ratio else "small_difference",
        "size": list(cn.size), "changed_pixel_ratio": changed_ratio,
        "mean_absolute_pixel_error_rgba": ImageStat.Stat(difference).mean,
    }


def compare_group(jp_paths, cn_paths, preview: Path, threshold: int, min_ratio: float, jp_text_paths=None, cn_text_paths=None) -> dict:
    """比较共有对象，仅为明显图像差异保存预览，不判断差异的业务原因。"""
    jp, jp_ambiguous = index_group(jp_paths)
    cn, cn_ambiguous = index_group(cn_paths)
    jp_text = index_group(jp_text_paths)[0] if jp_text_paths else {}
    cn_text = index_group(cn_text_paths)[0] if cn_text_paths else {}
    records = []
    for key in sorted(jp.keys() & cn.keys()):
        kind, name = key
        record = {"type": kind, "name": name, "cn_sha256": asset_digest(cn[key]), "jp_sha256": asset_digest(jp[key])}
        if record["cn_sha256"] == record["jp_sha256"]:
            record["classification"] = "identical"
        elif kind == "TextAsset":
            record["classification"] = "text_difference"
        else:
            cn_image, jp_image = cn[key].read().image, jp[key].read().image
            atlas_key = ("TextAsset", name + ".atlas")
            region_previews = {}
            if cn_image.size != jp_image.size:
                record.update(image_difference(cn_image, jp_image, threshold, min_ratio))
            elif jp_text_paths is not None:
                if atlas_key not in jp_text or atlas_key not in cn_text:
                    record.update(classification="needs_review", reason="缺少可对应的图集")
                else:
                    comparison, region_previews = compare_spine(
                        cn_image, jp_image,
                        text_bytes(cn_text[atlas_key].read()).decode("utf-8"),
                        text_bytes(jp_text[atlas_key].read()).decode("utf-8"), name,
                        lambda left, right: image_difference(left, right, threshold, min_ratio),
                    )
                    record.update(comparison)
            else:
                record.update(image_difference(cn_image, jp_image, threshold, min_ratio))
            if record["classification"] == "visual_difference":
                if Path(name).name != name or any(char in name for char in ("/", "\\", ":")):
                    raise ValueError(f"预览对象名包含路径: {name}")
                preview.mkdir(parents=True, exist_ok=True)
                cn_image.save(preview / f"{name}-cn.png")
                jp_image.save(preview / f"{name}-jp.png")
                for region, (left, right) in region_previews.items():
                    suffix = hashlib.sha256(region.encode()).hexdigest()[:12]
                    left.save(preview / f"{name}-region-{suffix}-cn.png")
                    right.save(preview / f"{name}-region-{suffix}-jp.png")
                record["preview_dir"] = preview.relative_to(ROOT_DIR).as_posix()
        records.append(record)
    return {
        "assets": records, "jp_ambiguous": jp_ambiguous, "cn_ambiguous": cn_ambiguous,
        "jp_only": [list(key) for key in sorted(jp.keys() - cn.keys())],
        "cn_only": [list(key) for key in sorted(cn.keys() - jp.keys())],
    }


def candidate_config(results: dict) -> dict:
    """将差异转为现有构建器配置，Spine 按完整贴图/图集/骨骼配套选择。"""
    characters = {}
    collection = results.get(COLLECTION, {}).get("assets", [])
    portraits = [record["name"] for record in collection if record["classification"] == "visual_difference"]
    if portraits:
        characters["collection-differences"] = {"name": "收藏画像差异候选", "bundles": [{"key": COLLECTION, "textures": portraits, "text_assets": []}]}
    for group, result in sorted(results.items()):
        if not group.endswith("-_mxdependency-textures"):
            continue
        base = group.removesuffix("textures")
        text_group = base + "textassets"
        text = results.get(text_group, {}).get("assets", [])
        changed = any(record["classification"] == "visual_difference" for record in result["assets"])
        textures = [record["name"] for record in result["assets"] if record["type"] == "Texture2D" and record["classification"] != "incompatible_size"]
        texts = [record["name"] for record in text if record["type"] == "TextAsset"]
        required_texts = {name + suffix for name in textures for suffix in (".atlas", ".skel")}
        # 不自动拼接缺少配套骨骼、图集或有尺寸冲突的立绘。
        if not changed or not textures or not required_texts.issubset(texts):
            continue
        if any(record["classification"] == "incompatible_size" for record in result["assets"]):
            continue
        identifier = group.removeprefix("assets-_mx-spinecharacters-").removesuffix("-_mxdependency-textures")
        characters[identifier] = {"name": identifier + " 差异候选", "bundles": [
            {"key": group, "textures": textures, "text_assets": []},
            {"key": text_group, "textures": [], "text_assets": sorted(required_texts)},
        ]}
    return {"schema_version": 1, "characters": characters}


def scan(args) -> dict:
    """分批取得更新的包并比较，成功组及时保存，失败组留待重试。"""
    cache = workspace_path(args.cache_dir)
    cache.mkdir(parents=True, exist_ok=True)
    session = create_session()
    if args.proxy:
        session.trust_env = False
        session.proxies.update(http=args.proxy, https=args.proxy)
    jp_entries, jp_metadata = fetch_jp_catalog(session, cache, proxy=args.proxy, offline=args.offline)
    if args.offline:
        cn_catalog = json.loads((cache / "cn-catalog.json").read_text(encoding="utf-8"))
        cn_metadata = json.loads((cache / "cn-source.json").read_text(encoding="utf-8"))
    else:
        cn_catalog, cn_metadata = fetch_cn_catalog(session, cache)
    jp_groups, cn_groups = catalog_groups(jp_entries), catalog_groups(cn_catalog["BundleFiles"])
    common = sorted(jp_groups.keys() & cn_groups.keys())
    selected = [key for key in common if not args.group or any(fnmatch.fnmatchcase(key, pattern) for pattern in args.group)]
    selected = sorted(set(selected) | {
        key.removesuffix("textures") + "textassets" for key in selected
        if key.endswith("-_mxdependency-textures") and key.removesuffix("textures") + "textassets" in common
    })
    if not selected:
        raise ValueError("没有匹配的两服共有资源组。")
    state_path = cache / "scan-state.json"
    state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {"pairs": {}}
    results, errors = {}, {}
    counters = {"jp_downloaded_files": 0, "jp_payload_bytes": 0, "cn_downloaded_files": 0, "cn_payload_bytes": 0, "compared_groups": 0, "reused_groups": 0}
    def required_groups(key):
        """贴图比较需要配套图集，图集更新也应让贴图比较失效。"""
        groups = [key]
        text_key = key.removesuffix("textures") + "textassets"
        if key.endswith("-_mxdependency-textures") and text_key in common:
            groups.append(text_key)
        return groups

    fingerprints = {
        key: pair_fingerprint(
            [entry for group in required_groups(key) for entry in jp_groups[group]],
            [entry for group in required_groups(key) for entry in cn_groups[group]],
            args.pixel_threshold, args.min_changed_ratio,
        ) for key in selected
    }
    pending = []
    for key in selected:
        previous = state["pairs"].get(key)
        if previous and previous["fingerprint"] == fingerprints[key] and not args.rescan:
            results[key] = previous["result"]
            counters["reused_groups"] += 1
        else:
            pending.append(key)
    jp_dir = ROOT_DIR / jp_metadata["bundle_dir"]
    cn_dir = cache / "cn" / cn_metadata["resource_version"]
    print(f"[PLAN] 共有 {len(common)} 组，选择 {len(selected)} 组，复用 {len(results)} 组，待比较 {len(pending)} 组", flush=True)

    def acquire_cn(entry):
        """检查国服缓存，只下载 MD5 不符或缺失的原包。"""
        name = entry["Name"]
        if Path(name).name != name or any(char in name for char in ("/", "\\", ":")):
            raise ValueError(f"国服包名包含路径: {name}")
        target = cn_dir / name
        valid = target.is_file() and target.stat().st_size == entry["Size"]
        if valid:
            with target.open("rb") as stream:
                valid = hashlib.file_digest(stream, "md5").hexdigest() == entry["Crc"].lower()
        if not valid:
            # 国服版本号变化不等于所有包都更新，按官方 MD5 复用旧版本同名包。
            for previous in (cache / "cn").glob(f"*/{name}"):
                if previous == target or previous.stat().st_size != entry["Size"]:
                    continue
                with previous.open("rb") as stream:
                    matches = hashlib.file_digest(stream, "md5").hexdigest() == entry["Crc"].lower()
                if matches:
                    import shutil
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(previous, target)
                    valid = True
                    break
        if args.offline and not valid:
            raise RuntimeError(f"离线国服原包缺失或摘要不符: {name}")
        if not valid:
            download_file(session, f"{cn_metadata['root_url']}/AssetBundles/Android/{name}", target, md5=entry["Crc"], size=entry["Size"])
        return int(not valid), entry["Size"] if not valid else 0

    # 分批控制内存与命令行长度，记录完成组以便中断后续跑。
    for offset in range(0, len(pending), 20):
        batch = pending[offset:offset + 20]
        fetch_groups = sorted({group for key in batch for group in required_groups(key)})
        print(f"[FETCH] 待比较组 {offset + 1}～{offset + len(batch)}/{len(pending)}", flush=True)
        downloads = ensure_jp_bundles(session, cache, [entry for key in fetch_groups for entry in jp_groups[key]], jp_metadata, proxy=args.proxy, offline=args.offline)
        counters["jp_downloaded_files"] += downloads["downloaded_files"]
        counters["jp_payload_bytes"] += downloads["downloaded_payload_bytes"]
        with ThreadPoolExecutor(max_workers=8) as executor:
            for count, size in executor.map(acquire_cn, [entry for key in fetch_groups for entry in cn_groups[key]]):
                counters["cn_downloaded_files"] += count
                counters["cn_payload_bytes"] += size
        tasks = {}
        # 下载完成后并行比较不同资源组；只有主进程更新扫描台账。
        with ProcessPoolExecutor(max_workers=args.workers) if args.workers > 1 else nullcontext(None) as pool:
            for key in batch:
                text_group = key.removesuffix("textures") + "textassets"
                is_spine = key.endswith("-_mxdependency-textures")
                parameters = (
                    [jp_dir / entry["Name"] for entry in jp_groups[key]],
                    [cn_dir / entry["Name"] for entry in cn_groups[key]],
                    cache / "scan-previews" / fingerprints[key], args.pixel_threshold, args.min_changed_ratio,
                    [jp_dir / entry["Name"] for entry in jp_groups.get(text_group, [])] if is_spine else None,
                    [cn_dir / entry["Name"] for entry in cn_groups.get(text_group, [])] if is_spine else None,
                )
                tasks[key] = pool.submit(compare_group, *parameters) if pool else parameters
            for key in batch:
                try:
                    result = tasks[key].result() if pool else compare_group(*tasks[key])
                    results[key] = result
                    state["pairs"][key] = {"fingerprint": fingerprints[key], "result": result}
                    counters["compared_groups"] += 1
                except Exception as exc:
                    # UnityPy/图集版本异常属于该组失败，保留其他成功组供续跑。
                    errors[key] = f"{type(exc).__name__}: {exc}"
                print(f"[SCAN] {len(results)}/{len(selected)} {key}", flush=True)
        save_json(state_path, state)
    config = candidate_config(results)
    report = {
        "schema_version": 1, "generated_at": datetime.now(timezone.utc).isoformat(),
        "jp_source": jp_metadata, "cn_source": cn_metadata,
        "pixel_threshold": args.pixel_threshold, "min_changed_ratio": args.min_changed_ratio,
        "complete": not errors, "classification": "difference_candidates_not_confirmed_censorship",
        "counters": counters, "groups": results, "errors": errors,
        "jp_only_groups": sorted(jp_groups.keys() - cn_groups.keys()),
        "cn_only_groups": sorted(cn_groups.keys() - jp_groups.keys()),
        "candidate_profiles": len(config["characters"]),
    }
    save_json(workspace_path(args.report), report)
    save_json(workspace_path(args.config_output), config)
    print(f"[DONE] {counters}；候选配置 {len(config['characters'])} 项；错误 {len(errors)} 组", flush=True)
    if errors:
        raise RuntimeError("部分资源组比较失败，详见报告；成功组已保留，下次仅重试失败组。")
    if args.build and config["characters"]:
        from sync_character_resources import run
        run(argparse.Namespace(
            config=args.config_output, character=[], cache_dir=args.cache_dir, proxy=args.proxy,
            output_dir=None, report="reports/character-resources.json", offline=True, apply=False,
            workers=args.workers,
        ))
    return report


def main() -> int:
    """提供全量基线、范围试跑、离线重放和可选候选补丁构建入口。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache-dir", default=".cache/character-resources")
    parser.add_argument("--report", default="reports/resource-diff.json")
    parser.add_argument("--config-output", default="reports/resource-diff-characters.json")
    parser.add_argument("--group", action="append", default=[], help="资源组通配符；可重复，默认扫描两服共有组")
    parser.add_argument("--proxy")
    parser.add_argument("--offline", action="store_true")
    parser.add_argument("--rescan", action="store_true", help="重新比较，仍复用原包缓存")
    parser.add_argument("--build", action="store_true", help="扫描完成后构建所有差异候选；不发布、不写入 replacement")
    parser.add_argument("--pixel-threshold", type=int, default=12)
    parser.add_argument("--min-changed-ratio", type=float, default=0.01)
    parser.add_argument("--workers", type=int, default=4, help="解包比较进程数；内存较小时可设置 1")
    args = parser.parse_args()
    if not 0 <= args.pixel_threshold < 255 or not 0 < args.min_changed_ratio <= 1:
        parser.error("像素阈值范围为 0～254，变化比例范围为 (0, 1]。")
    if args.workers < 1:
        parser.error("比较进程数必须大于 0。")
    try:
        from resource_maintenance import pipeline_lock
        with pipeline_lock():
            scan(args)
    except (OSError, ValueError, RuntimeError, KeyError) as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    raise SystemExit(main())
