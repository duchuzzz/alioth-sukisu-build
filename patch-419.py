#!/usr/bin/env python3
# 为 Linux 4.19 内核打 SukiSU 4.x 兼容补丁（幂等，可重复运行）
# 用法: python3 patch-419.py kernel        # 内核源码根目录
import pathlib
import sys

kernel = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else "kernel")
drv = kernel / "drivers" / "kernelsu"
if not drv.is_dir():
    sys.exit(f"[!] 找不到 {drv}")

MARK = "ksu-4.19-compat: start"
SHIM = f"""/* {MARK} */
#include <linux/version.h>
#include <linux/types.h>
#include <linux/uaccess.h>

/* 4.19 没有 strncpy_from_user_nofault（5.8 引入）；sucompat 只在进程上下文用，直接走可睡眠版本 */
#if LINUX_VERSION_CODE < KERNEL_VERSION(5, 8, 0)
#ifndef strncpy_from_user_nofault
#define strncpy_from_user_nofault(dst, src, cnt) strncpy_from_user((dst), (src), (cnt))
#endif
#endif

/* 4.19 的 task_work_add 第三参是 bool notify，没有 TWA_RESUME 枚举（5.9 引入） */
#if LINUX_VERSION_CODE < KERNEL_VERSION(5, 9, 0)
#ifndef TWA_RESUME
#define TWA_RESUME true
#endif
#endif
/* ksu-4.19-compat: end */
"""

needles = ("strncpy_from_user_nofault", "TWA_RESUME")
patched = 0
for f in sorted(drv.rglob("*.c")):
    text = f.read_text(encoding="utf-8", errors="replace")
    if MARK in text:
        continue
    if any(n in text for n in needles):
        f.write_text(SHIM + "\n" + text, encoding="utf-8")
        patched += 1
        print("[+] compat:", f.relative_to(kernel))

# MODULE_IMPORT_NS 是 5.4+ 的宏，4.19 会把它当成隐式 int 函数声明
init = drv / "core" / "init.c"
if init.exists():
    lines = init.read_text(encoding="utf-8", errors="replace").splitlines(True)
    kept = [l for l in lines if "MODULE_IMPORT_NS" not in l]
    if len(kept) != len(lines):
        init.write_text("".join(kept), encoding="utf-8")
        print("[+] removed MODULE_IMPORT_NS from core/init.c")

print(f"[+] compat patched files: {patched}")
