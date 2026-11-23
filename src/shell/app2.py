# -*- coding: utf-8 -*-
"""app2.py —— 夸克网盘加速器（Edge --app 外壳版）。

架构：
  界面   = 本地 http://127.0.0.1:PORT/ 的 ui.html（视觉还原夸克网页版）
  外壳   = msedge.exe --app=…  （同一 Chromium 内核，无 pywebview 包装层）
  引擎   = engine.Engine（复用 quark_core.Downloader 的分片并发 + 完整性校验）
  通道   = 页面 ←同源→ /api/*（不用 CDP 回灌，避开 PNA）
"""
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import webbrowser

# onefile 的 --runtime-tmpdir . 相对的是**启动时的 CWD**，不是 exe 目录。
# 从任务计划、别的程序 Popen（起始位置被改过）启动时 CWD 可能是 System32，
# 解压就会失败。这里先把 CWD 钉到 exe 所在目录，双击之外的启动方式也稳。
if getattr(sys, "frozen", False):
    try:
        os.chdir(os.path.dirname(os.path.abspath(sys.executable)))
    except OSError:
        pass


def data_dir():
    """数据目录：第一个真正可写的位置。

    Edge 的 --user-data-dir 必须在可写目录里建 SingletonLock，否则它以 rc=21
    (ERROR_ACCESS_DENIED) 直接退出，界面永远不出现。受限环境（只允许写工作区）
    下 %LOCALAPPDATA% 会拒绝写入，所以要逐级探测、降级到 exe 同目录。
    结果缓存在 _DATA 里，进程生命周期内只探一次。
    """
    global _DATA
    if _DATA:
        return _DATA
    home = os.path.expanduser("~")
    cands = [
        os.path.join(os.environ.get("LOCALAPPDATA") or home, "QuarkAccel"),
        os.path.join(os.environ.get("APPDATA") or home, "QuarkAccel"),
        os.path.join(os.path.dirname(os.path.abspath(sys.executable)), "data"),
        os.path.join(tempfile.gettempdir(), "QuarkAccel"),
        os.path.join(home, ".quark_accel"),
    ]
    for d in cands:
        try:
            os.makedirs(d, exist_ok=True)
            probe = os.path.join(d, ".w")
            with io.open(probe, "w", encoding="utf-8") as f:
                f.write("1")
            os.remove(probe)
            _DATA = d
            return d
        except Exception:
            continue
    _DATA = os.path.join(home, ".quark_accel")
    return _DATA


_DATA = None


# 启动探针：打包后如果窗口没出来，看这个文件停在哪个 import
def _boot(msg):
    try:
        p = os.path.join(data_dir(), "boot.log")
        with io.open(p, "a", encoding="utf-8") as f:
            f.write(msg + "\n")
    except Exception:
        pass


_boot("0 start %s" % __file__)

FROZEN = bool(getattr(sys, "frozen", False))
if FROZEN:
    HERE = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(sys.executable)))
    ROOT = os.path.dirname(os.path.abspath(sys.executable))   # exe 同目录放 cookie.txt
else:
    HERE = os.path.dirname(os.path.abspath(__file__))
    ROOT = os.path.dirname(HERE)
for p in (ROOT, HERE):
    if p not in sys.path:
        sys.path.insert(0, p)

_boot("1 path ok")
import importer                                    # noqa: E402
import profiles                                    # noqa: E402
import procs                                       # noqa: E402
import stream                                      # noqa: E402
_boot("2 profiles")
from engine import Engine                           # noqa: E402
_boot("3 engine")
from quark_core import Api, COOKIE_KEYS             # noqa: E402
_boot("4 quark_core")
from server import Server                           # noqa: E402
_boot("5 server")

TITLE = "夸克网盘下载器"
CFG = os.path.join(os.path.expanduser("~"), ".quark_accel.json")
COOKIE_TXT = os.path.join(ROOT, "cookie.txt")
LOG_FILE = os.path.join(data_dir(), "shell.log")
# CDP 端口：--remote-debugging-port=0 让 Chromium 选一个随机空闲端口，只把端口号
# 写进 <profile>/DevToolsActivePort。这样本机别的网页猜不到端口，也就没法连上来用
# Runtime.evaluate 打 /api/*（CDP 本身没有任何鉴权）。
# QA_DEV_PORT 只在自动化测试/截图时指定固定端口用。
try:
    _dev = os.environ.get("QA_DEV_PORT") or ""
    DEV_PORT = int(_dev) if _dev.strip() else 0
except ValueError:
    DEV_PORT = 0
EDGE_PATHS = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
]
# 子进程一律不建控制台：windowed 程序起 powershell 会闪一个黑框，用户会以为中毒
CREATE_NO_WINDOW = 0x08000000
# 有些环境（受限 Job / 安全产品）里 Chromium 的渲染进程起不来，表现是纯白窗口。
# 这套参数能绕过，代价是沙箱降级 —— 只在探测到白屏时用，并记进配置免得每次都白一次。
EDGE_FALLBACK = ["--no-sandbox", "--disable-gpu", "--test-type"]
LOGIN_PORT = 9481
MODE_KEY = "edge_mode"          # standard（默认）| fallback（已记住能用的那套）


def edge_mode():
    try:
        return "fallback" if (load_cfg().get(MODE_KEY) == "fallback") else "standard"
    except Exception:
        return "standard"


def set_edge_mode(mode):
    try:
        cfg = load_cfg()
        if cfg.get(MODE_KEY) != mode:
            cfg[MODE_KEY] = mode
            save_cfg(cfg)
    except Exception:
        pass


# ---------------------------------------------------------------- config
def shell_open(path, select=False):
    """用资源管理器打开目录（或定位到某个文件）。

    受限令牌下 os.startfile 抛 WinError 5、explorer.exe /e, 直接以
    0xC0000142（STATUS_DLL_INIT_FAILED）退出 —— 两种情况 Popen 都「成功」
    返回，但桌面上什么也没出现，用户看到的就是「打开下载目录没反应」。
    所以这里按「能确实打开」的顺序试，第一条成功的返回 (True, "")：
      1. os.startfile（正常用户令牌下最快）
      2. Shell.Application 的 Explore / InvokeVerb（走已在运行的 explorer，
         不依赖本进程能不能启动 shell 进程）
      3. explorer.exe 带 /select 或 /e 参数
    """
    path = os.path.normpath(path)
    last = ""
    # 窗口标题 = 被打开的目录名（定位到文件时是它的父目录名），用它来验证
    want = os.path.basename(path)
    if select and not os.path.isdir(path):
        want = os.path.basename(os.path.dirname(path)) or want

    def done(tag, wait=1.5):
        """每一次「看起来成功」都要先确认窗口真的出来了。"""
        if _explorer_window_open(want, wait):
            return True, ""
        return False, tag + " 返回成功但没出现窗口"

    # 顺序按「起得来 + 快」排，别按经典写法排。实测（受限令牌下）：
    #   startfile      0.09 s  → PermissionError
    #   explorer Popen 0.01 s  → 进程立刻退，桌面无窗口
    #   cmd start     15.01 s  → 一直挂着直到超时
    #   powershell    25.04 s  → 同上
    # 后两条一旦排前面，用户点一下要等四十秒，等于死机。所以把两秒级的放前面，
    # 挂死的放最后并给死线。

    # 1) startfile：正常用户令牌下这一步就够了
    blocked = False
    try:
        os.startfile(path)  # noqa: S606
        ok, why = done("startfile", 1.4)
        if ok:
            return True, ""
        last = why
    except PermissionError as e:
        # 令牌不允许 ShellExecute。这时 cmd start 和 powershell 会各挂十几秒
        # 才超时，用户点了按钮像死机 —— 直接认定这条路走不通。
        blocked = True
        last = "startfile: %s" % str(e)[:50]
    except Exception as e:
        last = "startfile: %s" % str(e)[:50]

    # 2) explorer.exe：即使自己立刻退出，也会把请求转给已在运行的实例。
    #    参数必须拼成一个 argv：写成 ["explorer", "/select,", path] 时，路径里的
    #    空格会被 explorer 当成第二个选择项，表现就是「什么也没发生」。
    try:
        if select and os.path.exists(path):
            subprocess.Popen(["explorer", "/select," + path])
        else:
            subprocess.Popen(["explorer", "/e," + path])
        ok, why = done("explorer", 1.4)
        if ok:
            return True, ""
        last = why
    except Exception as e:
        last = "explorer: %s" % str(e)[:60]
    if blocked:
        # startfile 已经明确说「拒绝访问」，后面两条只会各挂十几秒。
        return False, last

    # 3) cmd start（走 ShellExecute，和双击等价；路径带空格/中文时最稳）
    try:
        subprocess.run('start "" "%s"' % path, shell=True,
                       capture_output=True, timeout=5,
                       creationflags=CREATE_NO_WINDOW)
        ok, why = done("cmd start")
        if ok:
            return True, ""
        last = why
    except Exception as e:
        last = "cmd start: %s" % str(e)[:60]

    # 4) Shell.Application（走已在运行的 explorer，不依赖本进程能不能起 shell 进程）
    ps = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command"]
    q = path.replace("'", "''")
    scripts = []
    if select and os.path.exists(path):
        dd = os.path.dirname(path).replace("'", "''")
        nn = os.path.basename(path).replace("'", "''")
        scripts.append(
            "$s = New-Object -ComObject Shell.Application;"
            "$f = $s.Namespace('%s'); $i = $f.ParseName('%s');"
            "if ($i) { $i.InvokeVerb('open') } else { $s.Explore('%s') }" % (dd, nn, dd))
    scripts.append("$s = New-Object -ComObject Shell.Application; $s.Explore('%s')" % q)
    for s in scripts:
        try:
            r = subprocess.run(ps + [s], capture_output=True, timeout=8,
                               creationflags=CREATE_NO_WINDOW)
            ok, why = done("Shell.Application")
            if ok:
                return True, ""
            last = why if r.returncode == 0 else "powershell rc=%d" % r.returncode
        except Exception as e:
            last = "powershell: %s" % str(e)[:50]
    return False, last


def _explorer_window_open(name, seconds=1.2):
    """在 seconds 秒内看有没有标题里带 name 的资源管理器窗口出现。

    为什么必须验证：受限令牌下 Shell.Application.Explore() 返回 0、
    `cmd /c start` 返回 0、Popen(explorer) 返回 0 —— 三条路都「成功」，
    桌面上却什么也没有。用户看到的就是按钮点了没反应。只有真的去数窗口，
    才能知道这一次到底有没有打开，失败时才好把路径交给用户。
    """
    try:
        import ctypes
        from ctypes import wintypes
        u32 = ctypes.windll.user32
        proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        key = (name or "").strip().lower()
        if not key:
            return False

        def scan():
            hit = []

            def cb(hwnd, _lp):
                if not u32.IsWindowVisible(hwnd):
                    return True
                n = u32.GetWindowTextLengthW(hwnd)
                if n <= 0:
                    return True
                buf = ctypes.create_unicode_buffer(n + 1)
                u32.GetWindowTextW(hwnd, buf, n + 1)
                if key in buf.value.lower():
                    hit.append(buf.value)
                return True

            u32.EnumWindows(proc(cb), 0)
            return bool(hit)

        deadline = time.time() + max(0.2, seconds)
        while time.time() < deadline:
            if scan():
                return True
            time.sleep(0.15)
        return False
    except Exception:
        return True      # 查不了就别误报失败，宁可当成打开了


def writable_dir(d):
    """真的写一次再删 —— os.access 在 Windows 上对 ACL 不可靠，判断结果常常是假的。

    目录不可写时如果不提前发现，第一次下载会在 os.open 那一步抛
    FileNotFoundError，用户看到的是「进度条刚出来就失败」，还找不到原因。
    """
    try:
        os.makedirs(d, exist_ok=True)
        p = os.path.join(d, ".qk_write_test")
        with open(p, "wb") as f:
            f.write(b"1")
        os.remove(p)
        return True
    except Exception:
        return False


def load_cfg():
    """读配置：两处都读并合并，data_dir 里的覆盖用户目录里的。

    早先是「第一个能读的说了算」——用户目录里那份老的（没有 edge_mode）永远赢，
    于是每次启动都要先白 9 秒再降级，记住参数形同虚设。
    """
    out = {}
    for p in (CFG, os.path.join(data_dir(), "quark_accel.json")):
        try:
            # utf-8-sig：PowerShell / 记事本存过的 JSON 常带 BOM，用 utf-8 读会抛
            # 「Unexpected UTF-8 BOM」，而这里又吞了异常 —— 表现就是「我明明设过
            # 保存目录，重启又变回默认」。BOM 属于写入方式的差异，不是坏文件。
            with io.open(p, encoding="utf-8-sig") as f:
                d = json.load(f)
            if isinstance(d, dict):
                out.update(d)
        except Exception:
            continue
    return out


def save_cfg(d):
    """原子写：先写 .tmp 再 replace。多个 HTTP 线程会同时改配置，中途被杀留下
    半截 JSON 会让下次启动静默回落到默认档（用户的自定义连接数就丢了）。"""
    # 两处都写：load_cfg 是「后者覆盖前者」，只写一处会让另一处变成陈旧值反压回来
    # （表现：明明降级过一次也记住了，下次启动又先白 9 秒）
    for p in (CFG, os.path.join(data_dir(), "quark_accel.json")):
        try:
            os.makedirs(os.path.dirname(p), exist_ok=True)
            tmp = p + ".tmp"
            with io.open(tmp, "w", encoding="utf-8") as f:
                json.dump(d, f, ensure_ascii=False, indent=1)
            os.replace(tmp, p)
        except Exception:
            continue


def parse_cookie(raw):
    ck = {}
    if not raw:
        return ck
    for part in str(raw).replace("\n", ";").split(";"):
        part = part.strip()
        if part and "=" in part:
            k, v = part.split("=", 1)
            ck[k.strip()] = v.strip()
    return ck


def load_cookie_file():
    for p in (COOKIE_TXT,
              os.path.join(data_dir(), "cookie.txt"),
              os.path.join(ROOT, "quarkgui", "cookie.txt")):
        try:
            if os.path.exists(p):
                with io.open(p, encoding="utf-8") as f:
                    ck = parse_cookie(f.read())
                if ck:
                    return ck
        except Exception:
            pass
    return {}


def reap_orphans(profile_dir):
    """杀掉还占着本程序 profile 的遗留 Edge 进程（上次被强杀留下的孤儿）。

    不清理的话新进程会 hand-off 给旧实例并立刻以 rc=21 退出，界面永远不出现。
    走 procs.reap（ctypes 快照），不再冷启动 powershell —— 那要 1~3 秒还闪黑框。
    """
    return procs.reap(profile_dir, settle=0.6)


def find_edge():
    for p in EDGE_PATHS:
        if os.path.exists(p):
            return p
    for name in ("msedge.exe", "chrome.exe"):
        p = shutil.which(name)
        if p:
            return p
    return None


EDGE_COMMON = [
    "--no-first-run", "--no-default-browser-check", "--disable-extensions",
    "--disable-sync", "--disable-notifications",
    # LocalNetworkAccessChecks：不去掉的话登录页顶部会弹一条 Edge 自己的
    # 「pan.quark.cn 想要访问此设备上的其他应用和服务 / 阻止 允许」，正好压在
    # 二维码上方。--deny-permission-prompts 兜底把所有站点权限弹窗直接拒掉。
    "--disable-features=Translate,SyncPromo,msEdgeIdentitySyncPromo,"
    "msSyncPromo,EdgeSyncPromo,msEdgeWelcomePage,LocalNetworkAccessChecks",
    "--deny-permission-prompts",
    # 登录窗口每次点都复用同一个 profile，被 kill 过后 Edge 会浮一个
    # 「还原页面 / Microsoft Edge 意外关闭」的气泡挡在右上角。
    "--hide-crash-restore-bubble", "--disable-session-crashed-bubble",
]


def edge_cmd(edge, url, prof_dir, mode="standard", port=0, size="1440,940", app=True):
    """拼外壳命令行。app=True 走 --app（无地址栏），登录窗口用普通标签页。

    port=0 表示随机端口（Chromium 选空闲口后写进 <profile>/DevToolsActivePort），
    端口号不进命令行，本机别的网页没法直接连上来。自动化测试传固定端口。
    """
    cmd = [edge, ("--app=" + url) if app else url, "--user-data-dir=" + prof_dir]
    cmd += EDGE_COMMON + ["--window-size=" + size,
                          "--remote-debugging-port=%d" % int(port or 0)]
    if mode == "fallback":
        cmd += EDGE_FALLBACK
    return cmd


def spawn_edge(cmd, log=None):
    """起浏览器；windowed 父进程下不能让它弹控制台。"""
    kw = {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL,
          "stdin": subprocess.DEVNULL, "close_fds": True}
    try:
        kw["creationflags"] = CREATE_NO_WINDOW
        p = subprocess.Popen(cmd, **kw)
    except (TypeError, ValueError):
        kw.pop("creationflags", None)
        p = subprocess.Popen(cmd, **kw)
    if log:
        log("起浏览器 pid=%d: %s" % (p.pid, " ".join(cmd)))
    return p


def read_devtools_port(prof_dir, seconds=4.0):
    """等 Chromium 把随机调试端口写进 <profile>/DevToolsActivePort 并读出来。

    显式 --remote-debugging-port=<固定端口> 时它也照样写这个文件，所以统一走这里。
    """
    f = os.path.join(prof_dir, "DevToolsActivePort")
    t0 = time.time()
    while time.time() - t0 < seconds:
        try:
            with io.open(f, encoding="utf-8", errors="replace") as fh:
                first = (fh.readline() or "").strip()
            if first.isdigit():
                return int(first)
        except Exception:
            pass
        time.sleep(0.15)
    return 0


def _tabs(port):
    try:
        with urllib.request.urlopen("http://127.0.0.1:%d/json/list" % port, timeout=1.0) as r:
            return json.loads(r.read().decode())
    except Exception:
        return []


def wait_ready(port, seconds=8.0, want=None):
    """等浏览器把页面真正画出来（渲染进程活着），返回 (ok, tab)。

    只看 /json/list 不够：渲染进程起不来的白窗口同样会列出 page target，
    必须再跑一次 Runtime.evaluate —— 卡死的渲染进程连 1+1 都不回。
    """
    t0 = time.time()
    tab = None
    while time.time() - t0 < seconds:
        tabs = _tabs(port)
        cand = [t for t in tabs if t.get("type") == "page"
                and (not want or want in (t.get("url") or ""))
                and not (t.get("url") or "").startswith("about:blank")]
        if not cand:
            cand = [t for t in tabs if t.get("type") == "page"]
        if cand:
            tab = cand[0]
            try:
                from cdp import CDP
                c = CDP(tab["webSocketDebuggerUrl"], timeout=2.5)
                try:
                    r = c.call("Runtime.evaluate",
                               {"expression": "1+1", "returnByValue": True})
                    if (r or {}).get("result", {}).get("value") == 2:
                        return True, tab
                finally:
                    c.close()
            except Exception:
                pass
        time.sleep(0.25)
    return False, tab


def open_edge(url, prof_dir, port=0, size="1440,940", app=True, log=None,
              remember=None, read_mode=None, settle=12.0):
    """两段式起浏览器：先用记住的（或标准）参数；没画出界面就降级重来一次。

    返回 (proc, tab, port, err)。port 是真实调试端口（随机模式下从
    DevToolsActivePort 读出来），0 表示没起来。

    Windows 受限环境里 Chromium 的渲染进程会起不来，表现是纯白窗口、标题停在
    127.0.0.1。降级参数（--no-sandbox 等）能过，但会削弱沙箱，所以只在必要时用，
    且把结论写进配置 —— 下次直接一步到位，不用每次都先白一遍。
    """
    edge = find_edge()
    if not edge:
        return None, None, 0, "找不到 Edge"
    # 读用 edge_mode()（无参），写用 remember(mode)（带参）—— 两者不是同一个函数，
    # 早先误把 remember 当读取器调用会 TypeError: missing 1 required positional argument
    mode = (read_mode or edge_mode)()
    order = ["fallback", "standard"] if mode == "fallback" else ["standard", "fallback"]
    for m in order:
        try:
            os.remove(os.path.join(prof_dir, "DevToolsActivePort"))
        except OSError:
            pass
        cmd = edge_cmd(edge, url, prof_dir, m, port, size, app)
        p = spawn_edge(cmd, log)
        real = read_devtools_port(prof_dir, 4.0) or port
        ok, tab = wait_ready(real, settle, want="quark") if real else (False, None)
        if ok:
            if remember and m != mode:
                remember(m)
            if log:
                log("浏览器就绪（%s 参数）调试口 %d pid=%d" %
                    ("降级" if m == "fallback" else "标准", real, p.pid))
            return p, tab, real, ""
        if log:
            log("浏览器 %s 参数下没画出界面，换另一套参数" %
                ("降级" if m == "fallback" else "标准"))
        try:
            p.terminate()
            p.wait(timeout=4)
        except Exception:
            pass
        procs.reap(prof_dir, settle=0.5)
    return None, None, 0, "浏览器没能渲染出界面"


# ---------------------------------------------------------------- app
class App:
    def __init__(self):
        cfg = load_cfg()
        self.cfg = cfg
        self.tier = cfg.get("tier") or profiles.DEFAULT_KEY
        self.conns, self.part, self.tokens = profiles.resolve(
            self.tier, cfg.get("conns"), cfg.get("part"), cfg.get("tokens"))
        self.outdir = cfg.get("out") or os.path.join(os.path.expanduser("~"), "Downloads", "QuarkDL")
        # 企业策略 / 受限令牌 / 只读盘下 ~/Downloads 可能根本写不进去。探一次，
        # 不可写就退到数据目录下的 downloads，并留一句说明给界面显示。
        self.outdir_note = ""
        if not writable_dir(self.outdir):
            alt = os.path.join(data_dir(), "downloads")
            if writable_dir(alt):
                self.outdir_note = "默认下载目录不可写，已改用 %s" % alt
                self.outdir = alt
        self.cookies = load_cookie_file()
        self.cookie = "; ".join("%s=%s" % kv for kv in self.cookies.items())
        # 演示模式：不联网，用假数据看界面。QA_DEMO=1 或配置 demo=true 打开
        self.demo = bool(os.environ.get("QA_DEMO")) or bool(cfg.get("demo"))
        self.user = "演示账号" if self.demo else ""
        self.api_err = ""
        self.engine = None
        self.server = None
        self.edge = None
        self.beat = 0.0          # 页面最近一次心跳（/api/state）的时间戳
        self._lock = threading_lock()
        self._api_cache = {}
        self._spool = None       # 视频预览的并发预取池，第一次点预览才建
        self._msize = {}         # fid -> 文件大小，避免每个 Range 请求都打一次接口
        self._login_pid = 0      # 登录窗口主进程（复用/关闭用）
        self._login_port = LOGIN_PORT
        # 导入链接：转存 → 下载 → 删临时件，状态机在 importer.Importer 里
        self.importer = importer.Importer(self)

    # -- 持久化
    def save(self):
        self.cfg.update({"tier": self.tier, "conns": self.conns, "part": self.part,
                         "tokens": self.tokens, "out": self.outdir})
        save_cfg(self.cfg)

    def log(self, msg):
        line = "%s %s" % (time.strftime("%H:%M:%S"), msg)
        try:
            os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
            with io.open(LOG_FILE, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            pass
        try:
            print(line, flush=True)
        except Exception:
            pass

    # -- 引擎
    def make_engine(self):
        if self.engine is None:
            # 下载记录放在 data_dir 里的 downloads.json：重启程序以后「下载记录」还在，
            # 不然用户下次打开就只剩一个空列表，看不出来之前下过什么。
            hist = os.path.join(data_dir(), "downloads.json")
            try:
                os.makedirs(self.outdir, exist_ok=True)
            except OSError:
                pass
            self.engine = Engine(self.cookie, self.outdir, self.tier,
                                 self.conns, self.part, self.tokens,
                                 on_push=None, on_log=self.log, demo=self.demo,
                                 hist_path=hist)
        return self.engine

    def api(self):
        if not self.cookie:
            return None
        ck = self._api_cache.get("api")
        if ck is None or self._api_cache.get("cookie") != self.cookie:
            self._api_cache = {"cookie": self.cookie, "api": Api(self.cookie)}
        return self._api_cache["api"]

    # -- 登录态
    def refresh_user(self):
        if self.demo:
            # 演示模式下没有真账号，别去撞接口把「演示账号」刷成空
            self.user, self.api_err = "演示账号", ""
            return
        api = self.api()
        if not api:
            self.user, self.api_err = "", "还没有 Cookie"
            return
        try:
            info = api.whoami() or {}
            # member_type 是 NORMAL / VIP / SVIP 这类枚举码，直接显示在胶囊里很难看
            mt = (info.get("member_type") or "").upper()
            label = {"NORMAL": "普通用户", "VIP": "会员", "SVIP": "超级会员",
                     "EXP_SVIP": "体验会员", "EXP_VIP": "体验会员"}.get(mt, "")
            nick = (info.get("nickname") or "").strip()
            self.user = nick or label or mt or "已登录"
            self.api_err = ""
            self.cfg["cap_total"] = info.get("total") or 0
            self.cfg["cap_used"] = info.get("used") or 0
        except Exception as e:
            msg = str(e)
            if "31004" in msg or "token" in msg.lower() and "invalid" in msg.lower():
                msg = "登录状态已过期"
            elif "31001" in msg:
                msg = "Cookie 已失效，需要重新登录"
            self.user, self.api_err = "", msg[:120]

    # ---------------- 视频预览（边下边播）
    def _pool(self):
        if getattr(self, "_spool", None) is None:
            # 预览不抢下载的带宽：默认 64 连接 / 1 MB 片，实测够 1080p 边下边播
            # 256 KB 小片是刻意的：片是「就绪」的最小单位，1 MB 片在单连接被整形到
            # 200 KB/s 时要 5 秒才凑齐一片，播放器开头就会卡；小片 1.3 秒就能出第一帧。
            # api 传可调用对象：登录/换 Cookie 后要重新取链，缓存住旧 Api 会让
            # 预览一片 412（或 api 为 None 时 AttributeError）
            self._spool = stream.Pool(self.api, os.path.join(data_dir(), "cache"),
                                      conns=64, part_mb=1, tokens=16, max_items=2,
                                      part_bytes=256 << 10)
        return self._spool

    def media_open(self, fid):
        """开一个预览流：返回文件大小，后台开始并发预取。"""
        if not fid:
            return {"ok": False, "err": "缺少 fid"}
        if self.demo:
            # 演示模式用打包进来的 demo.mp4 走完全一样的读取链路（本地取片代替 HTTP 取片）
            if fid != "demo12":
                return {"ok": False, "err": "演示模式只有 demo-20s.mp4 有内容可播"}
            p = os.path.join(HERE, "demo.mp4")
            if not os.path.exists(p):
                return {"ok": False, "err": "演示视频缺失"}
            if fid not in self._msize:
                self._msize[fid] = os.path.getsize(p)
            st = self._pool().get(fid, self._msize[fid], reader=stream.LocalReader(p))
            self._demo_st = st
            return {"ok": True, "size": st.size, "stat": st.stat()}
        api = self.api()
        if not api:
            return {"ok": False, "err": "还没有 Cookie，请先登录"}
        try:
            d = api.call("/file/download", method="POST", body={"fids": [fid]})
            if not (isinstance(d, list) and d and d[0].get("size")):
                return {"ok": False, "err": "取不到文件信息"}
            size = int(d[0]["size"])
            self._msize[fid] = size
            st = self._pool().get(fid, size)
            return {"ok": True, "size": st.size, "stat": st.stat()}
        except Exception as e:
            return {"ok": False, "err": "%s: %s" % (type(e).__name__, str(e)[:120])}

    def media_known(self, fid):
        """这个 fid 是否开过预览流（用来挡掉乱猜的请求）。"""
        if not fid:
            return False
        p = getattr(self, "_spool", None)
        return fid in self._msize or bool(p and fid in p.items)

    def media_size(self, fid):
        """已知道的文件大小（缓存），不知道就现取一次；取不到返回 0。"""
        if not fid:
            return 0
        if fid in self._msize:
            return self._msize[fid]
        return int((self.media_open(fid) or {}).get("size") or 0)

    def media_read(self, fid, a, b, size):
        """阻塞到 [a,b] 可从本地缓存读出；失败返回 None。

        只查不建：关闭预览后播放器还会甩出一两个尾随 Range，用 get() 会重建流并
        触发一次 64 线程的全量预取（每关一次预览就白下一整个视频）。
        """
        p = getattr(self, "_spool", None)
        st = p.peek(fid) if p else None
        if st is None:
            return None
        try:
            return st.read(a, b)
        except Exception as e:
            self.log("预览取数 %s: %s" % (fid[:8], str(e)[:100]))
            return None

    def media_stat(self, fid):
        p = getattr(self, "_spool", None)
        return {"ok": True, "stat": p.stat(fid) if (p and fid) else None}

    def media_clear(self):
        p = getattr(self, "_spool", None)
        if p:
            p.clear_cache()
        return {"ok": True}

    def media_stop(self, fid):
        p = getattr(self, "_spool", None)
        if p and fid:
            p.stop_one(fid)
        return {"ok": True}

    def media_stop_all(self):
        p = getattr(self, "_spool", None)
        if p:
            p.stop_all()

    def close_preview(self):
        self.media_stop_all()
        self._spool = None

    # ---------------- /api/* 实现
    def api_list(self, fid="0", nav="all"):
        if self.demo:
            # 找不到的目录返回空列表，绝不回落到根目录：否则点进子目录会看到
            # 「面包屑变了、列表还是原来那批」的假象
            return {"ok": True, "entries": DEMO_ENTRIES.get(fid, [])}
        api = self.api()
        if not api:
            return {"ok": False, "err": "还没有 Cookie，请先登录"}
        try:
            if nav == "recent":
                return {"ok": True, "entries": api.list_recent()}
            if nav == "trash":
                return {"ok": True, "entries": api.list_recycle()}
            return {"ok": True, "entries": api.list_raw(fid)}
        except Exception as e:
            return {"ok": False, "err": str(e)[:160], "entries": []}

    def api_state(self):
        self.beat = time.time()      # 页面在拉状态 = 界面真的渲染出来了
        # 引擎必须在这里建：下载记录存在 downloads.json 里，只有 Engine 构造时才
        # 读进来。以前写成 self.engine 为空就返回 None，结果重启程序后「下载记录」
        # 一直是空的，要先随便下个东西才看得到历史 —— 用户以为记录丢了。
        snap = self.make_engine().snapshot()
        total = int(self.cfg.get("cap_total") or 0)
        used = int(self.cfg.get("cap_used") or 0)
        if self.demo and not total:
            # 演示数据也要有存储条，否则左下角永远是「--」，看起来像没加载出来
            total, used = 10 * 1024 ** 3, 6 * 1024 ** 3 + 430 * 1024 ** 2
        return {
            "ok": True,
            "tier": self.tier, "tiers": profiles.TIERS,
            "conns": self.conns, "part_mb": self.part, "tokens": self.tokens,
            "user": self.user, "logged": bool(self.user),
            "banner": "" if self.demo else
                      ("" if self.user else
                       "还没有登录，点右上角「登录」用浏览器扫码" if not self.cookie
                       else ("Cookie 无效或账号被封禁：%s" % self.api_err) if self.api_err
                       else "还没有登录，点右上角「登录」用浏览器扫码"),
            "outdir": self.outdir,
            # 目录被降级过要说出来，否则用户以为文件下进了自己设的目录，去找却找不到
            "outdir_note": self.outdir_note,
            "cap_total": _human(total) if total else "",
            "cap_used": _human(used) if used else "",
            "cap_pct": round(used * 100.0 / total, 1) if total else 0,
            "demo": self.demo,
            "snap": snap,
        }

    def api_profile(self, tier=None, conns=None, part=None, tokens=None):
        self.tier = tier or profiles.DEFAULT_KEY
        self.conns, self.part, self.tokens = profiles.resolve(self.tier, conns, part, tokens)
        self.save()
        if self.engine:
            self.engine.apply_profile(self.tier, conns, part, tokens)
        return {"ok": True, "tier": self.tier, "tiers": profiles.TIERS,
                "conns": self.conns, "part_mb": self.part, "tokens": self.tokens}

    def api_add(self, items, fid_dir=None):
        if not items:
            return {"ok": False, "err": "没有选中文件"}
        eng = self.make_engine()
        try:
            flat = self._flatten(items)
            if not flat:
                return {"ok": False, "err": "选中的目录里没有文件"}
            added = eng.add(flat, self.outdir)
            return {"ok": True, "added": len(added)}
        except Exception as e:
            return {"ok": False, "err": "%s: %s" % (type(e).__name__, str(e)[:120])}

    def _flatten(self, items):
        """勾选的目录递归展开成文件（rel 带子路径），文件原样保留。"""
        out = []
        api = None
        for it in items:
            fid = str(it.get("fid") or "").strip()
            if not fid:
                continue
            name = str(it.get("name") or fid)
            if not it.get("dir") and not it.get("is_dir"):
                out.append({"fid": fid, "name": name, "rel": name, "size": it.get("size") or 0})
                continue
            if self.demo:
                continue
            if api is None:
                api = self.api()
            for en in api.walk(fid, prefix=name):
                out.append({"fid": en.fid, "name": en.name,
                            "rel": en.rel, "size": en.size})
        return out

    # -- 导入分享链接
    def api_import_preview(self, link=""):
        if self.demo:
            # 演示数据按真实分堆逻辑造：小文件走免转存直下，大文件要求登录后转存
            return {"ok": True, "files": 6, "bytes": 4294967296, "cap_total": 10 * 1024 ** 3,
                    "cap_free": 3 * 1024 ** 3, "batches": 2, "batch_sizes": [2 << 30, 2 << 30],
                    "too_big": [], "passcode": True, "pwd_id": "demo", "logged": False,
                    "direct_files": 3, "direct_bytes": 3 * (5 << 20),
                    "save_files": 3, "save_bytes": 4294967296 - 3 * (5 << 20),
                    "listed": 6, "truncated": False,
                    "list": [{"fid": "dm1", "name": "第 01 讲 · 资料分析入门.mp4", "size": 402653184, "direct": False},
                             {"fid": "dm2", "name": "第 02 讲 · 增长率速算.mp4", "size": 318767104, "direct": False},
                             {"fid": "dm3", "name": "讲义合集.pdf", "size": 8642560, "direct": True},
                             {"fid": "dm4", "name": "笔记 · 速算表.pdf", "size": 3874816, "direct": True},
                             {"fid": "dm5", "name": "题型清单.png", "size": 648192, "direct": True},
                             {"fid": "dm6", "name": "讲义附录.zip", "size": 3187671040, "direct": False}]}
        return self.importer.preview(link)

    def api_import_start(self, link="", delete_after=True, pick=None):
        if self.demo:
            return {"ok": False, "err": "演示模式不联网，导入链接需要真实登录"}
        return self.importer.start(link, delete_after, pick)

    def api_import_state(self):
        return {"ok": True, **self.importer.snap()}

    def api_import_cancel(self):
        return self.importer.cancel()

    def api_cancel(self, jid):
        return {"ok": bool(self.engine and self.engine.cancel(str(jid or "")))}

    def api_retry(self, jid):
        return {"ok": bool(self.engine and self.engine.retry(str(jid or "")))}

    def api_pause(self, jid=None):
        """带 id 只暂停那一个任务；不带就整体暂停。"""
        if not self.engine:
            return {"ok": True}
        jid = str(jid or "").strip()
        return {"ok": bool(self.engine.pause(jid or None))}

    def api_resume(self, jid=None):
        if not self.engine:
            return {"ok": True}
        jid = str(jid or "").strip()
        return {"ok": bool(self.engine.resume(jid or None))}

    def api_remove(self, jid):
        if not self.engine:
            return {"ok": False}
        return {"ok": bool(self.engine.remove(str(jid or "")))}

    def api_clear(self):
        if self.engine:
            self.engine.clear_done()
        return {"ok": True}

    def api_hist(self):
        snap = self.make_engine().snapshot()
        return {"ok": True, "hist": snap.get("hist") or []}

    def api_dir_list(self, limit=60):
        """列下载目录里的文件。

        给「打开下载目录」兜底：有些环境下 ShellExecute 被安全软件拦死，
        桌面上一辈子不会出现资源管理器窗口。这时至少让用户在程序里看到
        目录里到底有什么，能把路径复制走。
        """
        d = self.outdir or ""
        out = []
        try:
            names = os.listdir(d)
        except OSError as e:
            return {"ok": False, "outdir": d, "err": "读不了目录：%s" % str(e)[:80]}
        for n in sorted(names):
            p = os.path.join(d, n)
            part = n.endswith(".qstate") or n.endswith(".qbad")
            try:
                sz = os.path.getsize(p) if os.path.isfile(p) else 0
            except OSError:
                sz = 0
            out.append({"name": n, "size": sz, "dir": os.path.isdir(p), "part": part})
        return {"ok": True, "outdir": d, "total": len(out),
                "entries": [x for x in out if not x["part"]][:int(limit or 60)]}

    def api_delete(self, jid=None):
        """删掉已下载到本地的文件（连同任务条目与下载记录）。"""
        if not self.engine:
            return {"ok": False, "err": "还没有下载记录"}
        return self.engine.delete_files(str(jid or ""))

    def api_clear_hist(self):
        if self.engine:
            self.engine.clear_hist()
        return {"ok": True}

    def api_open_dir(self):
        """打开下载目录。

        早先直接 os.startfile(self.outdir)：目录还没建过（一次都没下载完）时
        startfile 会抛 WinError 2，前端又不看返回值，表现就是「按钮没反应」。
        现在先建目录，失败再用资源管理器定位到第一个已完成文件。
        """
        d = self.outdir or os.path.join(os.path.expanduser("~"), "Downloads", "QuarkDL")
        try:
            os.makedirs(d, exist_ok=True)
        except OSError:
            # 目录建不出来（只读盘 / 权限策略）时不要把错误甩给用户，
            # 退到一个肯定能写的位置，界面上也换过去，之后下载落这儿
            alt = os.path.join(data_dir(), "downloads")
            try:
                os.makedirs(alt, exist_ok=True)
            except OSError as e2:
                return {"ok": False, "outdir": d, "err": "建目录失败：%s" % str(e2)[:80]}
            d = alt
            self.outdir = alt
            if self.engine:
                self.engine.set_outdir(alt)
            self.save()
        # 打开失败是环境问题（受限令牌 / 安全软件拦 ShellExecute），这次会话里
        # 再点也不会变好：记住结论，后面几次直接返回，别让用户每次都等十几秒。
        cached = getattr(self, "_open_bad", "")
        if cached:
            return {"ok": False, "outdir": d, "openable": False,
                    "err": "打不开文件管理器（%s）。目录是：%s" % (cached, d)}
        ok, why = shell_open(d)
        if ok:
            self._open_bad = ""
            return {"ok": True, "outdir": d}
        self._open_bad = str(why)[:60]
        last = why
        # 打不开是环境限制（受限令牌 / 安全软件拦 ShellExecute），不是路径错。
        # 记一行日志，下次用户报「没反应」时能直接看出是哪一级被拦的。
        self.log("打开下载目录失败：%s → %s" % (str(why)[:80], d))
        # 连资源管理器都起不来：退到最近一层真实存在的父目录，至少给个反馈
        p = os.path.dirname(os.path.normpath(d))
        while p and not os.path.isdir(p):
            nxt = os.path.dirname(p)
            if nxt == p:
                break
            p = nxt
        if p and os.path.isdir(p) and shell_open(p)[0]:
            return {"ok": True, "outdir": p,
                    "note": "下载目录还没建好，先打开了上一层"}
        # 全部封面都打不开时把路径原样返回：界面上至少能复制出来自己开
        return {"ok": False, "outdir": d, "openable": False,
                "err": "打不开文件管理器（%s）。目录是：%s" % (str(last)[:60], d)}

    def api_reveal(self, jid=None, path=None):
        """在资源管理器里选中某个已下载的文件。"""
        p = str(path or "").strip()
        if not p and jid and self.engine:
            with self.engine.lock:
                j = self.engine.jobs.get(str(jid))
                p = j.out if j else ""
        if not p:
            return {"ok": False, "err": "没有这个文件"}
        try:
            if os.path.exists(p):
                ok, why = shell_open(p, select=True)
                if ok:
                    return {"ok": True, "path": p}
                return {"ok": False, "err": "打不开（%s）。文件在：%s" % (why, p)}
            return self.api_open_dir()
        except Exception as e:
            return {"ok": False, "err": str(e)[:100]}

    def api_pick_dir(self):
        """选目录：子进程跑 PowerShell 的现代文件对话框（不占用 HTTP 线程、不依赖 tkinter）。"""
        try:
            d = pick_folder(self.outdir)
        except Exception as e:
            return {"ok": False, "err": "%s: %s" % (type(e).__name__, str(e)[:90])}
        if not d:
            return {"ok": False, "err": "已取消"}
        self.outdir = d
        if self.engine:
            self.engine.set_outdir(d)
        self.save()
        return {"ok": True, "outdir": d}

    def api_cookie(self, raw):
        ck = parse_cookie(raw)
        if not ck:
            return {"ok": False, "err": "没解析出 cookie"}
        self.cookies = ck
        self.cookie = "; ".join("%s=%s" % kv for kv in ck.items())
        if self.engine:
            self.engine.cookie = self.cookie
            self.engine._api = None
        # 预览池里缓存的是旧身份的直链（签在旧 Cookie 上），换号后一律 412 —— 直接弃掉
        try:
            self.close_preview()
            self._msize = {}
        except Exception:
            pass
        self.refresh_user()
        if not self.api_err:
            for p in (COOKIE_TXT, os.path.join(data_dir(), "cookie.txt")):
                try:
                    with io.open(p, "w", encoding="utf-8") as f:
                        f.write(self.cookie)
                except Exception:
                    pass
        return {"ok": not self.api_err, "user": self.user, "err": self.api_err}

    # ---------------- 登录（独立窗口，异步起 + 后台自愈 + 已开就复用）
    def login_prof(self):
        return os.path.join(data_dir(), "login_profile")

    def _login_tab(self):
        """登录窗口里那个标签。优先找已经落到夸克的，其次任意普通标签页。

        只认 URL 含 quark 的话，页面还在 about:blank / 加载中时会返回 None，
        调用方就会再 Target.createTarget 一个，窗口里冒出两个标签页。
        """
        port = self._login_port or read_devtools_port(self.login_prof(), 0.1)
        pages = [t for t in _tabs(port) if t.get("type") == "page"]
        for t in pages:
            if "quark" in (t.get("url") or ""):
                return t
        return pages[0] if pages else None

    def login_alive(self):
        """登录窗口还活着吗（有进程 + 调试口答话）。"""
        if not procs.find_by_profile(self.login_prof()):
            return False
        port = read_devtools_port(self.login_prof(), 0.1)
        if port:
            self._login_port = port
        return bool(_tabs(self._login_port))

    def api_login(self):
        """拉起官方登录页。已经开着就直接把页面导回登录页，不再新起进程。"""
        if not find_edge():
            return {"ok": False, "err": "找不到 Edge"}
        prof = self.login_prof()
        os.makedirs(prof, exist_ok=True)
        # 复用：窗口还在，后台把它导回登录页并提到前台，接口立刻返回
        if self.login_alive():
            port = self._login_port
            threading.Thread(target=self._login_refocus, args=(port,), daemon=True).start()
            return {"ok": True, "port": port, "reused": True,
                    "hint": "登录窗口已切回夸克，登录后点右边按钮"}
        # 全新起一个：先清遗留进程（ctypes，毫秒级、无黑框）
        procs.reap(prof, settle=0.4)
        try:
            os.remove(os.path.join(prof, "DevToolsActivePort"))
        except OSError:
            pass
        # 登录窗口的调试口必须随机（--remote-debugging-port=0）：主窗口已经占着 DEV_PORT，
        # 两个 Edge 抢同一个端口时第二个绑不上，登录窗口就白屏、读 cookie 还会读到主窗口
        # 那个 ui_profile（里面没有 quark 的键），表现为「登录窗口打开了但永远读不到 cookie」
        cmd = edge_cmd(find_edge(), "https://pan.quark.cn/", prof,
                       edge_mode(), 0, size="1100,760", app=False)
        spawn_edge(cmd, self.log)
        self._login_port = 0
        # 后台盯着：白屏就换另一套参数重启（受限环境下渲染进程起不来是常态）
        threading.Thread(target=self._login_heal, args=(prof,), daemon=True).start()
        return {"ok": True, "hint": "登录窗口已在打开，扫码登录后点右边按钮",
                "port": self._login_port, "profile": prof}

    def _login_refocus(self, port):
        """把已经开着的登录窗口导回夸克首页并提到前台（后台线程，不阻塞接口）。"""
        try:
            from cdp import CDP
            tab = self._login_tab()
            if tab:
                c = CDP(tab["webSocketDebuggerUrl"], timeout=4)
                try:
                    c.call("Page.navigate", {"url": "https://pan.quark.cn/"})
                    c.call("Page.bringToFront", {})
                finally:
                    c.close()
                return
            ver = json.loads(urllib.request.urlopen(
                "http://127.0.0.1:%d/json/version" % port, timeout=2).read().decode())
            c = CDP(ver["webSocketDebuggerUrl"], timeout=4)
            try:
                c.call("Target.createTarget", {"url": "https://pan.quark.cn/"})
            finally:
                c.close()
        except Exception as e:
            self.log("切回登录页失败: %s" % str(e)[:80])

    def _login_heal(self, prof):
        """等页面真画出来；白屏就换参数重启。随机端口模式下靠 DevToolsActivePort 找口。"""
        port = read_devtools_port(prof, 4.0)
        self._login_port = port
        ok = bool(port) and wait_ready(port, 5.0, want="quark")[0]
        if ok:
            self.log("登录窗口就绪 调试口 %d" % port)
            return
        self.log("登录窗口白屏，换另一套参数重启")
        try:
            procs.reap(prof, settle=0.5)
            try:
                os.remove(os.path.join(prof, "DevToolsActivePort"))
            except OSError:
                pass
            other = "standard" if edge_mode() == "fallback" else "fallback"
            cmd = edge_cmd(find_edge(), "https://pan.quark.cn/", prof,
                           other, 0, size="1100,760", app=False)
            spawn_edge(cmd, self.log)
            port2 = read_devtools_port(prof, 4.0)
            self._login_port = port2 or 0
            ok2 = bool(port2) and wait_ready(port2, 7.0, want="quark")[0]
            if ok2:
                set_edge_mode(other)
                self.log("登录窗口降级启动成功（%s）调试口 %d" % (other, port2))
            else:
                self.log("登录窗口仍然没画出界面")
        except Exception as e:
            self.log("登录窗口自愈失败: %s" % str(e)[:100])

    def api_login_done(self, port=0):
        """从登录窗口的浏览器里捞 cookie（浏览器自己解密，绕开 v20 ABE）。"""
        try:
            tab = self._login_tab()
            if not tab and port:
                for t in _tabs(int(port)):
                    if t.get("type") == "page" and "quark" in (t.get("url") or ""):
                        tab = t
                        break
            if not tab:
                return {"ok": False, "err": "登录窗口还没打开夸克页面"}
            got = cdp_get_cookies(tab["webSocketDebuggerUrl"])
            keep = {k: v for k, v in got.items()
                    if k in COOKIE_KEYS or k.startswith("__")}
            if not keep:
                return {"ok": False, "err": "还没读到登录 cookie，请确认已登录"}
            res = self.api_cookie("; ".join("%s=%s" % kv for kv in keep.items()))
            if res.get("ok"):
                self.close_login_window()   # 登录窗口用完就关，主窗口不动
            return res
        except Exception as e:
            return {"ok": False, "err": "%s: %s" % (type(e).__name__, str(e)[:100])}

    def close_login_window(self):
        killed = procs.reap(self.login_prof(), settle=0.4)
        if killed:
            self.log("关闭登录窗口 %d 个进程" % len(killed))


def _e(fid, name, size, is_dir=False, ts=1730000000):
    return {"fid": fid, "name": name, "size": size, "is_dir": is_dir, "updated": ts}


# 演示数据按目录分桶。每个目录都要有自己的一份，否则点进子目录会「面包屑变了、
# 列表没变」——早期版本用 .get(fid) or .get("0") 兜底，就是这个坑。
DEMO_ENTRIES = {
    "0": [
        _e("demo1", "先保存，后下载【tpsy11.12 】课程", 0, True, 1731600000),
        _e("demo2", "我的备份", 0, True, 1731500000),
        _e("demo3", "来自：分享", 0, True, 1731400000),
        _e("demo4", "夸克精选壁纸", 0, True, 1731300000),
        _e("demo5", "tpsy11.12 .7z.001", 1048576000, False, 1731200000),
        _e("demo6", "扫描王全能宝_6_8_57.apk", 271167079, False, 1731100000),
        _e("demo7", "CamScanner_v7.19.0.apk", 231780060, False, 1731000000),
        _e("demo8", "probe-quark-video.mp4", 13534752, False, 1730900000),
        _e("demo9", "05 行测极致真题（国考卷） 解析.pdf", 20488723, False, 1730800000),
        _e("demo10", "Everything-The Black Skirts#1ONmv.mp3", 11724927, False, 1730700000),
        _e("demo11", "t1.mp4", 160730, False, 1730600000),
        _e("demo12", "demo-20s.mp4", 1054924, False, 1730500000),
    ],
    "demo1": [
        _e("demo1a", "第 01 讲 · 资料分析入门.mp4", 402653184, False, 1731630000),
        _e("demo1b", "第 02 讲 · 增长率速算.mp4", 318767104, False, 1731626000),
        _e("demo1c", "第 01 讲 讲义.pdf", 8642560, False, 1731622000),
        _e("demo1d", "补充资料", 0, True, 1731618000),
    ],
    "demo1d": [
        _e("demo1d1", "真题汇编 2015-2024.pdf", 34603008, False, 1731614000),
        _e("demo1d2", "答案与解析.pdf", 12582912, False, 1731610000),
    ],
    "demo2": [
        _e("demo2a", "手机相册备份", 0, True, 1731520000),
        _e("demo2b", "微信文件备份.zip", 1866465280, False, 1731510000),
        _e("demo2c", "简历-2025.docx", 132096, False, 1731500000),
    ],
    "demo2a": [
        _e("demo2a1", "IMG_20241105_141233.jpg", 3211264, False, 1731526000),
        _e("demo2a2", "IMG_20241106_090812.jpg", 2883584, False, 1731522000),
        _e("demo2a3", "VID_20241107_183000.mp4", 75497472, False, 1731518000),
    ],
    "demo3": [
        _e("demo3a", "别人分享给我的资料.7z", 536870912, False, 1731410000),
        _e("demo3b", "课程录像合集", 0, True, 1731405000),
    ],
    "demo3b": [
        _e("demo3b1", "录屏 2024-11-01.mp4", 136314880, False, 1731408000),
        _e("demo3b2", "录屏 2024-11-02.mp4", 121634816, False, 1731404000),
    ],
    "demo4": [
        _e("demo4a", "wallpaper-01.jpg", 4521984, False, 1731320000),
        _e("demo4b", "wallpaper-02.jpg", 3985408, False, 1731316000),
        _e("demo4c", "wallpaper-03.png", 6291456, False, 1731312000),
    ],
}


PS_PICK = r"""
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$OutputEncoding = [System.Text.Encoding]::UTF8
Add-Type -AssemblyName System.Windows.Forms | Out-Null
$d = New-Object System.Windows.Forms.OpenFileDialog
$d.ValidateNames = $false
$d.CheckFileExists = $false
$d.CheckPathExists = $true
$d.FileName = "选择此文件夹"
$d.InitialDirectory = $init
$d.Title = "选择下载保存目录"
if ($d.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) {
  $p = [System.IO.Path]::GetDirectoryName($d.FileName)
  if ($p) { [Console]::Out.Write($p) }
}
"""


def pick_folder(initial=""):
    """返回用户选中的目录；取消返回 ""。"""
    import tempfile
    init = (initial or "").replace("'", "''")
    script = PS_PICK.replace("$init", "'" + init + "'")
    fd, path = tempfile.mkstemp(suffix=".ps1")
    try:
        with os.fdopen(fd, "w", encoding="utf-8-sig") as f:
            f.write(script)
        # CREATE_NO_WINDOW：windowed 程序起 powershell 会闪一个控制台黑框，
        # 选目录时用户会看到「弹窗」，看起来像被杀软弹了警告。
        p = subprocess.run(["powershell", "-NoProfile", "-STA",
                            "-ExecutionPolicy", "Bypass", "-File", path],
                           capture_output=True, timeout=300,
                           creationflags=CREATE_NO_WINDOW)
        # 脚本里已经把 OutputEncoding 钉成 UTF-8；这里严格解，中文目录名不会被吃掉。
        # 解不出来（被安全软件插了提示输出之类）就退回 ignore，再不行当作取消。
        raw = p.stdout or b""
        try:
            out = raw.decode("utf-8").strip()
        except UnicodeDecodeError:
            out = raw.decode("utf-8", "ignore").strip()
        # 只认真正存在的目录，避免把提示文本当路径写进配置
        return out if out and os.path.isdir(out) else ""
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


def threading_lock():
    import threading
    return threading.Lock()


def _human(n):
    if not n:
        return "0 B"
    u = ["B", "KB", "MB", "GB", "TB"]
    i = 0
    v = float(n)
    while v >= 1024 and i < 4:
        v /= 1024.0
        i += 1
    return ("%d %s" % (v, u[i])) if i == 0 else ("%.1f %s" % (v, u[i]))


def cdp_get_cookies(ws_url):
    from cdp import CDP
    c = CDP(ws_url)
    out = {}
    try:
        c.call("Network.enable")
        r = c.call("Storage.getCookies", {})
        for x in r.get("cookies", []):
            if str(x.get("domain", "")).endswith("quark.cn"):
                out[x["name"]] = x["value"]
        if not out:
            r = c.call("Network.getCookies", {"urls": ["https://pan.quark.cn/"]})
            for x in r.get("cookies", []):
                out[x["name"]] = x["value"]
    finally:
        c.close()
    return out


# ---------------------------------------------------------------- main
def main():
    import threading

    _boot("9 main enter")
    app = App()
    _boot("10 App ok")
    srv = Server(app)
    srv.start()
    app.server = srv
    url = "http://127.0.0.1:%d/" % srv.port
    _boot("11 server on %d" % srv.port)
    app.log("启动 · 服务 %s · %d 连接 / %dMB 切片 / %d token · cookie %s" %
            (url, app.conns, app.part, app.tokens,
             "有 %d 键" % len(app.cookies) if app.cookies else "无"))
    threading.Thread(target=app.refresh_user, daemon=True).start()

    edge = find_edge()
    if not edge:
        app.log("找不到 Edge，用默认浏览器打开 " + url)
        webbrowser.open(url)
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass
        return 0

    prof_dir = os.path.join(data_dir(), "ui_profile")
    os.makedirs(prof_dir, exist_ok=True)
    # 清理上次被强杀留下的孤儿（ctypes，毫秒级）：不清理的话新进程会 hand-off
    # 给旧实例并立刻 rc=21 退出，界面永远不出现。
    killed = procs.reap(prof_dir, settle=0.6)
    if killed:
        app.log("清理遗留外壳进程 %d 个: %s" % (
            len(killed), " ".join(str(x) for x in killed)))
    _d1 = os.path.join(data_dir(), "quark_accel.json")
    app.log("浏览器参数模式 = %s（记住的）· data_dir=%s · 本目录配置=%s · 用户配置=%s" % (
        edge_mode(), data_dir(),
        "有" if os.path.exists(_d1) else "无",
        "有" if os.path.exists(CFG) else "无"))

    proc, tab, real_port, err = open_edge(url, prof_dir, DEV_PORT, size="1440,940",
                                          app=True, log=app.log,
                                          remember=set_edge_mode, settle=6.5)
    if not proc:
        app.log("外壳启动失败: " + (err or "未知"))
        srv.stop()
        return 1
    app.edge = proc
    app.tab = tab or {}
    app.port = real_port

    try:
        rc = proc.wait()
    except KeyboardInterrupt:
        rc = -1
    app.log("外壳已退出 rc=%s" % rc)
    srv.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
