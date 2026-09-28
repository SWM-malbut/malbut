"""Explicit provider wire formats. No SDK, credentials or network at import."""

from dataclasses import dataclass
from decimal import Decimal
import json
import re


@dataclass(frozen=True)
class Model:
    provider: str
    model: str
    key_env: str
    endpoint: str


MODELS = {
    'gpt-6-astra': Model('openai', 'gpt-6-astra', 'OPENAI_API_KEY',
                         'https://api.openai.com/v1/responses'),
    'gpt-6-sol': Model('openai', 'gpt-6-sol', 'OPENAI_API_KEY',
                       'https://api.openai.com/v1/responses'),
    'claude-opus-5-5': Model('anthropic', 'claude-opus-5-5', 'ANTHROPIC_API_KEY',
                             'https://api.anthropic.com/v1/messages'),
    'claude-sonnet-5': Model('anthropic', 'claude-sonnet-5', 'ANTHROPIC_API_KEY',
                             'https://api.anthropic.com/v1/messages'),
    'gemini-3.8-flash': Model('google', 'gemini-3.8-flash', 'GEMINI_API_KEY',
                              'https://generativelanguage.googleapis.com/v1beta/models/'),
    'gemini-3.5-flash': Model('google', 'gemini-3.5-flash', 'GEMINI_API_KEY',
                              'https://generativelanguage.googleapis.com/v1beta/models/'),
    # Standard only. Never substitute the training-enabled Contributor product.
    'muse-spark-1.2': Model('meta', 'muse-spark-1.2', 'MODEL_API_KEY',
                            'https://api.meta.ai/v1/chat/completions'),
    'qwen3.8-max': Model('alibaba', 'qwen3.8-max', 'DASHSCOPE_API_KEY', ''),
    'qwen3.8-flash': Model('alibaba', 'qwen3.8-flash', 'DASHSCOPE_API_KEY', ''),
    'glm-5.3-flash': Model('zai', 'glm-5.3-flash', 'ZAI_API_KEY',
                           'https://api.z.ai/api/paas/v4/chat/completions'),
    'kimi-k3': Model('moonshot', 'kimi-k3', 'MOONSHOT_API_KEY',
                      'https://api.moonshot.ai/v1/chat/completions'),
    'grok-4.7': Model('xai', 'grok-4.7', 'XAI_API_KEY',
                       'https://api.x.ai/v1/chat/completions'),
    'gemma4:31b': Model('ollama', 'gemma4:31b', 'OLLAMA_API_KEY',
                         'https://ollama.com/api/chat'),
}


def endpoint(model, workspace=None):
    if model.provider == 'alibaba':
        if not isinstance(workspace, str) or not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,62}', workspace):
            raise ValueError('Qwen requires a Singapore workspace ID')
        return (f'https://{workspace}.ap-southeast-1.maas.aliyuncs.com'
                '/compatible-mode/v1/chat/completions')
    if model.provider == 'google':
        return model.endpoint + model.model + ':generateContent'
    return model.endpoint


def headers(model, key):
    if not isinstance(key, str) or not key or not key.isascii() or any(c.isspace() for c in key):
        raise ValueError('missing or invalid API credential')
    result = {'Content-Type': 'application/json', 'Accept-Encoding': 'identity'}
    if model.provider == 'anthropic':
        result.update({'x-api-key': key, 'anthropic-version': '2023-06-01'})
    elif model.provider == 'google':
        result['x-goog-api-key'] = key
    else:
        result['Authorization'] = 'Bearer ' + key
    return result


def payload(model, common, profile='baseline'):
    """common contains only shared instructions, neutral metadata and JPEGs."""
    if profile not in ('baseline', 'low_reasoning'):
        raise ValueError('unknown model profile')
    system, text, images = common['system'], common['text'], common['images']
    p = model.provider
    if p == 'openai':
        return dict(model=model.model, store=False, service_tier='default', max_output_tokens=4096,
                    reasoning={'effort': 'low'}, input=[
                        {'role': 'system', 'content': system},
                        {'role': 'user', 'content': [{'type': 'input_text', 'text': text}] + [
                            {'type': 'input_image', 'image_url': 'data:image/jpeg;base64,' + im,
                             'detail': 'auto'} for im in images]}])
    if p == 'anthropic':
        return dict(model=model.model, max_tokens=4096, system=system,
                    messages=[{'role': 'user', 'content': [
                        {'type': 'image', 'source': {'type': 'base64', 'media_type': 'image/jpeg',
                                                    'data': im}} for im in images] + [
                        {'type': 'text', 'text': text}]}])
    if p == 'google':
        return dict(systemInstruction={'parts': [{'text': system}]},
                    contents=[{'role': 'user', 'parts': [
                        {'inlineData': {'mimeType': 'image/jpeg', 'data': im}} for im in images
                    ] + [{'text': text}]}],
                    generationConfig={'maxOutputTokens': 4096,
                                      'thinkingConfig': {'thinkingLevel': 'LOW'}})
    if p == 'ollama':
        return dict(model=model.model, stream=False, think=False,
                    options={'temperature': 0, 'num_predict': 2048},
                    messages=[{'role': 'system', 'content': system},
                              {'role': 'user', 'content': text, 'images': images}])
    result = dict(model=model.model, stream=False, messages=[
        {'role': 'system', 'content': system},
        {'role': 'user', 'content': [{'type': 'text', 'text': text}] + [
            {'type': 'image_url', 'image_url': {'url': 'data:image/jpeg;base64,' + im}}
            for im in images]}])
    # Model families have different reasoning/sampling restrictions. Do not force
    # temperature=0 or thinking=disabled onto models that do not support them.
    if p == 'moonshot':
        result.update(max_completion_tokens=4096, reasoning_effort='low')
    else:
        result['max_tokens'] = 4096
    if p == 'alibaba':
        result['enable_thinking'] = False
    if p == 'zai':
        result['thinking'] = {'type': 'enabled'}
    if p == 'xai':
        result['reasoning_effort'] = 'low'
    if profile == 'low_reasoning' and p in ('meta', 'zai'):
        result['reasoning_effort'] = 'low'
    return result


def error_details(raw, secret):
    """Keep diagnostic codes, never error prose which can echo keys or images."""
    result = {}
    try:
        body = strict_json(raw)
        error = body.get('error') if isinstance(body, dict) else None
        if not isinstance(error, dict):
            return result
        for field in ('code', 'type', 'param'):
            value = error.get(field)
            if type(value) is int:
                result[field] = value
            elif (isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9_.-]{1,100}', value)
                  and secret not in value and not value.startswith(('sk-', 'Bearer'))):
                result[field] = value
    except (ValueError, TypeError, UnicodeError, RecursionError):
        pass
    return result


def strict_json(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError('duplicate JSON key')
            result[key] = value
        return result

    def invalid(_):
        raise ValueError('non-finite JSON number')

    return json.loads(raw, object_pairs_hook=pairs, parse_constant=invalid)


def count(value):
    if type(value) is not int or value < 0:
        raise ValueError('invalid usage')
    return value


def numeric_usage(value):
    """Keep token diagnostics, not arbitrary provider error prose or secrets."""
    if not isinstance(value, dict):
        return None
    return {k: numeric_usage(v) if isinstance(v, dict) else v for k, v in value.items()
            if isinstance(k, str) and re.fullmatch(r'[A-Za-z0-9_]{1,80}', k)
            and (isinstance(v, dict) or type(v) is int and v >= 0)}


def explicit_signals(provider, body):
    """Inspect policy signals before parsing text, including malformed replies."""
    signals, refusal = [], []
    error = body.get('error')
    code = error.get('code') if isinstance(error, dict) else None
    if provider == 'alibaba' and code in ('DataInspectionFailed', 'data_inspection_failed'):
        signals.append('error.code=' + code)
    if provider == 'openai':
        if code in ('content_filter', 'content_policy_violation'):
            signals.append('error.code=' + code)
        incomplete = body.get('incomplete_details')
        if isinstance(incomplete, dict) and incomplete.get('reason') == 'content_filter':
            signals.append('incomplete_details.reason=content_filter')
        output = body.get('output')
        for item in output if isinstance(output, list) else []:
            content = item.get('content') if isinstance(item, dict) else None
            if isinstance(content, list) and any(isinstance(c, dict) and c.get('type') == 'refusal'
                                                 for c in content):
                refusal.append('output.content.type=refusal')
    elif provider == 'anthropic' and body.get('stop_reason') == 'refusal':
        refusal.append('stop_reason=refusal')
    elif provider == 'google':
        blocked = {'SAFETY', 'BLOCKLIST', 'PROHIBITED_CONTENT', 'IMAGE_SAFETY', 'SPII'}
        feedback = body.get('promptFeedback')
        reason = feedback.get('blockReason') if isinstance(feedback, dict) else None
        if isinstance(reason, str) and reason in blocked:
            signals.append('promptFeedback.blockReason=' + reason)
        candidates = body.get('candidates')
        for c in candidates if isinstance(candidates, list) else []:
            reason = c.get('finishReason') if isinstance(c, dict) else None
            if isinstance(reason, str) and reason in blocked:
                signals.append('candidates.finishReason=' + reason)
    elif provider == 'ollama':
        msg = body.get('message')
        if isinstance(msg, dict) and msg.get('refusal'):
            refusal.append('message.refusal')
    else:
        choices = body.get('choices')
        for c in choices if isinstance(choices, list) else []:
            if not isinstance(c, dict):
                continue
            if c.get('finish_reason') == 'content_filter':
                signals.append('choices.finish_reason=content_filter')
            msg = c.get('message')
            if isinstance(msg, dict) and msg.get('refusal'):
                refusal.append('choices.message.refusal')
    return ('safety_blocked' if signals else 'refused' if refusal else None,
            sorted(set(signals + refusal)))


def usage(provider, body, cache_write_headers=None):
    """Reasoning is a subset of output, except Gemini's separately reported total.

    A missing/unsupported usage object is unknown, never a zero-token call.
    Keep the raw usage privately as well; estimates are not invoice amounts.
    """
    raw = body.get('usageMetadata' if provider == 'google' else 'usage')
    if provider == 'ollama':
        raw = {k: body[k] for k in ('prompt_eval_count', 'eval_count') if k in body}
    try:
        if not isinstance(raw, dict) or not raw:
            raise ValueError('missing usage')
        cached = write5 = write1 = write30 = reasoning = 0
        if provider == 'openai':
            total, output = count(raw['input_tokens']), count(raw['output_tokens'])
            cached = count((raw.get('input_tokens_details') or {}).get('cached_tokens', 0))
            write30 = count((raw.get('input_tokens_details') or {}).get('cache_write_tokens', 0))
            reasoning = count((raw.get('output_tokens_details') or {}).get('reasoning_tokens', 0))
            fresh = total - cached - write30
        elif provider == 'anthropic':
            fresh, output = count(raw['input_tokens']), count(raw['output_tokens'])
            cached = count(raw.get('cache_read_input_tokens', 0))
            writes = count(raw.get('cache_creation_input_tokens', 0))
            if writes:
                parts = raw['cache_creation']
                write5 = count(parts['ephemeral_5m_input_tokens'])
                write1 = count(parts['ephemeral_1h_input_tokens'])
                if write5 + write1 != writes:
                    raise ValueError('cache count mismatch')
        elif provider == 'google':
            total = count(raw['promptTokenCount'])
            cached = count(raw.get('cachedContentTokenCount', 0))
            reasoning = count(raw.get('thoughtsTokenCount', 0))
            output = count(raw['candidatesTokenCount']) + reasoning
            fresh = total - cached
        elif provider == 'ollama':
            fresh, output = count(raw['prompt_eval_count']), count(raw['eval_count'])
        else:
            total, output = count(raw['prompt_tokens']), count(raw['completion_tokens'])
            details = raw.get('prompt_tokens_details') or {}
            cached = count(details.get('cached_tokens', raw.get('cached_tokens', 0)))
            reasoning = count((raw.get('completion_tokens_details') or {}).get('reasoning_tokens', 0))
            if provider == 'xai':
                # Chat Completions separates final-answer and reasoning tokens.
                # Responses API has a different, inclusive output_tokens field.
                output += reasoning
                if 'total_tokens' in raw and count(raw['total_tokens']) != total + output:
                    raise ValueError('inconsistent xAI usage')
            fresh = total - cached
            writes = count(details.get('cache_write_tokens', 0))
            if writes:
                if provider != 'moonshot' or cache_write_headers is None:
                    raise ValueError('unsupported cache accounting')
                write5 = count(cache_write_headers['5m'])
                write1 = count(cache_write_headers['1h'])
                if write5 + write1 != writes:
                    raise ValueError('cache count mismatch')
                fresh -= writes
            # Providers may charge additional cache creation. Until its accounting
            # is mapped, report unknown instead of silently underestimating it.
            if any('cache' in str(k) and 'creat' in str(k) and v
                   for obj in (raw, details) for k, v in obj.items()):
                raise ValueError('unsupported cache accounting')
        if fresh < 0 or reasoning > output:
            raise ValueError('inconsistent usage')
        return raw, dict(input=fresh, cached_input=cached, cache_write_5m=write5,
                         cache_write_1h=write1, cache_write_30m=write30,
                         output=output, reasoning_output=reasoning)
    except (ValueError, KeyError, TypeError, AttributeError):
        return raw, None


def normalize(model, http_status, raw, cache_write_headers=None):
    """Do not infer censorship from a generic HTTP status or Korean prose."""
    failure = {401: 'auth_error', 403: 'auth_error', 402: 'quota_error',
               429: 'rate_or_quota_error'}.get(http_status, 'request_failed')
    result = dict(outcome='invalid_response' if http_status == 200 else failure,
                  reason='invalid_envelope', text=None, provider_signals=[],
                  model_returned=None, usage_raw=None, usage=None, provider_billed_usd=None,
                  manual_review=http_status in (200, 400))
    try:
        body = strict_json(raw)
        if not isinstance(body, dict):
            raise ValueError('not object')
        result['usage_raw'], result['usage'] = usage(model.provider, body, cache_write_headers)
        result['usage_raw'] = numeric_usage(result['usage_raw'])
        if model.provider == 'xai' and isinstance(body.get('usage'), dict):
            ticks = body['usage'].get('cost_in_usd_ticks')
            if type(ticks) is int and ticks >= 0:
                result['provider_billed_usd'] = str(Decimal(ticks) / Decimal(10**10))
        returned = body.get('model', body.get('modelVersion'))
        if isinstance(returned, str) and re.fullmatch(r'[A-Za-z0-9_.:/-]{1,160}', returned):
            result['model_returned'] = returned
        p = model.provider
        flagged, result['provider_signals'] = explicit_signals(p, body)
        if flagged:
            result.update(outcome=flagged, reason='provider_policy_signal', manual_review=False)
            return result
        error = body.get('error') or {}
        text = None
        complete = False
        if p == 'openai':
            parts = [c for item in body.get('output', []) if item.get('type') == 'message'
                     for c in item.get('content', [])]
            texts = [c['text'] for c in parts if c.get('type') == 'output_text']
            text = ''.join(texts)
            complete = body.get('status') == 'completed' and all(
                item.get('type') in ('reasoning', 'message') for item in body.get('output', []))
        elif p == 'anthropic':
            text = ''.join(c['text'] for c in body.get('content', []) if c.get('type') == 'text')
            complete = body.get('stop_reason') == 'end_turn' and not any(
                c.get('type') == 'tool_use' for c in body.get('content', []))
        elif p == 'google':
            candidates = body.get('candidates', [])
            if len(candidates) == 1:
                c = candidates[0]
                parts = (c.get('content') or {}).get('parts', [])
                text = ''.join(x['text'] for x in parts if 'text' in x and not x.get('thought'))
                complete = c.get('finishReason') == 'STOP' and not any('functionCall' in x for x in parts)
        elif p == 'ollama':
            msg = body.get('message') or {}
            text = msg.get('content')
            complete = body.get('done') is True and body.get('done_reason') in (None, 'stop')
            complete &= msg.get('role') == 'assistant' and not msg.get('tool_calls') and not error
        else:
            choices = body.get('choices', [])
            if len(choices) == 1:
                c, msg = choices[0], choices[0].get('message') or {}
                text = msg.get('content')
                complete = (c.get('finish_reason') == 'stop' and msg.get('role') == 'assistant'
                            and not msg.get('tool_calls') and not msg.get('function_call'))
        if http_status != 200 or error:
            result.update(outcome=failure, reason='provider_error')
        elif complete and isinstance(text, str) and text.strip():
            result.update(outcome='response', reason='complete_text', text=text, manual_review=False)
        else:
            result.update(reason='incomplete_or_missing_text')
    except (ValueError, KeyError, TypeError, AttributeError, UnicodeError, RecursionError):
        pass
    return result
