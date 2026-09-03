# MobiAgent 应用功能评测工具

这是一个面向 Android 与 HarmonyOS 应用的自动化评测工具。它根据测试用例调用视觉模型完成页面操作，再用页面状态、操作轨迹、截图和可选的只读复核步骤判断目标功能是否完成，并在输出目录中生成可追溯的评测报告。

运行时，模型负责理解当前页面并提出下一步操作；执行器将操作发送到已连接的设备；步骤校验和应用校验器确认每一步及最终业务结果。对发帖、发送消息等会改变应用数据的用例，客户端会要求用户确认。评测结束后可在输出目录查看 `report.md`（摘要）和 `report.json`（完整结果）。

## 安装

需要 Python 3.10 或更高版本。

```powershell
git clone https://github.com/ZengXiaoqian7/Mobiagent-verification-v2.git
Set-Location .\Mobiagent-verification-v2

python -m venv .venv
& .\.venv\Scripts\Activate.ps1
python -m pip install -r requirements-runtime.txt
```

HarmonyOS 设备需要已安装 Harmony SDK，并让 `hdc` 可在命令行中使用；Android 设备需要安装 platform-tools，并让 `adb` 可在命令行中使用。

在启动前配置模型服务。密钥文件不要放入项目目录。

```powershell
$env:MOBIAGENT_API_KEY_FILE = "C:\secure\mobiagent-key.txt"
$env:MOBIAGENT_BASE_URL = "https://<兼容 OpenAI 的服务地址>/v1"
$env:MOBIAGENT_MODEL = "<视觉模型名称>"
$env:MOBIAGENT_WIRE_API = "responses"
$env:MOBIAGENT_DISABLE_RESPONSE_STORAGE = "true"
```

## 使用客户端

```powershell
python -m pc_client
```

客户端会自动发现已连接的 HarmonyOS 和 Android 设备。选择测试用例后，先运行设备预检；确认设备和测试内容无误后再开始真机评测。涉及发送、发布、删除等业务操作时，必须勾选确认选项。

评测结果写入所选输出目录，其中 `report.md` 用于阅读结果，`report.json` 包含完整结构化数据。

## 命令行运行

运行不连接设备的 Mock 示例：

```powershell
python -m verification_benchmark.tools.run_automated_evaluation `
  --app-test-case .\examples\post_create_app_test.json `
  --app-test-executor mock `
  --mock-scenario pass `
  --output-dir .\output\mock-run
```

对 HarmonyOS 设备执行预检但不操作设备：

```powershell
python -m verification_benchmark.tools.run_automated_evaluation `
  --app-test-case .\examples\xiaohongshu_publish_hello_world_with_runner_app_test.json `
  --app-test-executor mobiagent `
  --app-test-device Harmony `
  --app-test-device-serial "<设备序列号>" `
  --runner-root . `
  --output-dir .\output\preflight
```

仅在确认允许操作设备后，再附加 `--execute-runner` 参数。

## 打包 Windows 客户端

```powershell
python -m pip install -r requirements-package.txt
.\build_pc_client.ps1
```

生成的客户端位于 `dist\MobiAgentVerifierPC\`。
