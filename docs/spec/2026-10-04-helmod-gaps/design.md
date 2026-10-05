# 设计：补齐相对 Helmod 的规划缺口

- 实施补充：[implementation.md](implementation.md)（2026-10-05，含温度、对照验收及 HTML 可视化）
- 日期：2026-10-04
- 对应需求：[requirements.md](requirements.md)；实施计划：[tasks.md](tasks.md)
- 基线：commit `b654adf`

## 1. 总体结构

```
src/recipe_mcp/
├── planner/
│   ├── schema.py     【扩展】LineSpec.fuel / neighbours，Defaults.fuel，EnergyMode，PerCopy，Totals 能源字段
│   ├── energy.py     【新】能源伪物品键、单位规则、燃料选择、伪配方（generate/solar/wind/heat）产线构建
│   ├── model.py      【小改】recipe_view / line 识别伪配方；balance 模式下把能耗写进 balance；发现时放开涡轮类别
│   ├── temperature.py【新，P2】按温度拆分流体与温度匹配列
│   ├── graph.py      【新】产线图数据（二部图、端点、depth、按比例分摊边）
│   ├── report.py     【扩展】能源伪物品不随 per 换算；能源汇总；per_copy
│   └── api.py        【扩展】energy_mode、solar_factor、wind_factor、copies
└── plans/
    ├── models.py     【扩展】TargetRef.of / factor；consume 值可为引用；Block.copies 透传；新编辑操作
    ├── ops.py        【扩展】add_supply_block、add_consumer_block
    ├── store.py      【扩展】消耗引用校验
    └── factory.py    【扩展】引用来源解析、消耗引用依赖、账本抵消、suggestions、工厂图数据
```

原则：

- **能源进入现有物料平衡，而不是另起一套。** `balance` 模式下能耗直接写进产线的 `balance`（每次制作的净量），伪配方也是普通产线。于是 LP、矩阵、约束、最大化、消耗、弹性诊断、整数模式、方案与工厂账本都不用改就能处理能源。
- `report` 模式（默认）下任何新代码路径都不触发，保证 NFR-1。

## 2. 能源伪物品与单位

- 键：`energy:electric`、`energy:heat`。`Planner.key()` 接受 `energy:` 前缀；不带前缀的 `electric` / `heat` 不做别名，避免与物品重名。
- 内部单位：每秒 MJ，即 **MW**。与物品"每秒数量"同为每秒速率，所以 LP 与矩阵无需区分。
- 对外单位：恒为 MW，**不乘 `per` 因子**。实现为单一函数 `rate_factor(key, factor) = 1 if key 以 energy: 开头 else factor`，用于：目标与比例、`consume`、`limits.imports`、`imports` 与 `surplus` 输出、物品流表、影子价格、瓶颈边际、弹性诊断、`plan_compare` 的单位换算。
- 能源伪物品默认不可导入：`solve_lp` 的可导入集合中排除 `energy:*`，除非出现在 `imports` 或 `limits.imports`。可溢出（弃电 / 散热），且 `disposal` 跳过 `energy:*`。

## 3. 能源平衡模式（planner/model.py、planner/energy.py）

`Planner` 增加 `energy_mode`。`balance` 时 `Planner.line()` 在算出效果系数后追加（每次制作，即除以 cpm）：

| 机器能源类型 | 追加到 balance |
|---|---|
| `electric` | `energy:electric` −= (active_W + drain_W) / 1e6 / cpm |
| 任意类型的信标耗电 | `energy:electric` −= beacon_W / 1e6 / cpm |
| `heat` | `energy:heat` −= active_W / 1e6 / cpm |
| `burner` | 燃料 −= active_W / (fuel_value × effectivity) / cpm；`burnt_result` += 同量 |
| `fluid` 且 `burns_fluid` | 燃料流体 −= active_W / (fuel_value × effectivity) / cpm |
| `fluid` 且非燃烧（按温度取热） | 过滤流体 −= `fluid_usage_per_tick` × 60 / cpm（满负荷近似，结果注明） |
| `void` | 无 |

- `active_W` 已含插件能耗系数，与现有 `power_W` 口径一致，所以 `balance` 下 Σ 电力消耗 = `totals.power_MW`。
- 燃料选择（`energy.pick_fuel`）：`spec.fuel` > `defaults.fuel` 中第一个可用者 > 报错。可用 = 类别匹配（`fuel_categories` 或旧字段 `fuel_category`；流体为 `fluid_box.filter` 或有 `fuel_value` 的流体）、有正热值、`validate_stage` 时可制造。`report` 模式下若给了 `fuel`，只用于结果中每台燃料速率（`PlanLine.fuel_per_machine`），不进平衡。
- 自动发现：`balance` 时 `AUTO_EXCLUDED_CATEGORIES` 去掉 `turbine-open`、`turbine-closed`（`nullius-power-sink` 仍排除，它销毁能量）。现有前沿扩展逻辑会把负平衡的 `energy:*` 加入前沿；生产者查找对 `energy:*` 走 §4 的伪配方枚举。写进 `imports` 的能源键不扩展（现有逻辑）。

## 4. 伪配方（planner/energy.py）

与 `mining:<resource>` 一样由 `Planner.recipe_view()` / `line()` 识别前缀，但构建走独立函数 `energy_line(planner, spec, defaults) -> Line`，跳过插件逻辑（`modules` / `beacons` 非空即报错）。约定每台每秒"制作 1 次"（`crafts_per_machine = 1`），所以 balance 即每台每秒的流量。

| 伪配方 | 来源原型 | 每台 balance | 说明 |
|---|---|---|---|
| `generate:<g>` | `generator`，`burns_fluid=true` | `energy:electric` +P；燃料流体 −P/(fuel_value × effectivity)，P = `max_power_output` | Nullius 涡轮发电机：1 MW、效率 0.9、10 kJ → 111.1 单位/秒 |
| `generate:<g>` | `generator`，非燃烧 | P = `fluid_usage_per_tick` × 60 × (min(流体温度, `maximum_temperature`) − 默认温度) × 热容 × 效率；流体 −`fluid_usage_per_tick` × 60 | 流体温度取产物温度或默认温度；P2 温度建模后取实际温度 |
| `generate:<g>` | `burner-generator` | `energy:electric` +`max_power_output`；燃料按 §3 burner 规则 | |
| `solar:<p>` | `solar-panel` | `energy:electric` +`production` × `solar_factor` | 无输入 |
| `wind:<e>` | `electric-energy-interface` 且 `energy_production` > 0、非 creative / hidden | `energy:electric` +`energy_production` × `wind_factor` | `wind_factor` 缺省 → 报错 |
| `heat:<r>` | `reactor` | `energy:heat` +`consumption` × 效率 × (1 + `neighbour_bonus` × `neighbours`)；燃料按能源类型 | void（地热）无输入；fluid（集热器）消耗过滤流体 |

- `Line` 字段：`machine` = 实体名，`machine_type` = 原型类型，`energy_type` = `producer`，`active_W = drain_W = 0`，`power_W` = 0（不计入耗电），`pollution_per_minute` 取 `energy_source.emissions_per_minute.pollution`。
- 阶段校验：`buildable(entity=...)`；不可建造时与普通产线一样拒绝。
- 枚举生产者：`energy:electric` → 所有可建造的 `generator`、`burner-generator`、`solar-panel`，以及给了 `wind_factor` 时的风机；`energy:heat` → 所有可建造的 `reactor`。排除名字含 `creative`、`hidden`、`mirror`（沿用 `AUTO_EXCLUDED_MACHINES`）。
- `solar_factor`（默认 0.7）、`wind_factor`（无默认）是 `plan()` 参数，回显在结果 `energy_factors` 中。

## 5. 结果与汇总（planner/report.py、planner/schema.py）

- 物品流、`imports`、`surplus`、影子价格中 `energy:*` 不乘 `per` 因子（§2）。`ItemFlow` 新增 `unit`（`"MW"` 或 `"per <per>"`）。
- `Totals` 新增：`electric_generation_MW`、`heat_generation_MW`、`heat_consumption_MW`、`net_electric_MW`（发电 − `power_MW`，仅 `balance`）。`report` 模式下这些为 0，`heat_consumption_MW` 仍按热能机器计算（替代原先混在 `non_electric_fuel_MW` 里的那部分；`non_electric_fuel_MW` 保持原值不变，只增不改）。
- `PlanLine` 新增 `fuel`、`fuel_per_machine`（每台每 `per` 的燃料量）。

## 6. 块间联动（plans/）

### 6.1 引用

```python
class TargetRef(Spec):
    from_: list[str] | Literal['*'] = Field(alias='from')
    of: Literal['imports', 'surplus', 'net_outputs'] = 'imports'
    factor: float = Field(1.0, ge=0)
    plus: float = 0.0
```

- 值 = `factor` × Σ_块 来源值 + `plus`；来源值取被引用块 `PlanResult` 的 `imports[k]`、`surplus[k]`，或 `net_outputs[k]` = max(0, achieved_targets + surplus − imports − consume)。
- `BlockRequest.consume: dict[str, float | ConsumeRef]`，`ConsumeRef` 与 `TargetRef` 同结构但 `of` 默认 `surplus`。
- `ops.references()` 与 `factory.dependencies()` 同时扫描 `targets` 与 `consume`。`rename_block` 同步改写两处；`remove_block` 检查两处。
- 解析：`factory.resolve_amounts()` 统一处理目标与消耗；消耗解析值 ≤ 1e-9 → 移除该项并警告。直接调用 `solve_production` 时仍不允许引用（只在方案内有效）。

### 6.2 账本

现有公式不变：`out_k` 含溢出、`in_k` 含消耗，所以消耗引用吃掉的溢出自然进入 `internal_k`，处置量 `dispose_k = max(0, Σ surplus − in)` 自动减少。能源键按 MW 直接相加（共享电网 / 热网），写入 `assumptions`。

### 6.3 补区块操作与建议

| op | 参数 | 行为 |
|---|---|---|
| `add_supply_block` | `for_block`, `item`, `new_id`, `request?` | 新块 `targets: {item: {from: [for_block]}}`；继承 `for_block` 的 `defaults`、`validate_stage`、`objective`、`energy_mode`；`request` 浅合并覆盖 |
| `add_consumer_block` | `for_block`, `item`, `new_id`, `request` | 新块 `consume: {item: {from: [for_block], of: "surplus"}}`；`request` 必须给 `lines` 或 `targets`（否则无从确定用什么配方消耗） |

`for_block` 的上次结果中没有该物品的导入 / 溢出时照样创建并给警告（结果可能尚未求解）。

`FactoryLedger.suggestions: list[dict]`：对每个 `net_inputs` 项给出可直接放进 `plan_edit` 的 `add_supply_block` 操作（`new_id` 取 `supply-<物品名>`，冲突时加序号）；对每个工厂处置项给出 `add_consumer_block` 模板（`request.lines` 留空，提示调用方填写）。能源键不给建议（能源用 `energy_mode` 处理）。

### 6.4 为什么不做区块嵌套

Helmod 的子区块树表达的是"子块满足父块的需求"。平铺区块 + `{from: [父块]}` 引用 + 拓扑求解表达的是同一个依赖图，而且允许一个供应块同时服务多个消费块（树做不到）。嵌套只影响展示，留给调用方按依赖图分组。

## 7. 区块份数

- `plan()` 新增 `copies: int = 1`（`BlockRequest` 随之可用）。目标、消耗、约束、`fixed_machines` 都按全块总量解释。
- 连续求解：照常求解总量，事后生成 `per_copy`：每条产线 `machines / copies` 与其向上取整，每份信标取整、插件清单（取整口径按每份计），以及 `copies_totals`（每份取整 × 份数）。
- 整数模式：目标、消耗、`limits` 中的可加量（导入上限、电力、机器、信标、插件、污染）与 `fixed_machines` / `max_machines` 都除以份数，求 1 份的整数解，再把产线速率与台数乘回份数；`per_copy` 即这份整数解。
- `copies=1` 时不产生 `per_copy` 字段（保持 null）。

## 8. 流体温度（P2，planner/temperature.py）

- 触发：候选产线中任一原料带 `temperature` / `minimum_temperature` / `maximum_temperature`，或任一产物带 `temperature`。否则跳过（Nullius 当前恒跳过）。
- 产物键改为 `fluid:<名>@<T>`（T 为产物温度或流体默认温度）；带范围要求的原料键改为 `fluid:<名>[lo,hi]`；无要求的原料保留 `fluid:<名>`，视为接受任何温度。
- 每个"产物温度 → 可接受它的原料键"组合加一个零成本、非负的匹配列（在原料键行 +1，在产物键行 −1）。导入只建在原料键上（外部按需供应符合要求的温度）。
- 报告时把同名流体的各温度键合并显示，另给 `temperatures` 明细。

## 9. 可视化数据（planner/graph.py、plans/factory.py）

### 9.1 数据模型

```python
class GraphNode(BaseModel):
    id: str            # 'line:<id>' | 'item:<key>' | 'import:<key>' | 'supply:<key>' | 'target:<key>'
                       # | 'surplus:<key>' | 'dispose:<key>' | 'block:<id>' | 'factory-in:<key>' | 'factory-out:<key>'
    kind: Literal['line', 'disposal', 'item', 'import', 'supply', 'target', 'surplus', 'dispose',
                  'block', 'factory_input', 'factory_output']
    label: str         # 原型名；本地化名留给调用方
    depth: int | None  # 布局提示，见 9.2
    data: dict[str, Any] = {}  # 摘要：产线 {recipe, machine, machines, machines_ceil, modules, beacon_count, power_MW}；
                               # 物品 {key, unit}；区块 {status, machines, power_MW}

class GraphEdge(BaseModel):
    source: str
    target: str
    item: str
    rate: float
    unit: str          # 'MW' 或 'per <per>'
    kind: Literal['flow', 'allocated', 'link'] = 'flow'
    data: dict[str, Any] = {}  # link 边：{of, factor, plus, resolved}

class ProductionGraph(BaseModel):
    schema_version: Literal[1] = 1
    nodes: list[GraphNode]
    edges: list[GraphEdge]
    notes: list[str] = []   # 如"allocated 边按生产者占比分摊，不代表实际连接"
```

### 9.2 产线图构建（`graph.build_line_graph(result, lines)`）

- 只用已求出的 `PlanResult` 与 `Line`，不重新求解，所以对 LP、矩阵、整数、弹性诊断结果都适用。
- 边：产线 l 对物品 k 净产出 v > 0 → `line:l → item:k`（rate = v × 速率）；净消耗 → `item:k → line:l`。导入 / 消耗供给 / 目标 / 溢出 / 处置各接一个端点。由于来自同一组平衡方程，物品节点天然守恒；构建后断言守恒误差 < 1e-6，否则在 `notes` 中报告。
- `depth`：在反向图上从所有 `target:*` 端点做 BFS 的最短跳数；不可达的节点（只为溢出存在的支路）从 `surplus:*` / `dispose:*` 端点再做一次，取较小值；仍不可达为 `null`。环不影响 BFS。
- 分摊边（`allocate=true`）：物品 k 的总产出 P_k，生产者 p 的份额 v_p / P_k；对每个消费者 c 的需求 u_c，生成 `line:p → line:c`，rate = u_c × v_p / P_k；导入同样参与分摊。节点数 n 时边数上界为 Σ_k 生产者数 × 消费者数，只在请求时计算。
- 能源伪物品作为普通物品节点出现（`unit: "MW"`）。

### 9.3 工厂图构建（`factory.build_factory_graph(...)`）

- 区块节点 + 物品节点；区块的 `achieved_targets`、`surplus` → `block → item`；`imports`、`consume` → `item → block`。
- 引用边：每个解析过的目标引用 / 消耗引用生成 `kind: "link"` 的 `block:<被引用块> → block:<引用块>`，`data` 记录 `of`、`factor`、`plus`、解析值。
- 工厂端点：`net_inputs` → `factory-in:<键> → item:<键>`；`net_outputs` → `item:<键> → factory-out:<键>`；工厂处置 → `dispose:<键>`。
- `full`：每个区块的产线图节点 id 加前缀 `block:<id>/`，并以 `kind: "block"` 节点的 `data.children` 列出子节点 id，调用方可据此折叠 / 展开（这就是 Helmod 区块树的展示形态，而数据仍是平铺的）。
- 区块解析失败时只出现区块节点（`status: "error"`），无边。

### 9.4 渲染预留

- `graph_options: {allocate: bool = False, format: Literal['mermaid', 'dot'] | None = None}`；`format` 非空 → `ValueError("graph_options.format is reserved and not implemented yet")`。
- 实现时：`graph.render(graph, format) -> str` 纯函数；不进入求解路径，不新增依赖。

## 10. 接口变化

`solve_production` 新增参数（均有默认值）：

| 参数 | 默认 | 说明 |
|---|---|---|
| `energy_mode` | `"report"` | `"balance"` 时电、热、燃料进入平衡 |
| `solar_factor` | `0.7` | 太阳能平均出力系数 |
| `wind_factor` | `null` | 风机出力系数；使用 `wind:*` 时必填 |
| `copies` | `1` | 区块份数 |
| `graph` | `"none"` | `"bipartite"` 时附带产线图 |
| `graph_options` | `{}` | `{allocate, format}`；`format` 本期保留 |

`LineSpec` 新增 `fuel`、`neighbours`；`Defaults` 新增 `fuel`（优先级列表）。`recipe` 可写 `generate:` / `solar:` / `wind:` / `heat:` 前缀。

新增返回字段：`energy_mode`、`energy_factors`、`per_copy`、`copies_totals`、`ItemFlow.unit`、`PlanLine.fuel` / `fuel_per_machine`、`Totals` 能源字段、`FactoryLedger.suggestions`、`graph`（产线图，`solve_production`）、`graph`（工厂图，`plan_solve` 的 `graph: "blocks" | "full"`）。

方案编辑新增 `add_supply_block`、`add_consumer_block`；引用新增 `of`、`factor`；`consume` 可写引用。工具总数不变（21）。

## 11. 测试策略

| 层 | 文件 | 内容 |
|---|---|---|
| 能源合成 | `tests/test_energy.py`（新） | 发电机 9 MW → 9 台、1000 单位/秒；反应堆燃料 0.0125/秒与乏燃料同量；热能机器在 balance 下的热平衡；`report` 模式结果与现状一致；能源键不随 `per` 换算；能源不可导入除非列入 `imports`；风机缺 `wind_factor` 报错；燃料类别不符报错 |
| 份数合成 | `tests/test_planner_synthetic.py` | 1.2 台 × 4 份的取整；整数模式按份求解 |
| 方案 | `tests/test_plans.py` | `of` / `factor` 解析；消耗引用吃掉溢出后处置为 0 / 一半；环检测含消耗引用；补区块操作与 suggestions；旧格式引用可读 |
| 真实数据 | `tests/test_planner_real.py` | 10 甲醇/秒 balance 下热平衡闭合并出现热源；`energy:electric` 10 MW 自动发现出涡轮链 |
| 可视化 | `tests/test_graph.py`（新） | 物品节点守恒；端点齐全；depth 闭式值；分摊边按占比；id 稳定；矩阵 / 整数 / 不可行结果都能出图；工厂图的 link 边与解析值；`full` 前缀；`format` 报"未实现"；默认不输出 |
| 温度（P2） | `tests/test_temperature.py`（新） | 两种温度蒸汽的匹配；无温度时结果不变 |

## 12. 风险与应对

| 风险 | 应对 |
|---|---|
| balance 模式下自动发现因能源前沿扩展过多（每条链都追到发电） | 能源键扩展深度计入 `max_depth`；写进 `imports` 即可截断；性能检查覆盖（NFR-2） |
| 电 → 涡轮配方（本身耗电）形成循环 | LP 天然处理；矩阵模式下可能欠定，报错信息提示 pin 路线 |
| 风、光系数被当成精确值 | 结果回显系数；风机无默认值 |
| 反应堆相邻加成与实际布局不符 | `neighbours` 显式输入，默认 0（保守） |
| `per` 换算对能源键漏改导致单位混乱 | 统一走 `rate_factor()`；测试对每个输出字段断言 MW 不随 `per` 变化 |
| 图数据体积过大（分摊边平方增长） | 默认不输出；分摊边单独开关；NFR-6 体积检查 |
| 温度拆分改变物品键，影响方案引用与账本 | P2 单独实施；只在有温度约束时启用；报告合并显示 |
