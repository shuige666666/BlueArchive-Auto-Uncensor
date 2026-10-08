# 运行与维护

## 资源来源

日服通过 [BA-AD v3.1.0](https://github.com/Deathemonic/BA-AD/releases/tag/v3.1.0) 读取官方启动器配置、Android 清单和资源包，无需安装日服或登录。下载工具版本及摘要固定在 `config/download-tools.json`。

国服从官方 setup/state 接口查询版本与 CDN 地址，原包按大小和 MD5 校验；日服包按清单的大小和 CRC32 校验。构建时将日服素材写入国服包内对象，保留国服文件名、内部包名、CAB 和对象标识，再更新下载清单。

静态立绘的 Spine 贴图需要配套 `.atlas` 和 `.skel`；收藏画像是独立 Texture2D。本流程不处理记忆大厅、文字 UI 和 Excel 数据库。原项目的可用部署方式继续沿用，新增补丁单独验证。

## 常用命令

先按 README 创建 Python 3.12 虚拟环境并安装依赖，在项目根目录运行：

```powershell
# 建立差异基线；后续只下载变更/缺失包，复用未变组的比较结果。
.\.venv\Scripts\python.exe scripts\scan_character_resources.py

# 使用已有版本缓存重放，不检查服务器新版本。
.\.venv\Scripts\python.exe scripts\scan_character_resources.py --offline

# 扫描完成后构建差异候选补丁，不发布。
.\.venv\Scripts\python.exe scripts\scan_character_resources.py --build
```

可用 `--group '*asuna_spr*'` 限定资源组，`--rescan` 重新比较。比较和构建默认各使用 4 个进程，`--workers 1` 可降低内存占用。需要代理时增加 `--proxy http://127.0.0.1:7890`。

## 输出与缓存

| 位置 | 用途 |
| --- | --- |
| `build/character-resources/<run_id>/modified/` | 可上传的补丁和清单 |
| 同级 `previews/`、`report.json` | 国服、日服、输出预览及构建报告 |
| `reports/character-resources.json` | 最近一次成功报告 |
| `reports/resource-diff.json` | 差异对象、单服对象、歧义及比较失败 |
| `reports/resource-diff-characters.json` | 自动生成的候选构建配置 |
| `.cache/character-resources/` | 官方原包、工具、提取素材、日志及覆盖备份 |
| `resources/provenance.json` | 经 `--apply` 写入的素材来源 |

扫描缓存包括原包索引、`scan-state.json` 和 `scan-previews/`。阈值、算法或对应包更新后会重新比较，成功批次会保存以便中断续跑。缓存、构建和运行报告不进 Git；自动清理后预览路径可能已回收，需要时用 `--rescan` 重新生成。

手动构建仍可使用 `sync_character_resources.py --character asuna`，或传入 `--config reports/resource-diff-characters.json --offline`。该入口的 `--apply` 将所选素材备份后写入 `replacement/` 和来源台账；常规构建直接读取两服原包，不额外导出素材。扫描器不会自动应用素材。构建直接读取官方包，不使用手改的 PNG。

BA-AD 另使用 Windows 的 `%LOCALAPPDATA%\baad` 或 Linux 的 `~/.local/share/baad` 保存元数据。下载失败查看 `.cache/character-resources/jp-download.log`；失败不会更新最新报告，应结合退出状态和报告时间判断。

## 增加角色与更新

在 `config/characters.json` 登记角色/皮肤 ID、去掉日期与哈希后缀的资源组名称，以及包内真实对象名。立绘登记贴图、图集和骨骼，收藏画像单独登记。先确认国服已存在对应角色与皮肤，再单独构建、查看预览和验证游戏内效果。

立绘将日服纹理、图集和骨骼写入国服对应对象，保留国服内部标识与未选内容。纹理采用日服实际尺寸，不强制缩放或修改图集坐标；编码格式一致时直接写纹理数据，格式不同时转换为国服格式并记录误差。保存后重新解析校验对象、尺寸、格式和配套内容，游戏内显示仍需验证。共用画像包只修改选中的画像。

2026-10-08：维护者已确认亚子、泉（泳装）在游戏内显示正常，本方案作为默认构建方式。当前报告记录已验证角色，其他候选不视为已逐一验证。

原 `update_resources.py` 等脚本保留用于对照，它们会使用历史整包/PNG/ExcelDB，不支持本次新增的图集与骨骼写入；新流程使用 `sync_character_resources.py`。

## 差异判断

首次下载两服相匹配的立绘候选包。Spine 按图集恢复切片，消除排布、旋转、裁剪空白与等比例缩放影响，再比较可见像素；收藏画像直接比较。默认像素阈值为 12，明显变化比例为 1%，并容忍一像素边缘误差。阈值可通过 `--pixel-threshold` 和 `--min-changed-ratio` 调整。

结果仅标记为差异候选，不直接认定和谐。单纯图集/骨骼字节变化不自动生成补丁；附件比例不兼容、同名歧义和无法还原的图集留待复核；Spine 纹理页尺寸不同仍按各自图集还原比较。已确认某个附件有明显视觉差异时，不因其他附件无法比较而取消候选；报告以 `comparison_complete=false` 保留比较覆盖不完整的提示。Spine 候选按贴图、图集、骨骼配套构建。共用画像包中只选择有明显差异的对象。

当前版本已跑完两服共有的 1,205 个资源组，原包缓存约 537 MiB；资源组包含皮肤、NPC，同一立绘通常对应贴图与文本两组，因此不能按组数计算角色数。重复运行已验证不下载资源包，仅刷新版本清单。缓存、PNG、预览和构建输出会扩大磁盘占用，首次运行可先预留 3～5 GiB，此数为预算。

## 本机模拟器测试

成功构建后运行资源服务，保持该进程运行到游戏测试结束：

```powershell
.\.venv\Scripts\python.exe scripts\serve_resources.py --proxy http://127.0.0.1:7890
# 使用本机已有的 adb；设备编号以 adb devices 的输出为准。
adb -s 127.0.0.1:7555 reverse tcp:18888 tcp:18888
```

服务默认只监听电脑的 `127.0.0.1:18888`，ADB 转发后模拟器可通过同一地址访问电脑。保留原配置其他字段，将 `LocalizeConfig.txt` 的 `ResUrls` 改为 `http://127.0.0.1:18888/prodm39`，先保存原文件以便恢复。该地址依赖端口转发；模拟器重启后需重新执行 `adb reverse`。

配置使用 UTF-8 和 LF 换行，地址末尾不要混入控制字符；服务日志若出现 `/prodm39%13/` 或 `/prodm39%0D/` 并返回 404，应清理配置中的隐藏字符，保存后重新启动游戏。

服务启动后立即检查更新，之后默认每 24 小时检查一次；只有构建输入变化才重新编码。更新在独立工作进程完成，校验成功后自动切换，失败继续提供旧版。当前下载请求使用固定快照，上一版资源路径仍可读取。`--update-interval` 可调整间隔秒数，`--no-update` 只提供资源。

服务优先提供本地补丁，缺失资源通过 HTTP 302 跳转至报告记录的国服 CDN，由游戏直接下载，避免官方大包占用服务器带宽并超时重试；日志中的 `PATCH` / `REDIRECT` 表示来源。客户端若不支持跳转，可加 `--stream-official` 恢复服务器转发（日志为 `OFFICIAL`）。`--proxy` 用于更新及兼容模式回源，不影响客户端直连 CDN。支持 GET、HEAD 和补丁单段断点下载。前台运行按 Ctrl+C 停止；后台实例 PID 位于 `.cache/local-server/pid.txt`，请求日志为 `service.log`，更新日志为 `update.log`，两者均限制大小并轮转。`/health` 可查看当前构建和更新状态。

每轮成功更新后自动清理：保留最近两个成功构建以及正在使用的版本，删除预览、中间素材、失败/旧构建和探测文件；国服原包保留最近两个资源版本，日服原包保留当前清单仍引用的缓存。源码、`replacement/`、正式下载工具和覆盖备份不会清理。`--keep-previews` 保留最近预览，`--no-cleanup` 禁用自动清理。独立扫描、构建和清理命令使用互斥锁，避免同时改写缓存。

```powershell
# 先查看清理计划；确认需要保留的自定义输出可用 --protect 指定。
.\.venv\Scripts\python.exe scripts\resource_maintenance.py
# 实际执行清理。
.\.venv\Scripts\python.exe scripts\resource_maintenance.py --apply
```

本地 HTTP 连通不等于游戏已应用补丁。若游戏拒绝 HTTP、使用已缓存清单，或请求了另一个资源版本，应结合服务日志检查；保留原资源缓存，先观察下载和立绘效果。

## Docker Compose 部署

使用 Linux x86_64 服务器和 Docker Compose v2，本机 Docker Desktop 也可测试。在项目根目录执行 `docker compose up -d --build`；无需先在宿主机安装 Python。首次没有成功报告时会先下载、扫描和构建，初始化完成前端口尚未开放，进度用 `docker compose logs -f resources` 查看。服务启动后立即检查更新，之后每 24 小时检查一次并自动清理，无需另设定时任务或依赖 GitHub Actions。

Ubuntu 首次部署先按 [Docker 官方说明](https://docs.docker.com/engine/install/ubuntu/) 安装 Engine 和 Compose 插件，用 `uname -m` 确认架构为 `x86_64`。提交并推送本地改动后，在服务器执行：

```sh
git clone https://github.com/shuige666666/BlueArchive-Auto-Uncensor.git
cd BlueArchive-Auto-Uncensor
docker compose up -d --build
docker compose logs -f resources
```

Docker 命令无权限时按安装说明配置用户权限，或增加 `sudo`。Git 不包含本机补丁和缓存，服务器默认会自行下载构建；需要复用时一并复制 `.cache/`、`build/` 和 `reports/`。

服务器无法访问 Docker Hub 时，可在能联网的本机构建并导出镜像：`docker compose build`，然后执行 `docker save -o bluearchive-image.tar bluearchive-auto-uncensor:local`。将镜像传到服务器，执行 `docker load -i bluearchive-image.tar` 和 `docker compose up -d --no-build --pull never`；镜像应与服务器仓库提交一致。导入完成后可删除传输用的归档文件。

需要调整配置时，将 `.env.example` 复制为 `.env`。`BA_PORT` 是宿主机端口，默认 18888；已有 Python 服务占用此端口时可改为 18889。`BA_WORKERS` 默认 2，内存较小时可改为 1。`BA_DATA_DIR` 默认项目目录，持久保存 `.cache/`、`build/`、`reports/`，BA-AD 元数据保存在其中的 `.cache/baad/`。已有缓存和补丁可直接复用，但不要让原 Python 更新服务与容器同时写同一份数据。

本机代理填写 `BA_PROXY=http://host.docker.internal:7890`，并确保代理允许 Docker 访问；容器的 `127.0.0.1` 指向容器自身。服务器无需代理时留空。`.env` 不进 Git，镜像不包含资源缓存和本机配置。

```sh
docker compose ps
docker compose logs --tail=100 resources
curl http://127.0.0.1:18888/health
# 拉取自己已提交的代码后，重建并启动；持久化数据继续使用。
git pull
docker compose up -d --build
# 停止服务，保留缓存和补丁。
docker compose down
```

本机模拟器沿用 `adb reverse`，端口与 `BA_PORT` 一致。例如端口为 18889 时，执行 `adb -s 127.0.0.1:7555 reverse tcp:18889 tcp:18889`，游戏填写 `ResUrls=http://127.0.0.1:18889/prodm39`。

服务器默认只向本机开放端口，可由宿主机 Nginx 等反向代理提供 HTTPS，将游戏配置改为 `ResUrls=https://你的域名/prodm39`。代理应保留请求路径并允许较长的资源下载时间。需要直接从外部访问 HTTP 时，在 `.env` 设置 `BA_BIND=0.0.0.0` 并放行对应端口；例如使用已放行的 80 端口时设置 `BA_PORT=80`，游戏填写 `ResUrls=http://服务器IP/prodm39`。容器启用自动重启和限量日志；健康检查只确认 HTTP 服务可响应，更新状态仍需查看 `/health` 和 `.cache/local-server/update.log`。

## 自动化与上传

GitHub Actions 每天北京时间 16:00 扫描两服共有立绘，亦可手动输入资源组通配符；报告、候选配置和预览 Artifact 保留 7 天。提交到 GitHub 默认分支并启用 Actions 后生效，当前不自动发布。

使用资源服务时，未修改内容会自动回源，不需要上传完整官方资源。若沿用原项目的 R2 上传方式，输出仅是补丁覆盖层，托管端仍需配置官方资源回源。上传前查看计划：

```powershell
$resourceReport = Get-Content -Raw -Encoding UTF8 reports/character-resources.json | ConvertFrom-Json
.\.venv\Scripts\python.exe scripts\uploadR2.py --directory $resourceReport.output_dir --prefix staging/asuna --dry-run
```

真正上传时去掉 `--dry-run`，并在环境中提供 `R2_ACCOUNT_ID`、`R2_ACCESS_KEY_ID`、`R2_SECRET_ACCESS_KEY`、`R2_BUCKET`。上传顺序为资源、清单、更新标记；测试前缀需配置对应路由。当前 `.hash` 使用清单 CRC32 数字标记，新增构建在客户端的缓存更新行为尚未验证。

## 开发检查

源码使用 UTF-8，新增方法写简洁中文注释。获取逻辑在 `resource_sources.py`，清单解析在 `jp_catalog.py`，差异扫描在 `scan_character_resources.py` / `spine_compare.py`，对象替换在 `character_patch.py`；依赖与下载工具升级后重新验证。

```powershell
.\.venv\Scripts\python.exe -X utf8 -m unittest discover -s tests -v
.\.venv\Scripts\python.exe -m compileall -q scripts tests
```

来源和详细校验保存在自动报告中，不再另维护重复的日期验证文档。结构检查通过后，新增角色仍需实际验证客户端显示。
