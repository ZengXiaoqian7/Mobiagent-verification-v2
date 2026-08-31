# MobiAgent Verification

面向 Android / HarmonyOS 应用的端到端评测智能体。它执行结构化测试用例、记录可复核的设备轨迹，并依据终态证据给出 `APP_PASS`、`APP_FAIL` 或 `INCONCLUSIVE`。

## 评测闭环

```text
用户视角测试用例 → 契约编译 → 原 MobiAgent Decider/Grounder → 设备动作
                                                        ↓
                                  Step Gate（顺序、动作证据、观察窗口、安全审计）
                                                        ↓
                                           App Verifier 检查最终预期
                                                        ↓ 证据不足时
                                      只读 Verification Runner → App Verifier 复核
                                                        ↓
                                            归因、报告与可追溯证据
```

- `app_test_agent/`：用例契约、步骤编排、设备执行和证据汇总。
- `runner/mobiagent/`：模型决策、坐标转换、控件对齐与动作派发。
- `verification_benchmark/`：时序验证、规则检查、报告生成和命令行入口。
- `examples/`：可直接执行的应用测试用例。

真实设备运行时，每个步骤都保存动作前后状态。发布等终态步骤保留延时观测窗口，验证器只以该窗口内的可见 UI 证据判定结果，不以模型的“完成”声明判定成功。Runner 的 `done` 只终止当前步骤或已确认的 GOAL；`ACTION_DISPATCHED`、`CONFORMANT` 和 Step Gate 的 `CONTINUE` 均不等于 `APP_PASS`。已派发的写操作、`INPUT` 或 GOAL 内部副作用动作不得整体重派发；证据不足必须返回 `INCONCLUSIVE`。

## 环境准备

要求 Python 3.10+。离线回放、Mock、报告生成和 PC 客户端不要求安装真机或大模型依赖；按实际用途选择依赖层：

| 依赖文件 | 用途 |
| --- | --- |
| `requirements-core.txt` | PC 核心、离线回放、Mock 与预检 |
| `requirements-test.txt` | 本地回归测试 |
| `requirements-ci-lock.txt` | Windows/Python 3.12 可复现 CI 与打包 |
| `requirements-package.txt` | 本地 PyInstaller 打包 |
| `requirements-device-android.txt` | Android 真机执行 |
| `requirements-device-harmony.txt` | HarmonyOS 真机执行 |

原 `requirements.txt` 保留为包含 OCR、RAG、模型与设备框架的完整研究环境，不建议仅为使用 PC 客户端而安装。

```powershell
$root = "D:\Lab\MobiAgent-verifier-enhanced"
Set-Location $root
$env:PYTHONPATH = $root

python -m venv .venv
& .\.venv\Scripts\Activate.ps1
python -m pip install -r requirements-test.txt

# 不要将密钥写入文件或提交到 Git。
$env:MOBIAGENT_API_KEY = "<your-api-key>"
$env:MOBIAGENT_BASE_URL = "https://<your-openai-compatible-endpoint>/v1"
$env:MOBIAGENT_MODEL = "<your-vision-model>"
```

对于声明 `wire_api = "responses"` 的 OpenAI-compatible 服务，MobiAgent 与
App Verifier/Verification Runner 共用以下显式配置：

```powershell
# 密钥文件可为单行裸密钥，或包含 OPENAI_API_KEY 的 JSON 对象。
# 推荐放在仓库外；仓库根目录的 /api-key 也已被 Git 精确忽略。
$env:MOBIAGENT_API_KEY_FILE = "D:\Lab\MobiAgent-verifier-enhanced\api-key"
$env:MOBIAGENT_BASE_URL = "https://api.horizon1123.top"
$env:MOBIAGENT_MODEL = "gpt-5.4"
$env:MOBIAGENT_WIRE_API = "responses"
$env:MOBIAGENT_REASONING_EFFORT = "high"
$env:MOBIAGENT_DISABLE_RESPONSE_STORAGE = "true"
```

Responses 模式会把原 MobiAgent 的文字/截图消息转换为 `input_text` /
`input_image`，使用 `max_output_tokens`，传递 reasoning effort，并默认发送
`store=false`。未设置 `MOBIAGENT_WIRE_API` 时仍使用原来的
`chat/completions`，不会改变已有部署。Codex 配置中的 `review_model`、
`network_access`、`features.goals` 和 WSL acknowledgement 不是 MobiAgent API
字段，不会复制进模型请求。Codex/OpenAI 登录凭据也不会自动暴露给本项目，
真机试点前必须由用户在启动 PC 客户端的同一会话中显式提供密钥。

HarmonyOS 真实执行还需要已可用的 `hdc`、设备序列号和应用登录态。涉及发帖、发送消息或发布笔记的用例会产生真实业务副作用，请使用测试账号。

可在不连接设备的情况下检查各运行环境：

```powershell
python -m verification_benchmark.tools.check_pc_environment --profile core
python -m verification_benchmark.tools.check_pc_environment --profile package
python -m verification_benchmark.tools.check_pc_environment --profile android
python -m verification_benchmark.tools.check_pc_environment --profile harmony
```

## 运行测试

### 一键 PC 离线验收与正式准备门禁

以下命令依次运行依赖检查、完整回归、源码 Mock、可用的本地真实轨迹基线、Windows 打包和打包后 Mock，全程不会操作真实设备。源码与冻结进程的 Mock smoke 都会实际读取七个原 MobiAgent runtime prompt，避免 Mock 成功掩盖打包资源缺失：

```powershell
.\verify_pc_release.ps1
```

默认命令属于 `Offline` 验收。在没有真实轨迹的干净环境可使用
`-SkipRealReplay`；只做快速源码验收可再加 `-SkipBuild`。只要本地存在受保护
trace，脚本就要求完整 cohort 全部可用、exact accuracy `1.0`，并要求 false
pass、false fail 和 attribution error 全部为零。

连接设备后的正式准备门禁使用：

```powershell
conda activate mobiagent-e2e
python -m pip install -r requirements-test.txt
python -m pip install -r requirements-package.txt
.\verify_pc_release.ps1 `
  -AcceptanceLevel Formal `
  -DeviceProfile harmony `
  -DeviceSerial "<hdc-device-serial>" `
  -ProbeModelService
```

`Formal` 禁止 `-SkipRealReplay` 和 `-SkipBuild`，要求显式设备序列号、完整真实
trace、源码目标 profile READY，并在构建后从冻结 EXE 内再次检查同一 profile。
它只证明正式真机试点的代码、依赖、设备和打包物已经就绪；摘要会明确写入
`live_commercial_acceptance=PENDING_USER_TRIGGERED_PILOT`，不会把未执行的商业
App 测试冒充最终验收。该门禁始终不操作设备；只有显式传入
`-ProbeModelService` 才发出一次最小纯文本模型请求，否则摘要记录
`model_service_probe=NOT_RUN`。探测报告不保存密钥或模型原文，只保留配置、
耗时、字符数和响应哈希；摘要仍记录
`device_interaction=CONNECTIVITY_CHECK_ONLY`。临时验证可通过 `-OutputRoot <path>` 与正式报告隔离。
GitHub Actions 仍运行可复现的 Offline 档，并明确跳过受保护 trace。

源码或冻结客户端都可通过以下无设备操作入口输出机器可读依赖报告：

```powershell
python pc_client_entry.py --check-environment harmony --output-dir .\environment_check
```

### PC 客户端

Windows 下可直接双击 `launch_pc_client.bat`，或运行：

```powershell
python -m pc_client
```

客户端复用下文同一套评测内核，支持离线 Manifest 回放、Mock 自检、无设备操作的真机预检，以及需要二次副作用确认的真机执行。离线回放默认从 raw `actions.json` 与 observation frames 使用当前规则重算 Step Gate，不采信历史 gate 标签。密钥仍只从环境变量读取，客户端不保存密钥。

真机执行期间，源码 CLI 会继续显示原 MobiAgent 的 Decider/Grounder 响应；无控制台的 PC 客户端会把同一结构化事件实时转发到日志框。每次模型请求、原始响应、解析校验、失败重试和耗时还会按 step/attempt 写入 `mobiagent_step_trace/model_events.jsonl`。事件不会主动复制请求 prompt、截图 payload、消息正文或 API key；完整模型响应保留用于本地调试和审计，因此模型自行复述的 UI 文本仍可能包含敏感信息，trace 必须按设备截图同级保护。

如需打包为 Windows 客户端目录，安装打包依赖后运行：

```powershell
python -m pip install -r requirements-package.txt
.\build_pc_client.ps1
```

构建产物位于 `dist\MobiAgentVerifierPC\`。`build_pc_client.ps1` 会在构建前校验七个必需 prompt，并把 `prompts/` 收集到冻结客户端；缺失或空 prompt 会使验收失败。

打包程序是否具备真机执行能力取决于构建环境：Android 需要 `uiautomator2`，HarmonyOS 需要 `hmdriver2`，模型调用需要 `openai`。缺少这些依赖时，离线回放、Mock 自检和真机预检仍可使用，但真机执行只能返回环境阻断；正式发布前应在依赖齐全的环境重新构建，并连接测试设备完成验收。

### 1. 本地冒烟测试

不访问真实设备，用于检查用例格式、编排和报告链路。

```powershell
python -m verification_benchmark.tools.run_automated_evaluation `
  --app-test-case "$root\examples\post_create_app_test.json" `
  --app-test-executor mock `
  --mock-scenario pass `
  --output-dir "D:\Lab\mobiagent_smoke"
```

### 2. 真实 Runner 预检

不加 `--execute-runner` 时，只生成并检查设备执行载荷，不会操作设备。

```powershell
python -m verification_benchmark.tools.run_automated_evaluation `
  --app-test-case "$root\examples\cloudmusic_create_note_app_test.json" `
  --app-test-executor mobiagent `
  --app-test-device Harmony `
  --app-test-device-serial "<device-serial>" `
  --runner-root $root `
  --output-dir "D:\Lab\mobiagent_preflight"
```

### 3. HarmonyOS 真机评测

```powershell
$deviceSerial = "<device-serial>"
$outputDir = "D:\Lab\cloudmusic_run"

python -m verification_benchmark.tools.run_automated_evaluation `
  --app-test-case "$root\examples\cloudmusic_create_note_app_test.json" `
  --app-test-executor mobiagent `
  --execute-runner `
  --app-test-device Harmony `
  --app-test-device-serial $deviceSerial `
  --runner-root $root `
  --output-dir $outputDir
```

`--execute-runner` 会实际操作设备；缺少该参数时不会触发业务动作。

## 测试用例

所有应用用例位于 `examples/`，每个文件是一个 `app-test-case-v1` JSON：

| 用例 | 目的 |
| --- | --- |
| `cloudmusic_create_note_app_test.json` | 创建并发布网易云音乐笔记 |
| `bilibili_create_post_app_test.json` | 创建哔哩哔哩动态 |
| `qq_send_hello_zhexi_app_test.json` | 发送 QQ 消息 |
| `xiaohongshu_*.json` | 小红书发帖、聊天与只读场景 |
| `harmony_probe_capability_app_test.json` | HarmonyOS 设备能力探测 |
| `xiaohongshu_readonly_search_app_test.json` | 小红书只读搜索入口与无文字控件定位试点 |
| `minimal_user_view_app_test.json` | 最小只读用例 |

新增用例时，至少定义：`test_case_id`、必要的初始状态、用户能够描述的按序 `steps`，以及最终可观察的 `expected_results`。测试作者不必提供坐标、控件 ID、逐步 `expected_after` 或 Verification Runner 路线；这些只能作为运行时内部证据。对有副作用的操作，应使用唯一测试文本，并把最终断言绑定到发布后的界面证据。

## 输出与判读

`--output-dir` 下的关键文件：

| 文件/目录 | 内容 |
| --- | --- |
| `report.md` | 最终结论与断言结果 |
| `execution_result.json` | 步骤执行是否按序完成 |
| `direct_app_behavior_result.json` | 启动 Verification Runner 前的直接 App 证据结论 |
| `app_behavior_result.json` | 业务结果断言 |
| `mobiagent_step_trace/` | 截图、原始 UI 层级和 `actions.json` |
| `mobiagent_step_trace/model_events.jsonl` | Decider/Grounder 请求、返回、显式 reasoning、校验、重试和耗时 |
| `business_offline_review.json` | 时序证据、命中帧与诊断信息 |
| `verification_runner_result.json` | 条件启动的只读验证轨迹、burst 与 attempt 审计 |
| `attribution_result.json` / `run_envelope.json` | 归因与端到端 hash/时序边界 |

优先查看 `report.md`：

- `APP_PASS`：所有业务断言在规定观察窗口内得到证据支持。
- `APP_FAIL`：业务步骤符合且环境正常，但完整观察窗口明确证明终态断言未满足。
- `TEST_EXECUTION_FAIL`：步骤动作、输入、目标或顺序不符合测试契约。
- `ENV_BLOCKED`：账号、权限、网络、设备或系统状态阻断测试。
- `INCONCLUSIVE`：轨迹、设备状态或观察证据不足，不能可靠判定。

## 安全与版本控制

密钥只通过环境变量或本机受保护的密钥文件提供。根目录 `tests/` 仅包含合成、可公开的回归测试并随源码发布；`PLAN.md`、`APP_TEST_AGENT_README.md` 与 `docs/STAGE4_MOBIAGENT_PREFLIGHT.md` 作为当前架构说明一并维护，其余 `docs/`、嵌套第三方测试目录、运行产物、设备截图、构建缓存和密钥文件不进入发布分支。

当前离线验收基线（2026-08-31）：`218 passed`；六条冻结真实 trace 为 `6/6`，exact accuracy `1.0`，false pass、false fail 和 attribution error 均为 `0`。这些结果不替代真实设备试点；商业 App 的写入、发送、发布或支付流程只能由用户在明确选择的测试账号和设备上触发。
