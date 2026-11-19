"""Token-free rendering. No browser, database writes, or model imports."""
from __future__ import annotations


def render_report(snapshot: dict) -> str:
    c = snapshot['counts']
    lines = ['# 申请进度', '', f"核验时间：{snapshot['checked_at']}",
             f"提交完成（含用户确认） {c['submitted']}/{snapshot['target']}；正式同步读回 {c['synced']}；关页完成 {c['closed']}。",
             f"潜在提交预约 {c['reserved']}；执行中或状态未知 {c['occupied']}；暂停 {c['paused']}。",
             '证据来自本地回执、正式 Jobfeed 读回及 session 事件；不是重新访问官网。', '']
    if snapshot.get('blockers'):
        lines += [render_blockers(snapshot['blockers']), '']
    if snapshot.get('gap'):
        lines.append('检查缺口：' + snapshot['gap'])
    if snapshot.get('errors'):
        lines += ['未核实来源：' + ', '.join(snapshot['errors']), '']
    if snapshot.get('coordination_error'):
        lines += ['资源协调记录未核实：' + snapshot['coordination_error'], '']
    lines += ['| 岗位 | 提交 / 同步 / 关页 | 原任务状态 | 最近实际工具活动 | 下一步 |',
              '|---|---|---|---|---|']
    def cell(value):
        return str(value or '未知').replace('|', '\\|').replace('\n', ' ')
    for j in snapshot['jobs']:
        phases = ' / '.join(('官网回执' if j.get('submitted') else '用户确认' if j.get('user_submitted') else '未确认', *('已确认' if j[k] else '未确认' for k in ('synced', 'closed'))))
        lines.append('| ' + ' | '.join(map(cell, [f"{j['job_id']} {j['company']} · {j['title']}", phases,
                       ('旧批收尾；' if not j['counted'] else '') + ('官网已关闭；' if j.get('terminal') else '') + ('暂停；' if j['paused'] else '') + {'active': '执行中', 'ended': '本轮结束', 'interrupted': '已中断', 'unknown': '未核实', 'not_started': '未启动'}.get(j['turn_state'], j['turn_state']), j.get('last_tool_at'), j['next_step']])) + ' |')
    if snapshot.get('usage'):
        u = snapshot['usage']
        lines += ['', f"已读取申请 session 用量：{u['requests']} 次请求，{u['total_tokens']} tokens（输入 {u['input_tokens']}，其中缓存 {u['cached_input_tokens']}；输出 {u['output_tokens']}）。",
                  '此项为已读取 session 的累计用量，不包含未接入的主控、Claude 或工具费用；reasoning 已包含在输出。']
    lines += ['', '待交付事件：' + str(snapshot.get('pending_notifications', 0)),
              '普通巡检模型调用：0；重要事件交付若启用会调用模型，费用另计。',
              '报告已生成不等于聊天已送达或用户已读。若面板没有刷新，请重新打开本文件。', '']
    return '\n'.join(lines)


def render_blockers(board: dict) -> str:
    """阻碍台账 board: 待用户处理, the 待办 queue and records waiting for a result."""
    def where(item):
        if item.get('job_id') is None:
            return '系统'
        return f"{item['job_id']} {item.get('company') or ''} · {item.get('title') or ''}"
    lines = ['# 阻碍台账', '', '## 待用户处理']
    lines += [f"- {where(i)}：{i['label']}；{i.get('evidence') or '无说明'}；原会话 {i.get('thread') or '无'}；通知于 {i.get('notified_at') or '未通知'}"
              for i in board['parked']] or ['- 无']
    lines += ['', '## 待办队列（主控按先进先出领取）']
    lines += [f"- 编号 {t['id']} {'已领取' if t['status'] == 'claimed' else '未领取'}：{where(t)}：{t['label']}（第 {t['attempt']} 次）；{t.get('evidence') or '无说明'}"
              for t in board['queue']] or ['- 无']
    lines += ['', '## 等结果（主控已处理，程序复核中）']
    lines += [f"- {where(i)}：{i['label']}；处理于 {i.get('done_at')}；{i.get('evidence') or '无说明'}" for i in board['waiting']] or ['- 无']
    return '\n'.join(lines) + '\n'


def notification_text(events: list[dict], snapshot: dict) -> str:
    lines = []
    for event in events:
        if event['kind'] == '待办唤醒':
            lines.append(event['detail'])
        else:
            job = event.get('job_id')
            where = f'岗位 {job}' if job is not None else '整批'
            lines.append(f"- {event['kind']}：{where}；{event['detail']}")
    return '\n'.join(lines)


def render_readiness(readiness):
    from html import escape
    def esc(value):
        return escape(str(value if value is not None else '未记录'))
    readiness = readiness or {}
    if readiness.get('resume_mode') == 'selected_existing':
        catalog = readiness.get('resume_catalog', {})
        choices = ', '.join(str(e['label']) + ' (' + str(e['id']) + ')' for e in catalog.get('resumes', []))
        return ('<section class="readiness" aria-labelledby="readiness-title"><h2 id="readiness-title">环境与验证记录</h2>'
                '<dl class="readiness-list"><div><dt>现有简历目录</dt><dd>' + esc(catalog.get('state')) + '</dd></div>'
                '<div><dt>可选简历</dt><dd>' + esc(choices or catalog.get('error') or '尚未配置') + '</dd></div>'
                '<div><dt>执行方式</dt><dd>Claude CLI Applier · 原申请会话选择现有 PDF</dd></div>'
                '<div><dt>连接与申请验证</dt><dd>未在此面板测试。目录存在不代表已登录、已提交或已同步。</dd></div></dl></section>')
    return ''


def render_dashboard(snapshot, controls=None):
    """Render saved evidence. Optional controls can only request a batch stop."""
    from html import escape
    import json
    def esc(value):
        return escape(str(value if value is not None else '未知'))
    c = snapshot['counts']
    fixture_notice = '<p class="fixture-notice" role="note">合成测试面板。岗位、安装与测试记录均为演示数据。</p>' if snapshot.get('fixture_only') else ''
    rows = []
    for job in snapshot['jobs']:
        state = '岗位关闭' if job.get('terminal') else job.get('queue_state') or job.get('turn_state')
        owner = job.get('owner')
        chat = esc(owner) if owner else '未绑定会话'
        tabs = [f"{t.get('browser', t.get('browser_id', '?'))}/{t.get('tab_id', '?')} · {t.get('state', t.get('status', '?'))}" + (f" · popup of {t['opener_tab_id']}" if t.get('opener_tab_id') else '') for t in job.get('tabs', [])]
        claims = [f"{r.get('kind', '?')} · {r.get('state', r.get('status', '?'))}" for r in job.get('resource_claims', [])]
        claims += [f"等待 {r.get('kind', '?')} · {r.get('state', r.get('status', 'waiting'))}" for r in job.get('resource_waits', [])]
        resources = '<br>'.join(esc(v) for v in tabs + claims) or '无已记录资源'
        usage = job.get('usage', {})
        metrics = (str(usage['total_tokens']) + ' tokens' if 'total_tokens' in usage else '用量未接入')
        elapsed = job.get('elapsed_seconds')
        metrics += ' · ' + (str(elapsed) + ' 秒' if elapsed is not None else '时长未知')
        evidence_path = job.get('evidence_path')
        evidence = ('<details class="evidence-path"><summary>查看证据路径</summary><code>' + esc(evidence_path) + '</code></details>') if evidence_path else '无回执路径'
        activity = job.get('last_tool_at')
        activity_html = '<time title="' + esc(activity) + '">' + esc(str(activity).replace('T', ' ', 1)) + '</time>' if activity else '未知'
        cells = [esc('本批' if job['counted'] else '旧批收尾'), esc(job['job_id']),
                 esc(str(job['company']) + ' · ' + str(job.get('title', ''))), esc(state),
                 esc(' / '.join(('官网回执' if job.get('submitted') else '用户确认' if job.get('user_submitted') else '未确认', *('已确认' if job[k] else '未确认' for k in ('synced', 'closed'))))),
                 activity_html, resources, esc(metrics), chat + evidence,
                 esc(job['next_step']), esc(', '.join(job['errors']))]
        rows.append('<tr>' + ''.join('<td>' + value + '</td>' for value in cells) + '</tr>')
    blocker_html = '<pre class="blockers">' + esc(render_blockers(snapshot['blockers'])) + '</pre>' if snapshot.get('blockers') else ''
    if snapshot.get('coordination_error'):
        blocker_html += '<p role="alert">资源协调记录未核实：' + esc(snapshot['coordination_error']) + '</p>'
    checked = json.dumps(snapshot['checked_at']).replace('<', r'\u003c')
    control_html, control_script = '', ''
    if controls:
        paused = snapshot.get('operator_control', {}).get('state') == 'pause_and_drain_requested'
        message = '已保存暂停请求。尚未核验 worker 或在途动作停止。' if paused else ''
        disabled = ' disabled' if paused else ''
        control_html = f'<section aria-labelledby="controls"><h2 id="controls">运行控制</h2><p>暂停后由现有 worker 停止派发并收尾。在途工具调用可能继续，面板请求成功不证明已停止。</p><button id="pause" type="button"{disabled}>暂停并收尾</button><p id="control-result" role="status" aria-live="polite">{message}</p></section>'
        csrf = json.dumps(controls['csrf_token']).replace('<', r'\u003c')
        control_script = """document.getElementById('pause').addEventListener('click',async function(){this.disabled=true;const result=document.getElementById('control-result');result.textContent='正在保存暂停请求';try{const response=await fetch('/api/control',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({action:'pause_and_drain',csrf_token:CSRF})});if(!response.ok)throw new Error();result.textContent='已保存暂停请求。尚未核验 worker 或在途动作停止。';}catch(error){result.textContent='暂停请求失败。请核对当前 controller 和本地状态。';this.disabled=false;}});""".replace('CSRF', csrf)
    return '''<!doctype html><html lang="zh"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta http-equiv="refresh" content="15">
<title>申请运行面板</title><style>
:root{--bg:#101827;--text:#e5ecf5;--line:#526078;--accent:#ffd479;--link:#9ecbff}*{box-sizing:border-box}body{font:15px system-ui;margin:0;padding:1.5rem;background:var(--bg);color:var(--text)}h1{font-size:1.5rem}h2{font-size:1.125rem}a{color:var(--link)}table{border-collapse:collapse;width:100%;min-width:96rem;font-size:.875rem}td,th{padding:.75rem;text-align:left;border-bottom:1px solid var(--line);vertical-align:top;overflow-wrap:anywhere}th{background:var(--bg);white-space:nowrap}td:nth-child(3){min-width:12rem}td:nth-child(4){white-space:nowrap}td:nth-child(6){min-width:10rem}td:nth-child(7){min-width:14rem}td:nth-child(9){min-width:12rem}td:nth-child(10){min-width:14rem}code{overflow-wrap:anywhere}.table-scroll{overflow-x:auto;max-width:100%}.evidence-path{margin-top:.5rem;max-width:18rem}.evidence-path code{display:block;white-space:normal;margin-top:.5rem}.readiness{border-top:1px solid var(--line);border-bottom:1px solid var(--line);padding:1rem 0;margin:1rem 0}.readiness-list{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:1rem;margin:1rem 0}.readiness-list dt{font-weight:600;margin-bottom:.25rem}.readiness-list dd{margin:0;overflow-wrap:anywhere}.readiness details{margin-top:.75rem}.readiness li{overflow-wrap:anywhere}summary{cursor:pointer}#fresh,.blockers{color:var(--accent)}.blockers{white-space:pre-wrap}.counts{font-size:1.125rem;margin:1rem 0}input,button{font:inherit;padding:.75rem;border:1px solid var(--line);border-radius:.25rem}input{max-width:100%;width:24rem}button{cursor:pointer}button:disabled{cursor:default;opacity:.7}:focus-visible{outline:3px solid var(--accent);outline-offset:3px}@media(max-width:48rem){body{padding:1rem}td,th{padding:.5rem}.readiness-list{grid-template-columns:1fr}}
</style></head><body><main><h1>申请运行面板</h1><div id="fresh" role="status"></div><p>程序读取回执、正式同步与原任务事件。每 15 秒刷新。耗时包含等待，tokens 来自已读取的实际请求。</p>''' + fixture_notice + render_readiness(snapshot.get('readiness')) + f'''<div class="counts">本批提交完成（含用户确认） {c['submitted']}/{snapshot['target']} · 同步 {c['synced']} · 关页 {c['closed']} · 执行或未知 {c['occupied']}</div>
<p>最近核验：{esc(snapshot['checked_at'])}</p><p>核验错误：{esc(', '.join(snapshot.get('errors', [])) or '无')}</p>{control_html}{blocker_html}
<label for="filter">筛选公司、岗位、状态</label><br><input id="filter" type="search">
<div class="table-scroll" role="region" aria-label="岗位运行详情" tabindex="0"><table><caption>原会话、资源记录与提交证据分别核验</caption><thead><tr>{''.join('<th scope="col">'+h+'</th>' for h in ('范围','岗位','公司与职位','状态','提交/同步/关页','实际工具活动','标签与资源','实际用量与耗时','原会话与证据','下一步','问题'))}</tr></thead><tbody>{''.join(rows) or '<tr><td colspan="11">没有已记录岗位</td></tr>'}</tbody></table></div>
<p>资源记录来自参与协调的 pipeline 调用。未记录资源不证明空闲，也不授权接管。</p></main>
<script>const checked={checked};function freshness(){{const age=Math.max(0,Math.floor((Date.now()-Date.parse(checked))/1000));document.getElementById('fresh').textContent=(age>60?'快照过期，不能视为正常运行':'快照时间正常')+' · '+age+' 秒前';}}freshness();setInterval(freshness,1000);document.getElementById('filter').addEventListener('input',function(){{document.querySelectorAll('tbody tr').forEach(r=>r.hidden=!r.textContent.toLowerCase().includes(this.value.toLowerCase()));}});{control_script}</script></body></html>'''
