# 对象名称与可视化语言

## 目标

MCP 与离线规划观察器默认显示游戏的英文对象名称，允许指定其他语言，同时保留原型 ID。名称变化不得影响求解、温度约束、区块引用、缓存指纹或保存的配方选择。

## 数据和解析

- 从本次原型导出的 mod 版本与加载顺序读取游戏和 mod 的 locale CFG，保存 `data/locales.json`，绑定原型 SHA-256。可单独补导出，不需要启动或影响游戏。
- 支持英文及已安装语言；解析显式 `localised_name`、嵌套参数、连接、候选回退和对象名称引用。配方在主产物/单一产物与配方同名时继承其名称，否则使用配方名称键；建筑物品继承实体名称，装备物品继承装备名称。科技按游戏规则处理等级后缀。
- 请求语言缺少键时回退英文，仍然无法解析时返回 ID；返回实际解析语言及回退状态。无法安全解析的控制/复数等特殊表达式不伪装为成功翻译。
- 快照不存在或与原型不匹配时不读取不相关 mod 的名称；返回可观测的警告和 ID。记录来源遗漏，不能宣称与运行时翻译完全等价。

## MCP

- 配方、科技、机器查询增加 `display_name`、语言和解析状态，保留现有 `name` 和原始 `localised_name`。
- 查询和搜索支持 `language`，默认 `en`。搜索同时匹配 ID 和显示名称。
- 新增批量 `get_object_names`，使用 `{kind, name}` 精确区分配方、物品、流体、实体、科技，以及具体原型类型。
- `solve_production`、`plan_solve` 返回仅涉及当前结果的名称字典，并接受语言参数。字典以带类型的 ID 为键，保持原始图协议及流量键不变。

## 可视化

- 图节点、物料账本、设备/插件及区块引用使用显示名称；温度、内部能源通道及虚拟配方类型仍可辨识。
- 报告内支持已嵌入语言与原型 ID 模式切换，名称与 ID 均可搜索；悬停/点击可检查原始 ID 和翻译状态。
- CLI 可用重复的 `--language` 指定嵌入语言，首个为初始显示语言；英文始终作为回退。报告完全离线，无外部资源依赖。

## 验收

1. 合成 CFG/原型：覆盖嵌套、参数、回退、主产物/实体继承、同名异类型、缺失键、循环、mod 覆盖与版本不匹配。
2. 真实 Nullius：英文/简体中文名称与游戏 `--dump-prototype-locale` 导出逐项核对，先确认设置哈希及所有 data-stage checksum 一致；MCP stdio 验证默认英文、指定语言、名称搜索与求解字典。
3. 同一方案切换语言后，所有求解数值及原型 ID 不变。
4. 浏览器验证图、表、语言切换、名称/ID 搜索与详情；保留原有 HTML 注入防护测试。
5. 全套 pytest、mypy、ruff 与 wheel 构建通过后提交。

参考：[命名规则](https://wiki.factorio.com/Tutorial:Localisation#Default_behaviour)、[LocalisedString](https://lua-api.factorio.com/latest/types/LocalisedString.html)、[RecipePrototype.main_product](https://lua-api.factorio.com/latest/prototypes/RecipePrototype.html#main_product)、[ItemPrototype.place_result](https://lua-api.factorio.com/latest/prototypes/ItemPrototype.html#place_result)。

## 实施与验证结果（2026-10-05）

- 已实现名称快照、MCP 查询/搜索/批量名称接口、求解结果名称字典，以及离线语言切换。当前快照包含 55 个语言目录的 1,249 个 CFG 文件，包括只提供翻译的 mod。
- 游戏原生对照：Factorio 2.0.77，Nullius 2.0.11；mod 列表、启动设置哈希和所有 data-stage checksum 与原型导出一致。英语 12,144 条、简体中文 12,323 条，共 24,467 条有效名称，差异为 0。原生导出未给出有效名称的对象不计入该数字。
- 对照发现并修正：异名配方不能直接采用产物名称、科技等级后缀、嵌入式对象引用及 CFG 值的前导空格。相应规则已加入回归测试。
- `uv run pytest -q`：164 passed；`uv run mypy`：54 个文件通过；`uv run ruff check`、wheel 构建和 `git diff --check` 通过。
- 浏览器验证：默认英文；简体中文和德语切换；名称及 ID 搜索；节点详情保留 ID、译文状态与真实流量；加载发电站 JSON 后可搜索带温度的蒸汽；无浏览器脚本错误。
- 本地可复现产物：`data/reports/helmod-gaps/factory.html`、`power.html`、`locale-parity.json`；由 README 中两条 validation 脚本生成，产物按仓库惯例不入版本控制。
- 保留的范围限制：控件绑定、复数模板和运行时脚本文本不作完整游戏解释；缺少语言键回退英文，无法解析回退 ID，并暴露状态。
