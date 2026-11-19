"""Parse the final structured contract returned by pipeline Codex turns."""

from __future__ import annotations

import json
from typing import Any

# login_verification covers passwords and logins; captcha and binding_terms are user-only stops.
BLOCKER_KINDS = {'confirmation_repeat','missing_fact','login_verification','approval_rejected','submission_unknown','captcha','binding_terms'}

CHINESE_STATUSES = {
    "需要主控判断": "needs_advice",
    "等待检查": "ready_for_review", "需要补充信息": "needs_input",
    "岗位不可申请": "unavailable", "此前已投递": "already_applied",
    "检查通过": "review_passed", "需要修改": "changes_requested",
    "已投递": "submitted", "等待验证": "needs_verification",
    "暂时出错": "retryable_error", "提交结果待确认": "submission_unknown",
    "需要高级模型": "needs_strong_model",
    "等待平台额度": "platform_wait",
}


def human_result_instructions() -> str:
    from pipeline.tab_preservation import TAB_PRESERVATION_RULE
    return (TAB_PRESERVATION_RULE + "\n" + "所有进度消息和最终回复使用自然、简短的中文，不输出 JSON 或代码块。"
            "最终回复第一行写‘申请进度：’加以下一个准确状态："
            + "、".join(s for s in CHINESE_STATUSES if s != "需要高级模型")  # Only the routed default model is told about hand-off.
            + "。第二行以‘说明：’开头，用一两句话说明已完成的步骤、实际障碍或成功依据。"
            "停在需要主控判断、需要补充信息、等待验证或暂时出错时，第三行以‘原文：’逐字粘贴导致停下的工具报错或页面文字；"
            "原因只能从原文得出，不把推测写成原因，原文没有明确要求就不让用户更改浏览器或系统设置。"
            "停在验证码、约束性条款（仲裁、放弃陪审团、责任豁免）、密码或登录时，加一行‘阻碍分类：captcha / binding_terms / login_verification’；缺个人事实写 missing_fact。"
            "只有按 docs/confirmation-evidence.md 保存真实截图或原始确认邮件及岗位绑定回执，才能写已投递；仅说明文字不算凭据。"
            "不把工具拒绝归因于用户操作，不重复询问已经获得的授权。")


def parse_json_result(text: str) -> dict[str, Any]:
    decoder = json.JSONDecoder()
    results: list[dict[str, Any]] = []
    index = 0
    while index < len(text):
        start = text.find("{", index)
        if start < 0:
            break
        try:
            value, consumed = decoder.raw_decode(text[start:])
        except json.JSONDecodeError:
            index = start + 1
            continue
        if isinstance(value, dict):
            results.append(value)
        index = start + consumed
    if not results:
        raise ValueError(f"agent returned no JSON object: {text[-500:]}")
    return results[-1]


def parse_application_result(text: str) -> dict[str, Any]:
    text = text.strip()
    if text.startswith("申请进度：") and "说明：" in text.splitlines()[0]:
        text = text.replace("说明：", "\n说明：", 1)
    lines = text.splitlines()
    if lines and lines[0].startswith("申请进度："):
        status_text = lines[0].split("：", 1)[1].strip()
        inline_close = ""
        for separator in ("；", ";"):
            prefix, found, suffix = status_text.partition(separator)
            if found and prefix.strip() == "此前已投递" and suffix.strip() == "标签关闭：完成":
                status_text = prefix.strip()
                inline_close = suffix.strip()
                break
        status = CHINESE_STATUSES.get(status_text)
        quotes = [line.split("：", 1)[1].strip() for line in lines[2:] if line.startswith("原文：")]
        explanation = "\n".join(line for line in lines[1:] if not line.startswith(("原文：", "阻碍分类："))).strip()
        if not status or not explanation.startswith("说明：") or not explanation[3:].strip():
            raise ValueError("申请回复缺少有效进度或说明")
        explanation = explanation[3:].strip()
        if inline_close:
            explanation = inline_close + "；" + explanation
        result = {"status": status, "reason": explanation, "evidence": explanation}
        if quotes:
            result["quote"] = quotes[-1]
        kinds=[line.split('：',1)[1].strip() for line in lines[2:] if line.startswith('阻碍分类：')]
        if kinds:
            if len(kinds)!=1 or kinds[0] not in BLOCKER_KINDS:
                raise ValueError('invalid blocker_kind; omit if not applicable, otherwise use: ' + ', '.join(sorted(BLOCKER_KINDS)))
            result['blocker_kind']=kinds[0]
        if status == "submitted":
            result["success_evidence"] = explanation
        return result
    try:
        result = parse_json_result(text)
        if result.get('blocker_kind') is not None and result['blocker_kind'] not in BLOCKER_KINDS:
            raise ValueError('invalid blocker_kind; omit if not applicable, otherwise use: ' + ', '.join(sorted(BLOCKER_KINDS)))
        return result
    except ValueError:
        lowered = text.casefold()
        if any(
            phrase in lowered
            for phrase in (
                "declined permission",
                "denied permission",
                "permission was declined",
                "permission was denied",
            )
        ):
            return {
                "status": "needs_input",
                "confirmation_required": "desktop_browser_permission",
                "reason": "Open this application task in Codex Desktop, approve the one-time "
                "Chrome domain access request, then run `pipeline sync JOB_ID`.",
            }
        raise


ADVICE_CATEGORIES = {'application_answer', 'missing_fact', 'authorization_or_result', 'correctness_risk'}
ADVICE_STATUSES = {'controller_required', 'pending', 'running', 'answered', 'delivering', 'resolved', 'needs_user', 'critical'}


def _required_text(value: dict[str, Any], key: str) -> None:
    if not isinstance(value.get(key), str) or not value[key].strip():
        raise ValueError(f'{key} must be nonempty text')


def _text_list(value: dict[str, Any], key: str, *, nonempty: bool = False) -> None:
    items = value.get(key)
    if not isinstance(items, list) or (nonempty and not items) or any(not isinstance(x, str) or not x.strip() for x in items):
        raise ValueError(f'{key} must be a list of text')


def normalize_question_limits(question: dict[str, Any]) -> dict[str, Any]:
    question = dict(question)
    for key in ('word_limit', 'char_limit'):
        if question.get(key) is None:
            question.pop(key, None)
        elif type(question[key]) is not int or question[key] < 1:
            raise ValueError(f'{key} must be a positive integer observed on the form; omit unknown limits (null is also accepted), never invent a limit')
    return question


def validate_advice_request(value: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError('advice request must be an object')
    for key in ('luna_session_id', 'question'):
        _required_text(value, key)
    if type(value.get('revision')) is not int or value['revision'] < 1:
        raise ValueError('revision must be a positive integer')
    if not isinstance(value.get('category'), str) or value['category'] not in ADVICE_CATEGORIES:
        raise ValueError('unknown advice category; allowed: ' + ', '.join(sorted(ADVICE_CATEGORIES)))
    for key in ('evidence', 'attempted'):
        _text_list(value, key, nonempty=key == 'evidence')
    if type(value.get('suspected_critical')) is not bool:
        raise ValueError('suspected_critical must be boolean')
    if value['suspected_critical'] and value.get('pause_scope') not in ('job', 'global'):
        raise ValueError('critical request requires job/global pause_scope')
    if value.get('advice_id') is not None and (type(value['advice_id']) is not int or value['advice_id'] < 1):
        raise ValueError('advice_id must be a positive integer')
    if value.get('blocker_kind') is not None and value['blocker_kind'] not in BLOCKER_KINDS:
        raise ValueError('invalid blocker_kind; omit if not applicable, otherwise use: ' + ', '.join(sorted(BLOCKER_KINDS)))
    if value.get('routine_action') not in (None, 'fill', 'upload', 'login', 'required_application_privacy'):
        raise ValueError('unknown routine application action')
    if value.get('blocker_scope') not in (None, 'current', 'future_unanswered'):
        raise ValueError('invalid blocker_scope')
    if 'question_identity' in value:
        identity = value['question_identity']
        if not isinstance(identity, dict):
            raise ValueError('question_identity must be an object')
        for key in ('field_id', 'page_url', 'fact_key'):
            _required_text(identity, key)
        if 'version' in identity:
            _required_text(identity, 'version')
    for name, keys in [('current_control', ('label',)),
                       ('actual_tool_rejection', ('tool_name', 'error'))]:
        if name in value:
            if not isinstance(value[name], dict):
                raise ValueError(name + ' must be an object')
            for key in keys:
                _required_text(value[name], key)
    if 'current_page' in value:
        page = value['current_page']
        if not isinstance(page, dict):
            raise ValueError('current_page must be an object')
        for key in ('url', 'title', 'observed_at'):
            _required_text(page, key)
        if not isinstance(page.get('blocking_question'), str) or type(page.get('required_complete')) is not bool:
            raise ValueError('current_page requires blocking_question and required_complete')
        _text_list(page, 'visible_errors')
        action = page.get('next_action')
        if not isinstance(action, dict) or not isinstance(action.get('label'), str) or type(action.get('is_submit')) is not bool:
            raise ValueError('current_page requires next_action label/is_submit')
    if value['category'] == 'application_answer':
        questions = value.get('questions')
        if not isinstance(questions, list) or not questions:
            raise ValueError('application_answer requires questions, e.g. [{"field_id":"actual DOM field ID","text":"exact question"}]; include visible options for choices and omit unknown limits')
        questions = [normalize_question_limits(q) if isinstance(q, dict) else q for q in questions]
        value = {**value, 'questions': questions}
        for question in questions:
            if not isinstance(question, dict):
                raise ValueError('question must be an object')
            for key in ('field_id', 'text'):
                _required_text(question, key)
            if 'control_type' in question and (not isinstance(question['control_type'], str) or not question['control_type'].strip()):
                raise ValueError('question control_type must be nonempty text')
            if 'options' in question and (not isinstance(question['options'], list) or not question['options'] or not all(isinstance(x, str) and x.strip() for x in question['options'])):
                raise ValueError('question options must be a nonempty string array')
        if len({q['field_id'] for q in questions}) != len(questions):
            raise ValueError('question field IDs must be unique')
    return dict(value)


def validate_advice_result(value: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError('advice result must be an object')
    _required_text(value, 'reason')
    decision = value.get('decision')
    if decision not in ('continue', 'ask_luna', 'needs_user', 'critical'):
        raise ValueError('unknown advice decision')
    if decision in ('continue', 'ask_luna'):
        _required_text(value, 'instructions')
    if decision == 'continue':
        _required_text(value, 'verification')
    if decision == 'needs_user':
        _required_text(value, 'user_question')
    if decision == 'critical':
        _text_list(value, 'evidence', nonempty=True)
        if value.get('pause_scope') not in ('job', 'global'):
            raise ValueError('critical result requires job/global pause_scope')
    return dict(value)
