"""
活动库引擎（模块 A）

职责
----
从 mall-points-products 返回的 50 个混杂商品中，准确筛出「真正可预约的
亲子活动」，并把每个活动补齐结构化元数据。

为什么要筛
----------
实测 mall-points-products 返回 50 条，其中：
  - 22 条是「到店专用」优惠券（16.9元麦辣可乐组合、5.9元香芋派任选…）
  - 2 条是玩具/鞋袜类周边（Nike 联名、罗技键鼠）
  - 2 条是已结束活动（一年一度品虾会、金拱门板记尝新会）
只有 24 条是可预约的亲子活动。
不筛就会把优惠券当活动推给用户。

分类映射
--------
MCP 的 catName 只有 4 个活动类目：
  主题类派对 / 生日类派对 / 读书会 / 品鉴会
其中「品鉴会」下混着亲子体验营（消防/牙医/职业）和成人品鉴（奶铁品鉴会）。
本项目按用户认知重新归类为 5 类。
"""
import time
from datetime import datetime

from adapter import call_readonly, fen_to_yuan, strip_html

# MCP 原始 catName → 本项目对外的活动类型
CATEGORY_MAP = {
    "主题类派对": "主题派对",
    "生日类派对": "生日派对",
    "读书会": "读书会",
    "品鉴会": "品鉴会",
}

# 对外暴露的 5 类（确认过的分类）
ACTIVITY_TYPES = ["读书会", "主题派对", "生日派对", "体验营", "品鉴会"]

# 只有这几个 spu 属于亲子体验营；其余「品鉴会」是成人活动
CHILD_CAMP_SPUIDS = {12907, 16158, 12897}

# partyType 枚举
PARTY_TYPE_MAP = {1: "包场", 2: "拼团", -1: "包场/拼团"}


def _is_child_camp(spu_id, name):
    """判断是否亲子体验营。"""
    return spu_id in CHILD_CAMP_SPUIDS


def _normalize_category(cat_name, spu_id, name):
    """
    把 MCP 类目归一为本项目的对外类型。

    「品鉴会」是个筐：里面既有亲子体验营，也有成人奶铁品鉴会。
    体验营归到「体验营」，其余保留「品鉴会」。
    """
    if cat_name == "品鉴会":
        return "体验营" if _is_child_camp(spu_id, name) else "品鉴会"
    return CATEGORY_MAP.get(cat_name, cat_name)


def _parse_date(text):
    """解析 '2026-12-30 23:59:59' 为 date 对象。"""
    if not text:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def _check_on_sale(up_time, down_time, today=None):
    """
    双重校验上架/下架时间，剔除已结束与未开始的活动。

    实测陷阱：status 字段一律是 2（上架），包括已结束的活动——
    所以不能只看 status，必须校验时间区间。
    """
    today = today or datetime.now().date()
    start = _parse_date(up_time)
    end = _parse_date(down_time)
    if start and start > today:
        return False, "未开始"
    if end and end < today:
        return False, "已结束"
    return True, "可约"


def fetch_activity_library(client, today=None, verbose=False):
    """
    拉取并结构化全部可预约亲子活动。

    参数
    ----
    client : McdClient
    today  : 用于校验上下架时间，默认系统当天
    verbose: True 时打印筛选过程，便于排查

    返回
    ----
    list[dict] —— 每个活动的结构化信息；已结束/未开始/非活动的被剔除
    """
    items, _ = call_readonly(client, "mall-points-products")
    if not items:
        return []

    today = today or datetime.now().date()
    total = len(items)
    candidates = []
    skipped_non_activity = 0

    for item in items:
        cat = item.get("catName")
        if cat not in CATEGORY_MAP:
            skipped_non_activity += 1
            continue
        candidates.append(item)

    if verbose:
        print(f"[活动库] 共 {total} 条 → 剔除非活动类目 {skipped_non_activity} 条 "
              f"→ 候选活动 {len(candidates)} 条")

    activities = []
    skipped_expired = 0

    for item in candidates:
        spu_id = item.get("spuId")
        detail, _ = call_readonly(client, "mall-product-detail", {"spuId": spu_id})
        detail = detail or {}

        alive, status_text = _check_on_sale(
            item.get("upTime"), item.get("downTime"), today
        )
        if not alive:
            skipped_expired += 1
            continue

        name = item.get("spuName", "")
        party_type = detail.get("partyType")
        # partyType 为空说明该商品不参与主题活动预约流程
        if party_type not in (1, 2, -1):
            skipped_expired += 1
            continue

        # skuId 下单必传。实测在 detail.skuList[0] 或 detail.defaultSkuId
        sku_id = detail.get("defaultSkuId")
        sku_list = detail.get("skuList") or detail.get("skuInfo") or []
        if not sku_id and isinstance(sku_list, list) and sku_list:
            sku_id = sku_list[0].get("skuId")

        duration_h = detail.get("partyRange")
        activities.append({
            "spuId": spu_id,
            "skuId": sku_id,
            "name": name,
            "category": _normalize_category(item.get("catName"), spu_id, name),
            "price_yuan": float(item.get("price") or 0),
            "selling": item.get("selling", ""),
            "image": item.get("spuImage", ""),
            "cover": (detail.get("images") or [item.get("spuImage", "")])[0],
            "party_type": PARTY_TYPE_MAP.get(party_type, "未知"),
            "party_type_code": party_type,
            # partyAge 仅作参考展示，不用于过滤（实测标注4-12 岁，
            # 但 3 岁孩子同样可以报名成功）
            "party_age": detail.get("partyAge") or "未标注",
            "party_people": detail.get("partyPeople") or "未标注",
            "duration_hours": round(duration_h * 0.5, 1) if duration_h else None,
            "up_time": item.get("upTime"),
            "down_time": item.get("downTime"),
            "note_raw": detail.get("note", ""),
            # note 只做展示，不做业务判断（官方字段说明明确禁止解析数值）
            "note_display": strip_html(detail.get("note", "")),
        })
        time.sleep(0.15)  # 避免触发限流

    if verbose:
        print(f"[活动库] 剔除已结束/非预约类 {skipped_expired} 条 → "
              f"最终有效活动 {len(activities)} 个")

    activities.sort(key=lambda a: (ACTIVITY_TYPES.index(a["category"])
                                   if a["category"] in ACTIVITY_TYPES else 99,
                                   a["price_yuan"]))
    return activities


def summarize(activities):
    """按类型汇总，供 UI 与文档使用。"""
    summary = {}
    for a in activities:
        summary.setdefault(a["category"], []).append(a)
    return {k: len(v) for k, v in sorted(summary.items())}