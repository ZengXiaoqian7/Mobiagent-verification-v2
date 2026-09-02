#import "@preview/modern-sjtu-report:0.2.0": *
#import "@preview/cuti:0.4.0": fakeitalic, show-cn-fakebold
#import "@preview/lilaq:0.5.0" as lq

#let course-name = "课程实践报告"
#let course-name-en = "Course Practice Report"
#let experiment-name = "MobiAgent 移动应用评测智能体的设计与实现"
#let ident-color = "blue"
#let logo-path = "path/to/logo"
#let name-path = "path/to/name"
#let header-path = "path/to/header"
#let org-name = ""
#let info-items = (
  ([专#h(2em)业], [待填写]),
  ([学生姓名], [待填写]),
  ([学生学号], [待填写]),
  ([教#h(2em)师], [待填写]),
)
#let cover-fonts = ("Times New Roman", "Kaiti SC", "KaiTi", "Noto Serif SC", "SimSun")
#let article-fonts = ("Times New Roman", "Noto Serif SC", "Songti SC", "SimSun")
#let code-fonts = ("Consolas", "Ubuntu Mono", "Menlo", "Courier New", "Courier", "Noto Serif SC")

// 如需在仅有单字重的中文字体上模拟粗体，可取消下一行注释。
// #show: show-cn-fakebold

#make-cover(
  course-name: course-name,
  course-name-en: course-name-en,
  info-items: info-items,
  cover-fonts: cover-fonts,
  ident-color: ident-color,
  logo-path: logo-path,
  name-path: name-path,
  org-name: org-name,
)

#show: general-layout.with(
  ident-color: ident-color,
  header-logo: true,
  experiment-name: experiment-name,
  article-fonts: article-fonts,
  code-fonts: code-fonts,
  header-path: header-path,
)

#make-title(name: experiment-name)

= 摘要

我围绕移动应用中的“完成”判定，搭建了一个带过程审计的评测智能体。输入是一组用户视角的测试步骤，执行端仍由原始 MobiAgent Decider/Grounder 自主观察和操作；我在它外侧加入 Step Gate、App Verifier 和只读 Verification Runner，对动作顺序、目标、证据、观察窗口和重试安全性逐步检查。这样可以把“模型说做完了”和“应用确实完成了”分开记录。

目前项目已有 238 条测试通过，六条冻结真实 trace 全部重放通过，exact accuracy 为 1.0，false pass、false fail 和 attribution error 均为 0。2026 年 8 月 31 日至 9 月 2 日，我在 HarmonyOS 真机上完成了小红书消息发送、网易云音乐笔记发布、微信消息发送，以及小红书图文笔记发布并回访个人主页四类真实测试，结果均为 APP_PASS，并保留了动作、层级、截图和判定证据。报告重点记录系统的设计、实现过程，以及真机测试中暴露出的输入定位、历史消息误判和目标框格式问题。

= 一、问题定义与判定边界

== 1.1 从用户视角描述用例

测试用例只描述用户想完成的事情，例如打开小红书、进入联系人“青文”、输入“你好呀我是评测智能体”、点击发送，并验证会话中出现本步发送的消息。用例中保存包名、测试数据、步骤约束、最终预期和验证策略，执行过程中不写入固定坐标，也不写入某个应用的专用控件规则。

这样做有两个好处：测试语义可以跨设备和界面版本复用；评测系统可以检查模型的自主决策是否符合用户意图。屏幕坐标、XML bounds、模型置信度和验证路径都属于运行证据，不能替代用例本身。

== 1.2 结果语义

系统将执行状态、步骤合规性和应用行为分开保存。几个容易混淆的状态含义如下。

#figure(
  table(
    columns: (1.5fr, 3fr),
    inset: 7pt,
    align: left,
    [状态], [含义],
    [CONFORMANT], [当前动作符合步骤要求，但应用最终结果尚未确认。],
    [CONTINUE], [当前步骤可以继续执行，仍需后续动作或观察。],
    [APP_PASS], [最终预期已由应用侧直接、充分的证据确认。],
    [APP_FAIL], [应用状态明确与最终预期不符。],
    [INCONCLUSIVE], [证据不足，无法安全地判为成功或失败。],
    [TEST_EXECUTION_FAIL], [执行过程违反协议、无法继续或触发安全终止。],
  ),
  caption: [判定状态的边界],
)

done 只是原始 MobiAgent 的阶段信号。它不能直接代表 App 成功，也不能绕过 App Verifier。已派发的 INPUT、CLICK、GOAL 内部副作用动作不会因为模型再次输出同一意图而重复执行。

= 二、总体架构

系统的主链路为：用户视角测试用例 → TestCaseSpec / Contract → 原始 MobiAgent Decider/Grounder → MobiAgentStepExecutor → Step Gate（顺序、目标、动作、观察、重试）→ App Behavior Verifier（最终预期）→ 只读 Verification Runner（必要时）→ 归因与报告。

== 2.1 模块分工

#figure(
  table(
    columns: (2fr, 3fr),
    inset: 7pt,
    align: left,
    [模块], [职责],
    [schema.py / contract.py], [定义测试用例、步骤、动作类型、最终预期和验证策略。],
    [mobiagent\_executor.py], [驱动原始 MobiAgent，保存每次模型决策、Grounder 目标和派发边界。],
    [runner/mobiagent/mobiagent.py], [连接桌面端与 HarmonyOS 设备，完成截图、层级读取、点击和输入等运行时操作。],
    [step\_gate.py], [审核步骤顺序、目标角色、动作证据、观察窗口和重试条件。],
    [app\_verifier.py], [依据最终预期判定应用行为，不接受 done 作为成功证据。],
    [verification\_runner.py], [在需要时执行只读观察或读取层级，补充应用侧证据。],
    [attribution.py / reporting.py], [区分应用行为结果和执行归因，并输出可追溯报告。],
  ),
  caption: [主要模块及职责],
)

== 2.2 证据链

一次动作至少关联以下信息：模型决策、Grounder 目标、执行前观察、目标命中审计、实际派发记录、执行后观察和 Step Gate 判定。最终预期还要有独立的应用证据。任何一环缺失，都可能降低结果为 INCONCLUSIVE，不能由模型的文字结论补齐。

以输入动作举例，系统需要知道：模型要求输入什么文字、目标是否真的是可编辑节点、输入调用是否已经派发、调用后编辑器中是否出现精确文本，以及后续发送动作是否实际发生。这个链路能回答“模型想做什么”“设备做了什么”和“应用最后发生了什么”三个不同问题。

= 三、详细运行机制

== 3.1 合同加载与步骤执行

运行器先加载 TestCaseSpec，校验包名、步骤编号、允许动作、测试数据和最终断言。每一步都有独立的状态和尝试记录。执行器向原始 MobiAgent 提供当前屏幕与层级信息，由 Decider 决定动作，再由 Grounder 给出目标或区域。

Step Gate 在动作真正派发前检查：

+ 当前步骤编号是否正确；
+ 动作类型是否在允许集合内；
+ 目标是否满足语义和角色约束；
+ 坐标是否得到当前运行时几何证据支持；
+ 该动作是否已经派发，是否允许重试；
+ 动作后的观察窗口是否已经安排。

== 3.2 输入动作的安全定位

INPUT 的安全流程为：模型提出 click_input；在 hierarchy 中查找可见、enabled 的真实输入角色；直接命中，或对唯一且强语义匹配的输入节点做恢复；记录尝试、文本 SHA-256 和派发边界；点击编辑器并输入文本；采集 post-observation；确认精确文本位于编辑节点。

RichEditor 等可编辑节点会被统一识别为文本输入角色。目标想输入文字但命中普通聊天容器、列表项、按钮或其他非输入节点时，系统会在点击前阻断。若存在多个输入候选，也不会猜测。全局语义恢复只有在候选唯一、可见、enabled、角色明确且语义分数充分时才成立，同时把候选、分数、选择依据和拒绝原因写入审计。

== 3.3 Step Gate 与安全重试

重试只适用于尚未派发的安全失败，例如目标未命中、观察暂时不足或导航动作需要再次规划。若输入或其他有副作用的动作已经派发，后续结果即使未知，也会记录为 dispatched 或 uncertain，流程停止在 INCONCLUSIVE 或相应执行结果，不再重派相同动作。

观察窗口由步骤类型决定。导航后需要重新读取界面；输入后需要检查编辑器内容；发送后需要等待并检查新消息及其时间或状态证据。这样可以避免把执行前的旧画面误用为执行后的结果。

== 3.4 应用验证与只读运行器

App Verifier 只依据最终预期判定应用行为。对于“发送消息”这类预期，单独看到测试文本并不充分，因为文本可能来自历史消息、草稿或其他控件。验证器会结合会话上下文、消息结构、时间变化、发送后的观察帧和执行动作证据进行判断。

直接证据不足时，Verification Runner 只能进行只读检查，例如重新获取层级、截图或读取可见状态。它不能代替原始执行器发送消息，也不能用额外副作用来“试验”应用状态。

== 3.5 报告与归因

报告同时保存三类结论：

+ 应用行为：APP_PASS、APP_FAIL 或 INCONCLUSIVE；
+ 执行状态：COMPLETED、执行失败或安全停止；
+ 归因：结果来自应用自身行为，还是来自执行器、协议或证据问题。

这三类字段相互独立。例如一次输入定位错误可能得到执行失败；一次动作全部合规但应用没有出现预期结果，则应归入应用行为失败；模型输出 done 但没有发送动作时，不能生成应用成功。

= 四、搭建过程

我先在离线环境中固定协议和结果语义，再接入原始 MobiAgent，最后使用 HarmonyOS 真机验证。Git 提交记录基本反映了这个顺序。

#figure(
  table(
    columns: (1fr, 2fr, 3fr),
    inset: 6pt,
    align: left,
    [阶段], [代表提交], [完成内容],
    [0], [0c692e6], [建立 App Test Agent 基线。],
    [1], [11b684c], [冻结测试用例协议，明确用户视角输入。],
    [2], [9851403], [验证可替换执行器和控制流。],
    [3], [76d3b00], [加入步骤执行清单和 manifest。],
    [4], [71ac19d], [接入 MobiAgent 步骤前置适配。],
    [5], [f41492b、439da9a], [拆分 App Verifier，并加入证据感知判定。],
    [6], [d5ce815、de45a01、21d1e08], [加入小红书真实用例，完善 HarmonyOS 层级和视觉目标回退。],
    [证据与 PC 验收], [8823cf0 至 0caf357], [完善桌面验证器、回放证据、历史消息防误判、重试安全和 Formal 验收。],
    [输入安全与目标定位], [8183e4a、39f2acc、14fcc2a、c3bff1e], [收紧 HarmonyOS 输入目标、通用输入节点验证，并归一化 Grounder 的合法框格式。],
  ),
  caption: [从协议到真机验收的迭代过程],
)

实现上，PC 端负责测试合同、模型调用、设备控制、步骤审核和报告生成；HarmonyOS 端通过 HDC 与 hmdriver2 提供截图、层级和交互能力。Python 代码负责把这些环节串起来。离线测试使用最小化的合成 hierarchy 和固定 replay trace，重点验证判定边界；真机测试只用于确认设备上的实际 UI、层级和动作证据。

= 五、遇到的问题与修复

== 5.1 历史消息被当成新消息

早期运行中，模型看到会话里带有 07-24 23:53 时间戳的历史气泡，返回了 done。当时本步骤并没有独立的发送点击。Step Gate 拒绝了这个 done，所以最终没有产生 false pass，但这次运行暴露出模型观察和应用判定之间的边界：历史文本只能说明文本存在，不能证明本步发送已经完成。

修复后，最终断言保留 historical\_match\_not\_sufficient: true，并要求发送动作和发送后观察共同参与验证。send\_chat\_message 仍是原子 CLICK，模型的 done 不能替代这个动作。

== 5.2 错误输入框坐标落入聊天内容

失败 trace 中，Decider 给出的输入目标 bbox 是 [26,872,319,945]。按 resized-pixel 换算后得到 [52,1744,638,1890]，实际点击点为 [345,1817]。这一区域位于聊天内容中；当前 hierarchy 中真正的 RichEditor 是 [175,2220,756,2308]。结果是键盘没有打开，编辑器仍为空，后续动作也没有独立的发送点击。

离线调用对该目标的对齐结果为：target\_wants\_text\_entry=true，rejection\_reason=text\_entry\_target\_rejects\_non\_input\_node。

原来的 \_alignment\_rejection\_blocks\_click 没有把这个拒绝原因纳入阻断集合，错误点击因此仍然被派发。现在输入动作在派发前必须通过真实输入角色检查；明确拒绝、无候选和角色不符都会 fail closed。普通 Column、ListItem、NodeContainer 的命中只能作为反证，不能成为输入成功依据。

== 5.3 直接命中审计覆盖原始拒绝

执行器后续的 hierarchy hit-test 可能把普通节点命名为 direct_supported_hit，覆盖前面的 alignment rejection。这样报告表面上像是找到了支持目标，实际却丢失了最重要的反证。

修复后，原始 alignment audit 与后续 hierarchy audit 分字段保存。direct\_supported\_hit 只有在命中真实输入角色时才成立；明确拒绝在 \_xml\_hit\_test\_result\_is\_decisive 中被视为决定性结果，不能被普通节点命中覆盖。

== 5.4 输入调用异常的审计边界

HarmonyDevice.input 在无法安全确认时可能抛出异常，而异常有可能发生在动作记录追加之前。对于输入动作，系统现在先记录尝试编号、文本 hash、目标审计和派发边界，再执行底层调用；调用后无论成功、失败还是抛异常，都补写结果。这样可以区分“尚未派发，可以安全重试”和“已经派发但结果未知，必须停止重试”。

这些规则都是通用的，没有加入小红书坐标、联系人名称或专用控件分支。具体应用的层级语义只作为运行时证据参与判断。

= 六、真实测试记录

== 6.1 小红书发送消息

2026 年 8 月 31 日，我在 HarmonyOS 设备 5ZU0226122004500 上运行用例“给青文发送消息”。设备包名为 com.xingin.xhs\_hos，消息内容为“你好呀我是评测智能体”。这次运行使用修复后的输入定位逻辑，未使用固定坐标规则。

#figure(
  table(
    columns: (1fr, 2fr, 3fr),
    inset: 7pt,
    align: left,
    [步骤], [实际动作], [关键证据],
    [打开应用并进入消息], [open_app、click], [进入小红书消息页，并找到目标会话。],
    [定位联系人], [gui_task], [由原始 MobiAgent 在允许的微动作范围内导航。],
    [输入消息], [click_input], [选择唯一可见、enabled 的 RichEditor；实际点击 [465,2264]；输入后编辑器中出现精确文本。],
    [发送消息], [click], [实际点击 [949,1344]；发送后观察到带“刚刚”的新消息气泡。],
  ),
  caption: [小红书真实发送测试的动作与证据],
)

最终结果为 APP_PASS，执行状态为 COMPLETED，归因为 APP_BEHAVIOR。trace 顶层有 5 个动作记录：open_app、click、gui_task、click_input 和 click。输入动作只有一次实际派发；发送步骤虽然包含两次模型尝试，但只有一次实际发送点击，没有重复输入或重复发送。最终截图显示新出现的蓝色消息气泡，编辑器恢复为空状态。

本次 trace 的主要审计文件已归档至：D:/Lab/mobiagent_archive/real_traces/successful_20260901/xhs_chat_qingwen_20260831_live_final_after_fix/report.json、D:/Lab/mobiagent_archive/real_traces/successful_20260901/xhs_chat_qingwen_20260831_live_final_after_fix/mobiagent_step_trace/actions.json，以及其中的最终截图。

七月成功 trace 也验证了相同的用户路径：5 个动作，输入点击 [465,2264]，RichEditor bounds 为 [175,2220,756,2308]，输入后出现发送按钮，发送点击 [949,1344]，随后出现带“刚刚”的新消息气泡。两次运行的共同点是输入、发送和发送后观察都得到独立证据。

== 6.2 网易云音乐发布笔记

2026 年 8 月 31 日，我在同一台 HarmonyOS 设备上运行用例 cloudmusic-create-note-001，完成“进入我的页面、打开笔记、创建笔记、填写标题 TEST、填写正文 评测智能体并发布”的完整流程。运行前先将应用恢复到推荐页，避免上一次草稿留下的页面状态影响本次步骤。执行记录共有 7 个动作，标题和正文各输入一次，发布按钮只点击一次。

#figure(
  table(
    columns: (1.4fr, 2fr, 3fr),
    inset: 6pt,
    align: left,
    [阶段], [实际动作], [观察证据],
    [进入我的页面], [click], [通过当前层级和 Grounder 框定位底部“我的”。],
    [打开笔记并创建], [click、click], [进入“我的-笔记”页面，使用唯一的可见红色创建控件进入编辑页。],
    [填写标题和正文], [click_input、click_input], [标题和正文分别落在可编辑节点；输入后观察到 TEST 和评测智能体。],
    [发布], [click], [Grounder 给出目标框后，经层级证据对齐，实际点击 [948,166]。],
  ),
  caption: [网易云音乐真实发布测试的动作与证据],
)

这次测试前几轮没有直接成功。第一轮发布步骤中，Grounder 返回了合法的命名 XYWH 框，但执行器没有把对象框转换成统一的 XYXY，导致坐标运算抛出 TypeError，发布动作没有派发。修复后又遇到设备保留“我的-笔记”选中状态的问题，Step Gate 拒绝了模型在“打开笔记”步骤返回的 done，并要求恢复安全前态。重新运行后，模型完成了 7 个步骤，发布后的观察帧同时出现标题 TEST 和正文评测智能体。

最终结果为 APP_PASS，执行状态为 COMPLETED，归因为 APP_BEHAVIOR。两个最终断言均为 SATISFIED，直接应用证据已经充分，因此没有启动 Verification Runner。主要证据已归档至 D:/Lab/mobiagent_archive/real_traces/successful_20260901/cloudmusic_create_note_real_run_20260831_retest_fixed7/report.md 和 D:/Lab/mobiagent_archive/real_traces/successful_20260901/cloudmusic_create_note_real_run_20260831_retest_fixed7/mobiagent_step_trace/actions.json。

这次修复涉及 Grounder 框格式归一化、语义目标与层级节点对齐以及输入角色检查。代码只依赖通用的输入角色、可见性、enabled 状态、语义文本和候选唯一性，没有写入网易云页面坐标或专用控件名称。

== 6.3 微信发送消息：包名校正、目标对齐与端到端复测

2026 年 9 月 1 日，我在同一台 HarmonyOS 设备上自动运行微信消息发送用例。首次运行使用 Android 常见包名 #raw("com.tencent.mm")，HDC 返回设备没有该包，因此在任何模型决策、输入或点击派发之前停止，结果为 TEST_EXECUTION_FAIL / EXECUTOR / 0/4。只读查询设备安装包后确认实际 HarmonyOS bundle name 为 #raw("com.tencent.wechat")；该次失败证据保留在 #raw("D:/Lab/mobiagent_archive/real_traces/non_success_20260901/wechat-send-hello-world-zzzz-001/")。

修正包名后，自动重试成功打开微信，原始 Decider/Grounder 提出了会话行点击。但坐标转换后的点击点在运行时 hierarchy 中命中了不带目标语义的列表行，点击后页面也没有保留目标会话上下文。Step Gate 因此以 NON_CONFORMANT 目标证据返回 INCONCLUSIVE，并且不重派发已经派发的导航点击。该次完成 1/4 步，只有打开应用和一次会话点击；没有输入、发送或最终断言证据，且 NEVER 策略使 Verification Runner 保持 NOT_RUN。最终结果为 INCONCLUSIVE / EVIDENCE，不归因为微信功能失败，也不能证明消息已发送。证据归档于 #raw("D:/Lab/mobiagent_archive/real_traces/non_success_20260901/wechat-send-hello-world-zzzz-001-retry1/")。

对原始 Grounder 输出、截图和 hierarchy 的离线复核显示：Grounder 的缩放坐标转换本身正确，但其框覆盖了目标会话下方的另一列表行。修复后的通用规则在派发前对唯一、可见且 enabled 的会话/联系人文字目标建立 identity guard：Decider 和 Grounder 仍分别提出动作与 bbox；只有其最终点击点落在 hierarchy 证明的可点击目标范围内才允许派发。否则在派发前拒绝，并只允许使用原有的安全重试预算。这一规则不依赖微信包名、联系人名称或固定坐标；对应的最小化合成测试和本次 trace 离线复演均证明原错误点击会被阻断。

修复后第三次全自动重跑完成 4/4 步并得到 APP_PASS / APP_BEHAVIOR / COMPLETED。运行时 identity guard 确认会话点击落在唯一目标的可点击范围内；随后在可编辑输入框中确认指定文本，再派发一次发送点击。发送后多个稳定观察帧持续出现预期消息，最终断言为 SATISFIED。Verification Runner 仍因用例 NEVER 策略保持 NOT_RUN。完整成功证据归档于 #raw("D:/Lab/mobiagent_archive/real_traces/successful_20260901/wechat-send-hello-world-zzzz-001-retry2/")。

== 6.4 小红书图文笔记发布与只读回访核验

2026 年 9 月 2 日，我在同一台 HarmonyOS 设备上从初始应用状态运行用例 #raw("xiaohongshu-publish-hello-world-runner-001")。用例要求依次打开小红书、点击底部加号、选择“写文字”、输入 #raw("hello world")、生成图片、确认配图并发布笔记；发布后必须启用 Verification Runner，进入“我”的笔记页确认新笔记可见。

#figure(
  table(
    columns: (1.5fr, 2fr, 3fr),
    inset: 6pt,
    align: left,
    [阶段], [实际动作], [关键证据],
    [创建图文笔记], [open_app、click、click、click_input], [进入发布入口与“写文字”编辑页；编辑器中出现精确文本 hello world。],
    [生成并发布], [click、wait、click、click], [生成入口、配图确认与发布动作均只派发一次；业务步骤完成 8/8。],
    [只读回访], [wait、navigate、navigate、refresh、observe], [Runner 依次进入“我”和“笔记”页、刷新并观察；5 个动作全部标记为 read_only_action。],
  ),
  caption: [小红书图文笔记发布及个人主页回访的动作与证据],
)

原始 Decider 在“打开生成入口”步骤曾一次输出 done 而尚未派发设备动作。Step Gate 将该情形识别为预派发失败并消耗安全重试预算，随后重新规划并完成该点击；由于第一次没有设备派发，未重放任何输入、确认或发布动作。该处理是对所有步骤通用的预派发重试规则，不含小红书控件、坐标或文案的特例。

最终结果为 APP_PASS，执行状态为 COMPLETED，归因为 APP_BEHAVIOR，最终断言 #raw("hello_world_visible_in_own_note_list") 为 SATISFIED。Verification Runner 状态为 COMPLETED：其终态 hierarchy 在“我”的笔记列表中同时出现 #raw("hello world") 与“刚刚”，并且完整观察窗口已采集。业务发布与后续核验在同一份测试契约下完成，Runner 没有进行发布、编辑、删除或账户变更。

主要证据已归档至 #raw("D:/Lab/mobiagent_archive/real_traces/successful_20260902/xiaohongshu-publish-hello-world-runner-001-clean-rerun1/report.md")、#raw("D:/Lab/mobiagent_archive/real_traces/successful_20260902/xiaohongshu-publish-hello-world-runner-001-clean-rerun1/mobiagent_step_trace/actions.json") 和 #raw("D:/Lab/mobiagent_archive/real_traces/successful_20260902/xiaohongshu-publish-hello-world-runner-001-clean-rerun1/mobiagent_verification_trace/verification_actions.json")。

== 6.5 当前验证结果

#figure(
  table(
    columns: (2fr, 3fr),
    inset: 7pt,
    align: left,
    [项目], [结果],
    [全量自动化测试], [238 passed],
    [六条冻结真实 trace], [6/6；exact accuracy = 1.0],
    [false pass / false fail / attribution error], [均为 0],
    [Formal PC acceptance], [PASS；7 个 prompts 已加载，HarmonyOS 源码和打包环境就绪],
    [小红书真实发送用例], [APP_PASS；APP_BEHAVIOR；COMPLETED],
    [网易云音乐真实发布用例], [APP_PASS；APP_BEHAVIOR；COMPLETED；7/7],
    [微信发送消息（2026-09-01）], [APP_PASS；APP_BEHAVIOR；4/4；输入与发送各一次，Verification Runner 未启动],
    [小红书图文发布与回访（2026-09-02）], [APP_PASS；APP_BEHAVIOR；8/8；Verification Runner COMPLETED，5 个只读动作],
  ),
  caption: [当前测试与验收结果],
)

六条冻结 trace 的总体指标已经稳定。每条用例的动作级审计、观察窗口和最终证据仍按 trace 保存，后续会继续把其他真实用例逐条补入本节。

= 七、局限与后续工作

当前实现依赖原始 MobiAgent 的目标规划质量，也依赖设备层级信息在运行时可用。商业应用界面可能出现异步加载、节点角色缺失、坐标缩放差异和历史状态残留。系统对此采取保守判定，因此少量证据不足的运行会停在 INCONCLUSIVE，需要人工复核。

报告目前详细记录了两条真实的副作用用例。后续工作包括：补充小红书其他冻结和真实用例的逐条记录；继续收集输入框、发送按钮和历史消息的跨版本层级样本；将更多异常路径加入最小化离线测试；在不增加真实副作用的前提下完善 Formal acceptance 的证据摘要。

= 结论

这个评测智能体的核心价值在于把一次模型驱动的操作拆成可检查的证据链。模型负责理解界面和提出动作，Step Gate 负责约束动作是否安全、是否符合当前步骤，App Verifier 负责确认应用最终状态。三者边界清楚后，done、普通节点命中和历史文本都不能单独制造成功结果。

我在这几次真机测试中遇到的输入错位、历史消息误判、状态残留和框格式错误，最终都通过通用规则、审计保留和 fail-closed 判定解决了。真机结果说明修复后的路径可以完成实际操作，同时没有牺牲离线回放的准确性。当前代码最后提交为 c3bff1e，报告将在后续真实测试补齐后继续更新。
