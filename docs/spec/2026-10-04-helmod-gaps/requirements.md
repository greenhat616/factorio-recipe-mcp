# 需求：补齐相对 Helmod 的规划缺口

- 实施补充：[implementation.md](implementation.md)（2026-10-05，含温度、对照验收及 HTML 可视化）
- 日期：2026-10-04
- 基线：commit `b654adf`（计算模式、插件 / 信标、方案管理、整数模式、方案对比均已完成，见 [../2026-10-04-planner-modes-and-plans/](../2026-10-04-planner-modes-and-plans/requirements.md)）
- 相关文档：[design.md](design.md)、[tasks.md](tasks.md)
- 对照对象：Helmod 2.3.x（GitHub `Helfima/helmod` master，2025-11-21），已读源码 `src/dialog/ProductionPanel.lua`、`src/data/ModelCompute.lua`、`src/math/SolverLinkedMatrixAlgebra.lua` 与英文 locale

## 1. 背景

上一期之后，与 Helmod 对比仍有以下缺口（按 Nullius 规划中遇到的频率排序）：

| # | 缺口 | Helmod 对应能力 | 现状 |
|---|---|---|---|
| 1 | 区块不能吃掉另一个区块的副产物 | 子区块 Ingredient input（`by_product=false`）从上层产出自动取量；Consumer block | `consume` 只能填数字；块间引用只用于目标，且只取对方的**导入**量 |
| 2 | 燃烧类机器的燃料不进计算 | 机器选燃料，燃料作为原料、燃烧产物（灰、乏燃料棒）作为产物 | 只报告 `fuel_MW` |
| 3 | 热能来源不进计算 | 能源配方（反应堆、换热器）可加入区块 | Nullius 的换热器（`nullius-heat-exchanger-*`，热能驱动的沸腾配方）在甲醇等方案里大量出现，只报告热耗，不算反应堆和燃料棒 |
| 4 | 不能按发电规划 | 虚拟配方：太阳能、蓄电池、蒸汽机、发电机等，可把功率当目标 | 电力只作为消耗报告；涡轮配方（`turbine-open/closed`）被自动发现排除；不能把"发 X MW"当目标，也不能让方案自供电 |
| 5 | 不会自动补出供应 / 消耗区块 | 从缺口一键生成并链接子区块；区块树 | 只有平铺区块 + 手写引用 |
| 6 | 没有区块份数 | Assembler limitation（`by_limit` + `block.count`）按份显示台数 | 无 |
| 7 | 结果没有可供可视化的结构 | 产线图（配方—物品连线）、区块树、汇总面板 | 结果是产线列表 + 物品流表，调用方要自己从 `inputs` / `outputs` 拼图；块间联动关系只存在于请求里 |
| 8 | 流体温度只给警告 | 链接时检查流体温度 | 不同温度视为同一物品。当前 Nullius 数据中**没有**任何带温度要求或指定产出温度的配方（已扫描），只对其他模组组合有意义 |

Nullius 能源链的实际形态（已从当前导出数据核实）：

- 电：沸腾配方（`boiling` / `pressure-boiling`，在热能驱动的换热器或燃烧室中）产蒸汽 → 涡轮配方（`nullius-burn-*-steam`，类别 `turbine-open/closed`）产 `nullius-energy` 流体 → `generator` 原型 `nullius-turbine-generator-*` 燃烧 `nullius-energy`（`burns_fluid=true`，`fuel_value=10kJ`，`effectivity` 0.85–1，`max_power_output` 1 / 2.5 / 6 MW）。另有太阳能板（`solar-panel`，100–800 kW）、风机（`electric-energy-interface`，名义 1.5 / 4 / 12 MW，实际出力由 `scripts/wind.lua` 按风速与启动设置 `nullius-wind-turbine-energy-multiplier` 改写）、斯特林机。
- 热：`reactor` 原型——地热反应堆（`void` 能源，10 / 25 / 60 MW）、`nullius-reactor`（burner，燃料类别 `nullius-nuclear`，50 MW，燃料棒有 `burnt_result`）、太阳能集热器（`fluid` 能源，消耗 `nullius-solar-flux`）。热能消费者为 `energy_source.type="heat"` 的机器。
- 燃烧类制造机：只有原版熔炉 / 燃烧矿机（Nullius 中基本不用）；燃料主要出现在反应堆。

## 2. 目标与非目标

### 目标

- G1：**块间物料联动**：引用可取对方的导入、溢出或净产出，可乘系数；`consume` 也可以是引用（吃掉另一个区块的副产物）。
- G2：**能源平衡模式**：电、热作为可平衡的伪物品；燃料、反应堆、发电机进入 LP，方案可以自供电、自供热，也可以把功率当目标。
- G3：**补区块操作**：从一个区块的缺口或副产物一键生成供应 / 消耗区块并自动链接；工厂汇总给出建议。
- G4：**区块份数**：按 N 份等分并报告每份台数。
- G5：**流体温度**（P2）：有温度约束的模组中按温度区分流体。
- G7：**预留可视化能力**：求解结果可附带版本化的图数据（节点、边、流量、层级），产线级与工厂级各一份；渲染（Mermaid / DOT / HTML）本期只预留接口。
- G6：默认行为不变：不开启新选项时，现有工具的结果逐字段一致（只增不改）。

### 非目标

- 区块嵌套树与 GUI：平铺区块 + 引用 + 补区块操作已能表达同样的依赖，见 design §6.4。
- 本期不实现任何渲染器或图形界面：只输出图数据，渲染格式只占位（US-D3）。
- Helmod 矩阵求解器的主产物 / 排除产物约束：LP 的目标函数与 `surplus_items` / `imports` 已覆盖其用途。
- 蓄电池调度、昼夜曲线、电网瞬时峰值：只算平均功率；蓄电池只按比例估算（P2）。
- 反应堆相邻加成的几何推导：相邻数由调用方给出。
- 品质、腐坏、物流吞吐（沿用上一期边界）。

## 3. 术语

| 术语 | 含义 |
|---|---|
| 能源伪物品 | `energy:electric`（电）、`energy:heat`（热）；数值单位恒为 **MW**，不随 `per` 换算 |
| 能源模式 | `energy_mode`：`report`（默认，现状：只报告功耗）或 `balance`（电、热、燃料进入物料平衡） |
| 伪配方 | 由实体原型生成的"配方"，与现有 `mining:<resource>` 同类：`generate:<发电机>`、`solar:<太阳能板>`、`wind:<风机>`、`heat:<反应堆>` |
| 燃料 | 燃烧类能源（`burner`，或 `burns_fluid` 的 `fluid` 能源）消耗的物品或流体 |
| 引用来源 | 引用取对方结果中的 `imports`（默认）、`surplus` 或 `net_outputs` |
| 份数 | `copies`：一个区块按几份相同的模块建造 |
| 产线图 | 一次求解的二部图：产线节点与物品节点，边为物品流量；另有导入、目标、溢出、处置等端点节点 |
| 工厂图 | 一个方案的区块级图：区块节点与物品节点，边为块的输入输出与块间引用 |

## 4. 用户故事与验收标准

优先级：**P0** 本期必须，**P1** 本期应做，**P2** 可延后。

### A. 块间联动与补区块

#### US-A1 引用来源与系数（P0）

> 作为玩家，我想让"甲醇块"的产量等于"塑料块"导入的甲醇的 1.1 倍（留余量），或者等于"气体块"多出来的氧气。

验收标准：
1. 引用新增 `of: "imports" | "surplus" | "net_outputs"`（默认 `imports`，与现有行为一致）与 `factor`（默认 1）：值 = `factor` × Σ 被引用块的该项 + `plus`。
2. `net_outputs` 指被引用块的达成目标 + 溢出 − 导入 − 消耗（只取正值）。
3. 被引用块没有该物品时取 0 并给警告（现有行为）。
4. 旧方案文件（无 `of` / `factor`）读出后行为不变。

#### US-A2 消耗引用：吃掉其他区块的副产物（P0）

> 作为玩家，气体块每秒多出约 36 压缩氧气，我想新建一个块把它全部用掉，并随气体块的规模自动变化。

验收标准：
1. 块请求的 `consume` 值可以是引用（默认 `of: "surplus"`），求解时解析为数字后按现有 `consume` 语义求解（恰好用完）。
2. 依赖排序把 `consume` 引用也算作依赖；环检测覆盖目标与消耗两类引用。
3. 解析值 ≤ 1e-9 时该消耗项从本次求解中去掉并给警告（`consume` 要求正值）。
4. 工厂账本中，被引用块的溢出被消耗块吃掉的部分计入 `internal_transfers`，不再进入处置。
5. 合成验收：A 块溢出 40 o，B 块 `consume: {o: {from: [A]}}` → B 消耗 40，工厂处置 0；`factor: 0.5` → 消耗 20、处置 20。

#### US-A3 一键补区块（P1）

> 作为玩家，我看到工厂汇总里还要导入 31 b/s，想直接生成一个供应 b 的区块并自动链接。

验收标准：
1. `plan_edit` 新增 `add_supply_block`（`for_block`、`item`、`new_id`、可选 `request` 覆盖字段）：新块目标为 `{item: {from: [for_block]}}`，并继承原块的 `defaults`、`validate_stage`、`objective`。
2. 新增 `add_consumer_block`（`for_block`、`item`、`new_id`、`lines` 或 `targets`）：新块 `consume: {item: {from: [for_block], of: "surplus"}}`。
3. `for_block` 不导入（或不溢出）该物品时仍创建，但给警告。
4. `plan_solve` 的工厂结果新增 `suggestions`：对每个净输入给出 `add_supply_block` 建议，对每个需要处置的副产物给出 `add_consumer_block` 建议（只是建议，不自动执行）。

#### US-A4 区块份数（P1）

> 作为玩家，我要把 60 甲醇/秒拆成 4 个相同的模块分散建造，想知道每个模块各要几台。

验收标准：
1. `solve_production` 与块请求新增 `copies: int ≥ 1`（默认 1）；目标、消耗、约束都是全块总量。
2. 结果新增 `per_copy`：每条产线每份的小数台数与取整台数，以及每份的信标数、插件清单；`copies_totals.machines_ceil` = Σ 每份取整 × 份数。
3. `integer_machines=true` 时按 1 份（目标 / 份数）做整数求解，再乘份数。
4. `copies=1` 时结果与现状一致。

### B. 能源

#### US-B1 能源平衡模式与伪物品（P0）

验收标准：
1. `solve_production` 新增 `energy_mode: "report" | "balance"`（默认 `report`，结果与现状逐字段一致）。
2. `balance` 下：电力机器每台消耗 `energy:electric` = 现有 `power_W`（含待机与信标）；热能机器每台消耗 `energy:heat` = 有效能耗；燃料按 US-B2 进入平衡。
3. 能源伪物品的目标、导入上限、溢出、影子价格单位恒为 MW，与 `per` 无关；物品流表中注明单位。
4. 能源伪物品默认**不可导入**；写进 `imports` 才视为外部电网 / 外部热源。`limits.imports` 同样可用（例如"外部电网最多 20 MW"）。
5. 能源伪物品允许溢出（弃电、散热），不进入副产物处置。
6. `limits.power_MW` 口径不变（总耗电）。

#### US-B2 燃烧类机器的燃料（P0）

> 作为玩家，我想知道 `nullius-reactor` 每分钟要几根裂变燃料棒、产出几根乏燃料棒。

验收标准：
1. 产线新增 `fuel`（物品或流体名）；`defaults.fuel` 为优先级列表，选第一个燃料类别匹配、且当前阶段可制造的燃料。
2. `balance` 下燃烧类机器每台每秒消耗燃料 = 有效能耗 / (燃料热值 × 能源效率)，有 `burnt_result` 时同量产出燃烧产物。
3. `burns_fluid` 的流体能源按流体 `fuel_value` 同样计算。
4. 燃料不兼容（类别不符、无热值）报错；`report` 模式下 `fuel` 只用于报告每台燃料消耗，不进平衡。
5. 合成验收：50 MW、效率 1 的反应堆烧 4 GJ 燃料棒 → 每台 0.0125 根/秒，乏燃料棒同量。

#### US-B3 热源：反应堆伪配方（P0）

> 作为玩家，甲醇方案里的换热器要 8.5 MW 热，我想知道要几座地热反应堆，或者几座核反应堆加多少燃料棒。

验收标准：
1. 伪配方 `heat:<reactor>`：每台产出 `energy:heat` = `consumption` × 能源效率 × (1 + 相邻加成 × `neighbours`)，燃料按 US-B2。
2. `void` 能源的反应堆（地热）无输入；`fluid` 能源的反应堆按流体消耗计算（太阳能集热器消耗 `nullius-solar-flux`）。
3. `balance` 下自动发现会把 `heat:*` 作为 `energy:heat` 的生产者。
4. 产线新增 `neighbours`（默认 0，仅对有 `neighbour_bonus` 的反应堆有意义）。
5. 真实数据验收：10 甲醇/秒在 `balance` 下热平衡闭合，热源产线出现在结果中，`energy:heat` 平衡误差 < 1e-6。

#### US-B4 发电：发电机、太阳能、风机伪配方（P0）

> 作为玩家，我想规划一个 30 MW 的涡轮发电站，或者让整个方案自供电并知道要多少蒸汽。

验收标准：
1. 伪配方 `generate:<generator>`：每台产出 `energy:electric` = `max_power_output`（`burns_fluid` 时消耗流体 = 功率 / (热值 × 效率)）；非燃烧型（蒸汽机）按 `fluid_usage_per_tick` × 温差 × 热容 × 效率计算功率。
2. `solar:<panel>`：每台产出 = `production` × `solar_factor`（请求参数，默认 0.7，见 §6）。
3. `wind:<eei>`：每台产出 = `energy_production` × `wind_factor`；Nullius 风机出力由脚本改写，**未给出 `wind_factor` 时报错**，不猜默认值。
4. `balance` 下以 `energy:electric` 为目标（单位 MW）即可规划发电站；自动发现把 `generate:*` / `solar:*` / `wind:*` 作为生产者，并放开涡轮配方类别（`turbine-open`、`turbine-closed`）。
5. 发电伪配方不受插件影响；阶段校验按实体可建造性。
6. 合成验收：1 MW、效率 0.9、燃料 10 kJ 的发电机，目标 9 MW → 9 台，消耗 1000 单位/秒。
7. 真实数据验收：`energy:electric` 目标 10 MW、`balance`，自动发现出 沸腾 → 涡轮配方 → 涡轮发电机 链，电平衡闭合。

#### US-B5 能源汇总（P1）

验收标准：
1. `totals` 新增 `electric_generation_MW`、`heat_generation_MW`、`heat_consumption_MW`；`balance` 下 `net_electric_MW` = 发电 − 耗电。
2. 工厂汇总把能源伪物品与物料同样做块间抵消（共享电网 / 热网假设，写入 `assumptions`）。

### C. 流体温度（P2）

#### US-C1 按温度区分流体

验收标准：
1. 只有当本次候选产线中存在带温度要求的原料或指定温度的产物时才启用；否则结果与现状一致（Nullius 当前恒为此情况）。
2. 产物按温度区分为 `fluid:<名>@<温度>`；原料温度范围通过零成本"温度匹配"变量从符合范围的产物取量。
3. 发电机的温度要求（`maximum_temperature`）同样适用。
4. 合成验收：165° 与 500° 两种蒸汽，只接受 ≥ 500° 的消费者只取 500° 的那一种。

### D. 可视化预留

#### US-D1 产线图数据（P1）

> 作为玩家（或调用方的前端），我想把一次求解画成"配方—物品—配方"的流程图或桑基图，而不用自己从产线列表拼。

验收标准：
1. `solve_production` 新增 `graph: "none" | "bipartite"`（默认 `none`，结果不变）；`bipartite` 时结果附带 `graph`。
2. 节点：每条产线（`line:<产线id>`）、每个物品（`item:<物品键>`），以及端点 `import:<键>`、`supply:<键>`（consume）、`target:<键>`、`surplus:<键>`、`dispose:<键>`；处置机器作为产线节点（`kind: "disposal"`）。
3. 边只连产线与物品（以及端点与物品），方向为物料流向，带 `rate` 与 `unit`（物料按 `per`，能源为 MW）。图是精确的：每个物品节点流入之和 = 流出之和（误差 < 1e-6）。
4. 节点带布局提示 `depth`：从目标端点逆流向的最短距离（环中节点取可达的最小值），产线节点带台数、机器、插件、信标、功率摘要，供直接标注。
5. 可选 `graph_options.allocate=true`：额外给出"产线 → 产线"的边，同一物品按各生产者占比分摊给各消费者，并标注 `allocated: true`（按比例分摊是假设，不代表实际传送带连接）。
6. 图带 `schema_version`，节点 id 稳定（同一请求重复求解 id 相同），便于前端做增量更新。

#### US-D2 工厂图数据（P1）

> 作为玩家，我想看到整个塑料工厂里各区块之间谁给谁供料、外部要输入什么。

验收标准：
1. `plan_solve` 新增 `graph: "none" | "blocks" | "full"`（默认 `none`）。
2. `blocks`：区块节点（`block:<id>`，带状态、台数、电力摘要）+ 物品节点；边为区块输出（达成目标、溢出）与区块输入（导入、消耗）；另把块间引用（目标引用、消耗引用）作为 `kind: "link"` 的边，注明 `of` 与解析值。
3. 工厂净输入 / 净输出 / 处置作为端点节点；物品节点流量守恒（共享总线假设，与账本一致）。
4. `full`：每个区块再附带其产线图（US-D1），节点 id 以 `block:<id>/` 为前缀，可直接拼成嵌套视图。

#### US-D3 渲染格式预留（P2）

验收标准：
1. `graph_options.format` 字段保留 `"mermaid"`、`"dot"`；本期传入非 `null` 时报"未实现"。
2. 图数据结构不依赖任何渲染器：节点 / 边只含语义字段与布局提示，不含坐标或颜色。
3. 实现时以独立的纯函数把 `graph` 转为文本，不改动求解路径；HTML / Artifact 页面作为调用方的事，不进入本服务。

## 5. 非功能需求

| 编号 | 要求 |
|---|---|
| NFR-1 兼容性 | 不开启 `energy_mode="balance"`、不写新字段时，现有 21 个工具结果逐字段一致；现有测试全部通过；旧方案文件可读 |
| NFR-2 性能 | `balance` 模式下 1000 条候选的 LP < 2 s；含能源链的 10 块方案 < 5 s |
| NFR-3 数值 | 能源平衡误差 < 1e-6 MW；物料平衡要求同上一期 |
| NFR-4 可解释性 | 能源伪物品在所有输出中标注单位 MW；伪配方行注明实体类型；风、光系数在结果中回显 |
| NFR-5 依赖 | 不新增第三方依赖 |
| NFR-6 输出体积 | 图数据默认不输出；1000 条候选、实际选中约 100 条产线时 `graph` 部分 < 200 KB |

## 6. 未决问题

1. **太阳能系数默认值**：原版 Nauvis 昼夜平均约为峰值的 70%，Nullius 是否修改了昼夜长度或 `solar_power` 未核实。**默认 0.7，结果中回显，可由参数覆盖。**
2. **风机系数**：取决于启动设置倍率、当前风速与障碍物，无可靠默认值。**要求调用方给出 `wind_factor`。**
3. **地热反应堆是否需要特定地块**：Nullius 有 `nullius-geothermal-build-*`（`mining-drill`，void 能源）。本期视为无输入的热源，并在结果中注明；是否要建模为资源开采待确认。
4. **太阳能集热器的 `nullius-solar-flux` 来源**：其生产者可能是镜面类机器，而 `mirror` 目前在自动发现的机器排除列表中。本期集热器可显式使用，自动发现不保证找到 flux 来源。
5. **蓄电池**（P2）：是否按"每块太阳能板配 N 个蓄电池"的比例估算，留待太阳能实际使用后再定。
