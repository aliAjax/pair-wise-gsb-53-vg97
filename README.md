# 移民案件期限与材料管理

纯Python标准库实现的移民案件期限与材料管理原型，使用SQLite持久化，HTTP接口由`http.server`提供。

## 模块结构

- `app.py`：命令行参数、依赖组装和服务启动。
- `src/domain.py`：领域数据类型、错误和基础校验。
- `src/rules.py`：状态转换、法定天数、补件期限、材料完整性、司法暂缓停表与羁押复核规则。
- `src/repository.py`：SQLite建表、事务和查询。
- `src/service.py`：用例编排、权限检查、乐观并发、命令幂等和审计。
- `src/http_api.py`：HTTP路由与统一错误响应。
- `src/audit.py`：事件时间线。
- `static/index.html`：最小演示页面。
- `tests/`：完整流程、规则计算和失败场景测试。

## 启动

```bash
python3 app.py --db ./data.db --port 8329
```

默认端口为`8329`，默认数据库位于项目目录。服务启动时自动建表。

## 主要接口

- `GET /health`：健康检查。
- `GET /`：演示页面。
- `GET /api/records`：记录列表，可带`state`和`limit`参数。
- `GET /api/records/{id}`：记录详情。
- `GET /api/records/{id}/audit`：审计时间线。
- `GET /api/records/{id}/basis`：续作依据，汇总两套期限、停表状态、命令对账记录和复核队列。
- `GET /api/stats`：状态统计。
- `POST /api/records`：创建记录，请求体为`{"reference":"...","data":{...}}`。
- `POST /api/records/{id}/actions/{action}`：执行业务动作，请求体为`{"expected_version":1,"data":{...}}`。

除`/health`和`/`外，请求需提供`X-User-Id`、`X-Role`，可选`X-Org`。

## 司法暂缓与两套期限

案件同时维护办事处办案期限（`office_deadline_day`）和羁押复核期限（`custody_review_due_day`），两者均为基准日加累计停表天数（`stayed_days`）。法院暂缓命令通过`register_order`动作登记：

```json
{"expected_version": 1, "data": {"order_no": "ORD-1", "seq": 1, "kind": "issue", "start_day": 105, "end_day": 115, "issued_day": 105}}
```

- `kind`为`issue`/`update`/`revoke`；`seq`为命令序号，用于对账。
- 同一案件同一`order_no`只收一次（数据库唯一约束兜底）；保存失败后按同一命令号重试会幂等返回现状，期限不会再次延长。
- 序号不高于当前命令的迟到旧命令记为`divergent`留作差异，不改写当前停表区间；只有较新命令（`applied`）才改写区间并顺延两套期限。`revoke`把当前区间结账计入累计停表，之后的暂缓重新起算并累加。
- 命令生效后，尚未决定的复核立即失效（`superseded`）并按新期限重算一条；已作出的决定保留原依据（`basis`）不变，同时追加一条新复核。复核决定通过`decide_review`动作登记。
- 两名经办并发办理同一案件时，先落库的版本推进；另一方收到`409`版本冲突提示，演示页面会保留其当前填写内容，刷新版本后可原样重试。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

测试覆盖完整流程、规则计算、重复引用、权限拒绝、版本冲突、命令幂等重试、迟到命令差异、区间改写、撤销入账和复核失效重算。
