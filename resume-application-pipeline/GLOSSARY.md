# 自动申请流水线

对选定的一批岗位，由 Claude CLI Applier 选择已有简历、填表，并在用户确认后提交。新申请不制作或改写简历。

## 角色

**Applier**：
负责某一个岗位申请的 Claude CLI 会话；同一岗位的后续处理都交回给它，它的启动 ID 记在队列的 owner 字段里。
_Avoid_：原 Agent、owner session、application chat

**worker**：
运行一个批次的程序进程（`pipeline.cli watch`）；它启动和同步 Applier、补 Jobfeed 同步，从不替 Applier 填表或点 Submit。
_Avoid_：controller、主控

## 授权

**批次**：
`.pipeline/batches/<名字>.json` 冻结的候选岗位、并发、目标数和简历目录；来自 Dashboard 的 Start with Claude，或 autopilot 连续批次。

**autopilot 模式**：
用户本人开启的长期授权，从每日扫描到申请；不改变 Submit 规则。

**自动提交模式**：
用户本人开启的长期 Submit 确认；每次只覆盖一次 Submit 尝试，有未关闭的问题、阻碍、待审答案或 Dashboard 警告时仍留给用户。

## 阻碍处理

**阻碍**：
某个岗位此刻不能往前走的一个具体原因，例如验证码、缺回执、要签仲裁协议。
_Avoid_：blocker、kind、category、blocked、issue、advice（指阻碍时）

**问题记录**：
阻碍台账里跟踪同一岗位、步骤、字段/条款/操作、具体缺口与适用范围的问题从出现到解决的那一条，带处理历史。
_Avoid_：issue、blocker record

**阻碍台账**：
记录所有问题记录的唯一地方（`.pipeline/blockers.sqlite3`）；阻碍的判断和去重只在这里发生，Dashboard 显示它的计数。
_Avoid_：blocker audit、必办清单

**CAPTCHA needed**：
因验证码暂挂的岗位；它不占名额，用户亲自完成验证后清除暂挂。
