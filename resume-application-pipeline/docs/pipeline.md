# 自动申请流水线说明

术语以根目录 [GLOSSARY.md](../GLOSSARY.md) 为准。本文件描述当前运行路径；与其他旧文档冲突时，以本文件和代码为准。

## 一句话

用户选定一批岗位（或 autopilot 连续批次自动纳入合格岗位），程序准备现有简历目录和岗位材料，为每个岗位启动一个 Claude CLI Applier。Applier 选择已有 PDF、填表，问题和阻碍经 Discord 直达用户；最终 Submit 需要用户本人确认或用户开启的自动提交模式。

## 组成

| 部分 | 代码 | 做什么 |
|---|---|---|
| watch worker | `pipeline/worker.py`（`OrchestratorWorker`） | 主循环：准备现有简历材料、开始和续跑 Applier、同步和收尾 |
| supervisor | `pipeline/worker_supervisor.py` | `watch --batch --progress-output` 先起 supervisor，它再以 `--worker-child` 起真正的 worker；worker 意外退出最多自动重启 3 次；交接记录写着 `stopped_by_user` 时不重启 |
| 批次 | `pipeline/batch.py`（`BatchPipeline`） | 读批次清单（`.pipeline/batches/<名字>.json`）：候选岗位、并发、目标数、现有简历目录 |
| 通用流程 | `pipeline/orchestrator.py`（`ApplicationPipeline`） | 队列、岗位材料、Applier 开始和同步、Jobfeed 对账、提交后收尾、取消 |
| Applier | `pipeline/claude_dispatch.py`、`claude_applier.py`、`applier_mcp.py`、`applier_supervisor.py`、`applier_runtime.py` | 启动和恢复 Applier，提供它的工具，记录问题、回读和提交，空闲停放与重启 |
| Discord | `pipeline/applier_discord.py` | 把问题、阻碍和审阅摘要发到岗位线程，接收用户回复和 `submit` |
| 验证码与标签丢失 | `pipeline/captcha_holds.py`、`pipeline/requeue.py` | 暂挂或退回 Queued，见 `AGENTS.md` 第 2 节 |
| 模型 | `pipeline/model_routing.py` | `STRONG_ATS_HOSTS` 里的复杂招聘系统从强模型开始 |
| 巡检 | `pipeline/progress_monitor.py`、`claude_progress.py` | 只读汇总岗位事实（队列、Applier 记录、回执、Jobfeed），写 `monitor.json`、看板和报告 |
| 阻碍台账 | `pipeline/blocker_ledger.py`、`decision_queue.py`（`.pipeline/blockers.sqlite3`） | 判断和去重阻碍；Dashboard 显示它的计数，Dashboard Submit 核对它登记的批次范围 |
| Jobfeed | `pipeline/jobfeed.py`、`store.py` | 读 `../jobfeed/data/jobfeed.sqlite`；`application_queue` 表记录每个岗位的状态；只通过 `jobfeed apply/mark` 子进程写「已投递」 |

## 一个岗位的经历

Queued → Resume Ready（岗位和可选简历材料已准备）→ Applying（Applier 选择已有 PDF、填表、按授权提交）→ Already Applied。

Resume Ready 是保留的历史状态名称，不表示已制作新简历，也不表示最终提交获得授权。Dispatching、Tailoring、Tailoring Paused 和 Deferred 记录只供历史核查，不再派发。状态定义在 `pipeline/lifecycle.py`。

途中可能进入：Submission Unknown（提交结果不明，禁止重投）、Cancelled（用户要求跳过）、Unavailable（岗位已关）。

提交成功后，worker 正式同步 Jobfeed「已投递」并写 `.pipeline/submission-closeouts/<岗位>.json`。用户自己提交的岗位，写 `applications/<岗位>/user-submission-confirmation.json` 记录，不再重投。

## 运行

Dashboard 的 **Start with Claude** 和 autopilot 服务都用这条命令启动 worker：

```
.venv/bin/python -u -m pipeline.cli watch --batch .pipeline/batches/<名字>.json \
  --application-workers 4 \
  --progress-output monitor-output/<名字> --progress-local-only
```

- **停止**：对 worker 进程发一次 SIGINT，等它排空在途工作后退出。发信号前先看 `monitor-output/<名字>/worker.log` 末尾，已经有 `orchestrator_draining` 就不要再发第二次。要彻底停一批，把交接记录的状态改成 `stopped_by_user`，supervisor 就不会再拉起。
- **排空 Applier**：`python -m pipeline.claude_cutover drain` 停止新启动，停下在安全点等待的 Applier；`resume` 恢复。见 `../docs/claude-cli-cutover-checklist.md`。
- **改批次清单**：worker 会自动重新读取，不用重启。
- **看进度**：Dashboard 的 Runs，或 `monitor-output/<名字>/dashboard.html`、`latest.md`、`blockers.md`。

## 文件地图

| 位置 | 内容 | 谁写 |
|---|---|---|
| `applications/<岗位目录>/` | 岗位材料、冻结的简历选择、独立上传 PDF、`resume-source.json`、申请答案、提交和用户确认记录 | Applier、程序 |
| `.pipeline/batches/` | 批次清单和启动记录 | Dashboard、autopilot、用户 |
| `.pipeline/applier-runtime.sqlite3` | Applier 启动、问题、回复、回读和提交记录 | 程序 |
| `.pipeline/blockers.sqlite3` | 阻碍台账 | 程序 |
| `.pipeline/submission-closeouts/` | 提交后收尾记录 | 程序 |
| `.pipeline/worker-recovery/`、`*.lock` | 运行时状态 | 程序 |
| `monitor-output/<批次>/` | 巡检输出：`monitor.json`、看板、报告、`worker.log` | 程序 |
| `../jobfeed/data/jobfeed.sqlite` | 岗位库、`application_queue` 队列、已投递记录 | Jobfeed 和程序 |

## 规则写在哪

授权边界和收尾规则在本项目的 `AGENTS.md`；Applier 的实际提示词和工具在 `pipeline/claude_applier.py`、`pipeline/applier_mcp.py`；填表事实在 `application-profile.md`。

## 已知问题

- Dashboard 的 Rerun、重新启动和就绪检查仍走 Codex App Server（`application_rerun.py`、`application_restart.py`、`application_readiness.py`、`claude_codex_bridge.py`、`codex_app_server.py`），对 Claude Applier 的岗位不起作用，待移除或改写。
- 用户自己提交的岗位，内部队列记录不会自动改成已完成（Jobfeed 记录是对的）。
