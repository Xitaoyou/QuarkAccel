import os, subprocess, sys
HERE = os.path.dirname(os.path.abspath(__file__)); os.chdir(HERE)
args = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--onedir",
        "--console", "--name", "QuarkDir",     "--icon", "app.ico",
    "--version-file", "version_info.txt",
    "--add-data", "ui.html;.",
        "--add-data", "demo.mp4;.",
        "--add-data", "quark_mark.png;.",
        "--add-data", "quark_word.png;.", "--paths", "..",
        "--hidden-import", "quark_core", "--hidden-import", "profiles",
        "--hidden-import", "engine", "--hidden-import", "server", "--hidden-import", "cdp",
        "--hidden-import", "stream", "--hidden-import", "procs",
    "--hidden-import", "importer",
        "app2.py"]
sys.exit(subprocess.run(args).returncode)