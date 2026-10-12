"""
成团下单（模块 E）

这是全项目**唯一**调用写操作工具的模块。

安全设计
--------
1. 六项前置校验全部通过才允许调用 party-order-create
2. partyType=-1（包场/拼团皆可）必须由用户明确选择，不可推断
3. 必须把「提前 3 天」「拼团未成团自动退款」等风险如实告知
4. 调用前必须拿到用户对最终订单信息的明确确认
5. 下单成功后不重复调用，失败也不自动重试（避免重复扣款）

为什么下单必须放在最后
----------------------
实测 party-order-create 一经调用即产生扣款，没有「占位」中间态。
而 note 明确写明：拼团需超过 N 名儿童才可举办，
若未能成团，预约自动取消并自动退款。

所以正确顺序是：凑人 → 一次性下单。
本模块只负责最后那一次调用，前面的召集环节由 card_builder 承担。
"""
from adapter import call_readonly
from scheduler import BOOKING_LEAD_DAYS, gap_to_meeting

PARTY_TYPE_LABEL = {1: "包场", 2: "拼团"}


class BookingNotReady(Exception):
    """前置校验未通过时抛出。message 可直接展示给用户。"""


def precheck(activity, store, session, date_str, people_count, party_type_code,
             today=None):
    """
    六项前置校验。任一不通过即拒绝下单。

    返回 (ok, checks, message)
      checks  — 逐项校验结果，用于向用户逐条说明
      message — 未通过时的用户可读提示
    """
    from datetime import datetime
    today = today or datetime.now().date()

    checks = []

    # ① 活动可预约性
    checks.append({
        "item": "活动可预约",
        "ok": activity.get("spuId") is not None,
        "detail": activity.get("name"),
    })

    # ② SKU 完整性（下单必传）
    sku_ok = activity.get("skuId") is not None
    checks.append({
        "item": "商品 SKU 信息",
        "ok": sku_ok,
        "detail": f"skuId={activity.get('skuId')}" if sku_ok else "缺少 skuId，无法下单",
    })

    # ③ 成团方式明确
    pt_ok = party_type_code in (1, 2)
    checks.append({
        "item": "成团方式已确认",
        "ok": pt_ok,
        "detail": PARTY_TYPE_LABEL.get(party_type_code, "未选择"),
    })

    # ④ 场次余位足够
    left = session.get("left_num") if session else 0
    pmax = session.get("party_max") if session else 0
    room_ok = left is not None and people_count <= (left or 0)
    checks.append({
        "item": "场次余位充足",
        "ok": room_ok,
        "detail": f"余位 {left}，需要 {people_count} 人",
    })

    # ⑤ 提前预订天数（用真实日期判断，不解析 note）
    try:
        d = datetime.strptime(date_str, "%Y-%m-%d").date()
        days = (d - today).days
        lead_ok = days >= BOOKING_LEAD_DAYS
        lead_detail = f"距今 {days} 天，需≥ {BOOKING_LEAD_DAYS} 天"
    except (ValueError, TypeError):
        days, lead_ok, lead_detail = 0, False, "日期格式异常"
    checks.append({
        "item": f"提前 {BOOKING_LEAD_DAYS} 天预订",
        "ok": lead_ok,
        "detail": lead_detail,
    })

    # ⑥ 拼团人数下限
    party_min = session.get("party_min") if session else None
    if party_type_code == 2 and party_min is not None:
        g = gap_to_meeting(party_min, left, people_count)
        group_ok = g["can_form"]
        group_detail = g["rule"] + f"，本次 {people_count} 人"
    else:
        group_ok = True
        group_detail = f"包场 {party_min} 人起订，本次 {people_count} 人"
    checks.append({
        "item": "人数符合成团条件",
        "ok": group_ok,
        "detail": group_detail,
    })

    failed = [c for c in checks if not c["ok"]]
    if failed:
        msg = "；".join(f"{c['item']}：{c['detail']}" for c in failed)
        return False, checks, msg
    return True, checks, ""


def build_order_summary(activity, store, session, date_str, people_count,
                        party_type_code, city_code):
    """
    生成订单摘要。**必须完整展示给用户确认后**，才能调用下单接口。
    """
    pt_label = PARTY_TYPE_LABEL.get(party_type_code, "未知")
    price = session.get("price_yuan") if session else activity.get("price_yuan")
    try:
        total = round(float(price) * people_count, 2)
    except (TypeError, ValueError):
        total = None

    return {
        "activity_name": activity.get("name"),
        "cover": activity.get("cover") or activity.get("image"),
        "store_name": store.get("short_name") or store.get("name"),
        "store_address": store.get("address"),
        "date": date_str,
        "time_range": (f"{session.get('time_start')}-{session.get('time_end')}"
                       if session else None),
        "party_type": pt_label,
        "party_min": session.get("party_min") if session else None,
        "people_count": people_count,
        "unit_price": price,
        "total_amount": total,
        "risk_notice": [
            f"需提前 {BOOKING_LEAD_DAYS} 天预订，退改同样需提前 {BOOKING_LEAD_DAYS} 天",
            ("拼团需超过 %s 名儿童才成团，未成团将自动取消并全额退款"
             % session.get("party_min")) if party_type_code == 2
            else "包场需达到起订人数",
            "活动当天缺席，费用无法退还",
        ],
    }


def create_order(client, activity, store, session, date_str, people_count,
                 party_type_code, city_code, user_confirmed=False):
    """
    创建主题活动订单。

    参数
    ----
    user_confirmed : 必须为 True，即用户已对订单摘要明确确认。
                     传 False 会被拒绝，防止 Agent 自主下单。

    返回
    ----
    dict —— 订单结果
    """
    if not user_confirmed:
        raise BookingNotReady(
            "未获得用户对订单信息的明确确认，拒绝下单。"
            "请先向用户展示订单摘要（活动/门店/日期/时段/人数/费用），"
            "得到确认后再调用。"
        )

    ok, checks, msg = precheck(activity, store, session, date_str,
                               people_count, party_type_code)
    if not ok:
        raise BookingNotReady(f"前置校验未通过：{msg}")

    args = {
        "spuId": activity["spuId"],
        "skuId": activity["skuId"],
        "partyType": party_type_code,
        "code": city_code,
        "storeCode": store["code"],
        "dateStr": date_str,
        "id": session["session_id"],
        "timeStart": session["time_start"],
        "timeEnd": session["time_end"],
        "leftNum": session.get("left_num"),
        "count": people_count,
    }

    # 这是唯一一处 allow_write=True
    resp = client.call_tool("party-order-create", args, allow_write=True)

    return {
        "checks": checks,
        "summary": build_order_summary(activity, store, session, date_str,
                                       people_count, party_type_code, city_code),
        "raw": resp,
        "notice": "订单已创建。体验券请前往「麦当劳APP / 微信小程序 / 支付宝小程序 → 我的订单 → 商城」查看。"
                  "体验券经核销即失效，请勿转发凭证截图。",
    }


def cancel_order_guard():
    """
    占位：取消订单需要独立确认流程，本项目暂不实现。

    官方 cancel-order 会真实取消订单并触发退款规则，
    属于不可逆操作，因此不在本 Skill 中提供自动取消能力。
    如需取消，请引导用户在麦当劳官方渠道自行操作。
    """
    return {
        "supported": False,
        "reason": "取消订单会触发真实退款且不可逆，本 Skill 不提供自动取消。"
                  "请引导用户前往麦当劳官方渠道（APP / 小程序）自行操作。",
    }