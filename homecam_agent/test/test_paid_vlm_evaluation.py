"""Provider fixtures only. Never requires an API key, network, ROS or a model."""

import asyncio
import base64
import hashlib
import json
from pathlib import Path
import stat
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from paid_vlm import inputs, metrics, providers, runner  # noqa: E402


COMMON = dict(system='same instructions', text='ordered RGB only', images=['aGVsbG8=', 'd29ybGQ='])
TEXT = json.dumps(dict(assessment='normal_activity', explanation='몸을 제어하며 앉음', findings=[]))


def envelope(provider, text=TEXT):
    if provider == 'openai':
        return dict(status='completed', model='gpt-6-sol-snapshot',
                    output=[dict(type='message', content=[dict(type='output_text', text=text)])],
                    usage=dict(input_tokens=100, output_tokens=50,
                               input_tokens_details={'cached_tokens': 20},
                               output_tokens_details={'reasoning_tokens': 10}))
    if provider == 'anthropic':
        return dict(stop_reason='end_turn', content=[dict(type='text', text=text)],
                    usage=dict(input_tokens=80, output_tokens=50, cache_read_input_tokens=20))
    if provider == 'google':
        return dict(candidates=[dict(finishReason='STOP', content={'parts': [{'text': text}]})],
                    usageMetadata=dict(promptTokenCount=100, cachedContentTokenCount=20,
                                       candidatesTokenCount=40, thoughtsTokenCount=10))
    if provider == 'ollama':
        return dict(done=True, done_reason='stop', message=dict(role='assistant', content=text),
                    prompt_eval_count=100, eval_count=50)
    return dict(choices=[dict(finish_reason='stop', message=dict(role='assistant', content=text))],
                usage=dict(prompt_tokens=100, completion_tokens=40 if provider == 'xai' else 50,
                           prompt_tokens_details={'cached_tokens': 20},
                           completion_tokens_details={'reasoning_tokens': 10}))


RATE = dict(currency='USD', source='https://provider.example/pricing', checked_on='2026-09-26',
            per_million_tokens=dict(input='2', cached_input='1', cache_write_5m='3',
                                    cache_write_1h='4', output='10'))


def raw(body):
    return json.dumps(body).encode()


@pytest.mark.parametrize('mid', providers.MODELS)
def test_payload_keeps_shared_instructions_and_images(mid):
    model = providers.MODELS[mid]
    body = providers.payload(model, COMMON)
    wire = json.dumps(body)
    assert COMMON['system'] in wire and COMMON['text'] in wire
    assert all(im in wire for im in COMMON['images'])
    assert 'SECRET' not in wire and 'case_id' not in wire and 'label' not in wire
    assert providers.endpoint(model, 'workspace-1').startswith('https://')
    assert 'contributor' not in model.model
    if model.provider == 'moonshot':
        assert body['max_completion_tokens'] == 4096 and 'temperature' not in body
    if model.provider == 'zai':
        assert body['thinking'] == {'type': 'enabled'}


@pytest.mark.parametrize('mid', providers.MODELS)
def test_complete_response_for_each_candidate(mid):
    model = providers.MODELS[mid]
    result = providers.normalize(model, 200, raw(envelope(model.provider)))
    assert result['outcome'] == 'response'
    assert inputs.assess(result['text'], 12)['label'] == 'normal_activity'
    assert result['usage']['output'] == 50
    assert result['usage']['input'] + result['usage']['cached_input'] == 100


@pytest.mark.parametrize('mid,body,outcome', [
    ('gpt-6-sol', dict(status='completed', output=[dict(type='message', content=[
        dict(type='refusal', refusal='Cannot comply')])]), 'refused'),
    ('gpt-6-sol', dict(status='incomplete', incomplete_details={'reason': 'content_filter'},
        output=[dict(type='message', content=[dict(type='refusal', refusal='no')])]), 'safety_blocked'),
    ('claude-sonnet-5', dict(stop_reason='refusal', content=[]), 'refused'),
    ('gemini-3.8-flash', dict(promptFeedback={'blockReason': 'SAFETY'}), 'safety_blocked'),
    ('gemini-3.8-flash', dict(candidates=[{'finishReason': 'PROHIBITED_CONTENT'}]), 'safety_blocked'),
    ('qwen3.8-max', dict(error={'code': 'DataInspectionFailed'}), 'safety_blocked'),
    ('grok-4.7', dict(choices=[dict(finish_reason='content_filter',
        message={'refusal': 'no'})]), 'safety_blocked'),
    ('kimi-k3', dict(choices=[dict(finish_reason='stop', message={'refusal': 'no'})]), 'refused'),
    ('gemma4:31b', dict(done=True, message={'role': 'assistant', 'refusal': 'no'}), 'refused'),
])
def test_explicit_refusal_and_blocking(mid, body, outcome):
    result = providers.normalize(providers.MODELS[mid], 200, raw(body))
    assert result['outcome'] == outcome
    assert result['text'] is None


@pytest.mark.parametrize('status,expected', [(401, 'auth_error'), (403, 'auth_error'),
    (402, 'quota_error'), (429, 'rate_or_quota_error'), (500, 'request_failed')])
def test_http_failure_is_not_censorship(status, expected):
    result = providers.normalize(providers.MODELS['grok-4.7'], status, raw({'error': {'message': 'error'}}))
    assert result['outcome'] == expected
    assert providers.normalize(providers.MODELS['grok-4.7'], status, b'not JSON')['outcome'] == expected


@pytest.mark.parametrize('body', [b'not JSON', b'{"done":true,"done":false}', b'{"a":NaN}', b'[]'])
def test_invalid_envelope_not_normal(body):
    assert providers.normalize(providers.MODELS['gemma4:31b'], 200, body)['outcome'] == 'invalid_response'


@pytest.mark.parametrize('text', [
    'I cannot analyze this image.', 'uncertain', '{"assessment":"normal_activity"}',
    '{"assessment":"normal_activity","assessment":"observed_fall","explanation":"x","findings":[]}',
    json.dumps(dict(assessment='normal_activity', explanation='x', findings=[{}])),
    json.dumps(dict(assessment='observed_fall', explanation='x', findings=[
        dict(assessment='observed_fall', kind='already_down', regions=[])])),
    json.dumps(dict(assessment='suspected_fall', explanation='x', findings=[
        dict(assessment='suspected_fall', kind='unknown', regions=[dict(frame_index=12, box=[0, 0, 1, 1])])])),
])
def test_invalid_or_free_text_response_is_manual_review_not_refusal(text):
    result = inputs.assess(text, 12)
    assert result['outcome'] == 'invalid_response' and result['label'] is None
    assert result['manual_review']


def test_unobservable_and_one_outer_fence():
    text = json.dumps(dict(assessment='unobservable', explanation='가려져 확인 불가', findings=[]))
    assert inputs.assess(text, 12)['outcome'] == 'unobservable'
    assert inputs.assess('```json\n' + TEXT + '\n```', 12)['label'] == 'normal_activity'
    assert inputs.assess('extra\n' + TEXT, 12)['outcome'] == 'invalid_response'


def test_policy_signal_survives_malformed_text():
    cases = [('claude-sonnet-5', {'stop_reason': 'refusal', 'content': None}, 'refused'),
             ('gemini-3.8-flash', {'promptFeedback': {'blockReason': 'SAFETY'},
                                 'candidates': [None]}, 'safety_blocked'),
             ('grok-4.7', {'choices': [{'finish_reason': 'content_filter', 'message': None}]}, 'safety_blocked')]
    for mid, body, expected in cases:
        result = providers.normalize(providers.MODELS[mid], 200, raw(body))
        assert result['outcome'] == expected and result['provider_signals']


def test_truncation_is_not_a_finished_answer():
    body = envelope('openai')
    body.update(status='incomplete', incomplete_details={'reason': 'max_output_tokens'})
    assert providers.normalize(providers.MODELS['gpt-6-sol'], 200, raw(body))['outcome'] == 'invalid_response'
    body = envelope('moonshot')
    body['choices'][0]['finish_reason'] = 'length'
    assert providers.normalize(providers.MODELS['kimi-k3'], 200, raw(body))['outcome'] == 'invalid_response'


def test_unknown_google_block_is_not_counted_as_safety():
    result = providers.normalize(providers.MODELS['gemini-3.8-flash'], 200,
                                  raw({'promptFeedback': {'blockReason': 'OTHER'}}))
    assert result['outcome'] == 'invalid_response'


def test_reasoning_cost_not_added_twice_and_missing_usage_unknown():
    _, use = providers.usage('openai', envelope('openai'))
    assert metrics.estimate_cost(use, RATE) == '0.00068'
    _, gemini = providers.usage('google', envelope('google'))
    assert metrics.estimate_cost(gemini, RATE) == '0.00068'
    assert metrics.estimate_cost(None, RATE) is None
    assert metrics.estimate_cost(use, None) is None
    _, missing = providers.usage('openai', {})
    assert missing is None
    bad = envelope('openai')
    bad['usage']['input_tokens'] = True
    assert providers.usage('openai', bad)[1] is None
    bad = envelope('moonshot')
    bad['usage']['cache_creation_input_tokens'] = 20
    assert providers.usage('moonshot', bad)[1] is None


def test_anthropic_cache_writes_separate():
    body = dict(usage=dict(input_tokens=10, output_tokens=20, cache_creation_input_tokens=30,
                          cache_creation=dict(ephemeral_5m_input_tokens=10, ephemeral_1h_input_tokens=20)))
    use = providers.usage('anthropic', body)[1]
    assert use['input'] == 10 and use['cache_write_1h'] == 20
    assert metrics.estimate_cost(use, RATE) == '0.00033'


def test_openai_cache_writes_are_exclusive_input_and_need_verified_rate():
    body = envelope('openai')
    body['usage']['input_tokens_details']['cache_write_tokens'] = 30
    use = providers.usage('openai', body)[1]
    assert use['input'] == 50 and use['cache_write_30m'] == 30
    assert metrics.estimate_cost(use, RATE) is None
    rate = dict(RATE, per_million_tokens=dict(RATE['per_million_tokens'], cache_write_30m='2.5'))
    assert metrics.estimate_cost(use, rate) == '0.000695'
    body['usage']['input_tokens_details']['cache_write_tokens'] = 81
    assert providers.usage('openai', body)[1] is None
    assert providers.payload(providers.MODELS['gpt-6-astra'], COMMON)['service_tier'] == 'default'


def test_kimi_cache_write_ttl_requires_response_headers():
    body = envelope('moonshot')
    body['usage']['prompt_tokens_details']['cache_write_tokens'] = 30
    assert providers.usage('moonshot', body)[1] is None
    use = providers.usage('moonshot', body, {'5m': 10, '1h': 20})[1]
    assert use['input'] == 50 and use['cache_write_5m'] == 10 and use['cache_write_1h'] == 20
    assert providers.usage('moonshot', body, {'5m': 29, '1h': 0})[1] is None
    assert providers.usage('xai', body, {'5m': 30, '1h': 0})[1] is None


def test_xai_reasoning_is_separate_and_total_must_match():
    body = envelope('xai')
    body['usage']['completion_tokens'] = 9
    body['usage']['completion_tokens_details']['reasoning_tokens'] = 94
    body['usage']['total_tokens'] = 203
    assert providers.usage('xai', body)[1]['output'] == 103
    body['usage']['total_tokens'] = 109
    assert providers.usage('xai', body)[1] is None


def test_xai_provider_billed_ticks_not_an_extra_fee():
    body = envelope('xai')
    body['usage']['cost_in_usd_ticks'] = 120240000
    async def transport(*_):
        return 200, raw(body)
    r = asyncio.run(runner.invoke(providers.MODELS['grok-4.7'], COMMON, 'SECRET', RATE,
                                  transport=transport))
    assert r['provider_billed_usd'] == r['cost_estimate_usd'] == '0.012024'
    assert r['cost_basis'] == 'provider_reported'
    body['usage']['cost_in_usd_ticks'] = True
    assert providers.normalize(providers.MODELS['grok-4.7'], 200, raw(body))['provider_billed_usd'] is None


def test_refusal_denominator_missing_case_and_latency():
    labels = dict(A='observed_fall', B='suspected_fall', C='normal_activity')
    rows = [dict(case_id='A', outcome='refused', label=None, elapsed_s=.01, cost_estimate_usd=None),
            dict(case_id='B', outcome='classified', label='normal_activity', elapsed_s=3, cost_estimate_usd='0.1')]
    partial = metrics.summarize(rows, labels)
    assert partial['accuracy'] is None and partial['not_called'] == 1
    assert partial['attempted_accuracy']['total'] == 2
    assert partial['classification_latency']['median_s'] == 3
    assert partial['missed_as_normal']['suspected_fall'] == 1
    rows.append(dict(case_id='C', outcome='classified', label='normal_activity', elapsed_s=4,
                     cost_estimate_usd='0.1'))
    full = metrics.summarize(rows, labels)
    assert full['accuracy'] == dict(correct=1, total=3, rate=1/3)
    assert full['cost']['total_estimate_usd'] is None
    assert full['cost']['unknown_requests'] == 1


def test_label_only_score_does_not_repair_bad_boxes_or_strict_score():
    text = json.dumps(dict(assessment='observed_fall', explanation='Visible fall.', findings=[dict(
        assessment='observed_fall', kind='motion_seen',
        regions=[dict(frame_index=0, box=[.44, 145, .58, 482])])]))
    checked = inputs.assess(text, 12)
    assert checked['outcome'] == 'invalid_response' and checked['label'] is None
    assert checked['reported_assessment'] == 'observed_fall'
    assert checked['response_issue_codes'] == ['invalid_box_range']
    row = dict(checked, case_id='A', elapsed_s=1, cost_estimate_usd='.01')
    stats = metrics.summarize([row], {'A':'observed_fall'})
    assert stats['accuracy']['correct'] == 0
    assert stats['video_label_accuracy']['correct'] == 1
    assert stats['response_issue_counts'] == {'invalid_box_range':1}


@pytest.mark.parametrize('value,reported,issues', [
    ('not JSON', None, ['invalid_json']),
    ('[]', None, ['not_json_object']),
    ('{"assessment":"fall"}', None, ['invalid_assessment']),
    ('{"assessment":"normal_activity"}', 'normal_activity', ['response_contract_error'])])
def test_declared_label_is_not_guessed_from_prose(value, reported, issues):
    row = inputs.assess(value, 12)
    assert row['outcome'] == 'invalid_response'
    assert row['reported_assessment'] == reported
    assert row['response_issue_codes'] == issues


def test_label_only_normal_miss_count_keeps_errors_separate():
    rows=[dict(case_id='A', outcome='invalid_response', label=None,
               reported_assessment='normal_activity', response_issue_codes=['invalid_box_range'],
               elapsed_s=1, cost_estimate_usd='.01'),
          dict(case_id='B', outcome='timeout', label=None, elapsed_s=20, cost_estimate_usd=None)]
    stats=metrics.summarize(rows, {'A':'suspected_fall', 'B':'normal_activity'})
    assert stats['video_label_accuracy']['total'] == 2
    assert stats['video_label_accuracy']['correct'] == 0
    assert stats['video_label_missed_as_normal']['suspected_fall'] == 1
    assert stats['missed_as_normal']['suspected_fall'] == 0


def test_timeout_no_retry_and_no_secret():
    calls = []
    async def timeout(*args):
        calls.append(1)
        raise asyncio.TimeoutError('SECRET')
    result = asyncio.run(runner.invoke(providers.MODELS['gpt-6-sol'], COMMON, 'SECRET', RATE,
                                       transport=timeout))
    assert result['outcome'] == 'timeout' and result['label'] is None
    assert result['cost_estimate_usd'] is None and len(calls) == 1
    assert 'SECRET' not in json.dumps(result)


def test_deadline_cancels_whole_request(monkeypatch):
    monkeypatch.setattr(runner, 'TIMEOUT_S', .01)
    canceled = []
    async def slow(*args):
        try:
            await asyncio.sleep(1)
        finally:
            canceled.append(True)
    result = asyncio.run(runner.invoke(providers.MODELS['gpt-6-sol'], COMMON, 'fixture', RATE,
                                       transport=slow))
    assert result['outcome'] == 'timeout' and canceled == [True]


@pytest.mark.parametrize('elapsed,within', [(19.0, True), (20.0, True), (24.0, False)])
def test_long_diagnostic_keeps_twenty_second_flag(monkeypatch, elapsed, within):
    # Replace the module's clock, not asyncio's global clock.
    clock = iter([100.0, 100.0+elapsed])
    monkeypatch.setattr(runner, 'time', SimpleNamespace(monotonic=lambda:next(clock)))
    limits = []
    async def wait_for(call, timeout):
        limits.append(timeout)
        return await call
    monkeypatch.setattr(runner.asyncio, 'wait_for', wait_for)
    async def transport(*args):
        return 200, raw(envelope('openai'))
    result = asyncio.run(runner.invoke(providers.MODELS['gpt-6-sol'], COMMON, 'fixture', RATE,
                                       transport=transport, timeout_s=60))
    assert result['outcome'] == 'classified' and limits == [60]
    assert result['response_received']
    assert result['response_within_20s'] is within
    assert result['usable_reply_within_20s'] is within
    assert result['elapsed_s'] == elapsed


def test_sixty_second_plan_is_distinct_and_reaches_runner(tmp_path):
    manifest, data, default = fixture_run()
    plan = runner.make_plan(manifest, data, ['gpt-6-sol'], request_timeout_s=60)
    assert default['condition']['timeout_s'] == 20
    assert plan['condition']['timeout_s'] == 60
    assert plan['condition']['benchmark_deadline_s'] == 20
    async def transport(*args):
        return 200, raw(envelope('openai'))
    asyncio.run(runner.execute(plan, manifest, data, tmp_path/'out', {'gpt-6-sol':RATE},
        approved_upload=True, budget_usd='1', request_reserve_usd='.1', max_calls=1,
        credentials={'OPENAI_API_KEY':'fixture'}, transport=transport))
    row = json.loads((tmp_path/'out/00001.result.json').read_text())
    assert row['request_timeout_s'] == 60


@pytest.mark.parametrize('value', [0, -1, 61, float('nan'), float('inf'), True, '60'])
def test_invalid_request_deadline(value):
    with pytest.raises(ValueError):
        runner.request_deadline(value)


@pytest.mark.parametrize('oversized', [False, True])
@pytest.mark.parametrize('deadline', [20, 60])
def test_https_client_no_proxy_redirect_retry_or_unlimited_body(monkeypatch, oversized, deadline):
    recorded = {}
    class Context:
        def __init__(self, value):
            self.value = value
        async def __aenter__(self):
            return self.value
        async def __aexit__(self, *args):
            return False
    async def chunks(size):
        yield b'ab'
        yield b'cdef' if oversized else b'cd'
    def post(url, **kwargs):
        recorded['post'] = kwargs
        recorded['calls'] = recorded.get('calls', 0) + 1
        return Context(SimpleNamespace(status=200, content=SimpleNamespace(iter_chunked=chunks)))
    def session(**kwargs):
        recorded['session'] = kwargs
        return Context(SimpleNamespace(post=post))
    fake = SimpleNamespace(ClientSession=session, ClientTimeout=lambda **kwargs: kwargs)
    monkeypatch.setitem(sys.modules, 'aiohttp', fake)
    monkeypatch.setattr(runner, 'MAX_RESPONSE_BYTES', 4)
    if oversized:
        with pytest.raises(ValueError, match='response_too_large'):
            asyncio.run(runner.https_post('https://api.openai.com/v1/responses', b'{}', {}, timeout_s=deadline))
    else:
        assert asyncio.run(runner.https_post('https://api.openai.com/v1/responses', b'{}', {}, timeout_s=deadline)) == (200, b'abcd')
    assert recorded['post']['allow_redirects'] is False
    assert recorded['session']['trust_env'] is False
    assert recorded['session']['timeout']['total'] == deadline
    assert recorded['calls'] == 1


def test_cli_defaults_to_dry_run(monkeypatch, capsys):
    import evaluate_paid_vlm as cli
    manifest, data, _ = fixture_run()
    monkeypatch.setattr(cli, 'load_bundle', lambda _: (manifest, data))
    def forbidden(*args, **kwargs):
        raise AssertionError('dry-run must not execute or read API keys')
    monkeypatch.setattr(cli, 'execute', forbidden)
    monkeypatch.setattr(sys, 'argv', ['evaluate_paid_vlm.py', 'run', '--inputs', 'unused',
                                    '--models', 'gpt-6-sol'])
    cli.main()
    output = json.loads(capsys.readouterr().out)
    assert output['status'] == 'DRY_RUN' and output['uploads'] == 0


def test_no_arbitrary_endpoint_or_key_in_url():
    model = providers.MODELS['qwen3.8-max']
    for workspace in ('https://evil.example', 'evil/secret', '../foo', 'x?key=123', ''):
        with pytest.raises(ValueError):
            providers.endpoint(model, workspace)
    assert 'SECRET' not in providers.endpoint(providers.MODELS['gemini-3.8-flash'])
    with pytest.raises(ValueError):
        providers.headers(providers.MODELS['gemma4:31b'], 'key\nInjected')


def fixture_run():
    manifest = dict(scope='pilot', case_ids=['A', 'B'], labels={'A': 'normal_activity', 'B': 'normal_activity'},
                    condition={'window_s': 5, 'max_frames': 12, 'timeout_s': 20})
    data = dict(A=COMMON, B=COMMON)
    plan = runner.make_plan(manifest, data, ['gpt-6-sol', 'gemma4:31b'])
    return manifest, data, plan


def test_rotate_and_execution_requires_approval(tmp_path):
    manifest, data, plan = fixture_run()
    assert [j['model'] for j in plan['schedule']] == [
        'gpt-6-sol', 'gemma4:31b', 'gemma4:31b', 'gpt-6-sol']
    with pytest.raises(ValueError, match='approval'):
        asyncio.run(runner.execute(plan, manifest, data, tmp_path/'out', {}))
    assert not (tmp_path/'out').exists()


def test_missing_rates_or_credentials_prevents_all_calls(tmp_path):
    manifest, data, plan = fixture_run()
    calls = []
    async def fake(*args):
        calls.append(1)
        raise AssertionError('must not send')
    for rates, credentials in [({}, {}), ({mid: RATE for mid in plan['models']}, {})]:
        with pytest.raises(ValueError):
            asyncio.run(runner.execute(plan, manifest, data, tmp_path/'out', rates,
                approved_upload=True, budget_usd='1', request_reserve_usd='.1', max_calls=4,
                transport=fake, credentials=credentials))
    assert calls == [] and not (tmp_path/'out').exists()


def test_offline_run_journal_and_existing_output_rejected(tmp_path):
    manifest, data, plan = fixture_run()
    calls = []
    async def fake(url, body, hdrs):
        calls.append(url)
        assert hdrs['Authorization'] == 'Bearer fixture-secret'
        provider = 'ollama' if 'ollama.com' in url else 'openai'
        return 200, raw(envelope(provider))
    kw = dict(approved_upload=True, budget_usd='1', request_reserve_usd='.1', max_calls=4,
              credentials={'OPENAI_API_KEY': 'fixture-secret', 'OLLAMA_API_KEY': 'fixture-secret'}, transport=fake)
    rates = {mid: RATE for mid in plan['models']}
    out = tmp_path/'results'
    report = asyncio.run(runner.execute(plan, manifest, data, out, rates, **kw))
    assert report['completed'] and len(calls) == 4
    assert report['models']['gpt-6-sol']['accuracy']['correct'] == 2
    assert len(list(out.glob('*.started.json'))) == len(list(out.glob('*.result.json'))) == 4
    assert stat.S_IMODE(out.stat().st_mode) == 0o700
    for p in out.iterdir():
        assert stat.S_IMODE(p.stat().st_mode) == 0o600
        assert 'fixture-secret' not in p.read_text()
    with pytest.raises(FileExistsError):
        asyncio.run(runner.execute(plan, manifest, data, out, rates, **kw))
    assert len(calls) == 4


def test_unknown_cost_stops_next_call(tmp_path):
    manifest, data, plan = fixture_run()
    calls = []
    async def refusal(*args):
        calls.append(1)
        return 200, raw(dict(status='completed', output=[dict(type='message',
            content=[dict(type='refusal', refusal='No')])]))
    report = asyncio.run(runner.execute(plan, manifest, data, tmp_path/'results',
        {mid: RATE for mid in plan['models']}, approved_upload=True, budget_usd='1',
        request_reserve_usd='.1', max_calls=4, transport=refusal,
        credentials={'OPENAI_API_KEY': 'fixture', 'OLLAMA_API_KEY': 'fixture'}))
    assert calls == [1] and report['stop_reason'] == 'unknown_cost'
    assert not report['completed']
    assert report['models']['gpt-6-sol']['accuracy'] is None
    assert report['models']['gemma4:31b']['not_called'] == 2


@pytest.mark.parametrize('budget,count,stop', [('.2', 2, 'budget_reservation'),
                                             ('.4', 4, 'completed')])
def test_continue_timeouts_reserves_each_call(tmp_path, budget, count, stop):
    manifest, data, plan = fixture_run()
    calls = []
    async def timeout(*args):
        calls.append(1)
        raise asyncio.TimeoutError()
    report = asyncio.run(runner.execute(plan, manifest, data, tmp_path/'out',
        {mid: RATE for mid in plan['models']}, approved_upload=True, budget_usd=budget,
        request_reserve_usd='.1', max_calls=4, transport=timeout, continue_after_timeout=True,
        credentials={'OPENAI_API_KEY': 'fixture', 'OLLAMA_API_KEY': 'fixture'}))
    assert len(calls) == count and report['stop_reason'] == stop
    assert report['known_cost_estimate_usd'] == '0' and report['unknown_cost']
    assert metrics.money(report['unresolved_reservation_usd']) == metrics.money(budget)
    assert len(list((tmp_path/'out').glob('*.result.json'))) == count


def test_continue_timeout_policy_does_not_ignore_other_unknown_costs(tmp_path):
    manifest, data, plan = fixture_run()
    calls = []
    async def broken(*args):
        calls.append(1)
        return 503, b'{}'
    report = asyncio.run(runner.execute(plan, manifest, data, tmp_path/'out',
        {mid: RATE for mid in plan['models']}, approved_upload=True, budget_usd='1',
        request_reserve_usd='.1', max_calls=4, transport=broken, continue_after_timeout=True,
        credentials={'OPENAI_API_KEY': 'fixture', 'OLLAMA_API_KEY': 'fixture'}))
    assert calls == [1] and report['stop_reason'] == 'unknown_cost'


def test_timeout_continuation_keeps_later_valid_results(tmp_path):
    manifest, data, plan = fixture_run()
    calls = []
    async def fake(url, *args):
        calls.append(url)
        if len(calls) == 1:
            raise asyncio.TimeoutError()
        return 200, raw(envelope('ollama' if 'ollama.com' in url else 'openai'))
    report = asyncio.run(runner.execute(plan, manifest, data, tmp_path/'out',
        {mid: RATE for mid in plan['models']}, approved_upload=True, budget_usd='1',
        request_reserve_usd='.1', max_calls=4, transport=fake, continue_after_timeout=True,
        credentials={'OPENAI_API_KEY': 'fixture', 'OLLAMA_API_KEY': 'fixture'}))
    assert len(calls) == 4 and report['completed']
    assert report['models']['gpt-6-sol']['timeout_count'] == 1
    assert report['models']['gpt-6-sol']['accuracy']['correct'] == 1
    assert report['models']['gemma4:31b']['accuracy']['correct'] == 2
    assert metrics.money(report['known_cost_estimate_usd']) > 0
    assert report['unresolved_reservation_usd'] == '0.1'


@pytest.mark.parametrize('limit,reason', [(1, 'max_calls'), (4, 'budget_reservation')])
def test_call_count_and_budget_limit(tmp_path, limit, reason):
    manifest, data, plan = fixture_run()
    calls = []
    async def fake(*args):
        calls.append(1)
        return 200, raw(envelope('openai'))
    report = asyncio.run(runner.execute(plan, manifest, data, tmp_path/'out',
        {mid: RATE for mid in plan['models']}, approved_upload=True, budget_usd='.1',
        request_reserve_usd='.1', max_calls=limit, transport=fake,
        credentials={'OPENAI_API_KEY': 'fixture', 'OLLAMA_API_KEY': 'fixture'}))
    assert calls == [1] and report['stop_reason'] == reason
    assert not report['completed']


def test_interrupted_inflight_request_stays_in_journal(tmp_path):
    manifest, data, plan = fixture_run()
    out = tmp_path/'out'
    async def interrupted(*args):
        assert (out/'00001.started.json').exists()
        raise asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(runner.execute(plan, manifest, data, out,
            {mid: RATE for mid in plan['models']}, approved_upload=True, budget_usd='1',
            request_reserve_usd='.1', max_calls=4, transport=interrupted,
            credentials={'OPENAI_API_KEY': 'fixture', 'OLLAMA_API_KEY': 'fixture'}))
    assert (out/'00001.started.json').exists()
    assert not list(out.glob('*.result.json')) and not (out/'summary.json').exists()


def test_manifest_and_image_changes_rejected(tmp_path):
    common = dict(COMMON, system=inputs.adapter.CROSSCHECK_SYSTEM_PROMPT)
    evidence = dict(window_s=5.0, jpeg_sha256=[hashlib.sha256(base64.b64decode(im)).hexdigest()
                                             for im in common['images']])
    inputs.save(tmp_path/'A.input.json', dict(common=common, evidence=evidence))
    manifest = dict(version=inputs.VERSION, mode='standalone', case_ids=['A'],
                    labels={'A': 'normal_activity'}, sources=inputs.code_hashes(),
                    files={'A.input.json': inputs.sha(tmp_path/'A.input.json')})
    inputs.save(tmp_path/'manifest.json', manifest)
    inputs.save(tmp_path/'manifest_hash.json', dict(sha256=inputs.digest(manifest)))
    assert inputs.load_bundle(tmp_path)[1]['A'] == common
    (tmp_path/'A.input.json').write_text('{}')
    with pytest.raises(ValueError, match='changed input'):
        inputs.load_bundle(tmp_path)
    manifest['labels']['A'] = 'observed_fall'
    (tmp_path/'manifest.json').write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match='changed manifest'):
        inputs.load_bundle(tmp_path)


def test_extract_exact_last_five_seconds_and_identical_images(tmp_path):
    cv2 = pytest.importorskip('cv2')
    np = pytest.importorskip('numpy')
    path = tmp_path/'neutral.avi'
    writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*'MJPG'), 10, (64, 40))
    assert writer.isOpened()
    for n in range(71):
        writer.write(np.full((40, 64, 3), n, dtype=np.uint8))
    writer.release()
    common, evidence = inputs.extract(tmp_path, dict(source_path=path.name, sha256=inputs.sha(path),
        frames=71, fps=10, height=40, width=64))
    assert len(common['images']) == 12
    assert evidence['source_times_s'][0] == 2 and evidence['source_times_s'][-1] == 7
    assert not evidence['history_incomplete']
    assert len(set(evidence['frame_indices'])) == 12
    assert 'neutral.avi' not in json.dumps(common)
    assert all(hashlib.sha256(base64.b64decode(im)).hexdigest() == h
               for im, h in zip(common['images'], evidence['jpeg_sha256']))
    for model in providers.MODELS.values():
        assert all(im in json.dumps(providers.payload(model, common)) for im in common['images'])


@pytest.mark.parametrize('value', [None, True, '-1', 'NaN', 'Infinity'])
def test_bad_budget(value):
    with pytest.raises(ValueError):
        metrics.money(value)


def test_error_codes_without_echoed_credentials_or_prose():
    body = raw({'error': {'type': 'rate_limit_reached_error', 'code': '1302',
                         'message': 'SECRET data:image/jpeg;base64,private', 'param': 'SECRET'}})
    details = providers.error_details(body, 'SECRET')
    assert details == {'type': 'rate_limit_reached_error', 'code': '1302'}
    assert 'SECRET' not in json.dumps(details)
    assert providers.error_details(b'not JSON', 'SECRET') == {}
    assert runner.diagnostic_headers({'Retry-After': '60', 'Authorization': 'SECRET',
        'X-Request-ID': 'SECRET', 'Set-Cookie': 'private', 'X-RateLimit-Remaining': '0'}, 'SECRET') == {
        'retry-after': '60', 'x-ratelimit-remaining': '0'}


def test_low_reasoning_profile_is_explicit_and_baseline_unchanged():
    for mid in ('muse-spark-1.2', 'glm-5.3-flash'):
        model = providers.MODELS[mid]
        baseline = providers.payload(model, COMMON)
        assert 'reasoning_effort' not in baseline
        low = providers.payload(model, COMMON, 'low_reasoning')
        assert low.pop('reasoning_effort') == 'low' and low == baseline
    with pytest.raises(ValueError, match='unknown model profile'):
        providers.payload(providers.MODELS['kimi-k3'], COMMON, 'typo')


def test_http_error_has_diagnostics_but_not_invented_cost():
    async def limited(*args):
        return 429, raw({'error': {'type': 'engine_overloaded_error', 'message': 'SECRET'}})
    result = asyncio.run(runner.invoke(providers.MODELS['kimi-k3'], COMMON, 'SECRET', RATE,
                                       transport=limited))
    assert result['provider_error'] == {'type': 'engine_overloaded_error'}
    assert result['cost_estimate_usd'] is None and 'SECRET' not in json.dumps(result)


def test_derived_bundle_preserves_media_labels_and_old_files(tmp_path):
    source = tmp_path/'source'
    source.mkdir()
    common = dict(COMMON, system=inputs.adapter.CROSSCHECK_SYSTEM_PROMPT)
    evidence = dict(window_s=5.0, jpeg_sha256=[hashlib.sha256(base64.b64decode(im)).hexdigest()
                                             for im in common['images']])
    inputs.save(source/'A.input.json', dict(common=common, evidence=evidence))
    manifest = dict(version=inputs.VERSION, mode='standalone', scope='pilot', case_ids=['A'],
        labels={'A': 'normal_activity'}, sources={'old_code': 'original_hash'},
        condition={'prompt': 'runtime_crosscheck_with_findings'},
        files={'A.input.json': inputs.sha(source/'A.input.json')})
    inputs.save(source/'manifest.json', manifest)
    inputs.save(source/'manifest_hash.json', dict(sha256=inputs.digest(manifest)))
    before = {p.name: inputs.sha(p) for p in source.iterdir()}
    derived, data = inputs.derive_bundle(source, tmp_path/'derived', 'explicit_json_v2')
    assert data['A']['images'] == common['images'] and data['A']['text'] == common['text']
    assert data['A']['system'].startswith(common['system'])
    assert 'FOUR NUMBERS' in data['A']['system']
    assert derived['labels'] == manifest['labels']
    assert derived['parent_sources'] == manifest['sources']
    assert {p.name: inputs.sha(p) for p in source.iterdir()} == before


def test_timeout_keeps_received_headers_and_partial_body_size(monkeypatch):
    monkeypatch.setattr(runner, 'TIMEOUT_S', .01)
    class Context:
        def __init__(self, value):
            self.value = value
        async def __aenter__(self):
            return self.value
        async def __aexit__(self, *args):
            return False
    async def chunks(size):
        yield b'{'
        await asyncio.sleep(1)
    response = SimpleNamespace(status=200, headers={'X-Request-ID': 'req_123',
        'Retry-After': '30', 'Set-Cookie': 'SECRET'},
        content=SimpleNamespace(iter_chunked=chunks))
    fake = SimpleNamespace(ClientTimeout=lambda **kw: kw,
        ClientSession=lambda **kw: Context(SimpleNamespace(post=lambda *a, **kw: Context(response))))
    monkeypatch.setitem(sys.modules, 'aiohttp', fake)
    result = asyncio.run(runner.invoke(providers.MODELS['kimi-k3'], COMMON, 'SECRET', RATE))
    assert result['outcome'] == 'timeout' and result['http_status'] == 200
    progress = result['transport_diagnostics']
    assert progress['stage'] == 'reading_body' and progress['response_bytes'] == 1
    assert progress['headers_received_s'] >= 0
    assert progress['response_headers'] == {'x-request-id': 'req_123', 'retry-after': '30'}
    assert result['cost_estimate_usd'] is None and result['label'] is None
    assert 'SECRET' not in json.dumps(result)


def test_low_profile_in_plan_and_actual_request(tmp_path):
    manifest, data, _ = fixture_run()
    plan = runner.make_plan(manifest, data, ['glm-5.3-flash'], profile='low_reasoning')
    assert plan['model_profile'] == 'low_reasoning'
    seen = []
    async def fake(url, body, headers):
        seen.append(json.loads(body))
        return 200, raw(envelope('zai'))
    report = asyncio.run(runner.execute(plan, manifest, data, tmp_path/'run',
        {'glm-5.3-flash': RATE}, approved_upload=True, budget_usd='1',
        request_reserve_usd='.1', max_calls=1, credentials={'ZAI_API_KEY': 'fixture'}, transport=fake))
    assert report['actual_calls'] == 1 and seen[0]['reasoning_effort'] == 'low'
