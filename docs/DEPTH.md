# 深度登记 航运舱单拆并箱与重量守恒

规模：中型。使用者：货代单证员。核心规则：主提单与分提单拆并形成有向无环关系，件数和净毛重逐层守恒。

## 功能模块

| 模块 | 使用者动作 | 入口 | 核心文件 | 测试 |
|---|---|---|---|---|
| M1 舱单采信 | 执行并复核舱单采信 | `POST /api/intake` | `app/modules/intake/rules.py` | `tests/test_intake.py` |
| M2 包装映射 | 执行并复核包装映射 | `POST /api/packaging` | `app/modules/packaging/rules.py` | `tests/test_packaging.py` |
| M3 拆并编组 | 执行并复核拆并编组 | `POST /api/grouping` | `app/modules/grouping/rules.py` | `tests/test_grouping.py` |
| M4 危险品检查 | 执行并复核危险品检查 | `POST /api/hazard` | `app/modules/hazard/rules.py` | `tests/test_hazard.py` |
| M5 净毛重核验 | 执行并复核净毛重核验 | `POST /api/weight` | `app/modules/weight/rules.py` | `tests/test_weight.py` |
| M6 报关对账 | 执行并复核报关对账 | `POST /api/reconcile` | `app/modules/reconcile/rules.py` | `tests/test_reconcile.py` |
| M7 箱门重量交接 | 逐箱判断能否交接、写明证据与阻断、切换口径并回退 | `POST /api/handover`、`POST /api/runs/{run_id}/switch` | `app/modules/handover/rules.py` | `tests/test_handover.py`、`tests/test_handover_api.py` |
| M8 版本封存 | 执行并复核版本封存 | `POST /api/archive` | `app/modules/archive/rules.py` | `tests/test_archive.py` |

## 陷阱登记

| TRAP | 现象 | 朴素实现 | 本项目处理 | 处理位置 | 测试 |
|---|---|---|---|---|---|
| TRAP-01 | 旧编码回退 | 静默错算或证据丢失 | 以可复核规则显式处理 | `app/modules/intake/rules.py` | `tests/test_intake.py::test_legacy_source` |
| TRAP-02 | BOM表头 | 静默错算或证据丢失 | 以可复核规则显式处理 | `app/modules/intake/rules.py` | `tests/test_intake.py::test_bom_source` |
| TRAP-04 | 分段表头 | 静默错算或证据丢失 | 以可复核规则显式处理 | `app/modules/intake/rules.py` | `tests/test_intake.py::test_segment_header` |
| TRAP-05 | 缺列隔离 | 静默错算或证据丢失 | 以可复核规则显式处理 | `app/modules/intake/rules.py` | `tests/test_intake.py::test_column_isolation` |
| TRAP-14 | 编号冲突 | 静默错算或证据丢失 | 以可复核规则显式处理 | `app/modules/intake/rules.py` | `tests/test_intake.py::test_conflict_all_isolated` |
| TRAP-18 | 空态三分 | 静默错算或证据丢失 | 以可复核规则显式处理 | `app/modules/intake/rules.py` | `tests/test_intake.py::test_three_empty_states` |
| TRAP-15 | 吨与包装换算 | 静默错算或证据丢失 | 以可复核规则显式处理 | `app/modules/packaging/rules.py` | `tests/test_packaging.py::test_pallet_real` |
| TRAP-07 | 空值哨兵 | 静默错算或证据丢失 | 以可复核规则显式处理 | `app/modules/packaging/rules.py` | `tests/test_packaging.py::test_sentinel_real` |
| TRAP-06 | 全角千分位 | 静默错算或证据丢失 | 以可复核规则显式处理 | `app/modules/packaging/rules.py` | `tests/test_packaging.py::test_fullwidth_decimal` |
| TRAP-20 | 拆并依赖环 | 静默错算或证据丢失 | 以可复核规则显式处理 | `app/modules/grouping/rules.py` | `tests/test_grouping.py::test_cycle_real` |
| TRAP-21 | 缺失父级 | 静默错算或证据丢失 | 以可复核规则显式处理 | `app/modules/grouping/rules.py` | `tests/test_grouping.py::test_missing_parent_real` |
| TRAP-30 | 整箱毛重当货重 | 直接用码头整箱毛重覆盖申报货重，皮重被忽略 | 货毛重=整箱毛重-皮重，缺皮重即阻断 | `app/modules/handover/rules.py` | `tests/test_handover.py::test_terminal_gross_minus_tare` |
| TRAP-31 | 称重覆盖申报封签 | 以称重时读到的封签改写原封签 | 两条封签并列，原封签始终取封签台账 | `app/modules/handover/rules.py` | `tests/test_handover.py::test_seal_ledger_wins` |
| TRAP-32 | 有差异默认改口径 | 见称重值就改成称重口径 | 未说明差异时保留原申报并阻断 | `app/modules/handover/rules.py` | `tests/test_handover.py::test_dirty_delta_keeps_declared` |
| TRAP-33 | 拆并后重复计数 | 祖先与叶节点相加或跨箱重复 | 只取可达叶节点，每叶恰落一箱，另报既守恒又对账 | `app/modules/handover/rules.py` | `tests/test_handover.py::test_internal_nodes_not_double_counted` |
| TRAP-34 | 船期排序 | 无称重箱排前占位 | 先有称重且无阻断，再有称重待说明，最后待称重 | `app/modules/handover/rules.py` | `tests/test_handover.py::test_weighing_sequence_ship_first` |
| TRAP-35 | 重试新增版本 | 每次提交都追加版本 | 同一 instruction 摘要幂等复用 | `app/store.py` | `tests/test_handover_api.py::test_same_instruction_idempotent` |
| TRAP-36 | 切换失败留半状态 | 先写后校验，失败残留 | 事务外重算校验，通过才在同一事务写指针与版本 | `app/main.py`、`app/store.py` | `tests/test_handover_api.py::test_switch_reject_leaves_old_version` |
| TRAP-37 | 回退丢原封签 | 回退只换摘要或删旧版 | 版本不删，封签随不可变快照保留 | `app/store.py` | `tests/test_handover_api.py::test_failed_switch_keeps_original_seal` |

## 对账规则

原始逻辑输入=采纳行+隔离行+空行+重复表头+同值重复。业务计算按 DATA-DICTIONARY.md 分别核验，不能把上级与下级重复加总。所有隔离保留编号、原值、原始行号和稳定原因码。

## 已知不覆盖

数据来自仓内样例或用户粘贴文件，不连接外部业务生产系统。平台不执行实际资金划转或行政审批。
