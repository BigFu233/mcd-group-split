# WorkBuddy 开发对话上下文

> 本文件记录「麦麦拼单官 · McGroupSplit」在腾讯 WorkBuddy 中完成的完整开发过程，用于核验 WorkBuddy 专项奖励条件。
> 项目全程在 WorkBuddy Agent 模式下完成：需求拆解、MCP 接入验证、代码编写、真实数据跑通、文档生成。

- 开发时间：2026-10-25
- 开发工具：腾讯 WorkBuddy（Agent 模式）
- 操作系统：macOS
- 运行时：Python 3.13

---

## 一、起点：用户目标

用户在 WorkBuddy 对话中提出了一个角色设定需求——「麦当劳多人拼单点餐分摊助手」，核心痛点是：

1. 多人一起点麦当劳时，各自选餐混乱；
2. 券包有多张券，不知道哪张能用、能用在哪；
3. 优惠怎么在多人之间分摊，容易扯皮。

随后用户提供了麦当劳官方 MCP 文档地址 <https://open.mcd.cn/mcp/doc> 与 MCP Token，要求接入真实数据。

---

## 二、开发过程（对话纪要）

### 1. MCP 接入验证

- 阅读官方文档，确认接入方式：`https://mcp.mcd.cn`，Streamable HTTP，`Authorization: Bearer <token>`。
- 用最小 JSON-RPC 客户端做握手验证（`initialize`），确认服务端为**无状态模式**：握手后可直接 `tools/call`，无需 `Mcp-Session-Id`。
- 逐个验证 Tool 可用性：

| Tool | 结果 |
|---|---|
| `now-time-info` | ✅ 返回服务器时间 2026-10-25 09:00:09 |
| `query-my-coupons` | ✅ 返回 5 张券，含生效日期/星期/时段/渠道/名额 |
| `available-coupons` | ✅ 「免费脆薯饼」可领 |
| `query-nearby-stores` | ✅ 定位到杭州临平片区 5 家门店 |
| `delivery-query-addresses` / `delivery-query-stores` | ✅ 拿到 `storeCode → beCode` 映射 |
| `query-meals` | ✅ 门店可售 59–73 个餐品，含现价/原价 |
| `query-store-coupons` | ✅ 返回空数组（该门店无门店级券，如实记录） |

### 2. 关键踩坑（都是真实发生的）

| 坑 | 发现方式 | 处理 |
|---|---|---|
| **券有星期限制** | 5 张券里 4 张当天不可用：2 张限周一至周五，2 张限到店 | 把 `weekdays` / `tradeTimePeriods` / `orderTypes` 全部纳入校验，不做「有券就算省」 |
| **券有时段限制** | 「0元小食8选1」只在 10:30 后生效 | 校验时用服务器时间 `now-time-info` 作为基准，不用本地时钟 |
| **早餐/正餐菜单切换** | 09:00 拉到的 73 个餐品全是早餐类 | 文档中明确提示：正餐需 10:30 后重拉，避免算错 |
| **`beCode` 拿不到** | 到店搜索结果里没有 `beCode` 字段 | 改走 `delivery-query-stores` 从配送地址反查，拿到 `333047802` |
| **券码属于个人凭证** | 真实券包含 `couponCode` | 生成脱敏副本 `examples/coupons.example.json`，真实券码不入库 |

### 3. 代码实现

在 WorkBuddy 中直接生成并跑通三个模块：

- `scripts/mcd_client.py`：零依赖 MCP 客户端（JSON-RPC 2.0 / SSE 兼容 / 429 退避重试）
- `scripts/menu_sync.py`：门店 + 菜单 + 券包同步与结构归一化
- `scripts/split_engine.py`：券可用性校验 + 贪心凑券 + 三种分摊模式 + 尾差归位 + Markdown 渲染

### 4. 真实数据验证

用真实门店（麦当劳杭州临平乔司商城得来速餐厅 3330478）+ 真实券包，跑通 4 人拼单：

```
原价 ¥108.70 − 优惠 ¥9.50 = 实付 ¥99.20
26.82 + 19.99 + 27.29 + 25.10 = 99.20   ✅ 恒等式成立
```

同时正确判定 4 张券不可用并给出原因，正确识别「老王超预算 ¥0.10」。

---

## 三、WorkBuddy 在本次开发中具体做了什么

1. **需求结构化**：把一句「拼单助手」的角色描述，拆成「定位门店 → 收集成员 → 凑券 → 分摊 → 输出」五段可执行流程。
2. **MCP 接入与探测**：读文档、握手、逐 Tool 验证、记录每个 Tool 的真实返回结构。
3. **代码生成与联调**：生成三个 Python 模块并实际执行，捕获报错后修正（如 `--check` 模式下未加载 `couponsFile` 的 bug）。
4. **真实数据验证**：用真实门店与真实券包跑通全链路，输出可复核的结算单。
5. **文档与合规**：生成 README / MCP_INTEGRATION / SKILL.md，并按活动要求对 Token、券码做脱敏处理。

---

## 四、结论

本项目从「一句角色设定」到「可运行的完整仓库」，全过程在 WorkBuddy 内完成，**真实调用了麦当劳 MCP 的 8 个 Tool**，并用真实门店数据验证了算法正确性。
