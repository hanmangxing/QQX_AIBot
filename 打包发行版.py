# -*- coding: utf-8 -*-
"""打发行包 —— 只打包 git 跟踪的文件，天然不含 config.json 与你的数据。

用法：双击 打包发行版.bat，或命令行 py -3 打包发行版.py
产物：_deliver/QQX_AIBot-v<版本>-源码.zip（解压后是一层 QQX_AIBot-v<版本> 目录）
"""
import os, sys, subprocess, zipfile

os.chdir(os.path.dirname(os.path.abspath(__file__)) or ".")
sys.stdout.reconfigure(errors="replace")

try:
    from version import __version__ as V
except Exception:
    V = "0.0.0"

r = subprocess.run(["git", "ls-files"], capture_output=True, text=True,
                   encoding="utf-8", errors="replace")
if r.returncode != 0 or not r.stdout.strip():
    print("[ERROR] 这里不是 git 仓库或 git 不可用。发行包必须在仓库里打包。")
    sys.exit(1)
files = [x for x in r.stdout.split("\n") if x.strip()]

# 安全闸：这些东西绝不该出现在发行包里
BAN_NAME = ("config.json", ".env")
bad = [f for f in files
       if os.path.basename(f) in BAN_NAME or f.lower().endswith((".log", ".bak", ".tmp"))]
if bad:
    print("[ERROR] 仓库里有不该入库的文件，先处理再打包：")
    for f in bad:
        print("   " + f)
    sys.exit(1)

os.makedirs("_deliver", exist_ok=True)
root = "QQX_AIBot-v%s" % V
out = os.path.join("_deliver", root + "-源码.zip")
with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
    for f in files:
        z.write(f, root + "/" + f.replace("\\", "/"))

print("已生成：%s" % out)
print("  文件数 %d，体积 %.1f KB" % (len(files), os.path.getsize(out) / 1024.0))
print("  版本 %s（来自 version.py）" % V)
print("  不含：config.json / .env / 聊天记录 / 记忆 / 贴图库 / 日志")
print("  解压后目录名：%s" % root)
