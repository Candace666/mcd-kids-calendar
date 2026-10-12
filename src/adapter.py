"""
适配层：把麦当劳 MCP 的「脏数据」全部收敛在这一层。

存在理由（也是本项目最值得说的一点）
--------------------------------------
麦当劳 MCP 有三个实测出来的坑，都会在上层业务代码里制造 bug：
一但业务逻辑直接碰 MCP 原始返回，这些 bug 就会散落各处、难以根治。

坑 1｜返回体不是纯 JSON
    外层包了一段 Markdown 说明，真实数据在 `## Original Response` 之后。
    直接 json.loads() 会炸。

坑 2｜单位方向相反
    query-party-store-session.price  返回「分」（4500 = ¥45）
    order-list.realTotalAmount          返回「元」（字符串 "12.5"）
    两者方向相反，混用会导致金额差 100 倍。

坑 3｜同一份数据有三种载体
    list-nutrition-foods 的 data 是**字符串**（逗号分隔表格），不是数组
    query-my-coupons 返回**纯 Markdown**，不是 JSON
    空数据不返回空数组，而返回中文文案（如「暂无可用优惠券」）

所以：本模块对外只暴露「干净的 Python 对象」。
业务模块永远拿不到 MCP 原始响应，从架构上杜绝单位错乱。
"""
import json
import re

# ---------- 坑 1：返回体解包 ----------

_MD_PREFIX = re.compile(r"^#+\s*.*?Response", re.IGNORECASE | re.DOTALL)
_MD_TABLE_ROW = re.compile(r"^\s*\|\s*(.*?)\s*\|\s*$", re.MULTILINE)


def extract_json(resp):
    """
    从 MCP 工具返回中取出真正的 JSON。

    实测返回形如：
        {"content": [{"type":"text","text":"# API Response Information\\n\\n...
                    \\n## Original Response\\n\\n{...json...}"}]}
    但部分工具（如 campaign-calendar、query-my-coupons）直接返回纯 Markdown，
    没有 JSON 段。此时返回 None，由调用方走 Markdown 解析分支。
    """
    if not isinstance(resp, dict):
        return None
    blocks = resp.get("content") or []
    for block in blocks:
        if not isinstance(block, dict):
            continue
        text = block.get("text") or ""
        if "## Original Response" in text:
            payload = text.split("## Original Response", 1)[1].strip()
            try:
                return json.loads(payload)
            except json.JSONDecodeError:
                # 有些返回在 JSON 后还有尾巴
                decoder = json.JSONDecoder()
                obj, _ = decoder.raw_decode(payload)
                return obj
    return None


def call_readonly(client, tool_name, arguments=None):
    """
    调用只读工具并自动解包。
    返回 (data, raw_text)：
      data     — 解析出的 JSON data 字段；若返回是纯 Markdown 则为 None
      raw_text — 原始文本，供 Markdown 解析使用
    """
    resp = client.call_tool(tool_name, arguments or {})
    blocks = resp.get("content") or [] if isinstance(resp, dict) else []
    raw_text = blocks[0].get("text", "") if blocks and isinstance(blocks[0], dict) else ""

    payload = extract_json(resp)
    if payload is None:
        return None, raw_text
    if isinstance(payload, dict) and payload.get("success") is False:
        # 业务层失败（如缺参数），message 就是原因
        return None, raw_text
    return payload.get("data"), raw_text


# ---------- 坑 2：单位统一 ----------

def fen_to_yuan(fen):
    """分 → 元。用于 query-party-store-session.price。"""
    try:
        return round(float(fen) / 100, 2)
    except (TypeError, ValueError):
        return None


def yuan_str_to_float(value):
    """字符串元 → float。用于 order-list.realTotalAmount（可能是 "0" 或 None）。"""
    if value is None:
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


# ---------- 坑 3：非 JSON 返回的解析 ----------

def parse_markdown_coupons(raw_text):
    """
    解析 query-my-coupons 返回的 Markdown。

    实测格式：
        ## 券名
        - **优惠**: ¥13.5 (用券价格)
        - **有效期**: 2026-10-12 10:30-2026-10-16 23:59 周一、二...
        - **领取时间**: 昨日收到
        - **标签**: 到店专用、外送专用

    注意「暂无」类空返回要兜住，不能当空数组处理。
    """
    if not raw_text or "暂无" in raw_text:
        return []

    # 去掉每张券附带的 <img> 长标签，避免干扰切分
    text = re.sub(r"<img[^>]*>", "", raw_text)
    coupons = []
    for block in re.split(r"\n## ", text)[1:]:
        lines = [ln for ln in block.split("\n") if ln.strip()]
        if not lines:
            continue
        name = lines[0].strip()
        m_price = re.search(r"\*\*优惠\*\*:\s*¥([\d.]+)", block)
        m_valid = re.search(r"\*\*有效期\*\*:\s*([^\n]+)", block)
        m_got = re.search(r"\*\*领取时间\*\*:\s*([^\n]+)", block)
        m_tag = re.search(r"\*\*标签\*\*:\s*([^\n]+)", block)
        coupons.append({
            "name": name,
            "value_yuan": float(m_price.group(1)) if m_price else None,
            "validity": m_valid.group(1).strip() if m_valid else "",
            "received": m_got.group(1).strip() if m_got else "",
            "tags": m_tag.group(1).strip() if m_tag else "",
        })
    return coupons


def parse_nutrition_table(raw_text):
    """
    解析 list-nutrition-foods。

    实测 data 是字符串，不是数组，格式：
        [160]{productName,nutritionDescription,energyKj,energyKcal,
              protein,fat,carbohydrate,sodium,calcium}:
          猪柳麦满分,null,1288,308,16,16,24,781,213
          ...
    需要按行切、逗号 split，表头在 {...} 里，行尾有冒号。
    """
    payload = extract_json({"content": [{"type": "text", "text": raw_text}]})
    if not payload:
        return []
    table = payload.get("data")
    if not isinstance(table, str):
        return []

    header_match = re.search(r"\{([^}]+)\}", table)
    if not header_match:
        return []
    headers = [h.strip() for h in header_match.group(1).split(",")]

    rows = []
    for line in table.split("\n"):
        line = line.strip().rstrip(":").strip()
        if not line or line.startswith("[") or line.startswith("{"):
            continue
        parts = [p.strip() for p in line.split(",")]
        if len(parts) != len(headers):
            continue
        row = {}
        for h, v in zip(headers, parts):
            if h == "productName":
                row[h] = v
            elif h == "nutritionDescription":
                row[h] = v if v != "null" else None
            else:
                try:
                    row[h] = float(v)
                except ValueError:
                    row[h] = None
        rows.append(row)
    return rows


def strip_html(raw_html):
    """
    清理 note 字段里的 <p> 标签。

    重要：note 的官方字段说明明确禁止「从中提取任何数值用于
    下单校验、价格计算、库存判断等业务逻辑」。
    因此本函数只用于**展示**，任何业务判断都必须来自结构化字段或
    真实接口返回（例如「提前 3 天」要用 query-party-store-date 的
    实际日期倒推，不能去解析 note 里的文字）。
    """
    if not raw_html:
        return ""
    text = re.sub(r"<br\s*/?>", "\n", raw_html)
    text = re.sub(r"</p>", "\n", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = text.replace("&amp;", "&").replace("&nbsp;", " ")
    return re.sub(r"\n{3,}", "\n\n", text).strip()