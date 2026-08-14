# Laintas CLI 发布与同步方案

本文档说明如何将一个版本发布到 GitHub，并同步到 `cli.laintas.com`，使下载页、安装脚本和 CLI 的 `/v` 更新命令使用同一组发布资源。

## 1. 发布前检查

在仓库根目录执行：

```bash
git status
python3 -m py_compile version.py
```

**顶层模块登记校验**：`package_manifest.json` 是所有打包（setup.py、PyInstaller spec、
CI source 包、`/v` 自更新 manifest）的单一事实来源。新增任何顶层 `.py` 模块后必须登记到
`modules`，否则该模块不会进入任何发布产物（正式安装的 CLI 上会 `ImportError`）。发布前运行：

```bash
python3 - <<'PY'
import json, os
pm = json.load(open("package_manifest.json"))
modules = set(pm["modules"])
top_py = sorted(f[:-3] for f in os.listdir(".") if f.endswith(".py") and os.path.isfile(f))
missing = [m for m in top_py if m not in modules and m != "setup"]
assert not missing, f"顶层模块未登记到 package_manifest.json: {missing}"
print("package_manifest.json 完整: 所有顶层模块均已登记")
PY
```

当前登记在案的本地诊断模块（`event_log` / `critic` / `precheck` /
`rag_signals` / `mem_signals` / `stuck_signals` / `redactor`）只服务于
CLI 恢复和本地诊断，不上传到训练管线。

修改 [version.py](../version.py) 中的唯一版本号，例如：

```python
__version__ = "1.8.1"
```

下载页的版本号和下载地址位于：

```text
laintas_cli_download/src/components/DownloadSection.jsx
```

发布新版本时，必须同步更新：

- `DOWNLOAD_BASE`：`https://cli.laintas.com/releases/v1.8.1`
- `RELEASE_VERSION`：`v1.8.1`
- 页面兼容性区域的版本展示

然后构建下载页：

```bash
cd laintas_cli_download
mv dist/releases /tmp/laintas-release-assets
npm run build
mv /tmp/laintas-release-assets dist/releases
cd ..
```

构建前后必须保留 `dist/releases`，否则 Vite 的清理过程会删除自托管的安装包和 `/v` 更新包。

## 2. 创建 GitHub Release

提交并推送版本 tag：

```bash
git add version.py laintas_cli_download/src/components/DownloadSection.jsx
git commit -m "release: v1.8.1"
git tag v1.8.1
git push origin main
git push origin v1.8.1
```

`.github/workflows/release.yml` 会在 tag 推送后构建并发布：

- `laintas-cli_linux_amd64.tar.gz`
- `laintas-cli_linux_arm64.tar.gz`
- `laintas-cli_source.zip`
- `laintas-cli_<version>_amd64.deb`
- `manifest.json`
- `src_manifest.zip`
- `SHA256SUMS.txt`

确认 GitHub Release 已完成且不是 draft：

```bash
gh release view v1.8.1
```

## 3. 同步到 cli.laintas.com

Nginx 文档根目录必须是：

```text
/root/laintas_cli/laintas_cli_download/dist
```

推荐使用同步脚本生成源码 manifest，并从 GitHub Release 下载二进制资产：

```bash
python3 scripts/build_release_assets.py
```

该脚本依赖 `gh` 已登录，并从 `version.py` 读取版本号。它会写入：

```text
dist/releases/v1.8.1/
dist/releases/latest/
```

如果 `gh` 登录失效，可手动从 GitHub Release 下载全部资产，再放入上述两个目录。两个目录都必须至少包含：

```text
laintas-cli_linux_amd64.tar.gz
laintas-cli_linux_arm64.tar.gz
laintas-cli_source.zip
laintas-cli_<version>_amd64.deb
manifest.json
src_manifest.zip
SHA256SUMS.txt
```

同步后校验：

```bash
for dir in \
  laintas_cli_download/dist/releases/v1.8.1 \
  laintas_cli_download/dist/releases/latest; do
  (cd "$dir" && sha256sum -c SHA256SUMS.txt)
  python3 -c "import json; print(json.load(open('$dir/manifest.json'))['version'])"
done
```

两个目录的 manifest 版本都必须是当前发布版本，例如 `1.8.1`。

## 4. `/v` 更新地址

`updater.py` 默认配置为：

```python
DEFAULT_DOWNLOAD_BASE = "https://cli.laintas.com"
```

因此正常情况下 `/v` 只从以下地址下载，不直接访问 GitHub：

```text
https://cli.laintas.com/releases/latest/manifest.json
https://cli.laintas.com/releases/latest/src_manifest.zip
https://cli.laintas.com/releases/latest/laintas-cli_linux_amd64.tar.gz
https://cli.laintas.com/releases/latest/laintas-cli_linux_arm64.tar.gz
```

指定版本时使用：

```bash
LAINTAS_UPDATE_CHANNEL=v1.8.1 laintas-cli
```

它会读取：

```text
https://cli.laintas.com/releases/v1.8.1/manifest.json
```

如需测试镜像，可设置 `LAINTAS_DOWNLOAD_BASE`；生产环境不要将其设置为 GitHub 地址。

## 5. 发布后验证

```bash
curl -fsSL https://cli.laintas.com/releases/latest/manifest.json | python3 -m json.tool
curl -fsSIL https://cli.laintas.com/releases/latest/laintas-cli_linux_amd64.tar.gz
curl -fsSIL https://cli.laintas.com/install.sh
```

应确认：

- `latest/manifest.json` 的版本是新版本
- amd64、arm64、源码包和 Deb 包均返回 `200`
- 下载页显示新版本，下载链接指向正确的版本目录
- `/v` 检查更新时使用 `cli.laintas.com`
- `src_manifest.zip` 与 manifest 中的文件校验一致

静态文件更新不需要重载 Nginx；只有修改 Nginx 配置时才执行：

```bash
nginx -t && nginx -s reload
```

## 6. 常见问题

### 页面能打开，但下载链接是旧版本

检查 `DownloadSection.jsx` 中的 `DOWNLOAD_BASE` 和 `RELEASE_VERSION`，然后重新执行下载页构建。

### `/v` 报 manifest 版本旧

检查 `dist/releases/latest/manifest.json`，不要只更新 `v1.8.1` 目录；`latest` 是默认更新通道。

### 安装脚本返回 404

确认 `latest/` 中使用的是带架构后缀的文件名：

```text
laintas-cli_linux_amd64.tar.gz
laintas-cli_linux_arm64.tar.gz
```

### 构建后发布包消失

Vite 构建会清空 `dist`。构建前必须备份并在构建后恢复 `dist/releases`，或使用不会清空发布目录的独立构建目录。
