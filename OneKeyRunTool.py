# -*- coding: utf-8 -*-
"""
一键启停工具 (OneKeyRunTool)
批量启动 / 关闭本地程序的 Windows 桌面小工具，支持命令行参数静默执行。

v1.1.0 新增「获取本机启动项」：只读扫描注册表 Run 键与两个启动文件夹，
可勾选后一键导入为主配置项。

v1.2.0 启动项窗口改造：扫描与 .lnk 解析移到后台线程、窗口打开即可操作；
新增可逆的「禁止启动 / 恢复启动」——注册表值先备份到 startup_backup.json
再删除，启动文件夹文件移入 disabled_startup 目录保留，全程可原样恢复。

v1.2.1 主界面行选择改造：去掉行首冗余的圆形单选按钮，只保留方形「启用」
复选框；点击行内任意位置即选中该行，选中行整行浅色高亮（保留偶数行斑马纹），
「删除选中行 / 上移 / 下移」作用于当前高亮行，选中状态按 uid 记录。

v1.2.2 图标改造：新增 app.ico 作为程序图标，打包时注入 exe 资源；运行期通过
resource_path() 兼容 PyInstaller 单文件解压目录（sys._MEIPASS），主窗口与
「本机启动项」子窗口统一显示同一图标。

作者：杨爽工作室
版本：1.2.2

命令行用法（静默执行，不打开界面，执行记录写入 run.log）：
    OneKeyRunTool.exe --run "配置名"      启动指定项
    OneKeyRunTool.exe --stop "配置名"     关闭指定项
    OneKeyRunTool.exe --run-all           启动全部已勾选（启用）项
    OneKeyRunTool.exe --stop-all          关闭全部正在运行的项
    OneKeyRunTool.exe --list              输出配置名称清单
"""

import base64
import ctypes
import json
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
import uuid
import winreg

# ---------------------------------------------------------------- 常量

APP_TITLE = "一键启停工具"
APP_VERSION = "1.2.2"
AUTHOR = "杨爽工作室"

ICON_NAME = "app.ico"

CREATE_NO_WINDOW = 0x08000000
CREATE_NEW_PROCESS_GROUP = 0x00000200
DETACHED_PROCESS = 0x00000008
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
STILL_ACTIVE = 259

FONT_NAME = "Microsoft YaHei UI"
FONT_NORMAL = (FONT_NAME, 10)
FONT_TITLE = (FONT_NAME, 14, "bold")
FONT_BOLD = (FONT_NAME, 10, "bold")

BG_COLOR = "#f3f5f7"
HEADER_BG = "#e8edf2"

# 主界面列表行配色：偶数行白底 / 奇数行浅灰斑马纹；选中行整行浅蓝高亮
ROW_BG_EVEN = "#ffffff"
ROW_BG_ODD = "#fafbfc"
ROW_BG_SEL = "#e4eefc"
ROW_EDGE_SEL = "#5a9bf0"


# ---------------------------------------------------------------- 路径

def app_dir():
    """返回程序自身所在目录（打包后为 exe 所在目录）。"""
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def resource_path(name):
    """返回随程序发布的资源文件路径。

    单文件打包后资源被解压到 sys._MEIPASS 临时目录，需优先到该目录查找；
    源码运行时资源与 .py 同目录。资源不存在时返回预期的原始路径，由调用方判断。
    """
    base = getattr(sys, "_MEIPASS", None)
    if base:
        cand = os.path.join(base, name)
        if os.path.exists(cand):
            return cand
    return os.path.join(app_dir(), name)


def _is_writable(path):
    try:
        os.makedirs(path, exist_ok=True)
        probe = os.path.join(path, ".~write_ok")
        with open(probe, "w", encoding="utf-8") as f:
            f.write("1")
        os.remove(probe)
        return True
    except Exception:
        return False


def data_dir():
    """配置优先与 exe 同目录；不可写时回落到 %APPDATA%\\OneKeyRunTool。"""
    base = app_dir()
    if _is_writable(base):
        return base
    alt = os.path.join(os.environ.get("APPDATA", base), "OneKeyRunTool")
    try:
        os.makedirs(alt, exist_ok=True)
        return alt
    except Exception:
        return base


DATA_DIR = data_dir()
CONFIG_PATH = os.path.join(DATA_DIR, "config.json")
STATE_PATH = os.path.join(DATA_DIR, "state.json")
LOG_PATH = os.path.join(DATA_DIR, "run.log")


# ---------------------------------------------------------------- 基础工具

def log(msg):
    line = "[%s] %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg)
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def fix_stdio():
    """--windowed 打包后 stdout/stderr 为 None，避免 print / 库输出报错。"""
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding="utf-8")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w", encoding="utf-8")


def enable_high_dpi():
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
        return
    except Exception:
        pass
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass


def apply_window_icon(window):
    """设置窗口图标（标题栏与任务栏），资源缺失或平台不支持时静默跳过。

    用 default= 形式设置，Tk 会把该图标套用到此后创建的所有顶层窗口，
    因此主窗口与「本机启动项」子窗口无需各自调用。
    """
    path = resource_path(ICON_NAME)
    if not os.path.exists(path):
        log("图标资源缺失，跳过设置: %s" % (path,))
        return
    try:
        window.iconbitmap(default=path)
    except Exception as e:
        log("设置图标失败: %r" % (e,))


def write_json_atomic(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def norm_path(p):
    if not p:
        return ""
    try:
        return os.path.normcase(os.path.abspath(os.path.expandvars(p)))
    except Exception:
        return os.path.normcase(p)


# ---------------------------------------------------------------- 配置

def default_config():
    system_root = os.environ.get("SystemRoot", r"C:\Windows")
    return {
        "version": 1,
        "programs": [
            {
                "uid": uuid.uuid4().hex[:12],
                "name": "记事本（示例）",
                "path": os.path.join(system_root, "System32", "notepad.exe"),
                "args": "",
                "cwd": "",
                "enabled": True,
            }
        ],
    }


def normalize_item(raw):
    if not isinstance(raw, dict):
        return None
    item = {
        "uid": str(raw.get("uid") or uuid.uuid4().hex[:12]),
        "name": str(raw.get("name") or "").strip(),
        "path": str(raw.get("path") or "").strip(),
        "args": str(raw.get("args") or ""),
        "cwd": str(raw.get("cwd") or "").strip(),
        "enabled": bool(raw.get("enabled", True)),
    }
    if not item["name"]:
        item["name"] = "未命名"
    return item


def load_config():
    if not os.path.isfile(CONFIG_PATH):
        cfg = default_config()
        try:
            save_config(cfg)
        except Exception as e:
            log("写入默认配置失败: %r" % (e,))
        return cfg
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8-sig") as f:
            cfg = json.load(f)
        if not isinstance(cfg, dict):
            raise ValueError("配置根节点不是对象")
    except Exception as e:
        log("读取配置失败，使用默认配置: %r" % (e,))
        return default_config()

    programs = []
    for raw in (cfg.get("programs") or []):
        item = normalize_item(raw)
        if item:
            programs.append(item)
    seed = uuid.uuid4().hex
    for i, item in enumerate(programs):
        if not item.get("uid"):
            item["uid"] = seed[i:i + 8]
    cfg["version"] = cfg.get("version", 1)
    cfg["programs"] = programs
    return cfg


def save_config(cfg):
    write_json_atomic(CONFIG_PATH, cfg)


# ---------------------------------------------------------------- 进程状态

def load_state():
    if not os.path.isfile(STATE_PATH):
        return {}
    try:
        with open(STATE_PATH, "r", encoding="utf-8-sig") as f:
            data = json.load(f)
        running = data.get("running")
        return running if isinstance(running, dict) else {}
    except Exception:
        return {}


def save_state(running):
    try:
        write_json_atomic(STATE_PATH, {"running": running})
    except Exception as e:
        log("保存运行状态失败: %r" % (e,))


def pid_alive(pid):
    if not pid:
        return False
    try:
        pid = int(pid)
    except Exception:
        return False
    k32 = ctypes.windll.kernel32
    handle = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return False
    try:
        code = ctypes.c_ulong(0)
        ok = k32.GetExitCodeProcess(handle, ctypes.byref(code))
        if not ok:
            return False
        return code.value == STILL_ACTIVE
    finally:
        k32.CloseHandle(handle)


def query_exe_path(pid):
    try:
        pid = int(pid)
    except Exception:
        return None
    k32 = ctypes.windll.kernel32
    handle = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return None
    try:
        buf = ctypes.create_unicode_buffer(2048)
        size = ctypes.c_ulong(2048)
        ok = k32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size))
        return buf.value if ok else None
    except Exception:
        return None
    finally:
        k32.CloseHandle(handle)


def running_record(uid, running):
    """返回有效的运行记录；PID 已退出或被系统复用时返回 None。"""
    rec = running.get(uid)
    if not isinstance(rec, dict):
        return None
    pid = rec.get("pid")
    if not pid_alive(pid):
        return None
    actual = query_exe_path(pid)
    expected = rec.get("exe")
    if actual and expected and norm_path(actual) != norm_path(expected):
        # PID 被复用，视为已结束，避免误杀其它进程
        return None
    return rec


def prune_state(running):
    """清理已结束进程的残留记录，返回（新的状态字典, 是否有变化）。"""
    cleaned = {}
    changed = False
    for uid, rec in running.items():
        if running_record(uid, running):
            cleaned[uid] = rec
        else:
            changed = True
    return cleaned, changed


def _taskkill(pid, force=False):
    cmd = ["taskkill", "/PID", str(pid), "/T"]
    if force:
        cmd.append("/F")
    try:
        result = subprocess.run(
            cmd,
            creationflags=CREATE_NO_WINDOW,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=15,
        )
        return result.returncode
    except Exception as e:
        log("taskkill 调用异常: %r" % (e,))
        return -1


# ---------------------------------------------------------------- 启停核心

def build_command(exe, args):
    ext = os.path.splitext(exe)[1].lower()
    if ext in (".bat", ".cmd"):
        inner = '"%s"' % exe
        if args:
            inner += " " + args
        return 'cmd.exe /c "%s"' % inner
    cmd = '"%s"' % exe
    if args:
        cmd += " " + args
    return cmd


def start_one(item, running):
    """启动一个配置项，返回 (是否成功, 提示语)。"""
    name = item.get("name") or "未命名"
    uid = item.get("uid") or name

    if running_record(uid, running):
        return True, "已在运行中，跳过"

    exe_raw = os.path.expandvars((item.get("path") or "").strip())
    if not exe_raw:
        return False, "未填写程序路径"

    cwd_raw = os.path.expandvars((item.get("cwd") or "").strip())
    if not os.path.isabs(exe_raw):
        base = cwd_raw if (cwd_raw and os.path.isdir(cwd_raw)) else DATA_DIR
        exe = os.path.abspath(os.path.join(base, exe_raw))
    else:
        exe = os.path.abspath(exe_raw)

    if not os.path.isfile(exe):
        return False, "程序不存在：%s" % exe

    if cwd_raw:
        if not os.path.isdir(cwd_raw):
            return False, "工作目录不存在：%s" % cwd_raw
        cwd = cwd_raw
    else:
        cwd = os.path.dirname(exe) or None

    args = (item.get("args") or "").strip()
    cmd = build_command(exe, args)

    try:
        proc = subprocess.Popen(
            cmd,
            cwd=cwd,
            creationflags=CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP | DETACHED_PROCESS,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
        )
    except Exception as e:
        log("启动失败 [%s] %s：%r" % (name, cmd, e))
        return False, "启动失败：%s" % e

    pid = proc.pid
    time.sleep(0.2)

    running[uid] = {
        "pid": pid,
        "exe": exe,
        "name": name,
        "cmd": cmd,
        "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    save_state(running)
    log("启动 [%s] pid=%s cmd=%s" % (name, pid, cmd))

    if not pid_alive(pid):
        return True, "已启动，但进程已立即退出（请检查程序与参数）"
    return True, "已启动，PID %s" % pid


def stop_one(name_or_uid, running, uid=None):
    """关闭一个配置项及其整棵子进程树，返回 (是否成功, 提示语)。"""
    key = uid if uid else name_or_uid
    rec = running_record(key, running)
    if not rec:
        if key in running:
            running.pop(key, None)
            save_state(running)
        return False, "未在运行"

    pid = int(rec["pid"])
    label = rec.get("name") or name_or_uid
    # 先温和关闭（发送 WM_CLOSE），GUI 程序可正常退出
    gentle_rc = _taskkill(pid, force=False)
    if gentle_rc == 0:
        deadline = time.time() + 2.5
        while time.time() < deadline and pid_alive(pid):
            time.sleep(0.15)
    if pid_alive(pid):
        # 温和关闭无效（无窗口程序或拒绝退出），强杀整棵进程树
        _taskkill(pid, force=True)
        deadline = time.time() + 5.0
        while time.time() < deadline and pid_alive(pid):
            time.sleep(0.1)

    if pid_alive(pid):
        log("关闭失败 [%s] pid=%s（可能权限不足）" % (label, pid))
        return False, "关闭失败（PID %s 仍在运行）" % pid

    running.pop(key, None)
    save_state(running)
    log("关闭 [%s] pid=%s" % (label, pid))
    return True, "已关闭"


def stop_all(running):
    """关闭所有运行中的项，返回 (成功数, 失败数与提示列表)。"""
    ok = 0
    fails = []
    for uid in list(running.keys()):
        rec = running.get(uid) or {}
        label = rec.get("name") or uid
        success, msg = stop_one(label, running, uid=uid)
        if success:
            ok += 1
        else:
            fails.append("%s：%s" % (label, msg))
    return ok, fails


def find_item(programs, name):
    if not name:
        return None
    target = name.strip()
    for item in programs:
        if item.get("name") == target:
            return item
    low = target.lower()
    for item in programs:
        if (item.get("name") or "").lower() == low:
            return item
    return None


# ---------------------------------------------------------------- 启动项扫描
# 说明：本分区只读扫描本机自启动项（注册表 Run 键 + 两个启动文件夹），
#      绝不修改、禁用或删除任何启动项。

REG_RUN_SOURCES = (
    ("HKCU Run", winreg.HKEY_CURRENT_USER,
     r"Software\Microsoft\Windows\CurrentVersion\Run"),
    ("HKLM Run", winreg.HKEY_LOCAL_MACHINE,
     r"Software\Microsoft\Windows\CurrentVersion\Run"),
    ("HKLM WOW6432Node Run", winreg.HKEY_LOCAL_MACHINE,
     r"Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Run"),
)

STARTUP_FILE_EXTS = (".lnk", ".exe", ".bat", ".cmd", ".com", ".vbs", ".url")
EXE_EXTS = (".exe", ".com", ".bat", ".cmd", ".vbs", ".lnk")


def split_command(command):
    """把启动命令拆成 (可执行文件, 参数)。

    兼容三种常见写法：
        1) "C:\\Program Files\\App\\app.exe" --flag   带引号路径
        2) C:\\Program Files\\App\\app.exe --flag     无引号含空格路径
        3) %ProgramFiles%\\App\\app.exe --flag        含环境变量路径
    路径原样保留（含 %VAR%），由 start_one() 在启动时展开。
    """
    text = (command or "").strip()
    if not text:
        return "", ""

    if text[0] in ('"', "'"):
        quote = text[0]
        end = text.find(quote, 1)
        if end > 1:
            return text[1:end].strip(), text[end + 1:].strip()

    if os.path.isfile(os.path.expandvars(text)):
        return text, ""

    exe, args = _split_by_exe_ext(text)
    if exe:
        return exe, args

    # 无引号含空格路径：从最长前缀开始逐级回退
    parts = text.split()
    for i in range(len(parts), 0, -1):
        candidate = " ".join(parts[:i])
        if os.path.isfile(os.path.expandvars(candidate)):
            return candidate, " ".join(parts[i:]).strip()

    head = parts[0]
    rest = text[len(head):].strip()
    if not os.path.dirname(head):
        located = shutil.which(os.path.expandvars(head))
        if located:
            return located, rest
    return head, rest


def _split_by_exe_ext(text):
    """在命令串中定位第一个真实存在的可执行文件路径，返回 (路径, 参数)。"""
    low = text.lower()
    for ext in EXE_EXTS:
        pos = low.find(ext)
        while pos != -1:
            end = pos + len(ext)
            candidate = text[:end].strip()
            if candidate and os.path.isfile(os.path.expandvars(candidate)):
                return candidate, text[end:].strip()
            pos = low.find(ext, end)
    return "", ""


def guess_cwd(exe):
    """默认工作目录取程序所在目录；裸文件名（无目录部分）返回空串。"""
    if not exe:
        return ""
    return os.path.dirname(exe)


def _reg_run_items():
    """只读读取三处 Run 注册表键，返回启动项列表。"""
    items = []
    for source, hive, subkey in REG_RUN_SOURCES:
        key = None
        try:
            key = winreg.OpenKey(hive, subkey, 0, winreg.KEY_READ)
        except OSError:
            continue
        try:
            total = winreg.QueryInfoKey(key)[1]
            for i in range(total):
                try:
                    name, value, _ = winreg.EnumValue(key, i)
                except OSError:
                    break
                if not isinstance(value, str):
                    continue
                command = value.strip()
                if not command:
                    continue
                items.append({
                    "name": (name or command).strip(),
                    "command": command,
                    "source": source,
                    "path": split_command(command)[0],
                })
        except OSError as e:
            log("读取注册表启动项失败 %s: %r" % (source, e))
        finally:
            try:
                if key is not None:
                    winreg.CloseKey(key)
            except Exception:
                pass
    return items


def startup_folder_dirs():
    """返回 [(来源名, 启动文件夹)]，只包含真实存在的目录。"""
    pairs = []
    appdata = os.environ.get("APPDATA") or ""
    if appdata:
        pairs.append(("用户启动文件夹", os.path.join(
            appdata, r"Microsoft\Windows\Start Menu\Programs\Startup")))
    programdata = os.environ.get("ProgramData") or ""
    if programdata:
        pairs.append(("公共启动文件夹", os.path.join(
            programdata, r"Microsoft\Windows\Start Menu\Programs\Startup")))
    return [(s, d) for s, d in pairs if os.path.isdir(d)]


def _read_url_file(path):
    """读取 .url 快捷方式中的 URL 行。"""
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                if line.strip().lower().startswith("url="):
                    return line.split("=", 1)[1].strip()
    except Exception:
        pass
    return ""


def resolve_lnk_targets(lnk_paths):
    """用系统自带 WScript.Shell 批量解析 .lnk。

    返回 {normcase(lnk 路径): (目标程序, 参数)}；解析失败返回空字典，
    调用方应降级为直接使用 .lnk 文件路径。
    """
    result = {}
    paths = [p for p in (lnk_paths or []) if p and os.path.isfile(p)]
    if not paths:
        return result

    # 合并为尽量少的 PowerShell 进程：按命令行长度分块，常规场景只需 1 次调用
    chunk, size = [], 0
    for p in paths:
        plen = len(p) + 1
        if chunk and size + plen > 18000:
            result.update(_resolve_lnk_batch(chunk))
            chunk, size = [], 0
        chunk.append(p)
        size += plen
    if chunk:
        result.update(_resolve_lnk_batch(chunk))
    return result


def _resolve_lnk_batch(paths):
    """解析一批（<=40 个）.lnk，返回 {normcase(lnk 路径): (目标, 参数)}。"""
    result = {}
    if not paths:
        return result

    joined = "|".join(p.replace("'", "''") for p in paths)
    lines = [
        "$ErrorActionPreference='SilentlyContinue'",
        "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8",
        "$sh=New-Object -ComObject WScript.Shell",
        "$raw='%s'" % joined,
        "foreach($p in $raw.Split([char]124)){",
        "\tif([string]::IsNullOrWhiteSpace($p)){continue}",
        "\tif(-not (Test-Path -LiteralPath $p)){continue}",
        "\ttry{$s=$sh.CreateShortcut($p);"
        "Write-Output ($p+[char]124+$s.TargetPath+[char]124+$s.Arguments)}catch{}",
        "}",
    ]
    script = "\r\n".join(lines)
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")

    try:
        proc = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive",
             "-EncodedCommand", encoded],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, creationflags=CREATE_NO_WINDOW, timeout=25)
        text = proc.stdout.decode("utf-8", "replace")
    except Exception as e:
        log("解析快捷方式失败: %r" % (e,))
        return result

    for line in text.splitlines():
        parts = line.strip().split("|")
        if len(parts) < 2 or not parts[0]:
            continue
        target = parts[1].strip()
        if not target:
            continue
        result[os.path.normcase(parts[0].strip())] = (target, "|".join(parts[2:]).strip())
    return result


def collect_startup_items():
    """只读收集本机全部自启动项，返回 [{'name','command','source','path'}]。"""
    items = _reg_run_items()

    folder_files = []
    for source, folder in startup_folder_dirs():
        try:
            names = sorted(os.listdir(folder))
        except OSError as e:
            log("扫描启动文件夹失败 %s: %r" % (folder, e))
            continue
        for fn in names:
            full = os.path.join(folder, fn)
            if not os.path.isfile(full):
                continue
            if os.path.splitext(fn)[1].lower() not in STARTUP_FILE_EXTS:
                continue
            folder_files.append((source, full, fn))

    lnks = [f for _, f, _ in folder_files if f.lower().endswith(".lnk")]
    resolved = resolve_lnk_targets(lnks)

    for source, full, fn in folder_files:
        command, target = full, full
        low = full.lower()
        if low.endswith(".lnk"):
            hit = resolved.get(os.path.normcase(full))
            if hit:
                target, extra = hit
                command = '"%s"' % target
                if extra:
                    command += " " + extra
        elif low.endswith(".url"):
            url = _read_url_file(full)
            if url:
                command, target = url, ""
        items.append({
            "name": os.path.splitext(fn)[0],
            "command": command,
            "source": source,
            "path": target,
            # file_path 记录启动文件夹中的真实文件（.lnk 原始文件），禁用/恢复按它搬运
            "file_path": full,
        })

    seen, unique = set(), []
    for it in items:
        key = (it["source"], it["name"], it["command"])
        if key in seen:
            continue
        seen.add(key)
        unique.append(it)
    return unique


# ---------------------------------------------------------------- 启动项禁用 / 恢复
# 说明：禁用操作先备份、后变更，绝不真正删除用户数据：
#      注册表项先把 名称 / 命令 / 值类型 / 来源 写入 startup_backup.json 再删值；
#      启动文件夹项把文件移入 disabled_startup 目录保留，原位置与来源均有记录；
#      两者都可通过「恢复启动」按备份原样还原。

STARTUP_BACKUP_PATH = os.path.join(DATA_DIR, "startup_backup.json")
DISABLED_DIR = os.path.join(DATA_DIR, "disabled_startup")

REG_TYPE_NAMES = {
    winreg.REG_SZ: "REG_SZ",
    winreg.REG_EXPAND_SZ: "REG_EXPAND_SZ",
}


def is_admin():
    """当前进程是否具备管理员权限。"""
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def _need_admin_msg():
    return "需要以管理员身份运行本工具"


def _is_denied(exc):
    """判断异常是否为权限不足（WinError 5 / PermissionError）。"""
    if isinstance(exc, PermissionError):
        return True
    return getattr(exc, "winerror", None) == 5


def _now_str():
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _reg_source_info(source):
    """按来源名反查 (hive, subkey)；非注册表来源返回 None。"""
    for name, hive, subkey in REG_RUN_SOURCES:
        if name == source:
            return hive, subkey
    return None


def _startup_folder_map():
    """来源名 -> 启动文件夹标准路径（不判断目录是否存在）。"""
    return {
        "用户启动文件夹": os.path.join(
            os.environ.get("APPDATA") or "",
            r"Microsoft\Windows\Start Menu\Programs\Startup"),
        "公共启动文件夹": os.path.join(
            os.environ.get("ProgramData") or "",
            r"Microsoft\Windows\Start Menu\Programs\Startup"),
    }


def _empty_backup():
    return {"version": 1, "registry": [], "files": []}


def load_startup_backup():
    """读取 startup_backup.json；文件不存在或损坏时返回空结构。"""
    try:
        with open(STARTUP_BACKUP_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return _empty_backup()
    except Exception as e:
        log("读取启动项备份失败: %r" % (e,))
        return _empty_backup()
    if not isinstance(data, dict):
        return _empty_backup()
    data.setdefault("version", 1)
    if not isinstance(data.get("registry"), list):
        data["registry"] = []
    if not isinstance(data.get("files"), list):
        data["files"] = []
    return data


def save_startup_backup(data):
    """原子写入 startup_backup.json。"""
    folder = os.path.dirname(STARTUP_BACKUP_PATH)
    if folder:
        os.makedirs(folder, exist_ok=True)
    write_json_atomic(STARTUP_BACKUP_PATH, data)


def _reg_backup_key(rec):
    return (rec.get("source", ""), rec.get("name", ""))


def _file_backup_key(rec):
    return norm_path(rec.get("original_path", ""))


def disabled_registry_keys(data=None):
    data = data if data is not None else load_startup_backup()
    return set(_reg_backup_key(r) for r in data.get("registry", []))


def disabled_file_paths(data=None):
    data = data if data is not None else load_startup_backup()
    paths = set()
    for rec in data.get("files", []):
        key = _file_backup_key(rec)
        if key:
            paths.add(key)
    return paths


def list_disabled_startup_items():
    """把备份记录整理成可展示的「已禁用」行；无记录返回空列表。"""
    data = load_startup_backup()
    rows = []
    for rec in data.get("registry", []):
        command = rec.get("command", "")
        rows.append({
            "kind": "registry",
            "name": rec.get("name", ""),
            "command": command,
            "source": rec.get("source", ""),
            "path": split_command(command)[0],
            "status": "已禁用",
            "disabled_at": rec.get("disabled_at", ""),
        })
    for rec in data.get("files", []):
        orig = rec.get("original_path", "")
        rows.append({
            "kind": "file",
            "name": rec.get("name", "") or os.path.basename(orig),
            "command": orig,
            "source": rec.get("source", ""),
            "path": orig,
            "file_path": orig,
            "status": "已禁用",
            "disabled_at": rec.get("disabled_at", ""),
        })
    return rows


def collect_startup_rows():
    """活跃启动项 + 已禁用项合并视图，每行附加 kind / status 字段。"""
    data = load_startup_backup()
    reg_keys = disabled_registry_keys(data)
    file_keys = disabled_file_paths(data)
    admin = is_admin()

    rows = []
    for it in collect_startup_items():
        row = dict(it)
        row["kind"] = "registry" if _reg_source_info(it.get("source")) else "file"
        if row["kind"] == "registry":
            row["status"] = "已禁用" if (it.get("source"), it.get("name")) in reg_keys else "启用"
        else:
            row["status"] = ("已禁用"
                             if norm_path(it.get("file_path") or it.get("path")) in file_keys
                             else "启用")
        row["need_admin"] = bool(row["kind"] == "registry"
                                 and row["source"] != "HKCU Run") or \
            bool(row["kind"] == "file" and row.get("source") == "公共启动文件夹")
        row["admin_ok"] = admin
        rows.append(row)

    active_reg = set((r.get("source"), r.get("name")) for r in rows
                     if r.get("kind") == "registry")
    active_file = set(norm_path(r.get("file_path") or r.get("path"))
                      for r in rows if r.get("kind") == "file")
    for rec in list_disabled_startup_items():
        if rec.get("kind") == "registry":
            if (rec.get("source"), rec.get("name")) in active_reg:
                continue
        elif norm_path(rec.get("path")) in active_file:
            continue
        rec["need_admin"] = bool(rec.get("kind") == "registry"
                                 and rec.get("source") != "HKCU Run") or \
            bool(rec.get("kind") == "file" and rec.get("source") == "公共启动文件夹")
        rec["admin_ok"] = admin
        rows.append(rec)
    return rows


def _unique_target_path(folder, filename):
    """在 folder 内为 filename 找一个不冲突的路径。"""
    base, ext = os.path.splitext(filename)
    target = os.path.join(folder, filename)
    seq = 2
    while os.path.exists(target):
        target = os.path.join(folder, "%s (%d)%s" % (base, seq, ext))
        seq += 1
    return target


def _drop_registry_record(data, rec):
    try:
        data["registry"].remove(rec)
    except ValueError:
        pass
    try:
        save_startup_backup(data)
    except Exception as e:
        log("回写启动项备份失败: %r" % (e,))


def _drop_file_record(data, rec):
    try:
        data["files"].remove(rec)
    except ValueError:
        pass
    try:
        save_startup_backup(data)
    except Exception as e:
        log("回写启动项备份失败: %r" % (e,))


def disable_registry_item(row, data):
    """备份后删除一条 Run 注册表值，返回 (是否成功, 说明)。"""
    source, name = row.get("source", ""), row.get("name", "")
    info = _reg_source_info(source)
    if info is None:
        return False, "非注册表来源，未做处理"
    hive, subkey = info
    if hive != winreg.HKEY_CURRENT_USER and not is_admin():
        return False, _need_admin_msg()
    if (source, name) in disabled_registry_keys(data):
        return False, "备份中已存在同名记录，未重复处理"

    key = None
    try:
        try:
            key = winreg.OpenKey(hive, subkey, 0, winreg.KEY_READ | winreg.KEY_SET_VALUE)
        except FileNotFoundError:
            return False, "对应的 Run 注册表键不存在"
        except OSError as e:
            return False, _need_admin_msg() if _is_denied(e) else "打开注册表失败：%s" % e
        try:
            value, vtype = winreg.QueryValueEx(key, name)
        except FileNotFoundError:
            return False, "该启动项已不存在"
        except OSError as e:
            return False, "读取注册表值失败：%s" % e
        if not isinstance(value, str):
            return False, "该启动项不是字符串值，未做处理"

        rec = {
            "name": name,
            "command": value,
            "value_type": int(vtype),
            "value_type_name": REG_TYPE_NAMES.get(vtype, str(vtype)),
            "source": source,
            "subkey": subkey,
            "disabled_at": _now_str(),
        }
        data.setdefault("registry", []).append(rec)
        try:
            save_startup_backup(data)
        except Exception as e:
            data["registry"].remove(rec)
            return False, "写入备份失败，未做任何更改：%s" % e

        try:
            winreg.DeleteValue(key, name)
        except OSError as e:
            data["registry"].remove(rec)
            try:
                save_startup_backup(data)
            except Exception:
                pass
            if _is_denied(e):
                return False, _need_admin_msg()
            return False, "删除注册表值失败，已回滚备份记录：%s" % e

        try:
            winreg.QueryValueEx(key, name)
        except FileNotFoundError:
            pass
        except OSError:
            pass
        else:
            log("删除注册表值后仍能读到 %s（%s）" % (name, source))
        return True, "已备份并删除 Run 值（%s）" % REG_TYPE_NAMES.get(vtype, str(vtype))
    finally:
        if key is not None:
            try:
                winreg.CloseKey(key)
            except Exception:
                pass


def disable_file_item(row, data):
    """把启动文件夹中的文件移入 disabled_startup，返回 (是否成功, 说明)。"""
    src = row.get("file_path") or row.get("path") or ""
    source = row.get("source", "")
    if not src or not os.path.isfile(src):
        return False, "该启动项文件已不存在"
    public_dir = _startup_folder_map().get("公共启动文件夹") or ""
    if public_dir and norm_path(os.path.dirname(src)) == norm_path(public_dir) \
            and not is_admin():
        return False, _need_admin_msg()
    if norm_path(src) in disabled_file_paths(data):
        return False, "备份中已存在该文件记录，未重复处理"

    try:
        os.makedirs(DISABLED_DIR, exist_ok=True)
    except Exception as e:
        return False, "创建 disabled_startup 目录失败：%s" % e

    try:
        target = _unique_target_path(DISABLED_DIR, os.path.basename(src))
        rec = {
            "name": os.path.basename(src),
            "original_path": src,
            "stored_path": target,
            "source": source,
            "disabled_at": _now_str(),
        }
        data.setdefault("files", []).append(rec)
        try:
            save_startup_backup(data)
        except Exception as e:
            data["files"].remove(rec)
            return False, "写入备份失败，未做任何更改：%s" % e

        try:
            shutil.move(src, target)
        except Exception as e:
            data["files"].remove(rec)
            try:
                save_startup_backup(data)
            except Exception:
                pass
            if _is_denied(e):
                return False, _need_admin_msg()
            return False, "移动文件失败，已回滚备份记录：%s" % e
        return True, "已移动到 disabled_startup（原名 %s）" % rec["name"]
    finally:
        pass


def disable_startup_items(rows):
    """批量禁用，返回 {'ok': [...], 'failed': [(名称, 原因)], 'skipped': [...]}。"""
    result = {"ok": [], "failed": [], "skipped": []}
    data = load_startup_backup()
    for row in rows or []:
        label = row.get("name") or row.get("path") or ""
        if row.get("status") == "已禁用":
            result["skipped"].append(label)
            continue
        kind = row.get("kind") or (
            "registry" if _reg_source_info(row.get("source")) else "file")
        if kind == "registry":
            ok, msg = disable_registry_item(row, data)
        else:
            ok, msg = disable_file_item(row, data)
        if ok:
            result["ok"].append(label)
            log("已禁用启动项：%s（%s，%s）" % (label, row.get("source", ""), msg))
        else:
            result["failed"].append((label, msg))
            log("禁用启动项失败：%s（%s）%s" % (label, row.get("source", ""), msg))
    return result


def _find_registry_record(data, row):
    key = (row.get("source", ""), row.get("name", ""))
    for rec in data.get("registry", []):
        if _reg_backup_key(rec) == key:
            return rec
    return None


def _find_file_record(data, row):
    key = norm_path(row.get("path", ""))
    for rec in data.get("files", []):
        if _file_backup_key(rec) == key:
            return rec
    return None


def restore_registry_item(rec, data):
    """按备份把注册表值原样写回，返回 (是否成功, 说明)。"""
    source, name = rec.get("source", ""), rec.get("name", "")
    info = _reg_source_info(source)
    if info is None:
        return False, "备份中的来源信息缺失，无法定位注册表键"
    hive, subkey = info
    if hive != winreg.HKEY_CURRENT_USER and not is_admin():
        return False, _need_admin_msg()
    value = rec.get("command", "")
    vtype = rec.get("value_type", winreg.REG_SZ)
    if vtype not in REG_TYPE_NAMES:
        vtype = winreg.REG_SZ

    key = None
    try:
        try:
            key = winreg.CreateKeyEx(hive, subkey, 0,
                                     winreg.KEY_READ | winreg.KEY_SET_VALUE)
        except OSError as e:
            return False, _need_admin_msg() if _is_denied(e) else "打开注册表失败：%s" % e

        try:
            exist, _exist_type = winreg.QueryValueEx(key, name)
            if isinstance(exist, str) and exist.strip() != value.strip():
                return False, "注册表中已存在同名启动项（内容不同），未覆盖"
        except FileNotFoundError:
            pass
        except OSError:
            pass

        try:
            winreg.SetValueEx(key, name, 0, vtype, value)
        except OSError as e:
            return False, _need_admin_msg() if _is_denied(e) else "写回注册表失败：%s" % e

        try:
            winreg.QueryValueEx(key, name)
        except OSError:
            return False, "写回后校验失败"
        return True, "已写回 Run 值（%s）" % REG_TYPE_NAMES.get(vtype, str(vtype))
    finally:
        if key is not None:
            try:
                winreg.CloseKey(key)
            except Exception:
                pass


def restore_file_item(rec, data):
    """把 disabled_startup 中的文件移回原位置，返回 (是否成功, 说明)。"""
    stored = rec.get("stored_path", "")
    orig = rec.get("original_path", "")
    if not stored or not os.path.isfile(stored):
        return False, "disabled_startup 中找不到该文件"
    if not orig:
        return False, "备份中缺少原路径"
    if os.path.exists(orig):
        return False, "原位置已存在同名文件，未覆盖"
    public_dir = _startup_folder_map().get("公共启动文件夹") or ""
    if public_dir and norm_path(os.path.dirname(orig)) == norm_path(public_dir) \
            and not is_admin():
        return False, _need_admin_msg()
    try:
        folder = os.path.dirname(orig)
        if folder:
            os.makedirs(folder, exist_ok=True)
        shutil.move(stored, orig)
    except Exception as e:
        if _is_denied(e):
            return False, _need_admin_msg()
        return False, "移回原位置失败：%s" % e
    return True, "已恢复到原位置"


def restore_startup_items(rows):
    """批量恢复，返回 {'ok': [...], 'failed': [(名称, 原因)], 'skipped': [...]}。"""
    result = {"ok": [], "failed": [], "skipped": []}
    data = load_startup_backup()
    for row in rows or []:
        label = row.get("name") or row.get("path") or ""
        if row.get("status") != "已禁用":
            result["skipped"].append(label)
            continue
        kind = row.get("kind")
        if kind == "registry":
            rec = _find_registry_record(data, row)
        else:
            rec = _find_file_record(data, row)
        if rec is None:
            result["failed"].append((label, "找不到对应的备份记录，未做处理"))
            continue
        if kind == "registry":
            ok, msg = restore_registry_item(rec, data)
        else:
            ok, msg = restore_file_item(rec, data)
        if ok:
            if kind == "registry":
                _drop_registry_record(data, rec)
            else:
                _drop_file_record(data, rec)
            result["ok"].append(label)
            log("已恢复启动项：%s（%s，%s）" % (label, row.get("source", ""), msg))
        else:
            result["failed"].append((label, msg))
            log("恢复启动项失败：%s（%s）%s" % (label, row.get("source", ""), msg))
    return result


# ---------------------------------------------------------------- 命令行

def parse_args(argv):
    """返回 (动作, 名称)。动作取值：run / stop / run-all / stop-all / list / help / None"""
    action = None
    name = None
    i = 0
    while i < len(argv):
        arg = argv[i]
        low = arg.lower()
        if arg in ("--run", "-r", "/run"):
            if i + 1 >= len(argv):
                return "error", "--run 缺少配置名"
            action, name = "run", argv[i + 1]
            i += 2
        elif arg in ("--stop", "-s", "/stop"):
            if i + 1 >= len(argv):
                return "error", "--stop 缺少配置名"
            action, name = "stop", argv[i + 1]
            i += 2
        elif low in ("--run-all", "/run-all", "-ra"):
            action = "run-all"
            i += 1
        elif low in ("--stop-all", "/stop-all", "-sa"):
            action = "stop-all"
            i += 1
        elif low in ("--list", "-l", "/list"):
            action = "list"
            i += 1
        elif low in ("--help", "-h", "/?", "-help"):
            action = "help"
            i += 1
        else:
            return "error", "未知参数：%s" % arg
    return action, name


HELP_TEXT = (
    "一键启停工具 用法：\n"
    "  不带参数                         打开图形界面\n"
    '  --run "名称"                    启动指定配置项\n'
    '  --stop "名称"                   关闭指定配置项（含子进程）\n'
    "  --run-all                       启动所有已勾选（启用）的项\n"
    "  --stop-all                      关闭所有正在运行的项\n"
    "  --list                          列出配置项名称\n"
    "  --help                          显示本帮助\n"
)


def run_cli(argv):
    action, name = parse_args(argv)
    cfg = load_config()
    programs = cfg.get("programs") or []
    running = load_state()

    if action == "error":
        log("参数错误：%s" % name)
        log("可执行 --help 查看用法")
        return 2

    if action == "help":
        log(HELP_TEXT.replace("\n", " | "))
        return 0

    if action == "list":
        if not programs:
            log("配置为空")
        for idx, item in enumerate(programs, 1):
            state = "运行中" if running_record(item["uid"], running) else "已停止"
            flag = "启用" if item.get("enabled") else "停用"
            log("%d. [%s][%s] %s | %s %s" % (
                idx, flag, state, item.get("name"),
                item.get("path"), item.get("args")))
        return 0

    if action == "run":
        item = find_item(programs, name)
        if not item:
            log("启动失败：找不到配置项「%s」" % name)
            return 2
        ok, msg = start_one(item, running)
        log("[%s] %s：%s" % ("成功" if ok else "失败", item.get("name"), msg))
        return 0 if ok else 1

    if action == "stop":
        item = find_item(programs, name)
        uid = item["uid"] if item else None
        if item:
            ok, msg = stop_one(item.get("name"), running, uid=uid)
        else:
            ok, msg = stop_one(name, running)
        log("[%s] %s：%s" % ("成功" if ok else "失败", name, msg))
        return 0 if ok else 1

    if action == "run-all":
        targets = [it for it in programs if it.get("enabled")]
        if not targets:
            log("启动失败：没有已勾选的配置项")
            return 1
        ok_count, fails = 0, []
        for item in targets:
            ok, msg = start_one(item, running)
            if ok:
                ok_count += 1
            else:
                fails.append("%s：%s" % (item.get("name"), msg))
        log("一键运行完成：成功 %d，失败 %d%s" % (
            ok_count, len(fails), ("；" + "；".join(fails)) if fails else ""))
        return 0 if not fails else 1

    if action == "stop-all":
        ok_count, fails = stop_all(running)
        log("一键关闭完成：成功 %d，失败 %d%s" % (
            ok_count, len(fails), ("；" + "；".join(fails)) if fails else ""))
        return 0 if not fails else 1

    return None


# ---------------------------------------------------------------- 图形界面

def uipath(p):
    return p


class StartupWindow(object):
    """「获取本机启动项」窗口。

    v1.2.0：
      · 扫描与 .lnk 解析放到后台线程，结果经队列 + after 轮询回填，主线程不阻塞
      · 打开窗口立即显示骨架与「正在扫描」提示，扫描完成后异步填充
      · 新增「状态」列与「禁止启动 / 恢复启动」按钮；禁用前先备份，随时可原样恢复
    """

    COLS = (
        ("勾选", 0, 0),
        ("显示名", 1, 2),
        ("启动命令（完整命令行，双击可复制）", 2, 5),
        ("来源", 3, 2),
        ("状态", 4, 1),
    )

    def __init__(self, app):
        tk = app.tk
        self.app = app
        self.tk = tk
        self.items = []
        self.vars = []
        self._queue = queue.Queue()
        self._scan_seq = 0

        self.win = tk.Toplevel(app.root)
        self.win.title("本机启动项 - %s v%s" % (APP_TITLE, APP_VERSION))
        self.win.configure(bg=BG_COLOR)
        try:
            self.win.transient(app.root)
        except Exception:
            pass

        self.status_var = tk.StringVar(value="正在扫描本机启动项...")
        self._build()
        self._center(1140, 660)
        self.win.protocol("WM_DELETE_WINDOW", self._on_close)
        # 先让窗口完成绘制，再触发后台扫描：打开瞬间即可拖动、不假死
        self.win.after(50, self.refresh)

    # ---------- 构建

    def _center(self, width, height):
        win = self.win
        win.update_idletasks()
        screen_w = win.winfo_screenwidth()
        screen_h = win.winfo_screenheight()
        width = min(width, max(900, screen_w - 80))
        height = min(height, max(520, screen_h - 120))
        x = max(0, (screen_w - width) // 2)
        y = max(0, (screen_h - height) // 3)
        win.geometry("%dx%d+%d+%d" % (width, height, x, y))
        win.minsize(900, 520)

    def _build(self):
        tk, ttk = self.tk, self.app.ttk

        top = tk.Frame(self.win, bg="#ffffff")
        top.pack(side="top", fill="x")
        tk.Label(top, text="本机启动项", font=FONT_TITLE, bg="#ffffff",
                 fg="#1f2d3d").pack(side="left", padx=(16, 8), pady=12)
        tk.Label(top, text="  扫描 Run 注册表与启动文件夹；禁止启动会先备份到 startup_backup.json"
                           "（文件移入 disabled_startup），可随时恢复",
                 font=(FONT_NAME, 9), bg="#ffffff",
                 fg="#8492a6").pack(side="left", pady=16)

        tk.Frame(self.win, bg="#dfe4ea", height=1).pack(side="top", fill="x")

        bar = tk.Frame(self.win, bg=BG_COLOR)
        bar.pack(side="top", fill="x", padx=14, pady=(10, 6))
        self.app._mk_button(bar, "刷新", "#ffffff", "#1f2d3d",
                            self.refresh, outline=True).pack(side="left", padx=(0, 6))
        self.app._mk_button(bar, "全选", "#ffffff", "#1f2d3d",
                            lambda: self._check_all(True), outline=True).pack(side="left", padx=6)
        self.app._mk_button(bar, "全不选", "#ffffff", "#1f2d3d",
                            lambda: self._check_all(False), outline=True).pack(side="left", padx=6)
        self.app._mk_button(bar, "禁止启动", "#ffffff", "#c0392b",
                            self.on_disable, outline=True).pack(side="left", padx=(18, 6))
        self.app._mk_button(bar, "恢复启动", "#ffffff", "#1f9d55",
                            self.on_restore, outline=True).pack(side="left", padx=6)
        self.app._mk_button(bar, "导入勾选项到主列表", "#1f9d55", "#ffffff",
                            self.on_import).pack(side="left", padx=(18, 6))
        self.app._mk_button(bar, "关闭", "#ffffff", "#1f2d3d",
                            self._on_close, outline=True).pack(side="right", padx=6)

        wrap = tk.Frame(self.win, bg=BG_COLOR)
        wrap.pack(side="top", fill="both", expand=True, padx=14, pady=(0, 6))

        self.canvas = tk.Canvas(wrap, bg="#ffffff", highlightthickness=1,
                                highlightbackground="#dfe4ea")
        vbar = ttk.Scrollbar(wrap, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=vbar.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        vbar.pack(side="right", fill="y")

        self.inner = tk.Frame(self.canvas, bg="#ffffff")
        self._inner_id = self.canvas.create_window((0, 0), window=self.inner, anchor="nw")
        self.inner.bind("<Configure>", self._on_inner_configure)
        self.canvas.bind("<Configure>", self._on_canvas_configure)
        self.canvas.bind("<MouseWheel>", self._on_mousewheel)

        bottom = tk.Frame(self.win, bg="#ffffff")
        bottom.pack(side="bottom", fill="x")
        tk.Frame(self.win, bg="#dfe4ea", height=1).pack(side="bottom", fill="x")
        tk.Label(bottom, textvariable=self.status_var, anchor="w", font=(FONT_NAME, 9),
                 bg="#ffffff", fg="#5a6b80").pack(side="left", padx=14, pady=7)

    def _on_close(self):
        self._scan_seq += 1
        try:
            self.win.destroy()
        except Exception:
            pass

    # ---------- 滚动

    def _on_inner_configure(self, event=None):
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))

    def _on_canvas_configure(self, event):
        self.canvas.itemconfigure(self._inner_id, width=event.width)

    def _on_mousewheel(self, event):
        try:
            self.canvas.yview_scroll(int(-1 * (event.delta / 120.0)), "units")
        except Exception:
            pass

    def _bind_wheel(self, widget):
        widget.bind("<MouseWheel>", self._on_mousewheel)
        for child in widget.winfo_children():
            self._bind_wheel(child)

    # ---------- 扫描（后台线程 + 主线程回填）

    def refresh(self):
        """立即返回：重置视图后启动后台扫描线程，结果由 _poll 回填。"""
        self._scan_seq += 1
        seq = self._scan_seq
        self.items = []
        self.status_var.set("正在扫描本机启动项...（后台执行，界面可正常操作）")
        self._render_loading()
        threading.Thread(target=self._scan_worker, args=(seq,), daemon=True).start()
        self.win.after(60, lambda: self._poll(seq))

    def _scan_worker(self, seq):
        try:
            rows = collect_startup_rows()
            err = ""
        except Exception as e:
            rows, err = [], repr(e)
        self._queue.put((seq, rows, err))

    def _poll(self, seq):
        if seq != self._scan_seq:
            return
        try:
            if not self.win.winfo_exists():
                return
        except Exception:
            return

        payload = None
        try:
            while True:
                got = self._queue.get_nowait()
                if got[0] == seq:
                    payload = got
                    break
        except queue.Empty:
            pass

        if payload is None:
            try:
                self.win.after(60, lambda: self._poll(seq))
            except Exception:
                pass
            return

        rows, err = payload[1], payload[2]
        if err:
            log("扫描启动项异常: %s" % err)
        self.items = rows
        self._render()
        self._update_count()

    def _clear_rows(self):
        for child in self.inner.winfo_children():
            child.destroy()
        self.vars = []

    def _render_loading(self):
        tk = self.tk
        self._clear_rows()
        tk.Label(self.inner, text="正在扫描本机启动项，请稍候...",
                 font=FONT_NORMAL, bg="#ffffff", fg="#8492a6",
                 pady=28).grid(row=0, column=0, columnspan=len(self.COLS))

    # ---------- 渲染

    def _render(self):
        tk = self.tk
        self._clear_rows()

        for _text, col, weight in self.COLS:
            self.inner.grid_columnconfigure(col, weight=weight)

        for text, col, _weight in self.COLS:
            tk.Label(self.inner, text=text, font=FONT_BOLD, bg=HEADER_BG, fg="#1f2d3d",
                     anchor="w", padx=8, pady=6).grid(row=0, column=col, sticky="we")

        if not self.items:
            tk.Label(self.inner, text="未发现任何启动项（含已禁用项）", font=FONT_NORMAL,
                     bg="#ffffff", fg="#8492a6", pady=24).grid(
                row=1, column=0, columnspan=len(self.COLS))
            return

        for idx, item in enumerate(self.items):
            row_no = idx + 1
            disabled = item.get("status") == "已禁用"
            bg = "#fdf6f5" if disabled else ("#ffffff" if idx % 2 == 0 else "#fafbfc")
            var = tk.BooleanVar(value=False)
            self.vars.append(var)

            tk.Checkbutton(self.inner, variable=var, bg=bg, activebackground=bg,
                           command=self._update_count).grid(
                row=row_no, column=0, padx=(10, 0), pady=4, sticky="w")
            tk.Label(self.inner, text=item.get("name", ""), font=FONT_NORMAL, bg=bg,
                     fg="#1f2d3d", anchor="w", padx=6).grid(
                row=row_no, column=1, sticky="we", pady=4)

            command = item.get("command", "")
            cell = tk.Label(self.inner, text=command, font=(FONT_NAME, 9), bg=bg,
                            fg="#3d4c5f", anchor="w", padx=6)
            cell.bind("<Double-Button-1>", lambda e, t=command: self._copy_command(t))
            cell.grid(row=row_no, column=2, sticky="we", padx=6, pady=5)

            tk.Label(self.inner, text=item.get("source", ""), font=(FONT_NAME, 9), bg=bg,
                     fg="#5a6b80", anchor="w", padx=6).grid(
                row=row_no, column=3, sticky="we", pady=4)

            tk.Label(self.inner, text=item.get("status", "启用"), font=(FONT_NAME, 9),
                     bg=bg, fg="#c0392b" if disabled else "#1f9d55",
                     anchor="w", padx=6).grid(row=row_no, column=4, sticky="we", pady=4)

        self._bind_wheel(self.inner)

    def _copy_command(self, text):
        """双击命令行单元格时复制完整命令，避免大段文本占满界面。"""
        try:
            self.win.clipboard_clear()
            self.win.clipboard_append(text)
            self.status_var.set("已复制命令：%s" % (text[:80] + ("..." if len(text) > 80 else "")))
        except Exception:
            pass

    def _update_count(self):
        chosen = sum(1 for v in self.vars if v.get())
        active = sum(1 for it in self.items if it.get("status") != "已禁用")
        disabled = len(self.items) - active
        extra = "" if is_admin() else "　|　非管理员权限：HKLM 与公共启动文件夹不可禁用"
        self.status_var.set("共 %d 项（启用 %d / 已禁用 %d），已勾选 %d 项%s"
                            % (len(self.items), active, disabled, chosen, extra))

    def _check_all(self, flag):
        for v in self.vars:
            v.set(bool(flag))
        self._update_count()

    # ---------- 禁止 / 恢复

    def _chosen(self):
        return [it for it, v in zip(self.items, self.vars) if v.get()]

    def _confirm_lines(self, rows, action):
        lines = []
        if action == "disable":
            lines.append("将禁止以下 %d 项开机自启动：" % len(rows))
        else:
            lines.append("将恢复以下 %d 项开机自启动：" % len(rows))
        for it in rows[:8]:
            lines.append("  · %s（%s）" % (it.get("name", ""), it.get("source", "")))
        if len(rows) > 8:
            lines.append("  · …等共 %d 项" % len(rows))
        lines.append("")
        if action == "disable":
            lines.append("影响说明：")
            lines.append("  · 注册表项：先把名称/命令/值类型/来源备份到 startup_backup.json，再删除 Run 值")
            lines.append("  · 启动文件夹项：文件移入 disabled_startup 目录保留，不会真正删除")
            lines.append("  · 变更后这些项将不再随开机自动运行，可随时用「恢复启动」原样还原")
            lines.append("  · HKLM 与公共启动文件夹需要以管理员身份运行本工具")
        else:
            lines.append("影响说明：")
            lines.append("  · 注册表项：按备份原样写回 Run 键")
            lines.append("  · 启动文件夹项：从 disabled_startup 移回原路径（不覆盖同名文件）")
            lines.append("  · 恢复后这些项会重新随开机自动运行")
        blocked = [it for it in rows if it.get("need_admin") and not it.get("admin_ok")]
        if blocked:
            lines.append("")
            lines.append("注意：其中 %d 项需要管理员权限，当前将失败并提示" % len(blocked))
        return lines

    def on_disable(self):
        from tkinter import messagebox
        chosen = self._chosen()
        if not chosen:
            messagebox.showinfo("提示", "请先勾选要禁止启动的项。", parent=self.win)
            return
        targets = [it for it in chosen if it.get("status") != "已禁用"]
        already = len(chosen) - len(targets)
        if not targets:
            messagebox.showinfo("提示", "勾选项均已处于「已禁用」状态。", parent=self.win)
            return
        if not messagebox.askyesno("确认禁止启动",
                                   "\n".join(self._confirm_lines(targets, "disable")),
                                   parent=self.win):
            return

        self.status_var.set("正在禁止启动...")
        try:
            self.win.update_idletasks()
        except Exception:
            pass
        res = disable_startup_items(targets)

        parts = []
        if res["ok"]:
            parts.append("已禁止 %d 项" % len(res["ok"]))
        if res["failed"]:
            parts.append("失败 %d 项" % len(res["failed"]))
        if already:
            parts.append("跳过已禁用 %d 项" % already)
        msg = "；".join(parts) or "未做任何变更"
        if res["failed"]:
            msg += "\n\n失败明细：\n" + "\n".join(
                "· %s：%s" % (n, r) for n, r in res["failed"][:8])
        self.status_var.set(msg.split("\n")[0])
        messagebox.showinfo("禁止启动结果", msg, parent=self.win)
        self.refresh()

    def on_restore(self):
        from tkinter import messagebox
        chosen = self._chosen()
        if not chosen:
            messagebox.showinfo("提示", "请先勾选要恢复启动的项。", parent=self.win)
            return
        targets = [it for it in chosen if it.get("status") == "已禁用"]
        already = len(chosen) - len(targets)
        if not targets:
            messagebox.showinfo("提示", "勾选项中没有「已禁用」的启动项。", parent=self.win)
            return
        if not messagebox.askyesno("确认恢复启动",
                                   "\n".join(self._confirm_lines(targets, "restore")),
                                   parent=self.win):
            return

        self.status_var.set("正在恢复启动...")
        try:
            self.win.update_idletasks()
        except Exception:
            pass
        res = restore_startup_items(targets)

        parts = []
        if res["ok"]:
            parts.append("已恢复 %d 项" % len(res["ok"]))
        if res["failed"]:
            parts.append("失败 %d 项" % len(res["failed"]))
        if already:
            parts.append("跳过非禁用项 %d 项" % already)
        msg = "；".join(parts) or "未做任何变更"
        if res["failed"]:
            msg += "\n\n失败明细：\n" + "\n".join(
                "· %s：%s" % (n, r) for n, r in res["failed"][:8])
        self.status_var.set(msg.split("\n")[0])
        messagebox.showinfo("恢复启动结果", msg, parent=self.win)
        self.refresh()

    # ---------- 导入

    def on_import(self):
        from tkinter import messagebox
        chosen = [it for it, v in zip(self.items, self.vars) if v.get()]
        if not chosen:
            messagebox.showinfo("提示", "请先勾选要导入的启动项。", parent=self.win)
            return
        enabled = [it for it in chosen if it.get("status") != "已禁用"]
        if not enabled:
            messagebox.showinfo("提示", "勾选项均为「已禁用」状态，无法导入主列表。",
                                parent=self.win)
            return
        added, skipped, failed = self.app.import_startup_items(enabled)
        parts = ["已导入 %d 条配置" % added]
        if skipped:
            head = "、".join(skipped[:3])
            if len(skipped) > 3:
                head += " 等"
            parts.append("路径已存在跳过 %d 条（%s）" % (len(skipped), head))
        if failed:
            parts.append("无法解析命令行 %d 条" % len(failed))
        msg = "；".join(parts)
        self.status_var.set(msg)
        messagebox.showinfo("导入结果", msg, parent=self.win)


class OneKeyApp(object):
    def __init__(self, root):
        import tkinter as tk
        from tkinter import ttk

        self.tk = tk
        self.ttk = ttk
        self.root = root
        self.cfg = load_config()
        self.running = load_state()
        self.rows = []
        self._selected_uid = None
        self._busy = False

        root.title("%s v%s - %s" % (APP_TITLE, APP_VERSION, AUTHOR))
        root.configure(bg=BG_COLOR)
        try:
            root.tk.call("tk", "scaling", root.winfo_fpixels("1i") / 72.0)
        except Exception:
            pass

        self._init_style()
        self._build_ui()
        self._load_rows()
        self._center_window(1180, 700)
        self._refresh_status()

    # ---------- 样式

    def _init_style(self):
        style = self.ttk.Style()
        for theme in ("vista", "winnative", "clam"):
            try:
                style.theme_use(theme)
                break
            except Exception:
                continue
        style.configure("TLabel", font=FONT_NORMAL, background=BG_COLOR)
        style.configure("TEntry", font=FONT_NORMAL)
        style.configure("TCheckbutton", font=FONT_NORMAL, background=BG_COLOR)
        style.configure("TButton", font=FONT_NORMAL, padding=(8, 3))
        style.configure("Head.TLabel", font=FONT_BOLD, background=HEADER_BG)

    def _center_window(self, width, height):
        self.root.update_idletasks()
        screen_w = self.root.winfo_screenwidth()
        screen_h = self.root.winfo_screenheight()
        width = min(width, max(900, screen_w - 80))
        height = min(height, max(560, screen_h - 120))
        x = max(0, (screen_w - width) // 2)
        y = max(0, (screen_h - height) // 3)
        self.root.geometry("%dx%d+%d+%d" % (width, height, x, y))
        self.root.minsize(900, 520)

    # ---------- 界面骨架

    def _build_ui(self):
        tk, ttk = self.tk, self.ttk

        # 顶部：标题 + 全局操作
        top = tk.Frame(self.root, bg="#ffffff", height=64)
        top.pack(side="top", fill="x")
        tk.Label(top, text="%s" % APP_TITLE, font=FONT_TITLE,
                 bg="#ffffff", fg="#1f2d3d").pack(side="left", padx=(16, 8), pady=12)
        tk.Label(top, text="  多条程序配置 · 一键批量启停 · %s" % AUTHOR,
                 font=(FONT_NAME, 9), bg="#ffffff", fg="#8492a6").pack(side="left", pady=16)

        btns = tk.Frame(top, bg="#ffffff")
        btns.pack(side="right", padx=14, pady=12)
        self._mk_button(btns, "一键运行", "#1f9d55", "#ffffff", self.on_run_all).pack(side="left", padx=5)
        self._mk_button(btns, "一键关闭", "#d93025", "#ffffff", self.on_stop_all).pack(side="left", padx=5)

        tk.Frame(self.root, bg="#dfe4ea", height=1).pack(side="top", fill="x")

        # 次级工具栏：配置维护
        bar = tk.Frame(self.root, bg=BG_COLOR)
        bar.pack(side="top", fill="x", padx=14, pady=(10, 6))
        self._mk_button(bar, "添加配置", "#ffffff", "#1f2d3d", self.on_add, outline=True).pack(side="left", padx=(0, 6))
        self._mk_button(bar, "删除选中行", "#ffffff", "#1f2d3d", self.on_delete, outline=True).pack(side="left", padx=6)
        self._mk_button(bar, "上移", "#ffffff", "#1f2d3d", self.on_move_up, outline=True).pack(side="left", padx=6)
        self._mk_button(bar, "下移", "#ffffff", "#1f2d3d", self.on_move_down, outline=True).pack(side="left", padx=6)
        self._mk_button(bar, "保存配置", "#ffffff", "#1f2d3d", self.on_save, outline=True).pack(side="left", padx=6)
        self._mk_button(bar, "获取本机启动项", "#eaf3ff", "#1a56b3",
                        self.on_open_startup, outline=True).pack(side="left", padx=(18, 6))

        tk.Label(bar, text="点击任意一行即可选中（用于删除 / 排序）",
                 font=(FONT_NAME, 9), bg=BG_COLOR, fg="#8492a6").pack(side="right")

        # 列表区（可滚动）
        wrap = tk.Frame(self.root, bg=BG_COLOR)
        wrap.pack(side="top", fill="both", expand=True, padx=14, pady=(0, 6))

        self.canvas = tk.Canvas(wrap, bg="#ffffff", highlightthickness=1,
                                highlightbackground="#dfe4ea")
        vbar = ttk.Scrollbar(wrap, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=vbar.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        vbar.pack(side="right", fill="y")

        self.inner = tk.Frame(self.canvas, bg="#ffffff")
        self._inner_id = self.canvas.create_window((0, 0), window=self.inner, anchor="nw")

        self.inner.bind("<Configure>", self._on_inner_configure)
        self.canvas.bind("<Configure>", self._on_canvas_configure)
        self.canvas.bind_all("<MouseWheel>", self._on_mousewheel)

        # 底部状态栏
        bottom = tk.Frame(self.root, bg="#ffffff")
        bottom.pack(side="bottom", fill="x")
        tk.Frame(self.root, bg="#dfe4ea", height=1).pack(side="bottom", fill="x")
        self.status_var = tk.StringVar(value="就绪")
        tk.Label(bottom, textvariable=self.status_var, anchor="w", font=(FONT_NAME, 9),
                 bg="#ffffff", fg="#5a6b80").pack(side="left", padx=14, pady=7)
        tk.Label(bottom, text="提示：关闭本窗口不会结束已启动的程序，可再次打开点「一键关闭」",
                 font=(FONT_NAME, 9), bg="#ffffff", fg="#a0aec0").pack(side="right", padx=14, pady=7)

    def _mk_button(self, parent, text, bg, fg, command, outline=False):
        tk = self.tk
        btn = tk.Button(
            parent, text=text, command=command, font=FONT_NORMAL,
            bg=bg, fg=fg, activebackground=bg, activeforeground=fg,
            relief="solid" if outline else "flat",
            bd=1 if outline else 0,
            highlightbackground="#cfd8e3" if outline else bg,
            padx=12, pady=4, cursor="hand2",
        )
        if outline:
            btn.configure(highlightthickness=1)
        return btn

    def _on_inner_configure(self, _event=None):
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))

    def _on_canvas_configure(self, event):
        self.canvas.itemconfigure(self._inner_id, width=event.width)

    def _on_mousewheel(self, event):
        try:
            self.canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")
        except Exception:
            pass

    # ---------- 行渲染

    def _build_header(self):
        tk = self.tk
        cols = [
            ("", 5, "center"),
            ("名称", 14, "w"),
            ("程序路径", 0, "w"),
            ("启动参数", 0, "w"),
            ("工作目录", 0, "w"),
            ("状态", 9, "center"),
            ("操作", 13, "center"),
        ]
        for col, (text, _w, anchor) in enumerate(cols):
            lbl = tk.Label(self.inner, text=text, font=FONT_BOLD, bg=HEADER_BG,
                           fg="#34495e", anchor=anchor, padx=6, pady=6)
            lbl.grid(row=0, column=col, sticky="ew", padx=1, pady=1)

    def _load_rows(self):
        for child in self.inner.winfo_children():
            child.destroy()
        self.rows = []
        self._build_header()

        tk, ttk = self.tk, self.ttk
        self.inner.grid_columnconfigure(0, weight=0)
        self.inner.grid_columnconfigure(1, weight=0)
        self.inner.grid_columnconfigure(2, weight=3)
        self.inner.grid_columnconfigure(3, weight=2)
        self.inner.grid_columnconfigure(4, weight=2)
        self.inner.grid_columnconfigure(5, weight=0)
        self.inner.grid_columnconfigure(6, weight=0)

        for idx, item in enumerate(self.cfg.get("programs") or []):
            row_no = idx + 1
            uid = item.get("uid")
            vars_ = {
                "item": item,
                "name": tk.StringVar(value=item.get("name", "")),
                "path": tk.StringVar(value=item.get("path", "")),
                "args": tk.StringVar(value=item.get("args", "")),
                "cwd": tk.StringVar(value=item.get("cwd", "")),
                "enabled": tk.BooleanVar(value=bool(item.get("enabled", True))),
                "status": tk.StringVar(value="已停止"),
                "parts": [],
            }
            item_bg = ROW_BG_EVEN if idx % 2 == 0 else ROW_BG_ODD
            parts = vars_["parts"]

            # 行首：仅保留方形「启用」勾选框（选中行靠点击行内任意位置）
            first = tk.Frame(self.inner, bg=item_bg)
            first.grid(row=row_no, column=0, sticky="nsew", padx=2, pady=2)
            edge = tk.Frame(first, bg=item_bg, width=3)
            edge.pack(side="left", fill="y", padx=(0, 5))
            vars_["edge"] = edge
            chk = tk.Checkbutton(first, variable=vars_["enabled"], bg=item_bg,
                                 activebackground=item_bg, selectcolor="#ffffff",
                                 bd=0, highlightthickness=0, cursor="hand2")
            chk.pack(side="left")
            parts.append((first, item_bg))
            parts.append((chk, item_bg))
            self._bind_row_click(first, uid)

            name_entry = tk.Entry(self.inner, textvariable=vars_["name"], font=FONT_NORMAL,
                                  width=14, relief="solid", bd=1)
            name_entry.grid(row=row_no, column=1, sticky="ew", padx=3, pady=3, ipady=2)
            parts.append((name_entry, "#ffffff"))

            path_box = tk.Frame(self.inner, bg=item_bg)
            path_box.grid(row=row_no, column=2, sticky="ew", padx=3, pady=3)
            path_box.grid_columnconfigure(0, weight=1)
            path_entry = tk.Entry(path_box, textvariable=vars_["path"], font=FONT_NORMAL,
                                  relief="solid", bd=1)
            path_entry.grid(row=0, column=0, sticky="ew", ipady=2)
            btn_path = tk.Button(path_box, text="浏览", font=(FONT_NAME, 9), relief="solid", bd=1,
                                 bg="#f5f7fa", highlightthickness=0, cursor="hand2",
                                 command=lambda r=vars_: self.on_pick_file(r))
            btn_path.grid(row=0, column=1, padx=(4, 0))
            parts.append((path_box, item_bg))
            parts.append((path_entry, "#ffffff"))
            parts.append((btn_path, "#f5f7fa"))

            args_entry = tk.Entry(self.inner, textvariable=vars_["args"], font=FONT_NORMAL,
                                  relief="solid", bd=1)
            args_entry.grid(row=row_no, column=3, sticky="ew", padx=3, pady=3, ipady=2)
            parts.append((args_entry, "#ffffff"))

            cwd_box = tk.Frame(self.inner, bg=item_bg)
            cwd_box.grid(row=row_no, column=4, sticky="ew", padx=3, pady=3)
            cwd_box.grid_columnconfigure(0, weight=1)
            cwd_entry = tk.Entry(cwd_box, textvariable=vars_["cwd"], font=FONT_NORMAL,
                                 relief="solid", bd=1)
            cwd_entry.grid(row=0, column=0, sticky="ew", ipady=2)
            btn_cwd = tk.Button(cwd_box, text="浏览", font=(FONT_NAME, 9), relief="solid", bd=1,
                                bg="#f5f7fa", highlightthickness=0, cursor="hand2",
                                command=lambda r=vars_: self.on_pick_dir(r))
            btn_cwd.grid(row=0, column=1, padx=(4, 0))
            parts.append((cwd_box, item_bg))
            parts.append((cwd_entry, "#ffffff"))
            parts.append((btn_cwd, "#f5f7fa"))

            status_label = tk.Label(self.inner, textvariable=vars_["status"],
                                    font=(FONT_NAME, 9, "bold"),
                                    bg=item_bg, fg="#8a94a6", width=9)
            status_label.grid(row=row_no, column=5, sticky="nsew", padx=2, pady=3)
            vars_["status_label"] = status_label
            parts.append((status_label, item_bg))

            act = tk.Frame(self.inner, bg=item_bg)
            act.grid(row=row_no, column=6, sticky="nsew", padx=2, pady=3)
            btn_run = tk.Button(act, text="运行", font=(FONT_NAME, 9), bg="#e8f5ee", fg="#1f9d55",
                                relief="solid", bd=1, highlightthickness=0, cursor="hand2", width=5,
                                command=lambda r=vars_: self.on_run_one(r))
            btn_run.pack(side="left", padx=(0, 4))
            btn_stop = tk.Button(act, text="关闭", font=(FONT_NAME, 9), bg="#fdeceb", fg="#d93025",
                                 relief="solid", bd=1, highlightthickness=0, cursor="hand2", width=5,
                                 command=lambda r=vars_: self.on_stop_one(r))
            btn_stop.pack(side="left")
            parts.append((act, item_bg))

            # 点击行内任意位置（输入框、勾选框、状态、按钮、空白容器）即选中该行
            for widget, _base in parts:
                self._bind_row_click(widget, uid)
            self._bind_row_click(btn_run, uid)
            self._bind_row_click(btn_stop, uid)

            self.rows.append(vars_)

        self._apply_row_styles()

    # ---------- 行选中（按 uid 记录，不依赖行号）

    def _bind_row_click(self, widget, uid):
        if not uid:
            return
        try:
            widget.bind("<Button-1>", lambda _e, u=uid: self.select_row(u), add="+")
        except Exception:
            pass

    def select_row(self, uid):
        """把某一行设为当前选中行（uid 为空表示取消选中）。"""
        if uid == getattr(self, "_selected_uid", None):
            self._apply_row_styles()
            return
        self._selected_uid = uid or None
        self._apply_row_styles()

    def selected_uid(self):
        return getattr(self, "_selected_uid", None)

    def selected_index(self):
        """返回当前选中行在 cfg["programs"] 中的下标，未选中返回 -1。"""
        uid = getattr(self, "_selected_uid", None)
        if not uid:
            return -1
        for i, item in enumerate(self.cfg.get("programs") or []):
            if item.get("uid") == uid:
                return i
        return -1

    def _apply_row_styles(self):
        """按「选中 + 斑马纹」重算整行底色，刷新状态时也调用以保持高亮。"""
        sel_uid = getattr(self, "_selected_uid", None)
        if sel_uid:
            progs = self.cfg.get("programs") or []
            if not any(p.get("uid") == sel_uid for p in progs):
                self._selected_uid = None
                sel_uid = None

        for idx, row in enumerate(self.rows):
            sel = bool(sel_uid) and row["item"].get("uid") == sel_uid
            base_row_bg = ROW_BG_EVEN if idx % 2 == 0 else ROW_BG_ODD
            for widget, base_bg in row.get("parts") or []:
                want = ROW_BG_SEL if sel else base_bg
                try:
                    if str(widget.cget("bg")) != want:
                        widget.configure(bg=want)
                except Exception:
                    pass
                try:
                    if str(widget.cget("activebackground")) != want:
                        widget.configure(activebackground=want)
                except Exception:
                    pass
            edge = row.get("edge")
            if edge is not None:
                try:
                    edge.configure(bg=ROW_EDGE_SEL if sel else base_row_bg)
                except Exception:
                    pass

    # ---------- 数据同步

    def _sync_from_ui(self):
        for row in self.rows:
            item = row["item"]
            item["name"] = row["name"].get().strip() or "未命名"
            item["path"] = row["path"].get().strip()
            item["args"] = row["args"].get()
            item["cwd"] = row["cwd"].get().strip()
            item["enabled"] = bool(row["enabled"].get())

    def _persist(self, tip=None):
        self._sync_from_ui()
        try:
            save_config(self.cfg)
            if tip:
                self.status_var.set(tip)
        except Exception as e:
            log("保存配置失败: %r" % (e,))
            self.status_var.set("保存配置失败：%s" % e)

    # ---------- 界面动作

    def on_pick_file(self, row):
        from tkinter import filedialog
        path = filedialog.askopenfilename(
            title="选择程序",
            filetypes=[("可执行程序", "*.exe *.bat *.cmd"), ("所有文件", "*.*")],
        )
        if path:
            row["path"].set(path.replace("/", "\\"))
            if not row["name"].get().strip():
                row["name"].set(os.path.splitext(os.path.basename(path))[0])

    def on_pick_dir(self, row):
        from tkinter import filedialog
        path = filedialog.askdirectory(title="选择工作目录")
        if path:
            row["cwd"].set(path.replace("/", "\\"))

    def on_add(self):
        self._persist()
        new_uid = uuid.uuid4().hex[:12]
        self.cfg["programs"].append({
            "uid": new_uid,
            "name": "新建配置 %d" % (len(self.cfg["programs"]) + 1),
            "path": "",
            "args": "",
            "cwd": "",
            "enabled": True,
        })
        self._selected_uid = new_uid
        self._load_rows()
        self._persist("已添加配置项，请填写程序路径后保存")
        self._refresh_status()

    def on_delete(self):
        idx = self.selected_index()
        if idx < 0:
            from tkinter import messagebox
            messagebox.showinfo("提示", "请先点击要操作的行。")
            return
        self._persist()
        progs = self.cfg["programs"]
        if idx >= len(progs):
            return
        item = progs.pop(idx)
        self.running.pop(item.get("uid"), None)
        save_state(self.running)
        if progs:
            self._selected_uid = progs[min(idx, len(progs) - 1)].get("uid")
        else:
            self._selected_uid = None
        self._load_rows()
        self._persist("已删除配置项「%s」" % item.get("name"))
        self._refresh_status()

    def _move(self, step):
        idx = self.selected_index()
        if idx < 0:
            from tkinter import messagebox
            messagebox.showinfo("提示", "请先点击要操作的行。")
            return
        self._persist()
        new_idx = idx + step
        if new_idx < 0 or new_idx >= len(self.cfg["programs"]):
            return
        progs = self.cfg["programs"]
        progs[idx], progs[new_idx] = progs[new_idx], progs[idx]
        self._load_rows()
        self._persist("已调整顺序")
        self._refresh_status()

    def on_move_up(self):
        self._move(-1)

    def on_move_down(self):
        self._move(1)

    # ---------- 启动项导入

    def on_open_startup(self):
        win = getattr(self, "_startup_win", None)
        if win is not None:
            try:
                if win.win.winfo_exists():
                    win.win.deiconify()
                    win.win.lift()
                    win.win.focus_set()
                    return
            except Exception:
                pass
        self._startup_win = StartupWindow(self)

    def import_startup_items(self, items):
        """把勾选的启动项导入为主配置。

        返回 (新增数量, 路径已存在而跳过的名称列表, 无法解析命令行的名称列表)。
        """
        self._persist()
        programs = self.cfg.setdefault("programs", [])

        exist_paths, exist_names = set(), set()
        for it in programs:
            key = norm_path(it.get("path"))
            if key:
                exist_paths.add(key)
            name = (it.get("name") or "").strip()
            if name:
                exist_names.add(name.lower())

        added, skipped, failed = 0, [], []
        for src in items:
            label = (src.get("name") or "").strip()
            exe, args = split_command(src.get("command"))
            key = norm_path(exe) if exe else ""
            if not key:
                failed.append(label or (src.get("command") or ""))
                continue
            if key in exist_paths:
                skipped.append(label or exe)
                continue

            name = label or os.path.splitext(os.path.basename(exe))[0] or "启动项"
            name = name[:60]
            if name.lower() in exist_names:
                base, seq = name, 2
                while ("%s (%d)" % (base, seq)).lower() in exist_names:
                    seq += 1
                name = "%s (%d)" % (base, seq)

            programs.append({
                "uid": uuid.uuid4().hex[:12],
                "name": name,
                "path": exe,
                "args": args,
                "cwd": guess_cwd(exe),
                "enabled": True,
            })
            exist_paths.add(key)
            exist_names.add(name.lower())
            added += 1

        if added:
            self._load_rows()
            self._persist("已从启动项导入 %d 条配置，按需勾选后点「一键运行」" % added)
        return added, skipped, failed

    def on_save(self):
        self._persist("配置已保存到 %s" % CONFIG_PATH)

    def on_run_one(self, row):
        self._persist()
        ok, msg = start_one(row["item"], self.running)
        self.status_var.set("[%s] %s：%s" % ("成功" if ok else "失败", row["item"].get("name"), msg))
        self._refresh_status()

    def on_stop_one(self, row):
        self._persist()
        ok, msg = stop_one(row["item"].get("name"), self.running, uid=row["item"].get("uid"))
        self.status_var.set("[%s] %s：%s" % ("成功" if ok else "失败", row["item"].get("name"), msg))
        self._refresh_status()

    def on_run_all(self):
        self._persist()
        targets = [r for r in self.rows if r["enabled"].get()]
        if not targets:
            from tkinter import messagebox
            messagebox.showwarning("提示", "没有已勾选的配置项，请先勾选要启动的程序。")
            return
        self.status_var.set("正在启动 %d 个程序..." % len(targets))
        self.root.update_idletasks()
        ok_count, fails = 0, []
        for row in targets:
            ok, msg = start_one(row["item"], self.running)
            if ok:
                ok_count += 1
            else:
                fails.append("%s：%s" % (row["item"].get("name"), msg))
        text = "一键运行完成：成功 %d，失败 %d" % (ok_count, len(fails))
        if fails:
            text += " ｜ " + "；".join(fails)
        self.status_var.set(text)
        self._refresh_status()

    def on_stop_all(self):
        self._persist()
        self.running = load_state()
        if not self.running:
            self.status_var.set("当前没有正在运行的程序")
            self._refresh_status()
            return
        self.status_var.set("正在关闭已启动的程序...")
        self.root.update_idletasks()
        ok_count, fails = stop_all(self.running)
        text = "一键关闭完成：成功 %d，失败 %d" % (ok_count, len(fails))
        if fails:
            text += " ｜ " + "；".join(fails)
        self.status_var.set(text)
        self._refresh_status()

    # ---------- 状态刷新

    def _refresh_status(self):
        try:
            self.running = load_state()
            cleaned, changed = prune_state(self.running)
            if changed:
                self.running = cleaned
                save_state(self.running)

            for row in self.rows:
                rec = running_record(row["item"].get("uid"), self.running)
                label = row.get("status_label")
                if rec:
                    row["status"].set("运行中")
                    if label is not None:
                        label.configure(fg="#1f9d55")
                else:
                    row["status"].set("已停止")
                    if label is not None:
                        label.configure(fg="#8a94a6")
            # 状态刷新会改写行内颜色，这里按 uid 重新套用高亮，保证选中行不丢失
            self._apply_row_styles()
        except Exception as e:
            log("刷新状态异常: %r" % (e,))
        finally:
            self.root.after(1500, self._refresh_status)


# ---------------------------------------------------------------- 入口

def main():
    fix_stdio()
    enable_high_dpi()

    argv = sys.argv[1:]
    if argv:
        code = run_cli(argv)
        if code is None:
            code = 0
        sys.exit(code)

    import tkinter as tk
    root = tk.Tk()
    apply_window_icon(root)
    OneKeyApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
