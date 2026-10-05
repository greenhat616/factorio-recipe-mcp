# Helmod 方案导入 / 导出

- 日期：2026-10-05
- 对照对象：本机 Helmod 2.2.14（`mods/helmod_2.2.14.zip`），已读 `core/Converter.lua`、`dialog/Download.lua`、`data/Model.lua`、`data/ModelBuilder.lua`、`data/ModelCompute.lua`、`math/SolverLinkedMatrix.lua`、`model/RecipePrototype.lua`、`data/RemoteAPI.lua`

## 目标

- `helmod_import`：把 Helmod 的 *Upload Production line* 字符串保存为 plan，求解结果与 Helmod 自己的计算一致。
- `helmod_export`：把 plan 写成 Helmod *Download Production line* 能读入的字符串，Helmod 读入后重新计算，结果与 plan 一致。

## 字符串格式（已用 Factorio 实测）

- `Converter.write(model)` = `helpers.encode_string(serpent.dump(model))`：zlib（头字节 `78 da`）+ base64，没有版本前缀。`Converter.read` 也接受以 `do local` 开头的明文。
- `serpent.dump` 生成 Lua 代码块 `do local _={...};local __={};_.a.b=_.c.d;return _;end`。`model.blocks` 与 `block_root.children` 共享同一批表，第二处写成 `"SERPENT PLACEHOLDER"`，再用回填语句恢复引用。`codec.py` 只解析表字面量和这种回填语句，其他 Lua 一律拒绝，不执行任何代码。
- 导出串里同时带有 Helmod 的计算结果：`count`、`factory.count_deep`、`factory.effects`、`pivot`，以及区块 `products` / `ingredients` 的 `amount` 与 `state`。导入时用它们还原 Helmod 的语义，并作为对照基准。

## 导入映射

| Helmod | plan |
|---|---|
| `model.time` 1 / 60 / 3600 | `per` second / minute / hour |
| 含配方的区块 | 一个 plan 区块，`lines` 固定，`auto_discover=false` |
| `recipe` / `resource` / `energy` 配方 | 配方名 / `mining:<资源>` / `generate:`、`solar:`、`heat:`、`wind:`（`energy_mode=balance`） |
| `factory` 的 name、modules（`amount`）、fuel、neighbour_bonus | `machine`、`modules`、`fuel`、`neighbours` |
| 带插件的信标：`combo` / `per_factory` | `count` / `per_machine` |
| `products[k].input`；`by_product=false` 时为 `ingredients[k].input` | `targets`；`consume` |
| `by_factory` + `factory.input` | `fixed_machines` |
| 链接的子区块（`unlinked≠true`） | 目标引用父区块和排在前面的兄弟区块的 `imports`（输入模式下引用其 `surplus`） |
| 无配方区块上设置的输入 | 交给第一个自身产线产出该物品的链接后代区块 |
| 未设置输入 | 取 Helmod 为各配方 pivot 算出的产量 |
| `energy` / `steam-heat` 产物（焦耳每基准时间） | `energy:electric` / `energy:heat`（MW） |
| 采矿产线的 `factory.effects.productivity` | 扣除插件、信标和机器自带效果后，作为区块的 `mining_productivity` |

不导入（只给警告）：品质、产物约束（`contraints`）、产量份额（`production` < 1）、显示上限（`factory.limit`）、`by_limit`、`consumer`、全局效果、腐坏、温度、`per_factory_constant`、非整数 `combo`（取整）、`boiler` / `fluid` / `technology` / `rocket` / `agricultural` / `spoiling` / `constant` 配方。

## 求解器选择

Helmod 默认的代数求解器不做优化：每个配方只平衡自己的 pivot，其余物品的缺口记为区块输入，多余部分记为区块产物。LP 会优化溢出惩罚（例如把多余的氯气继续加工成四氯化钛），因此结果和 Helmod 不同。

- 代数区块：`solver="matrix"`。只有 pivot 物品保持平衡，Helmod 报告的区块输入作为 `imports`，区块产物作为 `surplus_items`。
- simplex 区块，以及 `matrix` 欠定或出现负值的区块（例如含有 Helmod 中 0 台、未使用的产线）：使用同一组产线的 LP。允许买入 Helmod 列为输入的中间产物，导入成本 ×1e6，让 LP 先运行自身产线。输入模式下禁止中间产物溢出。
- 导入时先在临时目录试解一次，按上面规则决定每个区块用哪个求解器，并给出警告。

## 导出

- 先求解 plan（不保存结果）。自动发现产线的区块导出求解选中的产线。引用按当前解析值固化，并给出警告。
- 每个区块成为一个 `unlinked` 的 Helmod 区块：目标写成 `products[k].input`；只有 consume 时用输入模式（`by_product=false`）；只有 `fixed_machines` 时用 `by_factory`。`lp` 对应 simplex，`matrix` 对应代数求解器。只有一个区块时直接放在 `block_root`。
- `limits`、`objective`、`imports` 等 LP 专有设置无法表达，列入 `warnings`。

## 文件

导出串可能很长，两个工具都支持文件：`helmod_import` 的 `path` 代替 `text`；`helmod_export` 给出 `path` 时把字符串写入该文件，返回的 `text` 为空，已有文件需 `overwrite=true`。相对路径以 `data/helmod/` 为基准，因为服务端的工作目录由 MCP 客户端决定。

## 验证

- `tests/test_helmod_exchange.py`：解析器（Factorio 真实的 serpent 输出、拒绝代码、转义往返）、导出结构、导出→导入往返、链接固化、嵌套区块链接；外加 9 个真实存档模型（已去除玩家名）导入后与 Helmod 机器数逐条比较。
- `scripts/validation/helmod_exchange.py`：对最新存档的副本做两次一 tick benchmark。第一次读出全部 Helmod 模型；第二次给 Helmod 副本追加一个测试接口，执行 Download 对话框的导入代码（`Converter.read` → `ModelBuilder.copyModel` → `ModelCompute.update`）。
- 2026-10-05 在 `Nullius-Next.zip` 上的结果：36 个模型中 3 个为空模型（无配方，报错跳过），33 个导入；127 条产线中，plan_solve 与 Helmod 原计算的机器数全部一致（相对误差 ≤ 1e-6）。导出的 33 个字符串全部被 Helmod 读入并重新计算，127 条产线与 plan 一致。其中采矿产线按产出比较，因为 Helmod 重新计算时使用的是该势力当前的采矿研究（+25%），而这些模型当初是按 +20% / +23% 算的。
