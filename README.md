# 远程诊疗链路连续性编排中心

协调区域医联体远程诊疗的排班准入、链路保障、授权边界与会诊生命周期。

排班确认时一次性纳入六类准入条件：**诊疗时段、参与机构、链路探测快照、共享故障域、
最低服务等级、患者授权有效期**；确认后的会诊在条件变化时可自动/人工切换备选路径、
有限降级、暂停或取消。患者同意失效、关键操作阶段、人工锁定三类情形遵循不同规则；
敏感字段按调用方职责（`X-Role`）裁剪返回；通知通过事务发件箱与业务状态原子提交，
失败可幂等补发，进程崩溃重启不重复占用、不丢失事件。

## 分层结构

```
telemedicine_continuity/
├── domain/                 # 纯领域层，无框架/持久化依赖
│   ├── enums.py            # 服务等级、健康度、会诊状态、职责等枚举
│   ├── models.py           # 机构/医生/患者/链路/授权/时段/会诊/发件箱事件
│   ├── admission.py        # 准入判断：授权窗口、快照时效、等级、故障域互斥择优
│   ├── policy.py           # 条件变化反应策略：切换/降级/暂停/恢复/取消/延后
│   └── projection.py       # 字段级安全投影（按角色裁剪敏感字段）
├── application/
│   ├── service.py          # 编排服务：事务边界、状态机、职责矩阵、幂等
│   └── notification.py     # 事务发件箱中继：pending→sending→sent，崩溃回收
├── infra/
│   ├── clock.py            # 可替换时钟（SystemClock / 测试可控 MutableClock）
│   └── db.py               # SQLite(WAL) 持久化、BEGIN IMMEDIATE 串行写、乐观锁
├── httpapi/server.py       # 标准库 ThreadingHTTPServer 接口（零第三方依赖）
└── main.py                 # 启动入口
```

## 关键业务规则

### 排班准入（全部同时满足）

- 患者同意状态为 granted，且有效期完整覆盖诊疗时段（不只是“当前有效”）；
- 主备链路探测快照不超过 5 分钟，健康度 up/degraded，实际等级（劣化降 1 级）
  不低于最低服务等级；
- 链路覆盖全部参与机构；
- `required_link_count` 条链路必须分布在**不同故障域**——两条同域链路即使都健康，
  也以 `shared_fault_domain` 拒绝，杜绝“主备共享同一故障域”；
- 同一医生重叠时段排他占用；准入失败事务回滚，不留下任何占用。

### 条件变化反应

| 情形 | 正常阶段 | 关键操作阶段 | 人工锁定 |
|---|---|---|---|
| 患者同意失效（撤销/到期/不覆盖） | 立即取消并释放时段 | 登记 `cancel_on_stage_exit`，阶段结束复查后落地 | 自动联动跳过，仅锁定者可处置 |
| 主链路故障且有跨域备选 | 自动切换 | 登记延迟效果，阶段结束按最新条件落地 | 同上 |
| 全部链路不可用 | 暂停，恢复后自动恢复 | 延后，阶段结束复查 | 跳过 |
| 链路连通但低于 SLA | 有达标备选则切换，否则有限降级继续 | 仅允许有限降级 | 跳过 |
| 打断性人工操作（切换/暂停/取消） | 允许 | 一律拒绝（`critical_phase`） | 仅锁定者 |

重复的坏状态探测不会产生重复通知；主链路健康时的环境变化只静默刷新备选指针。

### 字段级权限

- `coordinator`（排班员）：可见患者姓名，不可见证件号/电话；可见锁定操作者；
- `link_engineer`（链路工程师）：患者标识脱敏（`****尾号`），不可见授权明细与锁定者；
- `clinician`（医生）：可见完整患者信息，可标记关键阶段/有限降级/完成，不可取消；
- `auditor`（审计员）：只读，可见全量字段、发件箱与审计日志。

### 一致性保证

- 每个业务方法一个 `BEGIN IMMEDIATE` 事务：会诊状态、时段占用、审计日志、
  发件箱事件同事务提交（事务发件箱模式）；
- 排班支持 `Idempotency-Key`，重放返回同一会诊；发件箱以 `event_key` 去重；
- 通知中继 claim 为 `sending` 后在事务外发送，失败退回 `pending` 并记录错误，
  可反复补发；接收方按 `event_id` 幂等去重（至少一次投递，幂等接收）；
- 进程在 `sending` 期间崩溃，重启启动时自动回收为 `pending`；
- 聚合持久化带 `version` 乐观锁；跨线程/跨进程写由 SQLite WAL + busy_timeout 串行化。

## 运行

```bash
# 启动服务（默认 var/orchestrator.db，运行数据不入源码目录）
python3 -m telemedicine_continuity.main --port 8080

# 本地接口端到端演示（自动起服务、发真实 HTTP 请求）
python3 scripts/demo_local.py
```

所有写接口需带 `X-Role`（coordinator/link_engineer/clinician/auditor），
人工操作可用 `X-Actor-Id` 标识操作者，`Idempotency-Key` 标识幂等请求。主要接口：

- `POST /api/orgs|clinicians|patients|links|slots|consents` 基础数据
- `POST /api/links/{id}/snapshots` 链路探测快照（自动联动受影响会诊）
- `POST /api/consents/{patientId}/revoke` 撤销授权（自动联动）
- `POST /api/consultations` 排班准入（`min_grade`、`required_link_count`）
- `POST /api/consultations/{id}/switch|degrade|pause|resume|cancel|complete`
- `POST /api/consultations/{id}/phase` 关键操作阶段标记（`{"critical": true|false}`）
- `POST /api/consultations/{id}/lock|unlock` 人工锁定/解锁
- `POST /api/outbox/deliver` 发件箱补发；`GET /api/outbox`、`GET /api/audit`（审计员）

## 测试

```bash
python3 -m unittest discover -s tests -v
python3 -m compileall -q telemedicine_continuity tests scripts
```

| 测试文件 | 证明内容 |
|---|---|
| `test_admission.py` | 12 条准入纯规则：共享故障域、快照失鲜/缺失、授权撤销/到期/不覆盖、劣化降级、机构覆盖等 |
| `test_concurrent_booking.py` | 8 线程争抢同一时段仅 1 单成功；同医生重叠时段互斥；幂等键重放不重复建单 |
| `test_lifecycle.py` | 授权撤销（立即取消/关键阶段延后/阶段内续期清除）、故障域切换、全断暂停与恢复、关键阶段规则、锁定规则、自然到期、幂等取消、职责矩阵 |
| `test_security.py` | 四类角色的字段裁剪与接口准入 |
| `test_outbox.py` | 原子提交、失败幂等补发、崩溃回收、重启不重复占用/事件 |
| `test_http_api.py` | 真实线程 HTTP 服务器上的全流程与 6 线程并发排班 |
| `test_restart_subprocess.py` | 真实子进程 `kill -9` 后同库重启：占用/会诊完好、重复排班冲突、待发通知只投递一次、`sending` 残留回收 |
