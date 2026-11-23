"""用浏览器登录取 Cookie：起一个独立 Edge/Chrome 实例，开 CDP，登录后直接读 cookie。

思路：不碰用户日常浏览器的 Cookie 库（新版 Chromium 的 v20 加密读不出来），
而是自己拉一个带 --user-data-dir 的干净实例，登录态由浏览器自己保管，
再通过 DevTools Protocol 的 Storage.getCookies 取出来 —— 浏览器解密的，天然可用。

WebSocket 客户端是手写的最小实现（只走明文 ws://127.0.0.1），不引入额外依赖。
"""
from __future__ import annotations

import base64
import json
import os
import shutil
import socket
import struct
import subprocess
import tempfile
import time
import urllib.request

CANDIDATES = [
    os.path.expandvars(r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe"),
    os.path.expandvars(r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe"),
    os.path.expandvars(r"%ProgramFiles%\Google\Chrome\Application\chrome.exe"),
    os.path.expandvars(r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"),
    os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
]

LOGIN_URL = "https://pan.quark.cn/"


def find_browser():
    for p in CANDIDATES:
        if os.path.isfile(p):
            return p
    return None


# ------------------------------------------------------------ 最小 WebSocket
class WS:
    def __init__(self, url, timeout=10):
        assert url.startswith("ws://"), "只支持明文 ws（本地回环）"
        rest = url[5:]
        hostport, _, path = rest.partition("/")
        host, _, port = hostport.partition(":")
        self.sock = socket.create_connection((host, int(port or 80)), timeout=timeout)
        self.sock.settimeout(timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        req = ("GET /%s HTTP/1.1\r\nHost: %s\r\nUpgrade: websocket\r\n"
               "Connection: Upgrade\r\nSec-WebSocket-Key: %s\r\n"
               "Sec-WebSocket-Version: 13\r\n\r\n" % (path, hostport, key))
        self.sock.sendall(req.encode())
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise ConnectionError("WS 握手失败")
            buf += chunk
        head, _, self.rest = buf.partition(b"\r\n\r\n")
        if b"101" not in head.split(b"\r\n")[0]:
            raise ConnectionError("WS 握手被拒: " + head.decode("latin1")[:120])

    def send(self, text):
        data = text.encode()
        mask = os.urandom(4)
        n = len(data)
        hdr = bytearray([0x81])
        if n < 126:
            hdr.append(0x80 | n)
        elif n < 65536:
            hdr.append(0x80 | 126)
            hdr += struct.pack(">H", n)
        else:
            hdr.append(0x80 | 127)
            hdr += struct.pack(">Q", n)
        hdr += mask
        self.sock.sendall(bytes(hdr) + bytes(b ^ mask[i % 4] for i, b in enumerate(data)))

    def _read_exact(self, n):
        while len(self.rest) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise ConnectionError("WS 连接关闭")
            self.rest += chunk
        out, self.rest = self.rest[:n], self.rest[n:]
        return out

    def recv(self):
        b1, b2 = self._read_exact(2)
        opcode = b1 & 0x0F
        masked = b2 & 0x80
        n = b2 & 0x7F
        if n == 126:
            n = struct.unpack(">H", self._read_exact(2))[0]
        elif n == 127:
            n = struct.unpack(">Q", self._read_exact(8))[0]
        mask = self._read_exact(4) if masked else None
        data = self._read_exact(n)
        if mask:
            data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
        if opcode == 0x8:
            raise ConnectionError("WS 被对端关闭")
        if opcode == 0x9:
            return self.recv()
        return data.decode("utf-8", "replace")

    def call(self, method, params=None, _id=[0]):
        _id[0] += 1
        mid = _id[0]
        self.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
        deadline = time.time() + 15
        while time.time() < deadline:
            msg = json.loads(self.recv())
            if msg.get("id") == mid:
                if "error" in msg:
                    raise RuntimeError("CDP %s: %s" % (method, msg["error"]))
                return msg.get("result")
        raise TimeoutError("CDP %s 超时" % method)

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass


# ------------------------------------------------------------ 主流程
def _http_json(port, path, tries=60):
    url = "http://127.0.0.1:%d%s" % (port, path)
    for _ in range(tries):
        try:
            with urllib.request.urlopen(url, timeout=1) as r:
                return json.loads(r.read().decode())
        except Exception:
            time.sleep(0.25)
    raise TimeoutError("DevTools 端口 %d 没起来" % port)


def profile_dir():
    """持久化 profile：第一次登录后留下来，以后点「登录」直接复读，不用再扫一次码。"""
    base = os.path.join(os.environ.get("LOCALAPPDATA") or tempfile.gettempdir(),
                        "QuarkAccel")
    os.makedirs(base, exist_ok=True)
    return os.path.join(base, "profile")


def launch_and_read_cookie(log=print, need=("__puus",), port=9333, keep_profile=True,
                           wait_secs=300):
    """起浏览器 -> 等用户登录 -> 读 cookie。返回 cookie 字符串。"""
    exe = find_browser()
    if not exe:
        raise RuntimeError("没找到 Edge/Chrome，请手动粘贴 Cookie")
    profile = profile_dir()
    if not keep_profile:
        shutil.rmtree(profile, ignore_errors=True)
    os.makedirs(profile, exist_ok=True)
    args = [exe, "--remote-debugging-port=%d" % port,
            "--user-data-dir=%s" % profile, "--no-first-run",
            "--no-default-browser-check", "--disable-features=msEdgeSidebarV2",
            LOGIN_URL]
    log("启动浏览器：%s" % os.path.basename(exe))
    proc = subprocess.Popen(args)
    try:
        ver = _http_json(port, "/json/version")
        ws_url = ver["webSocketDebuggerUrl"]
        log("DevTools 就绪，等待登录 pan.quark.cn …")
        ws = WS(ws_url)
        try:
            deadline = time.time() + wait_secs
            last = ""
            while time.time() < deadline:
                res = ws.call("Storage.getCookies") or {}
                cookies = res.get("cookies") or []
                jar = {}
                for c in cookies:
                    dom = c.get("domain", "")
                    if "quark.cn" in dom:
                        jar[c["name"]] = c["value"]
                have = [k for k in need if k in jar]
                tip = "已取到 %d 个 quark cookie（%s）" % (
                    len(jar), "已登录" if len(have) == len(need) else "等待登录…")
                if tip != last:
                    log(tip)
                    last = tip
                if len(have) == len(need) and len(jar) >= 5:
                    order = [k for k in jar if k.startswith(("_c_", "__", "_UP_"))]
                    order.sort()
                    cookie = "; ".join("%s=%s" % (k, jar[k]) for k in order)
                    log("登录成功，cookie %d 字节" % len(cookie))
                    return cookie
                time.sleep(1.5)
            raise TimeoutError("等了 %d 秒没检测到登录" % wait_secs)
        finally:
            ws.close()
    finally:
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except Exception:
            pass
        if not keep_profile:
            time.sleep(0.5)
            shutil.rmtree(profile, ignore_errors=True)


if __name__ == "__main__":
    print(launch_and_read_cookie())
