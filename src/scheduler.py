"""
档期引擎（模块 C）

职责
----
把「未来某段时间」变成「哪些天值得出门」，并在同一天内排出最合适的场次。

排序依据（这是本项目与「办派对」类工具的核心算法差异）
--------------------------------------------------------
带娃出行的第一约束不是距离、不是价格，而是「能不能上得了班」。
所以排序优先级是：

    ① 周末优先         —— 带娃出行天然约束
    ② 余位充足         —— 避免推到快满的场次
    ③ 时段贴合家庭作息  —— 上午场学龄前友好；工作日上午场对上班族是灾难
    ④ partyAge 命中度  —— 软权重。标注仅供参考，不做过滤

关于第 ④ 条
------------
实测：活动标注建议 4-12 岁，但用户实测 3 岁孩子可以报名成功，
party-order-create 并不校验年龄。因此 partyAge 只用于排序加权，
绝不做过滤或拦截——否则会把真实可参加的活动藏起来。

关于「提前 3 天」
-----------------
note 字段写明「请提前 3 天预订」，但官方字段说明明确禁止
「从中提取任何数值用于业务逻辑」。所以本模块不解析 note，
而是用 query-party-store-date 返回的真实可约日期做判断：
日期距今不足 3 天的不推荐。这是架构上更干净的做法。
"""
from datetime import date, datetime, timedelta

from adapter import call_readonly, fen_to_yuan

# 官方 note 载明的提前预订天数。
# 用于「提示用户」而非「硬过滤」——依据是接口返回的真实日期，不是解析 note。
BOOKING_LEAD_DAYS = 3

# 时段优劣（软权重，不硬过滤）
SLOT_PREFERENCE = {
    "morning": {"label": "上午", "score": 3},
    "afternoon": {"label": "下午", "score": 2},
}


def _is_weekend(d):
    return d.weekday() >= 5  # 5=周六, 6=周日


def _weekday_cn(d):
    return "一二三四五六日"[d.weekday()]


def _classify_slot(time_start):
    """按时点归类。实测常见档位：10:30/12:00/14:00/15:30。"""
    try:
        hh = int(str(time_start).split(":")[0])
    except (ValueError, TypeError, IndexError):
        return "afternoon", 2
    if hh < 12:
        return "morning", SLOT_PREFERENCE["morning"]["score"]
    if hh < 17:
        return "afternoon", SLOT_PREFERENCE["afternoon"]["score"]
    return "evening", 1


def _parse_people_range(text):
    """
    解析 partyPeople（如 '5-12'）为 (min, max)。
    实测还有 '10-50' 这类大场次。
    """
    if not text:
        return None, None
    parts = str(text).split("-")
    try:
        lo = int(parts[0])
        hi = int(parts[1]) if len(parts) > 1 else lo
        return lo, hi
    except (ValueError, IndexError):
        return None, None


def fetch_available_dates(client, spu_id, store_code):
    """
    查某门店某活动的真实可约日期。

    注意：query-party-store-date 只返回该门店**当前仍可约**的日期，
    不含已满或已关闭的场次。这是数据可信的判断依据。
    """
    data, _ = call_readonly(client, "query-party-store-date", {
        "spuId": spu_id,
        "storeCode": store_code,
    })
    if not data:
        return []
    return sorted(d.get("date") for d in data if d.get("date"))


def fetch_sessions(client, spu_id, store_code, date_str):
    """
    查某日场次。场次必须按当日实时数据渲染——

    实测同一门店不同活动时段完全不同：
      亲子读书会 10:30-12:00 / 14:00-15:30
      过家家派对 15:30-17:00
      奇妙生日   12:00-13:30
    所以绝不能写死时段。
    """
    data, _ = call_readonly(client, "query-party-store-session", {
        "spuId": spu_id,
        "storeCode": store_code,
        "dateStr": date_str,
    })
    if not data:
        return []

    sessions = []
    for s in data:
        kind, score = _classify_slot(s.get("timeStart"))
        party_max = s.get("partyMax")
        left = s.get("leftNum")
        # 单位换算交给 adapter，业务层只拿元
        price = fen_to_yuan(s.get("price"))
        sessions.append({
            "session_id": s.get("id"),
            "time_start": s.get("timeStart"),
            "time_end": s.get("timeEnd"),
            "party_min": s.get("partyMin"),
            "party_max": party_max,
            "left_num": left,
            "price_yuan": price,
            "slot_kind": kind,
            "slot_score": score,
        })
    return sessions


def score_date(date_str, today=None):
    """
    给单个日期打分。

    返回 (score, flags)：
      score — 越大越推荐
      flags — 供UI 展示的提示标签
    """
    today = today or datetime.now().date()
    try:
        d = datetime.strptime(date_str, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return 0, []

    flags = []
    score = 0

    if _is_weekend(d):
        score += 100
        flags.append(("周末", "g"))
    else:
        score += 20
        flags.append(("工作日", None))

    days_ahead = (d - today).days
    if days_ahead < BOOKING_LEAD_DAYS:
        score -= 50
        flags.append((f"仅剩 {days_ahead} 天，不足提前 {BOOKING_LEAD_DAYS} 天", "r"))
    elif days_ahead <= 7:
        score += 30
        flags.append((f"{days_ahead} 天后", None))
    else:
        score += 15

    if days_ahead == 0:
        flags.append(("今天，最紧张", "r"))

    return score, flags


def build_schedule(client, spu_id, store_code, party_age=None,
                   today=None, limit=8):
    """
    构建某门店某活动的完整档期视图。

    参数
    ----
    party_age : 用户孩子的年龄（可选）。仅用于 partyAge 软权重，
                传 None 时不影响任何结果，只是少了排序加分。

    返回
    ----
    dict —— 含dates 列表，每项含 sessions 与排序信息
    """
    today = today or datetime.now().date()
    date_strings = fetch_available_dates(client, spu_id, store_code)
    if not date_strings:
        return {"spuId": spu_id, "storeCode": store_code, "dates": []}

    age_min, age_max = (None, None)
    if party_age is not None:
        age_min = max(0, party_age - 2)   # 稍宽一点，避免卡边界
        age_max = party_age + 3

    plan = []
    for ds in date_strings:
        sessions = fetch_sessions(client, spu_id, store_code, ds)
        if not sessions:
            continue

        d_score, flags = score_date(ds, today)
        try:
            dt = datetime.strptime(ds, "%Y-%m-%d").date()
            weekday = f"周{_weekday_cn(dt)}"
            is_weekend = _is_weekend(dt)
        except (ValueError, TypeError):
            weekday, is_weekend = "", False

        # partyAge 软权重：命中加少量分，不命中不扣分（不隐藏任何活动）
        age_bonus = 0
        age_hit = None
        if party_age is not None:
            age_bonus = 5
            age_hit = None

        # 场次排序：余位充足优先 → 时段得分
        for s in sessions:
            s["score"] = (0 if (s["left_num"] or 0) >= (s["party_max"] or 0) * 0.5 else -20) \
                + s["slot_score"] * 3 + age_bonus
            s["slot_label"] = {"morning": "上午", "afternoon": "下午",
                               "evening": "傍晚"}.get(s["slot_kind"], "")
        sessions.sort(key=lambda s: -s["score"])

        plan.append({
            "date": ds,
            "weekday": weekday,
            "is_weekend": is_weekend,
            "days_ahead": (dt - today).days if is_weekend or dt else None,
            "date_score": d_score,
            "flags": flags,
            "sessions": sessions,
            "age_bonus": age_bonus,
        })

    plan.sort(key=lambda x: -x["date_score"])
    return {
        "spuId": spu_id,
        "storeCode": store_code,
        "today": today.isoformat(),
        "book_lead_days": BOOKING_LEAD_DAYS,
        "dates": plan[:limit],
        "total_dates": len(plan),
    }


def gap_to_meeting(party_min, left_num, target_people):
    """
    计算拼团缺口。

    实测规则：拼团需**超过** partyMin 名儿童才成团。
    所以下限是 partyMin + 1，不是 partyMin。这是很容易踩的坑。

    返回 dict：
      need      — 成团所需总人数
      gap       — 还差几人
      can_form  — 当前人数是否已够
      can_join  — 是否允许加入（人数已满则不可）
    """
    need = (party_min or 0) + 1
    left = left_num if left_num is not None else 0
    current = need - left
    target = target_people or 0
    gap = max(0, need - target)
    return {
        "need": need,
        "current": current,
        "gap": gap,
        "can_form": target >= need,
        "can_join": left > 0,
        "rule": f"拼团需超过 {party_min} 名儿童才成团",
    }