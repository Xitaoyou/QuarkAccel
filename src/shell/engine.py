# -*- coding: utf-8 -*-
"""engine.py —— 任务队列 + 分片并发下载（复用 quark_core.Downloader）。

这里负责三件事：
  1. 任务状态机：排队 / 传输中 / 已暂停 / 完成 / 失败 / 已取消
  2. 实时速度：按 3 秒滑动窗口算字节增量，而不是「整片完成」的跳变 ——
     1 MB 片 + 384 连接时，整片计数会让速度每秒跳两次，看着和实际差很远
  3. 下载记录：完成/失败/取消的条目落盘到 downloads.json，重启后还在
"""
import collections
import io
import json
import os
import random
import threading
import time
import uuid

from quark_core import Api, Downloader, Task, file_ok, plan_conns, resolve_path

import profiles

HIST_MAX = 300          # 下载记录最多留多少条
HIST_VIEW = 80          # 一次给界面多少条
SPEED_WIN = 2.0         # 速度窗口（秒）：3 秒太钝，档位一变要等三秒界面才动
SMALL_FILE_MAX = 16 << 20   # <= 16 MB 视为小文件，支持多文件并发以消除切换空窗
SMALL_CONCUR = 3            # 小文件最多同时下几个


class Job:
    __slots__ = ("id", "fid", "name", "size", "rel", "out", "state", "got",
                 "speed", "note", "dl", "t0", "t_last", "bytes_last",
                 "paused", "samples", "t_done", "err", "conns_live", "t_mark",
                 "tag", "on_done", "link_fn", "md5", "dl_api")

    def __init__(self, fid, name, size, rel="", tag="", on_done=None):
        self.id = uuid.uuid4().hex[:12]
        self.fid = fid
        self.name = name
        self.size = int(size or 0)
        self.rel = rel or name
        self.out = ""
        self.state = "queued"
        self.got = 0
        self.speed = 0.0
        self.note = ""
        self.dl = None
        self.t0 = time.time()
        self.t_last = time.time()
        self.bytes_last = 0
        self.paused = False
        self.samples = collections.deque(maxlen=120)
        self.t_done = 0.0
        self.err = ""
        self.conns_live = 0     # 自适应并发当前实际在飞的连接数（界面要显示，别让用户以为被限速）
        self.t_mark = 0.0       # 最后一次采样时刻：停了就不再显示旧速度
        # 导入链接产生的任务要带批次号（tag）并回调（on_done），下载器本体不需要知道
        # 这些：它只管把文件搬下来，删除转存件、推进下一批由导入调度器做
        self.tag = tag
        self.on_done = on_done
        # 分享域直下：这个文件不属于自己网盘，取链要走 /file/share/download，
        # 而不是 /file/download。link_fn 由导入器注入，下载器本体不知情
        self.link_fn = None
        self.md5 = ""
        # 直链鉴权靠「发起取链的那个会话」里种下的 __sdid/__pugs：分享直下必须用
        # 导入器那个会话去下，用引擎自己新建的空会话一律 412
        self.dl_api = None

    def mark(self, got):
        """记一次字节数采样，按滑动窗口算瞬时速度。"""
        now = time.time()
        self.t_mark = now
        self.samples.append((now, got))
        while len(self.samples) > 2 and now - self.samples[0][0] > SPEED_WIN:
            self.samples.popleft()
        if len(self.samples) >= 2:
            t0, b0 = self.samples[0]
            dt = now - t0
            if dt >= 0.2:
                self.speed = max(0.0, (got - b0) / 1048576.0 / dt)

    def view(self):
        el = max(0.0, (self.t_done or time.time()) - self.t0)
        # 采样停了（下完/暂停/卡住）就别再显示最后那个数：否则任务已经结束，
        # 界面上还挂着 4.38 MB/s，看起来像速度虚报
        # 只有 running 才有速度：暂停/完成/取消的任务挂着上一秒的数字，
        # 会被读成「还在偷偷跑」
        spd = self.speed if self.state == "running" else 0.0
        if self.t_mark and time.time() - self.t_mark > 1.2:
            spd = 0.0
        left = ""
        if self.state == "running" and spd > 1e-3 and self.size:
            sec = (self.size - self.got) / (spd * 1048576.0)
            left = "%d 分 %d 秒" % (int(sec // 60), int(sec % 60)) if sec >= 60 else "%d 秒" % int(sec)
        return {
            "id": self.id, "fid": self.fid, "name": self.name, "size": self.size,
            "state": self.state, "got": self.got, "speed": round(spd, 2),
            "note": self.note, "left": left, "elapsed": int(el),
            "path": self.out, "paused": bool(self.paused),
            "pct": (self.got * 100.0 / self.size) if self.size else 0.0,
            "conns": self.conns_live,
        }


class Engine:
    """单线程拉队列，逐文件下载；进度按 4Hz 推给界面。"""

    def __init__(self, cookie, outdir, tier="turbo", conns=None, part_mb=None, tokens=None,
                 on_push=None, on_log=None, demo=False, hist_path=""):
        self.demo = bool(demo)
        self.cookie = cookie
        self.outdir = outdir
        self.tier = tier
        self.conns, self.part_mb, self.tokens = profiles.resolve(tier, conns, part_mb, tokens)
        self.on_push = on_push
        self.on_log = on_log
        self.jobs = {}
        self.order = []
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.current = None
        self.paused = False
        self.worker = None
        self._running = False
        self._api = None
        self._push_last = 0.0
        self._auto_best = None      # 记住自适应模式收敛出的最佳并发，供下一个文件直接起跑
        self.hist_path = hist_path or ""
        self.hist = []
        self._load_hist()

    # -- 下载记录
    def _load_hist(self):
        if not self.hist_path:
            return
        try:
            with io.open(self.hist_path, encoding="utf-8") as f:
                d = json.load(f)
            if isinstance(d, list):
                self.hist = [x for x in d if isinstance(x, dict)][-HIST_MAX:]
        except Exception:
            self.hist = []

    def _save_hist(self):
        if not self.hist_path:
            return
        try:
            d = os.path.dirname(self.hist_path)
            if d:
                os.makedirs(d, exist_ok=True)
            tmp = self.hist_path + ".tmp"
            with io.open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.hist[-HIST_MAX:], f, ensure_ascii=False)
            os.replace(tmp, self.hist_path)
        except Exception:
            pass

    def _finish(self, job):
        """任务终态时写一条下载记录（幂等：t_done 已置就跳过）。"""
        if job.t_done:
            return
        job.t_done = time.time()
        ent = {"id": job.id, "name": job.name, "size": job.size, "path": job.out,
               "state": job.state, "note": job.note, "ts": int(job.t_done),
               "secs": int(job.t_done - job.t0)}
        with self.lock:
            self.hist = [x for x in self.hist if x.get("id") != job.id]
            self.hist.append(ent)
            del self.hist[:-HIST_MAX]
        self._save_hist()

    # -- api
    @property
    def api(self):
        if self._api is None:
            self._api = Api(self.cookie)
        return self._api

    def log(self, msg):
        if self.on_log:
            try:
                self.on_log(str(msg))
            except Exception:
                pass

    # -- 任务
    def add(self, items, outdir=None):
        if outdir:
            self.outdir = outdir
        added = []
        with self.lock:
            for it in items:
                fid = str(it.get("fid") or "").strip()
                if not fid:
                    continue
                if any(j.fid == fid and j.state in ("queued", "running", "paused")
                       for j in self.jobs.values()):
                    continue
                j = Job(fid, str(it.get("name") or fid), it.get("size") or 0,
                        str(it.get("rel") or it.get("name") or fid),
                        tag=str(it.get("tag") or ""),
                        on_done=it.get("on_done"))
                j.link_fn = it.get("link_fn")
                j.md5 = str(it.get("md5") or "")
                j.dl_api = it.get("dl_api")
                base = self.outdir or os.path.expanduser("~\\Downloads\\QuarkDL")
                j.out = os.path.join(base, _safe(j.rel))
                self.jobs[j.id] = j
                self.order.append(j.id)
                added.append(j)
        if added:
            self._kick()
        self.push(force=True)
        return [j.view() for j in added]

    def apply_profile(self, tier, conns=None, part_mb=None, tokens=None):
        self.tier = tier
        self.conns, self.part_mb, self.tokens = profiles.resolve(tier, conns, part_mb, tokens)
        return {"tier": self.tier, "conns": self.conns,
                "part_mb": self.part_mb, "tokens": self.tokens}

    def set_outdir(self, path):
        self.outdir = path
        return path

    # -- 暂停 / 继续 / 取消
    def pause(self, jid=None):
        """暂停。带 jid 只暂停那一个，不带就整体暂停（队列保留，续传靠 .qstate）。"""
        ok = False
        with self.lock:
            if jid:
                j = self.jobs.get(jid)
                if not j:
                    return False
                if j.state == "running":
                    # 状态立刻改成 paused：只置 paused 标记的话，要等 worker 下一次
                    # 循环（演示引擎 0.12 秒、真机要等当前分片收完）才生效，这段时间
                    # 界面上还是「传输中」挂着 20 MB/s，看着像暂停键没反应
                    j.paused = True
                    j.state = "paused"
                    j.note = "已暂停"
                    j.speed = 0.0
                    if j.dl:
                        j.dl.stop.set()
                    ok = True
                elif j.state == "queued":
                    j.paused = True
                    j.state = "paused"
                    j.note = "已暂停"
                    ok = True
            else:
                self.paused = True
                for j in self.jobs.values():
                    if j.state == "queued":
                        j.state = "paused"
                        j.note = "已暂停"
                        ok = True
                    elif j.state == "running":
                        j.paused = True
                        j.state = "paused"
                        j.note = "已暂停"
                        j.speed = 0.0
                        if j.dl:
                            j.dl.stop.set()
                        ok = True
        self.push(force=True)
        return ok

    def resume(self, jid=None):
        """继续：把 paused 的任务重新排队，然后确保 worker 在跑。"""
        with self.lock:
            if jid:
                j = self.jobs.get(jid)
                if not j:
                    return False
                j.paused = False
                if j.state == "paused":
                    j.state = "queued"
                    j.note = ""
            else:
                self.paused = False
                for j in self.jobs.values():
                    if j.state == "paused":
                        j.paused = False
                        j.state = "queued"
                        j.note = ""
        self._kick()
        self.push(force=True)
        return True

    def retry(self, jid):
        """重试：清状态文件（避免沿用坏片），重新排队。"""
        with self.lock:
            j = self.jobs.get(jid)
            if not j:
                return False
            if j.state == "running":
                # 正在写盘的任务不能就地重置：旧 Downloader 会继续写、结束时再
                # _save_state() 把 .qstate 复活，用户看到进度来回跳
                return False
            for suf in (".qstate", ".qbad"):
                try:
                    p = j.out + suf
                    if os.path.exists(p):
                        os.remove(p)
                except OSError:
                    pass
            j.state = "queued"
            j.got = 0
            j.speed = 0.0
            j.note = ""
            j.paused = False
            j.t_done = 0.0
            j.samples.clear()
            self.hist = [x for x in self.hist if x.get("id") != j.id]
        self._kick()
        self.push(force=True)
        return True

    def cancel(self, jid):
        # _finish() 内部要拿 self.lock，这里不能在持锁时调它（Lock 不可重入，
        # 会直接把自己锁死 —— 表现就是界面上点「取消」永久转圈）
        done_now = False
        with self.lock:
            j = self.jobs.get(jid)
            if not j:
                return False
            if j.state == "running":
                j.paused = False
                j.state = "canceled"
                j.note = "已取消"
                if j.dl:
                    j.dl.stop.set()
            elif j.state in ("queued", "paused"):
                j.state = "canceled"
                j.note = "已取消"
                # 暂停中的任务仍然占着一个在跑的 Downloader：不叫停它的话，
                # 界面已经显示「已取消」而磁盘还在写，.qstate 回头又被保存一次
                j.paused = False
                if j.dl:
                    j.dl.stop.set()
                done_now = True
            else:
                return False
        if done_now:
            self._finish(j)
        self._save_hist()
        self.push(force=True)
        return True

    def remove(self, jid):
        """从列表里彻底删掉一条（含下载记录）。"""
        with self.lock:
            j = self.jobs.get(jid)
            if j and j.state == "running":
                return False
            self.jobs.pop(jid, None)
            if jid in self.order:
                self.order.remove(jid)
            self.hist = [x for x in self.hist if x.get("id") != jid]
        self._save_hist()
        self.push(force=True)
        return True

    def clear_done(self):
        """清空列表里的已结束条目（下载记录保留）。"""
        with self.lock:
            for jid in list(self.order):
                j = self.jobs.get(jid)
                if j and j.state in ("done", "failed", "canceled"):
                    self.order.remove(jid)
                    self.jobs.pop(jid, None)
        self.push(force=True)

    def delete_files(self, jid):
        """删掉磁盘上的成品文件，再从任务列表和下载记录里移除这条。

        只认「这个程序自己下过的路径」：先按 jid 从 jobs/hist 里取 path，
        绝不接受界面直接传路径过来删（一个手滑的绝对路径就是删别人的文件）。
        顺带清理 .qstate / .qbad 两个伴生文件，不然重下会接上半截进度。
        """
        jid = str(jid or "")
        with self.lock:
            j = self.jobs.get(jid)
            if j is not None and j.state == "running":
                return {"ok": False, "err": "这个任务还在跑，先取消再删"}
            if j is not None:
                path, size = j.out, j.size
            else:
                ent = next((x for x in self.hist if str(x.get("id")) == jid), None)
                if not ent:
                    return {"ok": False, "err": "找不到这条记录"}
                path, size = ent.get("path") or "", int(ent.get("size") or 0)
        if not path:
            return {"ok": False, "err": "这条记录没有文件路径"}
        freed, gone = 0, False
        for p in (path, path + ".qstate", path + ".qbad"):
            try:
                rp = resolve_path(p)
            except Exception:
                rp = p
            try:
                if os.path.exists(rp):
                    freed += os.path.getsize(rp)
                    os.remove(rp)
                    gone = True
            except OSError as e:
                return {"ok": False, "err": "删不掉 %s：%s"
                        % (os.path.basename(rp), str(e)[:60])}
        with self.lock:
            self.jobs.pop(jid, None)
            if jid in self.order:
                self.order.remove(jid)
            self.hist = [x for x in self.hist if str(x.get("id")) != jid]
        self._save_hist()
        self.push(force=True)
        return {"ok": True, "freed": freed, "size": size,
                "missing": not gone, "name": os.path.basename(path)}

    def clear_hist(self):
        with self.lock:
            self.hist = []
        self._save_hist()
        self.push(force=True)

    def snapshot(self):
        with self.lock:
            items = [self.jobs[i].view() for i in self.order if i in self.jobs]
            hist = list(self.hist)[-HIST_VIEW:][::-1]
        tot = sum(i["size"] for i in items) or 1
        got = sum(i["got"] for i in items)
        spd = sum(i["speed"] for i in items if i["state"] == "running")
        busy = any(i["state"] == "running" for i in items)
        return {
            "items": items,
            "hist": hist,
            "total": tot, "got": got,
            "pct": got * 100.0 / tot,
            "speed": round(spd, 2),
            "running": busy,
            "paused": bool(self.paused),
            "profile": {"tier": self.tier, "conns": self.conns,
                        "part_mb": self.part_mb, "tokens": self.tokens,
                        "adaptive": profiles.adaptive(self.tier)},
            "outdir": self.outdir,
        }

    # -- 推送
    def push(self, force=False):
        now = time.time()
        if not force and now - self._push_last < 0.22:
            return
        self._push_last = now
        if self.on_push:
            try:
                self.on_push(self.snapshot())
            except Exception:
                pass

    # -- 主循环
    def _exec_job(self, job, conn_budget=None):
        """执行单个任务并统一收尾（写记录 + 触发导入回调）。"""
        try:
            self._run_one(job, conn_budget=conn_budget)
        except Exception as e:
            # 没这层兜底，run_one 前半段抛异常会打死 worker，Job 永久停在
            # running（UI 一直显示「传输中」，清空也清不掉）
            self.log("任务异常 %s: %s" % (job.name[:24],
                                        ("%s: %s" % (type(e).__name__, str(e)))[:90]))
            with self.lock:
                if job.state == "running":
                    job.state = "failed"
                    job.note = ("%s: %s" % (type(e).__name__, str(e)))[:80]
            self._finish(job)
            self.push(force=True)
        # 演示分支和 _run_one 的暂停/取消分支都不会自己写记录，
        # 这里统一补一次（_finish 有 t_done 幂等）
        if job.state in ("done", "failed", "canceled"):
            self._finish(job)
            # 导入链接的任务在这里回调调度器（删除转存件 / 推进下一批）。
            # 回调只触发一次：清掉引用，重试时不会再跑一遍删除
            hook, job.on_done = job.on_done, None
            if hook:
                try:
                    hook(job)
                except Exception as e:
                    self.log("导入回调失败 %s: %s" % (job.name[:24], str(e)[:80]))

    def _loop(self):
        # _running 是「队列消费中」的唯一权威：is_alive() 不能用来判断能不能收新任务
        # （最后一个任务结束时线程还活着，这期间 add() 会以为有人在干活而不启新线程，
        #  结果任务永远停在排队）。
        try:
            while not self.stop.is_set():
                if self.paused:
                    time.sleep(0.3)
                    continue
                batch = []
                with self.lock:
                    for jid in self.order:
                        j = self.jobs.get(jid)
                        if not j or j.state != "queued":
                            continue
                        if not batch:
                            batch.append(j)
                            # 大文件独享满带宽；小文件（<= 16 MB）凑最多 3 个并发下
                            if self.demo or j.size > SMALL_FILE_MAX:
                                break
                        else:
                            if j.size <= SMALL_FILE_MAX and len(batch) < SMALL_CONCUR:
                                batch.append(j)
                            if len(batch) >= SMALL_CONCUR:
                                break
                if not batch:
                    return
                if len(batch) == 1:
                    self._exec_job(batch[0])
                else:
                    # 小文件并发：平分总连接数，避免超出服务端并发甜点
                    per = max(8, self.conns // len(batch))
                    ts = [threading.Thread(target=self._exec_job, args=(j, per), daemon=True)
                          for j in batch]
                    for t in ts:
                        t.start()
                    for t in ts:
                        t.join()
        finally:
            with self.lock:
                self._running = False
                pend = any(j.state == "queued" for j in self.jobs.values())
            self.push(force=True)
            # 退出瞬间可能刚好有新任务进队（add() 看到 _running=True 就没起线程），
            # 这里补一次，避免任务永远停在「排队」
            if pend and not self.stop.is_set() and not self.paused:
                self._kick()

    def _kick(self):
        """确保有 worker 在消费队列（锁内判 _running，锁外起线程）。"""
        need = False
        with self.lock:
            if not self._running:
                self._running = True
                need = True
        if need:
            self.stop.clear()
            self.worker = threading.Thread(target=self._loop, daemon=True)
            self.worker.start()
        return need

    def _run_demo(self, job):
        """演示模式：不联网，按 ~2.1 MB/s 假跑，用来验收界面动效。"""
        job.state = "running"
        job.t0 = time.time()
        job.samples.clear()
        # 演示速度按「真实速率」模拟：每 tick 走 速率×时长，而不是把文件均分成 220 步
        # （大文件均分会让界面显示 37 MB/s、50 MB/s 这种根本不可能的数字）
        #
        # 早期写死 2.1 MB/s，结果被当成「加速器只有 2 MB/s」的证据。真机自适应收敛后
        # 是 20~24 MB/s（本地塌陷模型实测 avg 9.8~10.7、峰值 20.3），演示必须贴近它。
        top = 22.0
        # 演示里也要诚实地显示并发：固定档就是设的那个数（小文件按 plan_conns 收敛），
        # 只有自适应档才是「起手 16、之后往上爬」
        auto = profiles.adaptive(self.tier)
        job.conns_live = min(self.conns, 16) if auto else plan_conns(job.size, self.conns)
        self.push(force=True)
        while job.got < job.size:
            if self.stop.is_set() or job.state == "canceled":
                job.note = "已取消"
                return
            if job.paused:
                job.state = "paused"
                job.note = "已暂停"
                return
            if self.paused:
                job.state = "queued"
                job.note = "已暂停"
                return
            time.sleep(0.12)
            # 起速 3 秒爬到 22 MB/s，之后小幅抖动：和真机的慢启动曲线一致
            ramp = min(1.0, (time.time() - job.t0) / 3.0)
            rate = top * (0.35 + 0.65 * ramp) * (0.92 + 0.16 * random.random())
            job.got = min(job.size, job.got + int(rate * 1048576 * 0.12))
            job.conns_live = (min(self.conns, 16 if ramp < 0.5 else 24) if auto
                              else plan_conns(job.size, self.conns))
            job.mark(job.got)
            self.push()
        job.got, job.speed = job.size, 0.0
        job.state = "done"
        job.note = "完成 · 已校验"
        self._finish(job)
        self.push(force=True)

    def _run_one(self, job, conn_budget=None):
        with self.lock:
            if job.state in ("canceled", "paused") or job.paused or self.paused:
                return
            job.state = "running"
            job.paused = False
        if self.demo:
            return self._run_demo(job)
        job.t0 = time.time()
        job.samples.clear()
        job.speed = 0.0
        job.bytes_last = 0
        self.current = job
        self.push(force=True)
        base = self.outdir or os.path.expanduser("~\\Downloads\\QuarkDL")
        out = os.path.join(base, _safe(job.rel))
        job.out = out
        have = resolve_path(out)
        try:
            if have and os.path.exists(have) and os.path.getsize(have) == job.size:
                ok, why = file_ok(job.dl_api or self.api, job.fid, have, job.size,
                                  link_fn=job.link_fn)
                if ok:
                    job.state = "done"
                    job.got = job.size
                    job.note = "已存在（校验通过）"
                    self._finish(job)
                    self.push(force=True)
                    return
                job.note = "已有文件校验不过，重下"
        except Exception as e:
            job.note = str(e)[:60]
        d = os.path.dirname(out)
        if d:
            try:
                os.makedirs(d, exist_ok=True)
            except OSError as e:
                # 目录建不出来（只读盘 / 策略限制 / 路径被占）时不能「吞掉继续」：
                # 后面 os.open 会抛 FileNotFoundError，用户只看到一条不知所云的
                # 失败记录；这里直接把原因写进任务说明并结束这一条。
                job.state = "failed"
                job.note = ("建目录失败 %s: %s"
                            % (os.path.basename(d) or d, str(e)[:60]))
                self.log(job.note)
                self._finish(job)
                self.push(force=True)
                return

        job_api = job.dl_api or self.api

        def on_prog(dl):
            # 进度看 bytes_done（回滚会让它回退，这是对的）
            # 速度看 bytes_rx（只增不减，重试不砸曲线）
            job.got = dl.bytes_done
            job.mark(getattr(dl, "bytes_rx", dl.bytes_done))
            g = getattr(dl, "gov", None)
            if g is not None:
                job.conns_live = g.limit
            self.push()

        max_c = min(self.conns, conn_budget) if conn_budget else self.conns
        n = plan_conns(job.size, max_c)
        is_auto = profiles.adaptive(self.tier)
        task = Task(rel=job.rel, fid=job.fid, size=job.size)
        dl = Downloader(job_api, task, out, conns=n, part_mb=self.part_mb,
                        tokens=self.tokens, resume=True, progress=on_prog,
                        link_fn=job.link_fn,
                        adaptive=is_auto,
                        gov_start=self._auto_best if is_auto else None)
        job.dl = dl
        ok = False
        try:
            ok = dl.run()
        except Exception as e:
            job.note = "%s: %s" % (type(e).__name__, str(e)[:70])
        if is_auto and ok and dl.gov.best_rate > 0:
            self._auto_best = dl.gov.best_limit
        job.speed = 0.0
        if job.state == "canceled":
            job.note = "已取消"
        elif job.paused:
            job.state = "paused"
            job.note = "已暂停"
        elif self.paused or (not ok and job.dl and job.dl.stop.is_set()):
            job.state = "queued"
            job.note = "已暂停"
        elif ok:
            job.state = "done"
            job.got = job.size
            job.note = "完成 · 已校验"
        else:
            job.state = "failed"
            if not job.note:
                job.note = "失败：%s" % ("; ".join(str(m) for _, m in dl.fail[:2]) or "未知")
        self.current = None
        if job.state in ("done", "failed", "canceled"):
            self._finish(job)
        self.push(force=True)


_BAD = '<>:"/\\|?*'


def _safe(name):
    """清洗文件名，但保留子目录层级（rel 可能带 / 或 \\）。"""
    parts = []
    for seg in str(name).replace("\\", "/").split("/"):
        if seg in ("", ".", ".."):
            continue
        buf = []
        for ch in seg:
            buf.append("_" if ch in _BAD else ch)
        s = "".join(buf).strip().rstrip(".")[:120]
        if s:
            parts.append(s)
    return os.path.join(*parts) if parts else "unnamed"
