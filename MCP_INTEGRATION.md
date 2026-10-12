# MCP 接入说明 · 麦麦遛娃地图

本项目基于**麦当劳中国官方 MCP Server** 开发。所有功能均通过真实接口调用完成，
不使用任何模拟数据、不使用任何抓取或绕过手段。

-接入地址：`https://mcp.mcd.cn`
- 传输协议：Streamable HTTP
- 认证方式：请求头 `Authorization: Bearer <MCP Token>`
- 服务端版本：`mcd-mcp 1.0.0`（协议版本 `2024-11-05`）
- Token 申请：https://open.mcd.cn 控制台，手机号登录后激活

---

## 一、使用的 MCP 工具

本项目仅使用下列 7 个工具，其中 6 个只读、1 个写操作。

| # | 工具名 | 类型 | 用途 |
|---|---|---|---|
| 1 | `mall-points-products` | 只读 | 拉取全部可兑换商品，从中筛出亲子活动 |
| 2 | `mall-product-detail` | 只读 | 取活动元数据：`partyAge` `partyType` `partyPeople` `partyRange` `skuId` `note` |
| 3 | `query-party-city` | 只读 | 查某活动可办理的城市列表（含经纬度） |
| 4 | `query-party-store` | 只读 | 查某城市内可办理该活动的门店（含经纬度、距离、营业状态） |
| 5 | `query-party-store-date` | 只读 | 查某门店某活动的真实可约日期 |
| 6 | `query-party-store-session` | 只读 | 查某日场次：时段、余位、成团下限、单价 |
| 7 | `party-order-create` | **写操作** | 创建主题活动订单（需用户明确确认后才调用） |

>服务端实际暴露 **35 个** tool（实测 `tools/list`），官方 README 列举了 30 个。
> 本项目按业务需要选用其中 7 个。

---

## 二、调用流程

```
① mall-points-products          无参
    └ 50 条混合商品 → 筛出亲子活动类目 → 26 条候选
       │
       ├ mall-product-detail(spuId)   × 26
       │ └ 取 partyType / skuId / partyAge / partyPeople / partyRange
       │   剔除上下架时间不符者 → 24 个有效活动
       │
② query-party-city(spuId)      无参可选，spuId 必传
    └ 全国可办城市（含 cityCode / 纬度 / 经度）
       │
③ query-party-store(code, spuId, latitude?, longitude?)
    └ 该城市可承办门店，已按 distance 升序
       │
       └─阶段二（按需）───────────────────────────
          query-party-store-date(spuId, storeCode)
              └ 该店真实可约日期列表
                 │
                 └ query-party-store-session(spuId, storeCode, dateStr)
                        └ 当日场次：timeStart / timeEnd / leftNum / partyMin / partyMax / price
       │
④ party-order-create(...)      写操作，须用户明确确认
    └ 需先通过六项前置校验
```

### 两阶段执行的原因

一次全量聚合是 24 个活动 × 数百家门店 = 上千次请求，**实测会超时**。
因此必须两阶段：

- **阶段一（秒级）**：只调 `query-party-store`，拿到门店与活动归属关系
- **阶段二（按需）**：用户点开某门店时，再查该店的日期与场次

代码中由 `map_service.aggregate_stores()` 的 `verify_top` 参数控制限量核验。

### 聚合的必要性（实测数据）

| 指标 | 数值 |
|---|---|
| 24 个活动在北京的（门店 × 活动）配对总数 | **7115** |
| 聚合后的门店数 | **471** |
| 单个活动最大覆盖门店数 | 457（海底小纵队） |
| 同一家门店最多可办活动数 | **18**（北京东单北大街店） |

若不聚合，地图上会出现上百个外观相同的 Marker，完全无法阅读。

---

## 三、实测发现的接口坑位

这些都是官方文档未写明、实测踩出来的。已全部收敛到 `src/adapter.py`。

### 坑 1：返回体不是纯 JSON

工具返回形如：

```json
{"content":[{"type":"text","text":"# API Response Information\n...\n## Original Response\n\n{...}"}]}
```

真实数据在 `## Original Response` 之后。直接 `json.loads()` 会失败。

**处理**：`adapter.extract_json()` 截取该段后再解析，并兼容尾部多余内容。

### 坑 2：单位方向相反

| 字段 | 单位 | 实测值 |
|---|---|---|
| `query-party-store-session.price` | **分** | `4500` = ¥45 |
| `order-list.realTotalAmount` | **元**（字符串） | `"12"` |

两者方向相反，混用会导致金额差 100 倍。

**处理**：全部换算收敛在 `adapter.fen_to_yuan()` 与 `yuan_str_to_float()`，
业务层只拿「元」，且永远拿不到 MCP 原始响应。

### 坑 3：同一份数据有三种载体

| 工具 | 实际返回 |
|---|---|
| `list-nutrition-foods` | `data` 是**字符串**（逗号分隔表格），不是数组 |
| `query-my-coupons` | 纯 **Markdown**，无 JSON 段 |
| 空结果 | 不返回空数组，而返回中文文案（如「暂无可用优惠券」） |

**处理**：`adapter.parse_nutrition_table()` / `adapter.parse_markdown_coupons()`，
空结果一律兜底为 `[]`。

### 坑 4：`status` 字段不可信

已结束的活动（一年一度品虾会、金拱门板记尝新会）的 `status` 仍返回 `2`（上架）。

**处理**：`activity_engine._check_on_sale()` 用 `upTime`/`downTime` 双重校验。

### 坑 5：`query-party-city` 必须传 `spuId`

不传直接返回 `{"code":400,"message":"缺少参数"}`。

所以链路只能是「先定活动 → 再查城市」，不能反。

### 坑 6：官方 README 的接口名有误

官方 README 写的是：

- ~~`query-partystore-date`~~
- ~~`query-partystore-session`~~

**真实名称是** `query-party-store-date` / `query-party-store-session`
（`party-store` 中间有连字符）。按官方文档写必然调不通。

### 坑 7：拼团下限是 `partyMin + 1`

官方 note 原文：「每场人数需**超过** 5 位儿童才可举办」。

即 `partyMin=5` 时，**5 人不成团，要 6 人**。
代码中 `scheduler.gap_to_meeting()` 已处理：`need = party_min + 1`。

### 坑 8：MCP 不支持拼团邀请

`party-order-create` 的完整参数中**没有任何** `inviteCode` / `shareUrl` /
`joinParty` 字段。

再实测 `leftNum` 语义：同一门店 4 个不同日期的场次，`leftNum` 全部等于
`partyMax`（12），说明它是「该场次整体剩余可约人数」而非「已报名人数」。

**结论**：MCP 只支持整团一次性下单，不支持逐个加入团。
凑人必然发生在 MCP 之外（社交平台），这直接决定了本产品
「**召集令**」而非「出行记录」的形态。

---

## 四、`note` 字段的使用边界

`mall-product-detail.note` 的官方字段说明明确写着：

> 纯展示文案。必须原文展示给用户，不得拆分、提取或改写为结构化形式；
> **严禁从中提取任何数值用于下单校验、价格计算、库存判断等业务逻辑**，
> 所有校验以下单接口返回结果为准。

本项目严格遵守：

| 业务判断 | 数据来源 | **不**从 note 解析 |
|---|---|---|
| 提前预订天数 | `query-party-store-date` 返回的真实日期倒推 | ~~「请提前3天预订」~~ |
| 成团人数下限 | `session.partyMin` | ~~「每场需超过5位儿童」~~ |
| 单价 | `session.price`（分→元） | ~~套餐说明文字~~ |
| 餐食构成 | 纯展示 | 展示，但不做判断 |

`adapter.strip_html()` 只用于把 `note` 渲染成可读文本，
输出的 `note_display` 仅在UI 上展示，不参与任何逻辑判断。

---

## 五、安全约束

### 写操作保护

`party-order-create` 是唯一的写操作，由 `src/booking.py` 独占。
保护机制：

1. `mcd_client` 用 `WRITE_TOOLS` 集合做代码级拦截，
   默认 `allow_write=False`，调用写工具必须显式传 `allow_write=True`
2. `booking.create_order()` 要求 `user_confirmed=True`，否则直接拒绝
3. 六项前置校验：活动可预约 / SKU 完整 / 成团方式已确认 /
   余位充足 / 距今 ≥ 3 天 / 人数符合成团条件
4. `partyType = -1`（包场拼团皆可）时必须由用户明确选择，不做推断
5. 订单摘要须完整展示给用户确认后才执行
6. 失败不自动重试，避免重复扣款

### 本项目不提供取消订单能力

`cancel-order` 会触发真实退款且不可逆，因此不实现自动取消。
引导用户前往麦当劳官方渠道（APP / 微信小程序 / 支付宝小程序）自行操作。

### 召集令不含凭证

官方 note 明确：「体验券经核销即失效，截屏或转发无效」。
召集令会被转发到社交平台，因此卡面严禁出现券码、体验凭证、核销码、订单号。

`card_builder._assert_no_credentials()` 递归校验 payload，
命中 `FORBIDDEN_FIELDS` 中的字段即抛 `ValueError`。

---

## 六、Token 与隐私处理

- Token 仅从环境变量 `MCD_MCP_TOKEN` 读取，不写入任何文件、不进日志
- `mcp-config.example.json` 只含`${MCD_MCP_TOKEN}` 占位符
- `.gitignore` 排除了 `.env`、`raw_*.txt`、`probe_raw.json` 等中间产物
- 代码内置请求间隔 ≥ 0.125 秒（保守取 480 次/分钟，官方上限 600）
- 不采集、不存储、不展示用户订单号与个人消费金额

---

## 七、复现方式

```bash
export MCD_MCP_TOKEN=<你的 token>

# 1. 活动库（24 个有效活动）
python3 scripts/plan.py activities

# 2. 地图聚合（471 家门店）
python3 scripts/plan.py stores --city 110100

# 3. 查档期（真实可约日期与场次）
python3 scripts/plan.py schedule --spu 1830 --store 1951035

# 4. 生成召集令（人未凑齐）
python3 scripts/plan.py card --spu 1830 --store 1951035 \
    --date 2026-10-18 --people 5 --time 10:30

# 5. 下单校验（不加 --confirm 不执行）
python3 scripts/plan.py order --spu 1830 --store 1951035 \
    --date 2026-10-18 --people 6 --party-type 2
```

零第三方依赖，纯 Python 标准库，Python 3.9+ 可运行。

---

## 八、地图服务合规说明

麦当劳门店经纬度属于境内地理数据，需使用有资质的国内地图服务。

- **在 WorkBuddy 内运行时**：使用腾讯地图 GL JS，密钥由 WorkBuddy 代理持有，
  前端不暴露任何 key
- **离线演示站**：GitHub Pages 无法使用该代理（代理仅在 WorkBuddy 运行时生效），
  因此改用「按真实经纬度等距换算绘制的 SVG 相对位置示意图」，
  不调用任何在线地图服务，仓库内不含任何密钥

坐标使用 GCJ-02。

---

本项目为麦当劳程序员创意开发大赛参赛作品，非麦当劳官方产品。
输出仅供参考，餐品信息、价格及供应状态以麦当劳官方渠道的实时结果为准。