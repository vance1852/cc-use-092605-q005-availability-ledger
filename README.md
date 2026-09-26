# 实现机组可用率权益账本基础平台

本项目是一套可离线运行的 Python 服务端平台，用于管理海上风电场、送出通道、机组资源批次、场站申报、功率分配、调度情景与机组健康准入。业务状态、登录权限、幂等结果和审计事件保存在 SQLite 中，适合生产调度、设备质量、风险与审计人员在单个 Linux 应用容器内协作。

## 目录

- `src/wind_dispatch/`：场站、送出通道、机组可用量、功率申报、日前分配、调度情景与可用率权益账本；
- `src/turbine_health/`：机组健康协议、测点导入、异常复核、分析任务租约和健康决定；
- `src/grid_qualification/`：并网机组批次、检测数据、分析、账号登录与质量审批；
- `fixtures/`：离线验收使用的检测协议和结构化测点；
- `tests/`：核心规则、权限、错误边界、事务、API 和命令行验收测试。

## 可用率权益账本

`wind_dispatch.entitlements.EntitlementService`（`SupplyService` 的子类）在同一 SQLite 库上提供按场站与机组批次分户的权益流水：

- **额度来源**：保障性电量 `GUARANTEED_VOLUME`、检修免责 `MAINTENANCE_EXEMPT`、容量补偿 `CAPACITY_COMPENSATION`，各带适用时段与到期规则；超额复核批准后生成可追溯的 `OVERAGE_EXCEPTION`。
- **流水动作**：`GRANT / HOLD / WRITE_OFF / RETURN / RELEASE / EXPIRE` 全部入 `entitlement_entries`，与哈希链审计事件一一对应，任一余额都能追到形成它的业务动作。
- **跨年送出计划**：按可注入的结算时钟（UTC 月边界）拆成周期段，电量按各段时长比例分摊、舍入差额并入末段；通道按日预留，已被占用的额度不会出现在另一份计划的可选余额里。
- **同事务确认**：确认送出计划时，额度选择（到期早者优先）与通道容量预留必须在同一 `BEGIN IMMEDIATE` 事务内完成，任一不足整体回滚。
- **核销与返还**：实际电量按预占逐笔核销；取消或执行失败只退回尚未形成实际电量的部分（`RETURN`），已核销部分保留。
- **限时超额复核**：额度不足的计划进入 24 小时限时复核，超时自动解除预占；提交人不能审批自己的例外，批准生成限定在该周期段的例外额度并补齐预留。
- **结算周期冻结**：经营人员可关闭结算周期，关闭后新额度与新计划不能落入该周期，新核算规则只作用于未结算周期。
- **角色视图**：`GET /role-view` 按场站人员（dispatcher）、经营人员（planner/risk）、审计人员（auditor）返回与职责相符的汇总与明细。

主要接口：

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/entitlements` | 登记保障性/检修/容量额度 |
| GET | `/entitlements?facility_id=...` | 分户余额汇总 |
| GET | `/entitlements/{id}` | 单笔额度余额与全部流水（可追溯） |
| POST | `/plans` | 创建送出计划（按结算时钟自动拆周期，幂等） |
| POST | `/plans/{id}/confirm` | 同事务完成额度选择与通道预留 |
| POST | `/plans/{id}/overage` | 提交限时超额复核 |
| POST | `/reviews/{id}/decision` | 经营/风险审批（提交人回避，限时有效） |
| GET | `/reviews/pending` | 待复核队列 |
| POST | `/plans/{id}/deliveries` | 登记实际电量并核销 |
| POST | `/plans/{id}/cancel` `/fail` | 取消/执行失败，只返还未核销部分 |
| POST | `/settlement-periods/{period}/close` | 关闭结算周期 |
| GET | `/plans/{id}` | 计划明细：周期段、预占、通道预留、复核、审计事件 |
| GET | `/role-view?facility_id=...` | 按角色的汇总视图 |


## 环境

- Linux
- Python 3.11 或更高版本
- 运行时仅使用 Python 标准库和 SQLite

## 测试

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

## 构建检查

```bash
python3 -m compileall -q src tests
```

## 离线验收

```bash
PYTHONPATH=src python3 -m wind_dispatch.acceptance --workspace .
PYTHONPATH=src python3 -m turbine_health.acceptance --workspace .
PYTHONPATH=src python3 -m grid_qualification.acceptance
```

三条命令使用临时 SQLite 数据库完成场站与通道登记、功率申报分配、健康测点分析和并网审批，不访问外部网络。

## HTTP 服务

```bash
PYTHONPATH=src python3 -m wind_dispatch.api --database wind.sqlite3 --host 127.0.0.1 --port 8080
PYTHONPATH=src python3 -m turbine_health.api --database health.sqlite3 --host 127.0.0.1 --port 8081
PYTHONPATH=src python3 -m grid_qualification.api --database grid.sqlite3 --host 127.0.0.1 --port 8082
```

服务均提供 `GET /health`，其余接口使用 JSON。账号登录和角色权限由服务端校验，进程重启后可以继续查询 SQLite 中的业务状态与审计历史。
