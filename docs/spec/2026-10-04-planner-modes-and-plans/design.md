# 设计：量化计算模式扩展与生产方案管理

- 日期：2026-10-04
- 对应需求：[requirements.md](requirements.md)；实施计划：[tasks.md](tasks.md)
- 基线：commit `5007663` + 项目结构重构（uv src 布局）

## 1. 总体结构

```
src/recipe_mcp/
├── server.py            MCP 工具层（参数 → 调用，不含业务逻辑）
├── paths.py             新增 PLANS_DIR = DATA_DIR / 'plans'
├── planner/
│   ├── model.py         产线模型 Planner.line()、自动发现（不变）
│   ├── lp.py            【扩展】LPBuilder、目标/最大化两阶段、约束、对偶值→瓶颈、弹性诊断、MILP
│   ├── matrix.py        【扩展】consume 支持
│   ├── disposal.py      【新】副产物处置核算
│   ├── report.py        【扩展】limits_usage / bottlenecks / disposal 字段
│   └── api.py           【扩展】plan() 新参数与模式分派
└── plans/               【新】
    ├── store.py         PlanStore：存储、校验、原子写、修订号、指纹、回收站
    ├── ops.py           编辑操作与 pin
    └── factory.py       工厂求解、联动、账本、对比
```

原则：

- `Planner.line()` 的产线模型不变；新功能都在"由产线构建优化问题"和"由结果做后处理"两层实现。
- `plan()` 保持现有签名与默认值，新参数全部有默认值，默认行为等同于现状（NFR-1）。
- `plans/` 只通过 `plan()` 求解，不直接接触 LP，保证单块求解与方案内求解结果一致。

## 2. 单位与内部表示

- 内部一律为"每秒"。用户输入的 `targets`、`consume`、`limits.imports` 按 `per` 除以因子换算；输出乘回因子。
- `limits.power_MW`（MW）、`limits.machines` / `machines_by_type`（台）、`limits.pollution_per_minute`（每分钟）与 `per` 无关。
- 最大化模式中 `targets` 的值是比例 r_k（无量纲，仍按 `per` 解读）；结果中的 `scale` 无量纲，实际产出 = `scale × r_k`（按 `per`）。
- 物品键沿用 `type:name`。

## 3. LP 统一形式（planner/lp.py）

### 3.1 变量

| 变量 | 含义 | 下界 | 上界 |
|---|---|---|---|
| x_l | 产线 l 的每秒制作次数 | `fixed`/0 | `fixed`/`max_machines×cpm_l`/∞ |
| m_k | 物品 k 的导入 | 0 | `limits.imports[k]` 或 ∞ |
| u_k | 物品 k 的溢出 | 0 | ∞（不允许溢出时不建变量） |
| s | 缩放倍数（仅最大化） | 0 | ∞ |
| n_l | 产线 l 的整数台数（仅整数模式） | 0 | ∞ |

`cpm_l` = 每台每秒制作次数（`crafts_per_machine`）。机器数 = x_l / cpm_l。

### 3.2 物品平衡（等式，每个物品一行）

设 a_kl 为产线 l 每次制作对物品 k 的净产量（已含产能），t_k 为目标，κ_k 为消耗量：

- 目标模式：  Σ_l a_kl x_l + m_k − u_k = t_k − κ_k
- 最大化模式：Σ_l a_kl x_l + m_k − u_k − s·r_k = −κ_k

规则：

- κ_k > 0 的物品（`consume`）不建 m_k、u_k，保证恰好消耗 κ_k。
- 可导入集合 = 无生产产线的物品 ∪ `imports` ∪ `limits.imports` 的键 − `forbid_imports` − 目标物品 − 消耗物品。
- 溢出集合：`allow_surplus=true` 时为全部非消耗物品；否则为 `surplus_items` ∪ 目标物品。
- `consume` 与 `targets` 键重叠 → `ValueError`。

### 3.3 约束（不等式行）

| 约束 | 行 |
|---|---|
| `power_MW` | Σ_l x_l · P_l / cpm_l ≤ P，P_l = 电力机器 (active+drain) + 信标功率，单位 MW（与 `totals.power_MW` 同口径） |
| `machines` | Σ_l x_l / cpm_l ≤ M |
| `machines_by_type[e]` | Σ_{l: machine_l=e} x_l / cpm_l ≤ M_e |
| `pollution_per_minute` | Σ_l x_l · pol_l / cpm_l ≤ Q |
| `imports[k]` | 作为 m_k 的变量上界（不单独建行） |

整数模式下，上面三类机器/电力/污染行中的 x_l / cpm_l 换成 n_l，并增加耦合行 x_l − cpm_l · n_l ≤ 0。

### 3.4 目标函数与两阶段

- 目标模式：一次求解，min c·v（c 同现有 `balanced/machines/power/imports` 权重）。
- 最大化模式：
  1. 阶段 1：min −s，得到 s*。s* 无界（HiGHS status 3）→ 报错"需要导入上限、电力或机器约束"。r 为空或含 ≤ 0 → 报错。
  2. 阶段 2：加约束 s ≥ s*·(1 − 1e-9)，min c·v，得到最终解。
  3. 瓶颈取自阶段 1 的对偶值（反映 s 对约束的敏感度）。

### 3.5 对偶值 → 瓶颈与边际

scipy/HiGHS 的 `marginals` 是目标函数对右端项的偏导（已在本机验证 `res.ineqlin.marginals`、`res.eqlin.marginals`、`res.upper.marginals` 均可用）。

- 阶段 1 目标为 −s，因此对 ≤ 约束：ds/db = −marginal（≥ 0）。导入上限用 `res.upper.marginals` 对应 m_k 的项，同样取负。
- 换算到 `per`：导入上限的边际 = (ds/db_内部) / 因子；电力、机器、污染不换算。
- 输出：

```json
"limits_usage": [{"constraint": "power_MW", "limit": 50, "used": 50.0, "utilization": 1.0}],
"bottlenecks": [{"constraint": "import:item:nullius-limestone", "limit": 30, "used": 30,
                 "marginal": 0.0347, "meaning": "scale increase per +1 unit/minute of limit"}]
```

- 紧约束判定：`|limit − used| ≤ 1e-6·max(1,|limit|)` 且 `|marginal| > 1e-9`。
- 目标模式下的边际直接为 marginal（目标函数单位，含义"放宽 1 单位能省多少成本"）。
- 整数模式：`bottlenecks` 不提供，`limits_usage` 照常，字段 `bottlenecks_note: "not available for MILP"`。

### 3.6 弹性不可行诊断

触发条件：LP 不可行（HiGHS status 2），且存在 `limits` 或 `consume`。否则保持现有报错。

在原问题上加非负松弛变量，每个松弛的成本为相对值（除以对应量的绝对值，下限 1e-9），全部权重为 1：

| 原约束 | 弹性形式 |
|---|---|
| 约束行 g(v) ≤ L | g(v) ≤ L + σ |
| 导入上界 m_k ≤ U_k | 改为行 m_k ≤ U_k + σ_k |
| 目标 t_k | 平衡行右端 t_k 改为 t_k − δ_k（0 ≤ δ_k ≤ t_k） |
| 消耗 κ_k | 平衡行加 −γ_k，即实际消耗 κ_k − γ_k（0 ≤ γ_k ≤ κ_k） |

min Σσ/|L| + Σδ/t + Σγ/κ。产线的 `fixed_machines` 保持硬约束（它们是用户的明确声明）；若弹性 LP 仍不可行，报错指向 `fixed_machines`。

返回（不抛异常，`status: "infeasible"`）：

```json
{"status": "infeasible",
 "infeasibility": [
   {"kind": "limit", "constraint": "power_MW", "limit": 30, "required": 41.2, "shortfall": 11.2, "relative": 0.373},
   {"kind": "target", "item": "fluid:nullius-methanol", "requested": 100, "achievable": 72.8}],
 "suggestions": ["Raise power_MW to >= 41.2", "Or lower nullius-methanol to <= 72.8 (with mode=maximize to explore)"]}
```

注意：弹性 LP 同时放松多项时，给出的是"相对总违反量最小"的一组组合，不是每项单独的最大可达值；文档字段 `note` 说明这一点。

### 3.7 整数模式（P2）

- `scipy.optimize.milp`，`integrality` 只对 n_l 置 1；`options={'time_limit': time_limit, 'mip_rel_gap': 1e-4}`。
- 成本中机器项改为 Σ w_machines · n_l，电力项仍按 x_l（平均功率）。
- 产线 > 400 → 报错（需求 US-A8.3）。
- 状态映射：最优 → `optimal`；超时有解 → `time_limit` + `mip_gap`；超时无解 → 报错。
- 最大化模式同样两阶段（两次 MILP）。

### 3.8 矩阵求解器扩展（planner/matrix.py、planner/api.py）

- 支持 `consume`：消耗物品角色为 `consumed_input`，不建导入/溢出未知量，右端为 −κ_k。
- `targets` 可为空，前提是 `consume` 非空或存在 `fixed_machines` 产线；否则报错"没有确定规模的量"。
- `mode="maximize"`、`limits`、`integer_machines` 与矩阵求解器同时出现 → 报错，提示改用 `lp`。

## 4. 副产物处置核算（planner/disposal.py）

- `disposal.py` 在首次使用时由 `Planner` 的配方表建索引 `void_by_key`：配方满足"恰好一个投入 k，且所有产出的期望量为 0（或无产出）"、非虚拟。Nullius 的销毁配方是 `nullius-gas-void` / `nullius-liquid-void` 两类，共 42 个，产出为 probability 0 的占位物品，可被此规则识别。
- 对每个溢出量 > 1e-9 的物品：候选销毁配方须当前阶段可用（`validate_stage` 时）；机器按 `disposal_defaults.machine_preference`（默认 **`efficient`**）选择。例如 Nullius 中 `efficient` 会选零耗电的烟囱 2（速度 5），而不选需 295 kW 的烟囱 3；`fastest` 会选烟囱 3。
- 台数 = 溢出量 / (每次投入量 × cpm)。有多个可用销毁配方时选台数最少的那个。
- 输出 `disposal`、`disposal_totals`、`totals_with_disposal`、`disposal_unhandled`。完全是后处理，不进入 LP。

## 5. `solve_production` 接口变化

新增参数（均有默认值）：

| 参数 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `mode` | `"targets" \| "maximize"` | `"targets"` | 最大化时 `targets` 为比例 |
| `limits` | dict | `{}` | `{imports:{物品:速率}, power_MW, machines, machines_by_type:{机器:台}, pollution_per_minute}` |
| `consume` | dict[str,float] | `{}` | 必须完全消耗的外部输入 |
| `disposal` | `"report" \| "none"` | `"report"` | 副产物处置核算 |
| `disposal_defaults` | dict | `{}` | 处置设备选择，同 `defaults` 格式 |
| `integer_machines` | bool | `false` | P2 |
| `time_limit` | float | `10` | 整数模式时限（秒） |

新增返回字段（只增不改）：`status`、`mode`、`scale`、`achieved_targets`、`limits_usage`、`bottlenecks`、`infeasibility`、`suggestions`、`disposal*`、`mip_gap`。`limits` 中未知键报错，避免拼写错误被静默忽略。

## 6. 方案管理（plans/）

### 6.1 存储

- 根目录 `paths.PLANS_DIR`（即 `data/plans/`；可注入，测试用 `tmp_path`）；回收站 `data/plans/.trash/`。
- 名称正则 `^[A-Za-z0-9_.-]{1,64}$`，且不等于 `.`、`..`；路径解析后必须位于根目录内（`Path.resolve()` 前缀检查）。
- 写盘：序列化 → 检查 ≤ 1 MB → 写 `<name>.json.tmp-<pid>` → `os.replace`。
- 读盘：JSON 解析失败 → 报错并指出文件，不改动原文件。

### 6.2 文件格式（schema 1）

```json
{
  "schema": 1,
  "name": "methanol-10ps",
  "description": "10 甲醇/秒模块",
  "revision": 3,
  "created_at": "2026-10-04T05:00:00+00:00",
  "updated_at": "2026-10-04T05:10:00+00:00",
  "force": "faction-a632079",
  "per": "minute",
  "fingerprint": {"prototype_raw_sha256": "c17d…", "progress_tick": 68760001, "source_copy_sha256": "3854…"},
  "blocks": [
    {
      "id": "methanol",
      "description": "",
      "enabled": true,
      "request": {"targets": {"nullius-methanol": 600}, "lines": [], "solver": "lp", "objective": "balanced"},
      "result": {
        "solved_at": "…", "fingerprint": {"…": "…"}, "status": "optimal", "solver": "lp",
        "targets": {}, "resolved_targets": {}, "totals": {}, "imports": {}, "surplus": {}, "consume": {},
        "disposal_totals": {}, "lines": [{"id": "", "recipe": "", "machine": "", "modules": [], "machines": 0, "machines_ceil": 0}],
        "lines_for_matrix": [], "matrix_args": {}, "warnings": []
      }
    }
  ]
}
```

- `request` 的键白名单 = `solve_production` 的参数 − {`force`, `per`}（这两项取方案级设置）。未知键报错。
- 目标值允许数字或引用对象（见 6.5）；引用只在方案内有效，直接调用 `solve_production` 时不允许。
- `result` 只保存摘要（够用于汇总、对比、固定路线），完整结果每次求解时重新生成，避免文件膨胀。

### 6.3 过期检测

`fingerprint` 取自当前 `Database`：`raw_sha256`、`progress['tick']`、`provenance.source_copy_sha256`。

| 比较结果 | 标记 |
|---|---|
| 全部一致 | `fresh` |
| 原型哈希不同 | `prototypes_changed` |
| 原型相同，tick 或存档哈希不同 | `stage_changed` |

方案级与块结果级分别比较。过期只产生警告，不阻止求解；重新求解并保存后刷新指纹。

### 6.4 编辑操作（`plan_edit`）

在内存副本上依次执行，全部成功并通过校验（每个启用块执行一次 `build_lines` 级的参数与产线构建检查，不求解）后才写盘。

| op | 参数 | 行为 |
|---|---|---|
| `add_block` | `block` | id 唯一，缺省 `enabled=true` |
| `remove_block` | `block_id` | 若被其他块引用则报错 |
| `rename_block` | `block_id`, `new_id` | 同步改写其他块中的引用 |
| `set_enabled` | `block_id`, `enabled` | |
| `update_request` | `block_id`, `fields` | 浅合并；值为 `null` 表示删除该键 |
| `add_line` | `block_id`, `line` | 产线 id 缺省为配方名，冲突报错 |
| `update_line` | `block_id`, `line_id`, `fields` | 浅合并，`null` 删除键 |
| `remove_line` | `block_id`, `line_id` | |
| `set_target` / `remove_target` | `block_id`, `item`, `value` | value 可为数字或引用 |
| `set_limit` / `remove_limit` | `block_id`, `key`, `value` | key 形如 `power_MW`、`imports.<item>`、`machines_by_type.<machine>` |
| `set_consume` / `remove_consume` | `block_id`, `item`, `value` | |
| `pin` | `block_id`, `solver?` | 用 `result.lines_for_matrix` 替换 `lines`，`matrix_args` 合并进请求，`auto_discover=false`；可选切换 `solver` |
| `set_meta` | `description?`, `force?`, `per?` | 修改 `per` 不自动换算已存数值，返回警告 |

`expected_revision` 不符 → 冲突错误，返回当前修订号。

### 6.5 工厂求解与块间联动（`plan_solve`）

1. 选出要求解的块（参数 `blocks` 为空 → 全部启用块）。被引用但未选中的块也会加入（依赖闭包）。
2. 依赖图：目标值为 `{"from": [ids]}` → 依赖这些块；`{"from": "*"}` → 依赖其他全部启用块。Kahn 拓扑排序；有环 → 报错，并用 DFS 给出环路径。
3. 按拓扑序求解：解析引用 = Σ 被引用块结果 `imports[item]`（`per` 单位）+ `plus`；被引用块未导入该物品 → 取 0 并加入警告。解析后的值写入 `resolved_targets`。
4. 每块调用 `plan(db, …, force=方案force, per=方案per, **request)`。某块报错不影响其他块，错误记录在该块结果中，工厂汇总注明不完整。
5. 工厂账本（每个物品 k，`per` 单位）：

```
out_k      = Σ_b (achieved_target_b,k + surplus_b,k)
in_k       = Σ_b (import_b,k + consume_b,k)
internal_k = min(out_k, in_k)
net_in_k   = max(0, in_k − out_k)
net_out_k  = max(0, out_k − in_k)
dispose_k  = max(0, Σ_b surplus_b,k − in_k)   # 内部需求优先消化副产物
```

6. 工厂处置：对 dispose_k > 0 的物品重新运行处置核算，替代各块独立处置结果的简单相加。
7. 汇总机器数、电力、污染（各块 `totals` 相加）+ 工厂处置设备。
8. 假设声明（写入返回的 `assumptions` 字段）：共享总线、不建模物流与缓冲、按块独立选路线。
9. `save_results=true` 时写回各块 `result` 和方案指纹（修订号 +1）。

返回：

```json
{"plan": "plastics", "revision": 8, "stale": "fresh",
 "blocks": [{"id": "gas", "status": "optimal", "resolved_targets": {}, "totals": {}, "imports": {}, "surplus": {}, "warnings": []}],
 "factory": {"net_inputs": {}, "net_outputs": {}, "internal_transfers": {}, "disposal": [], "totals": {}, "assumptions": []},
 "order": ["gas", "pcabs", "pex"], "warnings": []}
```

`detail="full"` 时每块附完整 `plan()` 结果。

### 6.6 MCP 工具

| 工具 | 签名（摘要） | 优先级 |
|---|---|---|
| `plan_list` | `()` | P0 |
| `plan_get` | `(name, include_results=True)` | P0 |
| `plan_save` | `(name, blocks, description='', force='', per='second', overwrite=False)` | P0 |
| `plan_edit` | `(name, ops, expected_revision=None)` | P0 |
| `plan_solve` | `(name, blocks=[], detail='summary', save_results=True)` | P0 |
| `plan_delete` | `(name, confirm)` | P1 |
| `plan_compare` | `(a, b)`，`a`/`b` 为 `"plan"` 或 `"plan/block"` | P2 |

工具总数 14 → 20（P2 完成后 21），`tests/test_mcp_stdio.py` 中的工具数断言同步修改。

## 7. 错误处理约定

- 参数错误、阶段锁定、矩阵欠定或矛盾：继续抛 `ValueError`（MCP 返回 isError + 文本）。
- 有约束或消耗时的不可行：返回结构化结果 `status:"infeasible"`，便于调用方读取缺口。
- 方案冲突：`ValueError("Revision conflict: expected 3, current 4")`。
- 所有错误信息给出下一步建议（NFR-5）。

## 8. 测试策略

| 层 | 文件 | 内容 |
|---|---|---|
| 合成数学 | `tests/test_planner_synthetic.py`（扩展） | 用现有类重油裂解夹具做闭式校验：原油上限 100/s → 石油气 97.5/s；电力上限；按机器类型的上限；矩阵模式消耗 100 原油 → 97.5；不可行诊断的缺口数值；处置核算（合成销毁配方）；整数模式小例子 |
| 方案 | `tests/test_plans.py`（新） | 临时根目录：名称校验/路径穿越、原子编辑回滚、修订冲突、pin 前后一致、联动拓扑与环检测、账本数值、过期标记、回收站 |
| 真实数据 | `tests/test_planner_real.py` / `tests/test_plans.py`（`realdata` 标记） | 甲醇：石灰石上限下最大化 + 瓶颈；消耗压缩氧气；两块方案（甲醇块 + 引用甲醇的下游块）汇总 |
| stdio | `tests/test_mcp_stdio.py` | 工具数 20；每个新工具至少调用一次；错误路径（冲突、非法名称）返回 isError |

## 9. 风险与应对

| 风险 | 应对 |
|---|---|
| 两阶段的 s* 容差导致阶段 2 不可行 | 使用 s ≥ s*(1 − 1e-9)；仍失败则退回阶段 1 的解并给出警告 |
| HiGHS 对偶值符号约定误读 | 用合成闭式例子断言边际值的符号与大小（如原油边际 = 0.975 石油气/原油） |
| 弹性诊断给出组合解而非单项上限 | 字段说明 + 建议改用 `mode=maximize` 探索单项 |
| 方案文件被外部工具改坏 | 解析失败不覆盖；schema 版本校验；未知键报错 |
| 引用 `*` 容易形成环 | 环检测给出路径；文档建议优先显式列出块 id |
| 单个模块继续膨胀 | 按组件落在 `planner/` 与 `plans/` 子模块，新增功能不往 `model.py` 里堆 |
