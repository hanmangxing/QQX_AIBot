# -*- coding: utf-8 -*-
"""
P7 状态变量引擎：解析 → 执行 → 守卫

源实现三件套：
  parser.js     从模型输出里抽出 <State> 块，解析出 schema（含 $range/$step/$enum 约束）、
                rule 规则行、以及 path: value 形式的数据操作行；
                值语法：(数字) / "字符串" / '字符串' / +"push" / -"pop" / {JSON} / [JSON]
  executor.js   把操作落到变量树：set / inc / push / pop / del；
                路径支持点号与下标 a.b[0].c；带按楼层的 WAL(stateLogV2)、
                检查点(stateCkptV2)、按消息幂等应用、可回滚到任意楼层；
                数组下标删除会重排为降序执行，避免删除错位。
  guard-core.js 规则守卫：路径通配符匹配、类型强制、min/max 截断、enum 校验、step 对齐、
                子树递归校验；不合法时返回错误而不写坏数据。
  semantic.js   把变更描述成自然语言（供注入/展示）。

本实现保持同样的语义，去掉酒馆宿主依赖，纯 Python、可直接嵌进 QQX_AIBot。
"""
from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional, Sequence, Tuple

OPEN_RE = re.compile(r"<state\b", re.I)
CLOSE_RE = re.compile(r"</state\s*>", re.I)
STATE_OPEN_LEN = 6
STATE_CLOSE_LEN = 7

OPS = ("set", "inc", "push", "pop", "del")


# ---------------------------------------------------------------- 路径
def split_path(path: str) -> List[str]:
    """拆路径：a.b[0].c -> ['a','b','0','c']，支持引号包裹的键。"""
    s = str(path or "")
    segs: List[str] = []
    buf = []
    i = 0
    while i < len(s):
        ch = s[i]
        if ch == ".":
            if buf:
                segs.append("".join(buf).strip()); buf = []
            i += 1
            continue
        if ch == "[":
            if buf:
                segs.append("".join(buf).strip()); buf = []
            j = s.find("]", i)
            if j < 0:
                buf.append(s[i:]); break
            inner = s[i + 1:j].strip()
            if len(inner) >= 2 and inner[0] == inner[-1] and inner[0] in "\"'":
                inner = inner[1:-1]
            segs.append(inner)
            i = j + 1
            continue
        buf.append(ch)
        i += 1
    if buf:
        segs.append("".join(buf).strip())
    return [x for x in segs if x != ""]


def normalize_path(path: str) -> str:
    return ".".join(split_path(path))


def get_root_from_path(path: str) -> str:
    segs = split_path(path)
    return segs[0] if segs else ""


# ---------------------------------------------------------------- 值语法
def parse_inline_value(raw: str) -> Tuple[Optional[str], Any]:
    """
    解析数据行右侧的值，返回 (操作, 值)。
      (123)         -> ('set', 123.0)
      +"钥匙"        -> ('push', '钥匙')
      -"钥匙"        -> ('pop', '钥匙')
      {"a":1}       -> ('set', {...})
      [1,2]         -> ('set', [...])
      "文本" / '文本' -> ('set', '文本')
      其它           -> ('set', 原样字符串)
    """
    t = str(raw or "").strip()
    if not t:
        return "set", ""
    m = re.match(r"^\((-?\d+(?:\.\d+)?)\)$", t)
    if m:
        return "set", float(m.group(1))
    m = re.match(r'^\+\s*"((?:[^"\\]|\\.)*)"\s*$', t) or re.match(r"^\+\s*'((?:[^'\\]|\\.)*)'\s*$", t)
    if m:
        return "push", _unescape(m.group(1))
    m = re.match(r'^-\s*"((?:[^"\\]|\\.)*)"\s*$', t) or re.match(r"^-\s*'((?:[^'\\]|\\.)*)'\s*$", t)
    if m:
        return "pop", _unescape(m.group(1))
    if t[:1] in "[{":
        try:
            return "set", json.loads(t)
        except Exception:
            return "set", t
    m = re.match(r'^"((?:[^"\\]|\\.)*)"\s*$', t) or re.match(r"^'((?:[^'\\]|\\.)*)'\s*$", t)
    if m:
        return "set", _unescape(m.group(1))
    return "set", t


def _unescape(s: str) -> str:
    return s.replace('\\"', '"').replace("\\'", "'").replace("\\\\", "\\")


def _strip_inline_comment(s: str) -> str:
    out, q = [], None
    for ch in s:
        if q:
            if ch == q:
                q = None
            out.append(ch)
            continue
        if ch in "\"'":
            q = ch; out.append(ch); continue
        if ch == "#":
            break
        out.append(ch)
    return "".join(out)


# ---------------------------------------------------------------- 块抽取
def extract_state_blocks(text: str) -> List[str]:
    """抽出所有 <State>...</State> 块（含标签）。"""
    s = str(text or "")
    spans = find_state_block_spans(s)
    return [s[sp[0]:sp[1]].strip() for sp in spans]


def find_state_block_spans(s: str) -> List[Tuple[int, int]]:
    spans: List[Tuple[int, int]] = []
    i = 0
    while True:
        m = OPEN_RE.search(s, i)
        if not m:
            break
        open_start = m.start()
        open_end = s.find(">", m.end())
        if open_end < 0:
            break
        cm = CLOSE_RE.search(s, open_end)
        if not cm:
            break
        spans.append((open_start, cm.end()))
        i = cm.end()
    return spans


def compute_state_signature(text: str) -> str:
    """块内容签名，用于判断同一条消息的状态是否已应用过（幂等）。"""
    blocks = extract_state_blocks(text)
    if not blocks:
        return ""
    body = "\n".join(blocks)
    import hashlib
    return hashlib.md5(body.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------- 解析
def parse_rule_line(line: str) -> Optional[Dict[str, Any]]:
    """解析规则行：`player.hp $range=[0,100] $step=1 $enum={a,b}`"""
    t = _strip_inline_comment(str(line or "")).strip()
    if not t:
        return None
    tokens = t.split()
    directives: List[str] = []
    path_tokens: List[str] = []
    for tok in tokens:
        if tok.startswith("$"):
            directives.append(tok)
        else:
            path_tokens.append(tok)
    path = " ".join(path_tokens).strip()
    if not path:
        return None
    rule: Dict[str, Any] = {"path": normalize_path(path)}
    for d in directives:
        m = re.match(r"^\$range=\[\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*\]$", d)
        if m:
            rule["min"] = float(m.group(1)); rule["max"] = float(m.group(2)); continue
        m = re.match(r"^\$step=(\d+(?:\.\d+)?)$", d)
        if m:
            rule["step"] = float(m.group(1)); continue
        m = re.match(r"^\$enum=\{([^}]+)\}$", d)
        if m:
            rule["enum"] = [x.strip() for x in m.group(1).split(",") if x.strip()]; continue
    return rule


def _split_top_level(s: str, sep: str = ",") -> List[str]:
    """按分隔符切分，忽略括号/引号内部（YAML 内联映射与列表都要用）。"""
    out, buf, depth, q = [], [], 0, None
    for ch in s:
        if q:
            buf.append(ch)
            if ch == q:
                q = None
            continue
        if ch in "\"'":
            q = ch; buf.append(ch); continue
        if ch in "[{(":
            depth += 1
        elif ch in "]})":
            depth -= 1
        if ch == sep and depth == 0:
            out.append("".join(buf)); buf = []
            continue
        buf.append(ch)
    if buf:
        out.append("".join(buf))
    return [x.strip() for x in out if x.strip()]


def _parse_schema_value(v: str) -> Any:
    v = (v or "").strip()
    if not v:
        return ""
    if v.startswith("[") and v.endswith("]"):
        return [_parse_schema_value(x) for x in _split_top_level(v[1:-1])]
    if v.startswith("{") and v.endswith("}"):
        return _parse_inline_map(v)
    try:
        return float(v)
    except (TypeError, ValueError):
        return v.strip("\"'")


def _parse_inline_map(spec: str) -> Dict[str, Any]:
    """解析 YAML 风格内联映射：{min: 0, max: 100} / {enum: [甲, 乙]}（键可无引号）。"""
    spec = (spec or "").strip()
    if not (spec.startswith("{") and spec.endswith("}")):
        return {}
    out: Dict[str, Any] = {}
    for part in _split_top_level(spec[1:-1]):
        idx = _find_top_level_colon(part)
        if idx < 0:
            continue
        k = part[:idx].strip().strip("\"'")
        if k:
            out[k] = _parse_schema_value(part[idx + 1:])
    return out


def parse_schema_block(base_path: str, schema_lines: Sequence[str]) -> List[Dict[str, Any]]:
    """
    解析 YAML 式 schema 缩进块，产出规则列表。
    `*` 作为通配模板。
    """
    rules: List[Dict[str, Any]] = []
    stack: List[Tuple[int, str]] = []          # (indent, path)

    def add_rule(path: str, spec: Any) -> None:
        if not path:
            return
        r: Dict[str, Any] = {"path": normalize_path(path)}
        if isinstance(spec, dict):
            for k in ("min", "max", "step"):
                if k in spec:
                    try:
                        r[k] = float(spec[k])
                    except (TypeError, ValueError):
                        pass
            if "enum" in spec:
                v = spec["enum"]
                if isinstance(v, str):
                    v = [x.strip() for x in v.strip("[]{}").split(",") if x.strip()]
                r["enum"] = list(v) if isinstance(v, list) else []
            if "type" in spec:
                r["type"] = str(spec["type"])
        rules.append(r)

    for raw in schema_lines:
        line = _strip_inline_comment(raw).rstrip()
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip())
        content = line.strip()
        if content.endswith(":"):
            key = content[:-1].strip().strip("\"'")
            while stack and stack[-1][0] >= indent:
                stack.pop()
            parent = stack[-1][1] if stack else base_path
            path = f"{parent}.{key}" if parent and key != "*" else (parent or key)
            stack.append((indent, path))
            continue
        if ":" in content:
            key, _, val = content.partition(":")
            key = key.strip().strip("\"'")
            while stack and stack[-1][0] >= indent:
                stack.pop()
            parent = stack[-1][1] if stack else base_path
            path = f"{parent}.{key}" if parent else key
            spec = val.strip()
            if spec.startswith("{"):
                spec = _parse_inline_map(spec)
            elif spec == "":
                spec = {}
            else:
                spec = _parse_schema_value(spec)
                spec = spec if isinstance(spec, dict) else {}
            add_rule(path, spec)
    return rules


def parse_state_block(content: str) -> Dict[str, Any]:
    """
    解析单个 <State> 块，返回 {schema, rules, ops}。
    ops: [{'op': 'set'|'inc'|'push'|'pop'|'del', 'path': ..., 'value': ...}]
    """
    s = str(content or "")
    m = OPEN_RE.search(s)
    if m:
        end = s.find(">", m.end())
        if end >= 0:
            s = s[end + 1:]
    s = CLOSE_RE.sub("", s)
    # 把 <data>/<rule>/<schema> 这类标签统一拆到独立行，兼容全部写在同一行的情况
    s = re.sub(r"</?(?:schema|rule|rules|data)\s*>",
               lambda m: "\n" + m.group(0) + "\n", s, flags=re.I)

    lines = s.split("\n")
    schema_lines: List[str] = []
    rule_lines: List[str] = []
    data_lines: List[str] = []
    section: Optional[str] = None

    for raw in lines:
        t = raw.strip()
        if not t:
            continue
        tag = t.strip().strip("<>/").strip().lower()
        if tag in ("schema", "rule", "rules", "data", "state"):
            section = None if tag == "state" else ("rule" if tag == "rules" else tag)
            continue
        if section == "schema":
            schema_lines.append(raw)
        elif section == "rule":
            rule_lines.append(raw)
        else:
            data_lines.append(raw)

    rules: List[Dict[str, Any]] = []
    for rl in rule_lines:
        r = parse_rule_line(rl)
        if r:
            rules.append(r)
    rules.extend(parse_schema_block("", schema_lines))

    ops: List[Dict[str, Any]] = []
    for dl in data_lines:
        line = _strip_inline_comment(dl).rstrip()
        if not line.strip():
            continue
        colon = _find_top_level_colon(line)
        if colon < 0:
            continue
        path = line[:colon].strip().strip("\"'")
        value_raw = line[colon + 1:].strip()
        if not path:
            continue
        op, val = parse_inline_value(value_raw)
        ops.append({"op": op, "path": normalize_path(path), "value": val})
    return {"schema": schema_lines, "rules": rules, "ops": ops}


def _find_top_level_colon(line: str) -> int:
    depth, q = 0, None
    for i, ch in enumerate(line):
        if q:
            if ch == q:
                q = None
            continue
        if ch in "\"'":
            q = ch; continue
        if ch in "[{(":
            depth += 1; continue
        if ch in "]})":
            depth -= 1; continue
        if ch == ":" and depth == 0:
            return i
    return -1


# ---------------------------------------------------------------- 守卫
def _value_type(v: Any) -> str:
    if isinstance(v, bool):
        return "boolean"
    if isinstance(v, (int, float)):
        return "number"
    if isinstance(v, str):
        return "string"
    if isinstance(v, list):
        return "array"
    if isinstance(v, dict):
        return "object"
    return "null" if v is None else "unknown"


def _is_plain_object(v: Any) -> bool:
    return isinstance(v, dict)


class RuleGuard:
    """规则守卫。"""

    def __init__(self, rules: Optional[Sequence[Dict[str, Any]]] = None):
        self.rules: List[Dict[str, Any]] = list(rules or [])

    def set_rule(self, rule: Dict[str, Any]) -> None:
        path = normalize_path(rule.get("path", ""))
        self.rules = [r for r in self.rules if normalize_path(r.get("path", "")) != path]
        self.rules.append(dict(rule, path=path))

    def clear_all(self) -> None:
        self.rules = []

    def get_rule_node(self, abs_path: str) -> Optional[Dict[str, Any]]:
        """精确匹配优先，其次通配符匹配（`*` 段可匹配任意一层）。"""
        p = normalize_path(abs_path)
        for r in self.rules:
            if normalize_path(r.get("path", "")) == p:
                return r
        segs = split_path(p)
        best: Optional[Dict[str, Any]] = None
        best_score = -1
        for r in self.rules:
            rs = split_path(normalize_path(r.get("path", "")))
            if len(rs) != len(segs):
                continue
            score = 0
            ok = True
            for a, b in zip(rs, segs):
                if a == "*":
                    score += 1
                elif a != b:
                    ok = False; break
            if ok and score > best_score:
                best, best_score = r, score
        return best

    def _coerce(self, node: Optional[Dict[str, Any]], payload: Any) -> Any:
        want = (node or {}).get("type")
        if not want:
            return payload
        if want == "number":
            try:
                return float(payload)
            except (TypeError, ValueError):
                return payload
        if want == "string":
            return str(payload)
        return payload

    def _validate_scalar(self, node: Optional[Dict[str, Any]], payload: Any) -> Dict[str, Any]:
        value, notes, clamped = payload, [], False
        if not node:
            return {"value": value, "clamped": False, "notes": notes}
        if "min" in node or "max" in node:
            try:
                num = float(payload)
                if "min" in node and num < node["min"]:
                    num, clamped = float(node["min"]), True
                if "max" in node and num > node["max"]:
                    num, clamped = float(node["max"]), True
                if clamped:
                    notes.append(f"超出范围已截断到 {num:g}")
                value = num
            except (TypeError, ValueError):
                notes.append("需要数值，保持原值")
        if "enum" in node and node["enum"]:
            if value not in node["enum"]:
                notes.append(f"不在允许取值 {node['enum']} 内")
        if "step" in node and node.get("step"):
            try:
                step = float(node["step"])
                if step > 0:
                    value = round(float(value) / step) * step
                    if value != float(payload):
                        notes.append(f"已对齐到步长 {step:g}")
            except (TypeError, ValueError):
                pass
        return {"value": value, "clamped": clamped, "notes": notes}

    def validate(self, op: str, abs_path: str, payload: Any, current: Any) -> Dict[str, Any]:
        """返回 {value, clamped, notes, error}。error 非空表示拒绝写入。"""
        node = self.get_rule_node(abs_path)
        if op == "del":
            return {"value": None, "clamped": False, "notes": [], "error": ""}
        if op == "inc":
            try:
                delta = float(payload)
            except (TypeError, ValueError):
                return {"value": current, "clamped": False, "notes": [],
                        "error": f"inc 需要数值增量，收到 {payload!r}"}
            base = float(current) if isinstance(current, (int, float)) else 0.0
            r = self._validate_scalar(node, base + delta)
            r["error"] = ""
            return r
        if op in ("push", "pop"):
            cur = current if isinstance(current, list) else []
            if op == "push":
                return {"value": cur + [payload], "clamped": False, "notes": [], "error": ""}
            items = payload if isinstance(payload, list) else [payload]
            new = list(cur)
            for it in items:
                try:
                    new.remove(it)
                except ValueError:
                    return {"value": cur, "clamped": False, "notes": [],
                            "error": f"要移除的值不存在：{it!r}"}
            return {"value": new, "clamped": False, "notes": [], "error": ""}
        # set
        coerced = self._coerce(node, payload)
        if isinstance(coerced, (dict, list)):
            return {"value": coerced, "clamped": False, "notes": [], "error": ""}
        r = self._validate_scalar(node, coerced)
        r["error"] = ""
        return r


# ---------------------------------------------------------------- 执行器
class StateEngine:
    """变量树 + 按楼层 WAL + 检查点 + 幂等应用 + 回滚。"""

    def __init__(self, variables: Optional[Dict[str, Any]] = None,
                 rules: Optional[Sequence[Dict[str, Any]]] = None,
                 checkpoint_every: int = 50):
        self.variables: Dict[str, Any] = json.loads(json.dumps(variables or {}))
        self.guard = RuleGuard(rules)
        self.checkpoint_every = max(1, int(checkpoint_every))
        self.log: Dict[int, Dict[str, Any]] = {}         # floor -> {signature, ops, roots}
        self.applied: Dict[str, int] = {}                # message_id -> floor
        self.checkpoints: Dict[int, Dict[str, Any]] = {}
        self.semantics: List[str] = []
        self.errors: List[str] = []

    # ---------- 变量读写 ----------
    def get_var(self, path: str) -> Any:
        segs = split_path(path)
        cur: Any = self.variables
        for s in segs:
            if isinstance(cur, dict):
                if s not in cur:
                    return None
                cur = cur[s]
            elif isinstance(cur, list):
                try:
                    cur = cur[int(s)]
                except (ValueError, IndexError):
                    return None
            else:
                return None
        return cur

    def set_var(self, path: str, value: Any) -> None:
        segs = split_path(path)
        if not segs:
            return
        cur = self.variables
        for i, s in enumerate(segs[:-1]):
            nxt = segs[i + 1]
            if isinstance(cur, list):
                try:
                    idx = int(s)
                except ValueError:
                    return
                while len(cur) <= idx:
                    cur.append(None)
                if not isinstance(cur[idx], (dict, list)):
                    cur[idx] = [] if nxt.isdigit() else {}
                cur = cur[idx]
            elif isinstance(cur, dict):
                if not isinstance(cur.get(s), (dict, list)):
                    cur[s] = [] if nxt.isdigit() else {}
                cur = cur[s]
            else:
                return
        last = segs[-1]
        if isinstance(cur, list):
            try:
                idx = int(last)
            except ValueError:
                return
            while len(cur) <= idx:
                cur.append(None)
            cur[idx] = value
        elif isinstance(cur, dict):
            cur[last] = value

    def del_var(self, path: str) -> None:
        segs = split_path(path)
        if not segs:
            return
        cur: Any = self.variables
        for s in segs[:-1]:
            if isinstance(cur, dict) and s in cur:
                cur = cur[s]
            elif isinstance(cur, list):
                try:
                    cur = cur[int(s)]
                except (ValueError, IndexError):
                    return
            else:
                return
        last = segs[-1]
        if isinstance(cur, dict):
            cur.pop(last, None)
        elif isinstance(cur, list):
            try:
                cur.pop(int(last))
            except (ValueError, IndexError):
                pass

    # ---------- 应用一条消息 ----------
    def apply_message(self, message_id: str, content: str, floor: int = 0) -> Dict[str, Any]:
        """解析并应用一条消息里的 <State> 块；同一条消息幂等。"""
        sig = compute_state_signature(content)
        if not sig:
            return {"applied": False, "ops": 0, "errors": []}
        if self.applied.get(str(message_id)) == floor or self.log.get(floor, {}).get("signature") == sig:
            return {"applied": False, "ops": 0, "errors": [], "duplicate": True}

        blocks = extract_state_blocks(content)
        ops: List[Dict[str, Any]] = []
        for b in blocks:
            parsed = parse_state_block(b)
            for r in parsed["rules"]:
                self.guard.set_rule(r)
            ops.extend(parsed["ops"])

        errs: List[str] = []
        applied_ops: List[Dict[str, Any]] = []
        for op in self._reorder_index_deletes(ops):
            try:
                self._exec_op(op, errs)
                applied_ops.append(op)
            except Exception as e:
                errs.append(f"{op.get('op')} {op.get('path')}: {e}")

        self.log[int(floor)] = {"signature": sig, "ops": applied_ops}
        self.applied[str(message_id)] = int(floor)
        self.errors.extend(errs)
        if int(floor) % self.checkpoint_every == 0:
            self.checkpoints[int(floor)] = json.loads(json.dumps(self.variables))
        return {"applied": True, "ops": len(applied_ops), "errors": errs}

    @staticmethod
    def _reorder_index_deletes(ops: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """同一父路径下的下标删除按降序执行，避免删除错位。"""
        dels = [o for o in ops if o.get("op") == "del"
                and split_path(o.get("path", ""))[-1:].__len__() == 1
                and str(split_path(o.get("path", ""))[-1]).isdigit()]
        others = [o for o in ops if o not in dels]
        groups: Dict[str, List[Dict[str, Any]]] = {}
        for d in dels:
            parent = ".".join(split_path(d["path"])[:-1])
            groups.setdefault(parent, []).append(d)
        out: List[Dict[str, Any]] = []
        for parent, items in groups.items():
            out.extend(sorted(items, key=lambda o: -int(split_path(o["path"])[-1])))
        return others + out

    def _exec_op(self, op: Dict[str, Any], errs: List[str]) -> None:
        kind = op.get("op")
        path = op.get("path", "")
        value = op.get("value")
        if not path:
            return
        old = self.get_var(path)
        if kind == "del":
            self.del_var(path)
            self.semantics.append(generate_semantic(path, "del", old, None, None, None))
            return
        guard = self.guard.validate(kind, normalize_path(path), value, old)
        if guard.get("error"):
            errs.append(f"{kind} {path}: {guard['error']}")
            return
        new = guard["value"]
        self.set_var(path, new)
        delta = None
        if kind == "inc":
            try:
                delta = float(value)
            except (TypeError, ValueError):
                delta = None
        self.semantics.append(generate_semantic(path, kind, old, new, delta, value))

    # ---------- 回滚 ----------
    def restore_to_floor(self, target_floor: int) -> bool:
        """回滚到指定楼层（用最近的检查点重放）。"""
        points = [f for f in self.checkpoints if f <= int(target_floor)]
        base = max(points) if points else None
        if base is not None:
            self.variables = json.loads(json.dumps(self.checkpoints[base]))
            start = base + 1
        else:
            start = 0
        for f in sorted(k for k in self.log if k >= start):
            if f > int(target_floor):
                self.log.pop(f, None)
                continue
            for op in self.log[f]["ops"]:
                self._exec_op(op, self.errors)
        for mid, fl in list(self.applied.items()):
            if fl > int(target_floor):
                self.applied.pop(mid, None)
        return True

    def trim_from_floor(self, from_floor: int) -> int:
        """丢弃某楼层之后的状态记录。"""
        victims = [f for f in self.log if f >= int(from_floor)]
        for f in victims:
            self.log.pop(f, None)
        for mid, fl in list(self.applied.items()):
            if fl >= int(from_floor):
                self.applied.pop(mid, None)
        return len(victims)

    def snapshot(self) -> Dict[str, Any]:
        return {"variables": json.loads(json.dumps(self.variables)),
                "rules": list(self.guard.rules),
                "log": json.loads(json.dumps(self.log)),
                "applied": dict(self.applied),
                "checkpoints": json.loads(json.dumps(self.checkpoints))}

    def load_snapshot(self, snap: Dict[str, Any]) -> None:
        self.variables = json.loads(json.dumps(snap.get("variables") or {}))
        self.guard = RuleGuard(snap.get("rules") or [])
        self.log = {int(k): v for k, v in (snap.get("log") or {}).items()}
        self.applied = dict(snap.get("applied") or {})
        self.checkpoints = {int(k): v for k, v in (snap.get("checkpoints") or {}).items()}

    def semantic_log(self, limit: int = 50) -> List[str]:
        return self.semantics[-limit:]


# ---------------------------------------------------------------- 语义化
def generate_semantic(path: str, op: str, old_value: Any, new_value: Any,
                      delta: Optional[float] = None, operand_value: Any = None) -> str:
    """把一次变量变更描述成自然语言。"""
    pretty = str(path or "").replace(".", " > ")
    if op == "del":
        return f"{pretty} 已删除（原值 {_fmt(old_value)}）"
    if op == "inc":
        sign = "+" if (delta or 0) >= 0 else ""
        return f"{pretty} {_fmt(old_value)} → {_fmt(new_value)}（{sign}{_fmt(delta)}）"
    if op == "push":
        items = operand_value if isinstance(operand_value, list) else [operand_value]
        return f"{pretty} 新增 {_fmt(items)}（现有 {len(new_value) if isinstance(new_value, list) else 0} 项）"
    if op == "pop":
        items = operand_value if isinstance(operand_value, list) else [operand_value]
        return f"{pretty} 移除 {_fmt(items)}（剩余 {len(new_value) if isinstance(new_value, list) else 0} 项）"
    return f"{pretty} {_fmt(old_value)} → {_fmt(new_value)}"


def _fmt(v: Any) -> str:
    if v is None:
        return "空"
    if isinstance(v, float):
        return f"{v:g}"
    if isinstance(v, (dict, list)):
        return json.dumps(v, ensure_ascii=False)
    return str(v)
