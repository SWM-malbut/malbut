# 사용자 Piper 음성 설치

대화용 ONNX 모델만 바꾸면 저장된 작업·오류·상황 확인 안내는 이전 목소리로
남는다. 같은 모델로 안내 카탈로그를 생성하고, 로봇의 `tts.yaml`에
`model_path`와 `audio_directory`를 함께 지정한다. 모델과 WAV는 로봇에 별도로
설치하며 Git merge/pull만으로 전달되지 않는다.

이 절차는 mono 22,050 Hz Piper 모델을 사용한다. 안내 파일은 기존 재생 계약인
mono 24,000 Hz PCM16으로 저장한다. 생성·검증에 API나 모델 다운로드를 사용하지
않는다. 개인 실험 모델·음성은 저장소에 포함하지 않고 원본 `ATTRIBUTION.txt`를
함께 보관한다.

## 모델과 안내 준비

아래는 선택된 MOSS 합성 음성 기반 Piper 발음 보강 2차, 누적 40회 모델의 예다.
다른 모델을 설치할 때는 파일명과 신뢰하는 원본 해시를 함께 바꾼다.
추가 학습이나 로봇 전체 빌드는 필요하지 않으며, 이 PR의 TTS 코드가 설치된
Python/ROS 환경을 사용한다. 소스에서 실행할 때는 저장소 루트에서 다음을 실행한다.

먼저 로봇에 다음 파일을 별도로 복사한다.

- `~/.cache/malbut_speech/models/piper-moss-round02-epoch40/ko_KR-moss_calm-medium.onnx`
- 같은 폴더의 `ko_KR-moss_calm-medium.onnx.json`과 `ATTRIBUTION.txt`

```bash
speech_cache="${XDG_CACHE_HOME:-$HOME/.cache}/malbut_speech"
speech_python="$speech_cache/runtime/bin/python"
voice_dir="$speech_cache/models/piper-moss-round02-epoch40"
voice_model="$voice_dir/ko_KR-moss_calm-medium.onnx"
voice_sha=bb621b60a4d030af30902584c388ec722cd3992ee6164ad9b5e119854217b487
config_sha=e6257ee2956315e515b90b358b176195bd416ff3dd9174d3b66250d95d86164b
printf '%s  %s\n' "$voice_sha" "$voice_model" "$config_sha" "$voice_model.json" | sha256sum -c -
```

모델과 JSON 해시가 모두 일치한 뒤 새 안내 폴더를 만든다. 기존 안내 폴더를
덮어쓰지 않으므로 생성 실패 시 현재 음성 설정은 유지된다.

```bash
notice_dir=$(mktemp -d "$speech_cache/piper-moss-epoch40-notices.XXXXXX")
cp malbut_tts/malbut_tts/audio/catalog.json "$notice_dir/catalog.json"
PYTHONPATH=malbut_tts "$speech_python" -m malbut_tts.prepare_notices \
  --backend piper --model-path "$voice_model" --piper-model-sha256 "$voice_sha" \
  --catalog "$notice_dir/catalog.json" --output-dir "$notice_dir"
PYTHONPATH=malbut_tts "$speech_python" -m malbut_tts.prepare_notices \
  --backend piper --model-path "$voice_model" --piper-model-sha256 "$voice_sha" \
  --catalog "$notice_dir/catalog.json" --output-dir "$notice_dir" --check
```

`--check`가 성공한 뒤에만 설정을 바꾼다. 이 명령은 모델을 로드하거나 재합성하지
않으며 WAV, 대본, 모델·JSON·정규화 코드 해시와 생성 환경을 검사한다.
모델 해시가 다르거나 설정·대본이 바뀌었으면 실패한다. 새로 생성한 매니페스트의
음성 식별자는 `local-piper/sha256/<모델 해시>`이며 KSS로 잘못 기록하지 않는다.

## 로봇 설정과 재생 확인

아래 명령은 기존의 출력 장치 등 다른 설정을 보존하고 이전 파일을 같은 폴더에
백업한다. PyYAML이 설치된 로봇 음성 환경을 사용한다.

```bash
"$speech_python" - "$voice_model" "$notice_dir" <<'PY'
from pathlib import Path
import shutil
import sys
import tempfile
import yaml

path = Path.home() / '.config/malbut/tts.yaml'
path.parent.mkdir(parents=True, exist_ok=True)
document = yaml.safe_load(path.read_text()) if path.exists() else {}
if path.exists():
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix='tts.before-custom-',
                                     suffix='.yaml', delete=False) as backup:
        shutil.copyfile(path, backup.name)
        print('원복용 설정:', backup.name)
parameters = document.setdefault('/**', {}).setdefault('ros__parameters', {})
parameters.update(backend='piper', model_path=sys.argv[1], audio_directory=sys.argv[2])
temporary = path.with_suffix('.yaml.tmp')
temporary.write_text(yaml.safe_dump(document, sort_keys=False))
temporary.replace(path)
PY
```

기존 speech/Bringup을 정상적으로 재시작한다. 별도 TTS 노드를 중복 실행하지
않는다. `ros2 param get /malbut_tts model_path`와
`ros2 param get /malbut_tts audio_directory`로 실제 적용 경로를 확인한다.
작업 완료·오류·낙상 확인 안내는 각각 `operation.succeeded`, `operation.failed`,
`situation.fall.confirmation`이다. 청취 시험은 TTS에 해당 `audio_id`를 전달하고
실제 미션이나 낙상 이벤트를 발생시키지 않는다. 재생 완료 상태와 스피커 출력,
사용자의 실제 청취 결과를 따로 확인한다.

현재 Piper 모드의 대화·안내 선택은 이 설정 파일에 저장되어 재시작 후에도
유지된다. OpenAI backend 전용 번들 `assets/notice_no_dialogue.wav`는 이 설정의
대상이 아니다. 로봇 설치 파일을 직접 바꾸면 이후 소스 복사·빌드에서 덮어써질
수 있으므로, Piper 안내는 별도의 `audio_directory`로 배포한다.

원복하려면 출력된 `tts.before-custom-*.yaml`을 `~/.config/malbut/tts.yaml`로
복사하고 speech/Bringup을 재시작한다. 이전 모델·안내 폴더는 선택을 확정하기
전까지 보관한다.
