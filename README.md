# xxbot-next 首发版（v1.0.0）

`xxbot-next` 是一个修仙题材的 Discord 文字 BOT。项目已经从早期“工程骨架”阶段演进到首发可用版本，核心玩法、Discord 交互入口、静态配置、数据库迁移与测试结构均已落地。

## 当前状态

- 首发主循环已基本打通
- 已提供 `/修仙` 命令组与多个面板入口
- 已具备角色成长、战斗、副本、装备、功法、斗法、榜单与恢复等核心系统
- 已具备 SQLite / PostgreSQL 数据库支持与 Alembic 迁移链路
- 仓库已包含单元测试与集成测试，覆盖主要业务模块

## 版本信息

- 项目版本：`1.0.0`
- Python 要求：`>=3.12`
- 静态配置基线：当前仓库内首发配置文件统一使用 `1.0.0` 版本标识

## 首发功能概览

### 角色与成长

- 角色创建与公开角色主面板
- 修炼、闭关、修为推进与境界进度展示
- 当前属性、角色资料与成长面板查询

### 战斗与副本

- 自动战斗核心
- 无尽副本挑战、结算与阶段推进
- 突破秘境、试炼进度、奖励账本与突破流程

### 装备、法宝与功法

- 装备 / 法宝 / 功法私有面板与装配查询
- 装备成长、阶数、命名批次与首发特殊词条骨架
- 功法掉落、生成、谱系与路径配置

### PVP 与排行

- 斗法挑战面板
- 防守快照、荣誉币结算与战报链路
- 排行榜查询与后台刷新

### 配置与基础设施

- Discord slash command 交互
- SQLAlchemy + Alembic 持久化
- 静态配置加载与校验
- 标准 `logging`、分层架构与测试体系

## Discord 命令

当前已注册以下命令入口：

| 命令 | 说明 | 默认可见性 |
| --- | --- | --- |
| `/ping` | 检查 BOT 与数据库状态 | 私有 |
| `/修仙 创建` | 创建角色并进入公开角色主面板 | 公开 |
| `/修仙 面板` | 打开公开角色主面板 | 公开 |
| `/修仙 修炼` | 打开修炼与闭关私有面板 | 私有 |
| `/修仙 无尽` | 打开无尽副本私有面板 | 私有 |
| `/修仙 突破` | 打开突破秘境私有面板 | 私有 |
| `/修仙 装备` | 打开装备 / 法宝 / 功法私有面板 | 私有 |
| `/修仙 斗法` | 打开 PVP 挑战私有面板 | 私有 |
| `/修仙 榜单` | 打开排行榜私有面板 | 私有 |
| `/修仙 恢复` | 打开恢复状态私有面板 | 私有 |

> 当前交互设计遵循“公开角色主页 + 私有功能面板”的首发策略，减少频道刷屏，同时保留公开展示与分享能力。

## 目录结构

```text
src/
  main.py
  bot/
  application/
  domain/
  infrastructure/
    config/
      static/
    db/
      migrations/
    discord/
    logging/
tests/
plans/
data/
```

## 环境要求

- Python 3.12 或更高版本
- 可用的 Discord BOT Token 与 Application ID
- 本地开发默认使用 SQLite，可按需切换 PostgreSQL

## 本地安装

1. 创建虚拟环境：

```bat
python -m venv .venv
```

2. 激活虚拟环境：

```bat
.venv\Scripts\activate
```

3. 以可编辑模式安装项目：

```bat
python -m pip install -e .[dev]
```

## 环境变量

复制 `.env.example` 为 `.env`，然后按需填写：

```bat
copy .env.example .env
```

关键变量如下：

- `DISCORD_BOT_TOKEN`：Discord BOT Token
- `DISCORD_APPLICATION_ID`：Discord 应用 ID
- `DATABASE_URL`：数据库连接地址
- `DISCORD_GUILD_ID`：开发期 guild ID，可留空
- `LOG_LEVEL`：日志级别
- `AI_NAMING_API_KEY`：批处理 AI 命名接口鉴权密钥
- `AI_NAMING_API_URL`：批处理 AI 命名 HTTP 接口地址
- `AI_NAMING_MODEL`：批处理 AI 命名使用的模型标识

### SQLite 示例

```env
DATABASE_URL=sqlite+pysqlite:///./data/app.db
```

### PostgreSQL 示例

```env
DATABASE_URL=postgresql+psycopg://postgres:postgres@127.0.0.1:5432/xxbot_next
```

说明：

- 本项目默认面向 SQLite 本地开发。
- 迁移脚本与数据库访问层保持跨数据库兼容，不依赖 PostgreSQL 专属能力。
- 如需开发期快速同步命令，建议配置 `DISCORD_GUILD_ID`。
- 仅当 `AI_NAMING_API_KEY`、`AI_NAMING_API_URL`、`AI_NAMING_MODEL` 三项同时存在时，系统才会启用批处理 AI 命名 HTTP 提供方。
- 未完整配置 AI 命名环境变量时，[`src/application/naming/batch_service.py`](src/application/naming/batch_service.py) 会继续走同步回退命名，并把命名批次标记为跳过，不阻塞无尽结算主链路。
- 当前 HTTP 提供方会把系统提示词放进请求体的 `prompt` 字段，并同时附带 `items` 列表。`prompt` 会明确要求模型按 `target_type + instance_id` 返回 JSON 结构，不要输出额外解释。
- 当前 `prompt` 的核心约束包括：修仙题材命名、基于 `fallback_name` 与上下文润色、名字尽量控制在 2~12 个中文字符、拿不准时允许返回空名并写 `error_message`、响应必须是 `{"results": [...]}`。
- 当前 HTTP 提供方约定向 `AI_NAMING_API_URL` 发送 `POST` JSON，请求体包含 `model`、`batch_id`、`character_id`、`source_type`、`source_ref`、`prompt`、`items`，响应体需返回 `{"results": [{"target_type": ..., "instance_id": ..., "generated_name": ..., "error_message": ...}]}`。

## 执行数据库迁移

升级到最新版本：

```bat
set DATABASE_URL=sqlite+pysqlite:///./data/app.db && python -m alembic upgrade head
```

查看当前版本：

```bat
set DATABASE_URL=sqlite+pysqlite:///./data/app.db && python -m alembic current
```

迁移脚本位于 `src/infrastructure/db/migrations/versions/`，当前已经覆盖首发相关的核心持久化结构，包括但不限于：

- 角色、库存、装备与战斗记录基础表结构
- 无尽副本运行态扩展
- 突破秘境进度与奖励账本扩展
- 角色评分快照与 PVP 核心结构
- 装备阶数、功法物品化、特殊词条骨架与命名批次扩展

## 运行 BOT

先确保已经执行迁移，再启动 BOT：

```bat
python -m main
```

或者使用项目脚本：

```bat
xianbot
```

启动流程会完成以下初始化：

- 读取环境配置
- 初始化标准库 `logging`
- 加载首发静态配置
- 初始化数据库引擎与会话工厂
- 构建应用服务与 Discord 控制器
- 注册并同步 Discord slash commands
- 启动排行榜后台刷新任务（如果已启用）

## 静态配置

静态配置文件位于 `src/infrastructure/config/static/files/`，目前已经拆分为多个首发配置文件：

- `realm_progression.toml`：境界与阶段推进
- `daily_cultivation.toml`、`cultivation_sources.toml`：修为来源与日修为规则
- `endless_dungeon.toml`：无尽副本结构
- `breakthrough_trials.toml`：突破秘境与奖励规则
- `equipment.toml`：装备 / 法宝 / 词条 / 阶数配置
- `pvp.toml`：斗法与奖励配置
- `battle_templates.toml`、`enemies.toml`、`base_coefficients.toml`：战斗模板、敌人与基础系数
- `skill_paths.toml`、`skill_lineages.toml`、`skill_generation.toml`、`skill_drops.toml`：功法体系配置

## 运行测试

```bat
python -m pytest
```

当前测试目录已覆盖以下主要模块：

- `battle`
- `breakthrough`
- `character`
- `dungeon`
- `equipment`
- `pvp`
- `ranking`
- `config`
- Discord 交互相关集成链路

## 技术栈

- `discord.py`
- `SQLAlchemy`
- `Alembic`
- `pydantic`
- `pydantic-settings`
- `pytest` / `pytest-asyncio`

## 当前边界与后续方向

当前版本聚焦首发玩法闭环，以下内容仍不属于本仓库现阶段的重点范围：

- Redis、消息队列、多实例分片与复杂部署编排
- 网页后台、运营后台与完整 CI/CD 发布链路
- 更多活动、社交、公会、经济系统等非首发模块
- 更高阶内容扩展、数值平衡与特殊词条的持续补完

如需继续迭代，建议优先沿现有 `application / domain / infrastructure` 分层扩展，避免把业务规则直接散落到 Discord 控制器或数据库访问层。
