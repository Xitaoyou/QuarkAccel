
# -*- coding: utf-8 -*-
"""importer.py —— 分享链接导入：解析 → 临时转存 → 下载 → 清理。

为什么必须转存：分享页里的 fid 属于「分享域」，拿它去 /file/download 换直链一律
报文件不存在（那是别人的文件）。Quark 没有给第三方「免转存直下」的口子，所以能做到
的最接近形态是：

    链接 → 解析 pwd_id/提取码 → 列分享内容 → 转存进自己的隐藏目录
         → 立刻下载到本地 → 下载完把转存件删掉（并清回收站）

对用户来说感受就是「贴链接直接下，网盘里不留东西」。程序内部多了一步转存，但它
只在下载期间存在几秒到几十秒，且默认下载完就删。

容量不够时按可用空间切批：一批转存 → 下完 → 删 → 下一批，任何时刻网盘占用不超过
一批的大小。单个文件就超过可用空间时直接报清楚需要多少、可用多少，不假装在排队。
"""
import os
import re
import threading
import time

from quark_core import QuarkError

# 免登录直下的体积上限：/file/share/download 对超过约 50MB 的文件一律报
# 23018 download file size limit。留一点余量，超过就老老实实转存。
DIRECT_MAX = 48 << 20

# 预览里最多列多少个文件给用户勾选。列表太长既看不清也没人一个个点，
# 超过这个数就截断并在界面上说明「只列了前 N 个」。
MAX_LIST = 500

TEMP_DIR = "QuarkAccel-临时转存"
MAX_SAVE = 100          # 一次 save 调用最多带多少个 fid（接口上限）
RESERVE = 64 << 20      # 给网盘自己的元数据留 64 MB，别把空间占到 0
BATCH_MAX = 8 << 30     # 一批最多 8 GB，避免一次转存太多、失败后重来成本高

LINK_RE = re.compile(r"/s/([0-9a-zA-Z_\-]+)")
PWD_RE = re.compile(r"(?:pwd|password|code)=([0-9a-zA-Z]{1,8})")


def friendly(err):
    """把接口错误码翻成人话 —— 用户看不懂 31001，但看得懂「先登录」。"""
    s = str(err or "")
    if "31001" in s or "require login" in s:
        return "需要先登录夸克账号：分享里的文件得先转存进你自己的网盘才拿得到直链"
    if "31004" in s or "token" in s and "invalid" in s:
        return "登录状态过期了，点右上角「登录」重新扫码"
    if "23018" in s or "size limit" in s:
        return "这个文件超过了免登录下载的体积上限，登录后再试"
    if "任务超时" in s:
        return "转存排队太久（夸克侧繁忙），过一会儿重新点一次"
    return s


def parse_link(text):
    """从分享链接/口令文本里抽 (pwd_id, passcode)。支持带 ?pwd=/&pwd= 的写法。"""
    s = (text or "").strip()
    m = LINK_RE.search(s)
    if not m:
        # 也接受直接贴 pwd_id
        m2 = re.fullmatch(r"([0-9a-zA-Z]{8,})", s)
        if not m2:
            raise QuarkError("这不是夸克分享链接（形如 https://pan.quark.cn/s/xxxx）")
        return m2.group(1), ""
    pwd_id = m.group(1)
    if pwd_id.lower().startswith("s"):
        pwd_id = pwd_id[1:]
    p = PWD_RE.search(s)
    return pwd_id, (p.group(1) if p else "")


def human(n):
    n = float(n or 0)
    for u in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or u == "TB":
            return ("%.1f %s" % (n, u)) if u != "B" else ("%d B" % int(n))
        n /= 1024.0


class Importer:
    """一条导入任务的状态机，供 /api/import_* 调用。"""

    def __init__(self, app):
        self.app = app
        self.lock = threading.Lock()
        self.th = None
        self.stop = threading.Event()
        self.pick = None      # 只下这些 fid（None=全都要）
        self.state = {
            "busy": False, "phase": "idle", "msg": "", "err": "",
            "link": "", "files": 0, "bytes": 0, "done_files": 0, "done_bytes": 0,
            "batch": 0, "batches": 0, "saved": 0, "deleted": 0,
            "delete_after": True, "skipped": [], "log": [],
        }

    # -- 状态
    def snap(self):
        with self.lock:
            d = dict(self.state)
            d["skipped"] = list(d.get("skipped") or [])[:20]
            d["log"] = list(d.get("log") or [])[-12:]
            return d

    def _set(self, **kw):
        with self.lock:
            self.state.update(kw)

    def _log(self, msg):
        with self.lock:
            self.state["log"] = (list(self.state.get("log") or []) + [msg])[-12:]
            self.state["msg"] = msg

    # -- 预览（不落盘，只列清单和分批方案）
    def _api(self):
        """拿到一个可用的 Api。

        免登录直下只需要会话里种下的游客 cookie，不需要登录态 —— 所以没登录时
        造一个空 cookie 的 Api 一样能取分享直链，这是「别人的分享也能直接下」的关键。
        """
        api = self.app.api()
        if api is not None:
            return api
        with self.lock:
            if not getattr(self, "_guest", None):
                from quark_core import Api
                self._guest = Api("")
            return self._guest

    def _split(self, files, logged=None):
        """按「走哪条通道更快」切两堆。

        实测（同一时段、同一个 5 MB PDF）：

            游客直下 1 连接 0.14 MB/s、4 连接 0.26 MB/s、16 连接 0.34 MB/s
            登录后走自己网盘的分片并发：47.7 MB/s

        游客通道是限速的（直链里带 sp=100，改这个参数签名就失效 412），再高的并发
        也提不上去。所以：**登录了就全部走转存 + 加速通道**（快两个数量级，代价是
        临时占一下网盘空间，下完自动删）；没登录才用免登录直下，而且只下 48 MB 以内
        的小文件，大文件转存不了（23018）。
        """
        if logged is None:
            logged = bool(getattr(self.app, "cookie", ""))
        if logged:
            return [], list(files)
        direct = [f for f in files if 0 < f["size"] <= DIRECT_MAX]
        small = {id(f) for f in direct}
        big = [f for f in files if id(f) not in small]
        return direct, big

    def preview(self, link):
        api = self._api()
        try:
            pwd_id, code = parse_link(link)
            stoken = api.share_token(pwd_id, code)
            items = api.share_list(pwd_id, stoken, "0")
            files, bytes_ = [], 0
            for it in items:
                if it["is_dir"]:
                    # 分享里的目录要递归展开：直接转存目录会连带整棵树，容量算不准
                    for en in self._walk_share(api, pwd_id, stoken, it):
                        files.append(en)
                        bytes_ += en["size"]
                else:
                    files.append({"fid": it["fid"], "token": it["token"],
                                  "name": it["name"], "size": it["size"],
                                  "rel": it["name"]})
                    bytes_ += it["size"]
            total, used = self._cap(api)
            free = max(0, total - used)
            # 不能用「cookie 字符串非空」判断登录：失效的 cookie 也非空，那样会把
            # 所有文件都推进转存通道，然后一律 31004。whoami 能拿到容量才算真登录。
            logged = bool(getattr(self.app, "cookie", "")) and bool(total)
            direct, big = self._split(files, logged)
            dset = {f["fid"] for f in direct}
            batches, too_big = self._plan(big, free, total)
            return {"ok": True, "pwd_id": pwd_id, "passcode": bool(code),
                    "files": len(files), "bytes": bytes_,
                    "cap_total": total, "cap_free": free, "logged": logged,
                    "direct_files": len(direct),
                    "direct_bytes": sum(f["size"] for f in direct),
                    "save_files": len(big),
                    "save_bytes": sum(f["size"] for f in big),
                    "batches": len(batches), "batch_sizes": [b["bytes"] for b in batches],
                    "too_big": too_big,
                    # list 带 fid：界面要按勾选结果回传要下哪些文件。
                    # 名字用 rel（目录里的文件带上级名），用户才分得清同名文件。
                    "list": [{"fid": f["fid"], "name": f.get("rel") or f["name"],
                              "size": f["size"], "direct": f["fid"] in dset}
                             for f in files[:MAX_LIST]],
                    "listed": min(len(files), MAX_LIST),
                    "truncated": len(files) > MAX_LIST}
        except QuarkError as e:
            return {"ok": False, "err": friendly(e)}
        except Exception as e:
            return {"ok": False, "err": "%s: %s" % (type(e).__name__, str(e)[:120])}

    def _walk_share(self, api, pwd_id, stoken, entry, prefix=""):
        """分享内的目录：递归列，名字带上前缀（下载时保留目录结构）。"""
        out = []
        rel = (prefix + "/" if prefix else "") + entry["name"]
        try:
            sub = api.share_list(pwd_id, stoken, entry["fid"])
        except Exception:
            return out
        for it in sub:
            if it["is_dir"]:
                out.extend(self._walk_share(api, pwd_id, stoken, it, rel))
            else:
                out.append({"fid": it["fid"], "token": it["token"],
                            "name": it["name"], "size": it["size"], "rel": rel + "/" + it["name"]})
        return out

    def _cap(self, api):
        try:
            u = api.whoami()
            return int(u.get("total") or 0), int(u.get("used") or 0)
        except Exception:
            return 0, 0

    def _plan(self, files, free, total=0):
        """按可用空间切批。返回 (batches, 无法完成的大文件列表)。

        容量接口没拿到（total == 0）时不能把每个文件都判成「装不下」——那会让一次
        网络抖动变成「什么都下不了」。这时只按 BATCH_MAX 分批，不做 too_big 判断。
        """
        known = bool(total)
        if known:
            budget = max(0, free - RESERVE)
            cap = max(1 << 20, min(budget, BATCH_MAX))
        else:
            cap = BATCH_MAX
        batches, cur, cur_bytes, too_big = [], [], 0, []
        for f in files:
            if known and f["size"] > cap:
                too_big.append({"name": f["name"], "size": f["size"]})
                continue
            if cur and (cur_bytes + f["size"] > cap or len(cur) >= MAX_SAVE):
                batches.append({"items": cur, "bytes": cur_bytes})
                cur, cur_bytes = [], 0
            cur.append(f)
            cur_bytes += f["size"]
        if cur:
            batches.append({"items": cur, "bytes": cur_bytes})
        return batches, too_big

    # -- 执行
    def start(self, link, delete_after=True, pick=None):
        api = self._api()
        if self.state.get("busy"):
            return {"ok": False, "err": "已经有一个导入任务在跑"}
        try:
            pwd_id, code = parse_link(link)
        except QuarkError as e:
            return {"ok": False, "err": str(e)}
        # pick：用户在清单里勾掉的之外的 fid。None 表示「全都要」，空列表表示
        # 「一个都没选」——这两种必须分开，空列表要直接拦下，不然会白跑一趟。
        only = None if pick is None else {str(x) for x in pick if x}
        if only is not None and not only:
            return {"ok": False, "err": "一个文件都没勾选"}
        self.stop.clear()
        self.pick = only
        self._set(busy=True, phase="listing", msg="正在读取分享内容…", err="",
                  link=link, delete_after=bool(delete_after), files=0, bytes=0,
                  done_files=0, done_bytes=0, batch=0, batches=0, saved=0,
                  deleted=0, skipped=[], log=[])
        self.th = threading.Thread(target=self._run, args=(api, pwd_id, code, bool(delete_after)),
                                   daemon=True)
        self.th.start()
        return {"ok": True, "started": True}

    def cancel(self):
        self.stop.set()
        self._log("已请求停止，当前这一批下完就收尾")
        return {"ok": True}

    def _summary(self, stopped=False):
        """收尾文案。失败的条数必须报出来。

        早先只说「完成：0 个文件，0 B」——链接明明有效、清单也列出来了，用户却只
        看到一个 0，完全不知道是磁盘写不进去还是直链取不到。现在把第一条失败原因
        直接带到这句话里。
        """
        done_n = self.state.get("done_files", 0)
        done_b = self.state.get("done_bytes", 0)
        bad = list(self.state.get("skipped") or [])
        if stopped:
            s = "已停止（已完成 %d 个文件" % done_n
        else:
            s = "完成：%d 个文件，%s" % (done_n, human(done_b))
        if bad:
            first = str((bad[0] or {}).get("name") or "")[:90]
            s += ("；%d 个失败，首个：%s" % (len(bad), first)) if not stopped else ""
        return s + "）" if stopped else s

    def _direct(self, api, pwd_id, stoken, files):
        """能免登录直下的那部分：不转存、不占网盘空间，直接进下载引擎。

        这是「贴别人的分享链接也能下」的主力路径 —— 小文件根本不用碰自己的网盘，
        也就不需要登录、不需要容量、不会在网盘里留下任何东西。
        """
        if not files:
            return 0, 0
        self._set(phase="downloading", msg="直接下载 %d 个文件（不转存、不占网盘空间）"
                  % len(files))
        ok_files = ok_bytes = 0
        ev = threading.Event()
        left = {"n": len(files)}
        lk = threading.Lock()

        def on_done(job):
            nonlocal ok_files, ok_bytes
            with lk:
                if job.state == "done":
                    ok_files += 1
                    ok_bytes += job.size
                else:
                    with self.lock:
                        self.state["skipped"] = (list(self.state.get("skipped") or [])
                                                 + [{"name": "%s：%s" % (job.name, job.note or job.state),
                                                     "size": 0}])
                left["n"] -= 1
                if left["n"] <= 0:
                    ev.set()

        payload = []
        for f in files:
            def mk_link(fid=f["fid"]):
                return api.share_download_url(pwd_id, stoken, fid)[0]

            payload.append({"fid": f["fid"], "name": f["name"],
                            "rel": f.get("rel") or f["name"], "size": f["size"],
                            "tag": "import", "on_done": on_done, "link_fn": mk_link,
                            "dl_api": api})
        eng = self.app.make_engine()
        views = eng.add(payload, self.app.outdir)
        if len(views) < left["n"]:
            left["n"] = len(views)
            if left["n"] <= 0:
                ev.set()
        jids = [v.get("id") for v in views if v.get("id")]
        while not ev.wait(0.5):
            if self.stop.is_set():
                for jid in jids:
                    try:
                        eng.cancel(jid)
                    except Exception:
                        pass
                ev.wait(3.0)
                break
        return ok_files, ok_bytes

    def _run(self, api, pwd_id, code, delete_after):
        temp_fid = ""
        saved_all = []
        gone = set()        # 已经删掉的 fid，收尾时不要重复调用删除
        try:
            stoken = api.share_token(pwd_id, code)
            items = api.share_list(pwd_id, stoken, "0")
            files = []
            for it in items:
                if it["is_dir"]:
                    for en in self._walk_share(api, pwd_id, stoken, it):
                        files.append(en)
                else:
                    files.append({"fid": it["fid"], "token": it["token"],
                                  "name": it["name"], "size": it["size"],
                                  "rel": it["name"]})
            if not files:
                self._set(busy=False, phase="done", msg="分享里没有文件")
                return
            # 只留下用户勾选的。挑完还是空的就别往下跑了 —— 那样会显示「完成 0 个文件」，
            # 用户会以为程序坏了，其实是自己没勾。
            if getattr(self, "pick", None) is not None:
                files = [f for f in files if f["fid"] in self.pick]
                if not files:
                    self._set(busy=False, phase="done",
                              msg="没有匹配到勾选的文件（分享内容可能变了，重新点一次「先看看里面有什么」）")
                    return
            total, used = self._cap(api)
            free = max(0, total - used)
            direct, big = self._split(files, bool(getattr(self.app, "cookie", "")) and bool(total))
            batches, too_big = self._plan(big, free, total)
            self._set(files=len(files), bytes=sum(f["size"] for f in files),
                      batches=len(batches), skipped=too_big,
                      phase="downloading" if direct else "saving",
                      msg=("%d 个直接下，%d 个要转存" % (len(direct), len(big)))
                          if direct and big else
                          ("%d 个文件直接下载（不转存）" % len(direct)) if direct else
                          ("分 %d 批转存" % len(batches)))
            if not batches and not direct:
                self._set(busy=False,
                          err=("网盘可用空间 %s，装不下这些文件；登录后空间不够就要先清一清"
                               % human(free)) if total else
                              "这些文件都超过了免登录直下的体积上限，需要登录后再下")
                return
            # 先下能直下的：不占空间、不等转存排队，用户能立刻看到速度
            if direct:
                n_ok, b_ok = self._direct(api, pwd_id, stoken, direct)
                with self.lock:
                    self.state["done_files"] = self.state.get("done_files", 0) + n_ok
                    self.state["done_bytes"] = self.state.get("done_bytes", 0) + b_ok
            if not batches or self.stop.is_set():
                # 没有需要转存的了（或者用户在直下阶段就按了停止）：到这儿就该收尾
                self._set(busy=False, phase="done",
                          msg=self._summary(stopped=self.stop.is_set()))
                return
            temp_fid = api.ensure_dir(TEMP_DIR)
            for bi, b in enumerate(batches):
                if self.stop.is_set():
                    break
                self._set(batch=bi + 1, phase="saving",
                          msg="第 %d/%d 批：转存 %d 个文件（%s）"
                              % (bi + 1, len(batches), len(b["items"]), human(b["bytes"])))
                before = {x["fid"] for x in api.list_raw(temp_fid)}
                task_id = api.share_save(pwd_id, stoken, b["items"], temp_fid)
                api.task_wait(task_id, timeout=300)
                after = [x for x in api.list_raw(temp_fid) if x["fid"] not in before]
                if not after:
                    raise QuarkError("转存后没在临时目录里看到文件")
                for x in after:
                    saved_all.append(x["fid"])
                self._set(saved=len(saved_all))
                # 交给下载引擎：按临时目录里的真实 fid 下（转存后服务端可能改名）
                events = {"left": len(after), "err": []}
                ev = threading.Event()

                def on_done(job, _ev=events, _e=ev):
                    _ev["left"] -= 1
                    if job.state == "done":
                        with self.lock:
                            self.state["done_files"] = self.state.get("done_files", 0) + 1
                            self.state["done_bytes"] = self.state.get("done_bytes", 0) + job.size
                    else:
                        _ev["err"].append("%s：%s" % (job.name, job.note or job.state))
                    if _ev["left"] <= 0:
                        _e.set()

                # 转存后服务端会按自己的规则命名（重名会加后缀），所以按名字回查原始
                # rel 才能保住分享里的目录层级；查不到就用自己的名字，不至于下不了
                want = {}
                for it in b["items"]:
                    want[os.path.basename(it.get("rel") or it["name"])] = it.get("rel") or it["name"]
                payload = [{"fid": x["fid"], "name": x["name"],
                            "rel": want.get(x["name"], x["name"]),
                            "size": x["size"], "tag": "import", "on_done": on_done}
                           for x in after]
                self._set(phase="downloading", msg="第 %d/%d 批：开始下载 %d 个文件"
                          % (bi + 1, len(batches), len(payload)))
                eng = self.app.make_engine()
                views = eng.add(payload, self.app.outdir)
                # 引擎会跳过「同 fid 已在队列里」的条目；被跳过的不可能回调 on_done，
                # 不在这里补账，等待就会永远等下去（用户看到「下载中」卡死）
                if len(views) < events["left"]:
                    events["left"] = len(views)
                    if events["left"] <= 0:
                        ev.set()
                jids = [v.get("id") for v in views if v.get("id")]
                # 等这一批下完（每 0.5 秒看一次，进度靠引擎自己的推送）
                while not ev.wait(0.5):
                    if self.stop.is_set():
                        # 停止时要把还在跑的这批任务一起停掉，否则网盘里的转存件
                        # 会在下载线程还在写盘的时候被删掉；给它们 3 秒收尾再走
                        for jid in jids:
                            try:
                                eng.cancel(jid)
                            except Exception:
                                pass
                        ev.wait(3.0)
                        break
                if delete_after and after:
                    self._set(phase="cleanup", msg="第 %d/%d 批：删除网盘里的临时文件"
                              % (bi + 1, len(batches)))
                    r = api.del_files([x["fid"] for x in after])
                    gone.update(x["fid"] for x in after)
                    with self.lock:
                        self.state["deleted"] = self.state.get("deleted", 0) + int(r.get("deleted") or 0)
                if events["err"]:
                    with self.lock:
                        self.state["skipped"] = (list(self.state.get("skipped") or [])
                                                 + [{"name": e, "size": 0} for e in events["err"][:5]])
            self._set(phase="done", busy=False,
                      msg=self._summary(stopped=self.stop.is_set()))
        except Exception as e:
            self._set(busy=False, phase="error",
                      err=friendly("%s: %s" % (type(e).__name__, str(e)[:150])),
                      msg="导入失败")
        finally:
            # 不管成功失败，临时目录都清干净（用户不该看到「夸克网盘里多了个奇怪目录」）
            try:
                if temp_fid:
                    left = [x["fid"] for x in api.list_raw(temp_fid)
                            if not x["is_dir"] and x["fid"] not in gone]
                    if left and delete_after:
                        api.del_files(left)
                        with self.lock:
                            self.state["deleted"] = self.state.get("deleted", 0) + len(left)
                    if delete_after:
                        try:
                            api.del_files([temp_fid])
                        except Exception:
                            pass
            except Exception:
                pass
