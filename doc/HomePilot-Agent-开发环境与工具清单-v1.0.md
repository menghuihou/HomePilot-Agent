# HomePilot-Agent 开发环境与工具清单

**文档版本：** v1.0  
**更新日期：** 2026-10-23  
**适用范围：** HomePilot-Agent 当前 MVP、Private Beta、Public Beta 到 GA 阶段。  
**目标：** 把开发一个可上线的智能设备管家 Agent 所需的运行环境、依赖工具、本地服务、配置项、IDE 插件、测试与生产工具整理成一份可执行清单。

> 原则：MVP 阶段保持轻量，优先用 Docker Compose、PostgreSQL、FastMCP、LangGraph、Langfuse 打通核心闭环；Public Beta 后再逐步引入 managed database、queue、OIDC、KMS、SLO 和生产级发布工具。  
> 安全原则：所有真实密钥不进 Git；仓库只保留 `../.env.example` 和占位配置。

---

## 1. 环境分层总览

| 阶段 | 目标 | 核心环境 | 必须引入的工具 |
|---|---|---|---|
| R0：本地 MVP | 跑通诊断、巡检、控制审批、trace、评估 | 本机 Docker Compose + Python 3.14 | uv、Docker、PostgreSQL/pgvector、FastAPI、LangGraph、FastMCP、Streamlit、Langfuse、pytest |
| R1：Private Beta | 少量真实家庭可用 | 云 VPC + managed PostgreSQL + staging/prod | OIDC、Secret Manager、Alembic、备份恢复、基础告警、Home Assistant / 真实设备适配 |
| R2：Public Beta | 受控外部用户 | 多实例 API + worker + queue + SLO | managed queue、Redis/Valkey、feature flag、canary、回滚、压测、成本看板 |
| R3：GA | 公网商用 / 开放注册 | 多可用区、可扩展、可运营 | 多区域策略、DR、状态页、客服工单、渗透测试、计费配额、第三方 MCP 治理 |

## 2. 本机基础环境

### 2.1 推荐硬件与系统

| 项目 | 最低要求 | 推荐配置 |
|---|---|---|
| 操作系统 | macOS 14+ / Ubuntu 22.04+ / Windows 11 + WSL2 | macOS 15+ 或 Ubuntu 24.04 LTS |
| CPU | 8 核 | 10 核以上 |
| 内存 | 16 GB | 32 GB，便于同时跑数据库、Langfuse、模型服务和多个 MCP Server |
| 磁盘 | 100 GB SSD | 512 GB SSD，保留容器镜像、向量索引、日志和模型缓存空间 |
| 网络 | 可访问模型 API、PyPI、容器镜像仓库 | 稳定代理或企业内网镜像；能访问 Home Assistant / 厂商云测试环境 |

### 2.2 基础命令行工具

| 工具 | 用途 | 安装方式 |
|---|---|---|
| Git | 版本控制 | `brew install git` / 系统包管理器 |
| uv | Python 解释器、虚拟环境、依赖管理 | `brew install uv` 或官方安装脚本 |
| Python 3.14 | 主运行时 | `uv python install 3.14` |
| Docker Engine / Docker Desktop | 容器、PostgreSQL、Langfuse、MCP、ntfy | Docker Desktop 或 OrbStack / Colima |
| Docker Compose | 本地多服务编排 | Docker Desktop 自带或单独安装 v2 |
| psql | PostgreSQL 命令行 | `brew install libpq` 后使用 `psql`，或安装 PostgreSQL client |
| pg_dump / pg_restore | 本地备份恢复演练 | 随 PostgreSQL client 安装 |
| curl / HTTPie / Bruno | API、SSE、webhook 调试 | `brew install curl httpd httpie` 或 Bruno App |
| jq / yq | JSON / YAML 处理 | `brew install jq yq` |
| Make 或 just | 固化常用开发命令 | `brew install make` / `brew install just` |
| pre-commit | 提交前检查 | `uv tool install pre-commit` |
| gh | GitHub PR / Actions / Issue 操作 | `brew install gh` |

### 2.3 当前核心运行时版本口径

| 组件 | 版本策略 | 说明 |
|---|---|---|
| Python | 3.14，兼容 3.13 | 使用 uv 锁定；3.15 刚发布时不首发追新 |
| FastAPI | 0.15x | 当前主线；不要误写成 1.x |
| Pydantic | v2 | 配置、Router 输出、工具参数、自动化 DSL 统一结构化 |
| SQLAlchemy | 2.x async | 配合 asyncpg 使用 |
| Alembic | 当前 stable | 第 1 周启用，避免手工改表 |
| LangGraph | 1.2.x | Agent 编排主框架 |
| FastMCP | 4.x | 设备 MCP Server 与动态工具发现 |
| PostgreSQL | 18 | 关系数据、审计、情景记忆 |
| pgvector | 当前 stable，兼容 PostgreSQL 18 | 设备手册 / 故障知识 RAG |
| Mem0 | Python SDK v2.x | 长期偏好记忆，通过 `MemoryBackend` 可插拔 |
| Langfuse | v4 | trace、token 成本、审批事件、降级标签 |
| APScheduler | 3.11.x | 当前稳定调度器；4.0 仍为 pre-release |
| Streamlit | 当前 stable 1.x | MVP 前端；Public Beta 后可迁移 PWA |
| Ruff / pytest / pre-commit | 当前 stable | 格式、静态检查、测试、提交门禁 |

## 3. 核心依赖清单

### 3.1 Agent 与后端

| 依赖 | 用途 |
|---|---|
| `fastapi` | REST API、SSE、webhook、健康检查 |
| `uvicorn[standard]` | ASGI server |
| `pydantic` | 请求响应、Router 结构化输出、Automation DSL、工具参数 |
| `pydantic-settings` | 类型安全配置加载 |
| `langgraph` | 路由、诊断子图、审批 interrupt、checkpointer、事件子图 |
| `fastmcp` | 模拟设备 MCP Server、真实设备适配 MCP Server |
| `httpx` | 调用模型、Home Assistant、厂商云、ntfy、健康检查 |
| `tenacity` | 模型 / 设备 / 通知有限重试 |
| `structlog` 或标准 logging | 结构化日志 |
| `orjson` | 高性能 JSON 序列化 |
| `sse-starlette` 或 FastAPI StreamingResponse | SSE 流式响应 |
| `python-multipart` | 表单、文件导入接口 |

### 3.2 数据与持久化

| 依赖 / 服务 | 用途 |
|---|---|
| PostgreSQL 18 | 设备、家庭、事件、偏好、审批、审计、情景记忆、自动化 |
| pgvector | 设备手册、故障知识、RAG chunk 向量 |
| `sqlalchemy[asyncio]` | ORM / async data access |
| `asyncpg` | PostgreSQL async driver |
| `alembic` | schema 迁移、回滚 |
| `pgvector` Python extension | SQLAlchemy vector column 支持 |
| `psycopg[binary]` | Alembic / 运维脚本可用的同步连接 |
| Redis / Valkey | Private Beta 后用于限流、会话态、分布式锁、热缓存；MVP 可不用 |
| Object Storage | CSV / JSON 导入文件、trace 附件、知识库原始文档 |

### 3.3 Agent 记忆与模型

| 依赖 / 服务 | 用途 |
|---|---|
| `mem0ai` | 用户设备偏好、别名、长期习惯记忆 |
| Qwen3-Embedding-0.6B | 默认 embedding 模型 |
| Qwen3-Reranker-0.6B | RAG 精排 |
| bge-m3 / bge-reranker-v2-m3 | 对照基线，不作为默认 |
| 云端 LLM | 槽位抽取、诊断报告、审批说明 |
| 本地小模型 | Private Beta / GA 阶段用于隐私敏感任务降级 |
| vLLM / Ollama | 本地模型部署和结构化输出实验 |
| RAGAS | 检索侧评估 |
| DeepEval | 对话侧评估、G-Eval |

### 3.4 测试、质量与安全

| 工具 | 用途 |
|---|---|
| `pytest` | 确定性断言主框架 |
| `pytest-asyncio` | async service / MCP 测试 |
| `pytest-cov` | 覆盖率 |
| `testcontainers` | PostgreSQL、Redis、MCP 依赖集成测试 |
| `faker` / `factory-boy` | 测试数据构造 |
| `freezegun` | 时间相关巡检、自动化、通知静默测试 |
| `ruff` | lint + format |
| `mypy` 或 `pyright` | 类型检查 |
| `pre-commit` | 提交前统一检查 |
| `pip-audit` / `uv audit` | Python 依赖漏洞扫描 |
| `gitleaks` | 防止 secret 提交 |
| `semgrep` | 静态安全规则 |
| `trivy` / `grype` | 容器镜像漏洞扫描 |
| `hadolint` | Dockerfile 检查 |
| `locust` 或 `k6` | Public Beta 后压测 |
| Playwright | 前端 / PWA E2E 测试 |

## 4. 本地服务与容器

### 4.1 R0 MVP 本地服务

| 服务 | 镜像 / 形态 | 用途 |
|---|---|---|
| PostgreSQL 18 + pgvector | 官方 pgvector 镜像 | 业务库、审计库、情景记忆、pgvector |
| Langfuse v4 | 官方 Docker Compose 模板 | Agent trace、成本、审批事件 |
| OpenTelemetry Collector | 官方镜像 | 统一接收 trace / metric |
| ntfy | `binwiederhier/ntfy` | 本地主动通知与远程审批演示 |
| API | 本地 Python 服务 | FastAPI gateway |
| Device MCP Server | 本地 Python 服务 | 模拟灯光、空调、传感器、网关 |
| Worker | 本地 Python 服务 | 事件处理、巡检、自动化、通知 |
| Streamlit | 本地 Python 服务 | MVP 前端 |
| Home Assistant Container | 可选 | 真实设备适配演练 |

### 4.2 R1 / R2 增加的服务

| 服务 | 用途 | 阶段 |
|---|---|---|
| Managed PostgreSQL HA | 多可用区数据库、自动备份、PITR | Private Beta |
| OIDC Provider | Auth0、Cognito、Keycloak 或企业 IdP | Private Beta |
| Secret Manager / KMS | secret、密钥、审计 HMAC key、数据加密 key | Private Beta |
| Managed Queue / NATS / Kafka / SQS | 事件、自动化、通知异步化 | Public Beta |
| Redis / Valkey | 限流、缓存、分布式锁 | Public Beta |
| Object Storage | 文件、导入包、知识库原文 | Private Beta |
| Prometheus / Grafana 或云监控 | metrics / SLO dashboard | Private Beta |
| Loki / ELK / 云日志 | 日志聚合 | Private Beta |
| Sentry | 后端与前端错误聚合 | Public Beta |
| Status Page | 公共 / 客户状态通知 | GA |
| PagerDuty / Opsgenie | on-call 和事故升级 | GA |

## 5. IDE 与插件

### 5.1 IDE 选择

| IDE | 适合场景 | 建议 |
|---|---|---|
| VS Code / Cursor | Python、Docker、前端、Markdown、MCP 联调 | 推荐 MVP 主力 |
| PyCharm Professional | 大型 Python 项目、数据库、调试 | 适合后端深度开发 |
| Codex / Claude Desktop | Agent 代码生成、MCP Server 调试、文档协作 | 作为辅助开发环境 |

### 5.2 VS Code / Cursor 推荐插件

| 插件 | 用途 |
|---|---|
| Python | Python 运行、调试、测试 |
| Pylance | 类型补全和静态分析体验 |
| Ruff | 保存时 format + lint |
| Docker | 容器、镜像、compose 管理 |
| PostgreSQL / Database Client | 查询表、执行 SQL、查看 explain |
| YAML | schema 校验和 compose / workflow 编辑 |
| JSON Even Better | JSON / JSON Schema 编辑 |
| Mermaid Markdown | 预览架构图和状态机 |
| GitLens | 查看提交历史、 blame、变更上下文 |
| GitHub Pull Requests | PR review、issue、action 状态 |
| Error Lens | 行内错误提示 |
| Test Explorer UI | pytest 测试入口 |
| Jupyter | RAG / embedding / 评估实验 |
| EditorConfig | 统一缩进、换行、编码 |
| Todo Tree | 汇总 TODO / FIXME |
| Markdown All in One | 文档维护 |
| Code Spell Checker | 英文注释 / 文档拼写 |
| EnvFile | 读取 `.env` 调试配置 |

### 5.3 PyCharm 推荐插件

| 插件 / 能力 | 用途 |
|---|---|
| Database Tools | PostgreSQL、pgvector 查询和 schema 检查 |
| Docker | compose 服务调试 |
| Ruff | 统一 lint / format |
| EnvFile | 本地环境变量 |
| .ignore | Git 忽略规则维护 |
| Rainbow CSV | CSV / JSONL 测试数据处理 |
| Mermaid | 架构图预览 |

### 5.4 Agent / MCP 调试工具

| 工具 | 用途 |
|---|---|
| MCP Inspector | 检查 MCP Server `tools/list`、`tools/call`、错误响应 |
| FastMCP CLI | 生成、运行、调试 MCP Server |
| Langfuse UI | 查看一次完整 Agent trace |
| Bruno / HTTPie | 调试 FastAPI、SSE、webhook、审批回调 |
| Home Assistant Developer Tools | 查看设备状态、服务调用、事件 |
| pgAdmin / DBeaver / VS Code DB client | 数据模型和 SQL 排查 |

## 6. 仓库结构与关键配置

### 6.1 建议仓库结构

```text
homepilot-agent/
├─ .env.example
├─ .gitignore
├─ .pre-commit-config.yaml
├─ pyproject.toml
├─ uv.lock
├─ Makefile
├─ docker-compose.yml
├─ docker-compose.override.yml
├─ alembic.ini
├─ src/
│  ├─ app/
│  │  ├─ main.py
│  │  ├─ api/
│  │  ├─ core/
│  │  ├─ agents/
│  │  ├─ tools/
│  │  ├─ memory/
│  │  ├─ models/
│  │  └─ observability/
│  ├─ mcp_servers/
│  │  ├─ light_server.py
│  │  ├─ ac_server.py
│  │  ├─ sensor_server.py
│  │  └─ gateway_server.py
│  └─ worker/
│     ├─ event_worker.py
│     ├─ automation_worker.py
│     └─ notification_worker.py
├─ frontend/                         # MVP 用 Streamlit；Public Beta 后可放 PWA
├─ db/
│  ├─ migrations/
│  └─ seeds/
├─ eval/
│  ├─ golden/
│  ├─ judges/
│  └─ harness.py
├─ config/
│  ├─ model_providers.yaml
│  ├─ mcp_servers.yaml
│  ├─ risk_rules.yaml
│  ├─ automation_risk_rules.yaml
│  ├─ event_trigger_rules.yaml
│  └─ feature_flags.yaml
├─ deploy/
│  ├─ local/
│  ├─ staging/
│  └─ production/
└─ docs/
```

### 6.2 `../pyproject.toml` 建议骨架

```toml
[project]
name = "homepilot-agent"
version = "0.1.0"
requires-python = ">=3.13,<3.15"
dependencies = [
  "fastapi>=0.159,<1.0",
  "uvicorn[standard]>=0.37",
  "pydantic>=2,<3",
  "pydantic-settings>=2",
  "sqlalchemy[asyncio]>=2",
  "asyncpg>=0.30",
  "alembic>=1.14",
  "pgvector>=0.3",
  "langgraph>=1.2,<2",
  "fastmcp>=4,<5",
  "jieba>=0.42",
  "mem0ai>=2,<3",
  "langfuse>=4,<5",
  "opentelemetry-sdk>=1.30",
  "opentelemetry-exporter-otlp>=1.30",
  "httpx>=0.28",
  "tenacity>=9",
  "orjson>=3",
  "sse-starlette>=2",
  "apscheduler>=3.11,<4",
  "streamlit>=1.49,<2"
]

[dependency-groups]
dev = [
  "pytest>=8",
  "pytest-asyncio>=0.25",
  "pytest-cov>=6",
  "ruff>=0.13",
  "mypy>=1.15",
  "pre-commit>=4",
  "testcontainers[postgres]>=4",
  "faker>=33",
  "freezegun>=1.5"
]
eval = [
  "ragas>=0.2",
  "deepeval>=2"
]
security = [
  "pip-audit>=2",
  "gitleaks>=8",
  "semgrep>=1"
]
local-model = [
  "sentence-transformers>=3",
  "transformers>=4"
]
```

> 具体锁定的 patch 版本以 `../uv.lock` 为准。不要在文档中手动改版本后不同步安装。

## 7. 环境变量与配置

### 7.1 `../.env.example`

```dotenv
# ---------- App ----------
APP_ENV=local
LOG_LEVEL=INFO
API_BASE_URL=http://localhost:8000
CORS_ORIGINS=http://localhost:8501

# ---------- PostgreSQL ----------
POSTGRES_HOST=localhost
POSTGRES_PORT=5432
POSTGRES_DB=homepilot
POSTGRES_USER=homepilot
POSTGRES_PASSWORD=change-me
DATABASE_URL=postgresql+asyncpg://homepilot:change-me@localhost:5432/homepilot
DATABASE_URL_SYNC=postgresql+psycopg://homepilot:change-me@localhost:5432/homepilot

# ---------- Model Gateway ----------
MODEL_PROVIDER=cloud
MODEL_BASE_URL=https://api.example.com/v1
MODEL_API_KEY=
LIGHT_MODEL=qwen-light
FLAGSHIP_MODEL=qwen-flagship
EMBEDDING_MODEL=Qwen3-Embedding-0.6B
RERANKER_MODEL=Qwen3-Reranker-0.6B

# ---------- Observability ----------
LANGFUSE_HOST=http://localhost:3000
LANGFUSE_PUBLIC_KEY=
LANGFUSE_SECRET_KEY=
OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318
OTEL_SERVICE_NAME=homepilot-agent

# ---------- MCP ----------
MCP_DEVICE_SERVER_URL=http://localhost:8100
MCP_AUTH_TOKEN=
MCP_ALLOWED_TOOLS=query_device_status,set_device_power,set_temperature,restart_device

# ---------- Notification ----------
NTFY_BASE_URL=http://localhost:8200
NTFY_TOPIC_PREFIX=homepilot
NTFY_TOKEN=
APPROVAL_BASE_URL=http://localhost:8000/approvals
APPROVAL_TOKEN_TTL_SECONDS=600

# ---------- Security ----------
SESSION_SECRET=
APPROVAL_HMAC_KEY=
AUDIT_HMAC_KEY=
DATA_ENCRYPTION_KEY=

# ---------- Public Beta / GA ----------
OIDC_ISSUER=
OIDC_CLIENT_ID=
OIDC_CLIENT_SECRET=
OIDC_AUDIENCE=
QUEUE_URL=
REDIS_URL=
FEATURE_AUTOMATION=false
FEATURE_EVENT_AGENT=true
FEATURE_REMOTE_APPROVAL=true
FEATURE_LOCAL_MODEL_FALLBACK=false
```

### 7.2 配置文件职责

| 文件 | 内容 | 变更方式 |
|---|---|---|
| `.env` | 本机真实 secret | 不提交 Git |
| `../.env.example` | 所有变量名和占位值 | 提交 Git |
| `config/model_providers.yaml` | 模型路由、超时、重试、fallback、成本上限 | PR 评审 |
| `config/mcp_servers.yaml` | MCP Server 地址、allow/deny 工具、超时、鉴权方式 | PR 评审 |
| `config/risk_rules.yaml` | 控制风险等级、审批要求、禁止动作 | PR 评审 + CI 红线 |
| `config/automation_risk_rules.yaml` | 自动化规则风险边界 | PR 评审 + CI 红线 |
| `config/event_trigger_rules.yaml` | 事件触发阈值、去重、静默期 | PR 评审 |
| `config/feature_flags.yaml` | 子图、设备域、模型、通知能力开关 | PR 评审 |
| `deploy/staging/*` | staging 镜像、资源、健康检查、备份策略 | PR 评审 |
| `deploy/production/*` | 生产发布、扩缩容、DR、告警策略 | 变更管理 |

### 7.3 Secret 管理策略

| 阶段 | 方式 | 要求 |
|---|---|---|
| 本地开发 | `.env` + macOS Keychain / 1Password CLI | `.env` 不提交；定期检查 gitleaks |
| Private Beta | 云 Secret Manager / Vault / SSM | 最小权限读取；访问审计；密钥可轮换 |
| Public Beta / GA | KMS + Secret Manager | 分环境隔离；自动轮换；禁止日志输出；trace 脱敏 |

## 8. 常用本地命令

```bash
# 1. 安装基础运行时
uv python install 3.14
uv sync --all-groups

# 2. 启动本地依赖
docker compose up -d postgres langfuse ntfy otel-collector

# 3. 初始化数据库
uv run alembic upgrade head
uv run python -m db.seeds.load_devices

# 4. 启动 MCP Server
uv run uvicorn mcp_servers.light_server:app --host 0.0.0.0 --port 8100 --reload

# 5. 启动 API
uv run uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload

# 6. 启动 worker
uv run python -m worker.event_worker

# 7. 启动前端
uv run streamlit run frontend/app.py

# 8. 检查
uv run ruff check .
uv run ruff format --check .
uv run mypy src
uv run pytest -q

# 9. 评估
uv run python eval/harness.py --suite deterministic --env local
uv run python eval/harness.py --suite semantic --sample 5

# 10. 安全检查
uv run gitleaks detect --no-banner
uv run pip-audit
```

> `Makefile` 中可以把这些命令固化成 `make dev`、`make test`、`make eval`、`make security`、`make down`，避免文档命令漂移。

## 9. Git、CI / CD 与协作工具

| 工具 | 用途 |
|---|---|
| GitHub | 代码、PR、issue、Actions、release |
| GitHub Actions | lint、类型检查、pytest、评估、镜像构建、部署 |
| pre-commit | 提交前检查 |
| conventional commits | `feat / fix / build / docs / test / chore` |
| semantic-release 或 release-please | 自动 changelog / release |
| Dependabot / Renovate | 依赖更新 |
| GitHub Environments | staging / production 权限和审批 |
| Terraform / Pulumi | Private Beta 后的基础设施即代码 |
| GitHub Container Registry / ECR / GAR | 镜像仓库 |
| Argo CD / Flux | GA 阶段如果引入 Kubernetes 可使用 |

### CI pipeline 建议

```text
checkout
  → uv sync
  → ruff format --check
  → ruff check
  → mypy / pyright
  → pytest deterministic
  → eval harness sample
  → gitleaks
  → pip-audit
  → docker build
  → container scan
  → push image
  → deploy staging
  → smoke test
  → manual approval
  → deploy production
```

## 10. 分阶段工具引入原则

### MVP 不要提前引入

| 工具 | 暂缓原因 |
|---|---|
| Kubernetes | 单机 Docker Compose 已足够；过早引入增加调试成本 |
| Kafka / Pulsar | Postgres outbox + worker 可满足 MVP |
| Redis / Valkey | 单用户场景可暂缓，Public Beta 再引入 |
| GraphQL | REST + SSE 已够用 |
| React / Next.js | Streamlit 已满足 MVP 前端 |
| 微服务拆分 | 先模块化单体，边界清晰后再拆 |
| 自建评估平台 | `eval/harness.py` + golden 数据集足够起步 |

### Public Beta 必须补齐

| 能力 | 工具方向 |
|---|---|
| OIDC 登录 | Auth0 / Cognito / Keycloak / 企业 IdP |
| managed database | 云 PostgreSQL HA + PITR |
| queue | NATS / SQS / Kafka / managed RabbitMQ |
| cache / lock | Redis / Valkey |
| feature flag | Unleash / LaunchDarkly / 云配置中心 |
| metrics / alert | Prometheus + Grafana 或云监控 |
| error tracking | Sentry |
| load test | k6 / Locust |
| release safety | canary、blue-green、rollback 演练 |

### GA 必须补齐

| 能力 | 工具方向 |
|---|---|
| secret / KMS | Vault / AWS KMS / GCP KMS |
| DR | 多可用区、异地备份、定期恢复演练 |
| status page | Atlassian Statuspage / Instatus / 自建 |
| on-call | PagerDuty / Opsgenie |
| customer support | Intercom / Zendesk / 自建工单 |
| billing | Stripe / Paddle / 国内合规支付方案 |
| runtime security | WAF、SIEM、入侵检测、审计告警 |
| compliance evidence | 数据流图、DPIA、访问审计、供应商评估 |

## 11. 最小安装清单

如果只列最短路径，MVP 开发机器至少准备这些：

```bash
brew install git uv jq yq pre-commit gh
brew install --cask docker
uv python install 3.14
git config --global init.defaultBranch main
pre-commit --version
docker compose version
```

再准备四个密钥 / 环境：

1. 模型 API key。
2. PostgreSQL 本地账号密码。
3. Langfuse public / secret key。
4. ntfy topic / token。

之后即可开始 R0 MVP 开发。

## 12. 检查清单

### 本地环境检查

| 状态 | 检查项 | 通过标准 |
|---|---|---|
| ☐ | Python | `uv python find 3.14` 可用 |
| ☐ | 依赖 | `uv sync --all-groups` 成功 |
| ☐ | 容器 | `docker compose ps` 关键服务 healthy |
| ☐ | 数据库 | `alembic upgrade head` 和 downgrade 测试通过 |
| ☐ | MCP | MCP Inspector 能列出设备工具 |
| ☐ | API | `/healthz` 返回 200 |
| ☐ | Agent | 一次诊断请求可在 Langfuse 看到 trace |
| ☐ | 前端 | Streamlit 能发起查询和控制审批 |
| ☐ | 测试 | `pytest -q` 全绿 |
| ☐ | 评估 | `eval/harness.py --suite deterministic` 全绿 |
| ☐ | 安全 | `gitleaks detect` 无泄漏 |

### Private Beta 前检查

| 状态 | 检查项 | 通过标准 |
|---|---|---|
| ☐ | staging / prod | 环境分离，配置不混用 |
| ☐ | OIDC | 登录、登出、过期、refresh 测试通过 |
| ☐ | 租户 | 越权访问其他 `home_id` 全部阻断 |
| ☐ | Secret | 无明文 secret，密钥可轮换 |
| ☐ | 备份 | 可从备份恢复到空环境 |
| ☐ | Alembic | 新表迁移和回滚通过 |
| ☐ | 真实设备 | 2-3 类设备可查询、控制、状态回读 |
| ☐ | 告警 | API 错误率、延迟、worker lag、LLM 成本有告警 |
| ☐ | 审计 | 审批、拒绝、执行、后台操作完整记录 |

### GA 前检查

| 状态 | 检查项 | 通过标准 |
|---|---|---|
| ☐ | SLO | API、Agent 任务、设备控制、通知有明确目标 |
| ☐ | 压测 | 目标家庭数和设备数压测通过 |
| ☐ | DR | RPO / RTO 明确，恢复演练通过 |
| ☐ | 安全 | 渗透测试 critical / high 清零 |
| ☐ | 合规 | 隐私政策、数据导出、删除、留存策略生效 |
| ☐ | 发布 | canary / blue-green / rollback 演练通过 |
| ☐ | 运营 | status page、on-call、runbook、客服流程齐备 |
| ☐ | 成本 | token、模型、通知、存储成本有看板和预算告警 |
