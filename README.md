# MobiAgent Verification

面向 Android / HarmonyOS 应用的端到端评测智能体。它执行结构化测试用例、记录可复核的设备轨迹，并依据终态证据给出 `APP_PASS`、`APP_FAIL` 或 `INCONCLUSIVE`。

## 评测闭环

```text
测试用例 JSON → 契约编译 → MobiAgent Runner 执行业务动作
                                      ↓
                           截图 + Harmony UI 层级 + 动作记录
                                      ↓
                      时序离线验证器 → 评测报告与可追溯证据
```

- `app_test_agent/`：用例契约、步骤编排、设备执行和证据汇总。
- `runner/mobiagent/`：模型决策、坐标转换、控件对齐与动作派发。
- `verification_benchmark/`：时序验证、规则检查、报告生成和命令行入口。
- `examples/`：可直接执行的应用测试用例。

真实设备运行时，每个步骤都保存动作前后状态。发布等终态步骤保留延时观测窗口，验证器只以该窗口内的可见 UI 证据判定结果，不以模型的“完成”声明判定成功。

## 环境准备

要求：Python 3.10+、已安装项目依赖、已连接并可调试的 Android 或 HarmonyOS 设备，以及 OpenAI 兼容的视觉模型服务。

```powershell
$root = "D:\Lab\MobiAgent-verifier-enhanced"
Set-Location $root
$env:PYTHONPATH = $root

python -m venv .venv
& .\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt

# 不要将密钥写入文件或提交到 Git。
$env:MOBIAGENT_API_KEY = "<your-api-key>"
$env:MOBIAGENT_BASE_URL = "https://<your-openai-compatible-endpoint>/v1"
$env:MOBIAGENT_MODEL = "<your-vision-model>"
```

HarmonyOS 真实执行还需要已可用的 `hdc`、设备序列号和应用登录态。涉及发帖、发送消息或发布笔记的用例会产生真实业务副作用，请使用测试账号。

## 运行测试

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
| `minimal_user_view_app_test.json` | 最小只读用例 |

新增用例时，至少定义：`test_case_id`、初始状态、按序 `steps`、每步后的预期上下文，以及 `expected_results`。对有副作用的操作，应使用唯一测试文本，并把最终断言绑定到发布后的界面证据。

## 输出与判读

`--output-dir` 下的关键文件：

| 文件/目录 | 内容 |
| --- | --- |
| `report.md` | 最终结论与断言结果 |
| `execution_result.json` | 步骤执行是否按序完成 |
| `app_behavior_result.json` | 业务结果断言 |
| `mobiagent_step_trace/` | 截图、原始 UI 层级和 `actions.json` |
| `business_offline_review.json` | 时序证据、命中帧与诊断信息 |

优先查看 `report.md`：

- `APP_PASS`：所有业务断言在规定观察窗口内得到证据支持。
- `APP_FAIL`：步骤失败、顺序不符或终态断言未满足。
- `INCONCLUSIVE`：轨迹、设备状态或观察证据不足，不能可靠判定。

## 安全与版本控制

密钥只通过环境变量或本机受保护的密钥文件提供。`docs/`、`tests/`、运行产物、设备截图、构建缓存和密钥文件均不进入发布分支；`README.md` 是唯一随源码发布的说明文档。
