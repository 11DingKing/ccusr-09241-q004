# 远程诊疗链路连续性编排中心

协调远程诊疗排班、链路保障和授权边界。把诊疗时段、参与机构、链路探测快照、共享故障域风险、最低服务等级和授权有效期一起纳入准入判断；会诊确认后随条件变化自动切换备选路径、有限降级、暂停或取消，并对患者同意失效、关键操作阶段、人工锁定遵循差异化规则。

## 架构

```
telemedicine_continuity/
├── domain/            # 领域层：枚举、模型、错误、准入与质量策略（纯函数）
├── application/       # 应用层：编排服务、发件箱调度、恢复流程、可替换端口
├── infrastructure/    # 持久化适配：SQLite（事务、唯一约束、乐观版本）
├── interfaces/        # 接口层：HTTP API、按职责过滤的字段视图
└── app.py             # 组合根：装配并执行启动恢复
```

工程约定：领域模型、应用服务、持久化适配和接口层保持边界清晰；时间、标识生成及外部观测（通知发送）均通过可替换端口接入，便于稳定复现业务过程。运行数据不写入源码目录。

## 核心规则

**准入判断**（`domain/policies.py`）：以下任一不满足即拒绝并返回全部原因——

- 患者同意与机构授权均有效且有效期完整覆盖诊疗时段；
- 主/备链路探测快照在新鲜度窗口（5 分钟）内且满足最低服务等级底线；
- GOLD/SILVER 要求主备链路分属不同故障域；BRONZE 允许共享但记录共享风险警示；
- 医生/设备在目标时段无重叠占用（串行化事务内检查并写入，构成原子临界区）。

**条件变化重估**（`application/orchestrator.py`）：

- 患者同意失效：无条件强制取消，凌驾于人工锁定与关键操作阶段之上；
- 人工锁定：冻结一切自动变迁，仅发出人工复核通知，解锁后立即补做重估；
- 关键操作阶段：仅允许满足完整服务等级的无缝切换；禁止降级/暂停/取消，无法满足时升级告警；
- 常规链路劣化：优先切换备选路径 → 有限降级（最多降一档）→ 暂停等待恢复；链路恢复后自动复原；
- 机构授权失效：暂停而非取消，授权恢复后自动复原。

**一致性与幂等**：

- 会诊状态变迁与通知发件箱事件在同一事务提交；
- 发件箱调度失败标记 FAILED 可补发，通知端口按 event_id 幂等，重复投递不产生重复副作用；
- 排班接口要求幂等键，唯一约束兜底，重放返回原结果；
- 资源占用持久化，进程退出再启动后仍然有效；启动恢复清理崩溃残留占用、过期锁定，重估停机期间的授权失效。

**字段级安全**（`interfaces/views.py`）：患者姓名/病历号、同意书载荷仅 clinician/admin 可见；链路探测指标与故障域仅 ops/admin 可见；auditor 看到脱敏占位；scheduler 仅获得排班所需最小字段集。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

覆盖：并发排班（多线程抢占同一时段）、幂等重放、授权撤销（同意失效强制取消 / 机构授权暂停恢复）、故障域联动（切换/降级/暂停/恢复）、人工锁定与关键阶段规则、发件箱一致提交与幂等补发、进程重启恢复、字段级过滤。

## 本地接口演示

```bash
python3 scripts/demo_api.py
```

启动真实 HTTP 服务并依次演示：并发排班 → 主链路劣化自动切换 → 双故障域暂停 → 链路恢复 → 锁定+关键阶段下撤销同意强制取消 → 进程重启后占用不重复、幂等键收敛、发件箱补发不重复。

## 接口概览

| 方法 | 路径 | 职责 | 说明 |
| --- | --- | --- | --- |
| POST | `/v1/institutions` | ops/admin | 注册机构 |
| POST | `/v1/links` | ops/admin | 注册链路（含故障域） |
| POST | `/v1/links/{id}/probes` | ops/admin | 写入探测快照并触发重估 |
| POST | `/v1/authorizations` | 各写角色 | 授予授权/患者同意 |
| POST | `/v1/authorizations/{id}/revoke` | clinician/admin | 撤销授权 |
| POST | `/v1/consultations` | scheduler/admin | 排班（需 `Idempotency-Key` 头） |
| GET | `/v1/consultations/{id}` | 各角色 | 按职责过滤的会诊视图 |
| POST | `/v1/consultations/{id}/actions` | 各写角色 | start/set_phase/lock/unlock/switch_path/degrade/pause/resume/cancel/complete |
| POST | `/v1/consultations/{id}/evaluate` | ops/admin | 手动触发重估 |
| GET | `/v1/outbox/events` | ops/admin | 发件箱事件列表 |
| POST | `/v1/outbox/dispatch` | ops/admin | 派发待发通知 |
| POST | `/v1/admin/recover` | admin | 手动执行恢复流程 |

所有接口要求 `X-Role` 头（scheduler/clinician/ops/auditor/admin），缺失返回 401，越权返回 403。

## 编译检查

```bash
python3 -m compileall -q telemedicine_continuity tests
```
