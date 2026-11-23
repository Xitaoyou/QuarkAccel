# -*- coding: utf-8 -*-
"""server.py —— 本地 HTTP 服务：把 ui.html 和 /api/* 跑在 127.0.0.1 上。

为什么用本地 http 而不是往 pan.quark.cn 里注入：
  · 页内 fetch http://127.0.0.1 会被 PNA / 跨源拦（实测永远 pending）
  · 页面自己从本机加载 → 与 /api/* 同源，零 CORS、零 PNA
  · 界面不再依赖官方网页版是否可用，账号被封也照样能跑引擎
"""
import io
import json
import mimetypes
import os
import re
import sys
import threading
import time
import traceback
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

def _ui_dir():
    """ui.html 所在目录：源码运行 = 本文件同级；打包后 = _MEIPASS 根（--add-data 打进去）。"""
    cands = [os.path.dirname(os.path.abspath(__file__)),
             getattr(sys, "_MEIPASS", ""),
             os.path.dirname(os.path.abspath(sys.executable))]
    for c in cands:
        if c and os.path.exists(os.path.join(c, "ui.html")):
            return c
    return cands[0]


HERE = _ui_dir()

MIME = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".png": "image/png",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
}


# 视频预览一次最多吐 4 MB：浏览器发 bytes=0- 时不会被一条巨型响应卡住，
# 它会自己接着发下一段，而我们每段都能等预取补齐再给。
MEDIA_SPAN = 1 << 20        # 稳定播放阶段一次喂 4 MB
MEDIA_FIRST = 1 << 20       # 开头只喂 1 MB，让它尽快出第一帧
RANGE_RE = re.compile(r"^bytes=(\d*)-(\d*)$")


class Handler(BaseHTTPRequestHandler):
    server_version = "QuarkAccel/2.0"
    protocol_version = "HTTP/1.1"

    # -- 基础设施
    def log_message(self, fmt, *args):
        pass

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False))

    def _body(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        if n <= 0:
            return {}
        raw = self.rfile.read(n)
        try:
            return json.loads(raw.decode("utf-8", "replace"))
        except Exception:
            return {}

    # -- 路由
    def do_GET(self):
        self._route("GET")

    def do_POST(self):
        self._route("POST")

    def do_HEAD(self):
        u = urllib.parse.urlparse(self.path)
        if u.path.startswith("/media/"):
            return self._media(urllib.parse.parse_qs(u.query), vid=u.path[7:], head=True)
        self.send_response(404)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "content-type")
        self.send_header("Access-Control-Allow-Methods", "GET,POST,OPTIONS")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _route(self, method):
        u = urllib.parse.urlparse(self.path)
        path = u.path
        qs = urllib.parse.parse_qs(u.query)
        app = self.server.app
        try:
            if path in ("/", "/index.html"):
                return self._file("ui.html")
            if path.startswith("/media/"):
                return self._media(qs, vid=path[7:])
            if path.startswith("/api/"):
                return self._api(path[5:], qs, method)
            # 静态资源（图标等）
            name = path.lstrip("/")
            if name and "/" not in name and ".." not in name and os.path.exists(os.path.join(HERE, name)):
                return self._file(name)
            return self._send(404, "not found", "text/plain; charset=utf-8")
        except Exception:
            app.log("HTTP 异常 %s: %s" % (path, traceback.format_exc().splitlines()[-1][:120]))
            return self._json({"ok": False, "err": "服务器内部错误"}, 500)

    def _file(self, name):
        p = os.path.join(HERE, name)
        with io.open(p, "rb") as f:
            data = f.read()
        ctype = MIME.get(os.path.splitext(name)[1].lower()) or "application/octet-stream"
        self._send(200, data, ctype)

    # -- 视频预览：把并发预取的本地缓存按 Range 喂给 <video>
    def _media(self, qs, vid="", head=False):
        app = self.server.app
        try:
            fid = vid or (qs.get("fid") or [""])[0]
            # 服务端已知大小优先：query 里的 size 是客户端传的，列表与 /file/download
            # 不一致时（或手改 URL）会让 Content-Range 报错总长，还会因 size 不匹配把
            # 正常流反复 _drop 重建，播放彻底卡死。
            size = int(app.media_size(fid) or 0) if app.media_known(fid) else 0
            if size <= 0:
                size = int((qs.get("size") or ["0"])[0] or 0)
            rng = self.headers.get("Range") or ""
            m = RANGE_RE.match(rng.strip())
            a, b = (0, -1)
            suffix = -1
            if m:
                if not m.group(1) and m.group(2):
                    # bytes=-N = 最后 N 字节（部分播放器 seek 会发），不能当成 0-N
                    suffix = int(m.group(2))
                else:
                    a = int(m.group(1)) if m.group(1) else 0
                    b = int(m.group(2)) if m.group(2) else -1
            # 第一次请求（哪怕是 HEAD 探路）就把流开起来；之后每个 Range 复用同一条流
            if not app.media_known(fid) or size <= 0:
                info = app.media_open(fid)
                if not info.get("ok"):
                    return self._send(404, info.get("err") or "unknown file",
                                      "text/plain; charset=utf-8")
                size = int(info.get("size") or 0)
            if size <= 0:
                return self._send(404, "unknown file", "text/plain; charset=utf-8")
            if suffix >= 0:
                a = max(0, size - suffix)
                b = size - 1
            if b < 0 or b >= size:
                b = size - 1
            if a >= size or b < a:
                self.send_response(416)
                self.send_header("Content-Range", "bytes */%d" % size)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            span = MEDIA_FIRST if a == 0 else MEDIA_SPAN
            if b - a + 1 > span:
                b = min(size - 1, a + span - 1)
            want = b - a + 1
            nm = (qs.get("name") or [""])[0]
            # 先取数据再发头：头里已经承诺了 Content-Length，先发头后取不到数会让
            # keep-alive 连接上的下一个响应错位（浏览器读到上一段视频的尾巴当 JSON）。
            data = None
            if not head:
                data = app.media_read(fid, a, b, size)
                if data is None or len(data) != want:
                    app.log("预览取数失败 %s @%d-%d got=%s" %
                            (fid[:8], a, b, "None" if data is None else len(data)))
                    self._send(503, "preview unavailable", "text/plain; charset=utf-8")
                    return
            self.send_response(206)
            self.send_header("Content-Type",
                             (mimetypes.guess_type(nm)[0] if nm else "") or "video/mp4")
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Range", "bytes %d-%d/%d" % (a, b, size))
            self.send_header("Content-Length", str(want))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            if head:
                return
            try:
                self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError):
                pass     # 播放器 seek 会中断上一段，正常
        except Exception:
            app.log("media 异常: " + traceback.format_exc().splitlines()[-1][:140])

    def _api(self, name, qs, method):
        app = self.server.app
        body = self._body() if method == "POST" else {}

        if name == "list":
            fid = (qs.get("fid") or ["0"])[0] or "0"
            nav = (qs.get("nav") or ["all"])[0] or "all"
            return self._json(app.api_list(fid, nav))
        if name == "state":
            return self._json(app.api_state())
        if name == "profile":
            return self._json(app.api_profile(body.get("tier"), body.get("conns"),
                                              body.get("part"), body.get("tokens")))
        if name == "add":
            return self._json(app.api_add(body.get("items") or [], body.get("dir")))
        if name == "import_preview":
            return self._json(app.api_import_preview(body.get("link") or
                                                     (qs.get("link") or [""])[0]))
        if name == "import_start":
            # pick 缺省（不传这个键）= 全都要；传空数组 = 用户一个都没勾，
            # 这两种必须区分开，所以不能写 body.get("pick") or None
            return self._json(app.api_import_start(body.get("link") or "",
                                                   bool(body.get("delete", True)),
                                                   body.get("pick")))
        if name == "import_state":
            return self._json(app.api_import_state())
        if name == "import_cancel":
            return self._json(app.api_import_cancel())
        if name == "cancel":
            return self._json(app.api_cancel(body.get("id")))
        if name == "retry":
            return self._json(app.api_retry(body.get("id")))
        if name == "pause":
            return self._json(app.api_pause(body.get("id")))
        if name == "resume":
            return self._json(app.api_resume(body.get("id")))
        if name == "remove":
            return self._json(app.api_remove(body.get("id")))
        if name == "clear":
            return self._json(app.api_clear())
        if name == "hist":
            return self._json(app.api_hist())
        if name == "dir_list":
            return self._json(app.api_dir_list(body.get("limit")
                                               or int((qs.get("limit") or [60])[0])))
        if name == "delete":
            return self._json(app.api_delete(body.get("id")))
        if name == "clear_hist":
            return self._json(app.api_clear_hist())
        if name == "reveal":
            return self._json(app.api_reveal(body.get("id"), body.get("path")))
        if name == "open_dir":
            return self._json(app.api_open_dir())
        if name == "pick_dir":
            return self._json(app.api_pick_dir())
        if name == "cookie":
            return self._json(app.api_cookie(body.get("cookie") or ""))
        if name == "demo":
            app.demo = bool(body.get("on"))
            return self._json({"ok": True, "on": app.demo})
        if name == "login":
            return self._json(app.api_login())
        if name == "login_done":
            # port 缺省/0 都表示「自己找」：登录窗口用的是随机调试端口，
            # 写死 9481 会让取 Cookie 永远连不上
            return self._json(app.api_login_done(int(body.get("port") or 0)))
        if name == "media":
            fid = body.get("fid") or (qs.get("fid") or [""])[0]
            return self._json(app.media_open(fid))
        if name == "media_stat":
            return self._json(app.media_stat((qs.get("fid") or [""])[0]))
        if name == "media_stop":
            fid = body.get("fid") or (qs.get("fid") or [""])[0]
            return self._json(app.media_stop(fid))
        if name == "media_clear":
            return self._json(app.media_clear())
        if name == "version":
            return self._json({"ok": True, "v": "2.0"})
        if name == "ping":
            return self._json({"ok": True})
        return self._json({"ok": False, "err": "未知接口 " + name}, 404)


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, app, host="127.0.0.1", port=0):
        self.app = app
        super().__init__((host, port), Handler)
        self.port = self.server_address[1]

    def start(self):
        t = threading.Thread(target=self.serve_forever, daemon=True)
        t.start()
        return t

    def stop(self):
        try:
            self.shutdown()
        except Exception:
            pass
        try:
            self.server_close()     # shutdown() 只停循环，监听 socket 要单独关
        except Exception:
            pass
