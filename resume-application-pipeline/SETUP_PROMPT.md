请克隆并实际配置这个私有仓库：
<REPOSITORY_URL>（粘贴时替换为当前私有仓库地址）

目标：完成当前机器上的安装、配置和离线验证，使现有简历选择和 Claude CLI Applier 申请 pipeline 做好运行准备。不要只提供说明，不要启动真实申请或提交。

1. 检查 GitHub 权限并克隆；已有 checkout 先检查修改，避免覆盖。阅读 README.md、PRIVACY.md、AGENTS.md、GLOSSARY.md 和 docs/pipeline.md。
2. 检查 Python 3.11+ 和 `claude` CLI。新申请只使用现有 PDF，不需要 Word 或文档生成。不修改已有凭据。
3. 创建独立 .venv，安装 requirements.txt 并验证 `python -m pipeline.cli --help`。配置 Git 忽略的 resume-catalog.json，显式列出已有 PDF。不要扫描并自动启用所有简历，不把带占位符或工作笔记的草稿当作可上传简历。
4. Jobfeed 仍是外部依赖，默认 ../jobfeed，需要数据库、API 和正式 apply CLI。先检查本机已有项目；缺失源码或权限时明确询问，不造假接口或数据库。等待时继续独立工作。
5. 按 examples 创建 Git 忽略的本地资料，集中列出需要用户提供的事实；bases 都是虚构示例，不能用于真实申请。秘密不得出现在聊天、日志或提交中。
6. 按 README 执行离线测试，对照 VALIDATION 区分已知回归与新的 setup 问题。修复必要问题，不删测试、不削弱授权规则来获得通过。验证个人配置和缓存均被 Git 忽略。
7. 记录依赖版本、执行命令、配置位置、验证结果及未完成项，不记录秘密。最终分别说明已就绪部分、阻碍和实际启动方法。

本次仅授权 setup 和离线验证。不启动 worker、不创建招聘账号、不派发申请、不发送对外消息、不点击 Submit。真实批次范围及提交权限须另行授权。
