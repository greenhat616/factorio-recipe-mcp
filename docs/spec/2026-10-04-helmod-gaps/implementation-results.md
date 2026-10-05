# 实施与验收结果

日期：2026-10-05。先完成 implementation.md 规格补充，再实施；核心规划与离线查看器分别提交。

## 已交付

- A1–A4：导入、溢出、正净产出引用；倍数与常量；消耗引用的拓扑排序、环检测、重命名和删除保护；补供应/消耗块；建议；连续和整数区块份数。
- B1–B5：MW 能源平衡；燃料选择、燃烧产物；反应堆、发电机、太阳能、风机；风光系数；电热工厂账本；machine_stats 能源参数。
- C1：温度池与零成本匹配，LP/矩阵/整数共用；自动发现蒸汽发电温度变体；匹配列不计设备。
- D1–D3：版本化守恒图、端点、最短深度、比例分摊、区块引用和完整子图；默认省略 graph；Mermaid/DOT 保留字段仍明确拒绝。独立 HTML 导出已追加实现。

## 自动验证

135 项 pytest 全部通过，包含真实数据和 MCP stdio 测试，无跳过。mypy 检查 50 个文件通过；ruff 通过。

新增验证覆盖：40 单位溢出全部/一半消耗；引用环和改名；补块；4 份取整及整数容量；发电效率；4 GJ 燃料棒；燃料类别拒绝与默认优先级；电网导入上限及秒/分/小时 MW 不变；自动电热链；风机缺系数；165/500°C 温度隔离；真实 Nullius 甲醇与 10 MW 发电；图守恒、重复 ID、矩阵/整数/弹性结果、full 子图、100 条选中产线体积；HTML 注入转义与 CLI。

## Helmod 证据边界

执行本机 Helmod 2.2.14 zip 内未经改写的 Lua 方法，使用原型 API 桩注入受控输入，得到 tests/fixtures/helmod-2.2.14.json。它是原始公式执行对照，不是游戏 GUI 点击测试，也没有声称替代完整 Helmod 求解器。

ZIP SHA256：5b0f00b7eddca81d46ad9d073b8b1af6a51e4a148507db68b67e15f1dbe949f3。

- ModelCompute.computeFactory：基础、速度、节能、信标四组台数/电力。
- Product：确定、概率、区间产物和产能组合。
- EntityPrototype：流体发电、反应堆、太阳能峰值和邻接布局系数。
- BurnerPrototype：燃料消耗。
- 已发现的概率产能扣除差异通过 defaults.productivity_model="helmod" 精确复现；默认 expected 保持旧值。pin 保留所选算法。

独立闭式测试与真实 Nullius 集成测试分别验证额外能源/区块能力，不将它们标成 Helmod 实测。LP 优选路线、共享能源总线及整数优化是本项目语义；比较路线需先固定配方和机器。

重建黄金数据（lupa 只供验收脚本运行，不加入项目依赖）：

```powershell
uv run --with lupa python scripts/validation/helmod_reference.py 'C:/Users/a6320/AppData/Roaming/Factorio/mods/helmod_2.2.14.zip'
```

## 性能与数值

当前机器一次完整验收运行：1000 条候选 0.015 秒；10 个自供电甲醇区块 0.685 秒；两块工厂求解并出图 0.118 秒。计时包含候选建立和报告，数据加载在计时外；不是不同硬件的性能保证。

两块工厂完整图 60,518 bytes；10 MW 发电图 15,415 bytes；真实能源平衡最大误差 1.14e-13 MW。另有 100 条实际选中产线图 <200 KB 的自动断言。

## 可观察的交付物

运行 `uv run python scripts/validation/planning_examples.py`，输出 data/reports/helmod-gaps/：

- factory.html / factory.json：甲醇块 + 自动链接公用工程块；甲醇 10/s，自供电与热。
- power.html / power.json：10 MW 发电规划。
- metrics.json：本次计时、图体积及平衡误差。

浏览器验收：Chrome headless 实际加载；切换 methanol 区块；点击 energy:electric 节点，确认区块间 7.03919 MW 同量流入流出；产线表搜索 pressure-methanol，显示 1.25 台、0.5 MW；区块引用表正确显示 7.03919 MW 电与 6.60892 MW 热；放大改变画布尺寸，打开 power.json 成功切换生产方案；浏览器未报告脚本异常；完整截图保存在 factory-preview.png。

## 模型边界

Nullius 涡轮内部能量已按开式/闭式及等级隔离。生成器是同一实体的内部辅助原型，产线/总量中的机器数代表求解容量，不能将涡轮行和生成器行当作两类要购买的建筑。界面标为“容量取整”，并在假设中提示。地热假定可选地块，太阳能集热器使用满流量近似，风/光平均系数是输入假设。

当前本机安装包是 Nullius 2.0.11，已有导出中确有带温度的候选；原 spec 关于 2.0.10 全无温度的描述不用于跳过温度建模。温度键在明细中保留，调用方应使用返回的完整物料键引用不同温度的流体。

蓄电池昼夜调度、物流与布局、品质和腐坏仍在原规格的非目标范围内。
