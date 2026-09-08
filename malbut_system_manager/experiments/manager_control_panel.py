#!/usr/bin/env python3
"""Small manual client for the isolated manager experiment."""

import math
import time
import tkinter as tk
from tkinter import ttk

from action_msgs.srv import CancelGoal
from malbut_interfaces.action import ExecuteMission
from malbut_interfaces.msg import SystemState
import rclpy
from rclpy.action import ActionClient
from rclpy.node import Node
from rclpy.qos import DurabilityPolicy, QoSProfile, ReliabilityPolicy
import yaml


class ControlPanel:
    """Send public manager requests and display the actual reported state."""

    def __init__(self):
        self.node = Node('manager_control_panel')
        self.client = ActionClient(self.node, ExecuteMission, '/malbut/mission/execute')
        self.cancel_client = self.node.create_client(
            CancelGoal, '/malbut/mission/execute/_action/cancel_goal')
        self.root = tk.Tk()
        self.root.title('Malbut 매니저 실험')
        self.root.geometry('600x470+100+80')
        self.root.protocol('WM_DELETE_WINDOW', self.close)
        self.state = None
        self.handles = {}
        self.pending_sends = 0
        self.stopping = False
        self.cancel_pending = False
        self.closing = False
        self.status_text = tk.StringVar(value='매니저 연결 대기 중')
        self.missions_text = tk.StringVar(value='')
        self.thoroughness = tk.StringVar(value='NORMAL')
        self.distance = tk.StringVar(value='1.0')
        self.x = tk.StringVar(value='-3.665503')
        self.y = tk.StringVar(value='-0.4874')
        self.buttons = []
        frame = ttk.Frame(self.root, padding=16)
        frame.pack(fill='both', expand=True)
        ttk.Label(frame, textvariable=self.status_text,
                  font=('', 15, 'bold')).pack(anchor='w')
        ttk.Label(frame, textvariable=self.missions_text, wraplength=560).pack(
            anchor='w', pady=(4, 12))

        row = ttk.Frame(frame)
        row.pack(fill='x', pady=4)
        self.button(row, '순찰 시작', self.patrol)
        ttk.Combobox(row, textvariable=self.thoroughness, state='readonly',
                     values=('LIGHT', 'NORMAL', 'THOROUGH'), width=12).pack(side='left')
        row = ttk.Frame(frame)
        row.pack(fill='x', pady=4)
        self.button(row, '사람 추적', self.follow)
        ttk.Label(row, text='희망 거리(m)').pack(side='left')
        ttk.Entry(row, textvariable=self.distance, width=7).pack(side='left', padx=5)
        row = ttk.Frame(frame)
        row.pack(fill='x', pady=4)
        self.button(row, '좌표로 이동', self.navigate)
        for label, variable in (('X', self.x), ('Y', self.y)):
            ttk.Label(row, text=label).pack(side='left', padx=(6, 2))
            ttk.Entry(row, textvariable=variable, width=10).pack(side='left')
        ttk.Label(frame, text='지도 좌표(m). 기본 좌표는 로봇의 시작 위치입니다.').pack(
            anchor='w', pady=(0, 10))
        ttk.Button(frame, text='전체 중지', command=self.stop).pack(fill='x', pady=4)
        ttk.Label(frame, text='새 명령은 매니저가 선점 처리합니다. 자동 시퀀스는 없습니다.').pack(
            anchor='w', pady=6)
        self.log = tk.Text(frame, height=8, state='disabled', wrap='word')
        self.log.pack(fill='both', expand=True)
        qos = QoSProfile(depth=1, reliability=ReliabilityPolicy.RELIABLE,
                         durability=DurabilityPolicy.TRANSIENT_LOCAL)
        self.node.create_subscription(SystemState, '/malbut/state', self.on_state, qos)
        self.note('버튼을 눌러 직접 시작하세요. 창을 닫으면 실험 전체가 종료됩니다.')
        self.tick()

    def button(self, parent, text, command):
        """Add a mission button disabled until the manager becomes available."""
        button = ttk.Button(parent, text=text, command=command, width=15)
        button.pack(side='left', padx=(0, 12))
        self.buttons.append(button)

    def note(self, text):
        """Show request and actual response messages without replacing state."""
        self.log.configure(state='normal')
        self.log.insert('end', time.strftime('%H:%M:%S ') + text + '\n')
        self.log.see('end')
        self.log.configure(state='disabled')
        print(text, flush=True)

    def on_state(self, message):
        """Keep the manager's authoritative state, including suspended missions."""
        self.state = message
        names = ('BOOTING', 'IDLE', 'EXECUTING_MISSION', 'RECHARGING', 'EMERGENCY')
        name = names[message.system_state] if message.system_state < len(names) else 'UNKNOWN'
        self.status_text.set(name)
        groups = (
            ('실행', list(message.active_foreground_missions)
             + list(message.active_background_missions)),
            ('보류', message.suspended_missions), ('대기', message.pending_missions),
        )
        self.missions_text.set(' | '.join(
            label + ': ' + (', '.join(m.capability_id for m in missions) or '없음')
            for label, missions in groups))

    def patrol(self):
        """Request a single patrol at the selected thoroughness."""
        value = ('LIGHT', 'NORMAL', 'THOROUGH').index(self.thoroughness.get())
        self.submit('patrol', {'thoroughness': value})

    def follow(self):
        """Request continuous visible-person following."""
        try:
            value = float(self.distance.get())
            if not math.isfinite(value) or value <= 0:
                raise ValueError()
        except ValueError:
            self.note('희망 거리는 0보다 큰 숫자로 입력하세요.')
            return
        self.submit('follow_person', {
            'target_mode': 0, 'target_person_id': '', 'desired_distance_m': value})

    def navigate(self):
        """Request navigation to user-selected map coordinates."""
        try:
            x, y = float(self.x.get()), float(self.y.get())
            if not all(math.isfinite(v) for v in (x, y)):
                raise ValueError()
        except ValueError:
            self.note('X, Y는 유효한 숫자로 입력하세요.')
            return
        self.submit('navigate_to_pose', {'pose': {
            'header': {'frame_id': 'map'}, 'pose': {
                'position': {'x': x, 'y': y}, 'orientation': {'w': 1.0}}}})

    def submit(self, capability, arguments):
        """Send only the common manager Action; scheduling stays in the manager."""
        if self.stopping or self.pending_sends or not self.client.server_is_ready():
            return
        self.pending_sends += 1
        goal = ExecuteMission.Goal(capability_id=capability,
                                   arguments_yaml=yaml.safe_dump(arguments))
        self.note(capability + ' 요청')
        future = self.client.send_goal_async(goal)
        future.add_done_callback(lambda done: self.accepted(capability, done))

    def accepted(self, capability, future):
        """Retain handles so a late acceptance cannot escape a stop request."""
        self.pending_sends -= 1
        try:
            handle = future.result()
            if not handle.accepted:
                self.note(capability + ' 요청 거부')
                return
            identifier = bytes(handle.goal_id.uuid).hex()
            self.handles[identifier] = handle
            result = handle.get_result_async()
            result.add_done_callback(lambda done: self.finished(capability, identifier, done))
            self.note(capability + ' 수락')
            if self.stopping:
                handle.cancel_goal_async()
        except Exception as error:
            self.note(str(error))

    def finished(self, capability, identifier, future):
        """Display actual Action termination rather than button-local state."""
        self.handles.pop(identifier, None)
        result = future.result()
        label = {4: '완료', 5: '취소', 6: '실패'}.get(result.status, str(result.status))
        if (result.status == 6
                and result.result.message == 'mission preempted by a replacement request'):
            label = '다른 요청으로 대체되어 종료'
        self.note(f'{capability}: {label} {result.result.message}')

    def stop(self):
        """Cancel all missions on this isolated manager, including suspended ones."""
        if self.cancel_pending:
            return
        self.stopping = True
        if not self.cancel_client.service_is_ready():
            self.note('매니저 취소 서비스 연결 대기 중입니다. 다시 눌러주세요.')
            return
        self.cancel_pending = True
        self.note('전체 취소 요청: 실제 IDLE 응답을 기다립니다.')
        # Zero UUID and timestamp are the standard ROS Action cancel-all request.
        future = self.cancel_client.call_async(CancelGoal.Request())
        future.add_done_callback(self.canceled)

    def canceled(self, future):
        """Acknowledge the cancellation request, not motion completion."""
        self.cancel_pending = False
        response = future.result()
        self.note(f'취소 접수: {len(response.goals_canceling)}개, 응답 코드 {response.return_code}')

    def tick(self):
        """Process ROS asynchronously with respect to the GUI event loop."""
        if not rclpy.ok():
            self.root.destroy()
            return
        rclpy.spin_once(self.node, timeout_sec=0.0)
        if (self.stopping and not self.pending_sends and not self.cancel_pending
                and not self.handles and self.state is not None
                and self.state.system_state == SystemState.IDLE
                and not any((self.state.active_foreground_missions,
                             self.state.active_background_missions,
                             self.state.suspended_missions, self.state.pending_missions))):
            self.stopping = False
            self.note('전체 미션 종료: IDLE')
            if self.closing:
                self.root.destroy()
                return
        enabled = self.client.server_is_ready() and not self.stopping and not self.pending_sends
        for button in self.buttons:
            button.configure(state='normal' if enabled else 'disabled')
        self.root.after(20, self.tick)

    def close(self):
        """Request stop before closing; the launcher owns final process cleanup."""
        self.closing = True
        self.stop()
        self.root.after(3000, self.root.destroy)


def main():
    """Open a manual control window without issuing any automatic missions."""
    rclpy.init()
    panel = ControlPanel()
    try:
        panel.root.mainloop()
    finally:
        panel.node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()


if __name__ == '__main__':
    main()
