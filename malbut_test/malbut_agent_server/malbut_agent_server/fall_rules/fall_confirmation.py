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
# nobody_there: went back after waiting and found no one; the case stays open.
PERSON_CHECK_REASONS = {'person', 'not_a_person', 'partial_body', 'nobody_there'}
# Where the whole body rests (fall_full_view): two Cloud looks a few seconds apart.
POSTURE_WAIT_S = 60.0
POSTURE_REASONS = {'sitting_off_floor', 'standing', 'active_on_floor', 'on_floor', 'body_unseen',
                   'judgements_differ', 'posture_unconfirmed'}
# No question: off the floor, or sitting up busy on it (2026-10-14).
OFF_FLOOR_REASONS = {'sitting_off_floor', 'standing', 'active_on_floor'}
# A fall check yields to 직접 움직이기 and waits out another job (2026-10-12): the
# Manager refuses or interrupts the step, and the same step goes again a second
# later. Only that waiting is counted, never a drive, a look or a question in
# progress. A minute of it alerts the guardian and the robot goes back to the
# spot before asking; ten minutes in all and it stops trying (the guardian's
# 처리 완료 stops it at once).
WAITING_FOR = ('manual_drive', 'busy')
# The incident history notes a wait only once it lasts: the Agent finishing the
# previous conversation or the Manager's first state is a passing moment.
WAIT_NOTE_S = 3.0
WAIT_ALERT_S = 60.0
WAIT_LIMIT_S = 600.0
# Why a question was not asked: speech is off, the question failed, or the
# robot waited ten minutes in all.
FAILURE_REASONS = ('speech_unavailable', 'question_error', 'time_limit')


def _identifier(value):
    return isinstance(value, str) and bool(value.strip()) and len(value) <= 200


def _map_point(value):
    """(x, y) of a {'x', 'y', 'frame': 'map'} point, None when absent, False when malformed."""
    if value is None:
        return None
    if (not isinstance(value, dict) or set(value) != {'x', 'y', 'frame'} or value['frame'] != 'map'
            or any(isinstance(value[k], bool) or not isinstance(value[k], (int, float))
                   or not math.isfinite(value[k]) for k in ('x', 'y'))):
        return False
    return float(value['x']), float(value['y'])


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
    # Map point of the suspicion, to go back to after a minute of waiting.
    revisit_target: Optional[Tuple[float, float]] = None

    @property
    def request_id(self):
        return self.question_id

    def decision(self, action):
        return dict(action=action, boot_id=self.boot_id, incident_id=self.incident_id,
                    question_id=self.question_id, evidence_revision=self.revision)


@dataclass(frozen=True)
class ReturnJob:
    """Drive back without asking: no person, off the floor, or a revisit's report."""

    boot_id: str
    incident_id: str
    question_id: str
    revision: int
    reason: str = 'not_a_person'
    # The spot just checked, for the map screen while driving back.
    target: Optional[Tuple[float, float]] = None

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
        # question_id -> 'approach' | 'check' | 'view' | 'posture' | 'closer' | 'confirm';
        # check and posture carry their start time. closer: back to 1 m before
        # asking, after the view moved away (the user, 2026-10-13).
        self.stages = {}
        self.check_since = {}
        self.returns = deque()
        self._clear_waits()

    def _clear_waits(self):
        # question_id -> seconds waited before the current wait, and when its
        # share of the current wait began. Every queued request waits together.
        self.blocked_total = {}
        self.blocked_from = {}
        self.waiting_for = None  # 'manual_drive' | 'busy' while the robot waits
        self.waiting_sent = {}  # question_id -> reasons check_waiting recorded
        self.alerted = {}  # question_id -> seconds waited at the last alert or revisit
        self.revisits = {}  # question_id -> what it waited for before going back

    def _forget(self, qid):
        for table in (self.blocked_total, self.blocked_from, self.waiting_sent,
                      self.alerted, self.revisits):
            table.pop(qid, None)
        if not self.requests:
            self.waiting_for = None

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
                'person_check_completed', 'posture_check_completed'}:
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
            target, revisit = (_map_point(event.get('approach_target')),
                               _map_point(event.get('revisit_target')))
            if target is False or revisit is False:
                return False
        if kind == 'person_check_completed' and (
                not _identifier(event.get('question_id'))
                or event.get('reason') not in PERSON_CHECK_REASONS):
            return False
        if kind == 'posture_check_completed' and (
                not _identifier(event.get('question_id'))
                or event.get('reason') not in POSTURE_REASONS):
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
            self._clear_waits()
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
            reason = event['reason']
            if reason == 'person':
                self.stages[qid] = 'confirm'
                return True
            if reason == 'partial_body':
                self.stages[qid] = 'view'  # Move to where the whole body is visible.
                return True
            # No person: no question; the runtime closes the case (or, when it went
            # back after waiting, reports nobody there and keeps it open). Go back.
            self._close_and_return(boot, iid, qid, request,
                                   'revisit' if qid in self.revisits else 'not_a_person')
            return True
        if kind == 'posture_check_completed':
            qid = event['question_id']
            request = self.requests.get(qid)
            if (request is None or request.incident_id != iid
                    or self.stages.get(qid) not in ('view', 'posture')):
                return True
            self.check_since.pop(qid, None)
            if event['reason'] in OFF_FLOOR_REASONS:
                # Seen off the floor: no question; the runtime closes the case (after a
                # revisit it alerts instead and keeps it open). Go back.
                self._close_and_return(boot, iid, qid, request,
                                       'revisit' if qid in self.revisits else 'off_floor')
            else:
                self.stages[qid] = 'closer'  # Ask from 1 m, where the answer is heard.
            return True
        if kind in {'incident_resolved', 'incident_merged'}:
            self.terminal_revisions[iid] = revision
            for rid, request in tuple(self.requests.items()):
                if request.incident_id == iid:
                    del self.requests[rid]
                    self.stages.pop(rid, None)
                    self.check_since.pop(rid, None)
                    self._remember(rid)
                    self._forget(rid)
            return True
        if kind == 'confirmation_completed':
            qid = event.get('question_id')
            if _identifier(qid):
                self._remember(qid)
                request = self.requests.get(qid)
                if request is not None and request.incident_id == iid:
                    del self.requests[qid]
                    self._forget(qid)
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
            boot, iid, qid, subject, revision, summary, target, revisit)
        self.stages[qid] = 'approach' if target is not None else 'confirm'
        return True

    def _close_and_return(self, boot, iid, qid, request, reason):
        del self.requests[qid]
        self.stages.pop(qid, None)
        self._remember(qid)
        self._forget(qid)
        self.returns.append(ReturnJob(boot, iid, qid, request.revision, reason,
                                      request.revisit_target or request.approach_target))

    # ------------------------------------------------------------ waiting

    def _blocked_s(self, qid, now):
        since = self.blocked_from.get(qid)
        return self.blocked_total.get(qid, 0.0) + (
            max(0.0, now - since) if since is not None else 0.0)

    def wait(self, now, why):
        """The Manager cannot take the next step now; the link asks again later."""
        if not self.requests:
            return
        if why not in WAITING_FOR:
            why = 'busy'
        self.waiting_for = why
        for qid in self.requests:
            self.blocked_from.setdefault(qid, now)

    def resume(self, now):
        """The Manager took the step: the wait is over for every queued request."""
        for qid, since in self.blocked_from.items():
            self.blocked_total[qid] = self.blocked_total.get(qid, 0.0) + max(0.0, now - since)
        self.blocked_from.clear()
        self.waiting_for = None

    def check_waits(self, now):
        """Note a lasting wait; a minute alerts once and goes back; ten stop trying."""
        if self.waiting_for is None:
            return
        head = next(iter(self.requests.values()), None)
        since = self.blocked_from.get(head.question_id) if head is not None else None
        if since is not None and now - since >= WAIT_NOTE_S:
            recorded = self.waiting_sent.setdefault(head.question_id, set())
            if self.waiting_for not in recorded:
                recorded.add(self.waiting_for)
                decision = head.decision('check_waiting')
                decision['reason'] = self.waiting_for
                self.commands.append(decision)
        for qid, request in tuple(self.requests.items()):
            waited = self._blocked_s(qid, now)
            if waited >= WAIT_LIMIT_S:
                self.fail(request, 'time_limit')
                continue
            mark = self.alerted.get(qid)
            if waited - (mark or 0.0) < WAIT_ALERT_S:
                continue
            if mark is None:
                decision = request.decision('check_not_confirmed')
                decision['reason'] = 'waited_' + self.waiting_for
                self.commands.append(decision)
            self.alerted[qid] = waited
            if request.revisit_target is not None and self.stages.get(qid) != 'revisit':
                # The person may have moved meanwhile: look at the spot again first.
                self.stages[qid] = 'revisit'
                self.check_since.pop(qid, None)
                self.revisits[qid] = self.waiting_for

    def revisit_started(self, request):
        """The Manager runs the drive back to the spot."""
        if self.requests.get(request.request_id) != request or self.closed:
            return False
        decision = request.decision('revisit_started')
        decision['reason'] = self.revisits.get(request.question_id, 'busy')
        self.commands.append(decision)
        # Another minute of waiting from here sends it back once more (no new alert).
        self.alerted[request.question_id] = self.blocked_total.get(request.question_id, 0.0)
        return True

    def status(self, *, current=None, returning=False):
        """What the robot does for falls now, for the map screen (목업 27번).

        ``current`` is the kind of work the link has in flight. Active from the
        first drive to the end of the return, gaps included.
        """
        request = next(iter(self.requests.values()), None)
        job = self.returns[0] if self.returns else None
        if request is None and job is None and not returning:
            return dict(active=False, phase=None, waiting_for=None, alerted=False, target=None)
        if returning or job is not None:
            phase, target = 'return', job.target if job is not None else None
        else:
            stage = current or self.stages.get(request.question_id, 'confirm')
            # Moving back to see the whole body is part of looking at the spot.
            phase = ('wait' if self.waiting_for is not None
                     else 'look' if stage in ('check', 'view', 'posture')
                     else 'ask' if stage == 'confirm' else 'approach')
            target = request.revisit_target or request.approach_target
        return dict(active=True, phase=phase, waiting_for=self.waiting_for,
                    alerted=request is not None and request.question_id in self.alerted,
                    target=dict(x=target[0], y=target[1]) if target is not None else None)

    def next_work(self, now):
        """('return', job) | ('approach' | 'view' | 'revisit' | 'closer' | 'confirm', request)."""
        if self.returns:
            return 'return', self.returns[0]
        request = next(iter(self.requests.values()), None)
        if request is None:
            return None
        qid = request.question_id
        stage = self.stages.get(qid, 'confirm')
        if stage in ('check', 'posture'):
            since = self.check_since.setdefault(qid, now)
            if now - since < (CHECK_WAIT_S if stage == 'check' else POSTURE_WAIT_S):
                return None
            # The runtime did not answer in time: ask rather than wait, from 1 m
            # again when the view moved away.
            self.stages[qid] = stage = 'closer' if stage == 'posture' else 'confirm'
            self.check_since.pop(qid, None)
        return stage, request

    def approach_done(self, request, outcome, now, *, action='approach_result'):
        """Report the drive; arrived waits for the runtime's look, else ask here."""
        if self.requests.get(request.request_id) != request or self.closed:
            return False
        if outcome not in APPROACH_OUTCOMES:
            outcome = 'failed'
        decision = request.decision(action)
        decision['outcome'] = outcome
        self.commands.append(decision)
        if outcome == 'arrived':
            self.stages[request.question_id] = 'check'
            self.check_since[request.question_id] = now
        else:
            self.stages[request.question_id] = 'confirm'
        return True

    def revisit_done(self, request, outcome, now):
        """The drive back to the spot ended: look again (never closing), or ask here."""
        return self.approach_done(request, outcome, now, action='revisit_result')

    def view_done(self, request, outcome, now):
        """Report the move; the runtime then reads the posture (or reports none)."""
        if self.requests.get(request.request_id) != request or self.closed:
            return False
        if outcome not in APPROACH_OUTCOMES:
            outcome = 'failed'
        decision = request.decision('view_result')
        decision['outcome'] = outcome
        self.commands.append(decision)
        self.stages[request.question_id] = 'posture'
        self.check_since[request.question_id] = now
        return True

    def closer_done(self, request, outcome):
        """Back at 1 m (or as near as it got): ask now. The runtime has nothing to record."""
        if self.requests.get(request.request_id) != request or self.closed:
            return False
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
        self._forget(request.request_id)
        return True

    def fail(self, request, reason='question_error'):
        """Not asked: the runtime alerts the guardian at once (urgent)."""
        if self.requests.get(request.request_id) != request or self.closed:
            return False
        decision = request.decision('confirmation_failed')
        decision['reason'] = reason if reason in FAILURE_REASONS else 'question_error'
        self.commands.append(decision)
        del self.requests[request.request_id]
        self.stages.pop(request.request_id, None)
        self._remember(request.request_id, decision)
        self._forget(request.request_id)
        return True

    def stop(self, request):
        """Someone said "그만": stop here, ask nothing, the runtime tells the guardian."""
        if self.requests.get(request.request_id) != request or self.closed:
            return False
        decision = request.decision('check_stopped')
        decision['reason'] = 'user_stop'
        self.commands.append(decision)
        del self.requests[request.request_id]
        self.stages.pop(request.request_id, None)
        self._remember(request.request_id, decision)
        self._forget(request.request_id)
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
        self._clear_waits()
