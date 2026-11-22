# keepsake-build

Keepsake（留声）的**构建专库**：CI 规则、本机构建脚本、差分补丁工具、发布规则。

源码不在这个仓库：[`2806577488/keepsake`](https://github.com/2806577488/keepsake)。

- 构建/发布规则：[RELEASE.md](RELEASE.md)
- CI：`.github/workflows/release.yml`（分 ABI 构建 + 差分补丁 + Release）
- 本机 Windows 构建：`scripts/build-abis.cmd`
- 补丁工具：`tool/make_patch.py`（bsdiff → KSD1 容器 + 自检）

App 内置更新器读的 Release 在本仓库：

- `https://api.github.com/repos/2806577488/keepsake-build/releases/latest`
- `https://github.com/2806577488/keepsake-build/releases/latest`
