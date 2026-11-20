#!/usr/bin/env python3
"""Validate an agent-normalized case and write the 30-second edit manifest."""

import argparse
import json
from pathlib import Path

FEATURES = Path(__file__).resolve().parents[1] / "references" / "기능_코드.json"


def read_object(path):
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"JSON 객체가 필요합니다: {path.name}")
    return value


def object_field(data, name):
    value = data.get(name, {})
    if not isinstance(value, dict):
        raise ValueError(f"{name}은 JSON 객체여야 합니다.")
    return value


def array_field(data, name):
    value = data.get(name, [])
    if not isinstance(value, list):
        raise ValueError(f"{name}은 배열이어야 합니다.")
    return value


def field(data, name):
    value = data.get(name)
    return value.strip() if isinstance(value, str) else ""


def local_asset(base, raw, extensions):
    if not isinstance(raw, str) or not raw.strip():
        return None
    file = (base / raw).resolve()
    if file.is_file() and file.suffix.lower() in extensions:
        return str(file)
    return None


def compile_case(data, base, catalogue):
    case_id = field(data, "case_id")
    scenario = field(data, "scenario")
    mode = field(data, "mode")
    if not case_id or scenario not in {"hospital", "academy", "factory", "other"}:
        raise ValueError("case_id와 유효한 scenario가 필요합니다.")
    if mode not in {"real", "example", "pre_install"}:
        raise ValueError("mode는 real, example, pre_install 중 하나여야 합니다.")

    evidence = object_field(data, "evidence")
    destination = object_field(data, "destination")
    equipment = object_field(data, "equipment")
    photo = object_field(data, "photo")
    result = object_field(data, "result")
    copy = object_field(data, "copy")
    media = object_field(data, "media")
    character = object_field(data, "character")
    requests = []
    media_requests = []

    def sourced(item, kind):
        ids = array_field(item, "evidence")
        for reference_id in ids:
            if not isinstance(reference_id, str):
                raise ValueError("근거 ID는 문자열이어야 합니다.")
            if reference_id not in evidence:
                raise ValueError(f"등록되지 않은 근거 ID: {reference_id}")
        return any(
            evidence.get(reference_id, {}).get("type") == kind
            and evidence[reference_id].get("case_id") == case_id
            and field(evidence[reference_id], "source")
            for reference_id in ids
        )

    if any(not isinstance(entry, dict) for entry in evidence.values()):
        raise ValueError("각 근거는 JSON 객체여야 합니다.")

    is_real = mode == "real"
    city = field(destination, "name")
    site_name = field(data, "site_name")
    if not site_name:
        requests.append("공개 가능한 현장명이나 익명 표기를 알려 주세요.")
    if not city or (is_real and not sourced(destination, "location")):
        requests.append("설치 도시와 해당 사례의 위치 확인 기록을 추가해 주세요.")
    if not field(equipment, "name") or not field(equipment, "placement"):
        requests.append("주 장비 이름과 설치 위치 또는 단말 설정 방식을 알려 주세요.")
    elif is_real and not sourced(equipment, "installation"):
        requests.append("해당 현장에 설치한 장비와 위치를 확인할 기록을 추가해 주세요.")
    if character.get("kind") not in {"representative", "stand_in"}:
        requests.append("대표자 캐릭터 또는 시연용 대역을 지정해 주세요.")
    elif character.get("kind") == "representative" and character.get("approved") is not True:
        requests.append("대표자 캐릭터의 사용 범위를 확인해 주세요.")
    elif character.get("kind") == "stand_in" and is_real:
        requests.append("실제 사례에는 사용이 확인된 대표자 캐릭터를 연결해 주세요.")

    selected = []
    for feature in array_field(data, "features"):
        if not isinstance(feature, dict):
            raise ValueError("각 기능은 JSON 객체여야 합니다.")
        code = field(feature, "id")
        if code not in catalogue:
            requests.append(f"공식 자료로 확인하고 기능 코드를 등록해 주세요: {code or '코드 누락'}")
            continue
        if is_real and not sourced(feature, "feature_used"):
            requests.append(f"현장 적용 근거를 확인해 주세요: {catalogue[code]['label']}")
        article = {"id": code, **catalogue[code], "evidence": feature.get("evidence", [])}
        if article not in selected:
            selected.append(article)
    if not selected:
        requests.append("고객 질문에 답하는 티링크 기능을 하나 이상 골라 주세요.")

    image_extensions = {".jpg", ".jpeg", ".png", ".webp"}
    video_extensions = {".mp4", ".mov", ".m4v", ".webm"}
    photo_path = local_asset(base, photo.get("path"), image_extensions)
    relation = field(photo, "relation")
    if not photo_path or photo.get("approved") is not True or relation not in {"same_case", "reference_case"}:
        requests.append("엔딩에 쓸 실제 설치 사진과 사용 범위를 확인해 주세요.")
    elif is_real and (relation != "same_case" or not sourced(photo, "site_photo")):
        requests.append("실제 사례는 같은 현장 사진과 연결 근거가 필요합니다.")
    if mode == "pre_install":
        requests.append("설치 완료와 해당 현장 사진을 확인한 후 실제 사례로 제작해 주세요.")

    result_kind = field(result, "kind")
    if result_kind not in {"observed", "use_example"}:
        requests.append("마지막 설명이 확인된 운영 변화인지 기능 사용 예시인지 지정해 주세요.")
    elif result_kind == "observed" and (not is_real or not sourced(result, "observed_outcome")):
        requests.append("성과 근거를 추가하거나 결말을 기능 사용 예시로 바꿔 주세요.")
    if scenario == "hospital" and field(data, "reservation_state") not in {"request", "review", "confirmed"}:
        requests.append("예약 변경 상태를 요청·확인 중·확정 중 하나로 알려 주세요.")

    required_copy = ("question", "answer", "ending", "cta")
    if any(not field(copy, name) for name in required_copy):
        requests.append("문답, 마지막 기능 설명, 상담 안내 문구를 채워 주세요.")

    qa = local_asset(base, media.get("qa"), image_extensions | video_extensions)
    travel = local_asset(base, media.get("travel"), video_extensions)
    voiceover = local_asset(base, media.get("voiceover"), {".wav", ".m4a", ".mp3", ".aac", ".aiff"})
    if media.get("qa") and not qa:
        media_requests.append("지정한 Q&A 소재 경로를 확인해 주세요.")
    if not travel:
        media_requests.append("팀의 10초 지도 이동 애니메이션 파일을 연결해 주세요.")
    elif media.get("travel_approved") is not True:
        media_requests.append("지도 클립의 도시, 캐릭터, 장비가 콘티와 일치하는지 확인해 주세요.")
    if media.get("voiceover") and not voiceover:
        media_requests.append("지정한 내레이션 음성 경로를 확인해 주세요.")

    photo_label = f"실제 설치 사진 · {site_name}" if is_real and relation == "same_case" else "별도 현장 실사 참고"
    dialogue_label = "상담 요지 재구성\n" if is_real else "가상 활용 사례\n"
    outcome_label = "확인된 현장 변화" if result_kind == "observed" else "기능 사용 예시"
    character_label = "대표자 캐릭터" if character.get("kind") == "representative" else "시연용 캐릭터"
    montage = [
        ("qa", 0, 10, qa),
        ("travel", 10, 20, travel),
        ("proof", 20, 30, photo_path),
    ]
    labels = [
        (0, 5, f"{dialogue_label}Q. {field(copy, 'question')}"),
        (5, 10, f"{dialogue_label}A. {field(copy, 'answer')}"),
        (10, 20, f"서울 → {city or '[도시 확인 필요]'} · 이동 연출\n{character_label} · {field(equipment, 'name')}"),
        (20, 23, photo_label),
        (23, 27, f"{photo_label} · {outcome_label}\n{field(copy, 'ending')}"),
        (27, 30, f"{photo_label}\n{field(copy, 'cta')}"),
    ]
    status = "needs_case_details" if requests else "needs_generated_media" if media_requests else "ready_for_edit"
    return {
        "case_id": case_id,
        "status": status,
        "demo_only": not is_real,
        "duration_s": 30,
        "site_name": site_name or None,
        "destination": city or None,
        "equipment": equipment,
        "features": selected,
        "photo_relation": relation or "missing",
        "result_kind": result_kind or None,
        "reservations": field(data, "reservation_state") or None,
        "segments": [
            {"id": role, "role": role, "start_s": start, "end_s": end, "asset": asset}
            for role, start, end, asset in montage
        ],
        "labels": [{"start_s": start, "end_s": end, "text": text} for start, end, text in labels],
        "voiceover": voiceover,
        "requests": requests + media_requests,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("case", type=Path, help="에이전트가 정리한 사례 JSON")
    parser.add_argument("--output", type=Path, required=True, help="새 편집용 manifest.json")
    args = parser.parse_args()
    try:
        if args.case.resolve() == args.output.resolve():
            raise ValueError("입력 사례 파일과 출력 경로를 구분해 주세요.")
        manifest = compile_case(read_object(args.case), args.case.resolve().parent, read_object(FEATURES))
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("x", encoding="utf-8") as file:
            json.dump(manifest, file, ensure_ascii=False, indent=2)
            file.write("\n")
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    print(manifest["status"])
    for request in manifest["requests"]:
        print(f"- {request}")


if __name__ == "__main__":
    main()
