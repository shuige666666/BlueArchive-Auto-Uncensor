# BlueArchive-Auto-Uncensor

我这个采用了自动抓取和对比资源的方式，那时候想着是免得以后每多一个和谐角色我就得手动导入包。不过虽然说是自动脚本吧，但是这种方法会引入很多和和谐无关的改动的包，更新的资源会比实际和谐的角色多很多，不过我就没有对此进一步进行优化了，可能后续会做吧……假如有人做了提 PR 的话也许我会来看（也许不会），不过我这个没做任何宣传大概也不会有人找到吧……

100% vibe coding ，完全没有人工审核过一行代码。然后我的服务器的网速太慢了呀，假如有好心人能用超快网速服务器部署这个的话请把要更换的 url 告诉我谢谢[嘿嘿]。

部署完之后和之前防和谐的方式一样，把“LocalizeConfig.txt”中的 ResUrls 替换成你部署完后运行这个脚本服务的地址和端口。直接 ai 部署然后让它告诉你替换成什么网址就行了。

当然估计还会有很多 bug，这个后面发现了再说。

下方则是 ai 生成的介绍内容。


非官方《蔚蓝档案》角色资源补丁项目。沿用原项目可用的替换方式，新增日服素材自动获取、国服补丁构建和来源记录。

新流程会自动比较两服共有立绘和收藏画像，缓存比较结果并生成差异候选。差异不一定由和谐造成，生成的补丁仍需游戏内验证。立绘候选将配套日服素材写入国服包，保留内部标识并允许纹理尺寸变化；收藏画像仍按单张替换。

## 快速开始

在项目根目录使用 Python 3.12：

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe scripts\scan_character_resources.py
```

- `--offline`：使用已有缓存重放。
- `--build`：扫描后构建差异候选补丁，不发布。
- `--group '*asuna_spr*'`：限定试跑范围。
- `--proxy http://127.0.0.1:7890`：指定下载代理。

差异报告及候选配置位于 `reports/resource-diff.json` 和 `reports/resource-diff-characters.json`；使用 `--build` 后，补丁位于 `build/character-resources/<run_id>/`。

[运行与维护说明](docs/resource-pipeline.md) · [角色配置](config/characters.json) · [下载工具版本](config/download-tools.json)

已构建补丁后，可用 `scripts/serve_resources.py` 启动资源服务，自动定时更新并清理历史产物；模拟器端口转发与配置方式见维护说明。

## Docker Compose 部署

安装 Docker 和 Compose 后，在项目根目录执行：

```sh
docker compose up -d --build
docker compose logs -f resources
```

首次启动自动下载、扫描并生成补丁，完成后可访问 `http://127.0.0.1:18888/health`。之后每天检查更新，失败时继续提供已生成的补丁。缓存、补丁和报告保存在宿主机，不进入镜像。支持 Linux x86_64；端口、代理及服务器部署见[运行与维护说明](docs/resource-pipeline.md#docker-compose-部署)。

## 使用须知

> [!IMPORTANT]
> 本项目仅供学习和展示用途。
> 所有通过本项目获取的内容均应仅用于合法和正当的目的。开发者不对任何因使用本项目而引发的直接或间接损失、损害、法律责任或其他后果承担任何责任。
> 用户在使用本项目时需自行承担风险，并确保遵守所有相关法律法规。严禁将本项目用于任何未经授权或非法的活动。用户应对自身的行为负责。
