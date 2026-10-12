"""Join observed execution boundaries by IDs, without inferring missing stages."""


ROSOUT_LOGGERS = ('malbut_stt', 'malbut_agent_communication', 'malbut_tts')
AGENT_EVENTS = ('speech_transcript', 'dialogue_response_published')


def transactions(records, triggers):
    """Line numbers refer to events.jsonl; ordering is local observation time.

    ROS callbacks can be observed in a different order from their source logs.
    A missing event is not proof that an operation failed or did not happen.
    """
    by_uid = {}
    trigger_times = {t['utterance_id']: t['monotonic_s'] for t in triggers}

    def get(uid):
        return by_uid.setdefault(uid, dict(
            utterance_id=uid, triggered=uid in trigger_times, stages=[],
            observed=dict(stt_published=False, agent_received=False,
                          agent_response_published=False, tts_requested=False,
                          tts_finished=False)))

    def direct_uid(record):
        context = record.get('context') or {}
        return record.get('utterance_id') or context.get('utterance_id')

    for uid in trigger_times:
        get(uid)
    for record in records:
        uid = direct_uid(record)
        if isinstance(uid, str) and uid:
            get(uid)
    # Resolve playback-only events even if callback arrival preceded the request.
    playback_uids = {}
    for record in records:
        if record['event'] == 'tts_request' and record.get('request_id') in by_uid:
            playback_uids[record.get('playback_id')] = record['request_id']
    for line, record in enumerate(records, 1):
        event = record['event']
        uid = direct_uid(record)
        if not uid and event in ('tts_request', 'tts_playback'):
            uid = record.get('request_id') or playback_uids.get(record.get('playback_id'))
        if not isinstance(uid, str) or uid not in by_uid:
            continue
        item = get(uid)
        context = record.get('context') or {}
        stage = dict(event=event, event_line=line, monotonic_s=record['monotonic_s'],
                     wall_ns=record['wall_ns'])
        if uid in trigger_times:
            stage['seconds_from_trigger'] = record['monotonic_s'] - trigger_times[uid]
        for field in ('kind', 'generation', 'revision', 'decode_id', 'reason', 'error',
                      'text', 'state', 'playback_id', 'request_id', 'source_stamp_ns',
                      'native_decode'):
            if field in record or field in context:
                stage[field] = record.get(field, context.get(field))
        observed = item['observed']
        if event == 'transcript_published':
            observed['stt_published'] = True
        elif event == 'tts_request':
            observed['tts_requested'] = True
        elif event == 'tts_playback' and record.get('state') == 'finished':
            observed['tts_finished'] = True
        elif event == 'program_log':
            payload = record.get('payload') or {}
            stage['source_event'] = payload.get('event')
            stage['status'] = payload.get('status')
            if (record['logger'] == 'malbut_agent_communication'
                    and payload.get('event') == 'speech_transcript'
                    and payload.get('status') == 'received'):
                observed['agent_received'] = True
            if (record['logger'] == 'malbut_agent_communication'
                    and payload.get('event') == 'dialogue_response_published'):
                observed['agent_response_published'] = True
        item['stages'].append(stage)
    return dict(
        schema_version=1, ordering='local_observation_monotonic_time',
        missing_stage_meaning='not_observed_in_retained_window; not_proof_of_failure',
        coverage='STT instrumentation, selected ROS logs and speech topics; '
                 'no SQL BEGIN/COMMIT trace, LLM internals or native stderr',
        transactions=list(by_uid.values()))
