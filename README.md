# recipe-mcp

Factorio recipe, technology and save-progress queries plus Helmod / Factory Planner style production planning, exposed as MCP tools. Built against Factorio 2.0.77 and the Nullius mod set in this workspace, but the data comes from whatever mods are active when you export.

## Entry points

The same 21 tools are reachable two ways:

| Entry | Command | Use |
|---|---|---|
| **MCP server** | `recipe-mcp-server` (or `python -m recipe_mcp`) | Launched by an MCP client over stdio. No HTTP port. |
| **CLI** | `recipe-mcp-cli [TOOL] [JSON_ARGS] [--force NAME]` | Calls one tool from the shell, prints the JSON result and exits. With no `TOOL` it lists the tools. |

The CLI starts the server with the same interpreter and talks to it over a real stdio session, so its output and errors match what an MCP client sees. It exits non-zero when the tool returns an error.

Both accept `--force NAME` to set the default force for stage-aware queries. Without it, a save with several non-system forces needs `force` in every stage-aware call.

Two more commands refresh the data both entries read: `recipe-mcp-export` (prototypes) and `recipe-mcp-export-save` (save progress). See [Data export](#data-export).

## Setup

```powershell
uv sync --directory recipe-mcp
```

This builds `.venv` from `uv.lock` and installs the package editable, so the four commands above exist. Every command below is run as `uv run --directory recipe-mcp <command>`.

### MCP client configuration

`mcp-config.json`:

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

To pin a default force, append `"--force", "faction-a632079"` to `args`. `start.ps1 [-ForceName NAME]` is an alternative launcher. It runs the server in the foreground through `uv run`, and stdout carries only the protocol.

### CLI examples

```powershell
uv run --directory recipe-mcp recipe-mcp-cli
uv run --directory recipe-mcp recipe-mcp-cli get_progress_context
uv run --directory recipe-mcp recipe-mcp-cli --force faction-a632079 get_recipe '{"name":"nullius-methanol"}'
uv run --directory recipe-mcp recipe-mcp-cli list_technologies '{"force":"faction-a632079","state":"researched","limit":20}'
uv run --directory recipe-mcp recipe-mcp-cli get_technology '{"name":"nullius-high-pressure-chemistry","force":"faction-a632079"}'
uv run --directory recipe-mcp recipe-mcp-cli related_recipes '{"material":"nullius-methanol","direction":"producers","available_only":true,"force":"faction-a632079"}'
uv run --directory recipe-mcp recipe-mcp-cli validate_plan '{"recipe_rates":{"nullius-fermentation":1},"force":"faction-a632079"}'
```

## Tools

| Tool | Purpose |
|---|---|
| `get_progress_context` | Save source, tick, compatibility, forces, research counts, current research and queue |
| `list_technologies` | Paged technologies with name filter, state filter and optional hidden ones |
| `get_technology` | Effects, direct prerequisites, cost or trigger, saved level and progress |
| `technology_requirements` | Recursive prerequisites, science packs, Nullius checkpoint tokens, target state |
| `search_recipes` | Recipe search; `available_only` keeps recipes usable by the force |
| `get_recipe` | Amounts, time, productivity permission, unlocking technologies, availability |
| `related_recipes` | Producers and/or consumers of an item or fluid |
| `production_chain` | Upstream expansion, by default only through enabled recipes, with depth/count limits and cycle protection |
| `compatible_machines` | Machines for a recipe's category: speed, power, slots, fluid boxes, buildability |
| `validate_plan` | Checks recipes, and optionally machines/modules, against research |
| `net_balance` | Base net item flow for recipe rates; rejects locked recipes unless `validate_stage=false` |
| `solve_production` | LP or matrix production solver: machine counts, module/beacon effects, power, pollution, imports, surplus, shadow prices |
| `machine_stats` | One recipe in one machine: effective speed/productivity/power multipliers, per-machine flow, machines for a rate |
| `production_matrix` | Item × line stoichiometric matrix, rank, item roles and determinacy diagnostics |
| `plan_list` | Saved plans with revision, update time and staleness |
| `plan_get` | One saved plan: block requests and last result summaries |
| `plan_save` | Create or overwrite a named plan of blocks |
| `plan_edit` | Atomic, revision-checked edit operations on a plan |
| `plan_solve` | Solve a plan's blocks in dependency order and sum them into a factory ledger |
| `plan_compare` | Saved results of two plans or blocks side by side: totals, imports, surplus |
| `plan_delete` | Move a plan to the trash folder |

Every result is a pydantic model, so each tool publishes an output schema and returns `structuredContent` alongside the JSON text. Names are internal prototype IDs; localized names come back as raw locale keys. `net_balance` ignores modules and force productivity bonuses and does not treat fluids at different temperatures as interchangeable.

## Research gating

Modeled on Recipe Book 4.0.8 (`scripts/database/researched.lua`), which checks a force's researched technologies and its actually enabled recipes separately.

- **Prototype unlocks**: `unlock-recipe` effects, prerequisites, science packs or `research_trigger`. Several technologies unlocking one recipe are alternatives, not all required.
- **Technology state** comes from the save per force: `researched`, `enabled`, `level`, `saved_progress`, current research and queue. `enabled` means researchable, not researched.
- **Recipe state** follows the force's `LuaRecipe.enabled`, so mod scripts that toggle recipes are respected. Researched but disabled is `script_disabled`; enabled by script without research is still usable.
- **Hidden is not locked**: `hidden` affects display only.
- **Unknown is not locked**: no compatible snapshot, missing recipe or no force selected yields `unknown`, never `locked`.
- **Infinite and trigger technologies** report the real level and progress and keep `research_trigger` and Nullius checkpoint requirements instead of judging by science pack colour.

Recipe `availability.state` is one of `unlocked`, `locked`, `script_disabled`, `virtual`, `unknown`. `usable_at_stage` is `true`, `false` or `null` and means research permission only. Machines, ingredients and surface conditions are separate checks. Technology state is one of `researched`, `researching`, `available_to_research`, `locked`, `disabled`, `unknown`. `available_to_research` checks prerequisites and `enabled` only, not inventory or trigger completion.

Logistics-request and creative-mode pseudo-recipes are excluded by default and never pass stage validation. Ordinary hidden recipes can still be queried.

A machine or module counts as buildable when its item's crafting recipe is usable. Boxing/unboxing and self-returning loop recipes are ignored. When the mapping is insufficient the result is `unknown`. Inventory is not scanned, so equipment you already own but cannot yet craft must be allowed explicitly.

## Production planning (`recipe_mcp.planner`)

A line is `{recipe, id?, machine?, modules?, beacons?: [{beacon, count, modules, per_machine?, interference?}], fixed_machines?, max_machines?, cost_weight?, ignore_module_rules?}`; `recipe` may be `mining:<resource>`. A bare recipe name is also accepted. Line specs, `defaults` and beacons are pydantic models (`LineSpec`, `Defaults`, `BeaconSpec`): the tools publish them in their input schema, and unknown keys are rejected rather than ignored.

Effects follow the 2.0 prototype docs:

- Module effects are added to beacon effects, which are `distribution_effectivity × profile[beacon count]` with `beacon_counter` `total` or `same_type`.
- The machine's `effect_receiver.base_effect` and the force's recipe productivity bonus from the save are also added.
- Speed, consumption and pollution multipliers are floored at 20%.
- Productivity is clamped to `[0, maximum_productivity]`, and is 0 when the recipe disallows it.
- A beneficial module effect the machine, beacon (`allowed_effects`) or recipe (`allow_*`) does not permit is an error, and so is a module whose category the machine or beacon excludes (`allowed_module_categories`).
- Electric crafting machines without a declared `drain` draw `energy_usage / 30` ([CraftingMachinePrototype](https://lua-api.factorio.com/2.0.77/prototypes/CraftingMachinePrototype.html#energy_usage)).

Two solvers:

- **`lp`** (default): HiGHS linear program.
  - **Discovery**: with empty `lines`, it walks upstream from the targets through recipes usable at the current stage, up to `max_lines`. Barrelling/boxing, virtual, recycling, legacy, creative, power generation, void and hidden recipes are skipped. It then optimizes across the alternatives it found.
  - **Imports and surplus**: only items with no producing line, or those listed in `imports`, can be imported. Any item may be overproduced, at a penalty.
  - **Objectives**: `balanced` weighs machines plus 0.01 per unit/s imported plus 0.1 per unit/s surplus. The others are `machines`, `power` and `imports`, and `weights` / `import_costs` override the presets.
  - **Output** adds `shadow_prices` (the marginal objective cost of one more unit/s), `lines_for_matrix` and `matrix_args`.
- **`matrix`**: Factory Planner style square solve. It requires `lines`, with one recipe per intermediate.
  - **Item roles**: consumed-only items become import unknowns; produced-only non-targets become byproduct unknowns; intermediates balance to zero. `surplus_items` and `imports` free more items.
  - **Diagnostics**: an underdetermined system returns null-space directions, and an inconsistent one returns the items that cannot balance. Negative solutions produce a warning, not a silent fix.

Typical flow: let `lp` pick the routes, then pass `lines_for_matrix` and `matrix_args` to `matrix` to pin the plan, and adjust machines, modules and beacons line by line. `per` is `second`, `minute` or `hour`.

### Calculation modes (lp)

- **`limits`**: `{imports: {item: rate}, power_MW, machines, machines_by_type: {machine: n}, beacons, beacons_by_type: {beacon: n}, modules: {module: n}, pollution_per_minute}`. Rates are per `per`; counts are fractional. `machines` excludes beacons and disposal machines. A capped import is allowed even when the item has a producing line. Every limit comes back in `limits_usage`; binding ones are listed in `bottlenecks` with their marginal value.
- **`mode="maximize"`**: targets are ratios. The LP first finds the largest `scale` (output = `scale` × ratio) that fits the limits, then the cheapest plan at that scale. Bottleneck marginals are the scale gained per unit of limit. Without any limit the problem is unbounded and is rejected.
- **`consume`**: `{item: rate}` supplied externally and used up exactly, never imported or left over. Works with targets or ratios in `lp`; in `matrix` the targets may be empty and the outputs come back as byproducts (Helmod input mode).
- **Infeasibility**: when limits or consume amounts cannot all hold, the result has `status: "infeasible"`, the relaxations the elastic LP needed in `infeasibility` (a limit raised, a target or consume amount lowered), `suggestions`, and the relaxed plan in `lines`. The relaxations minimise the total relative violation together, so they are one combination, not each quantity's own maximum. `fixed_machines` and `max_machines` stay hard.
- **`integer_machines`**: whole machine counts per line carry the machine cost and the machine, power, pollution, beacon and module limits (a MILP; `time_limit` seconds per stage, after which the best plan comes back with `status: "time_limit"` and `mip_gap`). No marginal values and no elastic diagnosis. At most 400 lines, so pin the route continuously first for large plans.
- **Disposal** (`disposal="report"`, default): each surplus byproduct gets the void recipe and machine that vents it with the fewest machines, preferring the most power-efficient machine (`disposal_defaults` overrides, including modules). They are reported in `disposal`, `disposal_totals` and `totals_with_disposal`; `totals` and the route stay unchanged.

### Modules and beacons

- A beacon's `count` is how many beacons affect each machine (the effect); `per_machine` is how many beacon entities each machine accounts for (power and counts), lower than `count` when beacons are shared.
- `interference: k` uses the Nullius small-beacon variant `<beacon>-k` (k large-beacon interference fields); writing the variant name directly works too. Counts use the item that places the beacon, so variants count as their base beacon.
- Results give `beacon_count`, `beacon_count_ceil` and `beacon_power_MW` per line and in `totals`, `totals.beacon_count_by_type`, and `module_inventory` (machine slots plus beacon slots, disposal machines included; beacon modules count once per beacon entity).
- Several lines of one recipe with different modules (distinct `id`s) plus `limits.modules` let the LP allocate a limited module stock.
- `defaults.module_options` (automatic module selection) and the `beacons` / `modules` objective weights are reserved and rejected as not implemented.

### Plans

A plan is a named JSON file in `data/plans/` with blocks, each a `solve_production` request without `force` and `per` (those are plan-wide) plus a summary of its last result.

- `plan_save` validates every block like `solve_production` would, without solving, before writing. Names are 1-64 characters of `A-Z a-z 0-9 _ . -`.
- `plan_edit` applies a list of operations all or nothing and bumps the revision; pass `expected_revision` to fail on a concurrent change. `pin` replaces a block's lines with its last result's `lines_for_matrix` (optionally switching solver); `set_modules`, `set_beacons` and `replace_module` change modules and beacons on lines or block defaults.
- A target may be a link `{"from": ["block", ...] | "*", "plus": n}`: the sum of what those blocks import of the item. `plan_solve` solves the blocks in dependency order (cycles are reported with their path) and returns per-block results and a factory ledger: `net_inputs`, `net_outputs`, `internal_transfers`, totals, beacons, modules, and disposal for the surplus other blocks do not take. It assumes a shared bus and solves blocks independently.
- `plan_compare` puts the saved results of two plans (enabled blocks summed) or blocks (`plan/block`) side by side; it refuses missing or stale results.
- Plans record the prototype hash, save tick and save copy hash and report `fresh`, `stage_changed` or `prototypes_changed`. Stale plans still solve with the current data, with a warning. `plan_delete` moves the file to `data/plans/.trash/`.

Not modeled:

- Quality and surface effects.
- Belt and pipe throughput.
- Fluid-resource depletion (yield is taken as 100%).
- Mining drill drain (0 unless declared).
- Fuel chains of burner/heat machines. Their fuel is reported per line as `fuel_MW` (next to `energy_type`) and in total as `non_electric_fuel_MW`.

Fluid temperatures only raise warnings.

```powershell
uv run --directory recipe-mcp recipe-mcp-cli solve_production '{"targets":{"nullius-methanol":600},"per":"minute","force":"faction-a632079"}'
uv run --directory recipe-mcp recipe-mcp-cli machine_stats '{"recipe":"nullius-methanol","rate":10,"force":"faction-a632079"}'
uv run --directory recipe-mcp recipe-mcp-cli solve_production '{"targets":{"nullius-methanol":10},"objective":"power","defaults":{"modules":["nullius-yield-module-2","nullius-speed-module-2"]},"force":"faction-a632079"}'
uv run --directory recipe-mcp recipe-mcp-cli solve_production '{"targets":{"nullius-methanol":1},"mode":"maximize","limits":{"power_MW":5,"imports":{"nullius-box-limestone":2}},"force":"faction-a632079"}'
uv run --directory recipe-mcp recipe-mcp-cli machine_stats '{"recipe":"nullius-methanol","beacons":[{"beacon":"nullius-beacon-2","interference":1,"modules":["nullius-speed-module-1"]}],"force":"faction-a632079","validate_stage":false}'
uv run --directory recipe-mcp recipe-mcp-cli plan_save '{"name":"methanol-10ps","force":"faction-a632079","blocks":[{"id":"methanol","request":{"targets":{"nullius-methanol":10}}}]}'
uv run --directory recipe-mcp recipe-mcp-cli plan_solve '{"name":"methanol-10ps"}'
uv run --directory recipe-mcp recipe-mcp-cli plan_edit '{"name":"methanol-10ps","ops":[{"op":"pin","block_id":"methanol"}]}'
uv run --directory recipe-mcp recipe-mcp-cli plan_delete '{"name":"methanol-10ps","confirm":"methanol-10ps"}'
```

## Data export

Export the final recipe, machine and technology prototypes of the active mod set. This runs `factorio --dump-data` against your mods directory and does not touch a running game:

```powershell
uv run --directory recipe-mcp recipe-mcp-export
```

Export research progress from the newest save, or from an explicit one:

```powershell
uv run --directory recipe-mcp recipe-mcp-export-save
uv run --directory recipe-mcp recipe-mcp-export-save --save 'C:/Users/a6320/AppData/Roaming/Factorio/saves/Nullius-Next.zip'
```

Both accept `--factorio` and `--mods` to override the default Steam install and mods paths.

**How `recipe-mcp-export-save` reads the save:**

1. It picks the newest `.zip` in `saves` by modification time, unless `--save` is given.
2. It copies the save, the enabled mods and their settings into an isolated directory under `data/progress-*`.
3. It adds `factorio-recipe-progress-helper` to that copy only, never to the mods directory you play with.
4. It runs a 1-tick benchmark that writes the progress export.

The original save is never modified, and no command is sent to a running game. The chosen path, the copy's SHA256, the tick and the export time are recorded as provenance, and the benchmark input is hash-checked before and after.

Adding the helper triggers mod configuration-change handlers on load, followed by one simulated tick. The snapshot is therefore the save *as loaded under the current mod set*, which may include other mods' migration handling.

**Compatibility check.** Progress-based filtering is only enabled when the snapshot matches the prototype export:

- Every recipe's time, category, ingredients and results match the prototype export. Amounts are normalized to 6 decimals to absorb the game's fixed-point rounding of tiny logistics placeholder amounts.
- Every mod's data-stage checksum matches, ignoring the helper's own empty checksum.
- The raw prototype dump's SHA256 matches.

Otherwise state is `unknown` and stage calculations are refused. Never mix old progress with updated mod data.

After re-exporting, restart the server in your MCP client. The CLI starts a fresh server on every call, so it picks up new data immediately.

## Files

| Path | Content |
|---|---|
| `data/script-output/data-raw-dump.json` | All final prototypes |
| `data/recipes.json`, `data/technologies.json` | Readable summaries |
| `data/manifest.json`, `data/mod-list.snapshot.json` | Prototype export provenance and hashes |
| `data/progress.json` | Per-force technology and recipe state, provenance and consistency checks |
| `data/progress-*/` | Isolated load directory, save copy, helper output and logs, kept for auditing |
| `data/plans/` | Saved plans; `.trash/` holds deleted ones |
| `../factorio-recipe-progress-helper_0.1.0.zip` | Helper mod package |

`data/` is not tracked by git. `RECIPE_MCP_HOME` overrides the project root and `RECIPE_MCP_DATA` only the data directory.

## Project layout

```
recipe-mcp/
├── pyproject.toml / uv.lock     uv project (uv_build, src layout)
├── mcp-config.json / start.ps1  MCP client config example / foreground server launcher
├── src/recipe_mcp/              the installable package
│   ├── server.py                MCP server: tool registration (create_server), recipe-mcp-server entry
│   ├── cli.py                   CLI: recipe-mcp-cli entry
│   ├── __main__.py              python -m recipe_mcp → server
│   ├── paths.py                 project root, data directory, dump/progress paths, exporter defaults
│   ├── models.py                pydantic models of query results (raw prototypes and the snapshot stay JSON)
│   ├── database.py              prototype index and save-based research gating
│   ├── planner/                 production planning
│   │   ├── schema.py            pydantic models: line specs, defaults, normalised lines, solutions, results
│   │   ├── model.py             line model: machines, modules, beacons, productivity, power, gating, discovery
│   │   ├── lp.py                LP builder and solver (HiGHS): limits, maximize, consume, elastic diagnosis
│   │   ├── limits.py            limits -> LP rows; usage, bottleneck and infeasibility reports
│   │   ├── matrix.py            exact matrix solver and stoichiometric analysis
│   │   ├── disposal.py          void machines for surplus byproducts
│   │   ├── report.py            result shaping: lines, item flows, totals, beacons, modules, shadow prices
│   │   └── api.py               plan / machine_stats / production_matrix
│   ├── plans/                   saved plans
│   │   ├── models.py            plan file schema, block requests, result summaries, edit operations
│   │   ├── store.py             PlanStore: names, validation, atomic writes, revisions, fingerprints, trash
│   │   ├── ops.py               plan_edit operations and pin
│   │   └── factory.py           plan_solve and plan_compare: dependency order, linked targets, factory ledger
│   └── export/                  prototypes.py (recipe-mcp-export), save.py (recipe-mcp-export-save)
├── scripts/                     one-off analyses, not part of the package; they import recipe_mcp
│   ├── analysis/                methanol.py, science_fluids.py, pressure_transition.py
│   └── pressure_report/         build.py, zones.py, check.py (needs playwright), templates/
├── tests/                       pytest: synthetic math, gating semantics, real data, stdio end to end
├── docs/spec/                   requirement / design / task specs
└── data/                        exports and analysis outputs (untracked)
```

## Development

```powershell
uv run --directory recipe-mcp pytest
uv run --directory recipe-mcp mypy
uv run --directory recipe-mcp ruff check
uv run --directory recipe-mcp ruff format
```

Tests marked `realdata` skip when `data/` has no compatible export. They use force `faction-a632079`; set `RECIPE_MCP_TEST_FORCE` to override. mypy runs with `disallow_untyped_defs` and the pydantic plugin over `src`, `tests` and `scripts`. Command lines are built with click.

## Methanol analysis script

`scripts/analysis/methanol.py` filters recipes, machines and modules by the progress snapshot and writes `data/methanol-current-stage.json`, including each stage check and the save provenance:

```powershell
uv run --directory recipe-mcp scripts/analysis/methanol.py --force faction-a632079
uv run --directory recipe-mcp scripts/analysis/methanol.py --theoretical
uv run --directory recipe-mcp pytest tests/test_analysis_outputs.py
```

`--theoretical` ignores current research and recomputes the 14 comparison scenarios of the earlier assumed stage into `data/methanol-analysis.json`.

The current-stage run keeps the exported recipe productivity bonuses. Passing the research filter does not make a factory buildable: fluid boxes, supply and surface conditions still apply. The analysis covers only the declared air/water synthesis chain and the listed machine and module tiers. It is not a global optimum over all biological routes, beacon layouts or integer machine counts.

## References

- [Factoriopedia (FFF #397)](https://www.factorio.com/blog/post/fff-397)
- [LuaRecipe.enabled](https://lua-api.factorio.com/2.0.77/classes/LuaRecipe.html#enabled)
- [LuaTechnology](https://lua-api.factorio.com/2.0.77/classes/LuaTechnology.html)

Runtime fields were checked against the `runtime-api.json` shipped with the local 2.0.77 install, to avoid APIs from newer versions.


### Energy balance and repeated blocks

`solve_production` accepts `energy_mode="balance"`: electric and heat flows use
`energy:electric` and `energy:heat`, always in MW even with `per="minute"`.
Energy cannot be imported unless explicitly listed in `imports` or `limits.imports`.
Use `heat:<reactor>`, `generate:<generator>`, `solar:<panel>` or `wind:<interface>`
as lines, or let discovery find them. Solar output defaults to 0.7 of peak;
wind requires an explicit `wind_factor`. Choose burner fuel with `lines[].fuel`
or the priority list `defaults.fuel`; burnt results enter the balance.

Example: `{"targets":{"energy:electric":10},"energy_mode":"balance"}`.
`copies=4` reports four identical construction modules; input rates and limits
are totals, while `per_copy` shows each module and `copies_totals` includes its
individual construction rounding. Integer solving applies the limits per copy.

Plan targets and consume values accept `{from:["block"], of:"surplus", factor:0.5}`;
`of` defaults to `imports` for targets and `surplus` for consume. `plan_edit`
supports `add_supply_block` and `add_consumer_block` with `for_block`, `item`,
`new_id` and optional `request` overrides. Consumers need `request.lines` or
`request.targets`. Factory suggestions contain supply edits and consumer templates.

Nullius turbine generator rows represent internal helper entities created by the
mod, alongside the turbine recipe rows. Their machine counts are model capacities,
not two separate purchasable entities. Geothermal heat assumes suitable placement;
solar collector input uses full-flow consumption. These assumptions appear in warnings.


Helmod 2.2.14 parity checks execute the original Lua methods with controlled
prototype stubs (`scripts/validation/helmod_reference.py`). Set
`defaults.productivity_model="helmod"` (or the same field on a line) to reproduce
its probability/productivity deduction order. The default `expected` model
retains existing results. Pinned lines preserve the choice.

`graph="bipartite"` returns a versioned production flow graph; `plan_solve`
accepts `graph="blocks"` or `"full"`. `graph_options.allocate=true` includes
proportional allocation edges as an explicit mixing assumption. Graphs use
achieved flows, including relaxed infeasible results. Fluid temperatures retain
separate keys and zero-cost matching edges; internal Nullius turbine energy
channels are isolated by turbine type and tier.


### Offline planning viewer

Export any `solve_production` result with `graph="bipartite"`, or a
`plan_solve` result with `detail="full", graph="full"`, as JSON, then run:

```powershell
uv run python -m recipe_mcp.viewer result.json report.html
```

MCP CLI response envelopes are also accepted. Open the resulting HTML directly:
no server, CDN or additional project dependency is required. It shows factory and
block flow graphs, searchable line/material tables, reference values, node details,
power/heat summaries, snapshot provenance and assumptions. You can load another
JSON result, download the embedded data, and zoom the diagram.

Generate the real Nullius examples and performance measurements:

```powershell
uv run python scripts/validation/planning_examples.py
```

Outputs are local under `data/reports/helmod-gaps/`: `factory.html` for a linked
methanol/utility factory, `power.html` for 10 MW generation, their JSON files, and
`metrics.json`. See [implementation evidence](docs/spec/2026-10-04-helmod-gaps/implementation-results.md)
for test coverage, Helmod reference provenance and model boundaries.
