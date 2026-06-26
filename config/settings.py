from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # LLM（litellm 格式，如 deepseek/deepseek-chat、claude-sonnet-4-6）
    LLM_MODEL: str = "deepseek/deepseek-chat"

    # API Keys（litellm 从环境变量自动读取，无需手动传入）
    DEEPSEEK_API_KEY: str = ""
    ANTHROPIC_API_KEY: str = ""
    OPENAI_API_KEY: str = ""
    OPENAI_API_BASE: str = ""          # 自定义 OpenAI 兼容接口地址（如智谱）

    # Claude Code subprocess 模式（Worker 用 claude CLI + 智谱 GLM）
    ANTHROPIC_BASE_URL: str = ""          # 留空则用 litellm；填智谱则走 GLM
    GLM_API_KEY: str = ""                 # 智谱 API Key（映射到 ANTHROPIC_AUTH_TOKEN）
    CLAUDE_CODE_MODEL: str = "glm-4.7"   # claude settings.json 写入的模型名
    CLAUDE_CODE_TIMEOUT: int = 300        # subprocess 超时秒数

    # Codex CLI subprocess 模式
    # CODEX_MODEL 默认留空：切换到 codex 后端前必须在 .env 中显式指定
    # 可用的 OpenAI/Codex 模型（如 "o4-mini"），避免用 GLM 模型名去调用 OpenAI API。
    CODEX_MODEL: str = ""                # -m 参数传给 codex exec；切 codex 前必须填
    CODEX_TIMEOUT: int = 180             # subprocess 超时秒数

    # Reviewer Router（8B/8C）
    # DRY_RUN=False（默认）：不调用 AgentRouter，行为与 8A 之前完全一致。
    # DRY_RUN=True：Router 被调用，推荐结果仅记录日志，真实 backend 不变。
    ENABLE_REVIEW_ROUTER_DRY_RUN: bool = False

    # STATS=False（默认）：Router 调用时 failure_categories 传空列表，不读取 JSONL。
    # STATS=True：ReviewAgent 在调用 Router 前读取 EXECUTION_LOG_PATH，
    #   注入最近 REVIEW_ROUTER_STATS_RECENT_LIMIT 个 run 的错误分类。
    #   读取失败安全回退 []，不影响 review。
    ENABLE_REVIEW_ROUTER_STATS: bool = False
    REVIEW_ROUTER_STATS_RECENT_LIMIT: int = 10

    # ACTIVE=False（默认）：Router 推荐结果不影响真实 backend 选择。
    # ACTIVE=True：Router 推荐的 selected_backend 作为真实 Reviewer 执行 backend。
    #   若 Router 异常或返回非法 backend，安全回退到 REVIEW_AGENT_BACKEND。
    #   ACTIVE 优先级高于 DRY_RUN；开启 ACTIVE 会隐式调用 Router（无需另设 DRY_RUN）。
    ENABLE_REVIEW_ROUTER_ACTIVE: bool = False

    # Reviewer 后端选择："codex" | "claude-code"
    # 默认 "claude-code"（Claude Code CLI + GLM，稳定可用）。
    # 切换为 "codex" 需：① 有效 OPENAI_API_KEY；② CODEX_MODEL 填写 OpenAI 可用模型。
    # Codex CLI 使用自己的 OpenAI/Codex 认证体系，与自定义网关的兼容性因版本而异。
    REVIEW_AGENT_BACKEND: str = "claude-code"

    # Pre-publish quality gate（lint / typecheck）
    # 留空时跳过对应检查；失败时若 QUALITY_GATE_WARN_ONLY=True 则只记录日志不阻断。
    LINT_COMMAND: str = ""
    TYPECHECK_COMMAND: str = ""
    QUALITY_GATE_WARN_ONLY: bool = False

    # Integrator 可选集成测试命令
    # 留空（默认）时 IntegratorAgent 跳过测试，不改变现有行为。
    # 非空时在 ctx.workspace_path 下运行该 shell 命令；失败则 pipeline 提前终止。
    # 示例：INTEGRATION_TEST_COMMAND=pytest tests/unit
    INTEGRATION_TEST_COMMAND: str = ""
    INTEGRATION_TEST_TIMEOUT: int = 120          # 秒；命令超时时 kill 进程

    # Worker workspace 隔离策略："clone" | "worktree"
    # 默认 "clone"（git clone --local，行为稳定，无需 git >= 2.5 worktree 支持）。
    # 切换为 "worktree" 时 WorkerWorkspaceManager 改用 git worktree add，
    # 所有 Worker 共享同一个对象库，磁盘占用更低。非法值自动 fallback 到 "clone"。
    WORKER_WORKSPACE_STRATEGY: str = "clone"

    # GitHub
    GITHUB_TOKEN: str = ""
    GITHUB_APP_ID: str = ""
    GITHUB_APP_PRIVATE_KEY_PATH: str = ""

    # 飞书
    FEISHU_APP_ID: str = ""
    FEISHU_APP_SECRET: str = ""
    FEISHU_VERIFICATION_TOKEN: str = ""
    FEISHU_ENCRYPT_KEY: str = ""

    # Pipeline 行为
    MAX_DEBUG_RETRIES: int = 5
    MAX_PARALLEL_WORKERS: int = 4
    CI_POLL_INTERVAL_SECONDS: int = 30
    CI_POLL_TIMEOUT_SECONDS: int = 600

    # TypeScript 集成层地址
    TS_BRIDGE_URL: str = "http://localhost:3001"

    # 工作区
    WORKSPACE_BASE_PATH: str = "/tmp/openclaw-workspaces"
    LOG_LEVEL: str = "INFO"

    # Agent 执行数据记录（JSONL）
    # 留空（默认）时不写任何日志，不影响主流程。
    # 非空时每个 Agent 执行事件追加一行 JSON 到该文件；父目录自动创建。
    EXECUTION_LOG_PATH: str = ""


settings = Settings()
