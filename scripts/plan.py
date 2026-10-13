#!/usr/bin/env python3
"""
麦麦遛娃地图 · 命令行入口

用法
----
    export MCD_MCP_TOKEN=<你的 token>
    python3 scripts/plan.py activities                    # 拉活动库
    python3 scripts/plan.py stores --city 110100          # 地图聚合
    python3 scripts/plan.py schedule --spu 1830 --store 1951035   # 查档期
    python3 scripts/plan.py card --spu 1830 --store 1951035 \
        --date 2026-10-18 --people 5 --vertical          # 生成召集令
    python3 scripts/plan.py order --spu 1830 --store 1951035 \
        --date 2026-10-18 --people 6 --party-type 2 --confirm   # 下单

设计原则
--------
- 零第三方依赖，纯 Python 标准库
- Token 只从环境变量读取
- 写操作必须显式 --confirm，绝不默认下单
"""
import argparse
import json
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

import activity_engine      # noqa: E402
import booking              # noqa: E402
import card_builder         # noqa: E402
import map_service          # noqa: E402
import mcd_client           # noqa: E402
import scheduler            # noqa: E402

OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data")


def _client():
    if not os.environ.get("MCD_MCP_TOKEN", "").strip():
        print("错误：未设置 MCD_MCP_TOKEN。")
        print("请先执行：export MCD_MCP_TOKEN=<你的 token>")
        print("Token 申请地址：https://open.mcd.cn 控制台")
        sys.exit(1)
    c = mcd_client.get_client()
    c.connect()
    return c


def _dump(obj):
    print(json.dumps(obj, ensure_ascii=False, indent=2))


# ---------- activities ----------

def cmd_activities(args):
    c = _client()
    acts = activity_engine.fetch_activity_library(c, verbose=True)
    summary = activity_engine.summarize(acts)
    print(f"\n按类型汇总（{len(acts)} 个有效活动）：")
    for k, v in summary.items():
        print(f"  {k:<8} {v} 个")
    if args.json:
        _dump(acts)
    else:
        print()
        for a in acts:
            dur = f"{a['duration_hours']}h" if a.get("duration_hours") else "—"
            print(f"  {a['name'][:34]:<36} {a['category']:<6} ¥{a['price_yuan']:<6.0f} "
                  f"{dur:<5} {a['party_type']:<7} 建议{a['party_age']}")
            print(f"       spuId={a['spuId']} skuId={a['skuId']}")


# ---------- stores ----------

def cmd_stores(args):
    c = _client()
    acts = activity_engine.fetch_activity_library(c)
    if args.types:
        want = set(args.types.split(","))
        acts = [a for a in acts if a["category"] in want]
        if not acts:
            print(f"没有类型为 {want} 的活动。可用类型："
                  f"{sorted(set(a['category'] for a in acts))}")
            return
    print(f"参与聚合的活动 {len(acts)} 个…")
    result = map_service.aggregate_stores(
        c, acts,
        city_code=args.city,
        lat_lng=(args.lat, args.lng) if args.lat and args.lng else None,
        date_str=args.date,
        verify_sessions=bool(args.date and args.verify),
        verify_top=args.verify_top,
        max_stores=args.limit,
        verbose=True,
    )
    print(f"\n聚合完成：{result['total_raw']} 个(门店×活动)配对 → {result['store_count']} 家门店")
    print(f"各类型覆盖门店数：{result['type_stats']}")
    if args.date and result["verified_stores"]:
        print(f"已核验余位的前 {result['verified_stores']} 家门店")
    print()
    for s in result["stores"][:10]:
        print(f"  {s['name'][:24]:<26} {s['distance_text']:<7} {len(s['activities'])} 个活动")
        for a in s["activities"][:3]:
            seat = ""
            if a.get("left_num") is not None:
                seat = f"  余位 {a['left_num']}/{a['party_max']}"
            print(f"      - {a['name'][:30]:<32} ¥{a['price_yuan']:<6.0f}{seat}")
    if args.json:
        _dump(result)


# ---------- schedule ----------

def cmd_schedule(args):
    c = _client()
    sch = scheduler.build_schedule(
        c, spu_id=args.spu, store_code=args.store, party_age=args.age
    )
    if not sch["dates"]:
        print("该门店该活动目前没有可约日期。")
        return
    print(f"共 {sch['total_dates']} 个可约日（今天 {sch['today']}）\n")
    for d in sch["dates"]:
        flags = " ".join(f"[{f[0]}]" for f in d["flags"])
        print(f"{d['date']} {d['weekday']}  {flags}  距今 {d['days_ahead']} 天")
        for s in d["sessions"]:
            print(f"    {s['time_start']}-{s['time_end']}  "
                  f"余位 {s['left_num']}/{s['party_max']}  "
                  f"¥{s['price_yuan']:.0f}  [{s['slot_label']}]")
    if args.json:
        _dump(sch)


# ---------- card ----------

def cmd_card(args):
    c = _client()
    acts = activity_engine.fetch_activity_library(c)
    match = next((a for a in acts if a["spuId"] == args.spu), None)
    if not match:
        print(f"活动 spuId={args.spu} 不在当前活动库中。")
        return

    stores = map_service.fetch_stores(c, args.city, args.spu, args.lat, args.lng)
    store = next((s for s in stores if s["code"] == args.store), None)
    if not store:
        print(f"门店 {args.store} 不支持活动 {args.spu}。")
        return

    sch = scheduler.build_schedule(c, args.spu, args.store)
    target = next((d for d in sch["dates"] if d["date"] == args.date), None)
    if not target:
        avail = "、".join(d["date"] for d in sch["dates"])
        print(f"{args.date} 不可约。该店可约日期：{avail}")
        return
    session = next((s for s in target["sessions"]
                    if s["time_start"] == args.time), target["sessions"][0])

    data = card_builder.build_card_data(
        match, store, session, args.date,
        session["party_min"], args.people,
    )
    os.makedirs(OUT_DIR, exist_ok=True)
    if args.horizontal:
        out = os.path.join(OUT_DIR, "card_horizontal.html")
        content = card_builder.render_horizontal_card(data)
    else:
        out = os.path.join(OUT_DIR, "card_vertical.html")
        content = card_builder.render_vertical_card(data)
    with open(out, "w", encoding="utf-8") as f:
        f.write(content)

    print("=== 分享文案（可直接复制）===")
    print(card_builder.render_share_text(data))
    print(f"\n卡片已生成：{out}")
    print("用浏览器打开后截图即可分享。")
    if args.open:
        import webbrowser
        webbrowser.open("file://" + os.path.abspath(out))


# ---------- order ----------

def cmd_order(args):
    c = _client()
    acts = activity_engine.fetch_activity_library(c)
    match = next((a for a in acts if a["spuId"] == args.spu), None)
    if not match:
        print(f"活动 spuId={args.spu} 不在活动库中。")
        return
    stores = map_service.fetch_stores(c, args.city, args.spu, args.lat, args.lng)
    store = next((s for s in stores if s["code"] == args.store), None)
    if not store:
        print(f"门店 {args.store} 不支持活动 {args.spu}。")
        return
    sch = scheduler.build_schedule(c, args.spu, args.store)
    target = next((d for d in sch["dates"] if d["date"] == args.date), None)
    if not target:
        print(f"{args.date} 不可约。")
        return
    session = next((s for s in target["sessions"]
                    if s["time_start"] == args.time), target["sessions"][0])

    if args.party_type is None:
        if match["party_type_code"] == -1:
            print("该活动包场/拼团皆可，请用 --party-type 1 或 2 明确指定。")
            return
        args.party_type = match["party_type_code"]

    ok, checks, msg = booking.precheck(match, store, session, args.date,
                                       args.people, args.party_type)
    print("=== 前置校验 ===")
    for c_ in checks:
        print(f"  {'✓' if c_['ok'] else '✗'} {c_['item']:<18} {c_['detail']}")
    if not ok:
        print(f"\n未通过：{msg}\n已拒绝下单。")
        sys.exit(1)

    summary = booking.build_order_summary(match, store, session, args.date,
                                          args.people, args.party_type, args.city)
    print("\n=== 订单摘要（请与用户逐项确认）===")
    for k, v in summary.items():
        if k == "cover":
            continue
        if k == "risk_notice":
            print("  风险提示：")
            for r in v:
                print(f"    - {r}")
        else:
            print(f"  {k:<14} {v}")

    if not args.confirm:
        print("\n未加 --confirm，未执行下单。请确认以上信息无误后加该参数。")
        return

    result = booking.create_order(c, match, store, session, args.date,
                                  args.people, args.party_type, args.city,
                                  user_confirmed=True)
    print("\n=== 下单完成 ===")
    print(result["notice"])
    if args.json:
        _dump(result["raw"])


def main():
    ap = argparse.ArgumentParser(description="麦麦遛娃地图 CLI")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("activities", help="拉取活动库")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_activities)

    p = sub.add_parser("stores", help="地图聚合门店")
    p.add_argument("--city", default=110100, type=int, help="城市编码，默认北京")
    p.add_argument("--lat", type=float)
    p.add_argument("--lng", type=float)
    p.add_argument("--types", help="活动类型，逗号分隔，如 读书会,主题派对")
    p.add_argument("--date", help="指定日期以核验余位，如 2026-10-18")
    p.add_argument("--verify", action="store_true", help="核验余位（较慢）")
    p.add_argument("--verify-top", type=int, default=8, help="核验前N 家")
    p.add_argument("--limit", type=int, default=30)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_stores)

    p = sub.add_parser("schedule", help="查询档期")
    p.add_argument("--spu", type=int, required=True)
    p.add_argument("--store", required=True)
    p.add_argument("--age", type=int, help="孩子年龄，仅作排序软权重")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_schedule)

    p = sub.add_parser("card", help="生成拼团召集令")
    p.add_argument("--spu", type=int, required=True)
    p.add_argument("--store", required=True)
    p.add_argument("--date", required=True)
    p.add_argument("--people", type=int, required=True, help="已确定参加人数")
    p.add_argument("--time", help="场次开始时间，如 10:30；缺省取第一个")
    p.add_argument("--city", default=110100, type=int)
    p.add_argument("--lat", type=float)
    p.add_argument("--lng", type=float)
    p.add_argument("--horizontal", action="store_true", help="横版 16:9")
    p.add_argument("--open", action="store_true", help="生成后打开浏览器")
    p.set_defaults(func=cmd_card)

    p = sub.add_parser("order", help="创建订单（写操作）")
    p.add_argument("--spu", type=int, required=True)
    p.add_argument("--store", required=True)
    p.add_argument("--date", required=True)
    p.add_argument("--people", type=int, required=True)
    p.add_argument("--time", help="场次开始时间")
    p.add_argument("--party-type", type=int, choices=[1, 2],
                   help="1=包场 2=拼团；活动允许两种时必须指定")
    p.add_argument("--city", default=110100, type=int)
    p.add_argument("--lat", type=float)
    p.add_argument("--lng", type=float)
    p.add_argument("--confirm", action="store_true",
                   help="确认下单。不加此参数只做校验与摘要")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_order)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()