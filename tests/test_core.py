# -*- coding: utf-8 -*-
"""核心引擎、下载重试、尾部抢占、流式预览与本地服务安全测试。"""
import hashlib
import http.server
import os
import re
import socketserver
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "src")
SHELL = os.path.join(SRC, "shell")
for p in (SRC, SHELL):
    if p not in sys.path:
        sys.path.insert(0, p)

import engine
import profiles
import quark_core
import server
import stream


class _MockRangeServer:
    """本地 Range HTTP 测试服务，可注入分片失败与慢连接。"""

    def __init__(self, data: bytes, fail_first_n=None, slow_once_offset=None, slow_sec=4.5):
        self.data = data
        self.fail_first_n = dict(fail_first_n or {})
        self.slow_once_offset = slow_once_offset
        self.slow_sec = slow_sec
        self.hits = {}
        self.lock = threading.Lock()
        parent = self

        class _H(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, fmt, *args):
                pass

            def do_GET(self):
                rng = self.headers.get("Range") or ""
                m = re.match(r"^bytes=(\d+)-(\d+)$", rng.strip())
                if not m:
                    body = parent.data
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                a, b = int(m.group(1)), int(m.group(2))
                with parent.lock:
                    cnt = parent.hits.get(a, 0) + 1
                    parent.hits[a] = cnt
                    need_fail = parent.fail_first_n.get(a, 0)
                    is_slow = (parent.slow_once_offset == a and cnt == 1)
                if cnt <= need_fail:
                    self.send_response(503)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                body = parent.data[a:b + 1]
                self.send_response(206)
                self.send_header("Content-Range", "bytes %d-%d/%d" % (a, b, len(parent.data)))
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                if is_slow:
                    time.sleep(parent.slow_sec)
                try:
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    pass

        class _Srv(socketserver.ThreadingMixIn, http.server.HTTPServer):
            daemon_threads = True
            allow_reuse_address = True

        self.srv = _Srv(("127.0.0.1", 0), _H)
        self.port = self.srv.server_address[1]
        self.url = "http://127.0.0.1:%d/file.bin" % self.port
        self.th = threading.Thread(target=self.srv.serve_forever, daemon=True)

    def __enter__(self):
        self.th.start()
        return self

    def __exit__(self, *args):
        self.srv.shutdown()
        self.srv.server_close()


class TestQuarkCore(unittest.TestCase):
    def test_pool_maxsize_covers_turbo_conns(self):
        api = quark_core.Api("")
        ad = api.s.get_adapter("https://drive-pc.quark.cn/")
        self.assertGreaterEqual(ad._pool_maxsize, 384)

    def test_governor_reward_resets_fails(self):
        gov = quark_core.Governor(hi=64, lo=8, start=32, on=False)
        gov.penalty()  # fails = 1
        self.assertEqual(gov.limit, 64)
        gov.reward()   # 成功应清零 fails
        gov.penalty()  # fails = 1（不应触发折半）
        self.assertEqual(gov.limit, 64)
        gov.penalty()  # 连续第 2 次连接失败，触发折半
        self.assertEqual(gov.limit, 32)
        self.assertTrue(gov.degraded)

    def test_downloader_outer_retry_recovers_after_4_failures(self):
        # 构造 3 MB 数据，第 2 片前 4 次请求返回 503，迫使其进入 run() 的外层重试循环
        data = os.urandom(3 << 20)
        with _MockRangeServer(data, fail_first_n={1 << 20: 4}) as srv:
            api = quark_core.Api("")
            with tempfile.TemporaryDirectory() as td:
                out = os.path.join(td, "retry.bin")
                task = quark_core.Task(rel="retry.bin", fid="f1", size=len(data))
                dl = quark_core.Downloader(
                    api, task, out, conns=4, part_mb=1, tokens=2,
                    link_fn=lambda: srv.url, adaptive=False,
                )
                ok = dl.run()
                self.assertTrue(ok, "外层重试应恢复前 4 次失败的分片: %s" % dl.fail)
                with open(out, "rb") as f:
                    got = f.read()
                self.assertEqual(hashlib.sha256(got).hexdigest(),
                                 hashlib.sha256(data).hexdigest())
                self.assertFalse(os.path.exists(out + ".qstate"))
                self.assertFalse(os.path.exists(out + ".qbad"))

    def test_downloader_hedged_tail_preempts_slow_chunk(self):
        # 第 3 片首次请求卡 5 秒，空闲线程应通过尾部投机抢占提前完成，且字节计数准确
        data = os.urandom(3 << 20)
        with _MockRangeServer(data, slow_once_offset=2 << 20, slow_sec=5.0) as srv:
            api = quark_core.Api("")
            with tempfile.TemporaryDirectory() as td:
                out = os.path.join(td, "hedge.bin")
                task = quark_core.Task(rel="hedge.bin", fid="f2", size=len(data))
                dl = quark_core.Downloader(
                    api, task, out, conns=4, part_mb=1, tokens=2,
                    link_fn=lambda: srv.url, adaptive=False,
                )
                ok = dl.run()
                self.assertTrue(ok, "尾部投机重试后应成功: %s" % dl.fail)
                self.assertEqual(dl.bytes_done, len(data))
                with open(out, "rb") as f:
                    self.assertEqual(f.read(), data)

    def test_stream_link_random_distribution(self):
        class _DummyApi:
            def __init__(self):
                self.n = 0

            def download_url(self, fid):
                self.n += 1
                return "http://127.0.0.1/tok/%d" % self.n

        with tempfile.TemporaryDirectory() as td:
            st = stream.Stream(_DummyApi(), "fid1", 1024, os.path.join(td, "c.bin"),
                               conns=4, tokens=8)
            picked = {st._link() for _ in range(30)}
            self.assertGreater(len(picked), 1, "预览取链应随机分散在 token 池中")

    def test_engine_small_files_concurrent_download(self):
        data = os.urandom(2 << 20)
        with _MockRangeServer(data) as srv:
            api = quark_core.Api("")
            with tempfile.TemporaryDirectory() as td:
                eng = engine.Engine("", td, tier="turbo", conns=12, part_mb=1, tokens=2)
                done_ev = threading.Event()
                left = {"n": 3}

                def _cb(job):
                    left["n"] -= 1
                    if left["n"] <= 0:
                        done_ev.set()

                items = [
                    {"fid": "s%d" % i, "name": "small_%d.bin" % i, "size": len(data),
                     "link_fn": lambda: srv.url, "dl_api": api, "on_done": _cb}
                    for i in range(3)
                ]
                eng.add(items, td)
                self.assertTrue(done_ev.wait(25), "3 个小文件并发下载应在超时内完成")
                for i in range(3):
                    p = os.path.join(td, "small_%d.bin" % i)
                    self.assertTrue(os.path.exists(p))
                    with open(p, "rb") as f:
                        self.assertEqual(f.read(), data)

    def test_server_token_and_host_guard(self):
        class _MiniApp:
            api_token = "tok_test_123456"

            def log(self, msg):
                pass

            def api_state(self):
                return {"ok": True, "demo": True}

        srv = server.Server(_MiniApp(), host="127.0.0.1", port=0)
        srv.start()
        try:
            base = "http://127.0.0.1:%d" % srv.port
            # 1) ui.html 注入 qa-token
            with urllib.request.urlopen(base + "/", timeout=5) as r:
                html = r.read().decode("utf-8")
            self.assertIn('name="qa-token" content="tok_test_123456"', html)
            # 2) 无 token 调 /api/state 必须被 403 拒绝
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                urllib.request.urlopen(base + "/api/state", timeout=5)
            self.assertEqual(ctx.exception.code, 403)
            # 3) 带正确 token 放行
            req = urllib.request.Request(base + "/api/state",
                                         headers={"X-QA-Token": "tok_test_123456"})
            with urllib.request.urlopen(req, timeout=5) as r:
                self.assertEqual(r.status, 200)
            # 4) 伪造 Host 头被拒绝（防 DNS Rebinding）
            req_bad = urllib.request.Request(base + "/api/ping",
                                             headers={"Host": "evil.example.com"})
            with self.assertRaises(urllib.error.HTTPError) as ctx2:
                urllib.request.urlopen(req_bad, timeout=5)
            self.assertEqual(ctx2.exception.code, 403)
        finally:
            srv.stop()

    def test_profiles_clamp(self):
        self.assertEqual(profiles.resolve("custom", 9999, 99, 999), (384, 32, 64))
        self.assertEqual(profiles.resolve("custom", 1, 0, 1), (8, 1, 4))


if __name__ == "__main__":
    unittest.main()
