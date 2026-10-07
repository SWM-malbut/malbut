"""Typed, nonblocking access to the native robot operator and movement stop."""

import json
import time


class RobotDeviceClient:
    """Send each operation once; late acceptance never causes a second send."""

    def __init__(self, node, clock=time.monotonic):
        from malbut_interfaces.action import DeviceOperation
        from malbut_interfaces.srv import StopMovement
        from rclpy.action import ActionClient

        self.node, self.clock = node, clock
        self.operation_type, self.stop_type = DeviceOperation, StopMovement
        self.action = ActionClient(node, DeviceOperation, '/malbut/device/operate')
        self.stop_client = node.create_client(StopMovement, '/malbut/mission/stop_movement')
        self.pending = {}
        self.closed = False
        self.timer = node.create_timer(0.1, self._timeouts)

    def send(self, request_id, operation, arguments, callback):
        if self.closed or not self.action.server_is_ready():
            callback(dict(success=False, code='unavailable', result={}, message='기기 운영 연결을 확인하지 못했어요.'))
            return
        if request_id in self.pending:
            raise ValueError('operation was already sent')
        goal = self.operation_type.Goal()
        goal.request_id, goal.operation = request_id, operation
        goal.arguments_json = json.dumps(arguments, ensure_ascii=False, allow_nan=False)
        record = dict(callback=callback, deadline=self.clock() + 240, handle=None, done=False, canceled=False)
        self.pending[request_id] = record
        try:
            future = self.action.send_goal_async(goal)
            future.add_done_callback(lambda done: self._accepted(request_id, done))
        except Exception:
            self._finish(request_id, 'unknown', '요청 전달 여부를 확인하지 못했어요.')

    def _accepted(self, request_id, future):
        record = self.pending.get(request_id)
        if record is None:
            return
        try:
            handle = future.result()
            if not handle.accepted:
                self._finish(request_id, 'rejected', '기기 운영 요청이 거절됐어요.')
                return
            record['handle'] = handle
            if self.closed or record['done'] or record['canceled']:
                handle.cancel_goal_async()
            result = handle.get_result_async()
            result.add_done_callback(lambda done: self._result(request_id, done))
        except Exception:
            self._finish(request_id, 'unknown', '기기 운영 접수 상태를 확인하지 못했어요.')

    def _result(self, request_id, future):
        try:
            response = future.result()
            result = response.result
            if not isinstance(result.result_json, str) or len(result.result_json.encode()) > 256 * 1024:
                raise ValueError('oversized device result')
            value = json.loads(result.result_json or '{}', parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))
            event = dict(success=response.status == 4 and result.success,
                         code=result.code, result=value, message=result.message)
            self._deliver(request_id, event)
        except Exception:
            self._finish(request_id, 'unknown', '기기 운영 결과를 검증하지 못했어요.')

    def stop(self, request_id, callback, *, confirmed_ids=None):
        if self.closed or not self.stop_client.service_is_ready():
            callback(dict(success=False, code='unavailable', result={}, message='전체 이동 중지 연결을 확인하지 못했어요.'))
            return
        if request_id in self.pending:
            raise ValueError('stop was already sent')
        request = self.stop_type.Request()
        request.request_id = request_id
        request.require_preemption_confirmation = confirmed_ids is not None
        request.confirmed_preemption_mission_ids = list(confirmed_ids or ())
        self.pending[request_id] = dict(callback=callback, deadline=self.clock() + 30,
                                        handle=None, done=False, canceled=False)
        try:
            future = self.stop_client.call_async(request)
            future.add_done_callback(lambda done: self._stopped(request_id, done))
        except Exception:
            self._finish(request_id, 'unknown', '이동 중지 요청의 전달 여부를 확인하지 못했어요.')

    def _stopped(self, request_id, future):
        try:
            result = future.result()
            self._deliver(request_id, dict(success=result.stopped, code=result.code, message=result.message,
                result={'affected_mission_ids': list(result.affected_mission_ids),
                        'unresolved_mission_ids': list(result.unresolved_mission_ids),
                        'conflicting_mission_ids': list(result.unresolved_mission_ids)}))
        except Exception:
            self._finish(request_id, 'unknown', '이동 중지를 확인하지 못했어요.')

    def _deliver(self, request_id, result):
        record = self.pending.get(request_id)
        if record is not None and not record['done']:
            record['done'] = True
            record['code'] = result.get('code')
            record['callback'](result)

    def snapshot(self, request_id):
        record = self.pending.get(request_id, {})
        return {'done': record.get('done', False), 'code': record.get('code')}

    def _finish(self, request_id, code, message):
        self._deliver(request_id, dict(success=False, code=code, result={}, message=message))

    def cancel(self, request_id):
        record = self.pending.get(request_id)
        if record is not None:
            record['canceled'] = True
            if record['handle'] is not None:
                record['handle'].cancel_goal_async()

    def _timeouts(self):
        now = self.clock()
        for request_id, record in list(self.pending.items()):
            if not record['done'] and now >= record['deadline']:
                self._finish(request_id, 'timeout', '기기 운영 결과를 확인할 시간이 지났어요. 자동 재전송하지 않았어요.')
                self.cancel(request_id)

    def close(self):
        self.closed = True
        self.node.destroy_timer(self.timer)
        self.action.destroy()
        self.node.destroy_client(self.stop_client)
