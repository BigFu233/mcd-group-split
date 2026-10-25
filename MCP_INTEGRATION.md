# MCP 接入说明 · MCP_INTEGRATION

本文说明「麦麦拼单官」**实际**使用了哪些麦当劳 MCP 能力、怎么调、为什么这么调，以及它带来的业务价值。

---

## 1. 接入的 MCP Server

| 项目 | 值 |
|---|---|
| Server 名称 | `mcd-mcp`（麦当劳官方 MCP Server） |
| 接入地址 | `https://mcp.mcd.cn` |
| 传输协议 | Streamable HTTP（JSON-RPC 2.0） |
| 协议版本 | `2025-06-18` |
| 认证方式 | 请求头 `Authorization: Bearer <MCP_TOKEN>` |
| Token 获取 | <https://open.mcd.cn/mcp> → 登录 → 控制台 → 激活 → 复制 |
| 限流 | 600 次 / 分钟，超限返回 `429` |

配置示例（脱敏，仅环境变量占位符）见 [`mcp-config.example.json`](mcp-config.example.json)。

客户端实现：[`scripts/mcd_client.py`](scripts/mcd_client.py)，**只用 Python 标准库**，不依赖第三方 MCP SDK，便于任何人复现与审计：

```python
# 握手（麦当劳 MCP 为无状态模式，握手后可直接 tools/call）
initialize()
# 调用一个 Tool，优先读 structuredContent
call_tool("query-my-coupons", {})
```

---

## 2. 实际调用的 Tool 清单

| # | Tool | 入参 | 在项目中的职责 | 业务价值 |
|:--:|---|---|---|---|
| 1 | `now-time-info` | 无 | 取服务器时间作为券时段判定的唯一基准 | 避免本地时区/时钟漂移导致「以为券还能用」 |
| 2 | `query-nearby-stores` | `searchType=2, city, keyword, beType` | 按「城市 + 关键词」定位门店 | 用户只说「临平那家」，也能落到唯一 storeCode |
| 3 | `delivery-query-addresses` | 无 | 取账号下配送地址，作为麦乐送场景入口 | 门店是否可送，由真实地址决定，不靠猜 |
| 4 | `delivery-query-stores` | `addressId, beType=2` | 地址 → 可送门店列表，拿到 **beCode** | `beCode` 是麦乐送下单/算价的必需业务编码，缺了整条链路走不通 |
| 5 | `query-meals` | `storeCode, beCode, orderType, beType` | 拉可售餐品、实时现价与原价 | 拼单明细里每一分钱都来自这里，不写死价格 |
| 6 | `query-my-coupons` | 无 | 拉券包 + 券的全部可用性约束 | **本项目最关键的输入**：券面值、生效日期、星期、时段、渠道、剩余名额 |
| 7 | `available-coupons` | 无 | 麦麦省可领券列表 | 提示「还有券可以领」，把优惠天花板再抬一层 |
| 8 | `query-store-coupons` | `storeCode, beCode, orderType, beType` | 门店级叠加券 | 有则叠加，无则如实告知「本店无门店券」 |

> 未调用 `create-order` / `calculate-price` 的下单分支：本项目定位是**只出方案、不下单**，这也符合活动规则对「方案参考」的要求。

---

## 3. 调用流程

```
① 定位门店
   query-nearby-stores(city, keyword)
        │  storeCode
        ▼
   delivery-query-addresses           → addressId
   delivery-query-stores(addressId)   → storeCode ↦ beCode
        │
② 取数（可缓存为快照）
   now-time-info  → 时间基准
   query-meals(storeCode, beCode)        → 菜单 + 实时价
   query-my-coupons                      → 券包 + 可用性约束
   available-coupons                     → 可领券
   query-store-coupons(storeCode, beCode)→ 门店券
        │  归一化为 data/{store,menu,coupons}.json
        ▼
③ 本地算账（不依赖网络，纯函数）
   券可用性校验 → 凑券最优分配 → 三种模式分摊 → 尾差归位
        │
④ 输出
   结算单.md（含 5 个板块）+ 分享版清单.txt
```

**关键设计**：② 与 ③ 之间用 JSON 快照解耦。
菜单和券是会变的外部状态，而分摊是必须可复核的确定性逻辑。拆开之后，任何人都能拿一份快照在无 Token 的情况下验证算法是否正确——这对一个「算钱」的工具是底线。

---

## 4. 券可用性校验：真正的技术含量在这里

`query-my-coupons` 返回的券结构远比「一张券 + 一个折扣数字」复杂。本项目把这些字段全部用上了：

| MCP 字段 | 含义 | 判定逻辑 |
|---|---|---|
| `tradeStartDate` / `tradeEndDate` | 生效日期区间 | 与下单日期比较，未生效 / 已过期直接判死 |
| `weekdays` | 可用星期，`1=周日 … 7=周六` | 与当前星期比对；空数组 = 全周 |
| `tradeTimePeriods[].startTime/endTime` | 可用时段 | 当前 `HH:MM` 是否落在任一区间内 |
| `orderTypes` / `beTypes` | 渠道，`1=到店` `2=外送` | 与本次取餐方式比对 |
| `totalAvailableCount` | 剩余可用张数 | `<=0` 判死，并在提示里告知剩余名额 |
| `discountInfo.discountValue` + `discountDesc` | 券面值 + 语义 | `desc` 含「价格」→ 记为**用券价**（优惠 = 原价 − 券价）；否则记为**立减** |

实测例子（2026-10-25，周日）：

```
❌ 11.9元麦辣鸡腿堡      未到生效日（2026-10-26 起）
❌ 19.9元王牌炸鸡任选2    未到生效日（2026-10-26 起）
✅ 0元小食8选1           可用
❌ 1024“码”力翻倍        限到店使用
```

人肉算账时，这 4 张券大概率会被当成「我有 5 张券，能省不少」，最终在付款页发现只有 1 张生效，再回头跟同事重新分摊——**这正是本项目要消灭的返工**。

---

## 5. 凑券与分摊算法

### 5.1 凑券：贪心最大节省

对每张通过校验的券，在所有「未被占用且范围匹配」的餐品行上计算可省金额：

```
price 型券：saving = 餐品单价 − 券面值      （要求 > 0）
cut   型券：saving = min(券面值, 餐品单价)
```

按 saving 降序分配，一张券只吃一份餐品，同一餐品最多被一张券占用。
复杂度 `O(券数 × 餐品行数)`，拼单场景（< 20 行）无需更重的算法。

### 5.2 分摊：三种模式 + 恒等式

| 模式 | 公式 |
|---|---|
| `proportional` | `实付ᵢ = 原价ᵢ − 优惠 × 原价ᵢ / Σ原价` |
| `initiator` | `实付ᵢ = 原价ᵢ`（发起人 = 原价 − 全部优惠） |
| `equal` | `实付ᵢ = 原价ᵢ − 优惠 / n` |

三者都先按 `Decimal` + `ROUND_HALF_UP` 归到分，再把残差整笔记到发起者名下，从而保证：

```
Σ 每人实付 ≡ 整单实付
```

示例（原价 ¥108.70，优惠 ¥9.50，实付 ¥99.20）：
`26.82 + 19.99 + 27.29 + 25.10 = 99.20` ✅

---

## 6. 工程细节

| 关注点 | 处理 |
|---|---|
| 凭证安全 | Token 仅从环境变量 `MCD_MCP_TOKEN` 读取，仓库内零凭证；示例券包已脱敏 |
| 限流 | `call_with_retry()` 仅对 `429` / 网络抖动做指数退避重试，业务错误立即抛出，避免放大请求 |
| 错误可读性 | `401` → 「Token 无效或已过期」，`429` → 「触发限流（600 次/分钟）」，直接给到用户可执行的提示 |
| 响应兼容 | 同时兼容 `application/json` 与 `text/event-stream`（SSE 帧解析），适配不同网关 |
| 数据归一化 | MCP 原始结构 → 统一的 `data/*.json`，引擎侧不再关心 MCP 字段命名 |
| 可复核性 | 引擎为纯函数，可离线跑、可用快照复现任何一单的结算结果 |

---

## 7. 业务价值

1. **把「券」从玄学变成确定性**——券能不能用、为什么不能用，逐张给出原因，不再靠付款页试错。
2. **把「AA」从扯皮变成一条恒等式**——每人金额相加必然等于整单实付，质疑时有据可依。
3. **把「拼单」从群里接龙变成一个结构化输入**——昵称、餐品、忌口、预算一次收齐，混乱信息自动成表。
4. **把「点餐」的决策成本前置**——在付款前就知道该选到店还是外送、该等到 10:30 还是现在下单，避免为了用券白等或白点。
