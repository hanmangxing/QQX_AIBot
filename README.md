# QQX_AIBot

[![License](https://img.shields.io/badge/License-AGPL--3.0-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/Python-3.10%2B-3776ab.svg)](https://www.python.org/downloads/)
[![NapCat](https://img.shields.io/badge/QQ%20Protocol-NapCat-2f6fdb.svg)](https://github.com/NapNeko/NapCatQQ)
[![SnowLuma](https://img.shields.io/badge/QQ%20Protocol-SnowLuma-8a5cf6.svg)](https://github.com/SnowLuma/SnowLuma)

**QQ星，一个自托管、一次部署就能用的 QQ AI 伴侣。**

前言：如果你是酒馆玩家，这个项目你能很快上手，你可以把你喜欢的角色直接放进QQ里全天候互动，你也可以自己把喜欢的预设和世界书写在里面，并且使用Gemini模型，你懂的。同时有外审拦截保驾护航，自动重roll，但预设需要加群拿(见安装文档和本文底部)。只需部署并配置，就可以直接享受文生图与表情包等高互动，有人味的全面体验。这不是又一个机器人开发框架——框架这块已经有 AstrBot、NoneBot2 做得很好。QQX_AIBot 的定位是酒馆与AstrBot中间，做第一个把番茄和鸡蛋炒在一起的项目，想做的是把「陪伴感」直接做成产品：部署完就有记忆、有情绪、会主动找你说话，不用自己一个个拼插件。

## 它解决什么问题

| 现有方案 | 缺什么 |
| --- | --- |
| AstrBot 等机器人框架 | 互动和插件生态很强，但伴侣感不是它的目标；想接便宜量大、角色扮演强的 Gemini 类模型，还要额外折腾 |
| SillyTavern（酒馆） | 角色扮演与内容自由度好，但缺少 QQ 上的强互动和伴侣感 |
| 自己拼装 | 记忆、情绪、主动搭话、表情包、生图、内容边界……每样都要单独找插件、单独调 |

QQX_AIBot 把这些打包成一套可运行的整体：**协议端保活 + 多模型接入 + 人格与预设 + 长期记忆 + 情绪 + 主动性 + 多模态 + 内容边界处理**，双击启动即可。

## 主要能力

### 部署与运行
- 一键启动：双击 `一键启动.bat`，自动安装依赖并拉起服务
- WebUI 控制台：浏览器里配 API、人设、各项开关，不用改代码
- 双协议端：[NapCat](https://github.com/NapNeko/NapCatQQ) / [SnowLuma](https://github.com/SnowLuma/SnowLuma) 可切换，支持自动启停与保活
- 依赖极轻：`fastapi`、`uvicorn`、`aiohttp`、`websockets`、`pydantic`

### 对话与人格
- 多套 API 档案（`llm_profiles`）：不同 key / 模型随时切换，OpenAI 兼容接口（Gemini、DeepSeek 等均可）
- 人设、风格预设、世界书，注入层级可调；世界书支持**按数据源分别存多套配置档**（主 Bot 与群聊模式各自独立、互不影响），选中即切换，改完自动保存
- 时间注入、思考链剥离
- 群聊独立链路：群规则、群内人格、静默标记、呼叫模式（@ 或关键词触发）

### 记忆与情绪
- 向量长期记忆（`memory_engine/`）：按相关度召回、批量整理、跨群共享开关
- 笔记本：AI 可读写的知识库，支持工具调用模式
- 情绪状态：把当前情绪注入上下文
- 剧情记忆整理：自动从对话中抽取值得长期记住的信息

### 主动性与互动
- 主动搭话：空闲一段时间后主动找人，频率可爬坡、夜间静默、每日上限、可召回旧话题
- 戳一戳：回戳、跟戳、冷却与备注
- 消息分段：按字数与节奏拆成多条，模拟打字
- 连发合并：本地小模型判断对方是不是还没说完（自带 `debounce_model/`，纯 Python 前向，无需 onnxruntime）
- 表情包：池子容量与淘汰策略、视觉打标、自动偷图（可设白名单/黑名单）

### 多模态
- 看图：图片理解，带数量与体积上限
- 生图：接 [ComfyUI](https://github.com/comfyanonymous/ComfyUI) 工作流，支持节点映射与冷却
- 表情包视觉理解与自动标注

### 设备联动
- 郊狼（DG-LAB）设备连接（需自备设备，用法自行配置）

### 内容边界
- 回复拦截处理：识别空回复 / 被拦截的回复并重试、兜底
- 群聊秘书网关：判断「现在该不该插话」（关键词、情绪阈值、重复消息检测）
- 预设系统：system prompt、风格、世界书的注入层级均可调，**具体内容由使用者自行配置**

## 快速开始

需要先准备好这些前置：

| 前置 | 作用 | 地址 |
| --- | --- | --- |
| **Python 3.10+** | 运行环境（实测 3.12） | [python.org/downloads](https://www.python.org/downloads/) |
| **QQ 协议端** | 用来登录 QQ，二选一 | [NapCat](https://github.com/NapNeko/NapCatQQ) ／ [SnowLuma](https://github.com/SnowLuma/SnowLuma) |
| **模型 API** | OpenAI 兼容接口，Gemini / DeepSeek 等均可 | 按你用的服务商 |

1. **环境**：Windows，Python 3.10+（实测 3.12）
2. **启动**：双击 `一键启动.bat`；或命令行 `py -3 start.py`（`python start.py` 亦可）
3. **配置**：启动后控制台会自动打开，在「API 配置」里填模型 key
4. **接协议端**：按 [NapCat](https://github.com/NapNeko/NapCatQQ) / [SnowLuma](https://github.com/SnowLuma/SnowLuma) 的说明登录 QQ，填入 WebSocket 地址与 token

> 📖 **图文安装教程**（保姆级，含 24 张截图，从装 Python 到握手成功）：[`docs/安装教程.md`](docs/安装教程.md)；想要 PDF 版见 [Releases](../../releases)。

> `config.json` 由程序首次运行时自动生成，**不要**提交或外传——它含密钥，已在 `.gitignore` 中。所有可配置项的键名见 `config.example.json`。

## Docker 部署（可选 · 需要一点基础）

**先说清楚：这一节不是零门槛。** 它唯一的价值是让用的人**不用装 Python**。如果你只是自己用、本机跑得好好的，**可以完全跳过这一节**。

不适合用 Docker 的情况：

- 你想改代码、边改边看效果 —— 容器里挂载目录虽然也能改，但路径和权限容易绕晕，本机跑更顺
- 你机器上已经跑着一个本机版协议端 —— 看文末「备选」

### 一条命令起来

```bash
git clone https://github.com/hanmangxing/QQX_AIBot.git
cd QQX_AIBot
docker compose up -d
```

会起两个容器：`snowluma`（协议端，官方镜像 `motricseven7/snowluma`）+ `qqx-aibot`（主程序）。首次自动拉镜像，约 2.7GB。

### 首次使用：扫码登录 QQ

浏览器打开 **http://127.0.0.1:6081** 进 noVNC 网页远程桌面，用手机 QQ 扫码登录。

- 那个「突然多出来的桌面」就是**容器里的 Linux 桌面**，容器没有屏幕，所以用浏览器远程看它。扫码在这里完成，**登录一次就行**
- 要密码的话在 `docker logs snowluma` 里；建议直接建个 `.env` 写 `VNC_PASSWD=你的密码` 固定住（不写的话每次重建容器都会随机换一个）
- 登录数据存在具名卷里，`docker compose restart` 不用重扫；但 `--force-recreate` 会掉登录，需要重扫

### 最容易踩的坑：`127.0.0.1` 到底指谁

**记住这一条，能省掉大半麻烦：**

> 容器里的 `127.0.0.1` 指的是**容器自己**，不是你的电脑。

所以任何「本机地址」写进配置、又在容器里用，都会连不上。下面三处编排已经自动处理：

| 配置项 | 容器里自动变成 | 靠什么 |
|---|---|---|
| `snowluma_ws_url` | `ws://snowluma:3001` | 环境变量覆盖 |
| 协议端 WebUI 入口 | 宿主机映射出去的端口 | `QQX_SNOWLUMA_WEBUI_URL` |
| `comfy_server`（生图） | `http://host.docker.internal:8000` | 代码自动改写回环地址 |

**生图（ComfyUI）**：ComfyUI 通常跑在宿主机上，容器访问它得用 `host.docker.internal`，代码已自动改写，你不用动配置。如果 ComfyUI 在**别的机器**上，直接填那台机器的 IP（**别填 `127.0.0.1`**）。

### 地址和 token 都不用管

两个容器在同一个 compose 网络里，编排已经把地址设成容器名：

```yaml
QQX_SNOWLUMA_WS_URL: ws://snowluma:3001
```

`access_token` 也会自动读出来 —— SnowLuma 的数据卷只读挂给了主程序（`snowluma-data:/snowluma:ro` + `QQX_SNOWLUMA_DIR=/snowluma`）。SnowLuma 换了 token 也会自动跟上。

**打开 http://127.0.0.1:8090 就能用，不用改任何配置。**

### 端口

| 端口 | 用途 |
|---|---|
| 8090 | QQX_AIBot 控制台 |
| 6081 | noVNC 网页桌面（扫码登录用） |
| 5099 | SnowLuma 自带 WebUI |
| 3001 / 3000 | OneBot 正向 WS / HTTP |

撞端口就在 `docker-compose.yml` 里改 `ports` 冒号左边那个数。如果宿主机上已经装了本机版 SnowLuma（占着 3001/5099），可以把 `ports` 左边换成别的数，或者用 `docker compose -f docker-compose.yml -f 你的覆盖文件.yml up -d`。

### 几个注意点

- **WebUI 里的「启动协议端」按钮** —— 协议端是另一个容器，主程序管不到它，点它不会启动任何东西。它会直接跳转到协议端 WebUI（容器里跳的是宿主机映射出去的地址，不会跳到你本机那个）。要重启协议端用 `docker compose restart snowluma`
- **保活功能不生效** —— 容器配了 `restart: unless-stopped`，崩了会自动重启
- **改了配置要 `docker compose restart qqx-aibot`**；`docker restart` 不会应用新的环境变量，环境变量变了要 `up -d --force-recreate`；依赖变了（`requirements.txt`）要 `up -d --build`
- 调参数（VNC 密码、屏幕分辨率等）在项目根目录建 `.env`，变量名参考 SnowLuma 官方模板的 `.env.example`

### 备选：协议端留在容器外

宿主机上已经跑着 SnowLuma / NapCat、不想动它的话：把 `docker-compose.yml` 里 `qqx-aibot` 的 `depends_on: [snowluma]` 和 `- snowluma-data:/snowluma:ro` 两处删掉，再删掉 `QQX_SNOWLUMA_DIR` / `QQX_SNOWLUMA_WS_URL` 两个环境变量。

- 主程序用 `host.docker.internal` 访问宿主机上的协议端。Windows / macOS 的 Docker Desktop 自带这个主机名；**Linux 上需要 `extra_hosts`**（`docker-compose.yml` 里已配好）
- 这种模式下 `access_token` 要自己带在地址里：`host.docker.internal:3001?access_token=你的token`
- 地址不写 `ws://` 也没关系，会自动补上


## 目录结构

| 文件 / 目录 | 作用 |
| --- | --- |
| `app.py` | 后端主体：WebUI 接口、配置读写、默认配置 |
| `index.html` | WebUI 控制台前端 |
| `bot.py` | 主循环与消息分发 |
| `claude_group.py` | 群聊独立链路 |
| `memory.py` / `memory_engine/` | 长期记忆与向量库 |
| `mood.py` | 情绪状态 |
| `notebook.py` / `notebook_tools.py` | AI 可读写笔记本 |
| `idle.py` | 主动搭话 |
| `poke.py` | 戳一戳 |
| `segments.py` | 消息分段 |
| `debounce.py` / `debounce_model/` | 连发合并判断 |
| `sticker_steal.py` | 表情包池与偷图 |
| `comfyui.py` | [ComfyUI](https://github.com/comfyanonymous/ComfyUI) 生图 |
| `secretary.py` | 群聊秘书网关 |
| `keepalive.py` | 协议端保活 |
| `start.py` / `一键启动.bat` | 启动入口（自动装依赖） |
| `snowluma_token.py` | 自动读取 [SnowLuma](https://github.com/SnowLuma/SnowLuma) 的 accessToken（省去手填令牌） |
| `config.example.json` | 配置键名清单 |
| `docs/安装教程.md` | 图文安装教程 |
| `预设_自检.py` | 前端预设自检脚本 |

## 隐私

聊天记录、记忆库、图片、表情包等数据全部留在本机项目目录内，不会上传到任何第三方服务（除你自己配置的模型接口之外）。

## 致谢

本项目部分功能在**设计思路上**参考了以下开源项目，代码均为独立实现、未复制其源码：

| 参考项目 | 作者 | 借鉴的设计 |
| --- | --- | --- |
| [LittleWhiteBox](https://github.com/RT15548/LittleWhiteBox) | biex | 长期记忆的分层思路（逐楼锚点 / 结构化事件 / 多路混合召回） |
| [astrbot_plugin_stealer](https://github.com/nagatoquin33/astrbot_plugin_stealer) | nagatoquin33 | 表情包偷取流程设计 |
| [astrbot_plugin_debounce](https://github.com/advent259141/astrbot_plugin_debounce) | advent259141 | 消息防抖判定思路 |
| [astrbot_dg_lab_plugin](https://github.com/RC-CHN/astrbot_dg_lab_plugin) | RC-CHN | 郊狼（DG-LAB）连接功能设计 |

`debounce_model/` 所用的连发判断模型来自 ModelScope `advent259141/astrbot_debouncer_small`（Apache-2.0），版权归原作者。

上述许可证均已按各项目仓库内的许可证文件核对（核对日期 2026-10-25）；如与上游有出入，以上游仓库为准。

Based on LittleWhiteBox by biex


## 免责声明

- 本项目面向**个人自托管**用途，请自行确认所在地区的法律法规，以及你所使用模型服务商的条款
- 禁止用于任何涉及未成年人的性相关内容，以及任何违法用途
- [NapCat](https://github.com/NapNeko/NapCatQQ) / [SnowLuma](https://github.com/SnowLuma/SnowLuma) / [ComfyUI](https://github.com/comfyanonymous/ComfyUI) 等第三方组件的使用风险与合规，由使用者自行承担

## 许可证

AGPL-3.0。Copyright (c) 2026 QQX_AIBot

- **自己部署、自己用**：不触发任何额外义务
- **改完分发，或把它作为网络服务对外提供**：必须按 AGPL-3.0 公开你的修改
- 选它而不是 MIT 的原因很直接：不希望它被拿去闭源套壳，变成只有付费才能用的产品

## 更新

`git pull` 即可。`config.json` 与数据目录都不在版本控制里，更新不会覆盖你的配置和数据。

## 加群交流

有问题、想交流，或者要拿预设，都可以加群（群号 **1132194482**）：

<img src="docs/images/群二维码.jpeg" width="200" alt="QQ 群二维码">

## 支持作者

如果这个项目帮到了你，可以请作者喝杯奶茶 🥛。

<img src="docs/images/赞赏码.jpeg" width="200" alt="赞赏码">
