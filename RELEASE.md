# Keepsake 构建与发布规则（构建专库）

本仓库**只有构建/发布规则**，不放 App 源码。
源码仓库：[`2806577488/keepsake`](https://github.com/2806577488/keepsake)（公开，只读拉取）。

App 内置更新器读的就是**本仓库**的 Release：

- API：`https://api.github.com/repos/2806577488/keepsake-build/releases/latest`
- 手动下载兜底：`https://github.com/2806577488/keepsake-build/releases/latest`

---

## 1. 目录结构

```
keepsake-build/
├─ .github/workflows/release.yml   # 唯一的 CI 规则：分 ABI 构建 + 差分补丁 + 发布
├─ scripts/build-abis.cmd          # 本机 Windows 分 ABI 构建（与 CI 等价的产物命名）
├─ tool/make_patch.py              # bsdiff 补丁 → KSD1 容器 + 发布前自检
└─ RELEASE.md                      # 本文件
```

源码库里**不再有任何** `.github/workflows/`。

---

## 2. 触发方式

| 触发 | 行为 |
| --- | --- |
| `workflow_dispatch`（手动） | 可填 `source_ref`（tag 或 `main`）、`version`、`force`。`source_ref` 留空 = 取源码库最新 tag |
| `schedule`（每天 03:00 UTC） | 查源码库最新 tag；该版本还没有 Release 就构建，已有就跳过 |

`force=true` 时即使 `v<version>` 已有 Release 也会重建并覆盖资产（用于补签名、修 CI）。

---

## 3. 版本号规则

1. 手填 `version` 优先；
2. 否则若 `source_ref` 是 tag（`v0.4.21`），去掉前缀 `v`；
3. 否则取源码 `pubspec.yaml` 的 `version:`，去掉 `+buildNumber`。

Release tag = `v<version>`（例如 `v0.4.21`）。App 内版本比较按语义化版本号进行。

---

## 4. 资产命名（**不要随意改**）

| 资产 | 说明 |
| --- | --- |
| `keepsake-<ver>-arm64.apk` | 64 位（arm64-v8a）安装包 |
| `keepsake-<ver>-arm.apk` | 32 位（armeabi-v7a）安装包 |
| `keepsake-<ver>-<abi>.apk.sha256` | 一行：`<sha256>  <文件名>`（App 下载完整包后强制校验，E-24） |
| `keepsake-<from>-to-<to>-<abi>.patch` | 差分补丁（KSD1 容器） |
| `keepsake-<ver>-patches.json` | 补丁元数据（App 靠它决定能不能走差分） |
| `IMPLEMENTATION-<ver>.md` | 本次构建的发行说明（版本/大小/哈希/签名指纹，由 CI 生成，E-03/N-7） |
| `cert-<ver>-<abi>.txt` | 签名证书信息（装不上时核对用） |

老版本（v0.4.19 及以前，还在源码库那边）可能有 `keepsake-<ver>-universal.apk`；
App 侧对不上本机 ABI 时会回退到通用包，补丁生成也兼容把 universal 当旧包。

构建命令（CI 与本机一致）：

```
flutter build apk --release --target-platform android-arm64   # → keepsake-<ver>-arm64.apk
flutter build apk --release --target-platform android-arm     # → keepsake-<ver>-arm.apk
```

---

## 5. 签名要求（**必须固定 keystore**）

签名不固定 → 每次更新的包签名不同 → 用户覆盖安装会报「签名不一致 / 应用未安装」。
所以 CI 一律从 Secrets 取同一份 keystore：

| Secret | 说明 |
| --- | --- |
| `KEYSTORE_BASE64` | `keystore.jks` 的 base64（`certutil -encode` / `base64 -w0`） |
| `KEYSTORE_PASSWORD` | keystore 口令（storePassword） |
| `KEY_ALIAS` | 密钥别名（当前本地生成的是 `keepsake`） |
| `KEY_PASSWORD` | 密钥口令（keyPassword） |

CI 步骤（`build` job）：

1. `printf '%s' "$KEYSTORE_BASE64" | base64 -d > android/keystore.jks`
2. 写 `android/key.properties`：`storePassword` / `keyPassword` / `keyAlias` /
   `storeFile=keystore.jks`（相对 `android/`，源码库 `build.gradle.kts` 用
   `rootProject.file()` 解析）
3. 构建完成后跑 `apksigner verify --print-certs`，把证书 DN 与 SHA-256 指纹
   写进 Release body（也作为 `cert-<ver>-<abi>.txt` 一起上传）

`keystore.jks` / `key.properties` **绝不进任何仓库**（源码库 `.gitignore` 已覆盖
`android/key.properties`、`**/*.jks`、`**/*.keystore`）。

没有配 Secrets 时的兜底：回退 debug 签名，并把 `~/.android/debug.keystore`
用 `actions/cache` 固定下来（key `keepsake-debug-keystore-v1`），至少让多次 CI
构建之间签名一致；Release body 会打 ⚠️ 提示。**正式发布必须配 Secrets。**

本机构建同理：把手上的 `android/keystore.jks` 与 `android/key.properties` 放在
源码库里，Gradle 会自动读，`build-abis.cmd` 不用改。

### 5.1 签名一致性守卫（CI 自动做）

生成差分补丁前，会下载上一版 Release 的 `cert-<ver>-<abi>.txt` 与本次对比：
SHA-256 指纹不同 → 直接跳过该 ABI 的补丁并 `::warning::`（签名不同连全量包
都装不上，补丁更没意义）。

---

## 6. 差分更新（KSD1）

### 6.1 为什么不是「zip 条目差分」

APK 的 Signature Scheme v2/v3 签名覆盖**整个文件的字节**。按 zip 条目重新打包
不可能逐字节复现下游产物（条目顺序、对齐、压缩级别、签名块位置都会变），
而客户端没有私钥重签 → 装上去必然签名校验失败。所以必须「逐字节重建新 APK」，
即 bsdiff 语义。

### 6.2 为什么压缩层是 zlib

标准 bsdiff 补丁的三段（ctrl/diff/extra）用 bzip2 压缩，而 Java/Android 没有
内置 bzip2 解码器；为了不引入第三方纯 Java 库（那要改 App 的 Gradle 依赖），
CI 用 `bsdiff` 生成标准补丁后，由 `tool/make_patch.py` 把三段 bzip2 **转码**成
zlib（raw deflate）写进 **KSD1** 容器。App 端 `android/.../BsPatch.kt` 用
JDK 自带的 `Inflater` 解，纯 Kotlin，无 NDK/JNI。

容器格式（小端）：

```
0..3    "KSD1"
4..11   newSize        int64
12..15  ctrlCompLen    uint32      16..19 ctrlRawLen   uint32
20..23  diffCompLen    uint32      24..27 diffRawLen   uint32
28..31  extraCompLen   uint32      32..35 extraRawLen  uint32
36..    ctrl | diff | extra       三段 raw deflate
```

### 6.3 CI 生成流程（最近 3 个版本 × 每版一个并行 job）

差分补丁不只为「相邻上一版」生成，而是**最近 3 个比当前小的 Release** 各生成一份
（`prepare` 输出 `patch_from`，最多 3 个版本）——用户跳过一两个版本升级也能差分。

1. `prepare` 解析版本，并算出 `patch_from`（最近 3 个更小的已发布版本）；
2. 两个 ABI 构建并发布；
3. `patch` **matrix**（每个 from 版本一个 job，并行）：
   - 从**旧 Release** 下载该版本的 APK 与 `cert-<from>-<abi>.txt`；
   - 签名指纹不一致 → 跳过该 ABI（`::warning::`）；
   - `bsdiff old.apk new.apk` → `make_patch.py make` 转码 KSD1；
   - **自检**：补丁套回旧包必须逐字节等于新包；补丁 ≥ 旧包 90% 丢弃；
   - 上传补丁文件 + 条目元数据 artifact（文件名带 from 版本，避免并行汇总时同名覆盖）；
4. `patch-metadata` 汇总所有条目：
   - 用**内联 python** 拼 `keepsake-<ver>-patches.json` 与说明表格
     （不再调用 `make_patch.py merge` 的 argparse 入口——6 个条目并行上传时
     那个 CLI 解析失败过一次）；
   - 上传 `patches.json`、把表格追加到 Release 说明；
   - 清理旧 Release（保留最近 10 个）。

补丁生成是**尽力而为**：任何一步失败都只告警，不阻塞发布（App 会全量下载）。

> **历史补记（0.4.39 / 0.4.40）**：这两版的补丁由并行 job 正常上传，但「汇总」job
> 跑的是修复前的工作流，所以当时**没有 `patches.json`**，是手工补齐的。
> 手工补齐的要点：`from_sha256` 必须取自**旧版本 Release** 的 `keepsake-<from>-<abi>.apk.sha256`
> （不在当前 Release 里），否则 App 端校验不过、补丁会被跳过。**从 0.4.41 起自动生成。**

### 6.4 元数据格式

```json
{
  "version": "0.4.21",
  "patch_format": "KSD1 (bsdiff semantics, zlib sections)",
  "patches": [
    {
      "name": "keepsake-0.4.20-to-0.4.21-arm64.patch",
      "from": "0.4.20",
      "to": "0.4.21",
      "abi": "arm64",
      "patch_size": 8123456,
      "patch_sha256": "…",
      "from_apk": "keepsake-0.4.20-arm64.apk",
      "from_sha256": "…",
      "target_apk": "keepsake-0.4.21-arm64.apk",
      "target_sha256": "…"
    }
  ]
}
```

### 6.5 App 端如何用

1. 检查更新时按 `Build.SUPPORTED_ABIS` 挑 APK，同时读 `-patches.json`；
2. 只接受 `abi` 匹配、`from == 当前版本`、`to == 最新版本`、且 `from_sha256`
   等于「本机已安装 APK 的 sha256」、`target_sha256` 齐全的补丁；
3. 下载补丁 → 原生 `applyPatch`（读 `applicationInfo.sourceDir` 合并）→
   `target_sha256` 校验；校验失败/任何异常 → 删掉半成品并**回退全量下载**；
4. 安装仍走原来的 `installApk`，签名不变（合并结果是逐字节还原），
   因此**华为纯净模式等厂商策略的表现与全量包一致**。

---

## 7. 保留策略

发布成功后自动清理：**只保留最近 10 个 Release**（`KEEP_RELEASES` 环境变量可调），
更旧的 Release 连 tag 一起删除（`gh release delete --yes --cleanup-tag`）。

---

## 8. 本机构建（Windows）

```bat
:: 分 ABI 构建，产物落到 <源码>\build\abi\
call Z:\dsh-restore\scripts\build-release.cmd
:: 或直接调构建库脚本（可传源码目录）
Z:\dsh-restore\keepsake-build\scripts\build-abis.cmd Z:\dsh-restore\keepsake
```

产出：

```
build\abi\keepsake-<版本>-arm64.apk(.sha256)
build\abi\keepsake-<版本>-arm.apk(.sha256)
```

本机不生成补丁（补丁需要“上一版 APK + bsdiff”，只在 CI 里做）。

---

## 9. 排查清单

| 现象 | 先看什么 |
| --- | --- |
| 装不上/覆盖安装失败 | 签名是否一致（debug vs 正式），`KEYSTORE_*` 是否配好 |
| App 提示没有可用更新 | 是否发布到**本仓库**；tag 是否为 `v<ver>` 且不是 draft |
| 只有全量下载，没有差分 | `-patches.json` 是否存在；上一版 Release 是否被清理掉（保留 10 个）；`from_sha256` 是否为空 |
| Release 有 patch 文件但没有 `patches.json` | 「补丁汇总」job 是否失败（看「合并 patches.json」步骤）；旧版需按 §6.3 补记手工补 |
| 差分合并失败 | CI 的自检步骤是否通过；`from_sha256` 与本机 APK 是否一致 |
| 32 位机装不上 arm64 包 | 资产名是否写成 `-arm.apk`（不能用 universal 混过去） |
