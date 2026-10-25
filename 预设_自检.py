# -*- coding: utf-8 -*-
"""预设页签自检（只在内存/tempfile 跑，不碰真实配置）。"""
import io, os, re, sys, json

os.chdir(r"C:\ai\QQX_AIBot")
P = F = 0


def ck(name, cond, extra=""):
    global P, F
    if cond:
        P += 1
        print("  PASS  %s" % name)
    else:
        F += 1
        print("  FAIL  %s %s" % (name, extra))


# ---------- 1. bot.py 预设数据 ----------
import bot

bs = bot.preset_builtins()
ck("仓库不预置破限预设：内置两套已移除", len(bs) == 0, len(bs))
ck("仓库不自带破限正文常量（recency 为空）", not getattr(bot, "JAILBREAK_RECENCY", ""))
ck("仓库不携带内置预设常量", not hasattr(bot, "JAILBREAK_PRESETS"))
_ap = io.open("app.py", encoding="utf-8").read()
ck("预设默认值为空列表", '"llm_presets": []' in _ap)
ck("预设由使用者自行导入（不再自动写入）", "自动写入" not in _ap and "内置两套" not in _ap)
cat = {"id": "cat_god", "name": "小猫之神", "tool_name": "game_content", "body": "", "bypass": "", "recency": "", "tfe": "", "desc": "", "tool_desc": "d", "tool_arg_desc": "a"}
whale = {"id": "whale_god", "name": "小鲸之神", "tool_name": "whale_content", "body": "", "bypass": "", "recency": "", "tfe": "", "desc": "", "tool_desc": "d", "tool_arg_desc": "a"}
# ---------- 2. 当前预设的取值 ----------
cfg_cat = {"llm_presets": [cat, whale], "llm_preset_id": "cat_god"}
cfg_whale = {"llm_presets": [cat, whale], "llm_preset_id": "whale_god"}
ck("按 id 取到小猫之神", bot._preset_name(cfg_cat) == "小猫之神")
ck("按 id 取到小鲸之神", bot._preset_name(cfg_whale) == "小鲸之神")
_cfa = {"llm_presets": [{"id": "a", "name": "A", "body": "AAA", "recency": "RA", "tfe": "TA"},
                        {"id": "b", "name": "B", "body": "BBB", "recency": "RB", "tfe": "TB"}], "llm_preset_id": "a"}
ck("导入的 body 跟着预设切", bot._preset_field(_cfa, "body") == "AAA"
   and bot._preset_field({**_cfa, "llm_preset_id": "b"}, "body") == "BBB")
ck("导入的 recency/tfe 跟着预设切", bot._preset_field(_cfa, "recency") == "RA"
   and bot._preset_field({**_cfa, "llm_preset_id": "b"}, "tfe") == "TB")
t1 = bot._preset_tool(cfg_cat)["function"]
t2 = bot._preset_tool(cfg_whale)["function"]
ck("工具定义跟随预设", t1["name"] == "game_content" and t2["name"] == "whale_content", (t1["name"], t2["name"]))
ck("工具 schema 完整", t2["parameters"]["properties"]["content"]["type"] == "string"
   and t2["parameters"]["required"] == ["content"])
ck("空配置不回落任何预设", bot._preset_name({}) not in ("小猫之神", "小鲸之神"))
ck("坏 id 只回落到已有预设（不会凭空造内置）", bot._preset_name({"llm_presets": [cat, whale], "llm_preset_id": "nope"}) in ("小猫之神", "小鲸之神"))
cfg_custom = {"llm_presets": [{"id": "x", "name": "自写", "tool_name": "my_tool",
                               "tool_desc": "d", "tool_arg_desc": "a", "body": "b"}], "llm_preset_id": "x"}
ck("自定义工具名生效", bot._preset_tool(cfg_custom)["function"]["name"] == "my_tool")
ck("自定义 body 生效", bot._preset_field(cfg_custom, "body") == "b")
ck("自定义没填 tfe 时回落内置", bot._preset_field(cfg_custom, "tfe") == "")

# 主体拼装真的用了预设 body
body_cat = bot.assemble_darkside_system_prompt(preset_body=cat["body"])
ck("assemble 用预设 body", cat["body"] in body_cat and len(body_cat) >= len(cat["body"]))
body_w = bot.assemble_darkside_system_prompt(preset_body=whale["body"])
ck("assemble 换预设后内容变了",
   bot.assemble_darkside_system_prompt(preset_body="AAA") != bot.assemble_darkside_system_prompt(preset_body="BBB"))
body_d = bot.assemble_darkside_system_prompt()
ck("不传 body 时不带出任何内置文案", len(body_d.strip()) == 0, len(body_d))

# ---------- 3. bot.py 接线（源码级） ----------
B = io.open("bot.py", encoding="utf-8").read()
ck("tfe 取预设", '_tfe_now = _preset_field(self.config, "tfe") or TFE_SYSTEM_PROMPT' in B)
ck("combined 用 _tfe_now", "combined_system_prompt = _tfe_now" in B)
ck("去重也用 _tfe_now", "darkside_body.startswith(_tfe_now)" in B)
ck("两处 assemble 都传预设 body", B.count('preset_body=_preset_field(self.config, "body")') == 2,
   B.count('preset_body=_preset_field(self.config, "body")'))
ck("bypass 取预设", '_preset_field(self.config, "bypass") or DARKSIDE_BYPASS' in B)
ck("recency 取预设", '_preset_field(self.config, "recency") or JAILBREAK_RECENCY' in B)
ck("工具按预设生成", "_jail_tool = _preset_tool(self.config)" in B)
ck("旧硬编码工具分支已移除", 'if _jt not in ("game_content", "output_text")' not in B)
ck("控制台打印预设+工具", '[破限] 预设「{_preset_name(self.config)}」· 工具 {_jt}' in B)

# ---------- 4. app.py 接口 ----------
A = io.open("app.py", encoding="utf-8").read()
ck("GET /api/presets", '@app.get("/api/presets")' in A)
ck("POST /api/presets", '@app.post("/api/presets")' in A)
for act in ("activate", "delete", "duplicate", "reset"):
    ck("接口支持 %s" % act, '"%s"' % act in A)
ck("配置键 llm_presets", '"llm_presets": []' in A)
ck("配置键 llm_preset_id", '"llm_preset_id": ""' in A)
ck("不预置内置预设（骨架与正文都为空）", bot.preset_builtins() == [] and "_preset_builtins" in A)
ck("保存后同步给 bot", "bot_client.update_config(current_config)" in A)
ck("至少留一套预设", "至少留一套预设" in A)

# 接口逻辑：用假 config 跑一遍 CRUD（不写盘）
class Fake:
    pass


import types
src = A[A.index("def _preset_flush"):A.index('@app.get("/api/mem/insight/{user_id}")')]
ns = {"current_config": {"llm_presets": [], "llm_preset_id": ""}, "save_config": lambda c: None,
      "bot_client": types.SimpleNamespace(update_config=lambda c: None), "print": print,
      "Body": lambda default=None: None, "Dict": dict, "Any": object, "time": __import__("time")}
# 剥掉所有 @app.<method>("...") 装饰器：本片段只做函数级校验，不依赖真实 FastAPI 实例。
# 用通用剥法，新增路由时不会再因为装饰器引用未定义的 app 而 NameError。
_src = re.sub(r"(?m)^@app\.(get|post|delete|put)\([^\n]*\)[ \t\r]*$", "", src)
exec(compile(_src, "x", "exec"), ns)
cur = ns["current_config"]
ps = ns["_preset_list"]()
ck("首次调用不写入任何预设", len(ps) == 0 and not cur.get("llm_preset_id"), (len(ps), cur.get("llm_preset_id")))
import asyncio
r = asyncio.run(ns["presets_post"]({"action": "import", "data": [
    {"name": "小猫之神", "tool_name": "game_content", "body": "", "bypass": "", "recency": "", "tfe": ""},
    {"name": "小鲸之神", "tool_name": "whale_content", "body": "", "bypass": "", "recency": "", "tfe": ""}]}))
_ids = [x["id"] for x in (r.get("presets") or [])]
ck("导入 JSON 的预设可直接使用", r["status"] == "ok" and len(_ids) == 2, r.get("status"))
import asyncio
r = asyncio.run(ns["presets_post"]({"action": "activate", "id": _ids[1]}))
ck("切换生效", r["status"] == "ok" and cur["llm_preset_id"] == _ids[1] and r["tool_name"] == "whale_content", r)
r = asyncio.run(ns["presets_post"]({"action": "duplicate", "id": _ids[1]}))
nid = r["id"]
ck("复制出新预设", r["status"] == "ok" and len(r["presets"]) == 3 and nid)
dup = [p for p in r["presets"] if p["id"] == nid][0]
ck("副本工具名不撞车", dup["tool_name"] == "whale_content_2", dup["tool_name"])
_all = "".join(str(dup.get(k) or "") for k in ("body", "bypass", "recency", "tfe"))
ck("仓库不带文案时副本无可替换（导入用例见第 19 节）", _all.strip() == "", repr(_all[:20]))
r = asyncio.run(ns["presets_post"]({"action": "save", "preset": {"id": nid, "name": "我的破限", "tool_name": "my_jb"}}))
ck("保存改名", r["status"] == "ok" and [p for p in r["presets"] if p["id"] == nid][0]["name"] == "我的破限")
ck("保存不动其他字段", [p for p in r["presets"] if p["id"] == nid][0]["body"] == dup["body"])
r = asyncio.run(ns["presets_post"]({"action": "reset", "id": _ids[1]}))
ck("无出厂文案时恢复出厂被拒（仓库不预置）", r["status"] == "error", r)
r = asyncio.run(ns["presets_post"]({"action": "delete", "id": nid}))
ck("删除生效", r["status"] == "ok" and len(r["presets"]) == 2, r.get("msg"))
r = asyncio.run(ns["presets_post"]({"action": "delete", "id": "cat_god"}))
r2 = asyncio.run(ns["presets_post"]({"action": "delete", "id": "whale_god"}))
ck("最后一两套仍可删（保底至少一套）", r2["status"] == "error", r2)

# ---------- 5. index.html ----------
H = io.open("index.html", encoding="utf-8").read()
ck("页签按钮存在", 'id="tabBtnPreset"' in H and "switchView('preset')" in H)
ck("视图容器存在", 'id="viewPreset"' in H)
ck("视图在 API 配置 之前", H.index('id="viewPreset"') < H.index('id="viewApiConfig"'))
ck("按钮在 API 配置 之前", H.index('id="tabBtnPreset"') < H.index('id="tabBtnApiConfig"'))
ck("views 映射有 preset", "preset: document.getElementById('viewPreset')" in H)
ck("切页签会加载", "tabScopeEnter" in H and "fetchWorldBook" in H and "presetLoad" in H)
for eid in ("presetList", "presetMsg", "presetTitle", "presetEditor", "presetState", "presetName",
            "presetDesc", "presetTool", "presetToolDesc", "presetToolArg", "presetTfe",
            "presetBody", "presetBypass", "presetRecency"):
    ck("元素 %s 有对应输入框" % eid, ('id="%s"' % eid) in H, eid)
for fn in ("presetLoad", "presetSave", "presetActivate", "presetNew", "presetDel", "presetReset", "presetEdit"):
    _has = (("function %s(" % fn) in H) or (("async function %s(" % fn) in H)
    ck("函数 %s 有定义和调用" % fn, _has and (("%s(" % fn) in H))

ck("btns 映射有 preset（否则切进去整页空白）", "preset: document.getElementById('tabBtnPreset')" in H)
ck("页签顺序 控制台->预设->聊天记录",
   H.index('id="tabBtnPreset"') > H.index('id="tabBtnConsole"') and H.index('id="tabBtnPreset"') < H.index('id="tabBtnChat"'))
ck("预设页签没有表情 emoji", "<span>\U0001f3ad</span>" not in H and "\U0001f3ad \u7834\u9650\u9884\u8bbe" not in H)
ck("btns/views 两处 id 都对得上",
   "preset: document.getElementById('viewPreset')" in H and "preset: document.getElementById('tabBtnPreset')" in H)

# ---------- 6. 文风预设 ----------
sb = bot.style_builtins()
ck("内置三套文风", len(sb) == 3, len(sb))
ck("第一套是不指定且空白", sb[0]["id"] == "style_off" and sb[0]["text"] == "")
ck("小说体/口语体有正文", len(sb[1]["text"]) > 40 and len(sb[2]["text"]) > 30)
ck("按 id 取文风", bot._style_name({"style_presets": sb, "style_preset_id": "style_novel"}) == "小说体")
ck("空配置回落不指定", bot._style_field({}, "text") == "")
ck("文风正文能取到", "第三人称" in bot._style_field({"style_presets": sb, "style_preset_id": "style_novel"}, "text"))
ck("bot 注入文风", "_style_enabled(self.config)" in B and "<writing_style>" in B)
ck("控制台打印文风", "[文风] 已套用 %d 套" in B)
ck("清洗会去掉 writing_style 标签", "avoid_words|writing_style" in B)
ck("配置键 style_presets/style_preset_id", '"style_presets": []' in A and '"style_preset_id": ""' in A)
ck("GET 返回文风", '"style_active": sid, "style_presets": sps,' in A)
ck("POST 支持 kind", 'kind = str(payload.get("kind") or "jb")' in A)
ck("POST 支持 blank", 'if act == "blank":' in A)

_cur = ns["current_config"]
_sps = ns["_style_list"]()
ck("首次写入三套文风", len(_sps) == 3 and _cur["style_preset_id"] == "style_off", (len(_sps), _cur.get("style_preset_id")))
_r = asyncio.run(ns["presets_post"]({"action": "activate", "kind": "style", "id": "style_novel"}))
ck("文风切换生效", _r["status"] == "ok" and _cur["style_preset_id"] == "style_novel", _r)
_r = asyncio.run(ns["presets_post"]({"action": "blank", "kind": "style"}))
_sid = _r["id"]
ck("新建空白文风", _r["status"] == "ok" and len(_r["presets"]) == 4 and _sid.startswith("s_"), _r)
ck("空白文风正文为空", [x for x in _r["presets"] if x["id"] == _sid][0]["text"] == "")
_r = asyncio.run(ns["presets_post"]({"action": "save", "kind": "style", "preset": {"id": _sid, "name": "我的文风", "text": "短句"}}))
ck("保存文风正文", [x for x in _r["presets"] if x["id"] == _sid][0]["text"] == "短句")
_r = asyncio.run(ns["presets_post"]({"action": "blank", "kind": "jb"}))
_jid = _r["id"]
_nm = [x for x in _r["presets"] if x["id"] == _jid][0]["tool_name"]
ck("新建空白破限工具名不撞车",
   _nm.endswith("_new") and _nm not in [x.get("tool_name") for x in _r["presets"] if x["id"] != _jid], _nm)
_r = asyncio.run(ns["presets_post"]({"action": "delete", "kind": "style", "id": _sid}))
ck("删文风生效", _r["status"] == "ok" and len(_r["presets"]) == 3, _r.get("msg"))
_r = asyncio.run(ns["presets_post"]({"action": "reset", "kind": "style", "id": "style_novel"}))
ck("文风恢复出厂", _r["status"] == "ok")

# ---------- 7. 文风界面 ----------
ck("界面有文风整块", 'id="styleText"' in H and "文风预设" in H)
ck("破限列表在文风列表上面", H.index('onclick="presetBlank()"') < H.index('onclick="styleBlank()"'))
ck("文风列表在左栏（编辑区之前）", H.index('id="styleList"') < H.index('id="presetEditor"'))
ck("两个编辑区各只有一个", H.count('id="presetEditor"') == 1 and H.count('id="styleEditor"') == 1)
ck("文风编辑区默认收起", 'id="styleEditor" class="hidden' in H)
ck("选中会互斥收起对方", 'EDIT_KIND = "jb"' in H and 'EDIT_KIND = "style"' in H
   and H.count('classList.add("hidden")') >= 2)
ck("打开页签默认停在破限", 'if (EDIT_KIND === "style") {' in H)
ck("破限有新建空白按钮", "presetBlank()" in H)
ck("切页签会一起拉文风", "styleLoad();" in H)
for _eid in ("styleList", "styleMsg", "styleTitle", "styleEditor", "styleState", "styleName", "styleDesc", "styleText"):
    ck("文风元素 %s 存在" % _eid, ('id="%s"' % _eid) in H)
for _fn in ("styleLoad", "styleSave", "styleActivate", "styleBlank", "styleNew", "styleDel", "styleReset", "presetBlank"):
    _hh = (("function %s(" % _fn) in H) or (("async function %s(" % _fn) in H)
    ck("文风函数 %s 有定义和调用" % _fn, _hh and (("%s(" % _fn) in H))


# ---------- 8. 文风多选 + 拖动排序 ----------
ck("bot 有 _style_enabled", "def _style_enabled(cfg" in B)
ck("注入走多套", "_style_enabled(self.config)" in B and '"[文风] 已套用 %d 套：%s"' in B)
ck("不再按单套注入", '_style = _style_field(self.config, "text")' not in B)
ck("配置键 style_enabled", '"style_enabled": []' in A)
ck("GET 返回启用列表", '"style_enabled": list(current_config.get("style_enabled") or []),' in A)
ck("POST 支持 toggle", 'if act == "toggle":' in A)
ck("POST 支持 order", 'if act == "order":' in A)
_cur["style_enabled"] = []
_r = asyncio.run(ns["presets_post"]({"action": "toggle", "kind": "style", "id": "style_novel", "on": True}))
ck("勾选启用", _r["status"] == "ok" and _cur["style_enabled"] == ["style_novel"], _r)
asyncio.run(ns["presets_post"]({"action": "toggle", "kind": "style", "id": "style_chat", "on": True}))
ck("可同时勾多套", _cur["style_enabled"] == ["style_novel", "style_chat"], _cur["style_enabled"])
ck("按列表顺序注入", [n for n, _x in bot._style_enabled(_cur)] == ["小说体", "口语聊天体"],
   [n for n, _x in bot._style_enabled(_cur)])
_r = asyncio.run(ns["presets_post"]({"action": "order", "kind": "style", "ids": ["style_chat", "style_off", "style_novel"]}))
ck("排序生效", [x["id"] for x in _r["presets"]] == ["style_chat", "style_off", "style_novel"],
   [x["id"] for x in _r["presets"]])
ck("注入顺序跟着排序变", [n for n, _x in bot._style_enabled(_cur)] == ["口语聊天体", "小说体"],
   [n for n, _x in bot._style_enabled(_cur)])
asyncio.run(ns["presets_post"]({"action": "toggle", "kind": "style", "id": "style_chat", "on": False}))
ck("取消勾选", _cur["style_enabled"] == ["style_novel"], _cur["style_enabled"])
_r = asyncio.run(ns["presets_post"]({"action": "blank", "kind": "style"}))
_sid3 = _r["id"]
ck("新建空白自动勾上", _r["status"] == "ok" and _sid3 in _cur["style_enabled"], _cur["style_enabled"])
ck("空正文不参与注入", [n for n, _x in bot._style_enabled(_cur)] == ["小说体"],
   [n for n, _x in bot._style_enabled(_cur)])
asyncio.run(ns["presets_post"]({"action": "delete", "kind": "style", "id": _sid3}))
ck("删掉后从启用里移除", _sid3 not in (_cur["style_enabled"] or []), _cur["style_enabled"])
asyncio.run(ns["presets_post"]({"action": "activate", "kind": "style", "id": "style_chat"}))
ck("只启用这一条", _cur["style_enabled"] == ["style_chat"], _cur["style_enabled"])
_cur["style_preset_id"] = "style_novel"
_cur.pop("style_enabled", None)
ns["_style_list"]()
ck("老配置升级自动勾上原来那套", _cur["style_enabled"] == ["style_novel"], _cur["style_enabled"])
_cur["style_enabled"] = []
ns["_style_list"]()
ck("取消后不会被重新勾上", _cur["style_enabled"] == [], _cur["style_enabled"])
for _f in ("styleToggle", "styleDrop", "styleDragStart", "styleDragOver", "styleDragEnd",
           "styleMove", "styleStateText", "styleOrderNow"):
    ck("文风函数 " + _f, ("function " + _f + "(") in H)
ck("列表可拖动", 'draggable="true"' in H and "ondrop=\"styleDrop(event," in H)
ck("有勾选框", 'type="checkbox"' in H and "styleToggle(" in H)
ck("有 STYLE_ENABLED", "STYLE_ENABLED = []" in H and "STYLE_ENABLED = d.style_enabled" in H)
ck("状态行显示注入顺序", "注入顺序：" in H)


# ---------- 9. 收尾状态串不当正文 ----------
ck("识别收尾状态串", bot._looks_like_finish_note(
    "finishReason: STOP finishMessage: Model generated function call(s)."))
ck("正常正文不误伤", not bot._looks_like_finish_note("她抬起头，看了他一眼，没说话。"))
ck("长文本不误伤", not bot._looks_like_finish_note("finishReason " + "x" * 400))
ck("清洗会拦掉收尾状态串",
   bot.sanitize_reply("finishReason: STOP finishMessage: Model generated function call(s).") == "")
ck("清洗正常正文不受影响", bot.sanitize_reply("<game>她抬起头。</game>") == "她抬起头。")
ck("源码里有兜底重试", "_looks_like_finish_note(raw_reply)" in B)
ck("源码里有 function_call 兜底", 'message_obj.get("function_call")' in B)


# ---------- 10. 正文工具名跟着预设走 ----------
import json as _json
_cfg = _json.load(io.open("config.json", encoding="utf-8"))
ck("game_content 认得", bot._is_text_tool_name("game_content", _cfg))
ck("output_text 认得", bot._is_text_tool_name("output_text", _cfg))
_act = [x for x in (_cfg.get("llm_presets") or []) if x.get("id") == _cfg.get("llm_preset_id")]
_an = str((_act[0] if _act else {}).get("tool_name") or "")
ck("当前预设工具名认得", bool(_an) and bot._is_text_tool_name(_an, _cfg))
ck("没见过的名字不认", not bot._is_text_tool_name("something_else", _cfg))
ck("空名字不认", not bot._is_text_tool_name("", _cfg))
ck("源码里按预设名认", "_is_text_tool_name(_fn_name, self.config)" in B)
ck("源码里有参数兜底", "参数里有 content，按正文处理" in B)
ck("日志不再只认两个名字", 'if _src == "output_text":' not in B and "elif _src:" in B)


# ---------- 11. 记忆库裁剪 / 预览口径 / 防截断钉底 ----------
ck("群聊会话显示群名", "get_group_name" in B and "_fill_group_names" in A and "c.group_name ||" in H)
ck("世界书可导出 JSON", '/api/world_book/export' in A and '"qqbotx.worldbook"' in A)
ck("世界书可导入 JSON", '/api/world_book/import' in A and '_wb_fp(' in A)
ck("世界书导入按数据源", '_wb_save(entries)' in A and '_wb_apply(entries)' in A)
ck("世界书导入导出按钮在页面", 'wbExportJSON()' in H and 'wbImportJSON()' in H and 'id="wbImportFile"' in H)
ck("总结单轮上限已调大", "MAX_PER_RUN = 400" in io.open(os.path.join("memory_engine", "generate.py"), encoding="utf-8", errors="replace").read())
ck("裁剪只留 keep_recent", "cut = max(0, len(msgs) - keep_recent)" in B)
ck("裁剪不再受 cursor 限制", "min(cursor, len(msgs) - keep_recent)" not in B)
ck("预览按保留外条数判断", '"will_summarize": len(msgs) >= thr' in A)
ck("预览带 unprotected", '"unprotected": _unprot,' in A)
ck("防截断条目不再贴进 system", not re.search(r'messages\[0\]\["content"\] \+= .{0,10}_preset_field', B))
ck("防截断条目单独成 system", 'messages.append({"role": "system", "content": _bypass})' in B)
_ib = B.index('messages.append({"role": "system", "content": _bypass})')
ck("钉底在末尾硬提醒之后", B.index("_apply_jailbreak_recency(messages)") < _ib)
ck("钉底在调用模型之前",
   _ib < B.index('reply_raw = await self._call_llm_native(messages, enable_notebook_tools=(nb_mode == "inline"))'))
ck("钉底有日志", "[破限] <censorship_bypass> 已作为独立 system 消息钉在请求最底部" in B)


# ---------- 12. 上下文口径：未总结的全带上 ----------
ck("bot 默认不限条数", 'self.config.get("memory_history_limit", 0)' in B)
ck("不限时有 400 兜底", "history = history[-400:]" in B)
ck("app 默认不限", '"memory_history_limit": 0,' in A)
ck("界面默认不限", "const hlKey = isC ? 'claude_context_limit' : 'memory_history_limit';" in H and "(c[hlKey] === undefined ? 0 : c[hlKey])" in H)
ck("界面回读不限", "value || '0', 10" in H)
ck("界面标签说明 0=不限", "0=不限" in H)
_cfg2 = _json.load(io.open("config.json", encoding="utf-8"))
ck("当前配置不限条数", int(_cfg2.get("memory_history_limit") or 0) == 0, _cfg2.get("memory_history_limit"))


# ---------- 13. 群友模式（Claude）同样生效 ----------
_C = io.open("claude_group.py", encoding="utf-8").read()
ck("claude 上下文可设不限", 'self._cfg("context_limit", 0) or 0' in _C)
ck("claude 不限时有 400 兜底", "return msgs[-400:]" in _C)
ck("claude 界面默认不限", "c.claude_context_limit !== undefined ? c.claude_context_limit : 0" in H)
ck("claude 界面回读默认 0", "claude_context_limit: num('claudeContextLimit', 0)" in H)
ck("两个上下文标签都写了 0=不限", H.count("0=不限") >= 2, H.count("0=不限"))
ck("当前 claude 配置不限", int(_cfg2.get("claude_context_limit") or 0) == 0, _cfg2.get("claude_context_limit"))
ck("群聊自动总结走 claude scope", '_fire_auto_summary(uid, sn, "claude")' in B)
ck("手动总结按当前 scope", "await lwb_auto_summary(uid, sender, force, _view_scope)" in A)
ck("手动总结覆盖全部未总结", "unprotected = summarize_n" in A)
ck("手动总结后清空已总结", "0 if force else keep_recent" in A)


# ---------- 14. 外审拦截 ----------
ck("外审识别函数在", "_is_prohibited_note" in B and "_PROHIBITED_RE" in B)
ck("sanitize 也拦外审", "if _is_prohibited_note(text):" in B)
ck("拦截时不重试直接标记", 'last_debug_info["blocked"] = True' in B)
ck("拦截文案", 'reply_text = "被外审拦截"' in B)
ck("拦截那条 hidden 留档", "reply_text, hidden=True" in B)
ck("append 支持 hidden", "hidden: bool = False" in B and 'msg["hidden"] = True' in B)
ck("日志分支有外审", '"prohibited"' in B)
ck("拦截那批不进上下文", 'if not test_mode:\n                if _blocked:' in B)

# ---------- 15. 生图工作流可视化 ----------
ck("可视化按钮在", "wfGraphOpen()" in H)
ck("可视化弹窗在", 'id="wfGraphModal"' in H)
ck("可视化 SVG 在", 'id="wfGraphSvg"' in H)
ck("节点面板在", 'id="wfNodePanel"' in H)
ck("读工作流接口", "'/api/comfy/workflow/' + encodeURIComponent" in H)
ck("存工作流接口", "JSON.stringify({ name: WF_NAME, data: WF_DATA })" in H)
ck("分层布局", "const dep = id =>" in H)
ck("可点选节点", "function wfNodeSelect" in H)
ck("节点参数写回", "node.inputs[k] = v" in H)


# ---------- 16. 外审拦截设置（API 配置页签内） ----------
ck("app 默认 block_action", '"block_action": "none",' in A)
ck("app 默认 block_retry_times", '"block_retry_times": 2,' in A)
ck("bot 读重试设置", 'self.config.get("block_action", "none")' in B and 'block_retry_times' in B)
_CGS = open("claude_group.py", encoding="utf-8", errors="replace").read()
ck("群聊有外审判定", "_is_blocked_note" in _CGS and "_is_prohibited_note" in _CGS)
ck("群聊外审走重试", "[群聊][外审拦截] 按设置重试" in _CGS)
ck("群聊读全局外审设置", '_gcfg.get("block_action", "none")' in _CGS)
ck("主Bot 空回复/无工具受勾选控制", "block_empty_as_blocked" in B)
ck("主Bot 该用工具却没用工具判定", "_no_tool" in B)
ck("外审勾选项有默认值", '"block_empty_as_blocked": False,' in A)
ck("外审勾选框在页面上", 'id="blockEmptyAsBlocked"' in H)
ck("群聊默认启用外审拦截", "_is_blocked_note(reply)" in _CGS)
ck("bot 重试预算进循环", "range(max_rounds + 1 + _blk_retry)" in B)
ck("bot 重试分支", "_blk_try <= _blk_retry" in B)
ck("bot 重试用完仍标记 blocked", 'last_debug_info["blocked"] = True' in B)
ck("重试前有等待", "await asyncio.sleep(0.6)" in B)
ck("界面有处理方式下拉", 'id="blockAction"' in H)
ck("界面有次数输入", 'id="blockRetryTimes"' in H)
ck("界面有保存函数", "function blockSave()" in H)
ck("界面有加载函数", "function blockLoad()" in H)
ck("进 API 配置页会加载", "llmLoadProfiles(); blockLoad();" in H)
ck("设置块在 API 配置页里", H.index('id="viewApiConfig"') < H.index('id="blockAction"'))


# ---------- 17. 生图/表情包标记不进上下文 ----------
ck("有剥离函数", "def _strip_media_markers" in B)
ck("剥 pic 标记", "<pic\\b[^>]*?/?>" in B)
ck("剥 size 标记", "<size\\b" in B)
ck("剥 meme 标记", "<meme\\b" in B)
ck("上下文用剥离后的内容", '_strip_media_markers(str(item.get("content", "")))' in B)
ck("聊天记录里不留标记", 'self.history_store.append(user_id, "assistant", _strip_media_markers(reply_text))' in B)


# ---------- 18. 生图标记不留在任何地方 ----------
ck("存记录时就剥", "_strip_media_markers(reply_text)" in B)
ck("总结前先剥", "from bot import _strip_media_markers as _smm" in A)
ck("总结用剥过的消息", "st.summarize_lwb, uid, _sum_msgs, sender_name" in A)
ck("锚点抽取也用剥过的", "lwb.extract_anchors, uid, _sum_msgs, sender_name" in A)
ck("旧记录靠上下文剥离兜底", '_strip_media_markers(str(item.get("content", "")))' in B)


# ---------- 19. 「Claude 群友模式」改名「群聊模式」 ----------
ck("页签标签是群聊", ">群聊</span>" in H)
ck("界面无 群友模式 字样", "群友模式" not in H)
ck("界面无 Claude群友 字样", "Claude群友" not in H and "Claude 群友" not in H)
ck("数据源标签改成群聊", "'群聊' : '主 Bot'" in H)
ck("后端日志前缀改成群聊", "[群聊]" in B and "[Claude群友]" not in B)
ck("群聊模式文案在", H.count("群聊模式") >= 10)


# ---------- 20. 预设按作用域独立（主 Bot / 群聊模式） ----------
ck("app 有作用域切换", "def _preset_scope_enter" in A and "def _preset_scope_exit" in A)
ck("预设 GET 带 scope", 'async def presets_get(scope: str = ""):' in A)
ck("预设 POST 带 scope", '_preset_scope_enter(str((payload or {}).get("scope") or ""))' in A)
ck("群聊预设键有默认", '"claude_llm_presets": [],' in A and '"claude_style_enabled": [],' in A)
ck("作用域写回后落盘", "current_config[\"claude_\" + k] = current_config.get(k)" in A and "save_config(current_config)" in A)
ck("界面无手动作用域按钮", "presetSetScope('main')" not in H and "presetSetScope('claude')" not in H)
ck("界面请求带 scope", "body.scope = PRESET_SCOPE;" in H and "/api/presets?scope=" in H)
ck("群聊注入自己那套", "def _preset_extras" in _C and "claude_llm_preset_id" in _C)
ck("群聊文风也注入", "claude_style_enabled" in _C and "<writing_style>" in _C)
ck("群聊不读主 Bot 预设", 'cfg.get("claude_llm_preset_id")' in _C)
ck("群聊两档都注入", _C.count("_ex = self._preset_extras()") == 2)


# ---------- 21. 数据源跟随群聊模式 + 破限勾选 ----------
ck("启动按模式定数据源", '_view_scope = "claude" if current_config.get("claude_enabled") else "main"' in A)
ck("保存配置不再重置数据源", 'globals()["_view_scope"]' not in A
   and '_view_scope = "claude" if current_config.get("claude_enabled") else "main"' in A)
ck("启动时数据源默认跟随群聊模式", 'claude_enabled") else "main"' in A)
ck("scope 接口尊重前端选择，只在群聊没开时拦回 main",
   'if sc == "claude" and not current_config.get("claude_enabled"):' in A
   and "_view_scope = sc" in A)
ck("预设页签去掉手动作用域", "presetSetScope('main')" not in H)
ck("预设跟随数据源", "PRESET_SCOPE = (typeof currentScope" in H)
ck("破限有勾选框", "presetToggleOn" in H)
ck("勾选联动总开关", "claude_jailbreak_enabled" in H)
ck("群聊破限受开关控制", 'self._cfg("jailbreak_enabled", True)' in _C)
ck("默认群聊破限开关", '"claude_jailbreak_enabled": True,' in A)


# ---------- 22. 破限勾选可以取消 ----------
_BT = io.open("bot.py", encoding="utf-8", errors="replace").read()
ck("总开关掐掉预设解析", 'if not bool(cfg.get("jailbreak_enabled", True)):' in _BT and "return {}" in _BT)
ck("取消勾选会清当前启用", "llm_preset_id: ''" in H and "claude_llm_preset_id: ''" in H)
ck("勾选状态看总开关", "(p.id === PRESET_ACTIVE && PRESET_ON)" in H)
ck("页面读总开关", "'claude_jailbreak_enabled' : 'jailbreak_enabled'" in H)


# ---------- 23. 识图接口 Path 属性 ----------
ck("vision 状态用 stat().st_mtime", "files[-1].stat().st_mtime" in A)
ck("识图图片列表用 stat().st_mtime", "f.stat().st_mtime" in A)
ck("没有裸 .st_mtime", len(re.findall(r"(?<!\\)\\.st_mtime", A)) == 0)


# ---------- 24. 表情包位置：模型显式指定优先，其余按精力/情绪权重随机 ----------
_SSx = io.open("sticker_steal.py", encoding="utf-8", errors="replace").read()
_BTx = io.open("bot.py", encoding="utf-8", errors="replace").read()
_Cx = io.open("claude_group.py", encoding="utf-8", errors="replace").read()
ck("标记按位置定前后", 'attrs["pos"] = "before" if not s[:m.start()].strip() else "after"' in _SSx)
ck("显式 pos 也认", '"after", "back", "post", "tail"' in _SSx)
ck("规则里教了前后", "发在正文前面还是后面" in _SSx)
ck("规则里说明了显式指定优先", "你想强制位置就直接写" in _SSx)
ck("私聊分前后两拨", "_meme_before" in _BTx and "if _mb:" in _BTx)
ck("私聊先发正文前的", 'await self.send_private_msg(user_id, "", images=_meme_before)' in _BTx)
ck("群聊返三值", "return clean, _b, _a" in _Cx)
ck("群聊分前后两拨", "_memes_b" in _Cx and "_memes_a" in _Cx)
ck("群聊先发正文前的", 'await self.send_group_msg(int(group_id), "", images=_memes_b)' in _Cx)


# ---------- 25. 识图模型块位置 ----------
_AP = H.split('id="viewApiConfig"')[1].split("</main>")[0]
ck("两张卡片各自独立", 0 < _AP.find("外审拦截") < _AP.find("主对话 API 配置") < _AP.find("表情包识图模型（独立）"))
ck("识图块在主对话卡片之后", _AP.find("bg-card border border-borderDark rounded-xl p-5 shadow-lg") < _AP.find("表情包识图模型（独立）"))
ck("识图块在 LLM 字段之后", _AP.find("<!-- LLM Base URL -->") < _AP.find("表情包识图模型（独立）"))
ck("识图块在 API 页内", 0 < _AP.find("表情包识图模型（独立）"))
ck("API 页可滚动", 'id="viewApiConfig"' in H and "overflow-y-auto" in H.split('id="viewApiConfig"')[1][:200])
ck("魂按钮是蓝色圆形", "bg-blue-600" in H.split('id="tabBtnSoul"')[1][:260] and "rounded-full" in H.split('id="tabBtnSoul"')[1][:260])
ck("魂按钮在页签行内且不会被刷成绿色方块", "ml-5 w-9 h-9 shrink-0 rounded-full bg-blue-600" in H and "if (k === 'soul')" in H)
ck("魂页签是左栏列表 + 右栏配置", 'id="soulNavDebounce"' in H and 'id="soulPaneDebounce"' in H and "grid-cols-[240px_1fr]" in H)
ck("魂页签左栏切换函数在", "function soulNav(" in H and "soulNav('debounce')" in H)
ck("魂页签两栏都不再是旧的两列卡片", "lg:grid-cols-2" not in H.split('id="viewSoul"')[1].split("</main>")[0])
ck("魂页签左栏有「消息分段发送」", 'id="soulNavSegment"' in H and ">消息分段发送<" in H)
ck("魂页签右栏有分段面板", 'id="soulPaneSegment"' in H and 'id="sgEnabled"' in H and 'id="sgTestOut"' in H)
ck("分段规则有 3 档且可扩展", H.count('id="sgRule"') == 1 and "按换行分割" in H and "按空行分割" in H and "按句子分割" in H)
ck("分段面板字段齐全", all(('id="%s"' % k) in H for k in ["sgScope", "sgRule", "sgMinLen", "sgBaseMs", "sgPerCharMs", "sgJitter", "sgMinMs", "sgMaxMs", "sgDebug", "sgTestInput"]))
ck("分段 JS 在", "function sgFill(" in H and "function sgSave(" in H and "function sgTest(" in H)
_DP = os.path.dirname(os.path.abspath(__file__))
try:
    _SEG = io.open(os.path.join(_DP, "segments.py"), encoding="utf-8", errors="replace").read()
    _BOT = io.open(os.path.join(_DP, "bot.py"), encoding="utf-8", errors="replace").read()
    _CG = io.open(os.path.join(_DP, "claude_group.py"), encoding="utf-8", errors="replace").read()
    _APP = io.open(os.path.join(_DP, "app.py"), encoding="utf-8", errors="replace").read()
    _SSTEAL = io.open(os.path.join(_DP, "sticker_steal.py"), encoding="utf-8", errors="replace").read()
except Exception:
    _SEG = _BOT = _CG = _APP = _SSTEAL = ""
ck("segments.py 有规则注册表与延时算法", "SPLIT_RULES" in _SEG and "def register_rule(" in _SEG and "def delay_ms(" in _SEG and "def send_segmented(" in _SEG)
ck("segments.py 不碰任何记录/上下文", all(x not in _SEG for x in ["history.append", "add_message", "save_message", "memory.", "notebook."]))
ck("私聊已接分段发送", "segments.send_segmented(" in _BOT and 'scope="private"' in _BOT)
ck("群聊已接分段发送", "segments.send_segmented(" in _CG and 'scope="group"' in _CG)
ck("app.py 有分段配置与预览接口", '"seg_enabled"' in _APP and "/api/segments/test" in _APP)
ck("已收到图片上限默认 120", '"vision_store_max": 120' in _APP and "limit: int = 0" in _APP and "cap = int(current_config.get" in _APP)
ck("超上限自动删老图", "def _vision_prune" in _APP and "files[int(cap):]" in _APP)
ck("已收到图片可删除 / 可入库", "/api/vision/images/delete" in _APP and "/api/vision/images/to-library" in _APP)
ck("已收到图片有大图弹窗与退出按钮", 'id="visionViewModal"' in H and "visionViewClose()" in H and "退出大图" in H)
ck("已收到图片卡片有删/入库按钮", "visionToLib(" in H and "visionDel(" in H and "visionView(" in H and 'id="visionViewModal"' in H)
ck("两个图墙都能滚动", H.count("overflow-y") >= 2
   and 'id="visionImgGrid"' in H and 'id="stkLibGrid"' in H)
ck("已收到图片网格不被压扁", "grid-auto-rows:min-content" in H and "aspect-ratio:4/3" in H)
ck("已收到有上限输入框与保存", 'id="visionStoreMax"' in H and "function visionStoreSave(" in H)
ck("已收到→入库会走识图", "await st.upload(p.name, data, analyze=True)" in _APP)
ck("重新识图直接落库", "/api/sticker/library/reanalyze" in _APP and "def apply_analysis" in _SSTEAL and "reanalyzed_at" in _SSTEAL)
ck("前端重新识图调新接口并刷新", "library/reanalyze" in H and "await stkLoadLib();" in H)
try:
    _SEC = io.open(os.path.join(_DP, "secretary.py"), encoding="utf-8", errors="replace").read()
    _CG2 = io.open(os.path.join(_DP, "claude_group.py"), encoding="utf-8", errors="replace").read()
except Exception:
    _SEC = _CG2 = ""
ck("秘书网关模块在（复读/关键词/权重阈值/时机）", all(x in _SEC for x in ["def detect_repeat(", "def hit_keyword(", "def threshold(", "sec_yes_weight", "sec_mood_threshold", "async def gate("]))
ck("秘书网关阈值 = No/(Yes+No)", "return no / (yes + no)" in _SEC and "PROMPT" in _SEC)
ck("群聊消息入口接秘书网关", "self.secretary = SEC.create_secretary(" in _CG2 and "await self.secretary.gate(group_id, text, _rec, mentioned=_mentioned" in _CG2)
ck("复读/沉默/放行三条分支都在", all(x in _CG2 for x in ['_act == "repeat"', '_act == "silence"', '_act in ("direct", "reply")']))
ck("app.py 有秘书网关配置与接口", '"sec_enabled"' in _APP and "/api/secretary/status" in _APP and "/api/secretary/test" in _APP)
ck("控制台秘书网关占位卡已删", "秘书网关（占位：功能待接入）" not in H)
ck("群聊右栏有秘书网关卡", 'id="secEnabled"' in H and "\U0001f6ce\ufe0f 秘书网关" in H)
ck("API 配置有秘书网关 API 卡", 'id="secApiBase"' in H and 'id="secApiModel"' in H and "\U0001f6ce\ufe0f 秘书网关 API" in H)
ck("秘书网关前端函数齐", all(("function " + f + "(") in H for f in ["secFill", "secSave", "secTest", "secApiSave", "secApiTest", "secShowThreshold"]))
ck("两张 API 卡并排", "grid grid-cols-1 xl:grid-cols-2 gap-4 items-start" in H and "max-w-3xl" not in H.split('id="stkVisionBase"')[1][:400])
ck("两张卡都能拉模型列表下拉", H.count("probeModels(") >= 3 and H.count("获取可用模型列表") >= 2 and "function probeModels(" in H and "function pickModel(" in H)
ck("API 配置页回填识图/秘书配置", "if (name === 'apiconfig') { llmLoadProfiles(); blockLoad(); stkLoad(); secLoad(); }" in H)
ck("被艾特直接放行", "mentioned: bool = False" in _SEC and "被艾特了，直接交给主对话" in _SEC and "mentioned=_mentioned" in _CG2 and "self_id" in _CG2)
_PK = io.open(os.path.join(_DP, "poke.py"), encoding="utf-8", errors="replace").read()
_AM = io.open(os.path.join(_DP, "active_msg.py"), encoding="utf-8", errors="replace").read()
ck("主动消息模块", "def extract" in _AM and "def reschedule_missed" in _AM and "def prompt_block" in _AM and "def mark_done" in _AM)
ck("主动消息接入", "_active_msg_loop" in _BOT and "active_msg" in _CG2 and "api/active_msg" in _APP and "active_msg_shift" in _APP)
ck("主动消息前端", "soulPaneAm" in H and "function amFill(" in H and "amClearDone" in H)
ck("模型主动戳人", "def extract_active" in _PK and "def active_ok" in _PK and "active_prompt_block" in _BOT and "poke_active_enabled" in _APP and "pkActive" in H)
_IDL = io.open(os.path.join(_DP, "idle.py"), encoding="utf-8", errors="replace").read()
ck("闲置触发模块", "def should_fire" in _IDL and "def rate_now" in _IDL and "def pick_mode" in _IDL and "idle_prompt_recall" in _IDL)
ck("闲置触发接入", "_idle_loop" in _BOT and "def _fire_idle" in _BOT and "chat_recall_hit" in _BOT and "chat_recall_hit" in _CG2 and "api/idle" in _APP)
ck("闲置触发前端", "soulPaneIdle" in H and "function idFill(" in H and "idPromptRecall" in H)
_MD = io.open(os.path.join(_DP, "mood.py"), encoding="utf-8", errors="replace").read()
ck("情绪注入模块", "def prompt_block" in _MD and "def extract" in _MD and "emotion" in _MD)
ck("情绪注入接入", "_MO.prompt_block" in _BOT and "_MO3.extract" in _CG2 and "api/mood" in _APP)
ck("情绪注入前端", "soulPaneMood" in H and "function mdFill(" in H and "mdPrompt" in H)
ck("戳一戳模块", "def take" in _PK and "def add" in _PK and "poke_private" in _PK and "poke_group" in _PK)
ck("戳一戳接入", "_on_notice" in _BOT and 'notice_type == "poke"' in _BOT and 'poke as _POKE2' in _BOT and "_POKE.take" in _CG2 and "poke_enabled" in _APP)
ck("戳一戳前端", "soulPanePoke" in H and "soulNavPoke" in H and "function pkFill(" in H and "function pkSave(" in H)
ck("反戳与跟戳", "def decide_back" in _PK and "def decide_follow" in _PK and "def record_out" in _PK and "send_poke" in _BOT and "poke_back_rate" in _APP and "poke_follow_rate" in _APP)
ck("反戳跟戳前端", "pkBack" in H and "pkFollow" in H and "poke_back_rate" in H and "poke_follow_cooldown" in H)
ck("魂页签两栏都不再是旧的两列卡片", "lg:grid-cols-2" not in H.split('id="viewSoul"')[1].split("</main>")[0])
ck("页签行整条居中（页头 relative + flex 居中 + Logo 绝对定位）", "relative flex-shrink-0 h-16" in H and "px-6 flex items-center justify-center z-20" in H and "absolute left-6 top-1/2 -translate-y-1/2" in H and "-translate-x-12 flex items-center bg-terminal" in H)
ck("魂页签存在", 'id="viewSoul"' in H)
ck("消息防抖卡片在魂页签内", 'id="dbEnabled"' in H.split('id="viewSoul"')[1].split("</main>")[0])
ck("防抖字段齐全", all(k in H for k in ["dbEnabled", "dbScope", "dbJudge", "dbThreshold", "dbTimeout", "dbMinChars", "dbCancel", "dbDebug", "dbTestInput", "dbTestOut", "dbState"]))
ck("页签栏往左移了（-translate-x-12）", "-translate-x-12 flex items-center bg-terminal" in H)
ck("switchView 认识 soul", "if (name === 'soul') soulLoad();" in H)
ck("soulLoad/soulSave/soulTest 都在", all(k in H for k in ["function soulLoad", "function soulSave", "function soulTest"]))
ck("后端有防抖引擎文件", os.path.exists(os.path.join(os.path.dirname(os.path.abspath(__file__)), "debounce.py")))
ck("bot.py 已接防抖", "from debounce import Debouncer" in io.open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "bot.py"), encoding="utf-8").read())
ck("app.py 有防抖配置键", "debounce_enabled" in io.open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "app.py"), encoding="utf-8").read())
_MD = os.path.join(os.path.dirname(os.path.abspath(__file__)), "debounce_model")
ck("自带小模型文件已内置", os.path.exists(os.path.join(_MD, "model.onnx")) and os.path.exists(os.path.join(_MD, "vocab.txt")))
try:
    import debounce_model as _DM
    _p1 = _DM.predict_send("在吗")
    _p2 = _DM.predict_send("如果明天不下雨")
    ck("自带小模型可推理且判断合理", _p1 is not None and _p1 > 0.5 and _p2 is not None and _p2 < 0.5)
except Exception as _e:
    ck("自带小模型可推理且判断合理", False)
_D = io.open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "debounce.py"), encoding="utf-8").read()
_A = io.open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "app.py"), encoding="utf-8").read()
ck("debounce.py 判断器默认自带小模型", '"debounce_judge": "local"' in _D and "def local_score(" in _D)
ck("debounce.py 判断器三分支", 'mode == "local"' in _D and "asyncio.to_thread(local_score" in _D)
ck("app.py 判断器默认自带小模型", '"debounce_judge": "local"' in _A)
ck("app.py 测试接口返回小模型分数", '"local": lo' in _A)
ck("魂页签判断器两档且无 Ollama 档", H.count('<option value="local">项目自带小模型（内置，推荐）</option>') == 1 and H.count('<option value="builtin">内置规则（纯关键词，最省）</option>') == 1 and 'value="model"' not in H and "dbBaseUrl" not in H)

ck("main 容器成对", H.count('id="viewPreset"') == 1 and H.count("presetEditor") >= 2)

# JS 语法检查
_KL = io.open(os.path.join(_DP, "keepalive.py"), encoding="utf-8", errors="replace").read()
ck("登录二维码：keepalive 提供本地二维码读取", "QR_PNG" in _KL and "def login_qr():" in _KL and "def read_login_qr():" in _KL)
ck("登录二维码：status() 带出二维码元信息", '"qr": login_qr(),' in _KL)
ck("登录二维码：app.py 回传二维码与登录阶段",
   '/api/napcat/qr' in _APP and '/api/napcat/login_status' in _APP and "_KA.read_login_qr" in _APP)
ck("新设备验证：弹窗与前端函数齐备",
   'id="captchaStep2"' in H and 'id="captchaStep2Open"' in H and "function kaShowStep2(" in H
   and "async function kaWatchLogin(" in H and "id=\"qrNote\"" in H)
ck("新设备验证：接住 needNewDevice 与二维码解码URL",
   "d.need_new_device && d.jump_url" in H and "decMatch" in H and "kaWatchLogin" in H)
ck("验证码回传：captcha_login 接受前端传来的 sid/uin/url",
   'def captcha_login(ticket, randstr, sid="", uin="", url=""):' in _KL and "def _cap_sid(" in _KL)
ck("验证码回传：app.py 把 sid/uin/url 透传给 captcha_login",
   'sid=body.get("sid")' in _APP and 'uin=body.get("uin")' in _APP and 'url=body.get("url")' in _APP)
ck("验证码回传：前端提交 sid/uin/url",
   "randstr: r.randstr, sid: sid, uin: uin, url: latestCaptchaUrl" in H)
ck("验证码回传：链接里没 sid 时交给后端缓存兜底",
   "const sid = capSrv.sid || p.sid || ''" in H)
ck("保活：进程在但账号未登录也判掉线（带人工验证保护）",
   "def _manual_verify_pending()" in _KL and "if _manual_verify_pending() or _verify_words(" in _KL and "登录态已失效" in _KL)
ck("保活：NapCat 说等人工验证时不判掉线（不反复撞风控）",
   "VERIFY_WORDS" in _KL and '_verify_words(ls.get("error"))' in _KL)
ck("保活：验证/扫码弹窗期间发心跳，不误重启",
   "/api/napcat/verify_busy" in _APP and "def mark_verify_busy()" in _KL and "kaKeepBusy(" in H)
ck("NapCat 实例：启动前清残留 + 停止杀进程树 + 防并发重复启动",
   "def kill_napcat_procs(" in _KL and "taskkill" in _KL and "_napcat_starting = True" in _APP
   and "_KA.kill_napcat_procs" in _APP)
ck("NapCat 实例：WebUI 端口跟随实际实例",
   "def write_live_webui(" in _KL and "LIVE_JSON" in _KL and "_KA.write_live_webui(" in _APP)
ck("NapCat 实例：保活重启走同一个被跟踪的启停入口",
   "_KA.set_process_hooks(_ka_restart, _ka_stop)" in _APP and "def _ka_restart()" in _APP)
ck("二维码：验证后自动弹出真二维码",
   "async function kaMaybeShowQr(" in H and "await kaMaybeShowQr();" in H and "X-Qr-Mtime" in H)
ck("NapCat 进程清理：子进程调用不会被句柄问题静默吞掉", _KL.count("stdin=subprocess.DEVNULL") >= 2)
ck("NapCat 进程清理：失败不再静默假成功", "清理 NapCat 进程失败" in _KL and "failed.append" in _KL)
ck("启动钩子没挂错函数（on_event startup 紧贴 _ka_boot）",
   re.search(r'@app\.on_event\("startup"\)[ \t]*\nasync def _ka_boot\(\):', _APP) is not None)
ck("接口装饰器各自贴对自己的函数（login_status / verify_busy）",
   re.search(r'@app\.get\("/api/napcat/login_status"\)[ \t]*\nasync def napcat_login_status\(\):', _APP) is not None
   and re.search(r'@app\.post\("/api/napcat/verify_busy"\)[ \t]*\nasync def napcat_verify_busy\(\):', _APP) is not None)
try:
    import json as _json, subprocess as _sp
    _code = ("import app,json;print(json.dumps([[getattr(r, 'path', ''), "
             "getattr(getattr(r, 'endpoint', None), '__name__', '?')] for r in app.app.routes]))")
    _r = _sp.run([sys.executable, "-c", _code], stdin=_sp.DEVNULL, capture_output=True, text=True,
                 encoding="utf-8", errors="replace", timeout=240,
                 cwd=os.path.dirname(os.path.abspath(__file__)))
    _map = dict(_json.loads(_r.stdout.strip().splitlines()[-1]))
    _want = {"/api/napcat/login_status": "napcat_login_status", "/api/napcat/verify_busy": "napcat_verify_busy",
             "/api/napcat/captcha": "napcat_captcha", "/api/napcat/qr": "napcat_qr",
             "/api/napcat/start": "start_napcat", "/api/napcat/stop": "stop_napcat"}
    _bad = {k: _map.get(k) for k, v in _want.items() if _map.get(k) != v}
    ck("运行时路由体检：接口指向正确的处理函数", not _bad, str(_bad))
except Exception as _e:
    ck("运行时路由体检：接口指向正确的处理函数", False, repr(_e)[:150])
ck("NapCat 输出回放接口已就位（刷新页面也能看到 NapCat 输出）",
   '@app.get("/api/napcat/console_tail")' in _APP and "napcat_console_tail_api" in _APP)
ck("点「启动 NapCat」会显示后端返回的消息，不再静默",
   "data.status === 'warn' ? 'WARN' : 'INFO'" in H)
ck("NapCat 已在运行时前端会提示并回放输出",
   "NapCat 进程已在运行（如需重启" in H and "async function napcatReplayConsole(" in H
   and "napcatReplayConsole();" in H)
ck("启动中标记带超时与 finally 兜底，不会永久卡住",
   "_napcat_starting_at" in _APP and "time.time() - _napcat_starting_at" in _APP)
ck("左上角图标已改为 QQ星",
   'QQ<span class="text-emerald-400 logo-cn">星</span>' in H
   and 'QQbot<span class="text-emerald-400">X</span>' not in H)
ck("右上角设置齿轮已就位（正方形、不参与页签样式切换）",
   'id="tabBtnSettings"' in H and "if (k === 'settings')" in H)
ck("设置视图容器已注册进 switchView",
   'id="viewSettings"' in H and "settings: document.getElementById('viewSettings')" in H)

mm = list(re.finditer(r"<script[^>]*>", H))
j = mm[-1]
js = H[j.end():H.rfind("</script>")]
# ---------- 14.9 群聊模式「被@ / 被回复」直接放行：两种消息格式都要认 ----------
# NapCat 的 messagePostFormat 配的是 string，message 是一整串 CQ 码而不是数组段；
# 只按数组遍历的话这两个判定永远为假，「被@/被回复直接放行」等于没接。
_CGS = io.open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "claude_group.py"),
               encoding="utf-8", errors="replace").read()
ck("群聊有 CQ 码通用解析", "def _cq_all_segs" in _CGS)
ck("群聊有统一消息段解析", "def _msg_segments" in _CGS)
ck("群聊被@判定支持两种格式", "def _detect_mentioned" in _CGS)
ck("群聊被回复判定支持两种格式", "async def _detect_replied" in _CGS)
ck("被@判定已接入 handle_group_event", "self._detect_mentioned(data, _self_id)" in _CGS)
ck("被回复判定已接入 handle_group_event", "await self._detect_replied(data, _self_id)" in _CGS)
ck("旧数组遍历写法已清除", 'for _sg in (data.get("message") or []):' not in _CGS)
ck("message 是 CQ 串时也当 raw 用", "raw = msg if isinstance(msg, str) and msg.strip()" in _CGS)

# ---------- 14.10 表情包主动发送：独立成条 + 精力/情绪极性权重随机 ----------
# 表情包不再跟正文挤在同一条里；发在文字前还是后，按「精力值 + 表情包情绪极性」掷随机。
# 模型显式写了 pos= 的仍然听模型的（pos_explicit）。
_HERE = os.path.dirname(os.path.abspath(__file__))
_SS = io.open(os.path.join(_HERE, "sticker_steal.py"), encoding="utf-8", errors="replace").read()
_APP2 = io.open(os.path.join(_HERE, "app.py"), encoding="utf-8", errors="replace").read()
ck("表情包有情绪极性判定", "def polarity_of" in _SS)
ck("表情包有后置概率公式", "def pos_prob_after" in _SS)
ck("表情包有时机掷随机", "def decide_pos" in _SS)
ck("后置概率含精力项", "sticker_pos_energy" in _SS)
ck("后置概率含情绪极性项", "sticker_pos_polar" in _SS)
ck("正负情绪词表都在", "DEFAULT_POS_EMOS" in _SS and "DEFAULT_NEG_EMOS" in _SS)
ck("显式 pos 才优先（pos_explicit）", 'attrs["pos_explicit"] = True' in _SS
   and 'attrs["pos_explicit"] = False' in _SS)
ck("群聊已接入时机随机", "_SS.decide_pos(" in _CGS)
ck("群聊 after 表情包单独发", "张表情包（正文之后）" in _CGS)
ck("群聊有独立发送开关", "sticker_send_separate" in _CGS)
ck("私聊已接入时机随机", "CSS.decide_pos(" in B)
ck("私聊 after 表情包单独发", "私聊再发 {len(_meme_imgs)} 张（正文之后）" in B)
ck("私聊有独立发送开关", "sticker_send_separate" in B)
ck("配置默认值已加", '"sticker_send_separate": True' in _APP2
   and '"sticker_pos_random": True' in _APP2)
ck("配置白名单已加", '"sticker_send_separate", "sticker_pos_random"' in _APP2)
ck("前端有独立发送开关", 'id="stkSendSeparate"' in H)
ck("前端有权重随机开关", 'id="stkPosRandom"' in H)
ck("前端有精力值输入", 'id="stkEnergyNow"' in H)
ck("前端有试掷按钮", "function stkTestPos()" in H)
ck("前端保存带新字段", "sticker_send_separate:" in H and "sticker_pos_pos_emos:" in H)

# ---------- 14.11 群聊呼唤模式：被@/被回复后保持在线 N 分钟 ----------
# 被叫到之后这个群进入在线状态，期间消息不经秘书模型、防抖后直接放给主对话；
# 每条新消息把计时续满，冷场满 N 分钟自动退出。复读检测优先级在它之前。
_SEC = io.open(os.path.join(_HERE, "secretary.py"), encoding="utf-8", errors="replace").read()
ck("呼唤模式有窗口换算", "def _call_window" in _CGS)
ck("呼唤模式有倒计时", "def _call_left" in _CGS)
ck("呼唤模式有续命", "def _call_touch" in _CGS)
ck("入口接了呼唤模式", "_in_call = False" in _CGS and "in_call=_in_call" in _CGS)
ck("被@/被回复时进入在线", "分钟在线状态" in _CGS)
ck("在线期间每条消息续命", "这条直接放行、计时续满" in _CGS)
ck("秘书 gate 支持 in_call", "in_call: bool = False" in _SEC)
ck("在线时直接放行不叫模型", "呼唤模式在线，直接交给主对话" in _SEC)
ck("复读优先于呼唤模式（放行在复读之后）",
   _SEC.index("呼唤模式在线，直接交给主对话") > _SEC.index("detect_repeat(list(_rep_src"))
ck("呼唤模式配置默认值已加",
   '"claude_call_mode_minutes": 5' in _APP2 and '"claude_call_mode_enabled": True' in _APP2)
ck("前端有呼唤模式开关", 'id="secCallEnabled"' in H)
ck("前端有分钟数输入", 'id="secCallMinutes"' in H)
ck("前端保存带呼唤模式字段", "claude_call_mode_minutes:" in H and "claude_call_mode_enabled:" in H)

# ---------- 14.24 数据源选择留档 ----------
ck("手动切数据源会留档", "localStorage.setItem('qqbotx_scope_manual', next);" in H)
ck("下拉切数据源也留档", "if (v === 'main' || v === 'claude') localStorage.setItem('qqbotx_scope_manual', v);" in H)
ck("群聊开关不删留档", "localStorage.removeItem('qqbotx_scope_manual')" not in H)
ck("群聊开关不顶掉手动选择", "if (_scopeManual !== 'main' && _scopeManual !== 'claude') {" in H)
ck("页面加载以留档为准", "if (_m === 'main' || _m === 'claude') {\n          scopePaint(_m);" in H)

ck("验证弹窗不用老年模式", "enableAged: true" not in H and "enableAged: false" in H)

ck("验证链接完整内容入日志", "proofWaterUrl 完整内容" in io.open("keepalive.py", encoding="utf-8", errors="replace").read())
ck("验证参数入日志", "[调试] 验证参数" in H)
ck("sid 有后端兜底", "capSrv = ((ks && (ks.data || ks)) || {}).captcha || {};" in H)

ck("验证参数以后端会话为准", "const aid = capSrv.aid || p.aid" in H)
ck("验证链接会纠正成后端那条", "验证链接已纠正为后端保存的那条" in H)
ck("后端保存 aid/login_appid", "_qp(\"aid\"), \"login_appid\": _qp(\"login_appid\")" in io.open("keepalive.py", encoding="utf-8", errors="replace").read())
ck("腾讯组件用纠正后的参数", "login_appid: loginAppid, uin: uin, sid: sid," in H)

ck("二维码弹窗有解码链接区块", "qrDecWrap" in H and "qrDecUrl" in H)
ck("解码链接可复制", "function copyQrDecUrl()" in H)
ck("自动弹码时也画解码链接", "kaPaintQrDec(latestQrUrl);" in H)
ck("刷新二维码弹窗也画解码链接", H.count("kaPaintQrDec(latestQrUrl);") >= 2)

ck("验证后跟进不依赖弹窗", "if (typeof kaWatchLogin === 'function') kaWatchLogin('人工验证');" in H)
ck("关弹窗时提示会盯登录状态", "已开始盯登录状态" in H)

ck("点验证前现取新鲜会话", "/api/keepalive/refresh_captcha" in H)
ck("后端有 refresh_captcha", "def refresh_captcha():" in io.open("keepalive.py", encoding="utf-8", errors="replace").read())
ck("后端有 refresh_captcha 接口", "/api/keepalive/refresh_captcha" in io.open("app.py", encoding="utf-8", errors="replace").read())
ck("aid 误报已修", "没拿到真实的 aid" in H)
ck("新设备二维码读 str_url", "d.get(\"str_url\")" in io.open("keepalive.py", encoding="utf-8", errors="replace").read())

ck("只有 safe/verify 才算新设备验证", "indexOf('safe/verify') >= 0" in H)
ck("风控时也盯登录状态", "腾讯要求「身份验证」" in H)
ck("验证失败也盯登录状态", "NapCat 若回退到扫码登录，二维码会自动弹出" in H)

ck("链接验证有主动跟进", "function kaWatchLinkVerify()" in H)
ck("链接验证会重试登录", "链接验证已生效" in H)
ck("关弹窗时启动链接验证跟进", "kaWatchLinkVerify();" in H)

ck("点群聊页签同步留档（防刷新循环）", "点「群聊」页签本身也是一次手动切数据源" in H)
ck("scopeLoad 一致时不重复切", "服务端已经是这个数据源就不用再切一次" in H)

try:
    _BR = io.open("memory_engine/bridge.py", encoding="utf-8", errors="replace").read()
except Exception:
    _BR = ""
ck("记忆库页签有独立数据源切换", 'id="memScopeMain"' in H and "memScopeSet" in H)
ck("运行参数含总结阈值/保留最近/自动总结/L0", all(k in H for k in ('id="lwbOptThreshold"', 'id="lwbOptKeep"', 'id="lwbOptAuto"', 'id="lwbOptL0"')))
ck("运行参数标注当前数据源", 'id="memSettingsScope"' in H and 'id="lwbOptScope"' in H)
ck("记忆参数区存在", 'id="memCrossInject"' in H)
ck("跨会话白名单可增删", "memCrossAdd" in H and "memCrossDel" in H)
ck("跨会话召回接入桥接层", "extra_user_ids" in _BR and "other_user_ids" in _BR)
ck("跨会话白名单已挂到 MemoryStore", '"search", "other_user_ids"' in _BR)
ck("主 Bot 跨会话配置项", '"memory_cross_inject"' in A)
ck("群聊跨会话配置项", '"claude_memory_cross_inject"' in A)
ck("跨会话召回在两条链路都接入", "memory_cross_allow" in B and "memory_cross_allow" in _CGS)

ck("口径说明已并入运行参数区", 'id="lwbLogicNote"' in H)
ck("搜索接口也走跨会话（可现场验证）", "_cross_ids(" in A and "cross_scope_ids" in A)
ck("数据源三档辅助已就位", "def _scope_pair" in A and "def _claude_stores" in A)
ck("记忆库接口支持 scope", 'get_memory_overview(scope: str = "")' in A)
ck("笔记本接口支持 scope", 'notebook_items(kind: str = "", scope: str = "")' in A)
ck("笔记本跨对话设置接口", "/api/nb/options" in A and "def _nb_cross_on" in A)
ck("记忆库页签有「全部」档", 'id="memScopeAll"' in H)
ck("记忆库数据源有留档", "qqbotx_mem_scope" in H)
ck("笔记本页签有数据源三档", 'id="nbScopeAll"' in H and "qqbotx_nb_scope" in H)
ck("笔记本参数区已加", "toggleNbCross" in H and "saveNbCross" in H)
ck("群聊笔记本跨对话已接入", "claude_notebook_cross_inject" in _CGS)
ck("主 Bot 笔记本跨对话已接入", "notebook_cross_inject" in B)
ck("切换条「全部」在最左", H.find('id="memScopeAll"') < H.find('id="memScopeMain"') < H.find('id="memScopeClaude"'))
ck("笔记本切换条「全部」在最左", H.find('id="nbScopeAll"') < H.find('id="nbScopeMain"') < H.find('id="nbScopeClaude"'))
ck("统计格只剩两格", '已记录事件' in H and '待总结消息' in H and '已总结消息' not in H)
ck("全部档收起参数区", "function memScopeParamsVisible" in H and "nbCrossBtn" in H)
ck("笔记本详情按数据源取", 'notebook_item(nid: str, scope: str = "")' in A)
ck("笔记本全部档只读", "合并视图（只读）" in H)
ck("聊天记录默认合并两源", 'get_history_list(scope: str = "all")' in A)
ck("聊天详情支持 scope", 'get_history_detail(user_id: str, scope: str = "")' in A)
ck("聊天列表一次拉全部", "fetch('/api/history?scope=all')" in H)
ck("聊天会话带来源徽章", "currentConvScope" in H)
ck("世界书页签自带数据源", 'id="wbScopeMain"' in H and 'id="wbScopeClaude"' in H)
ck("预设页签自带数据源", 'id="presetScopeMain"' in H and 'id="presetScopeClaude"' in H)
ck("顶部数据源按钮已移除", 'id="scopeBadge"' not in H)
ck("记忆库统计不再引用已删元素", "memStatSummarized" not in H)
ck("switchView 为 async", "async function switchView" in H)
ck("搜索接口返回来源诊断", "cross_dbg" in A)
ck("切数据源会重载当前页签", "function reloadCurrentView" in H)
ck("世界书页签有加载分支", "if (name === 'worldbook')" in H)
ck("聊天记录页签有加载分支", "if (name === 'chat')" in H)
ck("开屏在数据源确定后重拉", "开屏时 fetchWorldBook" in H)
ck("手动新增记忆传参已修", "keywords=payload.get(\"keywords\")" in A)
ck("新设备二维码失败会如实报错", "_st == \"FAIL\" or not url" in io.open("keepalive.py", encoding="utf-8", errors="replace").read())
ck("新设备二维码失败带错误码", "ActionStatus=%s ErrorCode=%s" in io.open("keepalive.py", encoding="utf-8", errors="replace").read())

# ---------- 14.23 NapCat 二维码 / 新设备验证 ----------
_KQ = io.open("keepalive.py", encoding="utf-8").read()
ck("保活模块有 refresh_qr", "def refresh_qr():" in _KQ and "/api/QQLogin/RefreshQrcode" in _KQ)
ck("保活模块有新设备验证二维码", "def new_device_qr(" in _KQ and "/api/QQLogin/GetNewDeviceQrcode" in _KQ)
ck("L5 会先刷新二维码", "def act_l5():" in _KQ and "_rq = refresh_qr()" in _KQ)
ck("act_l3 保存新设备验证链接", '_state["new_device"] = {"uin": qq, "jump_url": _ju, "at": time.time()}' in _KQ)
ck("app 有刷新二维码接口", "/api/keepalive/refresh_qr" in A)
ck("app 有新设备验证二维码接口", "/api/keepalive/new_device_qr" in A)
ck("手动保活支持 L5", "5: \"L5 刷新登录二维码\"" in A or '"L5 刷新登录二维码"' in A)
ck("前端 L5 扫码先刷新", "await fetch('/api/keepalive/refresh_qr'" in H)
ck("前端新设备验证取二维码", "kaFetchNewDeviceQr" in H and "await fetch('/api/keepalive/new_device_qr'" in H)

# ---------- 14.22 调试模式 ----------
ck("调试模式默认关", '"debug_mode": False,' in A)
ck("调试模式接口存在", '/api/debug_mode' in A and 'current_config["debug_mode"] = enabled' in A)
ck("调试模式按钮存在", 'id="btnDebugMode"' in H and "toggleDebugMode()" in H)
ck("调试模式前端初始化", "debugModeOn = !!cfg.debug_mode;" in H and "renderDebugMode(debugModeOn);" in H)
ck("调试模式只打印不改上下文", '"[调试模式] 已开启：发往 API 的完整报文与返回内容会实时打印到控制台（不影响历史注入与回写）"' in A)
ck("主Bot 打印支持调试模式", '_dbg_tag = "测试模式" if self.config.get("test_mode", False) else (' in B)
ck("群聊链路打印支持调试模式", 'getattr(self, "config", {}).get("debug_mode", False)' in _C and '"[调试模式] 群聊发往 API 的完整请求报文' in _C)

# ---------- 14.21 记忆库运行参数跟随数据源 ----------
ck("运行参数按页签数据源读", "memScope === 'claude'" in H and "c[p + 'memory_top_k']" in H)
ck("运行参数按数据源写", "payload[p + 'memory_top_k']" in H and "payload[isC ? 'claude_context_limit' : 'memory_history_limit']" in H)
ck("群聊历史条数用 context_limit", "'claude_context_limit'" in H)
ck("切换数据源后刷新运行参数", "if (_ms && !_ms.classList.contains('hidden') && typeof loadMemoryConfig === 'function') loadMemoryConfig();" in H)


# ---------- 14.20 群聊记忆库注入的 user_id ----------
ck("群聊 _memory_block 收 group_id", "def _memory_block(self, query: str, group_id: Any = None) -> str:" in _CGS)
ck("群聊 recall 传群号不传原文", 'self.memory.recall(uid, query,' in _CGS and "self.memory.recall(query, query," not in _CGS)
ck("两处调用都传了 group_id", "self._memory_block(query, group_id)" in _CGS)


# ---------- 14.19 logo「星」字重 ----------
ck("logo 星 有中文字体栈类", ".logo-cn {" in H)
ck("logo 星 字重 800", "font-weight: 800;" in H and ".logo-cn {" in H)
ck("logo 星 已挂上类", 'text-emerald-400 logo-cn">星' in H)


# ---------- 14.18 NapCat 启动设置里的「重启 WebUI」 ----------
ck("启动设置卡片有重启 NapCat 按钮", 'onclick="restartNapCat()"' in H)
ck("有 restartNapCat 函数", "async function restartNapCat(" in H)
ck("按钮带二次确认", "重启 NapCat？" in H and "🔄 重启 NapCat</button>" in H)
ck("后端有重启接口", '/api/napcat/restart' in _APP)
ck("后端调 restart_webui", "_KA.restart_webui" in _APP)


# ---------- 14.17 控制台 ASCII 二维码排版 + 人工验证弹窗开关 ----------
ck("有 ascii-art 原样渲染类", ".ascii-art {" in H)
ck("ascii-art 行距锁 1", "line-height: 1 !important" in H)
ck("ascii-art 禁止折行", "word-break: keep-all !important" in H)
ck("终端容器不再 break-all",
   'ascii-terminal text-[13px] select-text bg-[#0a0e14] break-words' in H)
ck("appendNapCatText 识别块字符", "isArt ? ' ascii-art'" in H)
ck("appendLog 也保护 ASCII 行", "_isArt ? 'my-0.5 whitespace-pre ascii-art'" in H)
ck("设置页有验证弹窗开关", 'id="setCaptchaPopup"' in H)
ck("有 setLoad / setSave", "async function setLoad(" in H and "async function setSave(" in H)
ck("弹窗受开关控制", "if (captchaPopupEnabled) {" in H)
ck("后端有默认值", "captcha_popup_enabled" in _APP)


# ---------- 14.16 右上角设置按钮绿底 ----------
ck("设置按钮是绿底白图标", "bg-emerald-600 border border-emerald-500 text-white" in H)
ck("设置按钮不再是深色底",
   "rounded-lg bg-terminal border border-borderDark text-gray-400 hover:text-emerald-400" not in H)


# ---------- 14.15 保活卡片下方的手动执行按钮 ----------
# L1 重连 / L3 密码登录 / L5 扫码三个按钮：前两个走 /api/keepalive/manual，
# L5 直接复用已有的二维码弹窗 viewQrModal()。
ck("保活卡片有 L1 手动重连按钮", 'onclick="kaManual(1)"' in H)
ck("保活卡片有 L3 手动密码登录按钮", 'onclick="kaManual(3)"' in H)
ck("保活卡片有 L5 扫码按钮", 'onclick="viewQrModal()"' in H and 'L5 扫码' in H)
ck("有 kaManual 函数", "async function kaManual(" in H)
ck("后端有手动执行接口", '/api/keepalive/manual' in _APP)
ck("接口只放行 1 和 3", "level 只能是 1" in _APP)
ck("接口调 act_manual", "_KA.act_manual" in _APP)


# ---------- 14.14 群聊链路也要写「查看实际发送 Prompt」面板 ----------
# 起因：面板只由主 Bot 的 _call_llm_native 写入，群聊链路的 _call_api 完全不写，
# 所以在群聊模式点面板永远是空的。现在群聊也写，并标上来源。
ck("群聊 _call_api 会写调试面板", "import bot as _BOT" in _CGS)
ck("群聊记录带来源标记", '"source": "群聊"' in _CGS)
ck("群聊记录真实 messages", '"messages": messages,' in _CGS)
ck("群聊响应后补 raw_response", '_BOT.last_debug_info["raw_response"]' in _CGS)
ck("主 Bot 也标来源", 'last_debug_info["source"] = "主 Bot"' in B)
ck("面板显示来源", "info.source ? ' · ' + info.source" in H)


# ---------- 14.13 人设区「✏️ 编辑」按钮 ----------
# 内置档的人设框是只读的（内容由预设决定），光靠「＋ 新增人设」不好懂，
# 所以另加一个「✏️ 编辑」：点一下把当前内置档复制成一份可编辑的自定义档并启用。
ck("人设区有「✏️ 编辑」按钮", 'onclick="claudePersonaEdit()"' in H)
ck("有 claudePersonaEdit 函数", "async function claudePersonaEdit()" in H)
ck("编辑按钮排在删除按钮之后",
   H.index('onclick="claudePersonaEdit()"') > H.index('onclick="claudePersonaDelete()"'))
ck("编辑会提示已可编辑", "这档已经可编辑，直接改框里的内容" in H)
ck("内置档提示提到「✏️ 编辑」", "点「✏️ 编辑」" in H)


# ---------- 14.12 秘书网关保存不能把 API 配置清空 ----------
# 起因：secBody() 读的 secBase / secKey / secModel 这三个元素在页面里根本不存在，
# 群聊页签保存时永远读到空串，一保存就把秘书网关 API 冲成空。
ck("有 secPick 兜底取值", "function secPick(" in H)
ck("secBody 不再直接读不存在的 secBase",
   "document.getElementById('secBase') || {}).value" not in H)
ck("secBody 用 secPick 取 base", "sec_base_url: secPick('secBase', 'secApiBase')" in H)
ck("secBody 用 secPick 取 model", "sec_model: secPick('secModel', 'secApiModel')" in H)
ck("secApiSave 也用 secPick", "b.sec_base_url = secPick('secApiBase', 'secBase')" in H)
ck("两个页签的框都挂了脏标记", "'secApiModel'].forEach" in H)
ck("页面加载先回填秘书配置", "秘书网关配置先回填一遍" in H)




io.open("_chk_preset.js", "w", encoding="utf-8").write(js)
ck("JS 片段已导出", len(js) > 5000, len(js))
# ---------- 19. 仓库不带预设正文 + JSON 导入导出 ----------
for _n in ("TFE_SYSTEM_PROMPT", "DARKSIDE_ENHANCE_INJECT", "DARKSIDE_WORLD", "DARKSIDE_BRIDGE",
           "DARKSIDE_DIVIDER", "DARKSIDE_BYPASS", "JAILBREAK_RECENCY"):
    ck("仓库常量留空 %s" % _n, re.search(r'^%s = ""$' % _n, B, re.M) is not None)
ck("取值仍走「预设优先、常量兜底」", '_preset_field(self.config, "tfe") or TFE_SYSTEM_PROMPT' in B
   and '_preset_field(self.config, "bypass") or DARKSIDE_BYPASS' in B
   and '_preset_field(self.config, "recency") or JAILBREAK_RECENCY' in B)
ck("工具名仍是预设优先、全局兜底", '_preset_field(cfg, "tool_name")' in B)
ck("POST 支持 import", 'if act == "import":' in A)
ck("界面有导入按钮", "presetImport('jb')" in H and "presetImport('style')" in H)
ck("界面有导出按钮", "presetExport('jb')" in H and "presetExportAll('jb')" in H
   and "presetExport('style')" in H)
ck("导入导出函数有定义", "async function presetImport(" in H and "function presetExport(" in H
   and "function presetExportAll(" in H)
ck("正文工具名可自己填（和提示词里的名字对齐）", 'id="presetTool" readonly' not in H
   and "正文工具名（自己填，和提示词里要求的工具名保持一致）" in H)
ck("导出字段含三个工具名", "'tool_name', 'tool_desc', 'tool_arg_desc'" in H)

_r = asyncio.run(ns["presets_post"]({"action": "import", "data": [{"name": "导入A", "tool_name": "jb_a",
       "body": "用 jb_a 交正文", "bypass": "防截断也叫 jb_a", "tool_desc": "d", "tool_arg_desc": "a"}]}))
_ip = [x for x in _r["presets"] if x["id"] == _r["id"]][0]
ck("导入可用且 id 新分配", _r["status"] == "ok" and _ip["id"].startswith("p_") and _ip["id"] != "cat_god", _ip["id"])
ck("导入保留工具名与正文", _ip["tool_name"] == "jb_a" and _ip["body"] == "用 jb_a 交正文", _ip.get("tool_name"))
ck("导入的正文进注入链路", bot._preset_field({"llm_presets": [_ip], "llm_preset_id": _ip["id"]}, "body") == "用 jb_a 交正文")
ck("导入的工具名进注入链路", bot._preset_tool({"llm_presets": [_ip], "llm_preset_id": _ip["id"]})["function"]["name"] == "jb_a")
_r2 = asyncio.run(ns["presets_post"]({"action": "import", "data": [{"name": "导入B"}, {"name": "导入C"}]}))
ck("导入支持列表", _r2["status"] == "ok" and _r2["added"] == 2, _r2.get("added"))
ck("导入空内容报错", asyncio.run(ns["presets_post"]({"action": "import", "data": []}))["status"] == "error")
_r3 = asyncio.run(ns["presets_post"]({"action": "duplicate", "id": _ip["id"]}))
_d2 = [x for x in _r3["presets"] if x["id"] == _r3["id"]][0]
ck("副本工具名不撞车（导入的）", _d2["tool_name"] == "jb_a_2", _d2["tool_name"])
ck("副本文案里工具名同步换（导入的）", "jb_a_2" in _d2["body"] and "jb_a" not in _d2["body"].replace("jb_a_2", ""), _d2["body"])


ck("更新：/api/update/check", "/api/update/check" in A)
ck("更新：/api/update/apply", "/api/update/apply" in A)
ck("更新：只快进不合并", "\"--ff-only\"" in A)
ck("更新：拉前先备份本地改动", "\"stash\", \"push\"" in A)
ck("更新：远端地址可从 origin 推断", "get-url" in A and "origin" in A)
ck("更新：前端有检查更新按钮", "updateCheck()" in H and "updCheckBtn" in H)
ck("更新：前端有立即更新按钮", "updateApply()" in H and "updApplyBtn" in H)
ck("更新：设置页显示当前版本", "updCur" in H and "updateCardInit" in H)

ck("更新：支持 GitLab", "api/v4/projects" in A and "gitlab" in A)

ck("更新：没有上游分支时兜底", "显式指定 origin" in A and "abbrev-ref" in A)

ck("协议端：SnowLuma 令牌自动补全", "import snowluma_token" in A and "snowluma_cmd" in A
   and "accessToken" in io.open("snowluma_token.py", encoding="utf-8").read())

print("\n%s  PASS %d / FAIL %d" % ("[预设自检]", P, F))
sys.exit(1 if F else 0)


# ---------- 15. 防抖作废必须早于落库 ----------
ck("私聊防抖检查在落库之前", B.index("_db.should_discard(_sid)") < B.index("# 将 AI 回复落盘"))
ck("私聊防抖检查只有一处", B.count("_db.should_discard(_sid)") == 1)
ck("私聊作废不发送也不入库", "本次回复作废（不发送、不入聊天记录）" in B)
ck("群聊防抖检查在发送之前", _CG2.index("_db2.should_discard(_sid2)") < _CG2.index("群聊上下文", _CG2.index("_db2.should_discard(_sid2)")))
ck("群聊发送后标记已回复", _CG2.count("_db2.mark_replied(_sid2)") == 1)
ck("群聊 mark_replied 在检查之后", _CG2.index("_db2.mark_replied(_sid2)") > _CG2.index("_db2.should_discard(_sid2)"))
