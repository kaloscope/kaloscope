# AI 助手与 MCP 架构评审及分步精简方案

> 评审基准：`main` (`v1.2.0` / `5ca35125`)
> 目标：保持当前主要功能、权限边界、客户端协议与可见 UI 行为不变，通过小步、低风险、可独立验证的演进，逐步消除 AI 助手与 MCP 层的结构性冗余与过重实现。
> 第一至三节记录 Step 1–5 当时的基线和验收范围，后续产品决定见第四节。

---

## 一、体量与根因总览

### 1.1 代码规模盘点

仅统计 AI 助手与 MCP 相关核心模块（不含 `download.py`、`media.py`、`media_changes.py`、`media_batches.py`、`flow.py` 等领域服务本身），当前实现已达 **~2.5 万行**，对应测试约 **2.5 万行**：

| 区域                       | 代表文件                                                                                                                                                                                                                                                                                                                                                                                                                                                   |        行数 |
| -------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------: |
| 后端 `core/ai`             | `tools.py` (3,039)、`history.py` (1,073)、`queries.py` (961)、`documents.py` (542)、`completion.py` (224)、`discovery.py` (119)、`presets.py` (50)                                                                                                                                                                                                                                                                                                         |       6,009 |
| 后端助手 / 审批 / 订阅服务 | `assistant.py` (1,822)、`subscription_intent.py` (916)、`assistant_sessions.py` (868)、`workflow_authoring.py` (765)、`agent_actions.py` (618)、`assistant_proposals.py` (460)、`assistant_questions.py` (409)、`subscription_choices.py` (396)、`plan_replacement.py` (385)、`assistant_reviews.py` (358)、`subscription_destinations.py` (257)、`ai.py` (255)、`download_diagnostics.py` (236)、`assistant_plan_review.py` (201)、`mcp.py` service (152) |       7,498 |
| 后端 MCP 服务与模型        | `mcp.py` (2,603)、`models/mcp.py` (1,034)、`routes/mcp.py` (108)、`core/dl/access.py` (65)                                                                                                                                                                                                                                                                                                                                                                 |       3,810 |
| 后端助手模型与路由         | `models/assistant.py` (595)、`routes/assistant.py` (547)、`models/ai.py` (210)、`models/workflow_authoring.py` (152)、`models/review_decision.py` (132)、`models/agent_actions.py` (90)、`models/assistant_questions.py` (79)                                                                                                                                                                                                                              |       1,805 |
| 前端 `lib/assistant`       | `session.ts` (1,235)、`canvas.ts` (556)、`reviews.ts` (290)、`markdown.ts` (160)、`stream.ts` (124)、`review-card.svelte.ts` (110)、`navigation.ts` (97)、`context.ts` (87)、`device.ts` (36)、`errors.ts` (21)                                                                                                                                                                                                                                            |       2,716 |
| 前端助手组件               | `AssistantPanel.svelte` (1,070)、`AssistantHistory.svelte` (340)、`AssistantReviewCard.svelte` (323)、`AssistantQuestions.svelte` (319)、`AssistantTool.svelte` (314)、`AssistantMarkdown.svelte` (142)、`PlanReplacementCard.svelte` (135)、`AdminReviewCard.svelte` (108)、`PlanReviewCard.svelte` (101)、`AssistantChanges.svelte` (89)、`AssistantActivity.svelte` (71)、`FirstCheckResult.svelte` (58)                                                |       3,070 |
| 前端 MCP 页面与类型        | `routes/(app)/settings/personal/tokens/+page.svelte` (398)、`types.d.ts` 相关类型 (~465)                                                                                                                                                                                                                                                                                                                                                                   |        ~863 |
| **合计（核心代码）**       | **后端 ~19,122 + 前端 ~6,649**                                                                                                                                                                                                                                                                                                                                                                                                                             | **~25,771** |

### 1.2 为什么变得这么重

结合最近提交历史可以看到清晰的叠加路径：

1. `#182`（`386b61d0`）为压缩订阅/下载确认轮次，引入了服务端意图匹配与多轮衔接逻辑。
2. `#183`（`06568ef8`，110 个文件，+18,272 行）一次性引入了多套独立审批表与端点、订阅正则 NLU、MCP 域/库切片、以及前端 4 张审批卡片。
3. `#184`（`b05c0c7b`）将 AI 助手与 MCP 全量收紧为**仅管理员可用**，部分样板与历史演进层保留了下来。

---

## 二、核心冗余面与安全边界评审

### 2.1 权限边界与真正可清理的死代码

必须严格区分**仍在生效的安全边界**与**真正不可达的冗余代码**：

- **必须保留（不作为重构删减项）**：
  - MCP 令牌的 `domain` 与 `library_ids` 限制：管理员已可通过 `POST /_api/mcp/token/create` 签发受限令牌，停止执行限制会扩大已签发令牌权限，且 `AGENTS.md` 明确要求保留。
  - `backend/app/services/media.py:1830-1844` 的 `accessible_library_ids()`：同时服务普通 Web 用户（`user.perms.media_lib_ids`）与 MCP 库受限令牌，保持原样。
  - `AssistantSessionService.allowed()`（`history.py:707`、`assistant_reviews.py:306`）：同时参与历史摘要失效与旧审批复核，移除会改变资源删除后的模型可见性与旧审批行为，属独立产品决策。
  - `require_browser_admin` 的实时查库 `User.exists(id=user.id, role=ADMIN)` 与浏览器/MCP 通道隔离。
- **真正可安全清理的局部冗余**：
  - `backend/app/mcp.py:155, 2280, 2357` 中重复调用私有 `media_changes._require_admin`（`_user(ctx)` 在 `L417` 已验证 `UserRole.ADMIN`）及 `L1651` 冗余 `getattr(user, "mcp_domain", ...)`。
  - `backend/app/services/subscription_intent.py:864` 等仅在管理员路径下可达的冗余 `user.role` 判断（需逐处核对是否有直接单测传入非管理员）。
  - 前端死导出：`session.ts:39` (`errorCode`)、`navigation.ts:6` (`mediaPath`)、`reviews.ts:3` (`reviewTools`)。

---

### 2.2 六套并行的审批 / 确认 / 提问状态机

当前存在 **6 套独立 HTTP 确认流程**、**12+ 张 ORM 表**、**13 个审批端点**：

| #   | 流程                                                                      | 后端服务与规模                   | HTTP 端点与请求体                                                              | 状态表                                                                   |
| --- | ------------------------------------------------------------------------- | -------------------------------- | ------------------------------------------------------------------------------ | ------------------------------------------------------------------------ |
| 1   | 通用审批（7 个工具：订阅/下载校验、任务动作、定时任务、扫描/重刮/库配置） | `assistant_reviews.py` (358)     | `GET/POST /assistant/reviews/<call_id>` (`PlanReviewDecision`)                 | `AssistantToolCall` + `AssistantReviewDecision` + 5 张领域回执表         |
| 2   | 订阅计划编辑/删除审批（仅 1 个工具 `review_download_plan_change`）        | `assistant_plan_review.py` (201) | `GET/POST /assistant/plan-reviews/<call_id>` (`PlanReviewDecision`)            | `AssistantToolCall` + `DownloadPlanReceipt`                              |
| 3   | 原子计划重建（`review_plan_replacement`）                                 | `plan_replacement.py` (385)      | `GET/POST /download/plan/replacement/<id>` (`ReplacementDecision`)             | `DownloadPlanReplacement` + `DownloadPlanDispatch`                       |
| 4   | 管理员工作区变更（5 种动作）                                              | `agent_actions.py` (618)         | `GET/POST /assistant/operations/<id>` (`ReplacementDecision`)                  | `AgentAdminReview`                                                       |
| 5   | 工作流画布提案（`preview_workflow_draft`）                                | `assistant_proposals.py` (460)   | `/assistant/conversations/<id>/proposals/<id>/{canvas,outcome,confirm,cancel}` | `AssistantSnapshot` + `AssistantProposal`                                |
| 6   | 结构化提问 + 内嵌订阅提交（`ask_user`）                                   | `assistant_questions.py` (409)   | `POST /assistant/conversations/<id>/questions/<call_id>/answer`                | 原地回写 `AssistantToolCall.result` + 合成 `validate_download_plan` 记录 |

**明确区分“纯机械重复（优先抽）”与“语义不同（暂缓硬合并）”**：

- **优先抽取的纯机械重复**：
  - SQLite 延迟事务写锁预占（`update(updated_at=F("updated_at"))`）手写 10+ 处；
  - `require_live_admin`（`plan_replacement.py`、`agent_actions.py`）重复实现；
  - 归属查询 + 有效 preview 校验 + `request_id` 比对（`assistant_reviews.py` 与 `assistant_plan_review.py`）；
  - Superseded（新审批取代旧审批）目标与列表判定逻辑（`assistant_reviews.py` 与 `core/ai/tools.py` 重复）；
  - `core/ai/tools.py:2566-2828` 中 4 个高度雷同的 `apply_*` 函数（263 行）；
  - `agent_actions.py:114-218` 中混入的只读诊断 `inspect_workspace` / `_health`。
- **暂缓统一的状态语义**：
  - `assistant_reviews.py:53-118` (`_outcome`) 生成界面展示状态（`retryable` 默认取 `valid is False`）；
  - `review_decision.py:82-132` (`AssistantReviewDecision.finish`) 控制执行事务内的锁释放与重试资格（非 replay 失败默认 `retryable=True`）；
  - `assistant_plan_review.py:131-201` 依赖实时 `revision` 比对与独立 `DownloadPlanReceipt`。
  - **暂不强行统一 `classify_outcome()`**；后续若合并 `assistant_plan_review.py`，须专门回归**取消与聊天确认并发、提交前失败可重试、派发后结果不明**三种场景。

---

### 2.3 MCP 服务低逻辑密度、授权方向与体积基线

1. **实测基线（隔离 HTTP 测试环境）**：
   - 全域读写令牌 `tools/list` 返回 **53 个工具**，完整响应体 **223,743 字节**（包含完整 `inputSchema` 与 `outputSchema`）；
   - 其中工具描述占 **29,240 字节**；
   - 另有 **5,853 字节**的 `instructions` 随 `initialize` 返回（不在 `tools/list` 中）。
   - 优化验收时必须分别跟踪：**描述字节数、完整 `tools/list` 响应大小、输出模型契约兼容性**，不可混淆为单一指标。

2. **单处声明授权的正确方向（Fail-Closed）**：
   - `tests/test_mcp.py:467-495` (`test_write_tool_denied`) 明确验证：即使 `@server.tool(annotations=ToolAnnotations(read_only_hint=True))` 标注为只读，只要未在服务端授权表登记，就必须拒绝执行。
   - 因此单处声明时必须**由服务端显式授权（read/write + domains）推导客户端的 `read_only_hint`**，而不能反过来从 `annotations` 推导权限。

3. **输出模型与下载字段集注意点**：
   - `models/mcp.py:142-1034` 有 50 个 `Result(extra="allow")` 输出类（893 行，48 个仅 `mcp.py` 自用），其中 4 个 `ApplyResult`（`ScheduleApplyResult`、`PlanChangeApplyResult`、`TaskActionApplyResult`、`MediaChangeApplyResult`）完全等价，可安全合并。
   - `mcp.py:1804-1822` 的 `list_download_tasks` 比 `core/ai/queries.py:67-85` (`DOWNLOAD_STORED_FIELDS`) 多要求 `ratio`、`estimate`、`created_at` 三个字段；**不可直接替换**为助手字段集，如需复用必须采用超集定义（`(*DOWNLOAD_STORED_FIELDS, "ratio", "estimate", "created_at")`），确保 `DownloadResult` 校验不破。

---

### 2.4 助手 12 维动态工具目录状态机

- `backend/app/core/ai/tools.py:1141-1354`（214 行）与 `backend/app/services/assistant.py:594-745, 1204-1426`（~370 行）用 12 个分散布尔参数控制 54 个工具的可见性以满足 `test_floor_catalog <= 24,796` 字节预算。
- 重构为声明式 Profile / 阶段配置表时，必须**逐状态对照可用工具集合**与预算上限，确保每个目录状态零偏差。

---

### 2.5 订阅意图编排与旧卡兼容

- `subscription_intent.py` (916) + `subscription_choices.py` (396) + `subscription_destinations.py` (257) + `assistant_questions.py` 订阅提交胶水合计约 **1,890 行**。
- `tests/test_subscription_intent.py:925` (`test_legacy_release_card`) 与 `AGENTS.md` 均明确要求保持旧单问卡兼容。
- 工程重构只做**纯模块解耦**（如将 `_submit_subscription()` 从 `assistant_questions.py` 拆出，保持旧卡兼容）；**收窄正则 NLU 或改变确认方式属于独立产品决策**，需单独验收“一次提问、按已选条件创建、重试不重复创建”。

---

### 2.6 前端助手层内部重复与隐性开销

核心前端代码 5,786 行（22 个文件），以下均可在**零可见 UI 变更**下推进：

1. **4 张审批卡片公共逻辑重复**：值格式化 `display()`（3 处 + 1 处内联）、~12 个下载计划字段表（3 处）、领域刷新事件派发（4–5 处）、卡片外框/issues/错误提示/按钮条模板、`errorMessage(code)`、路由分类。
2. **大文件职责过载**：`session.ts` (1,235 行) 与 `AssistantPanel.svelte` (1,070 行) 可按职责清晰拆分，保持外部导出与 DOM 不变。
3. **隐性开销**：`AssistantTool.svelte:148-152` 折叠态 eager `JSON.stringify`、`context.ts:62-76` 对 512 KB 画布双次序列化、流式 `text_delta` 反复克隆 tools 与重解析 Markdown、模型 `<Select>` 反复 `JSON.stringify`/`JSON.parse`。

---

## 三、调整后的分步实施路线图

按**零行为变化、高确定性 → 逐步触及核心结构**排序，每步独立可测、不改已发布迁移、不改 UI：

### Step 1：审批机械底座抽取 + 统一 `apply_*` + 拆出只读诊断

- **改动范围**：
  - 新增 `backend/app/services/review_common.py`：抽取 `reserve_writer()`（SQLite 写锁预占）、`require_live_admin()`、`load_owned_review()`（归属查询 + 有效 preview + `request_id` 校验）、共享 superseded 判定辅助（`schedule_review_target()`、`is_media_file_listing()`）。**暂不合并 `classify_outcome()`**。
  - 将 `core/ai/tools.py:2566-2828` 的 4 个重复 `apply_*` 收敛为共用 `_apply_reviewed_change()` 辅助，保持签名与返回完全不变。
  - 将只读诊断 `inspect_workspace` / `_health` / `WORKSPACE_TOPICS` 从 `agent_actions.py` 拆到 `backend/app/services/workspace_queries.py`（在 `agent_actions.py` 保留兼容重导出）。
  - 仅清理确认无行为影响的冗余代码（如 `mcp.py` 重复 `_require_admin` / 冗余 `getattr(user, "mcp_domain", ...)`）。**不动** `accessible_library_ids()`、**不动** `domain`/`library_ids`、**不动** `allowed()`。
- **验证命令**：
  - `poetry run pytest tests/test_assistant_reviews.py tests/test_assistant_plan_review.py tests/test_agent_actions.py tests/test_plan_replacement.py tests/test_core_ai_tools.py tests/test_review_decision.py tests/test_agent_review_boundaries.py tests/test_mcp.py tests/test_mcp_domains.py -q`
  - `poetry run ruff check . && poetry run ruff format --check .`

### Step 2：MCP 声明式授权注册与描述瘦身

- **改动范围**：
  - 引入单处显式服务端授权声明（`access="read"|"write"`, `domains=...`），**由授权推导客户端 `read_only_hint`**，保持 `ScopedMCPServer` fail-closed（继续通过 `test_write_tool_denied`）及全部 `domain` / `library_ids` 限制完整不变；提取复用 `ToolAnnotations` 常量。
  - 通过 `@server.tool(description=...)` 收紧 53 个工具的描述并精简 `instructions`。
  - 合并 `models/mcp.py` 中 4 个完全等价的 `ApplyResult`，清晰分离 token/auth 模型与输出 Schema。
  - 下载字段集改用超集定义 `(*DOWNLOAD_STORED_FIELDS, "ratio", "estimate", "created_at")`，保持 `DownloadResult` 校验通过。
- **验收指标（分项核对）**：
  - 工具描述字节数（基线 29,240 bytes）与 `instructions` 字节数（基线 5,853 bytes）；
  - 完整 `tools/list` 响应体积（基线 223,743 bytes）；
  - 全部输出模型契约与域/库权限测试通过（`test_mcp.py`、`test_mcp_domains.py`、`test_mcp_authoring.py`、`test_routes_mcp.py`、`test_services_mcp.py`）。

### Step 3：前端助手内部无感抽公共与结构整理（零 UI 改动）

- **改动范围**：
  - 抽取共用 `refreshDomainTarget()`、`formatReviewValue()`、计划字段标签表、`ReviewCardShell.svelte`（保留各卡片原有类名与文案）、统一 `errorMessage(code)` 到 `errors.ts`、统一路由匹配到 `navigation.ts`，清理 3 处死导出。
  - 拆分 `session.ts`（`conversations.ts`、`handoff.ts`、`chat.ts`，`session.ts` 保留兼容导出）与 `AssistantPanel.svelte` 非视图逻辑。
  - 修复折叠态 eager `JSON.stringify`、画布双次序列化、模型选项 JSON 编解码与流式冗余克隆。
- **验证命令**：
  - `pnpm run check && pnpm run lint && pnpm run build`

### Step 4：声明式助手工具目录

- **改动范围**：
  - 将 `core/ai/tools.py:1141-1354` 与 `services/assistant.py:594-745, 1204-1426` 的 12 维布尔目录状态机重构为声明式 Profile + unlock 规则表。
- **验收要点**：
  - 逐状态比对重构前后可用工具集合 100% 一致；
  - `test_floor_catalog`（≤ 24,796 bytes）与 `test_cloud_catalog` 全部通过。

### Step 5：高敏审批合并与订阅纯模块解耦（需专项回归）

- **改动范围**：
  - 将 `assistant_plan_review.py` 的核心编排并入 `assistant_reviews.py`（保留 `/plan-reviews/<call_id>` 薄适配层），并在合并前专门验证：**用户取消与聊天确认并发、提交前失败可重试、派发后结果不明**三类场景。
  - 抽取 `agent_actions.py` 与 `plan_replacement.py` 共享的 digest / TTL / decision 生命周期外壳（保留各自执行器与恢复逻辑）。
  - 将 `_submit_subscription()` 从 `assistant_questions.py` 拆出解耦，**保留旧单问卡兼容**。

---

## 四、独立产品决策项（不与工程重构捆绑）

以下事项会改变外部权限、历史可见性、交互策略或可见 UI，均单列评估，不在 Step 1–5 中夹带变更：

1. **MCP 令牌的业务域与媒体库授权（已实施）**。新令牌默认读写，管理员可在创建时选择只读。两类均覆盖全部业务域和媒体库。数据库沿用 `0011` 的表结构，`domain` 和 `library_ids` 仅保留为存储字段。当前权限用例见 `backend/tests/test_mcp_scopes.py`。
2. **删除资源后的历史对话可见性（`allowed()`）**：管理员删除工作流/媒体库后，旧对话轮次与摘要是否继续按 `permission_denied` 遮蔽并阻止旧审批提交。
3. **订阅正则 NLU 的收窄**：是否逐步弱化 `subscription_intent.py` 的自然语言正则编排；若调整，需单独验收“一次提问、按已选条件创建、重试不重复创建”。
4. **前端审批卡片 UI 统一**：是否将 `PlanReviewCard.svelte`（`/plan-reviews/`）收编至 `AssistantReviewCard.svelte`（`/reviews/`）并统一 4 张卡片的视觉间距与按钮文案。
