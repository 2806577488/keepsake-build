#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Keepsake 差分补丁工具（构建库专用，**不进 App**）。

## 作用

把标准 `bsdiff` 生成的补丁（bzip2 压缩的三段）**转码**成 App 端能读的
KSD1 容器（zlib/raw-deflate 压缩的三段），并在上传前用一份**独立的 Python
参考实现**把补丁套回旧包，逐字节校验能否还原出新包 —— 补丁错了就不许发。

为什么转码而不是直接用 bspatch 原格式：Android/Java 没有内置 bzip2 解码器，
引入第三方纯 Java 库又要改 App 的 Gradle 依赖（本项目不允许）。zlib 是 JDK
自带的，App 侧用 `java.util.zip.Inflater` 直接解。

## KSD1 容器（小端）

```
0..3    "KSD1"
4..11   newSize        int64  新包字节数
12..15  ctrlCompLen    uint32
16..19  ctrlRawLen     uint32
20..23  diffCompLen    uint32
24..27  diffRawLen     uint32
28..31  extraCompLen   uint32
32..35  extraRawLen    uint32
36..    ctrl | diff | extra     三段 raw deflate（zlib, wbits=-15）
```

ctrl 段内容与 bsdiff 完全一致：每 24 字节一组 `(diffLen, extraLen, seek)`，
int64 小端，seek 可为负。合并语义见 `android/.../BsPatch.kt`。

## 用法

```
# 1) 生成一个补丁 + 元数据条目（会自动校验）
python tool/make_patch.py make \
    --old prev/keepsake-0.4.20-arm64.apk \
    --new dist/keepsake-0.4.21-arm64.apk \
    --out out/keepsake-0.4.20-to-0.4.21-arm64.patch \
    --from 0.4.20 --to 0.4.21 --abi arm64 \
    --from-apk keepsake-0.4.20-arm64.apk \
    --target-apk keepsake-0.4.21-arm64.apk \
    --entry out/entry-arm64.json

# 2) 把多个条目合并成 Release 里的 keepsake-<ver>-patches.json
python tool/make_patch.py merge --version 0.4.21 \
    --entry out/entry-arm64.json --entry out/entry-arm.json \
    --out out/keepsake-0.4.21-patches.json

# 3) 单独校验一个 KSD1 补丁（等价于 App 端的合并逻辑）
python tool/make_patch.py apply --old prev.apk --patch x.patch --out /tmp/new.apk
```

任何失败都打印 `错误：…` 并以非零码退出。
"""

from __future__ import annotations

import argparse
import bz2
import hashlib
import json
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import zlib

MAGIC = b"KSD1"
BSDIFF_MAGIC = b"BSDIFF40"
HEADER_SIZE = 36


def offtin(buf: bytes) -> int:
    """bsdiff 的整数编码：**符号位 + 绝对值**（不是补码）。

    bspatch.c 的 offtin()：低 7 字节按小端拼出绝对值，**最高字节的最高位是符号位**，
    置位表示负数。用 `struct.unpack('<q')` 读会把它当成补码 —— 于是 `seek=-1`
    读出来是 `-9223372036854775807`，old_pos 直接飞到 2^63 之外，
    跟出来的字节全被当成"旧包越界"处理。历史上「差分补丁一个都出不来」
    就是这个原因（CI 自检每次都在 ~1.1MB 处第一次失配）。
    """
    magnitude = buf[7] & 0x7F
    for i in range(6, -1, -1):
        magnitude = (magnitude << 8) | buf[i]
    return -magnitude if buf[7] & 0x80 else magnitude

# 0..255 + 0..255 的取模 256 加法表：索引 i*256 + j。
# 用 bytes.translate/map 走 C 层，比逐字节 Python 循环快几倍。
_ADD_TABLE = bytes(((i + j) & 0xFF) for i in range(256) for j in range(256))


def fail(message: str) -> None:
    print(f"错误：{message}", file=sys.stderr)
    sys.exit(1)


def sha256_of(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def raw_deflate(data: bytes, level: int = 9) -> bytes:
    """zlib 的 raw deflate（无头无尾，wbits=-15），与 Java Inflater(true) 对应。"""
    compressor = zlib.compressobj(level, zlib.DEFLATED, -15)
    return compressor.compress(data) + compressor.flush()


def raw_inflate(data: bytes) -> bytes:
    return zlib.decompress(data, -15)


# ---------------------------------------------------------------------------
# bsdiff 原格式 → KSD1
# ---------------------------------------------------------------------------


def read_bsdiff(path: str) -> tuple[bytes, bytes, bytes, int]:
    """拆出 bsdiff 补丁的三段（已解压）+ newSize。"""
    with open(path, "rb") as handle:
        blob = handle.read()
    if len(blob) < 32:
        fail(f"{path} 太短，不是 bsdiff 补丁")
    if blob[:8] != BSDIFF_MAGIC:
        fail(f"{path} 不是 bsdiff 补丁（缺少 BSDIFF40 标识）")
    ctrl_len, diff_len, new_size = struct.unpack("<qqq", blob[8:32])
    if ctrl_len <= 0 or diff_len < 0 or new_size <= 0:
        fail(f"{path} 头部数值不合法（ctrl={ctrl_len}, diff={diff_len}, new={new_size}）")
    start = 32
    ctrl_end = start + ctrl_len
    diff_end = ctrl_end + diff_len
    if diff_end > len(blob):
        fail(f"{path} 数据段越界（{diff_end} > {len(blob)}）")
    try:
        ctrl = bz2.decompress(blob[start:ctrl_end])
        diff = bz2.decompress(blob[ctrl_end:diff_end])
        extra = bz2.decompress(blob[diff_end:])
    except OSError as error:
        fail(f"{path} 的 bzip2 段解压失败：{error}")
    return ctrl, diff, extra, new_size


def write_ksd1(path: str, ctrl: bytes, diff: bytes, extra: bytes, new_size: int) -> None:
    ctrl_z = raw_deflate(ctrl)
    diff_z = raw_deflate(diff)
    extra_z = raw_deflate(extra)
    header = MAGIC + struct.pack(
        "<qIIIIII",
        new_size,
        len(ctrl_z),
        len(ctrl),
        len(diff_z),
        len(diff),
        len(extra_z),
        len(extra),
    )
    tmp = path + ".tmp"
    with open(tmp, "wb") as handle:
        handle.write(header)
        handle.write(ctrl_z)
        handle.write(diff_z)
        handle.write(extra_z)
    os.replace(tmp, path)


# ---------------------------------------------------------------------------
# KSD1 → 新包（App 端 BsPatch.kt 的 Python 参考实现，用于发布前自检）
# ---------------------------------------------------------------------------


def _add_bytes(base: bytes, delta: bytes) -> bytes:
    """逐字节 (base + delta) & 0xFF；长度取 delta。"""
    if not delta:
        return b""
    if not base:
        return delta
    table = _ADD_TABLE
    return bytes(
        map(table.__getitem__, ((b << 8) | d for b, d in zip(base, delta)))
    )


def apply_ksd1(old_path: str, patch_path: str, out_path: str) -> int:
    with open(patch_path, "rb") as handle:
        blob = handle.read()
    if len(blob) < HEADER_SIZE - 4 or blob[:4] != MAGIC:
        fail(f"{patch_path} 不是 KSD1 补丁")
    new_size = struct.unpack("<q", blob[4:12])[0]
    ctrl_comp, ctrl_raw, diff_comp, diff_raw, extra_comp, extra_raw = struct.unpack(
        "<IIIIII", blob[12:36]
    )
    if new_size <= 0:
        fail(f"{patch_path} 头部 newSize 不合法：{new_size}")
    expected = HEADER_SIZE + ctrl_comp + diff_comp + extra_comp
    if len(blob) < expected:
        fail(f"{patch_path} 不完整（{len(blob)} < {expected}）")

    offset = HEADER_SIZE
    ctrl = raw_inflate(blob[offset : offset + ctrl_comp])
    offset += ctrl_comp
    diff = raw_inflate(blob[offset : offset + diff_comp])
    offset += diff_comp
    extra = raw_inflate(blob[offset : offset + extra_comp])

    if len(ctrl) != ctrl_raw or len(diff) != diff_raw or len(extra) != extra_raw:
        fail(
            f"{patch_path} 段长度与头部声明不一致"
            f"（ctrl {len(ctrl)}/{ctrl_raw}, diff {len(diff)}/{diff_raw}, "
            f"extra {len(extra)}/{extra_raw}）"
        )

    with open(old_path, "rb") as handle:
        old = handle.read()

    chunks: list[bytes] = []
    produced = 0
    cpos = dpos = epos = 0
    old_pos = 0
    while produced < new_size:
        if cpos + 24 > len(ctrl):
            fail("控制段不够用（补丁被截断）")
        diff_len = offtin(ctrl[cpos : cpos + 8])
        extra_len = offtin(ctrl[cpos + 8 : cpos + 16])
        seek = offtin(ctrl[cpos + 16 : cpos + 24])
        cpos += 24
        if diff_len < 0 or extra_len < 0:
            fail(f"控制段长度非法（{diff_len}/{extra_len}）")
        if produced + diff_len + extra_len > new_size:
            fail("控制段越界：会写出声明的 newSize")

        delta = diff[dpos : dpos + diff_len]
        dpos += diff_len
        if len(delta) != diff_len:
            fail("diff 段不够用（补丁被截断）")
        # bspatch 语义：新字节 = diff 字节 + 对应位置的旧字节；旧字节**越界**
        # （<0 或 >= oldSize）时按 0 相加，也就是原样保留 diff 字节。
        # 注意：越界部分要取 **delta 里的那一段**，不能填 0 —— 填 0 就是把
        # 新包的字节抹成 0x00（CI 自检在 1119376 抓到过：期望 0x174、还原成 0x00）。
        start = max(0, old_pos)
        end = min(len(old), old_pos + diff_len)
        if end <= start:
            chunks.append(delta)
        else:
            skip = start - old_pos  # 开头落在旧包外的字节数（old_pos >= 0 时为 0）
            mid_len = end - start
            head = delta[:skip]
            mid = _add_bytes(old[start:end], delta[skip : skip + mid_len])
            tail = delta[skip + mid_len :]
            chunks.append(head + mid + tail)

        piece = extra[epos : epos + extra_len]
        epos += extra_len
        if len(piece) != extra_len:
            fail("extra 段不够用（补丁被截断）")
        chunks.append(piece)

        produced += diff_len + extra_len
        old_pos += diff_len + seek

    result = b"".join(chunks)
    if len(result) != new_size:
        fail(f"还原结果大小不对（{len(result)}/{new_size}）")
    tmp = out_path + ".tmp"
    with open(tmp, "wb") as handle:
        handle.write(result)
    os.replace(tmp, out_path)
    return new_size


# ---------------------------------------------------------------------------
# 子命令
# ---------------------------------------------------------------------------


def cmd_make(args: argparse.Namespace) -> None:
    for path in (args.old, args.new):
        if not os.path.isfile(path):
            fail(f"文件不存在：{path}")
    bsdiff = shutil.which(args.bsdiff_bin)
    if bsdiff is None:
        fail(f"找不到 bsdiff（{args.bsdiff_bin}），请先安装：apt-get install -y bsdiff")

    workdir = tempfile.mkdtemp(prefix="keepsake-bspatch-")
    raw_patch = os.path.join(workdir, "raw.bspatch")
    restored = os.path.join(workdir, "restored.apk")
    try:
        print(f"bsdiff：{os.path.basename(args.old)} → {os.path.basename(args.new)}")
        completed = subprocess.run(
            [bsdiff, args.old, args.new, raw_patch],
            capture_output=True,
            text=True,
        )
        if completed.returncode != 0:
            fail(
                "bsdiff 失败（返回码 "
                f"{completed.returncode}）：{completed.stderr.strip()[:500]}"
            )
        if not os.path.isfile(raw_patch) or os.path.getsize(raw_patch) == 0:
            fail("bsdiff 没有产出补丁文件")

        ctrl, diff, extra, new_size = read_bsdiff(raw_patch)
        os.makedirs(os.path.dirname(os.path.abspath(args.out)) or ".", exist_ok=True)
        write_ksd1(args.out, ctrl, diff, extra, new_size)

        # 自检：用独立实现把补丁套回旧包，必须逐字节等于新包。
        print("自检：套用补丁还原新包…")
        apply_ksd1(args.old, args.out, restored)
        restored_sha = sha256_of(restored)
        target_sha = sha256_of(args.new)
        if restored_sha != target_sha:
            os.unlink(args.out)
            fail(
                "自检失败：还原出来的包与新包不一致，补丁已删除"
                f"（{restored_sha[:16]}… ≠ {target_sha[:16]}…）"
            )

        patch_size = os.path.getsize(args.out)
        old_size = os.path.getsize(args.old)
        entry = {
            "name": os.path.basename(args.out),
            "from": args.from_version,
            "to": args.to_version,
            "abi": args.abi,
            "patch_size": patch_size,
            "patch_sha256": sha256_of(args.out),
            "from_apk": args.from_apk or os.path.basename(args.old),
            "from_sha256": sha256_of(args.old),
            "target_apk": args.target_apk or os.path.basename(args.new),
            "target_sha256": target_sha,
        }
        if args.entry:
            with open(args.entry, "w", encoding="utf-8") as handle:
                json.dump(entry, handle, ensure_ascii=False, indent=2)
                handle.write("\n")

        ratio = (patch_size * 100.0 / old_size) if old_size else 0.0
        print(
            f"补丁完成：{entry['name']}"
            f"（{patch_size / 1048576:.2f} MB，旧包 {old_size / 1048576:.2f} MB，"
            f"占 {ratio:.1f}%），自检通过"
        )
        print(json.dumps(entry, ensure_ascii=False))
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def cmd_apply(args: argparse.Namespace) -> None:
    for path in (args.old, args.patch):
        if not os.path.isfile(path):
            fail(f"文件不存在：{path}")
    size = apply_ksd1(args.old, args.patch, args.out)
    print(f"还原完成：{args.out}（{size} 字节，sha256={sha256_of(args.out)}）")


def cmd_merge(args: argparse.Namespace) -> None:
    entries = []
    for path in args.entry:
        if not os.path.isfile(path):
            fail(f"条目文件不存在：{path}")
        with open(path, "r", encoding="utf-8") as handle:
            entries.append(json.load(handle))
    if not entries:
        fail("没有任何补丁条目可合并")
    entries.sort(key=lambda item: (item.get("abi", ""), item.get("name", "")))
    payload = {
        "version": args.version,
        "patch_format": "KSD1 (bsdiff semantics, zlib sections)",
        "patches": entries,
    }
    with open(args.out, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    print(f"已写出 {args.out}：{len(entries)} 个补丁条目")

    if args.notes_out:
        # Release 说明里的差分更新表格（CI 直接 cat 到 notes.md 末尾）。
        lines = [
            "",
            f"### 差分更新（上一版 → {args.version}）",
            "",
            "| 目标 ABI | 补丁 | 大小 | 需要本机是 |",
            "| --- | --- | --- | --- |",
        ]
        for item in entries:
            lines.append(
                f"| {item['abi']} | `{item['name']}` | "
                f"{item['patch_size']} 字节 | {item['from_apk']} |"
            )
        lines.append("")
        with open(args.notes_out, "w", encoding="utf-8") as handle:
            handle.write("\n".join(lines))
        print(f"已写出补丁表格：{args.notes_out}")


def main(argv: list[str]) -> None:
    parser = argparse.ArgumentParser(description="Keepsake 差分补丁工具（KSD1 容器）")
    sub = parser.add_subparsers(dest="command", required=True)

    make = sub.add_parser("make", help="bsdiff 生成补丁 → 转码 KSD1 → 自检")
    make.add_argument("--old", required=True, help="上一版 APK")
    make.add_argument("--new", required=True, help="新版 APK")
    make.add_argument("--out", required=True, help="输出的 .patch")
    make.add_argument("--from", dest="from_version", required=True)
    make.add_argument("--to", dest="to_version", required=True)
    make.add_argument("--abi", required=True, choices=["arm64", "arm"])
    make.add_argument("--from-apk", default="", help="旧包在 Release 里的资产名")
    make.add_argument("--target-apk", default="", help="新包在 Release 里的资产名")
    make.add_argument("--entry", default="", help="把元数据条目写到这个 json")
    make.add_argument("--bsdiff-bin", default="bsdiff")
    make.set_defaults(func=cmd_make)

    apply_cmd = sub.add_parser("apply", help="用参考实现套补丁（自检/本地验证）")
    apply_cmd.add_argument("--old", required=True)
    apply_cmd.add_argument("--patch", required=True)
    apply_cmd.add_argument("--out", required=True)
    apply_cmd.set_defaults(func=cmd_apply)

    merge = sub.add_parser("merge", help="合并补丁条目为 Release 的 patches.json")
    merge.add_argument("--version", required=True)
    merge.add_argument("--entry", action="append", default=[])
    merge.add_argument("--out", required=True)
    merge.add_argument("--notes-out", default="", help="把补丁表格写成 Markdown（拼进 Release 说明）")
    merge.set_defaults(func=cmd_merge)

    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main(sys.argv[1:])
