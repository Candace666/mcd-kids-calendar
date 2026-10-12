"""
召集令生成器（模块 D）

职责
----
把「我想约但还差人」变成一张可直接截图发出去的卡片。

这是本项目的传播引擎，也是与其他同类工具的根本区别。

为什么是「召集令」而不是「出行记录」
------------------------------------
实测 MCP 的 35 个 tool 中，party-order-create 的参数里
**没有任何 inviteCode / shareUrl / joinParty 字段**——
MCP 只支持「整团一次性下单」，不支持「逐个加入团」。

再实测 leftNum 语义：同一门店 4 个不同日期的场次，
leftNum 全部等于 partyMax（12），说明它是
「该场次整体剩余可约人数」，而不是「已报名人数」。

结论：凑人这件事必然发生在 MCP 之外（社交平台），
MCP 只负责最后那一次下单。所以卡片必须在**下单之前**产生，
它的身份是「召集令」，不是「出行记录」。

安全约束（硬性）
----------------
官方 note 明确：「体验券经核销即失效，截屏或转发无效」。
所以本生成器**绝不包含**：
  × 券码 / 体验凭证 / 核销码
  × 订单号
  × 用户个人消费金额
卡片会被家长转发，任何凭证都可能导致核销风险。
"""
import html
from datetime import datetime

from scheduler import BOOKING_LEAD_DAYS, gap_to_meeting

# 卡片样式：麦当劳小程序视觉语言
BRAND = {
    "gold": "#FFC72C",
    "gold_dark": "#E8A200",
    "gold_light": "#FFF3D1",
    "red": "#E5342A",
    "green": "#00A650",
    "ink": "#1A1A1A",
    "ink2": "#5B5B5B",
    "ink3": "#9A9A9A",
}

# 合规：不对外呈现任何凭证类字段
FORBIDDEN_FIELDS = frozenset({
    "coupon_code", "couponId", "couponCode", "experience_code",
    "qrcode", "qr_code", "barcode", "order_id", "orderId",
    "verification_code", "ticket_code",
})


def _assert_no_credentials(payload):
    """
    安全校验：递归检查 payload 中不含任何凭证类字段。

    这是硬性防线。即便是误传进来的，也直接抛错而非静默输出。
    """
    if isinstance(payload, dict):
        for k, v in payload.items():
            if k in FORBIDDEN_FIELDS:
                raise ValueError(
                    f"召集令严禁包含凭证类字段：{k}。"
                    "官方明确『体验券经核销即失效，截屏或转发无效』。"
                )
            _assert_no_credentials(v)
    elif isinstance(payload, (list, tuple)):
        for item in payload:
            _assert_no_credentials(item)


def build_card_data(activity, store, session, date_str, party_min,
                    people_count, extra=None):
    """
    组装召集令的数据结构。

    参数
    ----
    activity  : 活动信息（来自 activity_engine）
    store     : 门店信息（来自 map_service）
    session   : 场次信息（来自 scheduler.fetch_sessions）
    date_str  : 已选日期
    people_count : 已确定参加人数（含发起人）
    party_min : 该场次最小成团人数

    返回
    ----
    dict —— 卡片所需全部数据，已剔除所有凭证字段
    """
    gap = gap_to_meeting(party_min,
                         session.get("left_num") if session else None,
                         people_count)

    weekday = ""
    try:
        weekday = "周" + "一二三四五六日"[
            datetime.strptime(date_str, "%Y-%m-%d").weekday()]
    except (ValueError, TypeError):
        pass

    data = {
        "activity": {
            "name": activity.get("name"),
            "category": activity.get("category"),
            "price_yuan": activity.get("price_yuan"),
            "duration_hours": activity.get("duration_hours"),
            "party_type": activity.get("party_type"),
            # 适龄仅作参考展示，不作为过滤条件
            "party_age": activity.get("party_age"),
        },
        "store": {
            "name": store.get("short_name") or store.get("name"),
            "address": store.get("address"),
            "distance_text": store.get("distance_text"),
        },
        "when": {
            "date": date_str,
            "weekday": weekday,
            "time_start": session.get("time_start") if session else None,
            "time_end": session.get("time_end") if session else None,
        },
        "booking": {
            "lead_days": BOOKING_LEAD_DAYS,
            "party_min": party_min,
            "people_count": people_count,
            "need": gap["need"],
            "gap": gap["gap"],
            "can_form": gap["can_form"],
            "rule": gap["rule"],
        },
        # 只展示公共信息，不含用户个人消费金额
        "disclaimer": "本卡片不含任何券码或体验凭证。官方明确「体验券经核销即失效，截屏或转发无效」。",
    }
    if extra:
        data["extra"] = extra

    _assert_no_credentials(data)
    return data


def render_vertical_card(data, qr_hint="回复活动名加入"):
    """
    渲染竖版卡片（3:4），适合小红书图文与朋友圈分享。
    纯 HTML+内联样式，无外部依赖，可直接另存为 .html 后截图。
    """
    a = data["activity"]
    s = data["store"]
    w = data["when"]
    b = data["booking"]

    when_line = f"{w['date']}"
    if w["weekday"]:
        when_line += f"（{w['weekday']}）"
    if w["time_start"]:
        when_line += f" {w['time_start']}–{w['time_end']}"

    age_text = a.get("party_age") or "未标注"
    meta_bits = [
        f"{age_text}",
        f"{a['duration_hours']}小时" if a.get("duration_hours") else None,
        f"¥{a['price_yuan']:.0f}/人",
    ]
    meta = " · ".join([m for m in meta_bits if m])

    if b["can_form"]:
        gap_html = (
            f'<div class="gap ok">'
            f'<div class="gap-n">人数已够，可直接下单</div>'
            f'<div class="gap-t">已选 {b["people_count"]} 人 · 超过 {b["party_min"]} 人下限</div>'
            f'</div>'
        )
    else:
        gap_html = (
            f'<div class="gap">'
            f'<div class="gap-n">还差 {b["gap"]} 人</div>'
            f'<div class="gap-t">已有 {b["people_count"]} 人 · 需超过 {b["party_min"]} 人成团</div>'
            f'<div class="gap-m">未成团自动取消并全额退款</div>'
            f'</div>'
        )

    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>{html.escape(a['name'] or '亲子召集令')}</title>
<style>
*{{margin:0;padding:0;box-sizing:border-box}}
body{{background:#E8E8E8;font-family:-apple-system,BlinkMacSystemFont,"PingFang SC",sans-serif;
  display:flex;justify-content:center;padding:20px}}
.card{{width:375px;background:#fff;border-radius:18px;overflow:hidden;box-shadow:0 8px 30px rgba(0,0,0,.14)}}
.head{{background:linear-gradient(135deg,{BRAND['gold']},#FFB300);padding:20px 20px 18px;position:relative;overflow:hidden}}
.head::after{{content:'';position:absolute;right:-26px;top:-26px;width:96px;height:96px;background:rgba(255,255,255,.2);border-radius:50%}}
.eyebrow{{font-size:11px;font-weight:600;color:rgba(26,26,26,.68);letter-spacing:.6px}}
.title{{font-size:23px;font-weight:800;color:{BRAND['ink']};margin:6px 0 4px;line-height:1.25}}
.meta{{font-size:12.5px;color:rgba(26,26,26,.78)}}
.body{{padding:16px 20px}}
.row{{display:flex;gap:11px;padding:11px 0;border-bottom:1px dashed #EFEFEF;font-size:13px;color:{BRAND['ink']}}}
.row:last-child{{border:none}}
.ico{{font-size:16px;width:20px;flex:0 0 auto}}
.val{{flex:1;line-height:1.65}}
.val b{{font-weight:600}}
.sub{{font-size:11px;color:{BRAND['ink3']};display:block;margin-top:3px}}
.warn{{background:#FFF6E5;border-left:3px solid #F0A400;border-radius:8px;padding:9px 11px;
  margin-top:13px;font-size:11.5px;color:#7A5400;line-height:1.75}}
.gap{{background:linear-gradient(135deg,#FFF0F0,#FFE3E1);border:1.5px dashed #F0908A;border-radius:14px;
  padding:17px;text-align:center;margin:15px 0 0}}
.gap.ok{{background:linear-gradient(135deg,#E4F7EC,#D2F0DF);border-color:#7ED4A4}}
.gap-n{{font-size:32px;font-weight:800;color:{BRAND['red']};line-height:1.1}}
.gap.ok .gap-n{{font-size:17px;color:#00713C}}
.gap-t{{font-size:12.5px;color:#A8281F;margin-top:6px;font-weight:600}}
.gap.ok .gap-t{{color:#00713C}}
.gap-m{{font-size:11px;color:#B8574E;margin-top:5px}}
.cta{{margin:16px 20px 0;background:{BRAND['gold']};border-radius:26px;padding:14px;text-align:center;
  font-size:16px;font-weight:700;color:{BRAND['ink']}}}
.safe{{margin:14px 20px 0;background:#F7F7F7;border-radius:9px;padding:10px 12px;
  font-size:10.5px;color:{BRAND['ink3']};line-height:1.7}}
.foot{{margin:12px 20px 20px;font-size:10.5px;color:{BRAND['ink3']};text-align:center;line-height:1.7}}
</style></head><body>
<div class="card">
  <div class="head">
    <div class="eyebrow">麦麦遛娃地图 · 亲子召集</div>
    <div class="title">{html.escape(a['name'] or '')}</div>
    <div class="meta">{html.escape(meta)}</div>
  </div>
  <div class="body">
    <div class="row"><span class="ico">📍</span><span class="val">
      <b>{html.escape(s['name'] or '')}</b>
      <span class="sub">{html.escape(s.get('address') or '')} · 距你 {html.escape(s.get('distance_text') or '')}</span>
    </span></div>
    <div class="row"><span class="ico">🗓</span><span class="val">
      <b>{html.escape(when_line)}</b>
      <span class="sub">需提前 {b['lead_days']} 天预订</span>
    </span></div>
    <div class="row"><span class="ico">👶</span><span class="val">
      <b>{html.escape(b['rule'])}</b>
      <span class="sub">建议年龄 {html.escape(age_text)}（仅供参考，不作限制）</span>
    </span></div>
    <div class="row"><span class="ico">🎁</span><span class="val">
      <b>费用含儿童餐与活动礼品</b>
      <span class="sub">具体餐食构成以麦当劳官方说明为准</span>
    </span></div>
    <div class="row"><span class="ico">💬</span><span class="val">
      <b>怎么加入</b>
      <span class="sub">{html.escape(qr_hint)}，凑齐人数即可开团</span>
    </span></div>
    <div class="warn">⚠️ 需提前 {b['lead_days']} 天预订；退改同样需提前 {b['lead_days']} 天。拼团未成团将自动取消并全额退款。</div>
    {gap_html}
  </div>
  <div class="safe">🔒 {html.escape(data['disclaimer'])}</div>
</div>
</body></html>"""


def render_horizontal_card(data):
    """
    渲染横版卡片（16:9），适合 B 站视频展示与二次传播。
    """
    a = data["activity"]
    s = data["store"]
    w = data["when"]
    b = data["booking"]

    when_line = w["date"]
    if w["weekday"]:
        when_line += f" {w['weekday']}"
    if w["time_start"]:
        when_line += f" {w['time_start']}–{w['time_end']}"

    gap_text = (f"人数已够，可直接下单" if b["can_form"]
                else f"还差 {b['gap']} 人")

    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>{html.escape(a['name'] or '亲子召集令')} · 横版</title>
<style>
*{{margin:0;padding:0;box-sizing:border-box}}
body{{background:#E8E8E8;font-family:-apple-system,BlinkMacSystemFont,"PingFang SC",sans-serif;
  display:flex;justify-content:center;padding:24px}}
.card{{width:720px;background:#fff;border-radius:16px;overflow:hidden;box-shadow:0 10px 34px rgba(0,0,0,.15)}}
.top{{display:flex;gap:22px}}
.left{{flex:1;padding:26px 0 22px 26px}}
.eyebrow{{font-size:11px;color:{BRAND['ink3']};letter-spacing:.6px;font-weight:600}}
.title{{font-size:26px;font-weight:800;margin:7px 0 8px;line-height:1.2}}
.kv{{display:flex;gap:9px;font-size:12.5px;color:{BRAND['ink2']};line-height:2}}
.kv .k{{color:{BRAND['ink3']};flex:0 0 52px}}
.kv .v{{color:{BRAND['ink']};font-weight:500}}
.price{{margin-top:12px;display:flex;align-items:baseline;gap:7px}}
.price .n{{font-size:28px;font-weight:800;color:{BRAND['red']}}}
.price .u{{font-size:12px;color:{BRAND['ink3']}}}
.right{{flex:0 0 226px;background:linear-gradient(135deg,{BRAND['gold']},#FFB300);
  padding:26px 22px;display:flex;flex-direction:column;justify-content:center;text-align:center;position:relative;overflow:hidden}}
.right::after{{content:'';position:absolute;right:-20px;bottom:-20px;width:80px;height:80px;background:rgba(255,255,255,.18);border-radius:50%}}
.gap-n{{font-size:34px;font-weight:800;color:{BRAND['ink']};line-height:1.1;position:relative;z-index:1}}
.gap-t{{font-size:12px;color:rgba(26,26,26,.75);margin-top:7px;position:relative;z-index:1}}
.gap-m{{font-size:10.5px;color:rgba(26,26,26,.6);margin-top:4px;position:relative;z-index:1}}
.bar{{display:flex;border-top:1px solid #EFEFEF}}
.cell{{flex:1;padding:14px 18px;border-right:1px solid #EFEFEF}}
.cell:last-child{{border:none}}
.cell .ck{{font-size:10.5px;color:{BRAND['ink3']}}}
.cell .cv{{font-size:13.5px;font-weight:600;margin-top:4px}}
.safe{{padding:11px 26px 20px;font-size:10.5px;color:{BRAND['ink3']};line-height:1.7}}
</style></head><body>
<div class="card">
  <div class="top">
    <div class="left">
      <div class="eyebrow">麦麦遛娃地图 · 亲子召集令</div>
      <div class="title">{html.escape(a['name'] or '')}</div>
      <div class="kv"><span class="k">门店</span><span class="v">{html.escape(s['name'] or '')}{'（'+html.escape(s.get('distance_text') or '')+'）' if s.get('distance_text') else ''}</span></div>
      <div class="kv"><span class="k">时间</span><span class="v">{html.escape(when_line)}</span></div>
      <div class="kv"><span class="k">成团</span><span class="v">{html.escape(b['rule'])}</span></div>
      <div class="price"><span class="n">¥{a['price_yuan']:.0f}</span><span class="u">/ 人</span></div>
    </div>
    <div class="right">
      <div class="gap-n">{html.escape(gap_text)}</div>
      <div class="gap-t">已选 {b['people_count']} 人 · 需 {b['need']} 人成团</div>
      <div class="gap-m">未成团全额退款</div>
    </div>
  </div>
  <div class="bar">
    <div class="cell"><div class="ck">地址</div><div class="cv">{html.escape(s.get('address') or '')}</div></div>
    <div class="cell"><div class="ck">时长</div><div class="cv">{a.get('duration_hours') or '—'} 小时</div></div>
    <div class="cell"><div class="ck">建议年龄</div><div class="cv">{html.escape(a.get('party_age') or '未标注')}</div></div>
    <div class="cell"><div class="ck">提前预订</div><div class="cv">{b['lead_days']} 天</div></div>
  </div>
  <div class="safe">🔒 {html.escape(data['disclaimer'])}　本项目为麦当劳程序员创意开发大赛参赛作品，非官方产品。</div>
</div>
</body></html>"""


def render_share_text(data):
    """生成社交平台分享文案。"""
    import html as _html
    a = data["activity"]
    s = data["store"]
    w = data["when"]
    b = data["booking"]

    name = _html.unescape(str(a["name"]))
    weekday = w.get("weekday") or ""
    lines = [
        f"{w['date']}{weekday} {w.get('time_start')}-{w.get('time_end')} "
        f"{s['name']} 有{name}",
        f"¥{a['price_yuan']:.0f} 一个人，含儿童餐和活动礼品",
    ]
    if b["can_form"]:
        lines.append(f"人数已够（{b['people_count']} 人），可以直接下单")
    else:
        lines.append(f"现在还差 {b['gap']} 人成团")
    lines.append(f"{s.get('address')}（{s.get('distance_text')}）")
    lines.append("有娃的一起约～")
    return "\n".join(lines)