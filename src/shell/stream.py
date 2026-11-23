# -*- coding: utf-8 -*-
"""stream.py —— 视频边下边播：把远端文件切片并发预取到本地缓存，再按 Range 喂给 <video>。

为什么需要它：
  浏览器播放器只会串行发 Range 请求，而夸克单连接实测 12–350 KB/s，
  720p 的码率就能把它喂死，看两秒卡三秒。这里把文件切成 1 MB 片、几十路并发
  预取到本地，播放器要哪一段就从缓存里给哪一段 —— 只要预取比播放快就不卡。

用法：
  st = Stream(api, fid, size, cache_path, conns=64)
  st.start()
  data = st.read(0, 262143)    # 阻塞到这段可用；超时返回 None
  st.stop()
"""
import bisect
import collections
import hashlib
import os
import threading
import time


class FakeApi:
    """测试用：把 fid 当文件名，从一个本地 HTTP 服务取内容。"""

    def __init__(self, base):
        import requests
        self.s = requests.Session()
        self.base = base.rstrip("/") + "/"

    def download_url(self, fid):
        return self.base + fid


class LocalReader:
    """本地文件版的取片器：签名与 HTTP 取片一致，(a,b) -> bytes 或 None。"""

    def __init__(self, path):
        self.path = path
        self.size = os.path.getsize(path)

    def __call__(self, a, b):
        try:
            with open(self.path, "rb") as f:
                f.seek(a)
                return f.read(b - a + 1)
        except OSError:
            return None


class Stream:
    """一个远端文件的并发预取器 + 随机读接口。

    reader 不为 None 时改用本地取片（演示视频、已下载的文件），不走 HTTP。
    """

    def __init__(self, api, fid, size, path, conns=64, part_mb=1, tokens=8, reader=None,
                 part_bytes=None):
        # api 可以是 Api 实例，也可以是返回 Api 的可调用对象（登录/换 Cookie 后才有效）。
        # 用可调用对象才能在换 Cookie 后取到新链，而不是一直用启动时那个。
        self._api_src = api
        self.reader = reader
        self.fid = fid
        self.size = int(size or 0)
        self.path = path
        # 片越小越早能喂出第一帧（片是取片与就绪的最小单位），预览用 256 KB；
        # 下载器那边仍用 1 MB 大切片换更高的吞吐。
        self.part = int(part_bytes) if part_bytes else (max(1, int(part_mb)) << 20)
        self.conns = max(4, int(conns))
        self.ntok = max(1, int(tokens))
        self.stop_ev = threading.Event()
        self.lock = threading.Lock()
        self.cond = threading.Condition(self.lock)
        self.done = set()
        self.taken = set()      # 已经在取/取完的片号，防止同一片被两路线程重复拉
        self.fail = []
        self.bytes_done = 0
        self.urgent = collections.deque()
        self.todo = collections.deque()
        self.links = []
        self.link_lock = threading.Lock()
        self.t0 = time.time()
        self.t_last = time.time()
        self.b_last = 0
        self.speed = 0.0
        self.threads = []
        self._wfd = -1
        self._rfd = -1
        # 读路径专用锁：read() 用 lseek+read 共享同一个 _rfd，播放器 seek 时会并发两条
        # Range 请求，不锁就会 A 线程 seek 到 a、B 线程 seek 到 b，A 读到 b 的内容（花屏）。
        # stop() 关 fd 也在这把锁里，避免关掉后被别的线程的 os.open 复用同一个 fd 号。
        self._rlock = threading.Lock()
        # 开头几 MB 用更小的片：片是「就绪」的最小单位，单连接被整形到 40 KB/s 时
        # 一片 256 KB 要 6 秒才凑齐，第一帧就得干等；64 KB 小片 1.6 秒就能出画面。
        self.chunks = []
        ramp_end = min(self.size, 2 << 20)
        nano = max(64 << 10, self.part // 4)
        pos = 0
        while pos < self.size:
            step = nano if pos < ramp_end else self.part
            e = min(pos + step - 1, self.size - 1)
            self.chunks.append((pos, e))
            pos = e + 1
        self.starts = [c[0] for c in self.chunks]
        self.todo.extend(range(len(self.chunks)))
        # 排队顺序决定"最先就绪的是哪几段"，直接决定首帧要等多久：
        #   ① 尾部 1 MB —— MP4 的 moov（时长/索引）几乎总在末尾，没有它播放器连
        #      视频多长都不知道，会一直停在 readyState 0；
        #   ② 开头 2 MB —— ftyp + 首帧的 mdat 数据；
        #   ③ 其余顺序铺开，保证后面的播放不断粮。
        front = []
        if self.size > (4 << 20):
            front += list(self._span(max(0, self.size - (1 << 20)), self.size - 1))
        if self.size > (512 << 10):
            front += list(self._span(0, min(self.size - 1, (2 << 20) - 1)))
        if front:
            seen = set()
            front = [i for i in front if i < len(self.chunks) and not (i in seen or seen.add(i))]
            skip = set(front)
            self.todo = collections.deque(front + [i for i in self.todo if i not in skip])

    # ------------------------------------------------------------ 生命周期
    def start(self):
        d = os.path.dirname(self.path)
        if d:
            try:
                os.makedirs(d, exist_ok=True)
            except OSError:
                pass
        b = getattr(os, "O_BINARY", 0)
        self._wfd = os.open(self.path, os.O_RDWR | os.O_CREAT | b, 0o666)
        self._rfd = os.open(self.path, os.O_RDONLY | b)
        try:
            os.ftruncate(self._wfd, self.size)
        except OSError:
            pass
        n = min(self.conns, len(self.chunks))
        for _ in range(n):
            t = threading.Thread(target=self._worker, daemon=True)
            t.start()
            self.threads.append(t)
        return self

    def stop(self):
        self.stop_ev.set()
        with self.cond:
            self.cond.notify_all()
        with self._rlock:
            for fd in (self._wfd, self._rfd):
                try:
                    if fd >= 0:
                        os.close(fd)
                except OSError:
                    pass
            self._wfd = self._rfd = -1

    # ------------------------------------------------------------ 取链
    def _api(self):
        """当前有效的 Api；解析不出来就抛，交给 _worker 的重试逻辑。"""
        src = self._api_src
        api = src() if callable(src) and not hasattr(src, "download_url") else src
        if api is None:
            raise RuntimeError("还没有登录")
        self.api = api
        return api

    def _refill(self, want):
        with self.link_lock:
            need = want - len(self.links)
        for _ in range(max(0, need)):
            try:
                u = self._api().download_url(self.fid)
            except Exception:
                break
            with self.link_lock:
                self.links.append(u)

    def _link(self):
        with self.link_lock:
            low = len(self.links) < max(2, self.ntok // 2)
        if low:
            self._refill(self.ntok)
        with self.link_lock:
            if self.links:
                return self.links[len(self.links) // 2]
        return self._api().download_url(self.fid)

    def _drop(self, url):
        if not url:
            return
        with self.link_lock:
            self.links = [x for x in self.links if x != url]

    # ------------------------------------------------------------ 取片
    def _next(self):
        """取一个还没人认领的片号；全被认领了就返回 None（线程退出）。"""
        with self.lock:
            for q in (self.urgent, self.todo):
                while q:
                    i = q.popleft()
                    if i in self.taken:
                        continue
                    self.taken.add(i)
                    return i
            return None

    def _worker(self):
        while not self.stop_ev.is_set():
            i = self._next()
            if i is None:
                return
            a, b = self.chunks[i]
            want = b - a + 1
            if self.reader is not None:
                buf = self.reader(a, b)
                if buf and len(buf) >= want:
                    with self.lock:
                        if self.stop_ev.is_set() or self._wfd < 0:
                            return
                        os.lseek(self._wfd, a, os.SEEK_SET)
                        os.write(self._wfd, bytes(buf[:want]))
                        self.done.add(i)
                        self.bytes_done += want
                        self.cond.notify_all()
                    continue
                with self.cond:
                    self.taken.discard(i)
                    self.fail.append(i)
                    self.cond.notify_all()
                continue
            for attempt in range(3):
                if self.stop_ev.is_set():
                    return
                url = None
                try:
                    url = self._link()
                    r = self._api().s.get(url, headers={
                        "Range": "bytes=%d-%d" % (a, b),
                        "Accept-Encoding": "identity"}, timeout=(15, 60), stream=True)
                    try:
                        if r.status_code == 412:
                            self._drop(url)
                            time.sleep(0.2)
                            continue
                        if r.status_code not in (200, 206):
                            raise RuntimeError("HTTP %d" % r.status_code)
                        # 服务端/中间层丢掉 Range 时会回 200 + 整个文件，buf[0:want] 是文件
                        # 开头而不是 a 处的数据 —— 直接写下去就是错位内容（静默花屏）。
                        if r.status_code == 200 and a > 0:
                            raise RuntimeError("忽略 Range")
                        buf = bytearray()
                        for blk in r.iter_content(1 << 16):
                            buf += blk
                            if len(buf) >= want:
                                break
                    finally:
                        try:
                            r.close()
                        except Exception:
                            pass
                    if len(buf) < want:
                        raise RuntimeError("短读 %d/%d" % (len(buf), want))
                    with self.lock:
                        if self.stop_ev.is_set() or self._wfd < 0:
                            return
                        os.lseek(self._wfd, a, os.SEEK_SET)
                        os.write(self._wfd, bytes(buf[:want]))
                        self.done.add(i)
                        self.bytes_done += want
                        self.cond.notify_all()
                    break
                except Exception as e:
                    if "412" in str(e):
                        self._drop(url)
                    time.sleep(0.3 * (attempt + 1))
            else:
                with self.cond:
                    self.taken.discard(i)     # 交回去，read() 还能再催一次
                    self.fail.append(i)
                    self.cond.notify_all()

    # ------------------------------------------------------------ 读
    def _span(self, a, b):
        """[a,b] 覆盖哪些片号。片长不统一（开头有斜坡），所以用二分查起点。"""
        if not self.chunks:
            return range(0)
        i0 = bisect.bisect_right(self.starts, a) - 1
        i1 = bisect.bisect_right(self.starts, b) - 1
        i0 = max(0, min(i0, len(self.chunks) - 1))
        i1 = max(i0, min(i1, len(self.chunks) - 1))
        return range(i0, i1 + 1)

    def read(self, a, b, timeout=90.0):
        """返回 [a,b] 的字节；超时或已停止返回 None。"""
        if self.size <= 0:
            return b""
        a = max(0, int(a))
        b = min(self.size - 1, int(b))
        if b < a:
            return b""
        need = [i for i in self._span(a, b) if i not in self.done]
        if need:
            with self.cond:
                self.urgent.extend(need)
                self.cond.notify_all()
                deadline = time.time() + timeout
                while True:
                    left = [i for i in need if i not in self.done]
                    if not left:
                        break
                    if all(i in self.fail for i in left):
                        return None
                    rem = deadline - time.time()
                    if rem <= 0 or self.stop_ev.is_set():
                        return None
                    self.cond.wait(min(0.25, rem))
        want = b - a + 1
        with self._rlock:
            if self._rfd < 0:
                return None
            try:
                os.lseek(self._rfd, a, os.SEEK_SET)
                buf = bytearray()
                while len(buf) < want:
                    blk = os.read(self._rfd, min(1 << 16, want - len(buf)))
                    if not blk:
                        break
                    buf += blk
                return bytes(buf)
            except OSError:
                return None

    # ------------------------------------------------------------ 状态
    def stat(self):
        with self.lock:
            got = self.bytes_done
            ndone = len(self.done)
        now = time.time()
        dt = now - self.t_last
        if dt >= 0.5:
            self.speed = (got - self.b_last) / 1048576.0 / dt
            self.b_last = got
            self.t_last = now
        total = len(self.chunks) or 1
        return {
            "fid": self.fid, "size": self.size, "got": min(got, self.size),
            "pct": min(100.0, got * 100.0 / (self.size or 1)),
            "speed": round(max(0.0, self.speed), 2),
            "done": ndone, "total": total,
            "failed": len(self.fail),
            "elapsed": int(now - self.t0),
        }


class Pool:
    """同一时间只留几个活跃流，超出按最久未用淘汰（缓存文件一起删）。"""

    def __init__(self, api, cache_dir, conns=64, part_mb=1, tokens=8, max_items=3,
                 part_bytes=None):
        self.api = api
        self.cache_dir = cache_dir
        self.conns = conns
        self.part_mb = part_mb
        self.tokens = tokens
        self.part_bytes = part_bytes
        self.max_items = max(1, int(max_items))
        self.items = {}
        self.lock = threading.Lock()
        try:
            os.makedirs(cache_dir, exist_ok=True)
        except OSError:
            pass

    def get(self, fid, size, reader=None):
        size = int(size or 0)
        with self.lock:
            cur = self.items.get(fid)
            if cur and (not size or cur["st"].size == size):
                cur["used"] = time.time()
                return cur["st"]
            if cur:
                self._drop(fid)
            while len(self.items) >= self.max_items:
                old = min(self.items.items(), key=lambda kv: kv[1]["used"])
                self._drop(old[0])
            # 文件名用哈希：fid 来自 URL，直接拼进路径是穿越面（也怕超长名 ENAMETOOLONG）
            path = os.path.join(self.cache_dir, hashlib.sha1(fid.encode("utf-8")).hexdigest() + ".bin")
            st = Stream(self.api, fid, size, path, conns=self.conns,
                        part_mb=self.part_mb, tokens=self.tokens, reader=reader,
                        part_bytes=self.part_bytes)
            st.start()
            self.items[fid] = {"st": st, "used": time.time()}
            return st

    def peek(self, fid):
        """只查已有流，绝不新建（Range 请求用它，否则关闭预览后每个尾随请求都会
        重新拉起一条 64 线程的全量预取）。"""
        with self.lock:
            cur = self.items.get(fid)
            if not cur:
                return None
            cur["used"] = time.time()
            return cur["st"]

    def stat(self, fid):
        with self.lock:
            cur = self.items.get(fid)
            if not cur:
                return None
            cur["used"] = time.time()
            return cur["st"].stat()

    def _drop(self, fid):
        cur = self.items.pop(fid, None)
        if not cur:
            return
        try:
            cur["st"].stop()
        except Exception:
            pass
        try:
            os.remove(cur["st"].path)
        except OSError:
            pass

    def stop_one(self, fid):
        with self.lock:
            self._drop(fid)

    def stop_all(self):
        with self.lock:
            for fid in list(self.items):
                self._drop(fid)

    def clear_cache(self):
        with self.lock:
            for fid in list(self.items):
                self._drop(fid)
        try:
            for n in os.listdir(self.cache_dir):
                if n.endswith(".bin"):
                    os.remove(os.path.join(self.cache_dir, n))
        except OSError:
            pass
