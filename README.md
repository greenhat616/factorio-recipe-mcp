# Factorio 配方、科技与存档进度 MCP

MCP 使用 **stdio**，由客户端启动 `python -m recipe_mcp`（或 `recipe-mcp` 命令）并管理进程，不监听HTTP端口。旧HTTP服务已停止。安装好的配置见 `mcp-config.json`：

```json
{
  "mcpServers": {
    "factorio-recipes": {
      "command": "G:/Programs/factorio/hotfix/recipe-mcp/.venv/Scripts/python.exe",
      "args": ["-m", "recipe_mcp"]
    }
  }
}
```

需要固定默认队伍时，在args末尾加入 `"--force", "faction-a632079"`。未指定默认队伍且存档有多个非系统队伍时，需要在工具调用中明确传入force。没有存档进度时仍可查全部原型和科技树，但状态标为unknown；不能推测当前已解锁。

## 数据更新

首次安装（本次已完成）：

```powershell
uv sync --directory recipe-mcp
```

依赖锁定在 `pyproject.toml` / `uv.lock`；`uv sync` 会按锁文件重建 `.venv`，并以可编辑方式安装本项目（`src/recipe_mcp`），生成 `recipe-mcp`、`recipe-mcp-client`、`recipe-mcp-export`、`recipe-mcp-export-save` 四个命令。以下命令均以 `uv run --directory recipe-mcp <命令>` 运行。

导出当前模组的最终配方、机器、科技原型：

```powershell
uv run --directory recipe-mcp recipe-mcp-export
```

从最新自动/手动存档导出科技进度，或明确选择一个存档：

```powershell
uv run --directory recipe-mcp recipe-mcp-export-save
uv run --directory recipe-mcp recipe-mcp-export-save --save 'C:/Users/a6320/AppData/Roaming/Factorio/saves/Nullius-Next.zip'
```

默认选择saves目录修改时间最新的zip，并在结果中记录实际选中的路径、存档副本SHA256和导出时间。它复制存档到独立目录，复制当前启用模组与设置，**仅在独立模组目录加入 `factorio-recipe-progress-helper`**，运行一次1 tick的benchmark导出；不会改写原存档，不会在正在游玩的mods目录安装helper，也不向正在运行的游戏发送命令。

加载副本时增加helper会触发模组配置变更回调，随后模拟1 tick。因此快照表示该存档在当前模组环境加载后的状态，可能包含其他模组的配置变更处理。来源、tick和此加载方式都记录在provenance中。benchmark输入副本本身也会做导出前后哈希核对。

导出器同时比较全部原配方的时间、类别、投入/产出及全部模组数据阶段校验值，忽略helper自身的零数据校验值。数量归一化到小数后6位，兼容游戏对极小物流占位数量的定点化。两者均匹配且原始原型文件SHA256一致时，MCP才允许用进度过滤和验证；不匹配时返回unknown并拒绝阶段计算。不要把旧存档进度与更新后的模组数据混用。

重新导出后重启MCP客户端中的服务进程，或重新运行一次性 `recipe-mcp-client`，即可读取新快照。

## 科技锁模型

参考本机Recipe Book 4.0.8的 `scripts/database/researched.lua`：分别检查force的已研究科技与实际启用配方。也参考Factoriopedia展示配方解锁科技的做法。

- **原型解锁关系**：科技effects中的unlock-recipe、科技前置、科学包或research_trigger。多个解锁科技是替代关系，不能要求全部研究。
- **实际科技状态**：按队伍读取researched、enabled、level、saved_progress；保留当前研究、当前进度和队列。科技enabled表示允许研究，不等于已研究。
- **实际配方状态**：以队伍的LuaRecipe.enabled为准，支持模组脚本单独开关配方。科技已研究但配方关闭时标为script_disabled；科技未研究但脚本开启配方时仍可用。
- **隐藏与锁定分开**：hidden影响显示，不意味着不可在机器中使用。列表可以包含隐藏科技。
- **未知与锁定分开**：没有兼容快照、缺失配方、尚未选择队伍时返回unknown，不把数据缺失当成locked。
- **无限科技/触发科技**：返回实际level和saved_progress；保留research_trigger和Nullius检查点要求，不只按科学包颜色判断。

配方availability.state为unlocked、locked、script_disabled、virtual、unknown；usable_at_stage为true/false/null，表示研究权限。科技state为researched、researching、available_to_research、locked、disabled、unknown。available_to_research只确认前置与enabled条件，不代表当前库存足够或触发事件已完成。

物流请求和创造模式的伪生产配方默认排除，阶段验证不允许它们。普通隐藏配方仍可查询并根据实际enabled判断。

## MCP工具

| 工具 | 用途 |
|---|---|
| get_progress_context | 存档来源、tick、兼容性、队伍列表、研究数量、当前研究和队列 |
| list_technologies | 科技分页、名称过滤、状态过滤、显示隐藏科技 |
| get_technology | 科技效果、直接前置、成本/触发条件、存档等级和进度 |
| technology_requirements | 递归前置、科学包、Nullius检查点代币、目标科技实际状态 |
| search_recipes | 配方搜索，available_only可筛当前队伍可用配方 |
| get_recipe | 配方数量、时间、产能许可、解锁科技和实际可用状态 |
| related_recipes | 材料的生产/消费配方，可筛当前可用 |
| production_chain | 默认只展开当前队伍已启用的上游配方，带深度/数量限制和循环保护 |
| compatible_machines | 类别兼容的机器、速度、耗电、槽位、流体接口和制造解锁状态 |
| validate_plan | 批量验证配方，以及可选machines/modules的制造权限 |
| net_balance | 默认拒绝锁定配方；显式validate_stage=false允许理论配比 |
| solve_production | Helmod/Factory Planner式量化计算：LP优化或矩阵精确解，输出机器数、插件/信标效果、电力、污染、导入、副产物、影子价格 |
| machine_stats | 单配方单机器计算器：有效速度/产能/耗电倍率、每台进出料、按目标速率求机器数 |
| production_matrix | 配方化学计量矩阵（物品×产线）、秩、物品角色与矩阵求解自由度诊断 |

搜索目前使用游戏内部英文ID，返回原始本地化键。状态过滤和默认阶段计算需要明确队伍。`net_balance`只算基础期望配比，不应用插件或队伍额外产能，也不证明不同温度流体可互换；实际配方产能加成会随get_recipe返回。

机器/插件检查通过对应物品的制造配方做科技验证，排除装拆箱和循环自返配方；映射不足时返回unknown，不假定可用。已经拥有的机器和库存没有扫描，因此尚不能制造但库存中已有的设备需要另行指定使用边界。

## 量化计算（`recipe_mcp.planner`）

产线模型：`{recipe, machine?, modules?, beacons?:[{beacon,count,modules,per_machine?}], fixed_machines?, max_machines?, cost_weight?}`，`recipe` 可写 `mining:<resource>` 表示采矿。效果按2.0原型文档计算：插件效果 + 信标（`distribution_effectivity × profile[信标数]`，`beacon_counter` 区分total/same_type）+ 机器 `effect_receiver.base_effect` + 存档中队伍的配方产能加成；速度/耗电/污染倍率下限20%，产能限制在 `[0, maximum_productivity]`，配方不允许产能时为0；插件的有益效果若不被机器/信标 `allowed_effects` 或配方 `allow_*` 允许则报错。电力机器未写drain时按 `energy_usage/30`（[CraftingMachinePrototype](https://lua-api.factorio.com/2.0.77/prototypes/CraftingMachinePrototype.html#energy_usage)）。

两种求解器：

- **lp**（默认）：HiGHS线性规划。`lines` 为空时从目标向上游自动发现当前阶段可用配方（排除装箱/桶装、虚拟、回收、发电、销毁、隐藏配方，最多 `max_lines` 条），在替代配方之间优化。仅无生产线的原料（或 `imports` 列出的）可导入；默认允许任意物品溢出但计罚。目标 `balanced`（机器数 + 0.01/单位导入 + 0.1/单位溢出）、`machines`、`power`、`imports`，`weights`/`import_costs` 可覆盖。返回 `shadow_prices`（每多产1单位/时间的目标函数边际成本）、`lines_for_matrix` 和 `matrix_args`。
- **matrix**：Factory Planner式方阵求解，必须给定lines（每个中间品一条配方）。物品角色自动判定：只消耗→导入未知量，只产出且非目标→副产物未知量，中间品平衡为0；`surplus_items`/`imports` 额外放开。欠定时返回零空间方向，矛盾时返回不能平衡的物品，负解给出警告而不隐藏。

推荐流程：先 lp 自动选路线，再把 `lines_for_matrix` + `matrix_args` 交给 matrix 固定方案，然后逐条改机器/插件/信标复算。`per` 支持second/minute/hour。

未建模：品质、表面效果、传送带/管道吞吐、流体矿产出衰减（按100%产量）、采矿机drain（未写时视为0）、热能/燃料机器的燃料链（单独报告为 `*_fuel_MW`）。流体温度只做告警检查。

```powershell
uv run --directory recipe-mcp recipe-mcp-client solve_production '{"targets":{"nullius-methanol":600},"per":"minute","force":"faction-a632079"}'
uv run --directory recipe-mcp recipe-mcp-client machine_stats '{"recipe":"nullius-methanol","rate":10,"force":"faction-a632079"}'
uv run --directory recipe-mcp recipe-mcp-client solve_production '{"targets":{"nullius-methanol":10},"objective":"power","defaults":{"modules":["nullius-yield-module-2","nullius-speed-module-2"]},"force":"faction-a632079"}'
uv run --directory recipe-mcp pytest tests/test_planner_synthetic.py tests/test_planner_real.py
```

## 调用示例

`recipe-mcp-client` 会用同一解释器启动 stdio 服务（`python -m recipe_mcp`）、完成调用并关闭子进程；不带参数时列出全部工具：

```powershell
uv run --directory recipe-mcp recipe-mcp-client get_progress_context
uv run --directory recipe-mcp recipe-mcp-client list_technologies '{"force":"faction-a632079","state":"researched","limit":20}'
uv run --directory recipe-mcp recipe-mcp-client get_technology '{"name":"nullius-high-pressure-chemistry","force":"faction-a632079"}'
uv run --directory recipe-mcp recipe-mcp-client related_recipes '{"material":"nullius-methanol","direction":"producers","available_only":true,"force":"faction-a632079"}'
uv run --directory recipe-mcp recipe-mcp-client validate_plan '{"recipe_rates":{"nullius-fermentation":1},"force":"faction-a632079"}'
```

也可由客户端调用start.ps1启动前台stdio服务；它不会自行创建后台窗口，stdout专用于协议。

## 项目结构

```
recipe-mcp/
├── pyproject.toml / uv.lock     uv 项目（uv_build，src 布局）
├── mcp-config.json / start.ps1  MCP 客户端配置示例 / 前台 stdio 启动器
├── src/recipe_mcp/              MCP 本体（可安装包）
│   ├── paths.py                 项目根、data 目录、原型/进度文件路径（RECIPE_MCP_HOME / RECIPE_MCP_DATA 可覆盖）
│   ├── database.py              原型索引与存档科技门控
│   ├── planner/                 量化计算
│   │   ├── model.py             产线模型：机器、插件、信标、产能、电力、阶段门控、自动发现
│   │   ├── lp.py                LP 求解（HiGHS）
│   │   ├── matrix.py            矩阵精确求解与化学计量矩阵分析
│   │   ├── report.py            结果整理：产线、物品流、合计、影子价格
│   │   └── api.py               plan / machine_stats / production_matrix 入口
│   ├── server.py                MCP 工具注册（create_server）与 recipe-mcp 入口
│   ├── client.py                一次性 stdio 客户端（recipe-mcp-client）
│   └── export/                  数据导出：prototypes.py（recipe-mcp-export）、save.py（recipe-mcp-export-save）
├── scripts/                     一次性分析与报告，不属于 MCP 本体，通过 `import recipe_mcp` 复用
│   ├── analysis/                methanol.py、science_fluids.py、pressure_transition.py
│   └── pressure_report/         build.py、zones.py、check.py（需另装 playwright）、templates/
├── tests/                       pytest：合成数学、门控语义、真实数据、stdio 端到端
├── docs/spec/                   需求/设计/任务 spec
└── data/                        导出数据与分析产物（不入库）
```

测试：`uv run --directory recipe-mcp pytest`。真实数据测试（`realdata` 标记）在 `data/` 缺少兼容导出时自动跳过；队伍默认 `faction-a632079`，可用环境变量 `RECIPE_MCP_TEST_FORCE` 指定。

## 文件与复算

- `data/script-output/data-raw-dump.json`：所有最终原型。
- `data/recipes.json`、`data/technologies.json`：便于阅读的摘要。
- `data/manifest.json`、`data/mod-list.snapshot.json`：原型导出来源和哈希。
- `data/progress.json`：各队伍科技与配方状态、来源和一致性验证。
- `data/progress-*/`：隔离加载目录、存档副本、helper输出与日志，保留供核验。
- `../factorio-recipe-progress-helper_0.1.0.zip`：helper安装包，内部为同名mod目录。

甲醇计算现在默认按进度快照筛选配方、机器和插件；未知/锁定候选被排除。传入队伍后，输出到 `data/methanol-current-stage.json`，并包含每项阶段验证与原存档来源：

```powershell
uv run --directory recipe-mcp scripts/analysis/methanol.py --force faction-a632079
uv run --directory recipe-mcp scripts/analysis/methanol.py --theoretical
uv run --directory recipe-mcp pytest tests/test_analysis_outputs.py
```

--theoretical显式忽略当前研究，重算上一版假设科技阶段的14组对比，写入独立的methanol-analysis.json。当前阶段计算保留已导出的配方额外产能加成；科技过滤通过并不代表完整工厂可建，还需满足机器流体接口、原料供应、表面条件等。本甲醇计算仍限于已声明的空气/水合成链与设备/插件等级，不是所有生物链、插件塔或整数布局的全局最优。

参考：[Factoriopedia官方介绍](https://www.factorio.com/blog/post/fff-397)、[LuaRecipe.enabled](https://lua-api.factorio.com/2.0.77/classes/LuaRecipe.html#enabled)、[LuaTechnology](https://lua-api.factorio.com/2.0.77/classes/LuaTechnology.html)。实现字段已对照本机2.0.77自带runtime-api.json，避免使用新版本才有的接口。
