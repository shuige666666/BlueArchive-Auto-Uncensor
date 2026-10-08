from __future__ import annotations

import re
import sys
from pathlib import Path

import UnityPy
from PIL import Image


# ============================================================
# 路径
# ============================================================

ROOT_DIR = Path(__file__).resolve().parent.parent

REPLACEMENT_DIR = ROOT_DIR / "replacement"
OUT_DIR = ROOT_DIR / "out"


# Bundle 名称末尾：
#
# -2026-06-04_assets_all_817837721
#
# 去掉这一部分后，与 replacement 文件夹名称进行精确匹配。
BUNDLE_SUFFIX_RE = re.compile(
    r"-\d{4}-\d{2}-\d{2}_assets_all_\d+$",
    re.IGNORECASE,
)


# ============================================================
# Bundle 名称
# ============================================================

def normalize_bundle_name(bundle_path: Path) -> str:
    """
    例如：

    uis-01_common-03_nonequipment-_mxload-textures-2026-06-04_assets_all_817837721.bundle

    ->
    
    uis-01_common-03_nonequipment-_mxload-textures
    """

    return BUNDLE_SUFFIX_RE.sub("", bundle_path.stem)


# ============================================================
# 找 Bundle
# ============================================================

def find_bundle_candidates(
    replacement_name: str,
    bundle_files: list[Path],
) -> list[Path]:
    """
    精确匹配 replacement 文件夹名称。

    不使用 startswith，避免：
        abc
        abc-xxx
        abc-def

    之间发生误匹配。
    """

    replacement_name_lower = replacement_name.lower()

    return [
        bundle
        for bundle in bundle_files
        if normalize_bundle_name(bundle).lower()
        == replacement_name_lower
    ]


# ============================================================
# Texture2D 索引
# ============================================================

def build_texture_index(env) -> dict[str, object]:
    """
    建立：

        Texture2D.m_Name -> ObjectReader

    """

    texture_index: dict[str, object] = {}

    for obj in env.objects:
        if obj.type.name != "Texture2D":
            continue

        try:
            name = obj.peek_name()

        except Exception:
            try:
                data = obj.read()
                name = data.m_Name
            except Exception as exc:
                print(
                    f"    [WARNING] 无法读取 Texture2D "
                    f"path_id={obj.path_id}: {exc}"
                )
                continue

        if not name:
            continue

        # 同一个 Bundle 中如果真的存在同名 Texture2D，
        # 这里不主动选择其中一个，避免误替换。
        if name in texture_index:
            raise RuntimeError(
                f"Bundle 中存在多个同名 Texture2D: {name}"
            )

        texture_index[name] = obj

    return texture_index


# ============================================================
# 替换单张图片
# ============================================================

def replace_texture(
    texture_obj,
    image_path: Path,
) -> None:
    """
    使用 PNG 替换 Texture2D。
    """

    texture = texture_obj.read()

    with Image.open(image_path) as img:
        img.load()
        replacement_image = img.copy()

    texture.image = replacement_image
    texture.save()


# ============================================================
# 处理一个 Bundle
# ============================================================

def process_bundle(
    bundle_path: Path,
    replacement_dir: Path,
) -> int:

    print()
    print("=" * 72)
    print(f"Bundle      : {bundle_path}")
    print(f"Replacement : {replacement_dir}")
    print("=" * 72)

    # --------------------------------------------------------
    # 查找 PNG
    # --------------------------------------------------------

    png_files = sorted(
        p
        for p in replacement_dir.rglob("*")
        if p.is_file()
        and p.suffix.lower() == ".png"
    )

    if not png_files:
        print("  [WARNING] replacement 文件夹中没有 PNG")
        return 0

    print(f"  PNG 数量: {len(png_files)}")

    # --------------------------------------------------------
    # 加载 Bundle
    # --------------------------------------------------------

    print("  正在加载 Bundle...")

    env = UnityPy.load(str(bundle_path))

    # --------------------------------------------------------
    # 建立 Texture2D 索引
    # --------------------------------------------------------

    texture_index = build_texture_index(env)

    print(
        f"  Bundle 中 Texture2D 数量: {len(texture_index)}"
    )

    replaced_count = 0
    skipped_count = 0

    # --------------------------------------------------------
    # 逐个 PNG 替换
    # --------------------------------------------------------

    for image_path in png_files:

        texture_name = image_path.stem

        texture_obj = texture_index.get(texture_name)

        # ----------------------------------------------------
        # 找不到对应 Texture2D：跳过
        # ----------------------------------------------------

        if texture_obj is None:
            print(
                f"  [SKIP] {image_path.name}"
                f" -> Bundle 中不存在同名 Texture2D"
            )
            skipped_count += 1
            continue

        try:
            texture = texture_obj.read()

            with Image.open(image_path) as img:
                width, height = img.size

            print(
                f"  [REPLACE] {image_path.name} "
                f"({width}x{height})"
            )

            replace_texture(
                texture_obj,
                image_path,
            )

            replaced_count += 1

        except Exception as exc:
            raise RuntimeError(
                f"替换 Texture2D 失败:\n"
                f"  Bundle: {bundle_path}\n"
                f"  Texture2D: {texture_name}\n"
                f"  PNG: {image_path}\n"
                f"  Error: {type(exc).__name__}: {exc}"
            ) from exc

    # --------------------------------------------------------
    # 没有任何修改
    # --------------------------------------------------------

    if replaced_count == 0:

        print(
            f"  [NO CHANGE] "
            f"没有任何 Texture2D 被替换"
        )

        return 0

    # --------------------------------------------------------
    # 输出路径
    # --------------------------------------------------------
    #
    # 保留 Bundle 相对于 ROOT_DIR 的目录结构。
    #
    # 例如：
    #
    # ./some/path/foo.bundle
    #
    # ->
    #
    # ./out/some/path/foo.bundle
    #
    # --------------------------------------------------------

    relative_path = bundle_path.relative_to(ROOT_DIR)

    output_path = OUT_DIR / relative_path

    output_path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    # --------------------------------------------------------
    # 保存
    # --------------------------------------------------------

    print(f"  正在写入:")
    print(f"    {output_path}")

    with output_path.open("wb") as f:
        f.write(
            env.file.save(
                packer="original"
            )
        )

    print(
        f"  [DONE] "
        f"替换 {replaced_count} 个，"
        f"跳过 {skipped_count} 个"
    )

    return replaced_count


# ============================================================
# 主程序
# ============================================================

def main() -> int:

    print("=" * 72)
    print("UnityPy Texture2D Replacement")
    print("=" * 72)

    print(f"ROOT        : {ROOT_DIR}")
    print(f"REPLACEMENT : {REPLACEMENT_DIR}")
    print(f"OUTPUT      : {OUT_DIR}")
    print(
        f"UnityPy     : "
        f"{getattr(UnityPy, '__version__', 'unknown')}"
    )

    # --------------------------------------------------------
    # 检查 replacement
    # --------------------------------------------------------

    if not REPLACEMENT_DIR.is_dir():
        print(
            f"[ERROR] replacement 不存在:\n"
            f"  {REPLACEMENT_DIR}",
            file=sys.stderr,
        )
        return 1

    replacement_dirs = sorted(
        p
        for p in REPLACEMENT_DIR.iterdir()
        if p.is_dir()
    )

    if not replacement_dirs:
        print(
            "[ERROR] replacement 中没有子文件夹",
            file=sys.stderr,
        )
        return 1

    # --------------------------------------------------------
    # 查找全部 Bundle
    # --------------------------------------------------------
    #
    # 排除：
    #   replacement/
    #   scripts/
    #   out/
    #
    # 避免重复处理已经生成的 Bundle。
    # --------------------------------------------------------

    excluded_dirs = {
        REPLACEMENT_DIR.resolve(),
        (ROOT_DIR / "scripts").resolve(),
        OUT_DIR.resolve(),
        (ROOT_DIR / "assetexclusions").resolve(),
        (ROOT_DIR / "modified").resolve(),
    }

    bundle_files: list[Path] = []

    for path in ROOT_DIR.rglob("*.bundle"):

        if not path.is_file():
            continue

        resolved = path.resolve()

        if any(
            excluded == resolved
            or excluded in resolved.parents
            for excluded in excluded_dirs
        ):
            continue

        bundle_files.append(path)

    bundle_files.sort()

    if not bundle_files:
        print(
            "[ERROR] 没有找到任何 .bundle",
            file=sys.stderr,
        )
        return 1

    print()
    print(f"Bundle 数量       : {len(bundle_files)}")
    print(f"Replacement 数量  : {len(replacement_dirs)}")

    # --------------------------------------------------------
    # 统计
    # --------------------------------------------------------

    matched_dirs = 0
    skipped_no_bundle = 0
    processed_bundles = 0
    total_replaced = 0

    # --------------------------------------------------------
    # 处理每个 replacement 文件夹
    # --------------------------------------------------------

    for replacement_dir in replacement_dirs:

        replacement_name = replacement_dir.name

        print()
        print("-" * 72)
        print(f"[MATCH] {replacement_name}")

        candidates = find_bundle_candidates(
            replacement_name,
            bundle_files,
        )

        # ----------------------------------------------------
        # 没有对应 Bundle
        # ----------------------------------------------------

        if not candidates:
            print(
                f"  [SKIP] 没有找到对应 Bundle"
            )
            skipped_no_bundle += 1
            continue

        matched_dirs += 1

        print(
            f"  找到 {len(candidates)} 个候选 Bundle:"
        )

        for bundle in candidates:
            print(f"    - {bundle}")

        # ----------------------------------------------------
        # 逐个 Bundle 检查
        # ----------------------------------------------------

        for bundle_path in candidates:

            try:
                replaced = process_bundle(
                    bundle_path,
                    replacement_dir,
                )

                if replaced > 0:
                    processed_bundles += 1
                    total_replaced += replaced

            except Exception as exc:

                print(
                    f"\n[ERROR] Bundle 处理失败:\n"
                    f"{exc}",
                    file=sys.stderr,
                )

                return 1

    # --------------------------------------------------------
    # 总结
    # --------------------------------------------------------

    print()
    print("=" * 72)
    print("处理完成")
    print("=" * 72)

    print(
        f"Replacement 文件夹 : {len(replacement_dirs)}"
    )

    print(
        f"匹配到 Bundle 的文件夹 : {matched_dirs}"
    )

    print(
        f"跳过无 Bundle 的文件夹 : {skipped_no_bundle}"
    )

    print(
        f"生成输出 Bundle      : {processed_bundles}"
    )

    print(
        f"替换 Texture2D       : {total_replaced}"
    )

    print(
        f"输出目录             : {OUT_DIR}"
    )

    print("=" * 72)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())