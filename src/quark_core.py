"""夸克网盘加速下载 —— 核心库（GUI 与 CLI 共用，只依赖 requests）"""
from __future__ import annotations

import json
import os
import queue
import random
import threading
import time
from dataclasses import dataclass

import requests

API = "https://drive-pc.quark.cn/1/clouddrive"
UA_CLIENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
             "(KHTML, like Gecko) quark-cloud-drive/3.20.0 Chrome/126.0.6478.234 "
             "Electron/31.4.0 Safari/537.36 Channel/pckk_other_ch")
UA_WEB = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/139.0.0.0 Safari/537.36")
BASE_PARAMS = {"pr": "ucpro", "fr": "pc", "uc_param_str": ""}

# 这 9 个键就是 API 需要的全部身份
COOKIE_KEYS = ("_c_WBKFRo", "__sdid", "_UP_D_", "__pus", "__kp",
               "__kps", "__ktd", "__uid", "__puus")


class QuarkError(RuntimeError):
    pass


def _is_conn_err(exc):
    """连接类失败（握手超时/被重置/连接池打满）—— 这是「并发开太大」的直接信号。"""
    if isinstance(exc, (requests.exceptions.ConnectTimeout,
                        requests.exceptions.ConnectionError,
                        requests.exceptions.ReadTimeout)):
        return True
    t = type(exc).__name__
    if t in ("ConnectTimeout", "ConnectionError", "RemoteDisconnected",
             "ProtocolError", "ReadTimeout", "SSLError"):
        return True
    m = str(exc).lower()
    return ("timed out" in m or "connection aborted" in m
            or "remote end closed" in m or "connection pool" in m)


# ------------------------------------------------------------ 完整性校验
def _rng_get(api, url, a, b, tries=3):
    """取远端 [a,b] 区间；失败返回 None。"""
    h = {"Range": "bytes=%d-%d" % (a, b), "Accept-Encoding": "identity"}
    try:
        # 直链鉴权靠会话里种下的 __sdid/__pugs，Api.s.headers 里的 Cookie 可能是空串
        ck = api.dl_cookie() if hasattr(api, "dl_cookie") else ""
        if ck:
            h["Cookie"] = ck
    except Exception:
        pass
    for _ in range(tries):
        try:
            r = api.s.get(url, headers=h, timeout=(15, 60))
            if r.status_code in (200, 206):
                return r.content
        except requests.RequestException:
            continue
    return None


def _all_zero(b):
    return not b.strip(b"\x00")


def resolve_path(path):
    """网盘上分卷包常被加 .pdf 后缀绕过上传限制（xxx.7z.001.pdf），
    本地为让 7-Zip 认得会改名成 .001。两种名字都认。"""
    if os.path.exists(path):
        return path
    if path.lower().endswith(".pdf") and os.path.exists(path[:-4]):
        return path[:-4]
    if os.path.exists(path + ".pdf"):
        return path + ".pdf"
    return path


def qbad_path(path):
    """失败标记固定挂在解析后的真实文件旁边。

    下载写的是 self.out（可能是 xxx.7z.001.pdf），校验查的是 resolve_path 之后的
    名字（xxx.7z.001）；两边不统一的话 .qbad 这道闸对方括号别名完全失效。
    """
    return resolve_path(path) + ".qbad"


def file_ok(api, fid, path, size, deep=False, link_fn=None, url=None):
    """判断本地文件是否真的完整。返回 (ok, why)。

    只比大小是不够的：下载器会先把文件预分配成满尺寸的全零，
    中途失败就留下一个"大小正确、内容全零"的假文件。
    这里额外比头尾各 32KB（并发拉取，deep=True 时逐 1MB 全比）。
    """
    path = resolve_path(path)
    if not os.path.exists(path):
        return False, "不存在"
    real = os.path.getsize(path)
    if real != size:
        return False, "大小不符 %d/%d" % (real, size)
    if os.path.exists(qbad_path(path)):
        return False, "上次下载失败留下的标记(.qbad)"
    if not url:
        try:
            url = link_fn() if link_fn else api.download_url(fid)
            if not url:
                return False, "取不到直链"
        except Exception as e:
            return False, "取链失败 %s" % str(e)[:40]
    if size <= 0:
        return True, "空文件"
    if deep:
        spans = []
        a = 0
        while a < size:
            # 括号不能省：'+' 比 '<<' 结合更紧，写 a + 1 << 20 会变成 (a+1)<<20，
            # 第二圈就把上界推到文件末尾，等于把整个文件读进内存
            b = min(a + (1 << 20), size) - 1
            spans.append((a, b))
            a = b + 1
    elif size <= 65536:
        spans = [(0, size - 1)]
    else:
        spans = [(0, 32767), (size - 32768, size - 1)]

    # 头尾两段并发取，把收尾校验延迟减半
    if not deep and len(spans) > 1:
        remotes = [None] * len(spans)

        def _fetch(idx, a0, b0):
            remotes[idx] = _rng_get(api, url, a0, b0)

        ts = [threading.Thread(target=_fetch, args=(idx, a, b), daemon=True)
              for idx, (a, b) in enumerate(spans)]
        for t in ts:
            t.start()
        for t in ts:
            t.join(timeout=65)
        for (a, b), remote in zip(spans, remotes):
            if remote is None:
                return False, "远端取不到，无法校验 @%d" % a
            with open(path, "rb") as f:
                f.seek(a)
                local = f.read(len(remote))
            if local != remote:
                return False, ("全零空洞 @%d" % a) if _all_zero(local) else ("内容不符 @%d" % a)
    else:
        for a, b in spans:
            remote = _rng_get(api, url, a, b)
            if remote is None:
                return False, "远端取不到，无法校验 @%d" % a
            with open(path, "rb") as f:
                f.seek(a)
                local = f.read(len(remote))
            if local != remote:
                return False, ("全零空洞 @%d" % a) if _all_zero(local) else ("内容不符 @%d" % a)
    # 内容抽查通过；残留的 .qstate 只是上次没清掉，顺手删掉
    if os.path.exists(path + ".qstate"):
        try:
            os.remove(path + ".qstate")
        except OSError:
            pass
    return True, "ok"


@dataclass
class Entry:
    fid: str
    name: str
    size: int = 0
    is_dir: bool = False


@dataclass
class Task:
    """一个待下载文件。"""
    rel: str
    fid: str
    size: int
    status: str = "等待"
    got: int = 0
    speed: float = 0.0
    note: str = ""

    @property
    def pct(self) -> float:
        return 100.0 * self.got / self.size if self.size else 0.0


# ------------------------------------------------------------------ API
class Api:
    def __init__(self, cookie: str, ua: str = UA_CLIENT, timeout: int = 20):
        self.cookie = cookie.strip()
        self.ua = ua
        self.timeout = timeout
        self.s = requests.Session()
        self.s.trust_env = False
        # requests 默认每个 host 只留 10 条空闲连接：闸门放到 32 条以上时，
        # 超出部分每次都要重新握手，等于把并发优势全丢掉
        try:
            from requests.adapters import HTTPAdapter
            # 连接池上限必须 >= 最大并发数（384），否则超出的连接用完即弃、每次重新握手
            ad = HTTPAdapter(pool_connections=64, pool_maxsize=512,
                             max_retries=0, pool_block=False)
            self.s.mount("https://", ad)
            self.s.mount("http://", ad)
        except Exception:
            pass
        self.s.headers.update({
            "User-Agent": ua,
            "Cookie": self.cookie,
            "Referer": "https://pan.quark.cn/",
            "Accept": "application/json, text/plain, */*",
            "Origin": "https://pan.quark.cn",
        })

    def call(self, path, method="GET", body=None, params=None, retries=4):
        p = dict(BASE_PARAMS)
        if params:
            p.update(params)
        url = path if str(path).startswith("http") else API + path
        last = None
        for attempt in range(retries):
            try:
                if method == "GET":
                    r = self.s.get(url, params=p, timeout=self.timeout)
                else:
                    r = self.s.post(url, params=p, json=body, timeout=self.timeout)
                j = r.json()
                if j.get("code") not in (0, None):
                    raise QuarkError("%s %s" % (j.get("code"), j.get("message")))
                return j.get("data")
            except QuarkError as e:
                last = e
                if "31001" in str(e) or "14001" in str(e):
                    break
                time.sleep(0.6 * (attempt + 1))
            except (requests.RequestException, ValueError) as e:
                last = e
                time.sleep(0.6 * (attempt + 1))
        raise QuarkError("请求失败 %s: %s" % (path, last))

    def whoami(self):
        """返回 {nickname, member_type, size_total, size_used}。"""
        d = self.call("/member") or {}
        info = {}
        try:
            info = self.call("/account/info") or {}
        except QuarkError:
            pass
        mt = d.get("member_type") or "?"
        mi = d.get("member_info") or {}
        cap = mi.get("capacity") or mi.get("total_capacity") or 0
        use = mi.get("used_capacity") or mi.get("use_capacity") or 0
        return {
            "nickname": info.get("nickname") or "已登录",
            "member_type": mt,
            "total": int(cap or 0),
            "used": int(use or 0),
        }

    def list_dir(self, fid="0"):
        out = []
        page = 1
        while True:
            d = self.call("/file/sort", params={
                "pdir_fid": fid, "_page": page, "_size": 100,
                "_fetch_total": 1, "_sort": "file_type:asc,file_name:asc",
            }) or {}
            lst = d.get("list") or []
            for it in lst:
                out.append(Entry(
                    fid=it.get("fid", ""),
                    name=it.get("file_name") or "unnamed",
                    size=int(it.get("size") or 0),
                    is_dir=bool(it.get("dir")),
                ))
            if len(lst) < 100 or page * 100 >= int(d.get("_total") or 0):
                break
            page += 1
        return out

    def list_recent(self, limit=100):
        """最近改动：按 l_updated_at 倒序取根目录。"""
        d = self.call("/file/sort", params={
            "pdir_fid": "0", "_page": 1, "_size": limit,
            "_fetch_total": 1, "_sort": "l_updated_at:desc",
        }) or {}
        return [{"fid": it.get("fid", ""),
                 "name": it.get("file_name") or "unnamed",
                 "size": int(it.get("size") or 0),
                 "is_dir": bool(it.get("dir")),
                 "updated": int(it.get("l_updated_at") or it.get("updated_at") or 0),
                 "category": it.get("category") or 0}
                for it in (d.get("list") or [])]

    def list_recycle(self, limit=100):
        """回收站列表。"""
        d = self.call("/file/recycle/list", params={
            "_page": 1, "_size": limit, "_fetch_total": 1, "_sort": "l_updated_at:desc",
        }) or {}
        return [{"fid": it.get("fid", ""),
                 "name": it.get("file_name") or "unnamed",
                 "size": int(it.get("size") or 0),
                 "is_dir": bool(it.get("dir")),
                 "updated": int(it.get("l_updated_at") or it.get("updated_at") or 0),
                 "category": it.get("category") or 0}
                for it in (d.get("list") or [])]

    def list_raw(self, fid="0"):
        """和 list_dir 一样翻页，但返回 dict 并带 updated（界面要显示修改时间）。"""
        out = []
        page = 1
        while True:
            d = self.call("/file/sort", params={
                "pdir_fid": fid, "_page": page, "_size": 100,
                "_fetch_total": 1, "_sort": "file_type:asc,file_name:asc",
            }) or {}
            lst = d.get("list") or []
            for it in lst:
                out.append({
                    "fid": it.get("fid", ""),
                    "name": it.get("file_name") or "unnamed",
                    "size": int(it.get("size") or 0),
                    "is_dir": bool(it.get("dir")),
                    "updated": int(it.get("l_updated_at") or it.get("updated_at") or 0),
                    "category": it.get("category") or 0,
                })
            if len(lst) < 100 or page * 100 >= int(d.get("_total") or 0):
                break
            page += 1
        return out

    def download_url(self, fid):
        d = self.call("/file/download", method="POST", body={"fids": [fid]})
        if not d:
            raise QuarkError("download 返回空")
        return d[0]["download_url"]

    def dl_cookie(self):
        """下载直链要带的 Cookie。

        直链（*.drive.quark.cn）不是靠登录态鉴权，而是靠会话里被种下的 __sdid/__pugs：
        裸请求一律 412 Precondition Failed。Api 的 s.headers 里那条 Cookie 是给接口用的，
        可能是空串（未登录），会盖掉 requests 自带的 cookie jar，所以下载必须显式拼一条
        带值的头：自己账号的 cookie + 本次会话种下的游客 cookie。
        """
        pairs, seen = [], set()
        for part in (self.cookie or "").split(";"):
            if "=" not in part:
                continue
            k = part.split("=", 1)[0].strip()
            if k and k not in seen:
                seen.add(k)
                pairs.append(part.strip())
        try:
            for k, v in self.s.cookies.get_dict().items():
                if k not in seen:
                    seen.add(k)
                    pairs.append("%s=%s" % (k, v))
        except Exception:
            pass
        return "; ".join(pairs)

    def share_download_url(self, pwd_id, stoken, fid):
        """分享域直链：不用转存、不用登录也能下（上限见文件体积）。

        走 /file/share/download，而不是 /file/download —— 后者对分享里的 fid 一律报
        31001 require login，因为那些文件的主人是别人。返回 (url, md5, size)。
        """
        d = self.call("/file/share/download", method="POST",
                      body={"fids": [fid], "pwd_id": pwd_id, "stoken": stoken},
                      retries=2) or []
        if not d:
            raise QuarkError("share/download 返回空")
        it = d[0]
        return (it.get("download_url") or "", (it.get("md5") or "").lower(),
                int(it.get("size") or 0))

    # ---------------------------------------------------------- 分享链接
    # 分享页里的 fid 属于「分享域」，不能直接拿去 /file/download 换直链
    # （会报 文件不存在 / 无权限），所以只能先转存进自己的网盘再走原有链路。
    def share_token(self, pwd_id, passcode=""):
        """取分享的 stoken，同时验证链接/提取码是否有效。"""
        d = self.call("/share/sharepage/token", method="POST",
                      body={"pwd_id": pwd_id, "passcode": passcode or ""}) or {}
        tk = d.get("stoken") or ""
        if not tk:
            raise QuarkError("分享链接无效或需要提取码")
        return tk

    def share_list(self, pwd_id, stoken, pdir_fid="0"):
        """列分享目录。返回 [{fid, token, name, size, is_dir, updated}]。"""
        out, page = [], 1
        while True:
            d = self.call("/share/sharepage/detail", params={
                "pwd_id": pwd_id, "stoken": stoken, "pdir_fid": pdir_fid or "0",
                "force": 0, "_page": page, "_size": 50,
                "_fetch_banner": 0, "_fetch_share": 0, "_fetch_total": 1,
                "_sort": "file_type:asc,updated_at:desc", "ver": 2,
                "fetch_share_full_path": 0,
            }) or {}
            lst = d.get("list") or []
            for it in lst:
                out.append({
                    "fid": it.get("fid") or "",
                    "token": it.get("share_fid_token") or "",
                    "name": it.get("file_name") or "unnamed",
                    "size": int(it.get("size") or 0),
                    "is_dir": bool(it.get("dir")),
                    "updated": int(it.get("updated_at") or it.get("l_updated_at") or 0),
                })
            total = int((d.get("metadata") or {}).get("_total") or 0) or len(out)
            if not lst or page * 50 >= total:
                break
            page += 1
        return out

    def share_save(self, pwd_id, stoken, items, to_pdir_fid="0"):
        """转存到自己的网盘。items = [{fid, token}]。返回 task_id。"""
        fids = [x["fid"] for x in items]
        toks = [x.get("token") or "" for x in items]
        d = self.call("/share/sharepage/save", method="POST", body={
            "fid_list": fids, "fid_token_list": toks,
            "to_pdir_fid": to_pdir_fid, "pwd_id": pwd_id, "stoken": stoken,
            "pdir_fid": "0", "scene": "link",
        }) or {}
        return d.get("task_id") or ""

    def task_wait(self, task_id, timeout=180.0, interval=0.6):
        """轮询异步任务（转存/移动都走它）。返回 task 的 data。"""
        if not task_id:
            raise QuarkError("没有任务号")
        t0 = time.time()
        retry = 0
        while time.time() - t0 < timeout:
            d = self.call("/task", params={"task_id": task_id, "retry_index": retry}) or {}
            st = int(d.get("status") or 0)
            if st == 2:
                return d
            if st == 3:
                raise QuarkError("任务失败：%s" % (d.get("task_title") or ""))
            retry += 1
            time.sleep(interval)
        raise QuarkError("任务超时")

    def mkdir(self, name, parent="0"):
        d = self.call("/file", method="POST", body={
            "pdir_fid": parent, "file_name": name, "dir_path": "",
            "dir_init_lock": False,
        }) or {}
        return d.get("fid") or ""

    def ensure_dir(self, name, parent="0"):
        """在 parent 下找到/建出 name 目录，返回 fid。"""
        for it in self.list_raw(parent):
            if it["is_dir"] and it["name"] == name:
                return it["fid"]
        return self.mkdir(name, parent)

    def del_files(self, fids):
        """删进回收站，然后清掉回收站记录 —— 临时转存件不该占用户的回收站。"""
        fids = [f for f in fids if f]
        if not fids:
            return {"deleted": 0, "purged": 0}
        self.call("/file/delete", method="POST",
                  body={"action_type": 2, "filelist": fids, "exclude_fids": []})
        purged = 0
        try:
            rec = self.call("/file/recycle/list", params={
                "_page": 1, "_size": 100, "_fetch_total": 1,
            }) or {}
            want = set(fids)
            rec_ids = [x.get("record_id") for x in (rec.get("list") or [])
                       if x.get("fid") in want and x.get("record_id")]
            if rec_ids:
                self.call("/file/recycle/remove", method="POST",
                          body={"select_mode": 2, "record_list": rec_ids})
                purged = len(rec_ids)
        except Exception:
            pass
        return {"deleted": len(fids), "purged": purged}

    def walk(self, fid="0", prefix="", on_dir=None):
        out = []
        stack = [(fid, prefix)]
        while stack:
            cur, pre = stack.pop()
            for e in self.list_dir(cur):
                if e.is_dir:
                    if on_dir:
                        on_dir(pre + e.name)
                    stack.append((e.fid, pre + e.name + "/"))
                else:
                    out.append(Task(rel=pre + e.name, fid=e.fid, size=e.size))
        out.sort(key=lambda t: t.rel)
        return out


# ------------------------------------------------------------------ 自适应并发
class Governor:
    """按实测吞吐自动找并发甜点，不再「连接开满就是快」。

    真机实测（100 MB 公网对象，同一台机器、同一时段）：

         8 连接   41.9 MB/s
        16 连接   43.1 MB/s
        32 连接   47.7 MB/s
        64 连接    1.7 MB/s
       128 连接    0.0 MB/s（400 次 ConnectTimeout，一片都没下成）

    目标函数是「单峰 + 右侧断崖」：左边没吃饱，右边被 CDN 掐死。所以控制器
    不去猜模型，只在 [lo, hi] 上做区间收缩：

      1. 慢启动：吞吐涨 → 往上翻倍，撞到第一次不涨为止，得到 [好档, 坏档]；
      2. 二分收缩：在区间里取中点试探，比好档快就抬下界，否则压上界；
      3. 收敛即停：区间小于 max(4, 好档/8) 就认定甜点，钉住不动；
      4. 连接超时是「已越界」的硬证据，直接减半并封上界，不等窗口统计。
    """

    def __init__(self, hi, lo=8, start=None, window=1.0, on=True):
        self.hi = max(1, int(hi))
        self.lo = max(1, min(int(lo), self.hi))
        self.window = float(window)
        self.on = bool(on)          # False = 固定并发档：只保留超时回撤，不做寻优
        self.degraded = False       # 固定档被迫降过档（塌陷网络）
        self.cond = threading.Condition()
        if self.on:
            # 起手 16 条：真机实测 8/16/32 三段吞吐几乎持平（41.9/43.1/47.7 MB/s），
            # 从 8 起爬要多花两个窗口，从 32 起又太靠近断崖（64 就掉到 1.7 MB/s）
            self.limit = max(self.lo, min(self.hi, int(start if start else min(self.hi, 16))))
        else:
            # 固定档：用户设多少就是多少，直接铺满（加自适应之前的老手感）
            self.limit = self.hi
        self.active = 0
        self.good = self.limit          # 已证明最快的一档（下界）
        self.bad = 0                    # 已证明变差的档（上界），0 = 还没撞到
        self.best_rate = 0.0
        self.best_limit = self.limit
        self.rate = 0.0
        self.fails = 0                  # 连续「连接类」失败次数
        self.done = False               # 已收敛
        self.cool = 0                   # 换档后需要丢弃的窗口数
        self._n = 0
        self._pl = 0                    # 上一窗口的档位（-1 = 新档，需重新量）
        self._pr = 0.0                  # 当前档最好的一个窗口
        self._prev = 0.0                # 上一档实测值

    # -- 闸门：真正决定同时在飞的请求数
    def acquire(self, stop=None):
        with self.cond:
            while self.active >= self.limit:
                if stop is not None and stop.is_set():
                    return False
                self.cond.wait(0.2)
            self.active += 1
            return True

    def release(self):
        with self.cond:
            if self.active > 0:
                self.active -= 1
            self.cond.notify()

    # -- 连接类失败：已越界的硬证据，立刻回撤并封上界
    def penalty(self):
        with self.cond:
            self.fails += 1
            if self.fails < 2:
                return
            self.fails = 0
            if not self.on:
                # 固定档不做寻优，但塌陷时也必须回撤：CDN 对同账号连接数有软上限，
                # 撞上去就是「一片都下不来」（实测 128 连接 400 次 ConnectTimeout）。
                # 回撤后保持在这个档，不再自动往上爬 —— 用户选固定就是不要它自己动。
                if self.limit > self.lo:
                    self.degraded = True
                    self._set(max(self.lo, self.limit // 2))
                return
            self.bad = self.limit if not self.bad else min(self.bad, self.limit)
            tgt = max(self.lo, self.good if self.good < self.bad else self.bad // 2)
            self._set(tgt)
            self.done = False

    def reward(self):
        with self.cond:
            self.fails = 0

    def _set(self, n):
        n = int(max(self.lo, min(self.hi, n)))
        if n != self.limit:
            self.limit = n
            self._pl = -1
            # 换档后的第一个窗口里，还在飞的旧请求和新请求混在一起，
            # 这个窗口的吞吐既不代表旧档也不代表新档，必须丢掉
            self.cool = 1
            self.cond.notify_all()

    def _span(self):
        return max(4, self.good // 8)

    # -- 每个窗口喂一次实测吞吐（MB/s）
    def update(self, rate):
        with self.cond:
            self.rate = rate
            self._n += 1
            if not self.on:
                # 固定档：速率只拿来显示。用户选固定就是要「设多少开多少」，
                # 这里一旦自己调档，就退化成自适应了 —— 也就是用户抱怨的
                # 「改完反而比以前慢」（起手 16 条，小文件还没爬完就下完了）。
                return
            if self._pl != self.limit:
                # 刚换档：本窗口混着旧档残余，只记数不判定
                self._pl, self._pr = self.limit, rate
                return
            self._pr = max(self._pr, rate)
            if self.cool > 0:
                # 换档后的过渡窗口：既不代表旧档也不代表新档，丢弃
                self.cool -= 1
                return
            cur = self._pr
            if cur > self.best_rate:
                self.best_rate, self.best_limit = cur, self.limit
            if self.done:
                return
            if not self.bad:
                # 慢启动：明显更快就往上翻倍；不涨了就把这一档记成上界
                if self._prev > 0 and cur >= self._prev * 1.08:
                    self.good = max(self.good, self.limit)
                    self._prev = cur
                    nxt = max(self.limit + 4, int(self.limit * 1.5))
                    if nxt >= self.hi:
                        self.bad = self.hi + 1
                        self._set(self.hi)
                    else:
                        self._set(nxt)
                    return
                if self._prev <= 0:
                    # 第一档没有参照物：先立基准，再往上探
                    self._prev = cur
                    self.good = max(self.good, self.limit)
                    self._set(max(self.limit + 4, int(self.limit * 1.5)))
                    return
                self.bad = self.limit
                self._set(self.good)
                self._prev = cur
                return
            # 区间收缩：不比好档差就抬下界，否则压上界
            if self.limit > self.good and cur >= self.best_rate * 0.97:
                self.good = self.limit
                self.best_rate, self.best_limit = cur, self.limit
            elif self.limit > self.good:
                self.bad = min(self.bad, self.limit)
            self._prev = cur
            if self.bad - self.good <= self._span():
                self.done = True
                self._set(self.best_limit)
                return
            self._set(self.good + (self.bad - self.good) // 2)
    def snapshot(self):
        with self.cond:
            return {"limit": self.limit, "active": self.active,
                    "good": self.good, "bad": self.bad, "done": self.done,
                    "best": self.best_limit, "rate": round(self.rate, 2),
                    "on": self.on, "degraded": self.degraded}


# ------------------------------------------------------------------ 下载
class Downloader:
    """切片队列 + 多 token 池 + 多线程。"""

    def __init__(self, api, task, out, conns=384, part_mb=1, tokens=32,
                 resume=True, progress=None, link_fn=None, adaptive=False,
                 gov_start=None):
        # link_fn: 自定义取链（分享域直下用）。默认走自己网盘的 /file/download；
        # 分享链一旦过期/限流会报 23018，这时拿到的是空串，_next_link 会抛出去让上层决定
        self.api, self.task, self.out = api, task, out
        self.link_fn = link_fn
        # Cookie 不能在这里算完存起来：__pugs 是 /file/share/download 那次响应才种下来的，
        # 构造 Downloader 时 jar 里通常只有 __sdid，带着半条 Cookie 去请求一律 412。
        # 所以改成每次发请求现取（见 _ck()），412 时再强制刷新一次。
        self.cookie_header = ""
        self.conns = max(1, int(conns))
        self.part = max(1, int(part_mb)) << 20
        self.ntok = max(1, int(tokens))
        self.resume = resume
        self.progress = progress
        self.stop = threading.Event()
        self.lock = threading.Lock()
        self.done = set()
        self.fail = []
        self.inflight = {}       # chunk_idx -> 开始下载时刻（用于尾部慢片抢占）
        self._hedged = set()     # 已被投机重试认领的慢片，避免多路重复抢同一片
        self._chunk_times = []   # 最近分片耗时样本（秒）
        self.bytes_done = 0
        self.bytes_rx = 0
        self.links = []
        self.link_lock = threading.Lock()
        # 并发闸门：adaptive=True 时由 Governor 按实测吞吐自己找甜点（起手 16 条往上探）；
        # adaptive=False（默认）= 固定并发，用户设多少就开多少，和早先的版本一致。
        # 为什么默认不开自适应：起手 16 条要爬好几个窗口才到最优，几十 MB 的小文件
        # 还没爬到就下完了，用户看到的就是「比之前慢」。
        self.adaptive = bool(adaptive)
        st_conns = min(self.conns, int(gov_start)) if gov_start else min(self.conns, 16)
        self.gov = Governor(self.conns, lo=min(8, self.conns),
                            start=st_conns, on=self.adaptive)
        self._gov_t = 0.0
        self._gov_b = 0
        self.state = out + ".qstate"
        size = task.size
        self.chunks = []
        pos = 0
        while pos < size:
            e = min(pos + self.part - 1, size - 1)
            self.chunks.append((pos, e))
            pos = e + 1
        self.ntok = max(1, min(self.ntok, len(self.chunks) or 1))

    # -- token 池
    def _refill(self):
        self._prime(self.ntok)

    def _prime(self, want):
        # 并发取链：串行取 48 条要 10~20 秒（每条约 0.3s 往返），
        # 起手这段空转在界面上就是「一直 0 MB/s」
        want = min(self.ntok, max(1, want))
        need = max(0, want - len(self.links))
        if not need:
            return
        got, lk = [], threading.Lock()

        def one():
            try:
                u = self._link()
            except Exception:
                return
            if u:
                with lk:
                    got.append(u)

        ts = [threading.Thread(target=one, daemon=True) for _ in range(need)]
        for t in ts:
            t.start()
        for t in ts:
            t.join(timeout=15)
        with self.link_lock:
            self.links.extend(got)

    def _next_link(self):
        # 池子不够就先补，但补链在锁外做：取链是一次 HTTP 往返(0.3~2s)，
        # 放在锁里会让 384 个线程全排在同一个锁上，文件一起头就卡成 0 MB/s。
        with self.link_lock:
            low = len(self.links) < max(2, self.ntok // 3)
        if low:
            self._refill()
        with self.link_lock:
            if self.links:
                return random.choice(self.links)
        return self._link()

    def _link(self):
        if self.link_fn is None:
            return self.api.download_url(self.task.fid)
        u = self.link_fn()
        if not u:
            raise QuarkError("直链取不到（分享可能已失效或超过免登录体积上限）")
        return u

    def _ck(self, refresh=False):
        """下载用的 Cookie 头，每次请求现取。

        直链鉴权靠游客会话里的 __sdid/__pugs，而 __pugs 是取直链那次响应才种下来的：
        在构造函数里算一次会拿到只有 __sdid 的半条 Cookie，一律 412。
        """
        try:
            if hasattr(self.api, "dl_cookie"):
                ck = self.api.dl_cookie()
                if ck:
                    self.cookie_header = ck
        except Exception:
            pass
        return self.cookie_header

    def _drop_link(self, url):
        if not url:
            return
        with self.link_lock:
            self.links = [x for x in self.links if x != url]

    # -- 状态
    def _load_state(self):
        if not self.resume or not os.path.exists(self.state):
            return
        try:
            with open(self.state, encoding="utf-8") as f:
                st = json.load(f)
            # 尺寸或切片变了，旧片号指向的是别的字节区间，一律作废。
            # 只比 len(done) 会立刻「100%」，而 file_ok 只抽头尾 3 段，中间的全零洞
            # 有可能整段漏检 —— 那就是尺寸正确、内容是空的假完成。
            if (int(st.get("size") or -1) != int(self.task.size)
                    or int(st.get("part") or -1) != int(self.part)):
                self.done, self.bytes_done = set(), 0
                return
            done = set(int(i) for i in st.get("done", []))
            self.done = {i for i in done if 0 <= i < len(self.chunks)}
            self.bytes_done = sum(b - a + 1 for i, (a, b) in
                                  enumerate(self.chunks) if i in self.done)
        except Exception:
            self.done, self.bytes_done = set(), 0

    def _save_state(self):
        # 先取快照再落盘：join 超时后仍有线程在 add()，直接 sorted(self.done)
        # 会抛 RuntimeError: Set changed size during iteration
        with self.lock:
            payload = {"done": sorted(self.done), "size": self.task.size,
                       "part": self.part}
        try:
            tmp = self.state + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(payload, f)
            os.replace(tmp, self.state)
        except (OSError, ValueError):
            pass

    # -- 线程
    def _worker(self, todo, allow_hedge=True):
        try:
            self._worker_inner(todo, allow_hedge=allow_hedge)
        except BaseException as e:
            with self.lock:
                self.fail.append((-1, "worker崩: %s: %s" % (type(e).__name__, str(e)[:60])))

    def _pick_hedge(self):
        """尾部慢片抢占：队列已空时，挑一条耗时过长的在飞分片做投机重试，消除 99% 卡顿。"""
        now = time.time()
        with self.lock:
            if not self.inflight:
                return None
            if self._chunk_times:
                avg = sum(self._chunk_times) / len(self._chunk_times)
            else:
                avg = 2.0
            thresh = max(3.5, avg * 2.5)
            cand = None
            oldest = 0.0
            for idx, t0 in list(self.inflight.items()):
                if idx in self.done or idx in self._hedged:
                    continue
                el = now - t0
                if el >= thresh and el > oldest:
                    a, b = self.chunks[idx]
                    if (b - a + 1) <= (4 << 20):
                        cand = (idx, a, b)
                        oldest = el
            if cand is not None:
                self._hedged.add(cand[0])
            return cand

    def _fetch_hedge(self, i, a, b, fd):
        """投机拉取单条慢片：先读进内存，赢了竞争再原子落盘，绝不与原线程重叠写。"""
        want = b - a + 1
        s = self.api.s
        url = None
        gated = False
        r = None
        try:
            if not self.gov.acquire(self.stop):
                return
            gated = True
            with self.lock:
                if i in self.done:
                    return
            url = self._next_link()
            h = {"Range": "bytes=%d-%d" % (a, b), "Accept-Encoding": "identity"}
            ck = self._ck()
            if ck:
                h["Cookie"] = ck
            r = s.get(url, headers=h, timeout=(10, 30), stream=True)
            if r.status_code not in (200, 206) or (r.status_code == 200 and a > 0):
                if r.status_code == 412:
                    self._drop_link(url)
                return
            buf = bytearray()
            pending_rx = 0
            last_flush = time.time()
            for blk in r.iter_content(1 << 17):
                if self.stop.is_set() or i in self.done:
                    return
                n = len(blk)
                if len(buf) + n > want:
                    blk = blk[:want - len(buf)]
                    n = len(blk)
                if n:
                    buf += blk
                    pending_rx += n
                    now = time.time()
                    if pending_rx >= (256 << 10) or now - last_flush >= 0.12:
                        with self.lock:
                            self.bytes_rx += pending_rx
                        pending_rx = 0
                        last_flush = now
                if len(buf) >= want:
                    break
            if pending_rx:
                with self.lock:
                    self.bytes_rx += pending_rx
            if len(buf) < want:
                return
            # 只有原线程还没完成时才落盘并记账，确保不重复计 bytes_done
            with self.lock:
                if i in self.done or self.stop.is_set():
                    return
                os.lseek(fd, a, os.SEEK_SET)
                mv = memoryview(buf)
                off = 0
                while off < want:
                    off += os.write(fd, mv[off:want])
                self.done.add(i)
                self.inflight.pop(i, None)
                self.bytes_done += want
            self.gov.reward()
        except Exception as e:
            if _is_conn_err(e):
                self.gov.penalty()
        finally:
            try:
                if r is not None:
                    r.close()
            except Exception:
                pass
            if gated:
                self.gov.release()
            with self.lock:
                self._hedged.discard(i)

    def _worker_inner(self, todo, allow_hedge=True):
        s = self.api.s
        # 每个 worker 复用同一个 fd：避免 384 线程每片都 os.open/os.close 触发
        # Windows 高频句柄开关与杀软实时扫描开销。各线程独立 fd，lseek 互不干扰。
        fd = -1
        try:
            while not self.stop.is_set():
                try:
                    i, a, b = todo.get_nowait()
                except queue.Empty:
                    if not allow_hedge:
                        return
                    h_item = self._pick_hedge()
                    if h_item is None:
                        return
                    if fd < 0:
                        fd = os.open(self.out, os.O_RDWR | getattr(os, "O_BINARY", 0))
                    self._fetch_hedge(h_item[0], h_item[1], h_item[2], fd)
                    continue

                want = b - a + 1
                with self.lock:
                    if i in self.done:
                        continue
                    self.inflight[i] = time.time()

                beaten = False
                for attempt in range(4):
                    part_got = 0      # 本次 attempt 已计入 bytes_done 的字节；失败要回滚
                    pending_rx = 0    # 本地攒批，降低全局锁竞争
                    if self.stop.is_set():
                        with self.lock:
                            self.inflight.pop(i, None)
                        return
                    if i in self.done:
                        beaten = True
                        break
                    url = None
                    gated = False
                    r = None
                    try:
                        if not self.gov.acquire(self.stop):
                            with self.lock:
                                self.inflight.pop(i, None)
                            return
                        gated = True
                        url = self._next_link()
                        h = {"Range": "bytes=%d-%d" % (a, b), "Accept-Encoding": "identity"}
                        ck = self._ck(refresh=(attempt > 0))
                        if ck:
                            # 直链要 __sdid/__pugs，缺了一律 412；Api.s 上那条空 Cookie
                            # 会盖掉 cookie jar，必须显式给值
                            h["Cookie"] = ck
                        r = s.get(url, headers=h, timeout=(15, 60), stream=True)
                        if r.status_code == 412:
                            # 412 = Cookie 里缺 __sdid/__pugs 或已过期：丢链接、重取 Cookie 再来
                            self._drop_link(url)
                            self._ck(refresh=True)
                            time.sleep(0.3 * (attempt + 1))
                            continue
                        if r.status_code not in (200, 206):
                            raise QuarkError("HTTP %d" % r.status_code)
                        if r.status_code == 200 and a > 0:
                            # 服务端忽略 Range 直接回整文件：照收会把文件开头写到偏移 a 处，
                            # 片计数全绿但内容错位（静默损坏），必须当失败处理
                            raise QuarkError("忽略 Range(200)")
                        # 边收边写：每线程复用自己的 fd 写自己那段偏移，不在内存里攒整片。
                        # 自定义档 32MB 片 × 384 连接如果都缓在内存里，峰值能到 12GB。
                        if fd < 0:
                            fd = os.open(self.out, os.O_RDWR | getattr(os, "O_BINARY", 0))
                        os.lseek(fd, a, os.SEEK_SET)
                        got = 0
                        t_read0 = time.time()
                        last_flush = t_read0
                        for blk in r.iter_content(1 << 17):
                            if self.stop.is_set():
                                raise QuarkError("已取消")
                            if i in self.done:
                                # 已被尾部投机线程抢先完成：立刻回退本次增量并释放连接
                                beaten = True
                                break
                            n = len(blk)
                            if got + n > want:
                                blk = blk[:want - got]
                                n = want - got
                            if n:
                                # os.write 可能只写一部分（大块 + 慢盘），必须写满
                                off = 0
                                while off < n:
                                    off += os.write(fd, blk[off:n])
                                got += n
                                pending_rx += n
                                now = time.time()
                                # 攒批更新全局计数（每 256KB 或 120ms 一次），兼顾界面流畅与低锁竞争
                                if pending_rx >= (256 << 10) or now - last_flush >= 0.12:
                                    with self.lock:
                                        self.bytes_done += pending_rx
                                        self.bytes_rx += pending_rx
                                    part_got += pending_rx
                                    pending_rx = 0
                                    last_flush = now
                                # 慢连接早退：持续 10 秒速率低于 10 KB/s 直接换链重试，不等 60 秒死等
                                el = now - t_read0
                                if el >= 10.0 and (got / el) < 10240 and self.bytes_rx > want:
                                    raise QuarkError("单连接过慢")
                            if got >= want:
                                break
                        if beaten:
                            if part_got:
                                with self.lock:
                                    self.bytes_done = max(0, self.bytes_done - part_got)
                            break
                        if pending_rx:
                            with self.lock:
                                self.bytes_done += pending_rx
                                self.bytes_rx += pending_rx
                            part_got += pending_rx
                            pending_rx = 0
                        if got < want:
                            raise QuarkError("短读 %d/%d" % (got, want))
                        dur = time.time() - t_read0
                        with self.lock:
                            if i in self.done:
                                # 刚写完瞬间被投机线程抢先登记：退回本地记账避免超 100%
                                self.bytes_done = max(0, self.bytes_done - part_got)
                            else:
                                self.done.add(i)
                                self._chunk_times.append(dur)
                                if len(self._chunk_times) > 32:
                                    del self._chunk_times[:-32]
                            self.inflight.pop(i, None)
                        self.gov.reward()
                        break
                    except Exception as e:
                        if part_got:
                            # 这一 attempt 收到的字节要退回，重试会从头重收这片
                            with self.lock:
                                self.bytes_done = max(0, self.bytes_done - part_got)
                        if "412" in str(e):
                            self._drop_link(url)
                        # 连接超时/被重置是并发过高的直接信号：立刻回撤闸门，
                        # 别让剩下的线程继续排队撞同一堵墙
                        if _is_conn_err(e):
                            self.gov.penalty()
                        time.sleep(0.4 * (attempt + 1))
                    finally:
                        # 提前 break 的响应体没读完，不显式关掉的话 urllib3 会把连接丢掉
                        # 而不是还回池子，等于每片都重新握手
                        try:
                            if r is not None:
                                r.close()
                        except Exception:
                            pass
                        r = None
                        if gated:
                            self.gov.release()
                else:
                    with self.lock:
                        self.inflight.pop(i, None)
                        if i not in self.done:
                            self.fail.append((i, "重试4次仍失败"))
        finally:
            if fd >= 0:
                try:
                    os.close(fd)
                except OSError:
                    pass

    # -- 主流程
    def run(self):
        self._load_state()
        try:
            # 大文件一次铺满 token 池：运行期每次补链是同步 HTTP 往返，会互相排队
            self._prime(4 if self.task.size <= (8 << 20) else self.ntok)
        except Exception:
            pass
        todo = queue.Queue()
        for i, (a, b) in enumerate(self.chunks):
            if i not in self.done:
                todo.put((i, a, b))
        pending = todo.qsize()
        if not pending:
            # 0 字节文件（或全部片已 done 但文件被手工删了）不能直接报成功 ——
            # 否则 UI 显示「完成 · 已校验」而磁盘上什么都没有
            return os.path.exists(self.out) and os.path.getsize(self.out) == self.task.size
        flags = os.O_RDWR | getattr(os, "O_BINARY", 0)
        if not os.path.exists(self.out):
            flags |= os.O_CREAT
        fd = os.open(self.out, flags, 0o666)
        try:
            os.ftruncate(fd, self.task.size)
        except OSError:
            pass
        os.close(fd)
        nthreads = min(self.conns, pending)
        threads = [threading.Thread(target=self._worker, args=(todo, True), daemon=True)
                   for _ in range(nthreads)]
        for t in threads:
            t.start()
        last_t, last_b = time.time(), 0
        while any(t.is_alive() for t in threads):
            if self.stop.wait(0.5):
                break
            # 每 ~1s 用实测吞吐喂一次闸门，让它自己收敛到最快的那一档
            now = time.time()
            if now - last_t >= 1.0:
                rate = (self.bytes_rx - last_b) / 1048576.0 / (now - last_t)
                last_t, last_b = now, self.bytes_rx
                self.gov.update(rate)
            if self.progress:
                try:
                    self.progress(self)
                except Exception:
                    pass
        # 注意：绝不能在重试前无条件 self.stop.set()！否则下面重试线程一启动看到
        # stop 已置位就会立刻退出，失败重试彻底沦为死代码。
        stopped_by_user = self.stop.is_set()
        for t in threads:
            t.join(timeout=3)
        self._save_state()
        if not stopped_by_user:
            for _ in range(4):
                if self.stop.is_set() or not self.fail:
                    break
                retry = [i for i, _ in self.fail if i >= 0 and i not in self.done]
                self.fail = [f for f in self.fail if f[0] < 0]
                if not retry:
                    break
                q2 = queue.Queue()
                for i in retry:
                    q2.put((i, self.chunks[i][0], self.chunks[i][1]))
                ts = [threading.Thread(target=self._worker, args=(q2, False), daemon=True)
                      for _ in range(min(self.conns, len(retry)))]
                for t in ts:
                    t.start()
                while any(t.is_alive() for t in ts):
                    if self.stop.wait(0.5):
                        break
                for t in ts:
                    t.join(timeout=3)
        self._save_state()
        ok = (not self.stop.is_set()) and (len(self.done) == len(self.chunks))
        if ok:
            bad = self.spot_check(3)
            if bad:
                self.fail.extend((-1, m) for m in bad)
                ok = False
        # 最后一道闸：整个文件做头/尾并发逐字节复核（复用已有直链，省一次取链 RTT）。
        # 服务端偶发返回 200+全零体（不是 4xx），片计数会全绿但内容是空的。
        if ok:
            with self.link_lock:
                reuse_url = self.links[0] if self.links else None
            # 分享直下必须把 link_fn 传进去：file_ok 内部要自己取直链再抽样比对，
            # 走 api.download_url 对分享 fid 一律 31001，会把好文件误判成坏文件
            ok2, why = file_ok(self.api, self.task.fid, self.out, self.task.size,
                               link_fn=self.link_fn, url=reuse_url)
            if not ok2:
                self.fail.append((-1, "整文件复核失败: " + why))
                ok = False
        if ok and os.path.exists(self.state):
            try:
                os.remove(self.state)
            except OSError:
                pass
        if not ok and not self.stop.is_set():
            try:
                with open(qbad_path(self.out), "w", encoding="utf-8") as f:
                    f.write("incomplete: %s\n" % "; ".join(str(m) for _, m in self.fail[:5]))
            except OSError:
                pass
        elif ok:
            for p in {self.out + ".qbad", qbad_path(self.out)}:
                if os.path.exists(p):
                    try:
                        os.remove(p)
                    except OSError:
                        pass
        return ok

    def spot_check(self, n=3):
        """并发随机抽 n 片（每片取前 16KB）与源逐字节比对；网络异常跳过，只有内容不符才报。"""
        if not self.chunks:
            return []
        idx = random.sample(range(len(self.chunks)), min(n, len(self.chunks)))
        bad = []
        lk = threading.Lock()

        def _check_one(i):
            a, b = self.chunks[i]
            want = min(b - a + 1, 16 << 10)
            b_end = a + want - 1
            got = None
            for _ in range(3):
                url = None
                try:
                    url = self._next_link()
                    hh = {"Range": "bytes=%d-%d" % (a, b_end),
                          "Accept-Encoding": "identity"}
                    ck = self._ck()
                    if ck:
                        hh["Cookie"] = ck
                    r = self.api.s.get(url, headers=hh, timeout=(15, 30))
                    if r.status_code in (200, 206):
                        got = r.content
                        break
                    if r.status_code == 412:
                        self._drop_link(url)
                except requests.RequestException:
                    continue
            if got is None:
                return
            try:
                with open(self.out, "rb") as f:
                    f.seek(a)
                    local = f.read(want)
            except OSError:
                return
            if len(local) < want or local[:want] != got[:want]:
                with lk:
                    bad.append("片 %d 内容不符" % i)

        ts = [threading.Thread(target=_check_one, args=(i,), daemon=True) for i in idx]
        for t in ts:
            t.start()
        for t in ts:
            t.join(timeout=35)
        return bad


def plan_conns(size, conns):
    """小文件不铺满连接，省调度开销。"""
    if size > 8 << 20:
        return conns
    return max(8, min(conns, max(16, size // (1 << 20))))
