TAB_PRESERVATION_RULE = """标签保留（用户最新要求，覆盖旧自动关页规则）：本岗位实际 Chrome 表单、结果和相关专属标签始终保持打开，包括成功提交、同步完成、岗位失效、暂停、失败和只读核验后的每一轮。仅用户另行明确要求关闭或明确重跑流程时按该次精确授权清理。不要关闭后重开，也不要把恢复 URL 当成恢复未保存表单。
每轮结束前，完成最后一次表单回读后，对本轮使用及本岗位已登记且仍存在的每个实际标签调用官方 tab.markDeliverable()；只读续轮也必须重新标记，旧轮标记和资源台账 state=preserved 都不能替代本轮调用。可用 autofill/preserve-tabs.mjs 的 preserveApplicationTabs 批量标记并保存 tab-preservation.json。使用实际 browser ID、tab ID、当前 URL 和本轮 owner/turn 身份；不得从 URL 猜标签。工具不可用或标记失败时保留原页，明确报告失败，不声称已保留，不因此重填、提交或打开替代页。最后结果附实际 tab ID 和标记结果。"""


def application_finished(job):
    return bool(job.get('terminal') or job.get('submitted') and job.get('synced'))
