# 티링크 설치 사례 숏츠 에이전트

현장 메모와 설치 소재를 바탕으로 어떤 제품 기능을 보여줄지 정하고, 기능 화면과 이동 영상을 30초 세로 숏츠로 편집합니다.

## 데모 재실행

Python 3.11 이상, Pillow, FFmpeg와 FFprobe, 한글 글꼴이 필요합니다. FFmpeg와 FFprobe를 PATH에 추가하세요. macOS 기본 한글 글꼴과 Linux의 Noto CJK를 지원합니다. 다른 글꼴은 `TLINK_FONT`와 `TLINK_FONT_BOLD`에 파일 경로를 지정하세요.

저장소 루트에서 실행합니다. 데모 재실행에는 외부 API 키가 필요하지 않습니다.

```sh
python3 -m pip install Pillow
python3 tlink-shorts-agent/scripts/run_short.py 영어학원_숏츠_실행/사례.json --out-dir 출력
```

출력 파일은 `출력/숏츠.mp4`입니다. 동봉한 학원 편은 가상 상담과 티링크의 공식 제품 화면을 사용합니다. [소재와 다시 편집하는 방법](영어학원_숏츠_실행/재실행.md)

## 새 사례 제작

사례 JSON을 복사하고 현장과 소재를 수정한 뒤 위 실행 명령에 `--prepare`를 추가하면 장면별 제작 브리프를 만들 수 있습니다. 새 생성 컷과 지도에 필요한 입력은 아래 스킬에 설명되어 있습니다.

- 고객 질문과 기능 근거를 장면 순서로 정리합니다. [스토리라인 스킬](installation-shorts-storyline/SKILL.md)
- 기능 화면, 이동, 설치 소재와 자막의 편집 순서를 정합니다. [영상 편집 스킬](installation-shorts-editing/SKILL.md)
- 사례 브리프를 만들고 준비된 소재로 MP4를 렌더링합니다. [실행 스킬](tlink-shorts-agent/SKILL.md)
