# 移动端 App 功能评测智能体施工计划

## 0. 当前状态（2026-08-31）

阶段 A–E 已完成，当前代码已经实现本文第 5 节的目标架构；第 3、4
节保留问题演进背景，但以本节和代码/测试为准。当前离线验收为：

- 全量测试 `210 passed`；
- 六条冻结真实 trace `6/6`，exact accuracy `1.0`；
- false pass、false fail、attribution error 均为 `0`；
- 默认生产路径不注入 locator，离线集成测试完整经过原始 MobiAgent
  Decider、runtime prompt、Grounder、动作 handler 与 Step Gate；
- Windows 构建收集七个 runtime Markdown prompt，源码与冻结进程 smoke
  均实际读取并验证这些文件；
- 业务执行和真实 Verification Runner 均有 observation burst 与 attempt
  审计，真实 Verification Runner 只重试可证明发生在派发前的定位失败。
- 原 MobiAgent Decider/Grounder 的请求、完整响应、显式 reasoning、校验、
  重试和耗时现在同时进入 CLI/PC 实时视图与持久化 `model_events.jsonl`；
  prompt、截图 payload、消息正文和 API key 不进入该事件流。

剩余阶段 F 必须由用户选择设备、测试账号和低风险商业 App 手动触发。
Codex 不自行执行发布、发送、支付等副作用操作。

## 1. 当前共识

本项目的目标是：使用 MobiAgent 操作一个已经存在的移动 App，并根据用户提供的功能测试用例判断 App 行为是否符合预期。

最重要的输入原则是：**测试用例保持用户视角，不随着内部架构增加实现细节。**

测试设计者提供的是：

- 被测 App；
- 必要的前置条件；
- 用户能够描述的业务步骤；
- 测试数据；
- 最终可观察的预期结果。

测试设计者不需要提供：

- 点击坐标；
- 控件 bounds；
- UI hierarchy 节点 ID；
- 每一步的 `expected_after`；
- 验证 Runner 的导航路线；
- 模型选择或截图区域。

这些内容只能由运行时系统根据当前页面、步骤文本、测试数据、后续步骤和最终结果自动产生或推断，并作为内部证据保存，不能反向修改测试用例。

## 2. 不变的测试用例形态

测试用例仍然表达完整的用户任务，例如：

```json
{
  "test_case_id": "create-post-001",
  "app_under_test": {
    "name": "DemoForum",
    "package": "com.example.demoforum"
  },
  "preconditions": ["用户已经登录"],
  "test_data": {
    "post_content": "app_test_${run_id}"
  },
  "steps": [
    "打开发布入口",
    "输入测试内容",
    "点击发布"
  ],
  "expected_results": [
    "可以在个人主页看到本轮发布的测试内容"
  ]
}
```

项目当前已经存在结构化 `TestCaseSpec`。第一版可以继续要求步骤和最终结果是结构化字段，但不得要求测试作者填写坐标或逐步后置条件。若现有 JSON 已经包含 `target`，它只能作为可选提示或兼容字段，不能成为真实执行的主要定位依据。

## 3. 已经实现的部分

以下内容已经落地并有离线/回放验证；真实商业 App 端到端试点仍不能由离线结果代替。

### 3.1 项目定位和阶段基线

- 已确定当前仓库就是 App 功能评测智能体项目，不创建第二个仓库。
- 已保留旧 `runner/`、设备操作、截图、hierarchy、action trace 和 `verification_benchmark` 底层能力。
- 阶段 0、阶段 1 已完成并有对应提交/分支历史。
- 阶段 2 已用可替换执行器验证编排器和结果语义。
- 旧的 `PASS/FAIL/ABSTAIN` 仍可作为 legacy 回归路径，但不再是新 App 测试的主语义。

### 3.2 App-test 协议和控制流

`app_test_agent/schema.py` 已实现：

- `TestCaseSpec`、`TestStep`、`ExpectedAssertion`、前置条件和测试数据引用；
- `OPEN_APP`、`CLICK`、`INPUT`、`WAIT`、`BACK` 等动作类型；
- `TEXT_VISIBLE`、`TEXT_ABSENT`、`STATE_CHANGED`、`SUCCESS_SIGNAL` 等结果断言；
- `run_id` 模板替换、测试用例 hash 和基础 schema 校验；
- `verification_steps`、`requires_verification_runner` 和显式的
  `verification_runner_policy` 字段。策略支持 `NEVER`、
  `IF_DIRECT_UNKNOWN`（默认）和 `REQUIRED_FOR_RESULT`。

这些字段可以继续兼容，但不能成为新测试用例必须补充的信息。后续应让“业务步骤 + 最终结果”成为推荐和默认路径。

### 3.3 Mock 执行和结果聚合

以下链路已经可以在 Mock/Fake 场景运行：

```text
TestCaseSpec
  -> StepExecutor
  -> ExecutionRecord
  -> Execution Conformance Verifier
  -> App Behavior Verifier
  -> Attribution
  -> Report
```

已有能力包括：

- 步骤顺序和执行状态检查；
- 输入值是否与测试数据一致的检查；
- `TEST_EXECUTION_FAIL`、`ENV_BLOCKED`、`INCONCLUSIVE`、`UNSUPPORTED` 等结果语义；
- App 直接证据检查；
- 直接证据不足时启动 Verification Runner 的控制流；
- Verification Runner 结果与业务执行结果分开记录；
- 报告、manifest 和 contract hash 的基础输出。

Mock 只证明控制流和归因规则，不证明真实设备、模型定位或 App 功能。

### 3.4 现有 MobiAgent 适配器和证据采集

`app_test_agent/mobiagent_executor.py` 已具备 step-bound 真实设备能力：

- 连接 Harmony/Android 设备；
- 启动被测 App；
- 截图和 hierarchy 采集；
- 业务步骤逐步循环；
- 点击、输入、等待、返回等基础动作；
- `step_id`、action index、pre-frame、post-frame 的基础记录；
- actions、frames 和 execution manifest 的输出；
- step/attempt 绑定的 Decider/Grounder 结构化模型事件、CLI 实时日志和
  PC GUI 实时日志；
- 默认调用原 MobiAgent Decider/Grounder、runtime prompt、坐标转换和动作
  handler；注入式 locator 仅保留为测试/兼容入口；
- `done` 只结束当前步骤或已经确认终态的 GOAL，绝不代表 App 成功；
- Step Gate 逐 attempt 审核目标、派发、进展、观察窗口与安全重试；
- 已派发写动作、`INPUT` 和 GOAL 副作用 micro-action 不会整体重派发。

### 3.5 Verification Runner

`app_test_agent/verification_runner.py` 已有两类能力：

- scripted runner，用于离线验证控制流；
- 真实设备上的受限只读 MobiAgent verification runner。

已有约束包括：

- 只在业务执行符合性通过且直接证据不足时考虑启动；
- 验证轨迹与业务轨迹分开；
- 限制导航、等待、刷新、滚动和观察；
- 禁止重复发布、删除、点赞、支付等写操作；
- 验证路线失败不能直接判定 `APP_FAIL`；
- 使用完整 observation burst，逐 attempt 记录 pre-frame、派发状态、
  observation frame、capture error 和 retry reason；
- 只有 `PRE_DISPATCH` 定位失败允许有界重试；派发成功、派发结果不确定或
  post-capture 失败均不得重发导航动作；
- `NAVIGATE` 必须有只读 role、精确语义候选和 runtime hit-node 证据，固定
  坐标本身不是只读证明。

它只承担最终结果证据查找，不承担业务执行或 App verdict；观察结果必须
交回 App Behavior Verifier。

### 3.6 已完成的验证

- App-test 单元/集成测试覆盖协议、Mock、默认原 Decider/Grounder、执行
  符合性、Step Gate、App verifier、报告、冻结 prompt 和 Verification Runner。
- 当前全量为 `210 passed`；受保护真实 trace 为 `6/6`，exact accuracy
  `1.0`，无 false pass、false fail 或归因错误。
<!-- - 已有小红书测试样例和真实设备探索记录，但不能视为可靠的端到端成功证明。 -->

## 4. 已关闭的差距与剩余真机验收

### 4.1 已关闭：默认路径不依赖测试用例坐标

默认真实业务路径现在是：

```text
当前 screenshot + hierarchy + 当前 TestStep instruction
  -> 原 MobiAgent Decider/Grounder
  -> 目标候选
  -> 动作执行
  -> Step Gate 独立执行证据验证
```

测试用例坐标和注入 locator 不参与默认生产路径。兼容字段可以继续读取，
但不能替代模型/运行时证据，也不能为了某个用例增加固定坐标规则。

### 4.2 已关闭：原 MobiAgent 链路已有离线集成证明

离线集成测试保持 `step_decider=None`、`target_locator=None` 和
`allow_legacy_target_hints=False`，只替换最底层模型传输与 fake device，
实际经过原始消息构建、响应解析/校验、Markdown prompt 加载、Grounder、
坐标转换和 click handler。断言同时覆盖 Decider → Grounder 调用顺序、
Grounder 几何来源、一次实际派发和 Step Gate 证据。

### 4.3 已关闭：Step Gate 在每一步派发后运行

```text
读取当前业务步骤
  -> Runner 执行一步
  -> 验证动作是否匹配当前步骤
  -> 采集 post-observation
  -> 自动推断当前步骤是否仍能推进
  -> 通过才进入下一步
```

Step Gate 不要求测试用例提供 `expected_after`。它必须使用以下内部信息推断：

- 当前步骤 instruction 和 action type；
- 当前页面 hierarchy、截图和前后 frame；
- 下一步骤目标是否变得可定位；
- 当前目标是否消失、页面是否进入合理的新状态；
- 输入值是否出现在可编辑区域；
- 是否出现崩溃、ANR、登录、权限、网络或其他环境阻断；
- 后续步骤是否仍然可以执行。

推断结果只是运行时证据，不是测试用例新增的断言。
`CONTINUE` 只允许推进业务步骤，不等于 `APP_PASS`；动作已经派发但观察
不足时只允许补观察或停止为 `INCONCLUSIVE`，不能重新执行写入。

### 4.4 不能把“页面没变化”直接判为 App 失败

没有显式逐步后置条件时，点击后页面未变化只能产生：

- 可继续观察；
- 可能需要重试；
- `INCONCLUSIVE`；
- 明确的环境阻断；
- 明确的崩溃/异常。

只有在原始测试用例的最终结果有明确证据、业务步骤执行正确、环境正常，并且最终结果经过直接观察或只读 Verification Runner 充分检查后，才能输出 `APP_FAIL`。

### 4.5 已关闭：过程 Gate 与最终 App oracle 已分离

Step Gate 已承担低权重的内部过程状态推断，并保持以下边界：

- 不修改 `TestCaseSpec`；
- 不把模型猜出的后置条件写回 contract 作为用户承诺；
- 用过程证据帮助 Step Gate 控制流程；
- 过程证据不足时保守输出 `INCONCLUSIVE`，不能冒充最终 App oracle。

最终 App oracle 仍只由测试用例已有的最终预期结果决定。

### 4.6 已关闭：重试和证据窗口已审计化

业务执行与真实 Verification Runner 均支持：

```text
pre-frame
  -> dispatch
  -> action result
  -> immediate post-frame
  -> observation burst
  -> inferred step state
  -> continue/retry/stop
```

每次 attempt 都有独立 action id、时间、frame、派发状态和原因。业务写
动作、`INPUT` 与 GOAL 副作用 micro-action 派发后不整体重试；真实
Verification Runner 只重试可证明没有派发动作的 `PRE_DISPATCH` 定位失败，
post-capture 恢复只追加观察帧。

### 4.7 商业 App 是当前测试重点

当前项目直接聚焦商业 App 用例。商业 App 主要用于验证真实 UI、模型定位、登录/网络/弹窗处理、步骤级证据、最终结果观察和只读 Verification Runner。商业 App 的服务端状态和版本变化不可控，因此必须保守归因：证据不足输出 `INCONCLUSIVE`，不能为了得到 `APP_FAIL` 而猜测 App 内部原因。

Demo App、故障注入和独立真值环境不属于当前施工主线，暂不安排相关开发和验收。

## 5. 目标架构

```text
用户视角测试用例
  -> 输入校验与 runtime test data
  -> 业务 TestStep 编排器
  -> 原 MobiAgent Decider/Grounder + 设备执行
  -> Step Gate
       - 动作符合性
       - post-observation
       - 内部步骤状态推断
       - continue / retry / stop
  -> 最终业务执行轨迹
  -> 直接 App 结果检查
  -> 必要时只读 Verification Runner
  -> App Behavior Verifier
  -> Attribution 和报告
```

职责边界：

### Runner

- 根据当前步骤文本和当前 UI 定位并执行动作；
- 只能处理当前步骤；
- 记录模型输出、目标候选、坐标、实际动作和原始设备证据；
- 不判断 App 最终成功或失败。

### Step Gate

- 检查是否执行了当前步骤要求的 action；
- 检查目标定位是否有足够证据；
- 观察动作后的页面；
- 根据当前步骤、下一步骤和环境信号推断是否允许继续；
- 不因为单纯页面无变化就判定 `APP_FAIL`；
- 证据不足时停止或标记 `INCONCLUSIVE`，防止错误推进。

### App Behavior Verifier

- 只根据原测试用例的最终 `expected_results` 判定业务效果；
- 先使用截图、UI XML、文本和时间线等确定性证据；
- 必要时使用单 assertion 的视觉模型；
- 直接证据不足时调用只读 Verification Runner；
- 不接受 Runner 的 `done` 或自我报告作为成功证据。

### Verification Runner

- 只寻找已经产生的结果；
- 只能执行导航、等待、刷新、有限滚动和观察；
- 有独立轨迹和预算；
- 不重复执行业务写操作；
- 路线失败不能直接等于 App 失败。

## 6. 后续重构路线

### 阶段 A：冻结不扩展测试用例输入（已完成）

目标：让 Codex 明确禁止通过增加字段解决 Runner/Verifier 问题。

工作：

- 将“步骤 + 最终预期结果”作为默认测试用例契约；
- 坐标、bounds、模型置信度和页面推断只作为运行时数据；
- `expected_after` 不作为必填字段，不为小红书样例继续添加逐步后置条件；
- `verification_steps` 保留兼容能力，但不作为普通测试用例必填项；
- 为最简测试用例增加 schema 测试，证明没有 target 坐标和 `expected_after` 也能被接受；
- 报告记录原始测试用例 hash，确保执行轨迹不能改写测试目标。

验收：同一份用户视角测试用例可被 Mock executor 接受，且不会因缺少坐标或逐步后置条件被拒绝。

### 阶段 B：拆出真正的单步 MobiAgent 执行模式（已完成）

目标：复用原 MobiAgent 的 Decider/Grounder 和设备动作能力，同时限制为一个 TestStep。

工作顺序：

1. 阅读并定位原 `runner/mobiagent/mobiagent.py` 的 Decider、Grounder、设备和坐标转换入口；
2. 设计 `execute_step(test_step, current_observation, constraints)` 适配边界；
3. 让原模型根据 instruction 定位目标，不读取测试用例坐标；
4. 保留原始 screenshot、hierarchy、model response、bbox、坐标转换和 action trace；
5. 把 `test_case_id`、`step_id`、attempt 和 action id 注入 trace；
6. 禁止模型执行下一步；
7. 把模型 `done` 解释为“当前步骤结束”，不解释为 App 成功；
8. 对模型无法定位、动作异常、设备断开和环境弹窗分别输出明确状态。

验收：一个没有坐标 target 的 CLICK/INPUT 测试步骤可以在 fake device + fake model 下完成；真实设备路径必须使用原 MobiAgent 的定位/执行链，而不是 `_model_target_locator` 的独立简化替代。

### 阶段 C：实现内部 Step Gate（已完成）

目标：逐步控制流程，但不要求测试作者描述逐步后置状态。

新增内部数据：

```json
{
  "step_id": "open_publish_entry",
  "attempt": 1,
  "pre_frame": "frame-10",
  "action_ids": [11],
  "post_frames": ["frame-11", "frame-12"],
  "target_evidence": "CONFORMANT",
  "progress_evidence": "NEXT_STEP_TARGET_AVAILABLE",
  "environment_signal": null,
  "gate_decision": "CONTINUE"
}
```

内部推断优先级：

1. 明确的设备/Runner 错误；
2. hierarchy bounds、action type、输入值等确定性执行证据；
3. 崩溃、ANR、登录、权限、网络和系统弹窗检测；
4. 下一步骤目标是否可定位；
5. 前后页面/控件状态变化；
6. 必要时才使用局部视觉判断。

Gate 结果：

- `CONTINUE`：当前步骤完成且后续流程仍可推进；
- `RETRY`：动作或观察可在预算内安全重试；
- `TEST_EXECUTION_FAIL`：动作未正确执行、目标错误或步骤无法完成；
- `ENV_BLOCKED`：环境阻断；
- `INCONCLUSIVE`：证据不足，不能继续安全判断。

明确禁止：

- 页面没有变化就直接输出 `APP_FAIL`；
- 用 VLM 自己生成用户未提供的最终预期；
- 让 Gate 的内部推断覆盖原测试用例的最终结果；
- 为了通过 Gate 向测试用例追加坐标或 `expected_after`。

验收：执行器在第二步已经无法定位时停止并指出失败步骤；点击正确但页面变化不明显时不误报 App 失败；所有 attempt 都有独立证据。

### 阶段 D：重构执行符合性和 App 结果判定（已完成）

目标：严格分开“动作是否正确”和“App 功能是否成功”。

执行符合性检查：

- step 顺序、数量和 attempt 预算；
- action type、输入数据和目标候选；
- 点击坐标是否落在运行时目标 bounds；
- trace 是否完整；
- 是否发生跳步、越权或环境阻断。

App 行为检查：

- 只检查原测试用例已有最终 expected result；
- `TEXT_VISIBLE` 优先使用本轮唯一运行时数据；
- 检查直接终态和 observation burst；
- 直接证据不足时启动只读 Verification Runner；
- 充分确认结果缺失时才能 `APP_FAIL`；
- 无法确认时必须 `INCONCLUSIVE`。

流程级 VLM 只作为执行符合性证据不足时的单次兜底，不能覆盖确定性的动作
错误、跳步、预算超限或原子步骤 `done` 错误，也不能输出 App 成功结论。

验收：同一条业务 trace 可以区分执行失败、App 失败、环境阻断和证据不足；Verifier 不要求 `expected_after` 才能运行。

### 阶段 E：完成只读 Verification Runner（已完成）

目标：在最终结果未出现在业务执行终态时，安全寻找结果证据。

Verification Runner 是否允许或必须启动由测试用例中的
`verification_runner_policy` 冻结到 Contract 中。Runner 不能因为自己的
诊断结果改变这个策略。

工作：

- 默认优先使用测试用例已有最终结果的观察 surface；
- `verification_steps` 只作为可选显式导航提示；
- 若没有显式验证路线，只允许使用有限、通用、只读的观察策略；
- 独立记录 `verification_step_id`，不能伪装成业务步骤；
- 限制步数、等待、刷新和滚动预算；
- 到达目标页面且观察充分后，才允许判定结果缺失；
- 路线失败、账号问题、网络问题或页面不确定时输出 `INCONCLUSIVE`/`ENV_BLOCKED`。

验收：成功找到本轮唯一内容为 `APP_PASS`；正确到达目标页面且观察充分但内容不存在为 `APP_FAIL`；验证路线失败不输出 `APP_FAIL`。

### 阶段 F：商业 App 真实环境试点（待用户手动触发）

在 Step Gate、observation burst、Verification Runner 和报告链路完成后，直接进入商业 App 用例试点。优先选择风险低、结果可观察、账号状态稳定的短流程，再进入小红书发帖流程。

商业 App 试点重点验证：

- Runner 是否能根据用户视角步骤稳定定位并执行；
- Step Gate 是否能在中途发现执行偏离；
- observation burst 是否能覆盖异步页面变化；
- 登录、权限、网络、弹窗和崩溃是否被正确归类；
- 最终结果缺失时，Verification Runner 是否能安全完成只读观察；
- `APP_PASS`、`APP_FAIL`、`INCONCLUSIVE` 是否有充分证据支撑。

小红书测试用例仍保持用户视角：进入发帖、输入唯一内容、发布、在本人内容列表观察结果。禁止将设备探测得到的固定坐标写入用例作为主路径。

真实测试由用户手动执行，Codex 只能准备命令、检查报告和分析证据，不能自行发布内容。

## 7. 结果语义

| 结果 | 含义 |
| --- | --- |
| `APP_PASS` | 业务步骤正确完成，环境正常，最终预期有充分证据支持 |
| `APP_FAIL` | 业务步骤正确完成，环境正常，最终预期明确缺失或被违反 |
| `TEST_EXECUTION_FAIL` | Runner 没有正确完成测试步骤，不能归因 App |
| `ENV_BLOCKED` | 登录、权限、网络、设备、账号或系统状态阻断测试 |
| `INCONCLUSIVE` | 证据不足，不能可靠判断 App |
| `UNSUPPORTED` | 当前 Runner 或 Verifier 不支持该动作/证据类型 |

特别规则：

- `ACTION_DISPATCHED` 不等于 `CONFORMANT`；
- `CONFORMANT` 不等于 App 成功；
- `CONTINUE` 不等于最终通过；
- Runner `done` 不等于 App 成功；
- 已派发写操作、`INPUT` 和 GOAL 内部副作用动作不得整体重派发；
- 页面无变化不等于 App 失败；
- 只有最终预期的充分证据才能产生最终 `APP_PASS`/`APP_FAIL`。

## 8. Codex 执行约束

Codex 后续工作必须遵守：

1. 先修改 Runner/Verifier，不通过扩展测试用例字段来规避实现问题。
2. 不把坐标写入新的测试样例；坐标只能出现在运行时 trace 中。
3. 不要求普通测试用例提供 `expected_after`。
4. 不把模型 `done`、页面停留或单次点击结果当作 App 结论。
5. 先复用并拆分原 MobiAgent Decider/Grounder，再考虑新增模型调用。
6. 每完成一个阶段，增加对应的 fake/mock/replay 测试；真实设备测试必须由用户手动触发。
7. 任何无法证明的结果输出 `INCONCLUSIVE`，不能用猜测规则强行通过或失败。
8. 保留用户已有未提交修改，不执行 destructive git 操作。

## 9. 当前下一步

代码侧阶段 A–E 已关闭。下一步只进行真实环境验收，不再通过增加用例
坐标或专用控件规则换取通过：

1. 由用户选择测试设备、账号与一个无发布/发送/支付副作用的商业 App
   短流程，先运行 preflight，再明确确认真实执行；
   在此之前先从目标 Conda 环境运行 `verify_pc_release.ps1
   -AcceptanceLevel Formal -DeviceProfile <platform> -DeviceSerial <serial>`；该门禁
   禁止跳过完整 trace 或打包，并检查源码和冻结客户端的目标设备依赖。门禁
   通过只代表正式试点已就绪，摘要仍将商业 App 验收标记为待用户触发；
2. 人工核对原始 Decider/Grounder 决策、Step Gate attempt、observation
   burst、App Verifier 和归因报告；
3. 低风险试点稳定后，再由用户手动触发小红书等有副作用流程；
4. 若真实 UI、登录态或网络导致证据不足，保留 trace 并输出
   `INCONCLUSIVE`/`ENV_BLOCKED`，不得加入测试专用坐标绕过；
5. 真实发布、发送、删除、点赞和支付始终不得由 Codex 自行执行。

测试用例格式继续保持不变，不新增逐步 `expected_after`、坐标或验证路线要求。

## 10. 最终验收

第一版必须在商业 App 真实环境中完成以下可审查闭环：

```text
用户视角测试用例
  -> 商业 App 正确执行且最终证据出现 -> APP_PASS
  -> 商业 App 执行偏离              -> TEST_EXECUTION_FAIL
  -> 登录/网络/设备等阻断            -> ENV_BLOCKED
  -> 结果或过程证据不足              -> INCONCLUSIVE
  -> 执行正确、观察充分但最终结果缺失 -> APP_FAIL
```

每个商业 App 结论都必须能追溯到：

- 原始测试用例和 hash；
- 每个业务步骤及 attempt；
- 运行时目标定位和实际动作；
- pre/post frame 和 observation burst；
- Step Gate 决策及原因；
- 最终 assertion；
- Verification Runner 的独立轨迹（如启动）；
- 归因所依据的截图、XML、动作和时间点。

测试用例格式保持用户视角，系统复杂度由内部 Runner、Step Gate、证据采集和 Verifier 承担。
