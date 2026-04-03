#!/usr/bin/env python3
import json
import re
import sys
import traceback
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

import requests

NOTION_VERSION = "2025-09-03"
NOTION_BASE_URL = "https://api.notion.com/v1"
TIMEZONE = ZoneInfo("Asia/Singapore")

# 使用脚本所在目录存储状态文件
STATE_DIR = Path(__file__).parent
STATE_PATH = STATE_DIR / "notion_quadrant_manager_state.json"

DONE_STATUS_HINTS = ("已完成", "完成", "done", "complete", "completed", "finished")
CANCEL_STATUS_HINTS = ("已取消", "取消", "canceled", "cancelled", "aborted", "void")
TODO_STATUS_HINTS = ("未完成", "待办", "todo", "to do", "not started", "进行中", "in progress", "未开始")

FIELD_ALIASES = {
    "title": ["待办事项", "待办", "标题", "task", "name", "title", "事项", "任务"],
    "due": ["截止时间", "截止日期", "due date", "due", "deadline", "日期", "时间", "到期"],
    "quadrant": ["四象限", "优先级", "priority", "重要程度", "等级"],
    "status": ["状态", "status", "进度"],
    "note": ["备注", "note", "备注说明", "说明", "描述", "details", "detail"],
    "category": ["分类", "category", "tag", "tags", "类别", "分组"],
}


class NotionQMError(Exception):
    pass


class ConfigError(NotionQMError):
    pass


class SchemaError(NotionQMError):
    pass


class APIError(NotionQMError):
    pass


def now() -> datetime:
    return datetime.now(tz=TIMEZONE)


def today() -> date:
    return now().date()


def norm(text: Any) -> str:
    text_str = str(text or "")[:1000]
    return re.sub(r"\s+", "", text_str).strip().lower()


def state_load() -> Dict[str, Any]:
    try:
        if STATE_PATH.exists():
            return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


def state_save(state: Dict[str, Any]) -> None:
    try:
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as exc:
        raise NotionQMError(f"状态保存失败：{exc}") from exc


def json_output(ok: bool, action: str, message: str, data: Optional[Dict[str, Any]] = None) -> None:
    payload = {"ok": ok, "action": action, "message": message, "data": data or {}}
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def make_headers(api_key: str) -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {api_key}",
        "Notion-Version": NOTION_VERSION,
        "Content-Type": "application/json",
    }


def notion_request(
    api_key: str,
    method: str,
    path: str,
    *,
    body: Optional[Dict[str, Any]] = None,
    params: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    url = f"{NOTION_BASE_URL}{path}"
    try:
        resp = requests.request(
            method=method,
            url=url,
            headers=make_headers(api_key),
            json=body,
            params=params,
            timeout=45,
        )
    except requests.RequestException as exc:
        if isinstance(exc, requests.Timeout):
            raise APIError("Notion 请求超时，请检查网络连接") from exc
        if isinstance(exc, requests.ConnectionError):
            raise APIError("Notion 连接失败，请检查网络连接") from exc
        raise APIError(f"Notion 请求失败：{exc}") from exc

    if not resp.ok:
        detail = ""
        try:
            err = resp.json()
            detail = err.get("message") or err.get("error") or resp.text
        except Exception:
            detail = resp.text
        
        if resp.status_code == 401:
            raise APIError("API 密钥无效，请检查 API 密钥是否正确")
        if resp.status_code == 403:
            raise APIError("权限不足，请检查 API 密钥权限")
        if resp.status_code == 404:
            raise APIError("资源不存在，请检查数据库名称是否正确")
        if resp.status_code == 429:
            raise APIError("API 调用过于频繁，请稍后重试")
        if resp.status_code >= 500:
            raise APIError("Notion 服务器错误，请稍后重试")
            
        raise APIError(f"Notion API 返回错误 {resp.status_code}：{detail}")

    if resp.text.strip():
        return resp.json()
    return {}


def search_targets(api_key: str, query: str) -> List[Dict[str, Any]]:
    results: List[Dict[str, Any]] = []
    cursor = None
    while True:
        body: Dict[str, Any] = {"query": query, "page_size": 100}
        if cursor:
            body["start_cursor"] = cursor
        payload = notion_request(api_key, "POST", "/search", body=body)
        results.extend(payload.get("results", []))
        if not payload.get("has_more"):
            break
        cursor = payload.get("next_cursor")
        if not cursor:
            break
    return results


def get_object_title(obj: Dict[str, Any]) -> str:
    title = obj.get("title")
    if isinstance(title, str):
        return title
    if isinstance(title, list):
        parts = []
        for item in title:
            if isinstance(item, dict):
                parts.append(item.get("plain_text") or item.get("text", {}).get("content", ""))
        return "".join(parts).strip()
    if isinstance(obj.get("name"), str):
        return obj["name"].strip()
    return ""


def match_title_score(candidate: str, query: str) -> int:
    c = norm(candidate)
    q = norm(query)
    if c == q:
        return 100
    score = 0
    if q and q in c:
        score += 60
    if c and c in q:
        score += 20
    for token in re.split(r"\s+", query.strip()):
        token = norm(token)
        if token and token in c:
            score += 5
    return score


def resolve_database(api_key: str, database_name: str) -> Dict[str, Any]:
    cache = state_load()
    cached = cache.get("resolved")
    if cached and norm(cached.get("database_name")) == norm(database_name) and cached.get("data_source_id"):
        return cached

    candidates = []
    for item in search_targets(api_key, database_name):
        obj_type = item.get("object")
        title = get_object_title(item)
        if obj_type in {"database", "data_source"}:
            score = match_title_score(title, database_name)
            if score > 0:
                candidates.append((score, item))

    if not candidates:
        raise ConfigError(f"未找到名称匹配的数据库/数据源：{database_name}")

    candidates.sort(key=lambda x: x[0], reverse=True)
    best = candidates[0][1]

    if best.get("object") == "data_source":
        resolved = {
            "database_name": database_name,
            "database_id": best.get("parent", {}).get("database_id") or best.get("database_id"),
            "data_source_id": best["id"],
            "title": get_object_title(best) or database_name,
        }
    else:
        db = notion_request(api_key, "GET", f"/databases/{best['id']}")
        data_sources = db.get("data_sources") or []
        if not data_sources:
            raise SchemaError("数据库已找到，但没有可用的数据源。")
        ds = data_sources[0]
        resolved = {
            "database_name": database_name,
            "database_id": db.get("id") or best["id"],
            "data_source_id": ds.get("id"),
            "title": get_object_title(db) or database_name,
        }

    if not resolved.get("data_source_id"):
        raise SchemaError("未能解析 data_source_id。")

    cache["resolved"] = resolved
    state_save(cache)
    return resolved


def retrieve_schema(api_key: str, resolved: Dict[str, Any]) -> Dict[str, Any]:
    data_source_id = resolved["data_source_id"]
    schema = notion_request(api_key, "GET", f"/data_sources/{data_source_id}")
    properties = schema.get("properties") or {}
    if not properties:
        raise SchemaError("数据库 schema 为空，无法识别字段。")
    return schema


def prop_items(schema: Dict[str, Any]) -> List[Tuple[str, Dict[str, Any]]]:
    props = schema.get("properties") or {}
    items = []
    for key, prop in props.items():
        if isinstance(prop, dict):
            items.append((key, prop))
    return items


def prop_name(prop_key: str, prop: Dict[str, Any]) -> str:
    return prop.get("name") or prop_key or ""


def prop_id(prop_key: str, prop: Dict[str, Any]) -> str:
    return prop.get("id") or prop_key or ""


def prop_type(prop: Dict[str, Any]) -> str:
    return prop.get("type") or ""


def extract_options(prop: Dict[str, Any]) -> List[str]:
    t = prop_type(prop)
    container = prop.get(t) or {}
    options = container.get("options") or []
    names = []
    for opt in options:
        if isinstance(opt, dict) and opt.get("name"):
            names.append(opt["name"])
    return names


def find_property(schema: Dict[str, Any], wanted: str, required_types: List[str]) -> Dict[str, Any]:
    try:
        wanted_aliases = [norm(x) for x in FIELD_ALIASES.get(wanted, [])]
        matched: List[Tuple[int, str, Dict[str, Any]]] = []
        for key, prop in prop_items(schema):
            name = prop_name(key, prop)
            n = norm(name)
            if prop_type(prop) in set(required_types) and any(alias == n or alias in n or n in alias for alias in wanted_aliases):
                score = 100 if any(alias == n for alias in wanted_aliases) else 50
                matched.append((score, name, prop))
        if not matched:
            if wanted == "title":
                for key, prop in prop_items(schema):
                    if prop_type(prop) == "title":
                        return prop
            raise SchemaError(f"缺少必要字段：{wanted}")
        matched.sort(key=lambda x: x[0], reverse=True)
        if matched and len(matched[0]) >= 3:
            return matched[0][2]
        else:
            raise SchemaError(f"字段匹配结果格式错误：{wanted}")
    except Exception as e:
        raise SchemaError(f"查找字段时出错：{e}") from e


def build_field_map(schema: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    fields = {}
    fields["title"] = find_property(schema, "title", ["title"])
    fields["due"] = find_property(schema, "due", ["date"])
    fields["quadrant"] = find_property(schema, "quadrant", ["select", "status", "multi_select"])
    fields["status"] = find_property(schema, "status", ["status", "select"])
    fields["note"] = find_property(schema, "note", ["rich_text", "title"])
    fields["category"] = find_property(schema, "category", ["multi_select", "select"])
    return fields


def prop_key_for_page(schema: Dict[str, Any], target_prop: Dict[str, Any]) -> str:
    target_id = prop_id("", target_prop)
    target_name = norm(prop_name("", target_prop))
    for key, prop in prop_items(schema):
        if prop_id(key, prop) == target_id or norm(prop_name(key, prop)) == target_name:
            return key
    return prop_name("", target_prop) or target_id


def option_names(prop: Dict[str, Any]) -> List[str]:
    t = prop_type(prop)
    if t in {"select", "status", "multi_select"}:
        container = prop.get(t) or {}
        opts = container.get("options") or []
        return [o.get("name") for o in opts if isinstance(o, dict) and o.get("name")]
    return []


def choose_option(prop: Dict[str, Any], preferred: List[str], fallback_first: bool = True) -> str:
    opts = option_names(prop)
    if not opts:
        raise SchemaError(f"字段 {prop.get('name', '')} 没有可用枚举值。")
    normalized_opts = [(opt, norm(opt)) for opt in opts]
    for want in preferred:
        nw = norm(want)
        for opt, no in normalized_opts:
            if no == nw or nw in no or no in nw:
                return opt
    if fallback_first:
        return opts[0]
    raise SchemaError(f"无法为字段 {prop.get('name', '')} 选择可用枚举值。")


def status_value(prop: Dict[str, Any], kind: str) -> str:
    if kind == "done":
        return choose_option(prop, DONE_STATUS_HINTS, True)
    if kind == "cancel":
        return choose_option(prop, CANCEL_STATUS_HINTS, True)
    return choose_option(prop, TODO_STATUS_HINTS, True)


def rich_text_payload(text: str) -> List[Dict[str, Any]]:
    return [{"type": "text", "text": {"content": text}}]


def page_value(page: Dict[str, Any], prop: Dict[str, Any], key_hint: str) -> Any:
    props = page.get("properties") or {}
    candidates = []
    if key_hint:
        candidates.append(key_hint)
    candidates.append(prop_id(key_hint, prop))
    candidates.append(prop_name(key_hint, prop))
    
    normalized_props = {norm(k): v for k, v in props.items()}
    for key in candidates:
        if key in props:
            return props[key]
        normalized_key = norm(key)
        if normalized_key in normalized_props:
            return normalized_props[normalized_key]
    
    target_id = prop_id(key_hint, prop)
    target_name = norm(prop_name(key_hint, prop))
    for k, v in props.items():
        if norm(k) == target_name or k == target_id:
            return v
        if isinstance(v, dict) and (v.get("id") == target_id or norm(v.get("name")) == target_name):
            return v
    return None


def extract_value(prop: Dict[str, Any], raw: Any) -> Any:
    if raw is None:
        return None
    t = prop_type(prop)
    if t == "title":
        parts = raw.get("title") or []
        return "".join([i.get("plain_text", "") if isinstance(i, dict) else "" for i in parts]).strip()
    if t == "rich_text":
        parts = raw.get("rich_text") or []
        return "".join([i.get("plain_text", "") if isinstance(i, dict) else "" for i in parts]).strip()
    if t == "date":
        d = raw.get("date") or {}
        return d.get("start")
    if t == "select":
        sel = raw.get("select") or {}
        return sel.get("name")
    if t == "status":
        st = raw.get("status") or {}
        return st.get("name")
    if t == "multi_select":
        arr = raw.get("multi_select") or []
        return [i.get("name") for i in arr if isinstance(i, dict) and i.get("name")]
    if t == "checkbox":
        return bool(raw.get("checkbox"))
    return raw


def page_to_task(page: Dict[str, Any], schema: Dict[str, Any], fields: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    out = {
        "page_id": page.get("id"),
        "url": page.get("url"),
        "created_time": page.get("created_time"),
        "last_edited_time": page.get("last_edited_time"),
    }
    for k in ("title", "due", "quadrant", "status", "note", "category"):
        prop = fields[k]
        key_hint = prop_name("", prop)
        raw = page_value(page, prop, key_hint)
        out[k] = extract_value(prop, raw)
    return out


def page_matches_open(task: Dict[str, Any]) -> bool:
    status = str(task.get("status") or "").strip()
    n = norm(status)
    return not any(h in n for h in DONE_STATUS_HINTS) and not any(h in n for h in CANCEL_STATUS_HINTS)


def page_matches_status(task: Dict[str, Any], status_list: List[str]) -> bool:
    """检查任务状态是否匹配指定状态列表"""
    status = str(task.get("status") or "").strip()
    n = norm(status)
    if not status_list:
        return True
    for s in status_list:
        if norm(s) in n or n in norm(s):
            return True
    return False


def due_date_value(task: Dict[str, Any]) -> Optional[date]:
    val = task.get("due")
    if not val:
        return None
    try:
        return datetime.strptime(val[:10], "%Y-%m-%d").date()
    except Exception:
        return None


def quadrant_score(task: Dict[str, Any]) -> int:
    quadrant = str(task.get("quadrant") or "").strip()
    if quadrant == "重要紧急":
        return 1
    elif quadrant == "紧急不重要":
        return 2
    elif quadrant == "重要不紧急":
        return 3
    elif quadrant == "不重要不紧急":
        return 4
    return 4


def is_overdue(task: Dict[str, Any]) -> bool:
    # 只对未完成的任务检查是否超时
    if not page_matches_open(task):
        return False
    due = due_date_value(task)
    if not due:
        return False
    return due < today()


def sort_tasks(tasks: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return sorted(
        tasks,
        key=lambda t: (
            not is_overdue(t),  # 反转 overdue 的排序，使 overdue 任务排在前面
            -quadrant_score(t),  # 反转 quadrant_score，使高优先级排在前面
            due_date_value(t) or date.min,  # 使日期早的排在前面
            t.get("created_time") or "",  # 保持创建时间的排序
        ),
        reverse=False,
    )


def query_data_source(api_key: str, data_source_id: str, filter_obj: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    results: List[Dict[str, Any]] = []
    cursor = None
    while True:
        body: Dict[str, Any] = {"page_size": 100}
        if filter_obj:
            body["filter"] = filter_obj
        if cursor:
            body["start_cursor"] = cursor
        payload = notion_request(api_key, "POST", f"/data_sources/{data_source_id}/query", body=body)
        results.extend(payload.get("results", []))
        if not payload.get("has_more"):
            break
        cursor = payload.get("next_cursor")
        if not cursor:
            break
    return results


def build_status_filter(fields: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    status_prop = fields["status"]
    done_name = status_value(status_prop, "done")
    cancel_name = status_value(status_prop, "cancel")
    key = prop_name("", status_prop)
    t = prop_type(status_prop)
    if t == "status":
        return {
            "and": [
                {"property": key, "status": {"does_not_equal": done_name}},
                {"property": key, "status": {"does_not_equal": cancel_name}},
            ]
        }
    return {
        "and": [
            {"property": key, "select": {"does_not_equal": done_name}},
            {"property": key, "select": {"does_not_equal": cancel_name}},
        ]
    }


def build_date_filter(fields: Dict[str, Dict[str, Any]], start: date, end: date) -> Dict[str, Any]:
    due_prop = fields["due"]
    key = prop_name("", due_prop)
    return {
        "and": [
            {"property": key, "date": {"on_or_after": start.isoformat()}},
            {"property": key, "date": {"on_or_before": end.isoformat()}},
        ]
    }


def query_tasks_in_range(api_key: str, resolved: Dict[str, Any], fields: Dict[str, Dict[str, Any]], start_date: date, end_date: date, status_list: List[str] = None) -> List[Dict[str, Any]]:
    """查询指定时间范围内的任务，支持状态过滤"""
    ds_id = resolved["data_source_id"]
    
    # 构建日期过滤器
    date_filter = build_date_filter(fields, start_date, end_date)
    
    # 构建状态过滤器
    status_filter = None
    if status_list:
        status_prop = fields["status"]
        status_key = prop_name("", status_prop)
        status_type = prop_type(status_prop)
        
        # 构建状态过滤条件
        status_conditions = []
        for status in status_list:
            if status_type == "status":
                status_conditions.append({"property": status_key, "status": {"equals": status}})
            else:
                status_conditions.append({"property": status_key, "select": {"equals": status}})
        
        if status_conditions:
            status_filter = {"or": status_conditions}
    
    # 组合过滤器
    if status_filter:
        combined_filter = {"and": [date_filter, status_filter]}
    else:
        combined_filter = date_filter
    
    # 查询指定时间范围的任务
    range_pages = query_data_source(api_key, ds_id, combined_filter)
    range_tasks = [page_to_task(p, {}, fields) for p in range_pages]
    
    # 标记超时任务
    for task in range_tasks:
        if is_overdue(task):
            task["overdue"] = True
    
    # 按状态过滤
    if status_list:
        range_tasks = [t for t in range_tasks if page_matches_status(t, status_list)]
    
    return sort_tasks(range_tasks)


def query_open_tasks_in_range(api_key: str, resolved: Dict[str, Any], fields: Dict[str, Dict[str, Any]], days: int) -> List[Dict[str, Any]]:
    """查询最近 X 天的未完成任务（保持向后兼容）"""
    start = today() - timedelta(days=days)
    end = today() + timedelta(days=days)
    return query_tasks_in_range(api_key, resolved, fields, start, end, ["未开始", "进行中"])


def query_today_tasks(api_key: str, resolved: Dict[str, Any], fields: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
    """查询今天未完成的任务（保持向后兼容）"""
    return query_tasks_in_range(api_key, resolved, fields, today(), today(), ["未开始", "进行中"])


def create_task(api_key: str, resolved: Dict[str, Any], schema: Dict[str, Any], fields: Dict[str, Dict[str, Any]], task_data: Dict[str, Any]) -> Dict[str, Any]:
    database_id = resolved["database_id"]
    
    title_prop = fields["title"]
    due_prop = fields["due"]
    quadrant_prop = fields["quadrant"]
    status_prop = fields["status"]
    note_prop = fields["note"]
    category_prop = fields["category"]
    
    properties = {}
    
    title_key = prop_key_for_page(schema, title_prop)
    properties[title_key] = {"title": rich_text_payload(task_data["title"])}
    
    due_key = prop_key_for_page(schema, due_prop)
    properties[due_key] = {"date": {"start": task_data["due_date"]}}
    
    quadrant_key = prop_key_for_page(schema, quadrant_prop)
    quadrant_value = choose_option(quadrant_prop, [task_data["quadrant"]], True)
    quadrant_type = prop_type(quadrant_prop)
    if quadrant_type == "multi_select":
        properties[quadrant_key] = {"multi_select": [{"name": quadrant_value}]}
    else:
        properties[quadrant_key] = {quadrant_type: {"name": quadrant_value}}
    
    status_key = prop_key_for_page(schema, status_prop)
    status_val = choose_option(status_prop, [task_data["status"]], True)
    status_type = prop_type(status_prop)
    properties[status_key] = {status_type: {"name": status_val}}
    
    if task_data.get("note"):
        note_key = prop_key_for_page(schema, note_prop)
        note_type = prop_type(note_prop)
        if note_type == "rich_text":
            properties[note_key] = {"rich_text": rich_text_payload(task_data["note"])}
        else:
            properties[note_key] = {"title": rich_text_payload(task_data["note"])}
    
    if task_data.get("category"):
        category_key = prop_key_for_page(schema, category_prop)
        category_type = prop_type(category_prop)
        category_value = choose_option(category_prop, [task_data["category"]], True)
        if category_type == "multi_select":
            properties[category_key] = {"multi_select": [{"name": category_value}]}
        else:
            properties[category_key] = {category_type: {"name": category_value}}
    
    body = {
        "parent": {"database_id": database_id},
        "properties": properties,
    }
    
    result = notion_request(api_key, "POST", "/pages", body=body)
    
    cache = state_load()
    cache["last_task"] = {"page_id": result["id"], "title": task_data["title"]}
    state_save(cache)
    
    return page_to_task(result, schema, fields)


def update_task_status(api_key: str, resolved: Dict[str, Any], schema: Dict[str, Any], fields: Dict[str, Dict[str, Any]], page_id: str, status_kind: str) -> Dict[str, Any]:
    status_prop = fields["status"]
    status_key = prop_key_for_page(schema, status_prop)
    status_type = prop_type(status_prop)
    status_val = status_value(status_prop, status_kind)
    
    body = {
        "properties": {
            status_key: {status_type: {"name": status_val}},
        },
    }
    
    result = notion_request(api_key, "PATCH", f"/pages/{page_id}", body=body)
    return page_to_task(result, schema, fields)


def calculate_similarity(task: Dict[str, Any], query: str) -> int:
    """计算任务与查询的相似度
    基于任务标题、备注、分类等字段计算相似度
    """
    score = 0
    
    # 标题相似度（权重最高）
    title = str(task.get("title") or "").strip()
    title_score = match_title_score(title, query)
    score += title_score * 3
    
    # 备注相似度
    note = str(task.get("note") or "").strip()
    note_score = match_title_score(note, query)
    score += note_score * 2
    
    # 分类相似度
    category = str(task.get("category") or "").strip()
    category_score = match_title_score(category, query)
    score += category_score
    
    # 四象限相似度
    quadrant = str(task.get("quadrant") or "").strip()
    quadrant_score_val = match_title_score(quadrant, query)
    score += quadrant_score_val
    
    return score


def find_task_by_text(api_key: str, resolved: Dict[str, Any], schema: Dict[str, Any], fields: Dict[str, Dict[str, Any]], text: str) -> Optional[Dict[str, Any]]:
    ds_id = resolved["data_source_id"]
    status_filter = build_status_filter(fields)
    pages = query_data_source(api_key, ds_id, status_filter)
    
    for page in pages:
        task = page_to_task(page, schema, fields)
        title = str(task.get("title") or "")
        if norm(text) in norm(title) or norm(title) in norm(text):
            return task
    return None


def search_tasks(api_key: str, resolved: Dict[str, Any], schema: Dict[str, Any], fields: Dict[str, Dict[str, Any]], query: str, limit: int = 3) -> List[Dict[str, Any]]:
    """搜索任务并返回最相似的前 N 个
    """
    ds_id = resolved["data_source_id"]
    # 搜索所有任务（包括已完成和未完成）
    pages = query_data_source(api_key, ds_id, None)
    
    tasks_with_score = []
    for page in pages:
        task = page_to_task(page, schema, fields)
        score = calculate_similarity(task, query)
        if score > 0:
            task["similarity_score"] = score
            tasks_with_score.append(task)
    
    # 按相似度排序，返回前 N 个
    tasks_with_score.sort(key=lambda x: x.get("similarity_score", 0), reverse=True)
    return tasks_with_score[:limit]


def generate_summary(tasks: List[Dict[str, Any]], days: int) -> Dict[str, Any]:
    quadrant_counts = {
        "重要紧急": 0,
        "紧急不重要": 0,
        "重要不紧急": 0,
        "不重要不紧急": 0,
    }
    
    important_urgent_tasks = []
    overdue_tasks = []
    
    for task in tasks:
        quadrant = str(task.get("quadrant") or "不重要不紧急")
        if quadrant in quadrant_counts:
            quadrant_counts[quadrant] += 1
        else:
            quadrant_counts["不重要不紧急"] += 1
        
        if quadrant == "重要紧急":
            important_urgent_tasks.append(task)
        
        if task.get("overdue"):
            overdue_tasks.append(task)
    
    total_tasks = sum(quadrant_counts.values())
    
    return {
        "days": days,
        "total_tasks": total_tasks,
        "quadrant_counts": quadrant_counts,
        "important_urgent_tasks": important_urgent_tasks,
        "overdue_tasks": overdue_tasks,
    }


def handle_bootstrap(args: Dict[str, Any]) -> None:
    api_key = args["notion_api_key"]
    database_name = args["database_name"]
    
    resolved = resolve_database(api_key, database_name)
    schema = retrieve_schema(api_key, resolved)
    fields = build_field_map(schema)
    
    # 保存字段映射到缓存
    cache = state_load()
    cache["fields"] = fields
    state_save(cache)
    
    json_output(True, "bootstrap", "数据库连接成功", {
        "resolved": resolved,
        "fields": fields,
    })


def handle_add(args: Dict[str, Any]) -> None:
    api_key = args["notion_api_key"]
    database_name = args["database_name"]
    title = args["title"]
    due_date = args["due_date"]
    quadrant = args["quadrant"]
    status = args.get("status", "未开始")
    category = args.get("category")
    note = args.get("note")
    
    resolved = resolve_database(api_key, database_name)
    schema = retrieve_schema(api_key, resolved)
    fields = build_field_map(schema)
    
    task_data = {
        "title": title,
        "due_date": due_date,
        "quadrant": quadrant,
        "status": status,
        "category": category,
        "note": note,
    }
    
    task = create_task(api_key, resolved, schema, fields, task_data)
    
    json_output(True, "add", "任务创建成功", {"task": task})


def handle_today(args: Dict[str, Any]) -> None:
    api_key = args["notion_api_key"]
    database_name = args["database_name"]
    
    resolved = resolve_database(api_key, database_name)
    schema = retrieve_schema(api_key, resolved)
    fields = build_field_map(schema)
    
    tasks = query_today_tasks(api_key, resolved, fields)
    
    # 保存状态到缓存
    cache = state_load()
    cache["fields"] = fields
    state_save(cache)
    
    json_output(True, "today", f"今天有 {len(tasks)} 个未完成任务", {"tasks": tasks})


def handle_query(args: Dict[str, Any]) -> None:
    """查询指定时间范围内的任务"""
    api_key = args["notion_api_key"]
    database_name = args["database_name"]
    start_date_str = args["start_date"]
    end_date_str = args["end_date"]
    status_list = args.get("status", ["未开始", "进行中"])
    
    # 解析日期
    try:
        start_date = datetime.strptime(start_date_str, "%Y-%m-%d").date()
        end_date = datetime.strptime(end_date_str, "%Y-%m-%d").date()
    except Exception as e:
        raise ConfigError(f"日期格式错误：{e}") from e
    
    resolved = resolve_database(api_key, database_name)
    schema = retrieve_schema(api_key, resolved)
    fields = build_field_map(schema)
    
    tasks = query_tasks_in_range(api_key, resolved, fields, start_date, end_date, status_list)
    
    # 保存状态到缓存
    cache = state_load()
    cache["fields"] = fields
    state_save(cache)
    
    json_output(True, "query", f"{start_date} 到 {end_date} 期间有 {len(tasks)} 个任务", {"tasks": tasks})


def handle_recent(args: Dict[str, Any]) -> None:
    """查询最近 X 天的未完成任务（保持向后兼容）"""
    api_key = args["notion_api_key"]
    database_name = args["database_name"]
    days = args.get("days", 3)
    
    resolved = resolve_database(api_key, database_name)
    schema = retrieve_schema(api_key, resolved)
    fields = build_field_map(schema)
    
    tasks = query_open_tasks_in_range(api_key, resolved, fields, days)
    
    # 保存状态到缓存
    cache = state_load()
    cache["fields"] = fields
    state_save(cache)
    
    json_output(True, "recent", f"最近 {days} 天有 {len(tasks)} 个未完成任务", {"tasks": tasks})


def handle_search(args: Dict[str, Any]) -> None:
    """搜索指定任务"""
    api_key = args["notion_api_key"]
    database_name = args["database_name"]
    query = args["query"]
    
    resolved = resolve_database(api_key, database_name)
    schema = retrieve_schema(api_key, resolved)
    fields = build_field_map(schema)
    
    tasks = search_tasks(api_key, resolved, schema, fields, query, limit=3)
    
    # 保存状态到缓存
    cache = state_load()
    cache["fields"] = fields
    state_save(cache)
    
    json_output(True, "search", f"找到 {len(tasks)} 个相关任务", {"tasks": tasks})


def handle_update_status(args: Dict[str, Any]) -> None:
    api_key = args["notion_api_key"]
    database_name = args["database_name"]
    page_id = args.get("page_id")
    text = args.get("text")
    status = args.get("status")
    
    if not status:
        raise ConfigError("未提供任务状态，请指定 status 参数")
    
    resolved = resolve_database(api_key, database_name)
    schema = retrieve_schema(api_key, resolved)
    fields = build_field_map(schema)
    
    if not page_id:
        cache = state_load()
        last_task = cache.get("last_task")
        if last_task:
            page_id = last_task.get("page_id")
        elif text:
            task = find_task_by_text(api_key, resolved, schema, fields, text)
            if task:
                page_id = task["page_id"]
    
    if not page_id:
        raise ConfigError("未找到任务，请提供任务 ID 或描述")
    
    # 直接使用用户指定的状态
    status_prop = fields["status"]
    status_key = prop_key_for_page(schema, status_prop)
    status_type = prop_type(status_prop)
    
    body = {
        "properties": {
            status_key: {status_type: {"name": status}},
        },
    }
    
    result = notion_request(api_key, "PATCH", f"/pages/{page_id}", body=body)
    task = page_to_task(result, schema, fields)
    
    # 保存状态到缓存
    cache = state_load()
    cache["fields"] = fields
    state_save(cache)
    
    json_output(True, "update_status", f"任务状态已更新为 {status}", {"task": task})


def handle_cancel(args: Dict[str, Any]) -> None:
    api_key = args["notion_api_key"]
    database_name = args["database_name"]
    page_id = args.get("page_id")
    text = args.get("text")
    
    resolved = resolve_database(api_key, database_name)
    schema = retrieve_schema(api_key, resolved)
    fields = build_field_map(schema)
    
    if not page_id:
        cache = state_load()
        last_task = cache.get("last_task")
        if last_task:
            page_id = last_task.get("page_id")
        elif text:
            task = find_task_by_text(api_key, resolved, schema, fields, text)
            if task:
                page_id = task["page_id"]
    
    if not page_id:
        raise ConfigError("未找到任务，请提供任务 ID 或描述")
    
    task = update_task_status(api_key, resolved, schema, fields, page_id, "cancel")
    
    # 保存状态到缓存
    cache = state_load()
    cache["fields"] = fields
    state_save(cache)
    
    json_output(True, "cancel", "任务已标记为已取消", {"task": task})


def handle_summary(args: Dict[str, Any]) -> None:
    api_key = args["notion_api_key"]
    database_name = args["database_name"]
    days = args.get("days", 15)
    
    resolved = resolve_database(api_key, database_name)
    schema = retrieve_schema(api_key, resolved)
    fields = build_field_map(schema)
    
    tasks = query_open_tasks_in_range(api_key, resolved, fields, days)
    summary = generate_summary(tasks, days)
    
    # 保存状态到缓存
    cache = state_load()
    cache["fields"] = fields
    state_save(cache)
    
    json_output(True, "summary", f"最近 {days} 天任务总结", {"summary": summary})


def get_api_key() -> str:
    """从 ~/.config/notion/api_key 文件读取 API 密钥"""
    api_key_path = Path.home() / ".config" / "notion" / "api_key"
    try:
        if not api_key_path.exists():
            raise ConfigError(f"API 密钥文件不存在：{api_key_path}")
        api_key = api_key_path.read_text(encoding="utf-8").strip()
        if not api_key:
            raise ConfigError("API 密钥文件为空")
        return api_key
    except Exception as e:
        raise ConfigError(f"读取 API 密钥失败：{e}") from e

def main() -> None:
    try:
        if len(sys.argv) < 3:
            json_output(False, "error", "用法: python3 notion_quadrant_manager.py <action> '<json_args>'", {})
            sys.exit(1)
        
        action = sys.argv[1]
        
        try:
            args = json.loads(sys.argv[2])
        except json.JSONDecodeError as e:
            json_output(False, action, f"JSON 解析失败：{e}", {})
            sys.exit(1)
        
        try:
            # 从配置文件读取 API 密钥
            api_key = get_api_key()
            # 将 API 密钥添加到 args 中
            args["notion_api_key"] = api_key
            
            if action == "bootstrap":
                handle_bootstrap(args)
            elif action == "add":
                handle_add(args)
            elif action == "today":
                handle_today(args)
            elif action == "query":
                handle_query(args)
            elif action == "recent":
                handle_recent(args)
            elif action == "search":
                handle_search(args)
            elif action == "update_status":
                handle_update_status(args)
            elif action == "cancel":
                handle_cancel(args)
            elif action == "summary":
                handle_summary(args)
            else:
                json_output(False, action, f"未知的动作：{action}", {})
                sys.exit(1)
        except NotionQMError as e:
            json_output(False, action, str(e), {})
            sys.exit(1)
        except Exception as e:
            error_message = "未知错误：{}\n{}".format(str(e), traceback.format_exc())
            json_output(False, action, error_message, {})
            sys.exit(1)
    except Exception as e:
        error_message = "严重错误：{}\n{}".format(str(e), traceback.format_exc())
        json_output(False, "error", error_message, {})
        sys.exit(1)


if __name__ == "__main__":
    main()
