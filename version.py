# -*- coding: utf-8 -*-
"""QQX_AIBot 版本号 —— 全项目唯一来源。

发版时只改这里：
  1. 改 __version__
  2. 在 CHANGELOG.md 顶部加一节
  3. git tag -a v<__version__> -m "..." 然后 git push origin v<__version__>

启动器横幅、/api/version、WebUI 的「检查更新」都读这里。
"""

__version__ = "1.0.0"

# 远端仓库（形如 "用户名/仓库名"）。留空时自动从 git remote origin 推断，
# 一般不用填；仓库有多个远端、或想固定检查地址时再填。
REPO = ""

# 平台：留空时按远端域名自动判断（github.com -> github，gitee.com -> gitee）。
PLATFORM = ""
