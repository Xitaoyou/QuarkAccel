# -*- coding: utf-8 -*-
"""build2.py —— 打包外壳版为单文件 exe。"""
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
os.chdir(HERE)

args = [
    sys.executable, "-m", "PyInstaller",
    "--noconfirm", "--clean", "--onefile", "--windowed",
    "--name", "QuarkAccel",
    # 解压目录必须显式指定：
    #   · 默认走 %TEMP% 在这台机器上会报 "Could not create temporary directory!"
    #     （连最小 hello 程序都一样，是环境问题）；
    #   · 指到 %LOCALAPPDATA%\QuarkAccel\tmp 会报 "CreateDirectory: 拒绝访问"。
    # 实测只有 "."（exe 所在目录）稳定可用，双击启动时 CWD 就是 exe 目录。
    # 代价：exe 必须放在可写目录里（正常放桌面/下载目录都满足）。
    "--runtime-tmpdir", ".",
        "--icon", "app.ico",
    "--version-file", "version_info.txt",
    "--add-data", "ui.html;.",
    "--add-data", "quark_mark.png;.",
    "--add-data", "quark_word.png;.",
    "--paths", "..",
    "--hidden-import", "quark_core",
    "--hidden-import", "profiles",
    "--hidden-import", "engine",
    "--hidden-import", "server",
    "--hidden-import", "cdp",
    "--hidden-import", "stream",
    "--hidden-import", "procs",
    "--hidden-import", "importer",
    "app2.py",
]
print(" ".join(args))
p = subprocess.run(args)
if p.returncode != 0:
    print("BUILD FAILED rc=%d" % p.returncode)
    sys.exit(p.returncode)
exe = os.path.join(HERE, "dist", "QuarkAccel.exe")
print("OK %s %d bytes" % (exe, os.path.getsize(exe)))