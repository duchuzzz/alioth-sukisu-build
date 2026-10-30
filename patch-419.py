#!/usr/bin/env python3
# 为 Linux 4.19 内核打 SukiSU 4.x 兼容补丁（幂等，可重复运行）
# 用法: python3 patch-419.py kernel        # 内核源码根目录
import pathlib
import re
import sys
import hashlib
import urllib.request

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
#ifndef copy_from_user_nofault
#define copy_from_user_nofault(dst, src, size) copy_from_user((dst), (src), (size))
#endif
#ifndef copy_to_user_nofault
#define copy_to_user_nofault(dst, src, size) copy_to_user((dst), (src), (size))
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
    "copy_from_user_nofault",
    "copy_to_user_nofault",
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
    if "subdir-ccflags-y += -I$(objtree)/security/selinux" not in text:
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
# 注意：这里只能逐行门控赋值语句，整块包裹会因为花括号嵌套位置判断错误而产生语法错误
cpu = drv / "extension" / "cpu_spoof.c"
if cpu.exists():
    t = cpu.read_text(encoding="utf-8", errors="replace")
    if "KSU_CS_GUARD" not in t:
        old_assign = (
            "                        (*curr_cs_ptr)->vdso_clock_mode =\n"
            "                            VDSO_CLOCKMODE_ARCHTIMER;\n"
        )
        new_assign = (
            "#if LINUX_VERSION_CODE >= KERNEL_VERSION(5, 2, 0) /* KSU_CS_GUARD */\n"
            "                        (*curr_cs_ptr)->vdso_clock_mode =\n"
            "                            VDSO_CLOCKMODE_ARCHTIMER;\n"
            "#else\n"
            "                        (void)curr_cs_ptr;\n"
            "#endif /* KSU_CS_GUARD */\n"
        )
        if old_assign in t:
            cpu.write_text(t.replace(old_assign, new_assign), encoding="utf-8")
            print("[+] extension/cpu_spoof.c: clocksource.vdso_clock_mode 赋值已门控")
        else:
            print("[!] cpu_spoof.c 没找到预期赋值语句，跳过")

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

# The 4.19 SELinux database uses selinux_state.ss and a rwlock. SukiSU 4.3.0
# assumes the newer, RCU-replaceable selinux_policy object. Use the older
# policy editor from the KernelSU fork already used by this kernel family.
# Pin both the source commit and file digests so CI cannot silently change it.
COMPAT_COMMIT = "f8aeac629022114d6b366b17048965a28ff398e8"
COMPAT_SHA256 = {
    "rules.c": "ff2371426688bf160d9b13a230f9863679b58b858ad86a5f696a3538511cdc04",
    "sepolicy.c": "0b6e2200692d04303d4ee6e66f24cccf31cd2f5533d250fea11e15971b3c0919",
}


def fetch_compat(name):
    url = f"https://raw.githubusercontent.com/backslashxx/KernelSU/{COMPAT_COMMIT}/kernel/selinux/{name}"
    with urllib.request.urlopen(url, timeout=30) as response:
        data = response.read(128 * 1024 + 1)
    if len(data) > 128 * 1024 or hashlib.sha256(data).hexdigest() != COMPAT_SHA256[name]:
        sys.exit(f"[!] 4.19 SELinux compat source hash mismatch: {name}")
    return data.decode("utf-8")


selinux_header = kernel / "security" / "selinux" / "include" / "security.h"
services_header = kernel / "security" / "selinux" / "ss" / "services.h"
policydb_header = kernel / "security" / "selinux" / "ss" / "policydb.h"
if not all(p.is_file() for p in (selinux_header, services_header, policydb_header)):
    sys.exit("[!] 4.19 SELinux headers are missing")
if ("struct selinux_ss *ss;" not in selinux_header.read_text(encoding="utf-8") or
        "policy_rwlock;" not in services_header.read_text(encoding="utf-8") or
        "type_val_to_struct_array;" not in policydb_header.read_text(encoding="utf-8")):
    sys.exit("[!] unexpected 4.19 SELinux layout; do not apply the compat editor")

rules = drv / "selinux" / "rules.c"
rules_text = rules.read_text(encoding="utf-8")
if "KSU_419_RULES" not in rules_text:
    if "struct selinux_policy *backup_sepolicy;" not in rules_text:
        sys.exit("[!] unexpected SukiSU rules.c layout")
    rules_header = rules_text.split("struct selinux_policy *backup_sepolicy;", 1)[0]
    rules_header += "#include <linux/stop_machine.h>\n#include <linux/cpu.h>\n#include <linux/preempt.h>\n"
    rules_header += "#define KSU_COMPAT_USE_SELINUX_STATE 1 /* KSU_419_RULES */\n"
    rules.write_text(rules_header + fetch_compat("rules.c"), encoding="utf-8")
    print("[+] selinux/rules.c: use pinned 4.19 policy editor")

sepolicy = drv / "selinux" / "sepolicy.c"
sepolicy_text = sepolicy.read_text(encoding="utf-8")
if "KSU_419_SEPOLICY" not in sepolicy_text:
    if "#define KSU_SUPPORT_ADD_TYPE" not in sepolicy_text:
        sys.exit("[!] unexpected SukiSU sepolicy.c layout")
    sepolicy_header = sepolicy_text.split("#define KSU_SUPPORT_ADD_TYPE", 1)[0]
    sepolicy_header += "#include <linux/flex_array.h>\n"
    sepolicy_header += "#define KSU_TYPE_VAL_TO_STRUCT_ARRAY 1 /* KSU_419_SEPOLICY */\n"
    sepolicy.write_text(sepolicy_header + fetch_compat("sepolicy.c"), encoding="utf-8")
    print("[+] selinux/sepolicy.c: use pinned 4.19 policy database adapter")

sepolicy_h = drv / "selinux" / "sepolicy.h"
header_text = sepolicy_h.read_text(encoding="utf-8")
if "KSU_419_SEPOLICY_H" not in header_text:
    if ("struct selinux_policy *ksu_dup_sepolicy(struct selinux_policy *old_pol);" not in header_text or
            "void ksu_destroy_sepolicy(struct selinux_policy *orig);" not in header_text):
        sys.exit("[!] unexpected SukiSU sepolicy.h layout")
    header_text = header_text.replace(
        "struct selinux_policy *ksu_dup_sepolicy(struct selinux_policy *old_pol);",
        "#if LINUX_VERSION_CODE >= KERNEL_VERSION(5, 10, 0) /* KSU_419_SEPOLICY_H */\n"
        "struct selinux_policy *ksu_dup_sepolicy(struct selinux_policy *old_pol);",
    )
    header_text = header_text.replace(
        "void ksu_destroy_sepolicy(struct selinux_policy *orig);",
        "void ksu_destroy_sepolicy(struct selinux_policy *orig);\n#endif",
    )
    sepolicy_h.write_text(header_text, encoding="utf-8")

selinux = drv / "selinux" / "selinux.c"
selinux_text = selinux.read_text(encoding="utf-8")
if "KSU_419_CRED" not in selinux_text:
    selinux_text = selinux_text.replace(
        '#include "selinux.h"',
        '#include "selinux.h"\n#define selinux_cred(cred) ((struct task_security_struct *)(cred)->security) /* KSU_419_CRED */',
        1,
    )
    selinux.write_text(selinux_text, encoding="utf-8")
    print("[+] selinux/selinux.c: use 4.19 cred security field")

# selinux_hide requires a second, independent selinux_policy snapshot, which
# the 4.19 SELinux subsystem does not have. Keep root policy changes enabled.
hide_stub = drv / "feature" / "selinux_hide_stub.c"
if not hide_stub.exists():
    hide_stub.write_text('''#include "feature/selinux_hide.h"

void ksu_selinux_hide_init(void) {}
void ksu_selinux_hide_exit(void) {}
void ksu_selinux_hide_drop_backup_if_unused(void) {}
void ksu_selinux_hide_handle_second_stage(void) {}
void ksu_selinux_hide_handle_post_fs_data(void) {}
''', encoding="utf-8")
    print("[+] created feature/selinux_hide_stub.c")

if kb.exists():
    text = kb.read_text(encoding="utf-8")
    marker = "kernelsu-objs += feature/selinux_hide.o"
    if marker in text and "selinux_hide_stub.o" not in text:
        text = text.replace(marker,
                            "ifeq ($(VERSION),4)\n"
                            "kernelsu-objs += feature/selinux_hide_stub.o\n"
                            "else\n" + marker + "\nendif")
        kb.write_text(text, encoding="utf-8")
        print("[+] Kbuild: 4.x uses selinux_hide_stub.o")
