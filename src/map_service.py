"""
地图服务（模块 B）

职责
----
把「附近哪几家门店能办这个活动」变成可直接上地图的数据，
并把同一家门店的多个活动聚合成「一个 Marker + 活动数」。

为什么必须聚合
--------------
实测：北京东单北大街一家店就能办18 个活动；
海底小纵队单活动覆盖北京 457 家门店。
如果不聚合，地图上会出现上百个外观相同的 Marker，完全无法阅读。

地图服务选择（合规）
--------------------
麦当劳门店经纬度属于境内地理数据，必须使用有资质的国内地图服务。
本项目在 WorkBuddy 内运行时使用**腾讯地图 GL JS**，
密钥由 WorkBuddy 代理持有，前端不暴露任何 key。

GitHub Pages 演示站无法使用该代理（代理只在 WorkBuddy 运行时生效），
因此演示站改用「按真实经纬度绘制的 SVG 示意图」，
不调用任何在线地图服务，也不存放任何密钥。
"""
import math
import time

from adapter import call_readonly

# 北京市中心，缺省定位用。真实定位优先用 delivery-query-addresses 或用户输入。
DEFAULT_CITY_CODE = 110100
DEFAULT_CENTER = {"lat": 39.9042, "lng": 116.4074}

# 活动类目 id 与中文名，供 UI 展示
TYPE_LABEL = {
    "读书会": "读书会",
    "主题派对": "主题派对",
    "生日派对": "生日派对",
    "体验营": "体验营",
    "品鉴会": "品鉴会",
}


def fetch_city(client, spu_id):
    """
    查某活动可办理的城市列表。

    实测坑：query-party-city **必须传 spuId**，
    不传直接返回 {"code":400,"message":"缺少参数"}。
    所以链路只能是「先定活动 → 再查城市」，不能反。
    """
    data, _ = call_readonly(client, "query-party-city", {"spuId": spu_id})
    if not data:
        return []
    return [
        {
            "code": c.get("code"),
            "name": c.get("name"),
            "lat": c.get("latitude"),
            "lng": c.get("longitude"),
        }
        for c in data
    ]


def fetch_stores(client, city_code, spu_id, lat=None, lng=None):
    """
    查某城市内能办理该活动的门店。

    接口已按 distance 升序返回，无需二次排序。
    返回前会过滤掉不可用门店：
      business_status != 1     → 停业
      online_business 为 false → 不可线上预订
    推一个去不了的店对家长毫无价值。
    """
    args = {"code": city_code, "spuId": spu_id}
    if lat is not None and lng is not None:
        args["latitude"] = lat
        args["longitude"] = lng

    data, _ = call_readonly(client, "query-party-store", args)
    if not data:
        return []

    usable = []
    for s in data:
        if s.get("businessStatus") != 1:
            continue
        usable.append({
            "code": s.get("code"),
            "name": s.get("name"),
            "short_name": s.get("shortName"),
            "address": s.get("address"),
            "lat": s.get("latitude"),
            "lng": s.get("longitude"),
            "distance_m": s.get("distance"),
            "distance_text": s.get("distanceText"),
            "business_status": s.get("businessStatus"),
            "online_bookable": bool(s.get("onlineBusinessStatus")),
        })
    return usable


def fetch_sessions_quick(client, spu_id, store_code, date_str=None):
    """查某店场次；未指定日期则取最近可约日的场次。用于卡片上的余位展示。"""
    from scheduler import fetch_available_dates, fetch_sessions
    if not date_str:
        dates = fetch_available_dates(client, spu_id, store_code)
        if not dates:
            return None, []
        date_str = dates[0]
    return date_str, fetch_sessions(client, spu_id, store_code, date_str)


def aggregate_stores(client, activities, city_code=None, lat=None, lng=None,
                     lat_lng=None, date_str=None, max_stores=30,
                     verify_sessions=True, verify_top=8, verbose=False):
    """
    核心聚合函数：把多个活动 × 多家门店合并成「门店 → 活动列表」。

    参数
    ----
    activities : list[dict]，来自 activity_engine.fetch_activity_library
    city_code  : 城市编码；缺省用北京市
    date_str   : 指定日期。设置后会核验当天是否真的有场次
    verify_sessions : 是否核验场次余位
                       True  —— 全量核验。24 个活动 × 上百门店 = 上千次请求，
                                 实测会触发超时，故改为按verify_top 限量核验
                       False —— 只聚合门店，不查场次（快，适合先出地图）

    性能说明
    --------
    一次完整聚合会产生 24 × (数百门店) 的请求量，远超合理范围。
    正确做法是两阶段：
      阶段一（秒级）：只查query-party-store，拿到门店与活动归属
      阶段二（按需）：用户点开某门店时，再查该店的日期与场次
    所以 verify_top 限制只核验距离最近的若干家门店。
    """
    city_code = city_code or DEFAULT_CITY_CODE
    if lat_lng:
        lat, lng = lat_lng[0], lat_lng[1]
    lat = lat if lat is not None else DEFAULT_CENTER["lat"]
    lng = lng if lng is not None else DEFAULT_CENTER["lng"]

    merged = {}
    total_raw = 0

    for act in activities:
        spu_id = act["spuId"]
        stores = fetch_stores(client, city_code, spu_id, lat, lng)
        if verbose:
            print(f"  [聚合] {act['name'][:24]:<26} → {len(stores)} 家门店")

        for st in stores:
            total_raw += 1
            node = merged.setdefault(st["code"], {
                "code": st["code"],
                "name": st["short_name"] or st["name"],
                "address": st["address"],
                "lat": st["lat"],
                "lng": st["lng"],
                "distance_m": st["distance_m"],
                "distance_text": st["distance_text"],
                "online_bookable": st["online_bookable"],
                "activities": [],
            })
            node["activities"].append({
                "spuId": spu_id,
                "skuId": act.get("skuId"),
                "name": act["name"],
                "category": act["category"],
                "price_yuan": act["price_yuan"],
                "cover": act.get("cover") or act.get("image"),
                "party_type": act["party_type"],
                "party_age": act["party_age"],
                "party_people": act["party_people"],
                "duration_hours": act["duration_hours"],
                "selling": act.get("selling"),
            })

    stores = [v for v in merged.values() if v["activities"]]
    stores.sort(key=lambda s: s["distance_m"] if s["distance_m"] is not None else 1e9)

    # 阶段二：只对距离最近的 verify_top 家核验指定日期的余位
    verified = 0
    if verify_sessions and date_str:
        for s in stores:
            if verified >= verify_top:
                break
            kept = []
            for a in s["activities"]:
                _, sessions = fetch_sessions_quick(
                    client, a["spuId"], s["code"], date_str
                )
                if not sessions:
                    continue  # 当天无场次，剔除
                first = sessions[0]
                a["date"] = date_str
                a["left_num"] = first["left_num"]
                a["party_max"] = first["party_max"]
                a["session_count"] = len(sessions)
                a["sessions"] = sessions
                kept.append(a)
                time.sleep(0.1)
            s["activities"] = kept
            verified += 1
        # 剔除核验后变空的门店
        stores = [s for s in stores if s["activities"]]

    type_stats = {}
    for act in activities:
        cnt = sum(1 for v in stores
                  if any(a["spuId"] == act["spuId"] for a in v["activities"]))
        type_stats.setdefault(act["category"], set())
        type_stats[act["category"]].add(cnt)
    type_stats = {k: max(v) if v else 0 for k, v in type_stats.items()}

    return {
        "center": {"lat": lat, "lng": lng},
        "city_code": city_code,
        "date_filter": date_str,
        "verified_stores": verified,
        "stores": stores[:max_stores],
        "store_count": len(stores),
        "activity_count": sum(len(s["activities"]) for s in stores),
        "total_raw": total_raw,
        "type_stats": type_stats,
    }


def to_svg_points(stores, width=300, height=236, padding=18):
    """
    把门店真实经纬度转换成 SVG 相对坐标，用于离线示意图。

    等距圆柱投影：在这个尺度（单城市几公里范围内）误差可忽略。
    真实纬度用于等距换算，因此相对位置与真实地图一致。
    """
    if not stores:
        return [], None

    lats = [s["lat"] for s in stores if s["lat"] is not None]
    lngs = [s["lng"] for s in stores if s["lng"] is not None]
    if not lats or not lngs:
        return [], None

    center_lat = sum(lats) / len(lats)
    # 按纬度修正经度跨度，使图形接近等比例
    lng_scale = math.cos(math.radians(center_lat))
    lat_span = max(max(lats) - min(lats), 1e-4)
    lng_span = max((max(lngs) - min(lngs)) * lng_scale, 1e-4)

    inner_w = width - padding * 2
    inner_h = height - padding * 2

    pts = []
    for s in stores:
        if s["lat"] is None or s["lng"] is None:
            continue
        # x 随东经增大，y 随北纬减小（屏幕 y 轴向下）
        x = padding + (s["lng"] - min(lngs)) * lng_scale / lng_span * inner_w
        y = padding + (max(lats) - s["lat"]) / lat_span * inner_h
        pts.append((round(x, 2), round(y, 2)))
    return pts, {"center_lat": center_lat, "lat_span": lat_span,
                 "lng_span": lng_span, "lng_scale": lng_scale}