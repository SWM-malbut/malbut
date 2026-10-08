"""Fall-owned, ROS-independent routing of VLM checks to the conversation Agent."""

from collections import OrderedDict, deque
from dataclasses import dataclass
import json
import math
from typing import Optional, Tuple


ASSESSMENTS = {'confirmed_incident', 'resolved', 'unknown'}
VIDEO_SUMMARIES = {
    'observed_fall': '영상에서 넘어지는 동작이 관측되었습니다.',
    'suspected_fall': '영상에서 낙상이 의심되지만, 실제 낙상 여부는 확정되지 않았습니다.',
    'unobservable': '영상만으로 사람의 상태를 확인하기 어렵습니다.',
    'normal_activity': '이전에 넘어지는 동작이 관측되었으며, 최근 영상에서는 정상적인 움직임이 보입니다.',
}
APPROACH_OUTCOMES = {'arrived', 'no_map', 'no_path', 'timeout', 'failed', 'rejected'}
RETURN_OUTCOMES = {'returned', 'failed'}
# The runtime looks after arrival (Pose, then one Cloud call). Then ask anyway.
CHECK_WAIT_S = 15.0


def _identifier(value):
    return isinstance(value, str) and bool(value.strip()) and len(value) <= 200


@dataclass(frozen=True)
class ConfirmationRequest:
    """Transport correlation stays with the coordinator, outside spoken prompts."""

    boot_id: str
    incident_id: str
    question_id: str
    subject_key: Optional[str]
    revision: int
    summary: str
    # Map point to drive near first: the runtime was not sure it is a person.
    approach_target: Optional[Tuple[float, float]] = None

    @property
    def request_id(self):
        return self.question_id

    def decision(self, action):
        return dict(action=action, boot_id=self.boot_id, incident_id=self.incident_id,
                    question_id=self.question_id, evidence_revision=self.revision)


@dataclass(frozen=True)
class ReturnJob:
    """Drive back after a check found no person; the incident is already closed."""

    boot_id: str
    incident_id: str
    question_id: str
    revision: int

    def decision(self, outcome):
        return dict(action='return_result', boot_id=self.boot_id, incident_id=self.incident_id,
                    question_id=self.question_id, evidence_revision=self.revision,
                    outcome=outcome)


class FallConfirmationCoordinator:
    """
    Keep outstanding conversations stable while newer evidence arrives.

    It never calls a guardian or interprets transport failures as user silence.
    Only successful Agent final results may carry a help-needed judgment.
    """

    def __init__(self, *, runtime_id=''):
        self.runtime_id = runtime_id
        self.boot_id = None
        self.retired_boots = set()
        self.requests = OrderedDict()
        self.revisions = {}
        self.terminal_revisions = {}
        self.finished = OrderedDict()
        self.commands = []
        self.closed = False
        # question_id -> 'approach' | 'check' | 'confirm'; check carries its start time.
        self.stages = {}
        self.check_since = {}
        self.returns = deque()

    def _remember(self, request_id, decision=None):
        self.finished[request_id] = decision
        while len(self.finished) > 4096:
            self.finished.popitem(last=False)

    def receive(self, payload):
        """Accept only bounded, structured VLM metadata, never its free-form prose."""
        if self.closed or not isinstance(payload, str) or len(payload) > 65536:
            return False
        try:
            event = json.loads(payload)
        except (TypeError, ValueError):
            return False
        if not isinstance(event, dict):
            return False
        kind = event.get('kind')
        if not isinstance(kind, str) or kind not in {
                'incident_opened', 'incident_updated', 'incident_resolved', 'incident_merged',
                'confirmation_completed', 'analysis_completed', 'question_requested',
                'person_check_completed'}:
            return False
        scope = event.get('confirmation_scope', 'subject')
        subject = event.get('subject_key')
        # A missing person ID is not permission to address the whole scene.
        # Only an explicit scene handoff may retain a null subject through the
        # managed conversation; the VLM runtime owns its non-clearance rule.
        if (scope not in ('subject', 'scene')
                or (scope == 'scene' and ('subject_key' not in event or subject is not None))):
            return False
        boot = event.get('boot_id')
        iid = event.get('incident_id')
        revision = event.get('evidence_revision')
        if (not _identifier(boot) or not _identifier(iid)
                or type(revision) is not int or revision < 1
                or (self.runtime_id and event.get('runtime_id') != self.runtime_id)
                or boot in self.retired_boots):
            return False
        if kind == 'question_requested':
            qid, subject = event.get('question_id'), event.get('subject_key')
            video = event.get('video_assessment')
            if (not _identifier(qid) or (scope == 'subject' and not _identifier(subject))
                    or not isinstance(video, str) or video not in VIDEO_SUMMARIES
                    or (scope == 'scene' and video not in {'observed_fall', 'suspected_fall'})
                    or (video == 'normal_activity'
                        and event.get('reason') != 'prior_fall_observed')):
                return False
            target = event.get('approach_target')
            if target is not None:
                if (not isinstance(target, dict) or set(target) != {'x', 'y', 'frame'}
                        or target['frame'] != 'map'
                        or any(isinstance(target[k], bool)
                               or not isinstance(target[k], (int, float))
                               or not math.isfinite(target[k]) for k in ('x', 'y'))):
                    return False
                target = (float(target['x']), float(target['y']))
        if kind == 'person_check_completed' and (
                not _identifier(event.get('question_id'))
                or event.get('reason') not in {'person', 'not_a_person'}):
            return False
        if kind == 'incident_merged':
            targets = event.get('merged_into_incident_ids')
            if (scope != 'scene' or event.get('reason') != 'findings_associated'
                    or not isinstance(targets, list) or not 1 <= len(targets) <= 128
                    or not all(_identifier(t) and t != iid for t in targets)
                    or len(set(targets)) != len(targets)):
                return False
        if self.boot_id != boot:
            if self.boot_id is not None:
                self.retired_boots.add(self.boot_id)
            self.boot_id = boot
            self.requests.clear()
            self.revisions.clear()
            self.terminal_revisions.clear()
            self.finished.clear()
            self.commands.clear()
            self.stages.clear()
            self.check_since.clear()
            self.returns.clear()
        # A question retains its original evidence revision until its session
        # ends. Its replay or completion may follow a newer incident update.
        if (revision < self.revisions.get(iid, 0)
                and kind not in {'question_requested', 'confirmation_completed'}):
            return False
        if revision > self.revisions.get(iid, 0):
            self.revisions[iid] = revision
        if kind == 'person_check_completed':
            qid = event['question_id']
            request = self.requests.get(qid)
            if request is None or request.incident_id != iid or self.stages.get(qid) != 'check':
                return True
            self.check_since.pop(qid, None)
            if event['reason'] == 'person':
                self.stages[qid] = 'confirm'
                return True
            # No person: no question; the runtime closes the case. Go back.
            del self.requests[qid]
            self.stages.pop(qid, None)
            self._remember(qid)
            self.returns.append(ReturnJob(boot, iid, qid, request.revision))
            return True
        if kind in {'incident_resolved', 'incident_merged'}:
            self.terminal_revisions[iid] = revision
            for rid, request in tuple(self.requests.items()):
                if request.incident_id == iid:
                    del self.requests[rid]
                    self.stages.pop(rid, None)
                    self.check_since.pop(rid, None)
                    self._remember(rid)
            return True
        if kind == 'confirmation_completed':
            qid = event.get('question_id')
            if _identifier(qid):
                self._remember(qid)
                request = self.requests.get(qid)
                if request is not None and request.incident_id == iid:
                    del self.requests[qid]
            return True
        if kind == 'analysis_completed' and event.get('video_assessment') == 'normal_activity':
            # Runtime revalidates that there was no earlier observed fall or
            # outstanding confirmation; a later normal frame cannot clear it.
            if scope != 'scene' and not any(r.incident_id == iid for r in self.requests.values()):
                self.commands.append(dict(action='dismiss_normal', boot_id=boot,
                                          incident_id=iid, evidence_revision=revision))
            return True
        if kind != 'question_requested':
            return True
        if revision <= self.terminal_revisions.get(iid, 0):
            return True
        if qid in self.finished:
            # A repeated unanswered handoff also retries delivery of the final
            # decision, without repeating the conversation.
            decision = self.finished[qid]
            if (decision is not None and decision['incident_id'] == iid
                    and decision['evidence_revision'] == revision):
                self.commands.append(dict(decision))
            return True
        if qid in self.requests:
            return True
        if len(self.requests) >= 128:
            return False
        # Runtime replays unfinished handoffs. A newer one must wait for this
        # incident's current conversation instead of replacing it.
        if any(request.incident_id == iid for request in self.requests.values()):
            return True
        summary = VIDEO_SUMMARIES[video]
        if scope == 'scene':
            summary += (' 영상 속 대상과 대답하는 사람의 연결은 확인되지 않았습니다.'
                        ' 특정인을 지목하지 말고, 주변에 넘어졌거나 도움이 필요한 분이 있는지'
                        ' 확인해 주세요. 한 사람의 괜찮다는 답변을 다른 사람의 상태로 판단하지 마세요.')
        else:
            summary += ' 실제로 넘어진 것인지와 도움 필요 여부를 확인해 주세요.'
        self.requests[qid] = ConfirmationRequest(
            boot, iid, qid, subject, revision, summary, target)
        self.stages[qid] = 'approach' if target is not None else 'confirm'
        return True

    def next_work(self, now):
        """('return', job) | ('approach' | 'confirm', request) | None (wait)."""
        if self.returns:
            return 'return', self.returns[0]
        request = next(iter(self.requests.values()), None)
        if request is None:
            return None
        stage = self.stages.get(request.question_id, 'confirm')
        if stage == 'check':
            if now - self.check_since.get(request.question_id, now) < CHECK_WAIT_S:
                return None
            # The runtime did not answer in time: ask from here rather than wait.
            self.stages[request.question_id] = stage = 'confirm'
            self.check_since.pop(request.question_id, None)
        return stage, request

    def approach_done(self, request, outcome, now):
        """Report the drive; arrived waits for the runtime's look, else ask here."""
        if self.requests.get(request.request_id) != request or self.closed:
            return False
        if outcome not in APPROACH_OUTCOMES:
            outcome = 'failed'
        decision = request.decision('approach_result')
        decision['outcome'] = outcome
        self.commands.append(decision)
        if outcome == 'arrived':
            self.stages[request.question_id] = 'check'
            self.check_since[request.question_id] = now
        else:
            self.stages[request.question_id] = 'confirm'
        return True

    def return_done(self, job, outcome):
        if not self.returns or self.returns[0] != job or self.closed:
            return False
        self.returns.popleft()
        self.commands.append(job.decision(outcome if outcome in RETURN_OUTCOMES else 'failed'))
        return True

    def complete(self, request, *, situation_assessment, help_needed):
        if self.requests.get(request.request_id) != request or self.closed:
            return False
        if (not isinstance(situation_assessment, str) or situation_assessment not in ASSESSMENTS
                or type(help_needed) is not bool):
            raise ValueError('invalid final confirmation result')
        decision = request.decision('confirmation_result')
        decision.update(subject_key=request.subject_key,
                        situation_assessment=situation_assessment, help_needed=help_needed)
        self.commands.append(decision)
        del self.requests[request.request_id]
        self.stages.pop(request.request_id, None)
        self._remember(request.request_id, decision)
        return True

    def fail(self, request):
        if self.requests.get(request.request_id) != request or self.closed:
            return False
        decision = request.decision('confirmation_failed')
        self.commands.append(decision)
        del self.requests[request.request_id]
        self.stages.pop(request.request_id, None)
        self._remember(request.request_id, decision)
        return True

    def drain_commands(self):
        commands, self.commands = tuple(self.commands), []
        return commands

    def close(self):
        self.closed = True
        self.requests.clear()
        self.commands.clear()
        self.stages.clear()
        self.returns.clear()
