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

HarmonyOS 真实执行还需要已可用的 `hdc`、设备序列号和应用登录态。涉及发帖、发送消息或发布笔记的用例会产生真实业务副作用，请使用测试账号。

可在不连接设备的情况下检查各运行环境：

```powershell
python -m verification_benchmark.tools.check_pc_environment --profile core
python -m verification_benchmark.tools.check_pc_environment --profile package
python -m verification_benchmark.tools.check_pc_environment --profile android
python -m verification_benchmark.tools.check_pc_environment --profile harmony
```

## 运行测试

### 一键 PC 验收

以下命令依次运行依赖检查、完整回归、源码 Mock、可用的本地真实轨迹基线、Windows 打包和打包后 Mock，全程不会操作真实设备：

```powershell
.\verify_pc_release.ps1
```

在没有真实轨迹的干净环境可使用 `-SkipRealReplay`；只做快速源码验收可再加 `-SkipBuild`。临时验证可通过 `-OutputRoot <path>` 与正式验收报告隔离。GitHub Actions 使用同一脚本和 `requirements-ci-lock.txt`，保证本地与 CI 执行入口一致。

### PC 客户端

Windows 下可直接双击 `launch_pc_client.bat`，或运行：

```powershell
python -m pc_client
```

客户端复用下文同一套评测内核，支持离线 Manifest 回放、Mock 自检、无设备操作的真机预检，以及需要二次副作用确认的真机执行。离线回放默认从 raw `actions.json` 与 observation frames 使用当前规则重算 Step Gate，不采信历史 gate 标签。密钥仍只从环境变量读取，客户端不保存密钥。

如需打包为 Windows 客户端目录，安装打包依赖后运行：

```powershell
python -m pip install -r requirements-package.txt
.\build_pc_client.ps1
```

构建产物位于 `dist\MobiAgentVerifierPC\`。

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
- `APP_FAIL`：业务步骤符合且环境正常，但完整观察窗口明确证明终态断言未满足。
- `TEST_EXECUTION_FAIL`：步骤动作、输入、目标或顺序不符合测试契约。
- `ENV_BLOCKED`：账号、权限、网络、设备或系统状态阻断测试。
- `INCONCLUSIVE`：轨迹、设备状态或观察证据不足，不能可靠判定。

## 安全与版本控制

密钥只通过环境变量或本机受保护的密钥文件提供。根目录 `tests/` 仅包含合成、可公开的回归测试并随源码发布；`docs/`、嵌套第三方测试目录、运行产物、设备截图、构建缓存和密钥文件均不进入发布分支，`README.md` 是唯一随源码发布的说明文档。
