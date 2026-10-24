# QQbotX - 原生 OneBot v11 QQ AI 伴侣框架 (Phase 1)

一个轻量级、无臃肿依赖、针对大语言模型“强制破甲”（Tool Calling 注入）的极简原生 QQ AI 伴侣开发框架。
无需 NoneBot / AstrBot 等重量级库，直接基于 OneBot v11 协议通信，对底层请求拥有绝对掌控力。

---

## 🛠️ 项目架构

整个项目仅包含 **2 个 Python 文件** 和 **1 个 HTML 文件**：

```text
QQbotX/
├── app.py             # FastAPI Web 服务、NapCat 子进程异步管理、Web UI 实时日志 WebSocket
├── bot.py             # 原生 OneBot v11 客户端协议处理、LLM Tool Calling 破甲调用与正则容错
├── index.html         # 极客风格 Web 控制台 (TailwindCSS CDN + 原生 JS + 等宽 ASCII 终端)
├── requirements.txt   # 基础依赖清单
└── config.json        # 运行时自动生成的参数配置文件
```

---

## 🚀 核心机制：Tool Calling 强制破甲

许多大语言模型（如各类具有强安全对齐与内审机制的模型）在面对角色扮演或敏感伴侣话题时，容易触发直接拒答拦截。

本框架采用 **Tool Calling 注入破甲方案**：
1. 在 payload 中构造强制工具定义：
   ```json
   {
     "type": "function",
     "function": {
       "name": "output_text",
       "description": "Output the final response text to the user.",
       "parameters": {
         "type": "object",
         "properties": {
           "content": {"type": "string", "description": "The response content"}
         },
         "required": ["content"]
       }
     }
   }
   ```
2. 强制指定 `tool_choice: {"type": "function", "function": {"name": "output_text"}}`，绕过纯文本审查器的直接拒绝；
3. 后端拦截 `choices[0].message.tool_calls[0].function.arguments`，结合 4 级 JSON / 正则容错算法抽取 `content` 内容，发送回 QQ 私聊用户。

---

## 📦 极简使用：双击一键启动（免命令行、免输端口）

无需手动敲击命令，项目内置了一键批处理脚本：

1. 进入 `C:\ai\QQX_AIBot\`，直接双击运行 **`一键启动.bat`**；
2. 脚本将**自动**执行以下动作：
   - 检查 Python 运行环境；
   - 自动检测并补全必要依赖（使用清华高速镜像源）；
   - 在 Windows 桌面上**自动生成【QQ AI伴侣控制台】桌面快捷方式**，以后直接在桌面上双击即可；
   - 启动服务（端口为 **`8090`**，完美避开 ComfyUI 占用的 8000 端口）；
   - **自动在系统默认浏览器中弹出 Web UI 控制台**，无需手动记端口！
3. 网页打开后会自动拉起本地 NapCat（位于 `C:\ai\NapCat.Shell.Windows.Node`），控制台终端实时输出登录二维码供手机 QQ 扫码！

---

## 💡 使用流程

1. **首次配置**：
   - 在弹出的 Web UI 中填写你的大模型 API Base URL、API Key、Model Name 等；
   - NapCat 路径已默认适配 `C:\ai\NapCat.Shell.Windows.Node\napcat.bat`；
   - 点击 **【保存配置】**（支持热重载，无需重启服务）。
2. **手机 QQ 扫码**：
   - 网页右侧黑客终端实时流式打印 NapCat 启动日志与 **ASCII 登录二维码**；
   - 用手机 QQ 扫描终端中的二维码完成登录（若捕获到登录链接，顶部也会弹出【扫码预览】弹窗）。
3. **连接 QQ 链路**：
   - NapCat 登录完成后，点击左侧 **【连接到 QQ】** 按钮；
   - 看到状态指示灯变为绿色，即可通过 QQ 私聊体验专属 AI 伴侣！

---

## 🔄 Phase 2：上下文持久化 + 聊天记录可视化窗口

### 新增能力
1. **上下文持久化**：用户与 AI 的全部私聊上下文会自动落盘到项目根目录的 `history.json`，服务重启后依然保留；不再是最初「重启即丢失、只留最近 20 条」的内存 `history`。
2. **聊天记录可视化窗口**：Web 控制台顶部新增【控制台 / 聊天记录】视图切换。聊天记录视图左侧为会话列表（按 QQ 用户），右侧以聊天气泡形式展示该用户的完整上下文，并随新消息实时刷新。
3. **删除即抹除**：在聊天记录窗口中，鼠标悬停任意消息 → 点击 🗑，即可把该条消息从 `history.json` 与后续 Prompt 上下文中彻底抹除；也可一键「清空该会话」。

### 新增后端接口
| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/history` | 获取所有会话摘要（供会话列表） |
| GET | `/api/history/{user_id}` | 获取指定用户的完整上下文 |
| DELETE | `/api/history/{user_id}/message/{message_id}` | 从上下文中彻底抹除单条消息 |
| DELETE | `/api/history/{user_id}` | 清空指定用户的全部上下文 |

### 说明
- 组装上下文时会自动合并相邻同角色消息，避免删除后出现连续同角色导致部分严格校验的 API 报错。
- `history.json` 为纯文本 JSON，可直接查看或备份。
- 本补丁已把原 `app.py` / `bot.py` / `index.html` / `README.md` 备份为 `*.bak_<时间戳>`。
