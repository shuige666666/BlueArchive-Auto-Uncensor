# 运行与维护

## 资源来源

日服通过 [BA-AD v3.1.0](https://github.com/Deathemonic/BA-AD/releases/tag/v3.1.0) 读取官方启动器配置、Android 清单和资源包，无需安装日服或登录。下载工具版本及摘要固定在 `config/download-tools.json`。

国服从官方 setup/state 接口查询版本与 CDN 地址，原包按大小和 MD5 校验；日服包按清单的大小和 CRC32 校验。构建时将所选日服素材写入国服对象，保留国服对象标识与文件名，再更新下载清单。

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

手动构建仍可使用 `sync_character_resources.py --character asuna`，或传入 `--config reports/resource-diff-characters.json --offline`。该入口的 `--apply` 会备份旧文件后写入 `replacement/` 和来源台账；扫描器不会自动应用素材。构建直接读取官方包，不使用手改的 PNG。

BA-AD 另使用 Windows 的 `%LOCALAPPDATA%\baad` 或 Linux 的 `~/.local/share/baad` 保存元数据。下载失败查看 `.cache/character-resources/jp-download.log`；失败不会更新最新报告，应结合退出状态和报告时间判断。

## 增加角色与更新

在 `config/characters.json` 登记角色/皮肤 ID、去掉日期与哈希后缀的资源组名称，以及包内真实对象名。立绘登记贴图、图集和骨骼，收藏画像单独登记。先确认国服已存在对应角色与皮肤，再单独构建、查看预览和验证游戏内效果。

缺少对象、同名内容冲突或图片尺寸不一致会停止构建。重打包后重新解析检查对象标识、未选择对象及目标内容；图片重编码有损，报告记录像素误差。

原 `update_resources.py` 等脚本保留用于对照，它们会使用历史整包/PNG/ExcelDB，不支持本次新增的图集与骨骼写入；新流程使用 `sync_character_resources.py`。

## 差异判断

首次下载两服相匹配的立绘候选包。Spine 按图集恢复切片，消除排布、旋转、裁剪空白与等比例缩放影响，再比较可见像素；收藏画像直接比较。默认像素阈值为 12，明显变化比例为 1%，并容忍一像素边缘误差。阈值可通过 `--pixel-threshold` 和 `--min-changed-ratio` 调整。

结果仅标记为差异候选，不直接认定和谐。单纯图集/骨骼字节变化不自动生成补丁；尺寸不兼容、同名歧义和无法还原的图集留待复核。Spine 候选按贴图、图集、骨骼配套构建。共用画像包中只选择有明显差异的对象。

当前版本已跑完两服共有的 1,205 个资源组，原包缓存约 537 MiB；资源组包含皮肤、NPC，同一立绘通常对应贴图与文本两组，因此不能按组数计算角色数。重复运行已验证不下载资源包，仅刷新版本清单。缓存、PNG、预览和构建输出会扩大磁盘占用，首次运行可先预留 3～5 GiB，此数为预算。

## 本机模拟器测试

成功构建后运行资源服务，保持该进程运行到游戏测试结束：

```powershell
.\.venv\Scripts\python.exe scripts\serve_resources.py --proxy http://127.0.0.1:7890
# 使用本机已有的 adb；设备编号以 adb devices 的输出为准。
adb -s 127.0.0.1:7555 reverse tcp:18888 tcp:18888
```

服务默认只监听电脑的 `127.0.0.1:18888`，ADB 转发后模拟器可通过同一地址访问电脑。保留原配置其他字段，将 `LocalizeConfig.txt` 的 `ResUrls` 改为 `http://127.0.0.1:18888/prodm39`，先保存原文件以便恢复。该地址依赖端口转发；模拟器重启后需重新执行 `adb reverse`。

服务启动后立即检查更新，之后默认每 24 小时检查一次；只有构建输入变化才重新编码。更新在独立工作进程完成，校验成功后自动切换，失败继续提供旧版。当前下载请求使用固定快照，上一版资源路径仍可读取。`--update-interval` 可调整间隔秒数，`--no-update` 只提供资源。

服务优先提供本地补丁，缺失资源回源至报告记录的国服 CDN；日志中的 `PATCH` / `OFFICIAL` 表示来源。支持 GET、HEAD 和补丁单段断点下载。前台运行按 Ctrl+C 停止；后台实例 PID 位于 `.cache/local-server/pid.txt`，请求日志为 `service.log`，更新日志为 `update.log`，两者均限制大小并轮转。`/health` 可查看当前构建和更新状态。

每轮成功更新后自动清理：保留最近两个成功构建以及正在使用的版本，删除预览、中间素材、失败/旧构建和探测文件；国服原包保留最近两个资源版本，日服原包保留当前清单仍引用的缓存。源码、`replacement/`、正式下载工具和覆盖备份不会清理。`--keep-previews` 保留最近预览，`--no-cleanup` 禁用自动清理。独立扫描、构建和清理命令使用互斥锁，避免同时改写缓存。

```powershell
# 先查看清理计划；确认需要保留的自定义输出可用 --protect 指定。
.\.venv\Scripts\python.exe scripts\resource_maintenance.py
# 实际执行清理。
.\.venv\Scripts\python.exe scripts\resource_maintenance.py --apply
```

本地 HTTP 连通不等于游戏已应用补丁。若游戏拒绝 HTTP、使用已缓存清单，或请求了另一个资源版本，应结合服务日志检查；保留原资源缓存，先观察下载和立绘效果。此服务用于本机测试，公网部署另行配置 HTTPS 和反向代理。

## 自动化与上传

GitHub Actions 每天北京时间 16:00 扫描两服共有立绘，亦可手动输入资源组通配符；报告、候选配置和预览 Artifact 保留 7 天。提交到 GitHub 默认分支并启用 Actions 后生效，当前不自动发布。

沿用原项目部署配置。输出是补丁覆盖层，服务器仍需提供未修改的官方资源。上传前查看计划：

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
