---
name: mcd-kids-calendar
description: 麦麦遛娃地图——基于麦当劳 MCP 的亲子活动发现与预约助手。用户说"这周末带娃去麦当劳""附近有什么亲子活动""找个训练营""麦当劳亲子读书会""带孩子去哪玩"时使用。从对话中提取日期、地点、活动类型三类线索，按真实接口数据在地图上聚合展示可办门店，选定活动与时段后生成可分享的拼团召集令，或在人数凑齐时一次性下单。age_created: true
---

# 麦麦遛娃地图

把「周末带娃去麦当劳」从刷 App 瞎找，变成一张地图 + 一张召集令。

## 何时使用

- 「这周六下午带孩子去东单」— 带娃出门找活动
- 「附近有哪些亲子活动」— 按位置发现
- 「找个训练营」— 指向体验营类
- 「麦当劳亲子读书会还能约吗」— 指定活动查档期
- 「10月18号带孩子玩什么」— 按日期查

## 三条产品原则（不可违背）

### 一、年龄只是参考，不是条件

`mall-product-detail.partyAge` 标注建议年龄（如 4-12 岁），但实测 3 岁孩子
同样可以报名成功，`party-order-create` 不校验年龄。

所以：**绝不按年龄过滤或拦截活动。** 年龄只出现在展示文案里，且必须
标注「仅供参考，不作限制」。

若用户主动告知孩子年龄，仅用于排序时的软加权（命中加少量分），
不命中不扣分，更不隐藏任何活动。

### 二、note字段只能展示，不能用于业务判断

`mall-product-detail.note` 的官方字段说明写明：

> 纯展示文案。必须原文展示给用户，不得拆分、提取或改写为结构化形式；
> **严禁从中提取任何数值用于下单校验、价格计算、库存判断等业务逻辑**，
> 所有校验以下单接口返回结果为准。

因此：
- 「提前 3 天」→用 `query-party-store-date` 返回的真实日期倒推计算，
  不去解析 note 文字
- 成团人数 → 用 `query-party-store-session` 的 `partyMin`
- 价格 → 用 `session.price`，经`adapter.fen_to_yuan()`换算

### 三、召集令绝不包含任何凭证

官方 note 明确：「体验券经核销即失效，截屏或转发无效」。

召集令会被家长转发到社交平台，所以卡面上**严禁出现**：
券码 / 体验凭证 / 核销码 / 订单号 / 用户个人消费金额。

`card_builder._assert_no_credentials()` 会递归校验，命中即抛错。

## 工作流程

### 第 0 步：确认 Token

需要环境变量 `MCD_MCP_TOKEN`。未设置时提示用户去 open.mcd.cn 申请，
**不要让用户把 Token 贴在对话里**。

### 第 1 步：从对话提取三类线索

| 线索 | 用户说了什么 | 没说什么时 |
|---|---|---|
| **日期** | 「周六」「下个月」「10月18号」 | 查未来全部可约，按日期排序 |
| **地点** | 「东单」「望京」「朝阳」 | 按所在城市查全部 |
| **活动类型** | 「训练营」「读书会」「派对」 | 全部 5 类，筛选器默认「全部」 |

**缺省值自动补，不要追问。**

活动类型映射：

| 用户说 | 本项目类目 |
|---|---|
| 训练营 / 体验营 / 牙医 / 消防 / 职业 | 体验营 |
| 读书会 / 亲子阅读 | 读书会 |
| 生日会 / 生日派对 | 生日派对 |
| 主题派对 / 玩主题 / 海底小纵队 | 主题派对 |
| 品鉴 / 奶铁 | 品鉴会 |

若用户指定了类型但该类型在附近没有覆盖，**如实告知并给两个出口**：
放宽搜索半径，或改看附近可办的其他类型。

实测参考：北京的体验营类仅 3 家门店可办，最近的在 19.3km 外，
小小牙医在北京 0 家可办。这类情况必须主动说明，不能静默返回空结果。

### 第 2 步：拉活动库

```bash
python3 scripts/plan.py activities
```

内部走 `activity_engine.fetch_activity_library()`：
从 50 个混杂商品中筛出 24 个有效亲子活动。

实测：50 条中24 条是「到店专用」优惠券（16.9元麦辣可乐组合等）、
2 条是已结束活动、2 条是周边商品。不筛就会把券当活动推给用户。

注意 `status` 字段对已结束活动也返回 2（上架），所以必须校验上下架时间。

### 第 3 步：地图聚合（先聚合，再按需核验）

```bash
python3 scripts/plan.py stores --city 110100 --types 读书会,主题派对
```

内部走 `map_service.aggregate_stores()`。

**必须两阶段执行**，否则会超时：

- 阶段一：只查 `query-party-store`，拿到门店与活动归属（秒级）
- 阶段二：用户点开某门店时，再查该店 `query-party-store-date` +
  `query-party-store-session` 拿真实余位

实测一次全量核验是24 活动 × 数百门店 = 上千次请求，会超时。

**聚合的必要性**：实测北京东单北大街一家店可办 18 个活动，
海底小纵队单活动覆盖 457 家门店。7115 个（门店 × 活动）配对会聚合成
471 家门店。不聚合的话地图上全是重复 Marker，无法阅读。

**必须过滤不可用门店**：`businessStatus != 1`（停业）、
`onlineBusinessStatus` 为 false（不可线上订）。

### 第 4 步：选档期

```bash
python3 scripts/plan.py schedule --spu 1830 --store 1951035
```

内部走 `scheduler.build_schedule()`。

排序优先级：

1. **周末优先** —— 带娃出行的第一约束是能不能上得了班
2. **余位充足优先**
3. **时段贴合作息** —— 上午场学龄前友好；工作日上午场对上班族是灾难
4. `partyAge` 命中度（软权重，不隐藏任何活动）

场次必须按当日实时数据渲染。实测同一门店不同活动时段完全不同：
亲子读书会 10:30-12:00、过家家派对 15:30-17:00、奇妙生日 12:00-13:30。

### 第 5 步：产出（两条路）

#### 路 A：生成召集令（人没凑齐）

```bash
python3 scripts/plan.py card --spu 1830 --store 1951035 \
  --date 2026-10-18 --people 5
```

默认输出竖版（3:4，小红书/朋友圈）；加 `--horizontal` 输出横版（16:9，B 站演示）。
以及一段可直接复制的分享文案。

**为什么是「召集令」而不是「出行记录」**：实测 MCP 的 35 个 tool 中，
`party-order-create` 参数里没有任何 inviteCode / shareUrl / joinParty 字段，
且 `leftNum` 语义是「该场次整体剩余可约人数」而非「已报名人数」
（实测同店4 个日期的leftNum 全部等于 partyMax）。
说明 MCP 只支持整团一次性下单，不支持逐个加入。
所以凑人必然发生在 MCP 之外，卡片必须在下单之前产生。

**拼团下限是partyMin + 1，不是 partyMin。** 官方写的是「需超过 N 名儿童」，
5 人不行，要 6 人。这个坑极易踩错，`scheduler.gap_to_meeting()`
已处理：`need = partyMin + 1`。

#### 路 B：直接下单（人已凑齐）

**必须先展示订单摘要，拿到用户明确确认后才能下单。**

```bash
python3 scripts/plan.py order --spu 1830 --store 1951035 \
  --date 2026-10-18 --people 6 --party-type 2
```

`booking.create_order()` 有六项前置校验，任一不过即拒绝：

1. 活动可预约
2. SKU 信息完整（`skuId` 下单必传）
3. 成团方式已确认（`partyType=-1` 必须问用户，不可推断）
4. 场次余位充足
5. 距今 ≥ 3 天（用真实日期判断）
6. 人数符合成团条件

且 `user_confirmed=False` 时直接拒绝——防止 Agent 自主下单。

**本Skill 不提供取消订单能力。** `cancel-order` 会触发真实退款且不可逆，
应引导用户前往麦当劳官方渠道自行操作。

## MCP 工具使用清单

| 用途 | Tool | 参数要点 |
|---|---|---|
| 活动列表 | `mall-points-products` | 无参 |
| 活动详情 | `mall-product-detail` | `spuId` |
| 可办城市 | `query-party-city` | **`spuId` 必填**，不传返回 400 缺少参数 |
| 可办门店 | `query-party-store` | `code` + `spuId`，可选 `latitude`/`longitude` |
| 可约日期 | `query-party-store-date` | `spuId` + `storeCode` |
| 当日场次 | `query-party-store-session` | `spuId` + `storeCode` + `dateStr` |
| 下单 | `party-order-create` | 写操作，需明确确认 |

⚠️ 官方 README 把日期/场次接口写成 `query-partystore-date` 与
`query-partystore-session`，**真实名称是 `query-party-store-date` 与
`query-party-store-session`**（party-store 中间有连字符）。
按官方文档写必然调不通。

## 已知坑位速查

| 坑 | 表现 | 处理位置 |
|---|---|---|
| 返回体非 JSON | 外层包 Markdown 说明 | `adapter.extract_json` |
| 价格单位相反 | session.price 是分，order 金额是元 | `adapter.fen_to_yuan` |
| status 不可信 | 已结束活动也返回 2 | `activity_engine._check_on_sale` |
| 缺 spuId 报错 | query-party-city 返回 400 | 调用前必传 |
| 拼团下限 +1 | 5 人不成团，要 6 人 | `scheduler.gap_to_meeting` |
| 门店大量重复 | 同店多活动 | `map_service.aggregate_stores` |
| 体验营覆盖稀疏 | 北京仅 3 家，最近 19.3km | 如实告知 + 给出口 |

## 合规约束

- 不输出医疗、营养或其他专业建议
- 不对用户饮食作任何价值判断
- 不使用贬损性、误导性或暗示负面性质的饮食表述
- 不暗示与麦当劳官方有合作关系，不使用其 Logo
- 门店地理数据使用有资质的国内地图服务；离线演示仅绘制相对位置示意图
- 项目为参赛作品，非官方产品