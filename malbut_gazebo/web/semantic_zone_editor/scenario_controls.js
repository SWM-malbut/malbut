(() => {
  const buttons = Array.from(
    document.querySelectorAll("[data-scenario-command]"),
  );
  const status = document.querySelector("#scenarioCommandStatus");
  const patrol = document.querySelector("#startPatrol");
  const tracking = document.querySelector("#startPersonTracking");
  const stopTracking = document.querySelector("#stopPersonTracking");
  const togglePerson = document.querySelector("#toggleScenarioPerson");
  const stopScenario = document.querySelector("#stopScenario");
  let pending = false;
  let scenario = {
    mode: "unavailable",
    target_mode: null,
    detail: "시나리오 상태를 기다리는 중입니다.",
    actor_visible: false,
  };

  const modeLabels = {
    idle: "대기 중",
    transitioning: "행동 전환 중",
    patrolling: "자율 순찰 중",
    web_navigation: "선택 위치로 이동 중",
    room_patrol: "선택한 방 순찰 중",
    person_tracking: "사람 추적 중",
    manual: "수동 조작 중",
    unavailable: "시나리오 연결 대기 중",
  };

  function exitDestinationSelection() {
    const navigateMode = document.querySelector("#navigateMode");
    if (navigateMode?.textContent === "이동 모드 종료") {
      navigateMode.click();
    }
  }

  function render() {
    const mode = scenario.mode || "unavailable";
    const transitioning = mode === "transitioning";
    const target = scenario.target_mode;
    const unavailable = mode === "unavailable";
    const patrolActive = mode === "patrolling";
    const trackingActive = mode === "person_tracking";
    const actorVisible = Boolean(scenario.actor_visible);

    patrol.textContent = patrolActive ? "자율 순찰 중" : "자율 순찰 시작";
    patrol.setAttribute("aria-pressed", String(patrolActive));
    patrol.disabled = pending || unavailable || transitioning || patrolActive;

    tracking.textContent = trackingActive ? "사람 추적 중" : "사람 추적 시작";
    tracking.setAttribute("aria-pressed", String(trackingActive));
    tracking.disabled = pending || unavailable || transitioning || trackingActive;

    stopTracking.disabled = pending || !trackingActive;
    togglePerson.textContent = actorVisible ? "사람 모델 퇴장" : "사람 모델 등장";
    togglePerson.setAttribute("aria-pressed", String(actorVisible));
    togglePerson.disabled = pending || unavailable;
    stopScenario.disabled = pending || unavailable || mode === "idle"
      || (transitioning && target === "idle");

    const label = modeLabels[mode] || `알 수 없는 상태: ${mode}`;
    const targetLabel = target ? modeLabels[target] || target : null;
    status.textContent = transitioning && targetLabel
      ? `${label}: ${targetLabel}`
      : label;
  }

  async function run(command) {
    pending = true;
    render();
    exitDestinationSelection();
    status.textContent = "명령을 보내는 중입니다.";
    try {
      const configResponse = await fetch("/api/editor-config", {
        cache: "no-store",
        credentials: "same-origin",
      });
      const config = await configResponse.json();
      if (!configResponse.ok || !config.csrf_token) {
        throw new Error(config.error || "웹 제어 세션을 준비하지 못했습니다.");
      }
      const response = await fetch(`/api/scenario/${command}`, {
        method: "POST",
        credentials: "same-origin",
        headers: {
          "Content-Type": "application/json",
          "X-CSRF-Token": config.csrf_token,
        },
        body: "{}",
      });
      const result = await response.json();
      if (!response.ok) {
        throw new Error(result.message || result.error || "명령 실행에 실패했습니다.");
      }
      if (result.scenario?.mode) scenario = result.scenario;
      status.textContent = result.message || "상태 전환을 요청했습니다.";
    } catch (error) {
      status.textContent = error instanceof Error
        ? error.message
        : "명령 실행에 실패했습니다.";
    } finally {
      pending = false;
      render();
    }
  }

  buttons.forEach((button) => {
    button.addEventListener("click", () => {
      void run(button.dataset.scenarioCommand);
    });
  });

  const stream = new EventSource("/api/robot/stream");
  stream.addEventListener("robot", (event) => {
    const value = JSON.parse(event.data);
    const previousMode = scenario.mode;
    if (value.scenario?.mode) scenario = value.scenario;
    if (
      ["web_navigation", "room_patrol"].includes(scenario.mode)
      || (scenario.mode === "idle" && previousMode !== "idle")
    ) {
      exitDestinationSelection();
    }
    render();
  });
  stream.onerror = () => {
    status.textContent = "시나리오 상태 서버에 다시 연결하고 있습니다.";
  };

  render();
})();
