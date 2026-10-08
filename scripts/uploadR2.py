from __future__ import annotations

import argparse
import mimetypes
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import boto3
from boto3.s3.transfer import TransferConfig


ROOT_DIR = Path(__file__).resolve().parent.parent
MODIFIED_DIR = ROOT_DIR / "modified"


def required_env(name: str) -> str:
    """读取上传凭据，不在日志中输出凭据值。"""
    value = os.environ.get(name)

    if not value:
        raise RuntimeError(
            f"缺少环境变量: {name}"
        )

    return value.strip()


def upload_file(
    client,
    bucket: str,
    local_path: Path,
    key: str,
    config: TransferConfig,
) -> None:
    """上传一个资源文件，并为清单和更新标记设置可刷新缓存策略。"""
    content_type = (
        mimetypes.guess_type(
            str(local_path)
        )[0]
        or "application/octet-stream"
    )

    if (
        local_path.name.endswith(".hash")
        or local_path.name == "TableManifestHash"
    ):
        content_type = "text/plain"

    if local_path.suffix.lower() == ".json":
        content_type = "application/json"

    client.upload_file(
        str(local_path),
        bucket,
        key,
        ExtraArgs={
            "ContentType": content_type,
            **({"CacheControl": "no-cache"} if upload_phase(local_path) else {}),
        },
        Config=config,
    )

    print(
        f"[UPLOADED] {key}"
    )


def upload_phase(path: Path) -> int:
    """按资源、清单、更新标记分阶段，防止清单先指向尚未上传的文件。"""
    if path.suffix == ".hash" or path.name == "TableManifestHash":
        return 2
    if path.name in ("bundleDownloadInfo.json", "TableManifest"):
        return 1
    return 0


def run(directory: Path, prefix: str, dry_run: bool) -> None:
    """上传指定构建目录；预览模式只展示对象路径，不读取凭据或访问 R2。"""
    directory = directory.resolve()
    if not directory.is_relative_to(ROOT_DIR) or directory == ROOT_DIR:
        raise ValueError("上传目录必须是项目内的资源子目录。")
    if not directory.is_dir():
        raise ValueError(f"上传目录不存在: {directory}")
    prefix = prefix.strip("/")
    if prefix and ("\\" in prefix or any(part in (".", "..", "") for part in prefix.split("/"))):
        raise ValueError("对象前缀不能包含反斜杠、空段或相对路径。")
    files = sorted(path for path in directory.rglob("*") if path.is_file())
    if not files:
        raise ValueError("上传目录没有文件。")
    # 防止符号链接把凭据或其他工作区外文件加入上传列表。
    if any(not path.resolve().is_relative_to(directory) for path in files):
        raise ValueError("上传目录中存在指向目录外的文件。")

    def object_key(path: Path) -> str:
        """保留相对于构建根目录的路径，并可加上隔离测试前缀。"""
        relative = path.relative_to(directory).as_posix()
        return f"{prefix}/{relative}" if prefix else relative

    if dry_run:
        for path in sorted(files, key=lambda item: (upload_phase(item), item.as_posix())):
            print(f"[PLAN:{upload_phase(path)}] {object_key(path)} ({path.stat().st_size} bytes)")
        print(f"[DRY-RUN] 共 {len(files)} 个文件，未执行上传。")
        return

    account_id = required_env("R2_ACCOUNT_ID")
    client = boto3.client(
        "s3",
        endpoint_url=f"https://{account_id}.r2.cloudflarestorage.com",
        aws_access_key_id=required_env("R2_ACCESS_KEY_ID"),
        aws_secret_access_key=required_env("R2_SECRET_ACCESS_KEY"),
        region_name="auto",
    )
    bucket = required_env("R2_BUCKET")
    config = TransferConfig(
        multipart_threshold=64 * 1024 * 1024,
        multipart_chunksize=64 * 1024 * 1024,
        max_concurrency=8,
        use_threads=True,
    )

    # 每阶段全部成功才进入下一阶段；异常直接终止，避免发布不完整清单。
    for phase in range(3):
        batch = [path for path in files if upload_phase(path) == phase]
        with ThreadPoolExecutor(max_workers=4) as executor:
            list(executor.map(lambda path: upload_file(client, bucket, path, object_key(path), config), batch))
    print(f"R2 上传完成，共 {len(files)} 个文件。")


def main() -> int:
    """解析上传目录和可选前缀，保留原项目 modified/ 的默认入口。"""
    parser = argparse.ArgumentParser(description="上传生成的资源目录到 Cloudflare R2")
    parser.add_argument("--directory", type=Path, default=MODIFIED_DIR)
    parser.add_argument("--prefix", default="", help="可选对象路径前缀，例如 staging/asuna")
    parser.add_argument("--dry-run", action="store_true", help="只查看上传文件，不连接 R2")
    args = parser.parse_args()
    try:
        run(args.directory, args.prefix, args.dry_run)
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    raise SystemExit(main())
