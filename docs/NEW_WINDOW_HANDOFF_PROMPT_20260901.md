# MobiAgent Verification 项目交接提示词

你正在继续维护一个面向 Android / HarmonyOS 商业 App 的端到端评测智能体。请先完整阅读本提示词，再阅读仓库中的 `README.md`、`APP_TEST_AGENT_README.md`、`PLAN.md` 和 `docs/` 下与当前阶段相关的文档。不要把它当作普通 UI 自动化脚本项目；核心任务是判断一次模型驱动的操作是否按测试契约完成，以及 App 是否真的产生了最终结果。

## 1. 项目位置与当前状态

- 主仓库：`D:\Lab\MobiAgent-verifier-enhanced`
- 分支：`main`
- 交接开始时先用 `git log -1` 核对提交；本次交接文件创建前的基线为 `5b88a99`，已推送到 `origin/main`
- 当前环境：Conda `mobiagent-e2e`
- HarmonyOS 设备：`5ZU0226122004500`
- Formal PC acceptance：PASS
- 全量回归：`238 passed`
- 六条冻结真实 trace：`6/6`
- exact accuracy：`1.0`
- false pass、false fail、attribution error：均为 `0`
- 最新 PC EXE SHA256：`D455E6326FDF2199195BE7F8DD22716C73F2AC6DF225869F709121E4A60EDEED`

仓库根目录的 `api-key` 是本机密钥文件：绝不能读取、打印、复制或提交。根目录的 `报错信息截取` 是用户材料：不能修改、删除或提交。开始任何修改前先运行 `git status --short`，保留用户已有修改。

## 2. 不可改变的系统边界

主链路固定为：

```text
用户视角测试用例
  -> TestCaseSpec / Contract
  -> 原始 MobiAgent Decider/Grounder 自主观察和提出动作
  -> Step Gate 审核顺序、目标、动作证据、观察窗口、环境与重试安全
  -> App Verifier 根据测试用例已有最终预期判定 App 行为
  -> 必要时启动只读 Verification Runner
  -> Attribution 与报告
```

必须始终保持：

1. 测试用例只写用户目标、按序步骤、测试数据和最终预期；不写固定坐标、设备 bounds、专用控件规则，也不为单个 App 增加绕过字段。
2. 原始 MobiAgent 仍负责 Decider/Grounder；评测层负责约束和审计，不能用另一个简化 locator 替代原始决策链。
3. Runner 的 `done` 只表示当前步骤或 GOAL 内部流程结束，不能代表 App 成功。
4. `ACTION_DISPATCHED`、`CONFORMANT`、`CONTINUE` 都不是 `APP_PASS`。
5. `APP_PASS` 只能由最终预期的充分、直接、可追溯 App 证据产生；历史文本、模型自述、单次点击和停留页面都不够。
6. 证据不足必须为 `INCONCLUSIVE`；执行偏离归 `TEST_EXECUTION_FAIL`；登录、网络、权限、设备等阻断归 `ENV_BLOCKED`；步骤正确但最终结果明确缺失才是 `APP_FAIL`。
7. 已派发的写操作、`INPUT` 和 GOAL 内部副作用 micro-action 不得整体重试或重派发。派发边界未知时记录 `DISPATCHED_UNCERTAIN`，停止为 `INCONCLUSIVE` 或相应安全结果。
8. 只读 Verification Runner 只能观察、等待、有限导航、刷新或滚动，不能发送、发布、删除、点赞、支付或重复业务动作。
9. 没有用户在当前窗口再次明确授权时，不得执行真实设备的发送、发帖、发布、删除、点赞、支付等副作用操作。之前窗口的授权不自动延续。

## 3. 主要代码入口

- `app_test_agent/schema.py`、`contract.py`：测试用例和最终预期契约。
- `app_test_agent/mobiagent_executor.py`：按用户步骤驱动原始 MobiAgent，记录模型决策、动作、派发边界和观察。
- `app_test_agent/step_gate.py`：步骤顺序、目标角色、动作符合性、post-observation、重试和停止判定。
- `runner/mobiagent/mobiagent.py`：Decider/Grounder、截图和 hierarchy、坐标转换、目标对齐、HarmonyOS/Android 动作派发。
- `app_test_agent/app_verifier.py`：最终 App 行为断言。
- `app_test_agent/verification_runner.py`：必要时执行只读验证。
- `verification_benchmark/`：CLI、离线 trace 重放、报告、归因和 Formal acceptance。
- `examples/`：用户视角测试用例。
- `tests/test_app_test_agent.py`：主要回归测试，必须优先使用最小合成 hierarchy 和假设备，不把完整截图或聊天内容放进测试资产。
- `report/modern-sjtu-report/main.typ`：Typst 报告正文，已按 `D:\Lab\modern-sjtu-report\main.typ` 的模板导入、字体链、封面和页眉布局对齐。

## 4. 已完成的关键修复

最近几次真机试点暴露过这些问题，修复时要继续坚持泛化规则：

- 小红书聊天输入：错误 Grounder 框落在普通聊天容器时，`INPUT` 必须被真实输入角色检查阻断；RichEditor 可通过可见、enabled、唯一且语义充分的通用候选恢复；多个候选或没有输入角色时 fail closed。
- executor 审计：已有 `rejection_reason` 不得被普通 Column/ListItem/NodeContainer 的 direct hit 覆盖；原始 alignment audit 和 hierarchy hit-test audit 要分字段保留；明确拒绝是决定性结果。
- 输入后证据：不能只凭 action 参数中的文本确认成功。post-observation 必须确认精确文本位于可编辑节点或存在明确的一致控件证据；底层调用之后发生异常时仍需保存 attempt、文本 hash、dispatch 状态和不重试事实。
- 历史消息与 `done`：带旧时间戳的历史气泡不能证明本轮发送；发送是原子 CLICK，模型 `done` 不能代替发送动作。
- Grounder 框格式：已统一处理 list/tuple XYXY、命名 XYXY、命名 left/top/right/bottom 和合法 x/y/width/height 对象；数值字符串也要在进入坐标运算前归一化。未知形状必须失败关闭。
- 普通视觉创建按钮：只有测试步骤明确要求创建/新增、候选唯一、可见、enabled、紧凑且语义充分时才允许通用视觉回退；多个候选必须拒绝。

这些规则没有写入小红书或网易云坐标，也没有加入应用专用控件分支。应用名在 runner 中只用于启动包名适配，不得扩展为评测定位逻辑。

## 5. 已完成的真实测试

### 小红书消息发送

HarmonyOS 设备上完成过真实用例“给青文发送消息”。修复后的成功记录包括 5 个动作：输入一次、发送一次，最终观察到带本轮时间标记的新消息气泡，结果为 `APP_PASS / APP_BEHAVIOR / COMPLETED`。

### 网易云音乐发布笔记

用例：`examples/cloudmusic_create_note_app_test.json`。已完成真实流程：进入“我的”、打开笔记、创建笔记、填写标题 `TEST`、填写正文 `评测智能体`、点击发布。最终成功记录有 7 个动作，标题和正文各输入一次，发布只点击一次，两个最终断言均满足，结果为 `APP_PASS / APP_BEHAVIOR / COMPLETED`，没有启动 Verification Runner。

这次试点曾暴露两类问题：合法的嵌套 XYWH 框未归一化导致 `TypeError`；设备残留在“我的-笔记”页面时，模型返回 `done`，但 Step Gate 正确拒绝了未完成的导航步骤。修复和状态恢复后才完成最终验收。不要因为这条成功记录就加入网易云专用坐标或按钮规则。

## 6. 测试与执行顺序

新增测试用例时按以下顺序工作：

1. 先阅读相近的 `examples/*.json` 和 schema，保持用户视角描述。
2. 只写用户能观察和验证的最终预期；不要新增坐标、逐步 `expected_after` 或固定 Verification Runner 路线来规避实现问题。
3. 先运行 schema、Mock、manifest replay 和最小合成 hierarchy 测试。
4. 对每个新边界补测试：正确目标、普通容器误命中、多个候选、层级缺失、派发后异常、历史匹配、`done` 早到、证据不足和不重试。
5. 运行定向测试，再运行全量 `pytest -q`；基线不得低于 238 passed。
6. 运行 Formal acceptance，确认七个 runtime prompts、源码 Harmony 环境、打包 Harmony 环境和冻结 trace cohort 都通过。Formal 只证明试点就绪，不等于商业 App 已验收。
7. 只有用户在当前窗口明确选择设备、账号、内容并授权后，才可执行真实副作用用例；先 preflight，再执行，并把输出写到新的独立目录。
8. 真实结果必须同时检查 `report.md`、`execution_result.json`、`app_behavior_result.json`、`attribution_result.json`、`actions.json`、hierarchy、截图和 attempt audit。
9. 成功运行先放入新的 `D:\Lab\mobiagent_archive\real_traces\successful_YYYYMMDD`；失败和不确定运行放入对应 `non_success_YYYYMMDD`，不要散落在 `D:\Lab` 根目录。
10. 每次真实运行后更新 Typst 报告的测试记录，但不要把完整截图、聊天正文或密钥复制进仓库。

常用离线命令：

```powershell
conda activate mobiagent-e2e
Set-Location D:\Lab\MobiAgent-verifier-enhanced
$env:PYTHONPATH = (Get-Location).Path
pytest -q
.\verify_pc_release.ps1 -AcceptanceLevel Formal -DeviceProfile harmony -DeviceSerial 5ZU0226122004500 -RealTraceAssetRoot D:\Lab
```

若需要模型服务，只能从本机密钥文件读取，不能输出密钥内容；模型探测必须是用户明确要求的只读 probe。真实设备命令必须显式带 `--execute-runner`，否则只能预检。

## 7. 当前报告任务

继续使用精炼、理性、实践学生视角写报告，少用空泛宣传语。重点说明：

- 为什么把动作执行、Step Gate 和 App Verifier 分开；
- Decider/Grounder 如何被复用，Step Gate 如何保护顺序、目标和副作用；
- 最终预期如何在观察窗口内得到证据；
- 真机遇到的输入错位、历史消息误判、状态残留和 bbox 类型问题；
- 修复为什么是角色、语义、唯一性、可见性和派发审计规则，而不是某个 App 的单一脚本；
- 每条真实用例的动作数、输入/发布次数、最终断言、结果和证据路径。

报告使用：

- 源文件：`D:\Lab\MobiAgent-verifier-enhanced\report\modern-sjtu-report\main.typ`
- 模板参考：`D:\Lab\modern-sjtu-report\main.typ`
- 最新编译 PDF：`D:\Lab\mobiagent_report_20260901_template_aligned.pdf`

模板字体链已对齐；本机缺少 `Kaiti SC`、`Songti SC` 时允许 Typst 使用 `KaiTi`、`Noto Serif SC`、`SimSun` 回退。不要为了消除字体警告改成与模板无关的字体体系。

## 8. 运行材料归档

本次已将 `D:\Lab` 根目录的运行材料整理为：

- `D:\Lab\mobiagent_archive\real_traces\successful_20260901`：9 个成功运行目录；
- `D:\Lab\mobiagent_archive\real_traces\non_success_20260901`：38 个明确失败、环境阻断或证据不足的目录，暂作可恢复归档；
- `D:\Lab\mobiagent_archive\real_traces\unclassified_20260901`：1 个没有标准报告的旧目录，待后续确认；
- `D:\Lab\mobiagent_archive\pc_acceptance_20260901`：13 个 PC acceptance/replay 输出目录；
- `D:\Lab\mobiagent_archive\standalone_evidence_20260901`：设备截图、hierarchy 和零散诊断文件；
- `D:\Lab\mobiagent_archive\reports_20260901`：旧版报告 PDF。

本次只做了移动归档，没有删除失败证据，以免丢失后续解释 bug 的材料。今后新增运行目录统一使用明确的日期、用例和目的命名，验收后再归档。

## 9. 交付前检查清单

- [ ] `git status` 中没有误改用户文件。
- [ ] 没有读取或输出 `api-key` 内容。
- [ ] 没有提交截图、trace、构建产物、模型原文或 `报错信息截取`。
- [ ] 测试用例没有固定坐标或 App 专用 locator。
- [ ] `done` 没有绕过 App Verifier。
- [ ] 已派发副作用没有重试。
- [ ] 证据不足没有被强行判成 `APP_PASS` 或 `APP_FAIL`。
- [ ] 定向测试、全量测试和 Formal acceptance 结果已记录。
- [ ] 报告已更新真实结果、根因、修复边界和证据路径。
- [ ] 只有用户本窗口明确授权后才执行真实发送、发布等副作用。
