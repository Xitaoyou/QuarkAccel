# -*- coding: utf-8 -*-
"""夸克网盘加速下载器（GUI）

目录树勾选 -> 选保存位置 -> 多连接分片下载，带实时进度。
"""
import json
import os
import queue
import sys
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import quark_core as qc
import quark_login as ql

APP = "夸克网盘加速下载器"
CFG = os.path.join(os.path.expanduser("~"), ".quark_accel.json")
BOX_ON, BOX_OFF = "[x]", "[ ]"


def human(n):
    n = float(n)
    for u in ("B", "KB", "MB", "GB"):
        if n < 1024 or u == "GB":
            return ("%.0f %s" if u == "B" else "%.1f %s") % (n, u)
        n /= 1024.0
    return "%.1f GB" % n


def dpi_aware():
    """让 tkinter 用物理像素布局；否则 125%/150% 缩放下窗口内容会溢出被裁。"""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        try:
            import ctypes
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def load_cfg():
    try:
        with open(CFG, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_cfg(d):
    try:
        with open(CFG, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=1)
    except OSError:
        pass


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(APP)
        sw = self.winfo_screenwidth()
        sh = self.winfo_screenheight()
        w = min(1280, max(880, int(sw * 0.82)))
        h = min(820, max(560, int(sh * 0.82)))
        self.geometry("%dx%d" % (w, h))
        self.minsize(min(880, w), min(560, h))
        self.cfg = load_cfg()
        self.api = None
        self.nodes = {}          # iid -> dict(fid, rel, size, dir, loaded)
        self.tasks = []          # 待下载 Task
        self.q = queue.Queue()   # 后台 -> UI
        self.worker = None
        self.cancel = threading.Event()
        self.dl = None           # 当前 Downloader
        self._build()
        self.after(200, self._pump)
        self._auto_cookie()

    # ---------------------------------------------------------- 界面
    def _build(self):
        bar = ttk.Frame(self, padding=(8, 6))
        bar.pack(fill="x")
        ttk.Button(bar, text="① 浏览器登录", command=self.on_login).pack(side="left")
        ttk.Button(bar, text="② 粘贴 Cookie", command=self.on_paste).pack(side="left", padx=6)
        ttk.Button(bar, text="测试", command=self.on_test).pack(side="left")
        self.lbl_user = ttk.Label(bar, text="未登录")
        self.lbl_user.pack(side="left", padx=12)

        mid = ttk.Frame(self)
        mid.pack(fill="both", expand=True, padx=8)

        left = ttk.LabelFrame(mid, text="网盘目录（点格子勾选，展开即加载）")
        left.pack(side="left", fill="both", expand=True)
        cols = ("size", "sel")
        self.tree = ttk.Treeview(left, columns=cols, show="tree headings",
                                 selectmode="browse")
        self.tree.heading("#0", text="名称")
        self.tree.heading("size", text="大小")
        self.tree.heading("sel", text="选")
        self.tree.column("#0", width=430, minwidth=200)
        self.tree.column("size", width=90, anchor="e", stretch=False)
        self.tree.column("sel", width=52, anchor="center", stretch=False)
        vs = ttk.Scrollbar(left, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vs.set)
        self.tree.pack(side="left", fill="both", expand=True)
        vs.pack(side="right", fill="y")
        self.tree.bind("<<TreeviewOpen>>", self.on_open)
        self.tree.bind("<Button-1>", self.on_click)
        self.tree.bind("<space>", lambda e: self._toggle(self.tree.focus()))

        right = ttk.LabelFrame(mid, text="下载队列")
        right.pack(side="left", fill="both", expand=True, padx=(8, 0))
        cols2 = ("size", "prog", "speed", "state")
        self.dtree = ttk.Treeview(right, columns=cols2, show="headings", height=18)
        for c, t, w, a in (("size", "大小", 80, "e"), ("prog", "进度", 180, "w"),
                           ("speed", "速度", 90, "e"), ("state", "状态", 130, "w")):
            self.dtree.heading(c, text=t)
            self.dtree.column(c, width=w, anchor=a, stretch=(c == "prog"))
        vs2 = ttk.Scrollbar(right, orient="vertical", command=self.dtree.yview)
        self.dtree.configure(yscrollcommand=vs2.set)
        self.dtree.pack(side="left", fill="both", expand=True)
        vs2.pack(side="right", fill="y")

        bot = ttk.Frame(self, padding=(8, 6))
        bot.pack(fill="x")
        ttk.Label(bot, text="保存到").grid(row=0, column=0, sticky="w")
        self.var_out = tk.StringVar(value=self.cfg.get("out") or
                                    os.path.join(os.path.expanduser("~"), "Downloads", "QuarkDL"))
        ttk.Entry(bot, textvariable=self.var_out).grid(row=0, column=1, sticky="we", padx=6)
        ttk.Button(bot, text="浏览…", command=self.on_pick).grid(row=0, column=2)
        bot.columnconfigure(1, weight=1)

        ttk.Label(bot, text="连接数").grid(row=1, column=0, sticky="w", pady=(6, 0))
        self.var_conns = tk.IntVar(value=int(self.cfg.get("conns", 384)))
        ttk.Spinbox(bot, from_=8, to=512, increment=8, width=6,
                    textvariable=self.var_conns).grid(row=1, column=1, sticky="w", padx=6, pady=(6, 0))
        ttk.Label(bot, text="切片 MB").grid(row=1, column=2, sticky="e")
        self.var_part = tk.IntVar(value=int(self.cfg.get("part", 1)))
        ttk.Spinbox(bot, from_=1, to=16, width=5,
                    textvariable=self.var_part).grid(row=1, column=3, sticky="w", padx=(4, 10))
        ttk.Label(bot, text="token 池").grid(row=1, column=4, sticky="e")
        self.var_tok = tk.IntVar(value=int(self.cfg.get("tokens", 32)))
        ttk.Spinbox(bot, from_=1, to=64, width=5,
                    textvariable=self.var_tok).grid(row=1, column=5, sticky="w", padx=(4, 10))

        self.btn_start = ttk.Button(bot, text="③ 开始下载", command=self.on_start)
        self.btn_start.grid(row=1, column=6, padx=(10, 4))
        self.btn_stop = ttk.Button(bot, text="停止", command=self.on_stop, state="disabled")
        self.btn_stop.grid(row=1, column=7, padx=4)
        ttk.Button(bot, text="打开目录", command=self.on_open_dir).grid(row=1, column=8)

        self.pb = ttk.Progressbar(self, mode="determinate", maximum=100)
        self.pb.pack(fill="x", padx=8, pady=(0, 2))
        self.lbl_status = ttk.Label(self, text="就绪", padding=(10, 0, 10, 8))
        self.lbl_status.pack(fill="x")

    # ---------------------------------------------------------- Cookie
    def _cookie_paths(self):
        here = os.path.dirname(os.path.abspath(__file__))
        return [os.path.join(here, "cookie.txt"),
                os.path.join(os.path.dirname(here), "cookie.txt"),
                os.path.abspath("cookie.txt")]

    def _auto_cookie(self):
        """依次找：程序同目录 / 上级目录 / 当前目录 / 上次保存的配置。"""
        for p in self._cookie_paths():
            if not os.path.exists(p):
                continue
            try:
                with open(p, encoding="utf-8") as f:
                    ck = f.read().strip()
            except OSError:
                continue
            if "__puus" in ck or "__kp" in ck or "_UP_D_" in ck:
                self.log("从 %s 读到 Cookie" % p)
                self.set_cookie(ck, silent=True)
                return
        ck = (self.cfg.get("cookie") or "").strip()
        if ck:
            self.log("用上次保存的 Cookie")
            self.set_cookie(ck, silent=True)

    def set_cookie(self, ck, silent=False):
        self.api = qc.Api(ck)
        self.cfg["cookie"] = ck
        save_cfg(self.cfg)
        if not silent:
            self.log("Cookie 已设置（%d 字节）" % len(ck))
        threading.Thread(target=self._verify, daemon=True).start()

    def _verify(self):
        try:
            d = self.api.whoami()
            nick = d.get("nickname") or d.get("nick_name") or "已登录"
            vip = d.get("member_type") or "?"
            self.q.put(("user", "%s（%s）" % (nick, vip)))
        except Exception as e:
            self.q.put(("user", "Cookie 无效: %s" % str(e)[:60]))

    def on_login(self):
        if self.api is not None:
            if not messagebox.askyesno("重新登录", "已经有一个 Cookie 了，要重新登录吗？"):
                return
        self.log("打开浏览器，请在窗口里登录 pan.quark.cn（登录后会自动关闭）")

        def work():
            try:
                ck = ql.launch_and_read_cookie(log=lambda m: self.q.put(("log", m)))
                self.q.put(("cookie", ck))
            except Exception as e:
                self.q.put(("log", "登录失败: %s" % e))

        threading.Thread(target=work, daemon=True).start()

    def on_paste(self):
        win = tk.Toplevel(self)
        win.title("粘贴 Cookie")
        win.geometry("640x260")
        ttk.Label(win, text="F12 → Network → 任意 drive-pc.quark.cn 请求 → 复制 Request Headers 里的 Cookie",
                  padding=8, wraplength=600).pack(fill="x")
        txt = tk.Text(win, wrap="char")
        txt.pack(fill="both", expand=True, padx=8)
        cur = self.cfg.get("cookie") or ""
        txt.insert("1.0", cur)

        def ok():
            ck = txt.get("1.0", "end").strip()
            if "=" not in ck:
                messagebox.showwarning("格式不对", "看起来不像 Cookie 字符串")
                return
            self.set_cookie(ck)
            win.destroy()

        ttk.Button(win, text="确定", command=ok).pack(pady=8)

    def on_test(self):
        if not self.api:
            messagebox.showinfo("还没登录", "先点「① 浏览器登录」或粘贴 Cookie")
            return
        threading.Thread(target=self._verify, daemon=True).start()

    # ---------------------------------------------------------- 目录树
    def refresh(self):
        if not self.api:
            messagebox.showinfo("还没登录", "先登录")
            return
        self.tree.delete(*self.tree.get_children())
        self.nodes.clear()
        self.tree.insert("", "end", iid="0", text="根目录", values=("", BOX_OFF), open=True)
        self.nodes["0"] = dict(fid="0", rel="", size=0, dir=True, loaded=False)
        self.load_children("0")

    def load_children(self, iid):
        n = self.nodes.get(iid)
        if not n or n["loaded"]:
            return
        n["loaded"] = True
        self.log("读取 %s …" % (n["rel"] or "根目录"))

        def work():
            try:
                items = self.api.list_dir(n["fid"])
                self.q.put(("children", iid, items))
            except Exception as e:
                self.q.put(("log", "读取目录失败: %s" % str(e)[:80]))

        threading.Thread(target=work, daemon=True).start()

    def _fill(self, iid, items):
        n = self.nodes[iid]
        ph = iid + "#ph"                 # 展开后把「…」占位行清掉
        if self.tree.exists(ph):
            self.tree.delete(ph)
        for e in items:
            child = e.fid
            if child in self.nodes:
                continue
            self.nodes[child] = dict(fid=e.fid, rel=n["rel"] + e.name, size=e.size,
                                     dir=e.is_dir, loaded=False)
            self.tree.insert(iid, "end", iid=child, text=e.name,
                             values=("" if e.is_dir else human(e.size), BOX_OFF))
            if e.is_dir:
                self.tree.insert(child, "end", iid=child + "#ph", text="…")

    def on_open(self, _evt=None):
        iid = self.tree.focus()
        if iid in self.nodes:
            self.load_children(iid)

    def on_click(self, evt):
        iid = self.tree.identify_row(evt.y)
        col = self.tree.identify_column(evt.x)
        if iid and col == "#2" and iid in self.nodes:
            self._toggle(iid)

    def _toggle(self, iid):
        n = self.nodes.get(iid)
        if not n:
            return
        on = self.tree.set(iid, "sel") == BOX_ON
        self._set_tree(iid, not on, mark_dirs=True)

    def _set_tree(self, iid, on, mark_dirs=False):
        self.tree.set(iid, "sel", BOX_ON if on else BOX_OFF)
        for c in self.tree.get_children(iid):
            if c in self.nodes:
                self._set_tree(c, on, mark_dirs)

    # ---------------------------------------------------------- 收集
    def _collect(self):
        out = []

        def walk(iid, base):
            n = self.nodes[iid]
            if n["dir"]:
                for c in self.tree.get_children(iid):
                    if c in self.nodes:
                        walk(c, base)
            else:
                out.append(qc.Task(rel=n["rel"], fid=n["fid"], size=n["size"]))

        for iid, n in list(self.nodes.items()):
            if self.tree.exists(iid) and self.tree.set(iid, "sel") == BOX_ON:
                # 避免父子重复：只处理"父亲没被勾"的节点
                parent = self.tree.parent(iid)
                dup = False
                while parent:
                    if parent in self.nodes and self.tree.set(parent, "sel") == BOX_ON:
                        dup = True
                        break
                    parent = self.tree.parent(parent)
                if not dup:
                    walk(iid, n["rel"])
        seen, uniq = set(), []
        for t in out:
            if t.rel not in seen:
                seen.add(t.rel)
                uniq.append(t)
        return uniq

    # ---------------------------------------------------------- 下载
    def on_start(self):
        if not self.api:
            messagebox.showinfo("还没登录", "先登录")
            return
        if self.worker and self.worker.is_alive():
            messagebox.showinfo("正在下载", "等当前任务结束，或先点停止")
            return
        tasks = self._collect()
        if not tasks:
            messagebox.showinfo("没选文件", "在左边目录树里勾选要下载的文件或文件夹")
            return
        outdir = self.var_out.get().strip()
        os.makedirs(outdir, exist_ok=True)
        self.cfg.update(out=outdir, conns=self.var_conns.get(),
                        part=self.var_part.get(), tokens=self.var_tok.get())
        save_cfg(self.cfg)

        self.dtree.delete(*self.dtree.get_children())
        for i, t in enumerate(tasks):
            self.dtree.insert("", "end", iid=str(i),
                              values=(human(t.size), "", "", "等待"),
                              text=t.rel)
        total = sum(t.size for t in tasks)
        self.log("共 %d 个文件 / %s -> %s" % (len(tasks), human(total), outdir))
        self.cancel.clear()
        self.btn_start.configure(state="disabled")
        self.btn_stop.configure(state="normal")
        params = {"conns": self.var_conns.get(), "part": self.var_part.get(),
                  "tokens": self.var_tok.get()}
        self.worker = threading.Thread(target=self._run, args=(tasks, outdir, params), daemon=True)
        self.worker.start()

    def _run(self, tasks, outdir, params):
        total = sum(t.size for t in tasks) or 1
        base = 0
        okn = 0
        t0 = time.time()
        for i, t in enumerate(tasks):
            if self.cancel.is_set():
                self.q.put(("state", i, "已取消"))
                break
            dst = os.path.join(outdir, *t.rel.split("/"))
            os.makedirs(os.path.dirname(dst) or outdir, exist_ok=True)
            have = qc.resolve_path(dst)
            if os.path.exists(have) and os.path.exists(have + ".qbad"):
                self.q.put(("state", i, "上次失败，重下"))
            elif os.path.exists(have) and os.path.getsize(have) == t.size:
                # 尺寸对不代表内容对：预分配会留下满尺寸的全零假文件
                good, why = qc.file_ok(self.api, t.fid, dst, t.size)
                if good:
                    self.q.put(("state", i, "已存在，跳过"))
                    base += t.size
                    okn += 1
                    continue
                self.q.put(("state", i, "校验不过，重下"))
                self.q.put(("log", "%s: %s" % (t.rel, why)))
            self.q.put(("state", i, "下载中"))
            conns = qc.plan_conns(t.size, params["conns"])
            last = [0.0, 0.0]

            def prog(d, _i=i, _t=t, _base=base):
                now = time.time()
                if now - last[0] < 0.5:
                    return
                dt = max(now - last[0], 0.001)
                sp = (d.bytes_done - last[1]) / dt
                last[0] = now
                last[1] = d.bytes_done
                self.q.put(("prog", _i, d.bytes_done, _t.size, sp,
                            (_base + d.bytes_done) / total))

            d = qc.Downloader(self.api, t, dst, conns=conns,
                              part_mb=params["part"], tokens=params["tokens"],
                              progress=prog)
            self.dl = d
            try:
                good = d.run()
            except Exception as e:
                self.q.put(("state", i, "出错: %s" % str(e)[:40]))
                good = False
            self.dl = None
            base += t.size
            if good:
                okn += 1
                self.q.put(("state", i, "完成"))
            else:
                self.q.put(("state", i, "未完成（可重跑续传）"))
            self.q.put(("prog", i, t.size, t.size, 0.0, base / total))
        el = max(time.time() - t0, 0.001)
        self.q.put(("done", okn, len(tasks), el))

    def on_stop(self):
        self.cancel.set()
        if self.dl:
            self.dl.stop.set()
        self.log("正在停止…")

    def on_open_dir(self):
        p = self.var_out.get().strip()
        os.makedirs(p, exist_ok=True)
        try:
            os.startfile(p)
        except AttributeError:
            pass

    def on_pick(self):
        d = filedialog.askdirectory(initialdir=self.var_out.get() or None)
        if d:
            self.var_out.set(d)

    # ---------------------------------------------------------- 事件泵
    def log(self, msg):
        self.lbl_status.configure(text=msg)

    def _pump(self):
        try:
            while True:
                item = self.q.get_nowait()
                kind = item[0]
                if kind == "children":
                    _, iid, items = item
                    if self.tree.exists(iid):
                        self._fill(iid, items)
                elif kind == "log":
                    self.log(item[1])
                elif kind == "user":
                    self.lbl_user.configure(text=item[1])
                    self.log("身份: " + item[1])
                    if not self.tree.get_children():
                        self.refresh()
                elif kind == "cookie":
                    self.set_cookie(item[1])
                elif kind == "state":
                    _, i, st = item
                    if self.dtree.exists(str(i)):
                        self.dtree.set(str(i), "state", st)
                        if st in ("完成", "已存在，跳过", "已取消"):
                            self.dtree.set(str(i), "speed", "")
                elif kind == "prog":
                    _, i, got, size, sp, frac = item
                    if self.dtree.exists(str(i)):
                        self.dtree.set(str(i), "prog", "%.1f%%" % (100.0 * got / max(size, 1)))
                        mb = sp / 1048576.0
                        self.dtree.set(str(i), "speed",
                                       ("%.2f MB/s" % mb) if mb > 0.005 else "")
                    self.pb.configure(value=min(100, 100 * frac))
                elif kind == "done":
                    _, okn, n, el = item
                    self.btn_start.configure(state="normal")
                    self.btn_stop.configure(state="disabled")
                    self.pb.configure(value=100)
                    self.log("结束：成功 %d / %d，用时 %.1f 分钟" % (okn, n, el / 60))
        except queue.Empty:
            pass
        self.after(250, self._pump)


def main():
    dpi_aware()
    app = App()
    app.mainloop()


if __name__ == "__main__":
    main()
