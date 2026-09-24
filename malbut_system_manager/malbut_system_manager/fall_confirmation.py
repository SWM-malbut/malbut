"""Manager-owned, ROS-independent routing of VLM checks to the conversation Agent."""

from collections import OrderedDict
from dataclasses import dataclass
import json


ASSESSMENTS = {'confirmed_incident', 'resolved', 'unknown'}
VIDEO_SUMMARIES = {
    'observed_fall': '영상에서 넘어지는 동작이 관측되었습니다.',
    'suspected_fall': '영상에서 낙상이 의심되지만, 실제 낙상 여부는 확정되지 않았습니다.',
    'unobservable': '영상만으로 사람의 상태를 확인하기 어렵습니다.',
    'normal_activity': '이전에 넘어지는 동작이 관측되었으며, 최근 영상에서는 정상적인 움직임이 보입니다.',
}


def _identifier(value):
    return isinstance(value, str) and bool(value.strip()) and len(value) <= 200


@dataclass(frozen=True)
class ConfirmationRequest:
    """Transport correlation stays with the Manager, outside spoken prompts."""

    boot_id: str
    incident_id: str
    question_id: str
    subject_key: str
    revision: int
    summary: str

    @property
    def request_id(self):
        return self.question_id

    def decision(self, action):
        return dict(action=action, boot_id=self.boot_id, incident_id=self.incident_id,
                    question_id=self.question_id, evidence_revision=self.revision)


class FallConfirmationCoordinator:
    """Deduplicate replayed handoffs and reject obsolete evidence/results.

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
        if kind not in {'incident_opened', 'incident_updated', 'incident_resolved',
                        'confirmation_completed', 'analysis_completed', 'question_requested'}:
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
            if (not _identifier(qid) or not _identifier(subject)
                    or not isinstance(video, str) or video not in VIDEO_SUMMARIES
                    or (video == 'normal_activity'
                        and event.get('reason') != 'prior_fall_observed')):
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
        if revision < self.revisions.get(iid, 0):
            return False
        if revision > self.revisions.get(iid, 0):
            self.revisions[iid] = revision
            for rid, request in tuple(self.requests.items()):
                if request.incident_id == iid:
                    del self.requests[rid]
                    self._remember(rid)
        if kind in {'incident_resolved', 'confirmation_completed'}:
            if kind == 'incident_resolved':
                self.terminal_revisions[iid] = revision
            elif _identifier(event.get('question_id')):
                self._remember(event['question_id'])
            for rid, request in tuple(self.requests.items()):
                if request.incident_id == iid:
                    del self.requests[rid]
                    self._remember(rid)
            return True
        if kind == 'analysis_completed' and event.get('video_assessment') == 'normal_activity':
            # Runtime revalidates that there was no earlier observed fall or
            # outstanding confirmation; a later normal frame cannot clear it.
            if not any(r.incident_id == iid for r in self.requests.values()):
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
        # The runtime guarantees at most one current question per incident.
        for rid, request in tuple(self.requests.items()):
            if request.incident_id == iid:
                del self.requests[rid]
                self._remember(rid)
        self.requests[qid] = ConfirmationRequest(
            boot, iid, qid, subject, revision,
            VIDEO_SUMMARIES[video] + ' 실제로 넘어진 것인지와 도움 필요 여부를 확인해 주세요.')
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
        self._remember(request.request_id, decision)
        return True

    def fail(self, request):
        if self.requests.get(request.request_id) != request or self.closed:
            return False
        decision = request.decision('confirmation_failed')
        self.commands.append(decision)
        del self.requests[request.request_id]
        self._remember(request.request_id, decision)
        return True

    def drain_commands(self):
        commands, self.commands = tuple(self.commands), []
        return commands

    def close(self):
        self.closed = True
        self.requests.clear()
        self.commands.clear()
