import os
import sys
import subprocess
import time
import threading
import webbrowser

try:
    from version import __version__
except Exception:
    __version__ = "unknown"

def check_and_install_requirements():
    print("=" * 60)
    print("   QQX_AIBot // OneBot v11 原生 AI 伴侣控制台启动器")
    print("   版本 v%s" % __version__)
    print("=" * 60)
    print("[1/3] 正在检查依赖包...")
    
    needed = ["fastapi", "uvicorn", "aiohttp", "websockets", "pydantic"]
    missing = []
    for pkg in needed:
        try:
            __import__(pkg)
        except ImportError:
            missing.append(pkg)

    if missing:
        print(f"检测到缺少依赖: {', '.join(missing)}，正在自动安装...")
        req_path = os.path.join(os.path.dirname(__file__), "requirements.txt")
        cmd = [sys.executable, "-m", "pip", "install", "-r", req_path, "--prefer-binary",
               "-i", "https://pypi.org/simple"]
        ret = subprocess.call(cmd)
        if ret != 0:
            print("官方源安装失败，尝试国内镜像...")
            ret = subprocess.call([sys.executable, "-m", "pip", "install", "-r", req_path,
                                   "--prefer-binary", "-i", "https://pypi.tuna.tsinghua.edu.cn/simple"])
        if ret != 0:
            print("=" * 62)
            print("  依赖安装失败，已停止启动。")
            print("  常见原因：pip 源上没有你所用 Python 版本的预编译包，")
            print("  退化成源码编译又缺少编译器（Microsoft Visual C++ Build Tools）。")
            print("  可手动重试：")
            print('    "{EXE}" -m pip install -r "{REQ}" -i https://pypi.org/simple'.format(
                EXE=sys.executable, REQ=req_path))
            print("  或改装 Python 3.12 / 3.13（预编译包覆盖最全）后重新运行。")
            print("=" * 62)
            sys.exit(1)
        print("依赖安装完成！")
    else:
        print("所有依赖包已就绪！")


def create_desktop_shortcut():
    try:
        current_dir = os.path.dirname(os.path.abspath(__file__))
        bat_path = os.path.join(current_dir, "一键启动.bat")
        
        ps_code = f"""
        $ws = New-Object -ComObject WScript.Shell
        $desktop = [System.Environment]::GetFolderPath('Desktop')
        $shortcut = $ws.CreateShortcut((Join-Path $desktop 'QQ AI伴侣控制台.lnk'))
        $shortcut.TargetPath = '{bat_path}'
        $shortcut.WorkingDirectory = '{current_dir}'
        $shortcut.Description = 'QQ AI伴侣一键启动控制台'
        $shortcut.Save()
        """
        subprocess.run(["powershell", "-NoProfile", "-Command", ps_code], capture_output=True)
    except Exception:
        pass


def open_browser():
    # 不要抢在 uvicorn 前面开浏览器：后台线程轮询端口，真的通了再弹
    def _open_when_ready(url, tries=120):
        import urllib.request as _ur, time as _t
        for _ in range(tries):
            try:
                _ur.urlopen(url, timeout=2).read(1)
                break
            except Exception:
                _t.sleep(1)
        else:
            print("\u63d0\u793a: \u7b49\u5f85 120 \u79d2\u4ecd\u672a\u76d1\u542c\uff0c\u4ecd\u5c1d\u8bd5\u6253\u5f00\u6d4f\u89c8\u5668\uff0c\u82e5\u62d2\u7edd\u8fde\u63a5\u8bf7\u624b\u52a8\u5237\u65b0")
        try:
            webbrowser.open(url)
        except Exception:
            pass

    import threading as _th
    _th.Thread(target=_open_when_ready, args=("http://127.0.0.1:8090",), daemon=True).start()


if __name__ == "__main__":
    check_and_install_requirements()
    
    print("[2/3] 正在检查桌面快捷方式...")
    create_desktop_shortcut()

    print("[3/3] 正在启动后台控制台并自动打开浏览器...")
    print("控制台访问地址: http://127.0.0.1:8090 (避开 ComfyUI 8000 端口)")
    print("NapCat 工作路径: C:\\ai\\NapCat.Shell.Windows.Node\\napcat.bat")
    print("-" * 60)

    # 异步在后台延迟打开浏览器
    threading.Thread(target=open_browser, daemon=True).start()

    # 启动 uvicorn
    import uvicorn
    from app import app
    uvicorn.run(app, host="127.0.0.1", port=8090, reload=False)
