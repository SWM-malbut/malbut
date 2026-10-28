"""One optional greeting after microphone readiness; never a periodic idle turn."""
import time
from uuid import uuid4


class StartupGreeting:
    def __init__(self, node, speak, idle, *, ready=lambda: True, clock=time.monotonic):
        from malbut_interfaces.srv import ControlSpeechSession
        self.request_type = ControlSpeechSession
        self.client = node.create_client(ControlSpeechSession, '/malbut/speech/session_control')
        self.speak, self.idle, self.clock = speak, idle, clock
        self.ready = ready
        self.deadline = clock() + 120
        self.future = None
        self.sent_at = None
        self.done = False
        self.timer = node.create_timer(1.0, self.tick)

    def cancel(self):
        self.done = True
        self.timer.cancel()
        if self.future is not None and not self.future.done():
            self.future.cancel()

    def tick(self):
        if self.done:
            return
        if self.clock() >= self.deadline or not self.idle():
            self.cancel()
            return
        if not self.ready():
            return
        if self.future is not None:
            if not self.future.done():
                if self.clock() - self.sent_at >= 3:
                    self.future.cancel()
                    self.future = None
                return
            future, self.future = self.future, None
            try:
                ready = future.result().barge_in_available
            except Exception:
                ready = False
            if ready:
                # The STT owner rechecks idleness upon receiving this request.
                # No fabricated user turn is added to the conversation history.
                self.speak('안녕하세요, 제이크예요. 오늘은 어떤 이야기부터 나눠 볼까요?',
                           request_id='conversation-opener:' + str(uuid4()))
                self.cancel()
                return
        if self.client.service_is_ready():
            request = self.request_type.Request()
            request.session_id = 'startup-greeting-readiness'
            request.check_only = True
            self.future = self.client.call_async(request)
            self.sent_at = self.clock()
