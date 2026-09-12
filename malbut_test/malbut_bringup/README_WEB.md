# 실로봇 LAN 테스트 페이지

같은 Wi-Fi의 Mac 브라우저에서 원본 영상, 사람 인식 영상, 시스템/추적 상태를
확인하고 AutoSLAM·사람 추적·순찰을 요청하는 작은 테스트 도구다.
페이지를 열거나 서버를 실행하는 것만으로는 로봇을 움직이지 않는다.

## 실행

로봇에서 해당 모드의 Bringup과 필요한 Action 서버를 준비한 뒤 새 터미널에서:

```zsh
source ~/ros2_ws/install/setup.zsh
source ~/ros2_ws/install/malbut_test/local_setup.zsh
ros2 run malbut_bringup robot_web_panel
```

로봇의 IP를 `hostname -I`로 확인하고 Mac에서 `http://<로봇-IP>:8766`을 연다.
터미널에 출력된 `Access token`을 페이지에 입력한다. 토큰은 실행할 때마다 바뀌며,
URL·브라우저 영구 저장소에 넣지 않는다. 8766 포트가 이미 사용 중이면
`--ros-args -p port:=8767`로 다른 포트를 지정한다.

## 동작과 경계

- 사람 추적·순찰: `/malbut/mission/execute`를 통해 요청한다.
- AutoSLAM: 관리자가 있으면 같은 경로로 요청하고, 관리자가 없으면 `/autoslam`에
  직접 요청한다. 직접 실행한 AutoSLAM이 끝나기 전에는 이 패널의 다른 시작을 거부한다.
- Action 서버가 없으면 기다리며 HTTP를 막지 않고 요청 실패를 표시한다.
- 원본은 `/depth_cam/rgb0/image_raw`, 사람 인식은
  `/perception/person/debug_image/compressed`를 본다. 인식 영상은 해당 노드에서
  `publish_debug_image`가 활성화되어 있어야 한다. 토픽 이름은 ROS parameter
  `rgb_topic`, `debug_topic`으로 바꿀 수 있다.
- 이미지 콜백은 최신 프레임만 보관한다. 브라우저가 보는 동안 최대 약 5Hz로
  JPEG를 요청하며, 같은 프레임의 인코딩 결과를 공유한다. 원본 영상 인코딩은
  OpenCV로 처리하며 rqt_image_view 또는 cv_bridge를 사용하지 않는다.
- 상태는 `/malbut/state`, `/tracking/person/status`와 실제 Action 결과를 표시한다.
- 지도 만들기와 저장 지도 주행 모드의 전환, 초기 위치 지정은 로봇 터미널/RViz에서
  한다. 이 페이지는 launch 실행, 임의 명령 실행, `/cmd_vel` 발행을 하지 않는다.

## 취소와 안전

시작 버튼은 확인 창을 거친다. 취소 버튼은 **이 웹 서버 프로세스가 보낸 요청만**
취소한다. 다른 CLI/웹 서버가 보낸 Goal을 취소하거나 강제 정지하지 않는다.
관리자는 같은 BASE 자원의 새 요청으로 기존 미션을 선점할 수 있다.

**브라우저를 닫거나 Wi-Fi 연결이 끊겨도 로봇은 멈추지 않는다.** 취소 버튼 또한
하드웨어 비상 정지가 아니다. `CANCELING`은 요청 중이며 최종 `CANCELED` 등 결과와
실제 정지를 확인해야 한다. 서버 종료 시 가능한 경우 자신이 보낸 Goal 취소를
요청하지만, 통신/서버 문제로 정지를 확인하지 못하면 터미널에 경고한다.

인터넷에 노출하거나 공유기 포트 포워딩을 하지 않는다. 인증 토큰을 요구하고
다른 웹사이트에서의 POST를 차단하지만, HTTP이므로 전송 암호화는 제공하지 않는다.
신뢰하는 로컬 네트워크에서만 사용한다. 로봇 내부에서만 볼 때는
`--ros-args -p host:=127.0.0.1`을 사용한다.
