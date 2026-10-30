#!/usr/bin/env python3
# 为 Linux 4.19 内核打 SukiSU 4.x 兼容补丁（幂等，可重复运行）
# 用法: python3 patch-419.py kernel        # 内核源码根目录
import pathlib
import re
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
#include <linux/string.h>
#include <linux/sched/task.h>

/* 4.19 没有 fallthrough 宏（5.4 引入） */
#ifndef fallthrough
#define fallthrough __attribute__((__fallthrough__))
#endif

/* 4.19 没有 strncpy_from_user_nofault（5.8 引入）；sucompat 只在进程上下文用，直接走可睡眠版本 */
#if LINUX_VERSION_CODE < KERNEL_VERSION(5, 8, 0)
#ifndef strncpy_from_user_nofault
#define strncpy_from_user_nofault(dst, src, cnt) strncpy_from_user((dst), (src), (cnt))
#endif
#ifndef copy_to_kernel_nofault
#define copy_to_kernel_nofault(dst, src, size) memcpy((dst), (src), (size))
#endif
#ifndef copy_from_kernel_nofault
#define copy_from_kernel_nofault(dst, src, size) memcpy((dst), (src), (size))
#endif
#endif

/* 4.19 的 task_work_add 第三参是 bool notify，没有 TWA_RESUME 枚举（5.9 引入） */
#if LINUX_VERSION_CODE < KERNEL_VERSION(5, 9, 0)
#ifndef TWA_RESUME
#define TWA_RESUME true
#endif
#endif

/* 4.19 没有 seccomp action cache 的架构编号宏（5.9 引入），用 syscall 总数兜底 */
#if LINUX_VERSION_CODE < KERNEL_VERSION(5, 9, 0)
#ifndef SECCOMP_ARCH_NATIVE_NR
#ifdef __NR_syscalls
#define SECCOMP_ARCH_NATIVE_NR __NR_syscalls
#else
#define SECCOMP_ARCH_NATIVE_NR 512
#endif
#endif
#endif
/* ksu-4.19-compat: end */
"""

needles = (
    "strncpy_from_user_nofault",
    "TWA_RESUME",
    "copy_to_kernel_nofault",
    "copy_from_kernel_nofault",
    "SECCOMP_ARCH_NATIVE_NR",
    "put_task_struct",
    "fallthrough",
)
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

# 4.19 没有 include/uapi/linux/mount.h，但 include/linux/mount.h 一样提供 MS_PRIVATE/MS_REC
mnt = drv / "infra" / "su_mount_ns.c"
if mnt.exists():
    text = mnt.read_text(encoding="utf-8", errors="replace")
    if "<uapi/linux/mount.h>" in text:
        text = text.replace("#include <uapi/linux/mount.h>", "#include <linux/mount.h>")
        mnt.write_text(text, encoding="utf-8")
        print("[+] infra/su_mount_ns.c: uapi/linux/mount.h -> linux/mount.h")

print(f"[+] compat patched files: {patched}")

# objsec.h 里 #include "flask.h"，而 flask.h 只生成在 objtree/security/selinux 下；
# 在 drivers/ 里编译 selinux 相关代码需要显式加 include 路径（且要传给子目录）
kb = drv / "Kbuild"
if kb.exists():
    text = kb.read_text(encoding="utf-8", errors="replace")
    if "security/selinux" not in text:
        text += (
            "\n# 4.19 compat: flask.h 生成在 $(objtree)/security/selinux，跨目录编译需要显式 include\n"
            "subdir-ccflags-y += -I$(objtree)/security/selinux\n"
        )
        kb.write_text(text, encoding="utf-8")
        print("[+] added -I$(objtree)/security/selinux to drivers/kernelsu/Kbuild")
    else:
        print("[=] Kbuild 已有 selinux include 路径")

# file_wrapper.c 用到了 5.x 才有的 file_operations 成员（iopoll / remap_file_range），
# 4.19 上换成空实现；只在 4.x 内核里替换，5/6.x 仍用原文件
STUB = '''/* 4.19 compat: file_operations 没有 iopoll / remap_file_range，用空实现替代 */
#include <linux/kernel.h>
#include <linux/errno.h>
#include "infra/file_wrapper.h"

int ksu_install_file_wrapper(int fd)
{
    (void)fd;
    return -EOPNOTSUPP;
}

void ksu_file_wrapper_init(void)
{
}
'''
stub_path = drv / "infra" / "file_wrapper_stub.c"
if not stub_path.exists():
    stub_path.write_text(STUB, encoding="utf-8")
    print("[+] created infra/file_wrapper_stub.c")

if kb.exists():
    text = kb.read_text(encoding="utf-8", errors="replace")
    marker = "kernelsu-objs += infra/file_wrapper.o"
    if marker in text and "file_wrapper_stub.o" not in text:
        text = text.replace(
            marker,
            "ifeq ($(VERSION),4)\n"
            "kernelsu-objs += infra/file_wrapper_stub.o\n"
            "else\n"
            "kernelsu-objs += infra/file_wrapper.o\n"
            "endif",
        )
        kb.write_text(text, encoding="utf-8")
        print("[+] Kbuild: 4.x 用 file_wrapper_stub.o 替代 file_wrapper.o")

# pkg_observer.c 依赖 5.9+ 的 fsnotify API（handle_inode_event / fsnotify_add_inode_mark），
# 4.19 换成空实现，保留 ksu_observer_init/exit 两个符号
OBS_STUB = '''/* 4.19 compat: fsnotify inode-event API 差异过大，关闭 packages.list 变更监听；
 * throne 扫描仍由开机流程和包变更路径触发 */
#include <linux/kernel.h>
#include "manager/manager_observer.h"

int ksu_observer_init(void)
{
    pr_info("ksu observer: disabled on 4.19\\n");
    return 0;
}

void ksu_observer_exit(void)
{
}
'''
obs = drv / "manager" / "pkg_observer_stub.c"
if not obs.exists():
    obs.write_text(OBS_STUB, encoding="utf-8")
    print("[+] created manager/pkg_observer_stub.c")

if kb.exists():
    text = kb.read_text(encoding="utf-8", errors="replace")
    marker = "kernelsu-objs += manager/pkg_observer.o"
    if marker in text and "pkg_observer_stub.o" not in text:
        text = text.replace(
            marker,
            "ifeq ($(VERSION),4)\n"
            "kernelsu-objs += manager/pkg_observer_stub.o\n"
            "else\n"
            "kernelsu-objs += manager/pkg_observer.o\n"
            "endif",
        )
        kb.write_text(text, encoding="utf-8")
        print("[+] Kbuild: 4.x 用 pkg_observer_stub.o 替代 pkg_observer.o")



# ---- 针对性版本门控（4.19 缺字段 / 缺宏） ----

# extension/cpu_spoof.c: struct clocksource 的 vdso_clock_mode 是 5.2+ 才有
cpu = drv / "extension" / "cpu_spoof.c"
if cpu.exists():
    t = cpu.read_text(encoding="utf-8", errors="replace")
    if "KSU_CS_GUARD" not in t:
        pat = re.compile(r"(\n[ ]*\{\n[ ]*struct clocksource \*\*curr_cs_ptr =.*?\n[ ]*\}\n)", re.S)
        m = pat.search(t)
        if m:
            wrapped = (
                "\n#if LINUX_VERSION_CODE >= KERNEL_VERSION(5, 2, 0) /* KSU_CS_GUARD */"
                + m.group(1)
                + "#endif /* KSU_CS_GUARD */\n"
            )
            t = t[: m.start(1)] + wrapped + t[m.end(1) :]
            cpu.write_text(t, encoding="utf-8")
            print("[+] extension/cpu_spoof.c: clocksource.vdso_clock_mode 已加版本门控")

# policy/app_profile.c: seccomp.filter_count 是 5.9+ 才有
prof = drv / "policy" / "app_profile.c"
if prof.exists():
    t = prof.read_text(encoding="utf-8", errors="replace")
    old = "    atomic_set(&current->seccomp.filter_count, 0);"
    if old in t and "KSU_SECCOMP_CNT_GUARD" not in t:
        t = t.replace(
            old,
            "#if LINUX_VERSION_CODE >= KERNEL_VERSION(5, 9, 0) /* KSU_SECCOMP_CNT_GUARD */\n"
            + old
            + "\n#endif /* KSU_SECCOMP_CNT_GUARD */",
        )
        prof.write_text(t, encoding="utf-8")
        print("[+] policy/app_profile.c: seccomp.filter_count 已加版本门控")