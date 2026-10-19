"""Live dialogue and real Manager/TTS; file audio sink and fake robot actions."""
import argparse
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys
from threading import RLock
import time
from types import SimpleNamespace
import wave

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--output', required=True, type=Path)
parser.add_argument('--piper-model', required=True, type=Path)
parser.add_argument('--python-dependencies', type=Path,
                    help='Optional Piper site-packages, appended after the ROS/system packages')
parser.add_argument('--domain', type=int, default=204)
args = parser.parse_args()
if args.output.exists() and any(args.output.iterdir()):
    parser.error('--output must be a new or empty directory')
if args.python_dependencies:
    sys.path.append(str(args.python_dependencies.resolve()))
import numpy as np

from malbut_agent_server import factory, voice_lab_graph
from malbut_agent_server.voice_lab import _dialogue_settings
from malbut_agent_server.providers.openai_responses import OpenAIResponsesProvider
from malbut_agent_server.mission_audio_cases import NOTICE_TEXTS
from malbut_tts.node import create_tts_node
from malbut_tts.runtime import SpeechRuntime
from malbut_tts.prerecorded import PrerecordedAudio
from malbut_tts.piper_synthesis import PiperSynthesizer

ROOT = args.output.resolve()
ROOT.mkdir(parents=True, exist_ok=True)
REPO = Path(__file__).resolve().parents[3]
MODEL = str(args.piper_model.resolve())
ROOT.joinpath('audio').mkdir(exist_ok=True)
started = time.monotonic()
lock = RLock()
records = []
checks = {}
models = []
payloads = {}
audio_files = []
report = {
    'started_at': datetime.now(timezone.utc).isoformat(),
    'expectations': {
        'utterances': ['지도 만들어줘', '작업실로'],
        'first_decision': {'type': 'clarification', 'clarification_for': 'mapping_name'},
        'second_decision': {'tool_name': 'request_mapping', 'arguments': {'map_name': '작업실'}},
        'capability': 'autoslam',
        'required_voice_path': ['Agent SpeechRequest', 'Manager accepted', 'private TTS request',
                                'real TTS runtime', 'WAV output', 'Manager finished'],
    },
    'scope': {
        'llm': 'live configured OpenAI model', 'agent': 'production source',
        'manager': 'production SystemManagerNode and SpeechCoordinator',
        'tts': 'production TtsNode + SpeechRuntime + PiperSynthesizer/prerecorded notices',
        'audio_device': 'WAV file sink; no physical speaker or realtime device drain',
        'input': 'text injected at final STT transcript boundary; no microphone/ASR',
        'robot': 'isolated fake AutoSlam Action; no driving or actual map creation',
    },
}


def record(kind, **values):
    item = {'t': round(time.monotonic() - started, 6), 'kind': kind, **deepcopy(values)}
    with lock:
        records.append(item)
        with ROOT.joinpath('events.jsonl').open('a') as output:
            output.write(json.dumps(item, ensure_ascii=False) + '\n')
    return item


def check(name, condition):
    checks[name] = bool(condition)
    print(json.dumps({'check': name, 'pass': bool(condition)}, ensure_ascii=False), flush=True)


def snapshot():
    return {
        str(f.relative_to(REPO)): hashlib.sha256(f.read_bytes()).hexdigest()
        for package in ('malbut_agent_server', 'malbut_system_manager', 'malbut_tts')
        for f in sorted((REPO / package / package).rglob('*.py'))
    }


def save():
    report.update(checks=checks, models=models, audio_files=audio_files, events=records)
    ROOT.joinpath('result.json').write_text(json.dumps(report, ensure_ascii=False, indent=2))


report['source_sha256'] = snapshot()
save()
settings = _dialogue_settings(SimpleNamespace(chat=True, provider=None, model=None, env_file=None))
assert settings.openai_api_key, 'configured API credential is missing'
report.update(model=settings.openai_model, reasoning_effort=settings.openai_reasoning_effort)

# Capture actual provider choices, without headers, credentials or settings.
original_payload = OpenAIResponsesProvider.build_payload


def capture_payload(self, request, *args, **kwargs):
    result = original_payload(self, request, *args, **kwargs)
    payloads.setdefault(request.request_id, []).append(deepcopy(result.get('tool_choice')))
    return result


OpenAIResponsesProvider.build_payload = capture_payload
original_build = factory.build_orchestrator


def instrument_build(*args, **kwargs):
    runtime = original_build(*args, **kwargs)
    original_complete = runtime.provider.complete

    def complete(request, *args, **kwargs):
        result = original_complete(request, *args, **kwargs)
        item = {
            'request_id': request.request_id, 'utterance': request.utterance,
            'mapping_name_requested': request.mapping_name_requested,
            'tool_choices': payloads.pop(request.request_id, []),
            'decision': result.decision.to_dict(),
            'provider': result.to_dict().get('provider'),
            'response_id': result.to_dict().get('response_id'),
        }
        models.append(item)
        record('provider_response', **item)
        return result

    runtime.provider.complete = complete
    return runtime


factory.build_orchestrator = instrument_build
# Match the speech launch's prerecorded_audio=true default.
from malbut_agent_server import ros_communication
original_communication = ros_communication.create_communication_node


def communication_with_launch_defaults(*args, **kwargs):
    kwargs.setdefault('prerecorded_audio', True)
    return original_communication(*args, **kwargs)


ros_communication.create_communication_node = communication_with_launch_defaults
report['prerecorded_audio'] = True
# Extend remaps in this process only, including the Manager's private TTS side.
voice_lab_graph._REMAPS += (
    '/malbut/tts/request', '/malbut/tts/playback_status',
    '/malbut/tts/playback_control', '/malbut/speech/playback_control',
)
graph = voice_lab_graph.VoiceLabGraph(lambda event: record('graph', data=event), args.domain,
                                     dialogue_settings=settings)
tts = None
runtime_holder = {}


class WaveSink:
    """Replace only sound-card playback; keep generated PCM and runtime states."""

    def __init__(self, on_state, cancel_event):
        self.on_state, self.cancel = on_state, cancel_event
        request = runtime_holder['runtime']._active
        self.playback_id, self.text = request.playback_id, request.text
        self.audio_id = request.audio_id
        self.chunks = []
        self.rate = None
        self.playing = False

    def wait_for_capacity(self, **kwargs):
        if self.cancel.is_set():
            raise RuntimeError('file sink canceled')

    def write(self, audio, sample_rate):
        self.wait_for_capacity()
        assert self.rate is None or self.rate == sample_rate
        self.rate = sample_rate
        self.chunks.append(np.asarray(audio, dtype=np.float32).copy())
        if not self.playing:
            self.playing = True
            self.on_state('playing')

    def finish(self):
        pcm = np.concatenate(self.chunks)
        assert pcm.size and np.isfinite(pcm).all() and np.max(np.abs(pcm)) > 0
        path = ROOT / 'audio' / (self.playback_id + '.wav')
        with wave.open(str(path), 'wb') as stream:
            stream.setparams((1, 2, self.rate, 0, 'NONE', 'not compressed'))
            stream.writeframes((np.clip(pcm, -1, 1) * 32767).astype('<i2').tobytes())
        item = dict(playback_id=self.playback_id, text=self.text, audio_id=self.audio_id,
                    path=str(path), sample_rate=self.rate, frames=int(pcm.size),
                    duration_s=round(pcm.size / self.rate, 3),
                    sha256=hashlib.sha256(path.read_bytes()).hexdigest())
        audio_files.append(item)
        record('wav_finished', **item)

    def pause(self):
        self.on_state('paused')
        return True

    def resume(self):
        self.on_state('playing')
        return True

    def stop(self):
        self.cancel.set()

    def close(self):
        pass


def runtime_factory(on_status):
    synth = PiperSynthesizer(MODEL)
    synth.load()

    def status(playback_id, state, interim, request_id):
        record('tts_runtime_status', playback_id=playback_id, state=state,
               interim=interim, request_id=request_id)
        on_status(playback_id, state, interim, request_id)

    runtime = SpeechRuntime(synth, WaveSink, status, prerecorded=PrerecordedAudio())
    runtime_holder['runtime'] = runtime
    original_submit = runtime.submit

    def submit(text, request_type=0, **kwargs):
        record('tts_runtime_submit', text=text, request_type=request_type, **kwargs)
        return original_submit(text, request_type, **kwargs)

    runtime.submit = submit
    return runtime


def messages(kind, playback_id=None):
    with lock:
        return [e for e in records if e['kind'] == kind
                and (playback_id is None or e.get('playback_id') == playback_id)]


def wait(predicate, timeout=60):
    graph._wait(predicate, 'expected end-to-end event did not arrive', timeout=timeout)


def statuses(playback_id):
    return [e['state'] for e in messages('public_status', playback_id)]


def voice_finished(text):
    return any('finished' in statuses(e['playback_id'])
               for e in messages('public_speech') if e['text'] == text)


try:
    graph.start()
    from malbut_interfaces.msg import SpeechRequest, SpeechPlaybackStatus

    def watch(kind, fields):
        return lambda message: record(kind, **{k: getattr(message, k) for k in fields})

    speech_fields = ('text', 'request_id', 'playback_id', 'audio_id', 'request_type', 'interim')
    state_fields = ('request_id', 'playback_id', 'state', 'interim')
    for topic, kind, message_type, fields in (
        ('/malbut/speech/response', 'public_speech', SpeechRequest, speech_fields),
        ('/malbut/tts/request', 'private_tts_request', SpeechRequest, speech_fields),
        ('/malbut/tts/playback_status', 'private_status', SpeechPlaybackStatus, state_fields),
        ('/malbut/speech/playback_status', 'public_status', SpeechPlaybackStatus, state_fields),
    ):
        graph._sender.create_subscription(message_type, topic, watch(kind, fields), 100)
    tts = create_tts_node(runtime_factory)
    graph._executor.add_node(tts)
    wait(lambda: graph.manager._speech.client.service_is_ready()
         and graph.manager._speech.requests.get_subscription_count() == 2)
    report['namespace'] = graph.namespace
    report['domain_id'] = graph.domain_id
    report['topic_graph'] = {
        topic: {
            'publishers': [n.node_name for n in graph._sender.get_publishers_info_by_topic(topic)],
            'subscribers': [n.node_name for n in graph._sender.get_subscriptions_info_by_topic(topic)],
        } for topic in (graph.namespace + '/speech/response', graph.namespace + '/tts/request',
                        graph.namespace + '/tts/playback_status', graph.namespace + '/speech/playback_status')
    }
    graph.set_behavior('autoslam', 'success', delay_s=1.0)
    uid = graph.send('지도 만들어줘')
    first = graph.wait_reply(uid)
    report['first_reply'] = first
    print(json.dumps({'input': '지도 만들어줘', 'reply': first}, ensure_ascii=False), flush=True)
    check('first_live_llm', len(models) == 1 and models[0]['provider'] == 'openai' and models[0]['response_id'])
    check('asks_map_name', models[0]['decision']['type'] == 'clarification'
          and models[0]['decision'].get('clarification_for') == 'mapping_name')
    check('no_execution_before_name', not graph.goals and not graph.events)
    wait(lambda: voice_finished(first['text']))
    check('question_finished_through_manager', voice_finished(first['text']))
    check('name_context_active_after_question', graph.agent.dialogue._mapping_name_question is not None)
    report['answer_sent_at'] = round(time.monotonic() - started, 6)
    uid = graph.send('작업실로')
    second = graph.wait_reply(uid)
    report['second_reply'] = second
    print(json.dumps({'input': '작업실로', 'reply': second}, ensure_ascii=False), flush=True)
    wait(lambda: any(e.get('terminal') and e.get('state') == 'SUCCEEDED' for e in graph.events))
    wait(lambda: voice_finished(NOTICE_TEXTS['operation.succeeded']))
    wait(lambda: not graph.manager._speech.playbacks)
    until = time.monotonic() + .25
    while time.monotonic() < until:
        graph.spin()
    check('second_live_llm', len(models) == 2 and models[1]['provider'] == 'openai' and models[1]['response_id'])
    second_model = models[1]
    check('second_has_pending_name_context', second_model['mapping_name_requested'])
    check('second_uses_auto_tool_choice', second_model['tool_choices'][-1] == 'auto')
    check('correct_mapping_tool_and_name', second_model['decision']['tool_name'] == 'request_mapping'
          and second_model['decision']['arguments'] == {'map_name': '작업실'})
    check('exactly_one_autoslam_goal', len(graph.goals) == 1
          and graph.goals[0]['capability_id'] == 'autoslam'
          and graph.goals[0]['arguments']['map_name'] == '작업실')
    check('no_navigation_goal', all(g['capability_id'] != 'navigate_to_pose' for g in graph.goals))
    check('manager_mission_succeeded', any(e.get('terminal') and e.get('state') == 'SUCCEEDED'
                                         for e in graph.events))
    check('completion_notice_finished', voice_finished(NOTICE_TEXTS['operation.succeeded']))
    for name, text in (('question', first['text']), ('start', second['text']),
                       ('completion', NOTICE_TEXTS['operation.succeeded'])):
        matching = [e for e in messages('public_speech') if e['text'] == text]
        check(name + '_published_once', len(matching) == 1)
        pid = matching[0]['playback_id']
        check(name + '_manager_accepted', statuses(pid).count('accepted') == 1)
        check(name + '_forwarded_once', len(messages('private_tts_request', pid)) == 1)
        check(name + '_actual_tts_received', len(messages('tts_runtime_submit', pid)) == 1)
        check(name + '_nonempty_audio', any(a['playback_id'] == pid and a['frames'] > 0 for a in audio_files))
        check(name + '_status_returned', statuses(pid).count('finished') == 1
              and any(e['state'] == 'finished' for e in messages('private_status', pid)))
    check('all_public_speech_admitted', all('accepted' in statuses(e['playback_id'])
                                           for e in messages('public_speech')))
    check('no_voice_failure', not any(e['state'] == 'failed' for e in messages('public_status')))
    check('private_tts_only_manager_publishes', report['topic_graph'][graph.namespace + '/tts/request']['publishers']
          == ['system_manager'])
    report['goals'] = deepcopy(graph.goals)
    report['mission_events'] = deepcopy(graph.events)
    check('source_unchanged_during_test', report['source_sha256'] == snapshot())
except Exception as error:
    report['error'] = {'type': type(error).__name__, 'message': str(error)}
    print(json.dumps({'error': report['error']}, ensure_ascii=False), flush=True)
finally:
    if tts is not None:
        graph._executor.remove_node(tts)
        tts.destroy_node()
    graph.close()
    report['finished_at'] = datetime.now(timezone.utc).isoformat()
    report['passed'] = bool(checks) and all(checks.values()) and 'error' not in report
    save()

print(json.dumps({'passed': report['passed'], 'checks': len(checks),
                  'audio_files': len(audio_files)}, ensure_ascii=False), flush=True)
raise SystemExit(0 if report['passed'] else 1)
