# 晚风 · SnowLuma 稳定架构契约（2026-11-14）

> 状态：**目标架构与逐模块风险清单**。当前代码仅完成一部分 P0 定点修复，本文件中标记“待实施”的契约**尚未全部落实**。禁止把此文件当作已经通过实机验收的证明。
>
> **唯一聊天接入：SnowLuma → OneBot WebSocket。** OneBot 是传输协议，与 NapCat 实现无关。历史配置环境变量 `NAPCAT_*` 在迁移期可只读兼容；运行管理器不再启动 NapCat。语音、唱歌、RVC 不列入开发范围。

## 0. 单一所有权原则

- 每个用户输入都有独立 `input_id`、`platform_message_id`、`conversation_key` 和递增 `conversation_revision`。**原始输入是事实源**，由接入层登记，模型不能伪造。
- 每次回复使用冻结的 `ContextSnapshot`；整合后的背景仅由 ContextAssembler 生成一次。分析请求与回复请求共享 `snapshot_id`，不可分别查询并写回用户事实（**待实施**）。
- 模型只能提交 `proposal`；显式用户请求由确定性校验产生 `UserIntent`。统一协调器裁决授权与提交，模型不直接发 QQ、不直接写记忆、不直接启动 GPU（**待实施**）。
- 永久用户事实、角色基本设定、会话状态、图片任务状态、短暂情绪分属不同数据所有者。每份数据只有一个写入者；其他模块只读。
- 外部发送、GPU 提交和数据库更新携带 `request_id`、`scene_revision`、`conversation_revision`。迟到结果不能覆盖更新的状态。
- 不得同时等待聊天锁和 GPU 槽位；先释放当前会话锁，再处理耗时图像；不能用一个全局 FIFO 队列排所有文字和图片。
- `KNOWN_FAILED` 与 `OUTCOME_UNKNOWN` 是不同状态。已向 SnowLuma 发送但无 ACK 的内容不能盲目重发。

## 1. 标准事件格式（目标契约，待实施）

```json
{
  "input_id": "uuid",
  "platform_message_id": "SnowLuma/OneBot message_id",
  "conversation_key": "private:0:<qq_user_id>",
  "conversation_revision": 14,
  "received_at": "2026-11-14T20:30:00+08:00",
  "source": "user",
  "text": "你还记得刚才那张图吗？",
  "attachments": [],
  "intent": {"kind": "image_followup", "evidence": "刚才那张图"},
  "snapshot_id": "hash-of-revision-and-source-versions"
}
```

`source` 必须为 `user` / `character_auto` / `system_schedule` 之一；不能把角色活动文本作为用户命令重分类。可追踪数据至少包含 `owner`、`created_at`、`source_message_id` 和版本。关系和情绪评分是**策略参考**，不能升级授权。

### 上下文读取分类

| 数据 | 所有者（唯一写入方） | 可读取模块 | 读取时允许副作用 |
| --- | --- | --- | --- |
| QQ 原始消息/回执 | TransportInbox / ReceiptJournal | Context、Dialogue、TaskMonitor | 否 |
| 对话历史/未解决问题 | SessionStore + DialogueCommitter | Context、Dialogue、ReplyPlanner | 否 |
| 用户长期事实 | FactStore（用户原文验证） | Context、Analysis | 否 |
| 角色卡/世界设定 | PersonaRepository | Context、VisualPlanner | 否 |
| 角色场景（衣服/地点/发型） | SceneStore | Context、VisualPlanner | 否 |
| 短暂情绪/关系倾向 | AffectStore | Context、Dialogue、ReactionPicker | 否 |
| 角色日程与天气缓存 | RoutineStore/WeatherCache | Context、VisualPlanner | 读取否；缓存更新为独立后台任务 |
| 绘图请求/质量/发送回执 | ImageJobJournal | TaskMonitor、Context、VisualPlanner | 否 |
| 表情图片库 | ReactionLibrary | ReactionPicker | 否 |

## 2. 现有模块输入 / 输出 / 副作用 / 故障降级

| 模块（现有代码） | 输入 | 输出 / 下游消费者 | 允许的写入 / 副作用 | 失败影响与**必须的退路** |
| --- | --- | --- | --- | --- |
| `snowluma_service.py` | 配置目录、启动/停止指令 | SnowLuma 进程状态 → Manager | 仅维护自己管理的 SnowLuma 进程 | 不可用：保持 QQ 收发服务状态为离线；不得启动 NapCat 或改动另一份 QQ 登录数据 |
| `manager.py` / `services.qq_status` | OneBot 地址、token、预期 QQ | 健康探针 → 控制台 | 仅管理 bot 子进程，查询 QQ | 探针失败：独立标记连接异常和退避；不可反复重启正常 QQ，不能中断现有 ComfyUI 任务 |
| `bot.accept` / `enqueue` | OneBot 消息段、message_id、白名单 | 标准化输入 → Inbox/Router | 持久化去重 ID（**待实施**） | 格式错误：拒绝单条并记录原因，不使 WebSocket 接收循环退出；不得因后台任务数满而静默丢消息 |
| `reply_scheduler.py` / `drain_inbox` | 会话输入版本、取消、当前任务 | 有序交付 Ticket → Dialogue/Transport | 每会话回复票据、取消标志 | 被抢占：旧版本结果无效；仅取消未交付阶段；过期操作必须释放会话锁 |
| `context_engine.py` | 当前会话、用户消息、事实、日程、角色卡 | 相关背景 → Analysis/Dialogue | **目标**只读快照；缓存写入另起低优先级流程 | 天气/笔记失败：省略该附加背景并标 `unavailable`；不能失败整轮聊天或注入过时日程 |
| `dialogue_depth.py` | 当前消息 + 历史与未决问题 | 讨论深度 A/B/C、引用关系 → DualCallRouter | 讨论更新仅交由 Committer ACK 后落地 | 路由不确定：可选择安全的双调用或一次正常文本生成；不能“没理解”就清空话题 |
| `chat_analysis.py`（待从另一分支审查移植） | 一份上下文快照 + 当前消息 | `AnalysisProposal`（记忆/话题/情绪建议） → Planner | **无副作用** | 超时/坏 JSON：完整文本契约降级，已有事实不修改；不丢本轮用户输入 |
| `social_reasoning.py` / `life.py:mood` | 有证据的事件、近期语气、时间 | `EmotionSnapshot` / `AffectProposal` → Dialogue/Reaction/Visual | 仅在交付/合法事件之后写短期情绪 | 失败：中性语气；不影响正文，不得生成虚构的关系转折；读状态不应写库 |
| `companion.py` | 用户、角色、相处记录 | 稳定关系阶段 → Dialogue | 仅真实交互记录/显式关系选择 | 失败：使用已确认的稳定关系状态；不得重置为初识，也不能由模型任意打分跳阶段 |
| `life.py:record` | 用户原话、证据片段和合法类别 | 有来源长期事实 → Context/FactStore | 仅写通过验证的用户事实 | 值与证据不匹配：拒写并记因；不能用模型摘要代替用户事实 |
| `image_requests.py` / `image_policy.py` | **用户原始消息**、最近视觉话题、原始任务账本 | `UserImageIntent`（request/amend/cancel/none） → ImageCoordinator | 仅请求状态，不启动 GPU | 无依据的“给我看看”→ 要么有界承接，要么普通聊天；模型暗示不能冒充用户授权 |
| `image_candidates.py` | 已确认情绪/角色活动/共享笑点 | `AutoImageCandidate` → ImageCoordinator | 候选记录，不占用 GPU | 故障：放弃自动图片，正文继续；不得把候选升格为明确索图 |
| `image_director.py` | UserIntent/AutoCandidate + SceneSnapshot + 预算/限流 | 单一 `ImageDecision`（permit、kind、reason、priority） | 权限裁决和记录（**统一协调器待实施**） | 拒绝：必须返回明确原因；用户索图错误不能改称“已在绘画” |
| `image_planner.py` / `visual_brief.py` / `prompt_spec.py` | 已授权视觉请求 + 冻结场景 + 用户约束 | 可校验提示词与镜头 → Workflow | 不得修改永久人设/地点 | 提示冲突：阻止提交 GPU，保留原请求待用户修正或单次安全修复；不自动编造设定 |
| `image_workflow.py` / `GPUManager` / ComfyUI | 合法图形、模型与工作流配置 | 任务 ID、PNG、错误码 → QA/Journal | 仅操作自己提交的 GPU 任务；无活动时按策略卸载 | 服务离线/显存满：文字继续；队列可观察，TTL 结束释放槽位，未知提交必须核对原 ID，不能重复提交 |
| `image_qa_cache.py` / `inspect_image` | 实际像素哈希、约束快照 | 通过/拦截/降级 → Delivery | QA 缓存仅允许对应像素及规则版本 | QA 失败/未知：保留失败原因，按显式用户策略处理；禁止把未检图当“质量通过” |
| `send_reaction` | 已提交文本对应的情绪/发送策略 | 表情文件 → Transport | 只更新发送冷却及回执 | 缺图/失败：直接跳过，不能阻塞文字 |
| `delivery_state.py` / `SessionStore` | 发送计划、实际 ACK、版本 | 历史、讨论状态、待发状态 → Context | 仅 Committer 在明确 ACK 后确认角色宣称 | ACK 未知：保留不确定，避免重复发送；写库冲突需三路合并或拒绝旧版本，不能静默覆盖 |
| `initiative.py` / 日程与笔记后台 | 明确开启、免打扰、沉默时间、未回复次数 | 低优先级候选 → Router | 只记录已批准的候选尝试 | 任意失败：停止该次主动活动；不能占用用户消息入口，也不能为“凑活跃”重复重试 |

上述“目标架构”术语只是期望的职责划分，并非已全部存在的类名。`ContextSnapshot`、`ImageCoordinator`、`FactStore` 等需要后续逐步落到实际 Python 模块。

## 3. 优先级：按资源车道，而不是一个总分

| 车道 | 顺序 | 约束 |
| --- | --- | --- |
| 控制车道 | 取消 / 重置 / 用户明确命令最高 | 不依赖 DeepSeek、GPU、外部笔记 |
| 文字车道 | 当前用户消息 > 主动文本 | 每会话可抢占旧未发正文；跨会话并行；永不等待 GPU |
| GPU 车道 | 明确索图 > 安慰/共同笑点候选 > 日常主动图 | GPU 只有有限并发；已提交运行中的任务不因后续高优先级而随意杀进程 |
| 可选发送车道 | 文本交付后才考虑表情包 | 发送失败不回滚已成功的文字 |
| 预取车道 | 天气/日程/OneNote/旧任务核对 | 有时间上限、限频，永远不能占满入站任务容量 |

**不同车道不可直接比较优先级整数。** 明确索图高于自动索图只影响 GPU 队列，不允许绘图锁压住当前用户文字。任何取消指令先提升当前 `conversation_revision`，再撤销尚未提交的后台动作。

### 两次模型调用（待合并）

`RouteDecision` 根据语义复杂度、话题承接、未解决问题、事实记忆依赖和用户明确指令决定是否双调用。10 多字的「你还记得那天吗」可以双调用；100 字的重复笑声可以只调用一次。长度只是兜底，不作为唯一阈值。

- 第一次 `Analysis`：只读取冻结快照，返回当前话题、引用解析、所需事实键和风险标记；**绝不修改任何数据库、创建图片任务或生成拟发送正文**。
- 第二次 `Dialogue`：只写最终文字、语气与可选行为提议。上下文来自同一个 `snapshot_id`，如中途有新消息导致 revision 更新，旧回复与旧分析结果不能落库/发出。
- 两次都失败时：最多一次有界回退，用户输入仍留账本；不使用半成品 JSON 更新情绪或记忆。参数错误、用户取消不能触发无止境重试。

## 4. 发生冲突时的确定性裁决

1. 用户说「不要画了」+ 旧图刚做完：停掉未发图片，保留 GPU 任务已提交/已生成记录；若已经交给 QQ 且 ACK 未知，只能标 UNKNOWN，禁止自动重发。
2. 用户说「换成白色裙子」+ 自动图还在规划：`scene_revision` 变化，旧规划作废；新裙子必须出现在新场景快照里，旧图不能当新衣服发送。
3. 用户只说「刚才那张呢」：从任务账本关联最近用户明确授权且未过期的请求；无关联则不要默认拍照、不要创造新任务。
4. 文本生成与绘图同时进行：文本先发送，绘图单独排队和 QA；`image_planner`、`ComfyUI` 的超时不改变文字交付状态。
5. 情绪判断提议发安慰图但用户正在忙：情绪可以影响文字，自动图被抑制；不允许反向修改用户的忙碌状态。
6. 本轮用户说「我喜欢热茶」但模型写入「每天喝咖啡」：必须拒写，不能凭引文存在就相信值。用户明确更正走独立可追踪的更正事务。
7. OneBot 暂时断开：不能声称已发 QQ；已知收到的输入保留待处理；对断连期间**根本没有送进应用的消息**不得谎称能恢复。

## 5. 状态机与故障屏障（目标）

`RECEIVED → ROUTED → CONTEXT_FROZEN → ANALYZED? → DRAFTED → VALIDATED → QUEUED_TO_SEND → ACKED`

每一步都有 `deadline`、`reason`、`request_id`、`revision`。失败状态为 `FAILED` / `CANCELLED` / `EXPIRED` / `OUTCOME_UNKNOWN`，不可含混地写成已完成。

图像独立状态机：
`REQUESTED → AUTHORIZED → PLANNING → PROMPT_VALIDATED → GPU_QUEUED → GPU_RUNNING → RENDERED → QA → SEND_PENDING → ACKED`。

后台进程守护只监视健康状态；发现卡死时先中止该任务的等待、释放它占用的调度资源，再独立修复服务。不能重启整套机器人以掩盖局部模块异常。

## 6. 分阶段实施与验收（不可跳过）

- **P0A：接口迁移** SnowLuma 唯一运行接入；旧 `NAPCAT_*` 只读过渡；所有配置操作不修改旧 NapCat；基本连接、断开重连、回执未知的测试。
- **P0B：最小修复** 消除后台任务总数导致的静默丢消息、绘图自动候选误判明确授权、情绪读状态写回、无原文依据的长期记忆；对应测试。
- **P0C：统一任务调度（待实施）** 取消与 GPU 分车道；队列限额和过载提示；有界超时与恢复。
- **P0D：单一 ContextSnapshot（待实施）** 所有模块从一份只读快照消费，持久数据只有提交器能修改；新增可解释来源与测试。
- **P0E：自适应双调用（待从独立实验分支移植）** 语义路由、失败回退、旧版本分析丢弃、最多两次主调用；复杂短句对照测试。
- **P1：角色体验** 稳定基础上引入 STBridge 的动态好感度、情绪衰减、表情库和主动文字消息；全部经同一个策略入口。**无语音与唱歌。**

### 必需测试矩阵

- 50 条连续 OneBot 消息 + 3 个后台 GPU 任务：接收端不因任务数量静默丢消息；每条可查询状态
- 两个会话交错输入：用户事实/私人背景/角色状态不互串
- 10 字深聊与 250 字玩笑：双调用不是简单字数判定
- 用户连发「拍照 → 不画了 → 换衣服 → 拍照」：旧任务绝不带着旧场景发送
- ComfyUI 超时/无响应/提交 ACK 丢失：文字照发；原任务 ID 对账，不重复发图
- DeepSeek 返回半个 JSON：不得写用户事实，也不得承诺图像正在生成
- SnowLuma WebSocket 中断：不误标已发、不重复发送、连接恢复后正常处理新消息
- 重启及数据库冲突：已确认记录保留，过期快照不能覆盖；测试使用临时数据库，不接触生产密钥/私聊记录
- 每个模块故意抛异常：只降级受影响功能，剩余文字服务维持可用

合并门槛：Python 全量测试 + 假 OneBot/ComfyUI E2E + Windows SnowLuma 真机冒烟测试 + 真实长聊/画图交错演练全部过关；在此之前 PR 保持草稿。
