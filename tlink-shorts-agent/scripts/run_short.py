#!/usr/bin/env python3
"""Prepare the per-shot Higgsfield brief or finish a supplied case as a vertical MP4."""

import argparse
import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile

from compile_case import FEATURES, compile_case, read_object
from compile_edit_brief import build_brief
from edit_shorts import probe, run_edit
import naver_route


def save(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def input_file(base, value):
    if not value:
        return None
    path = (base / value).resolve()
    if not path.is_file():
        raise ValueError(f"입력 파일을 찾을 수 없습니다: {path}")
    return path


def edit_layout(case, base, brief, travel):
    layout = copy.deepcopy(case["editing"])
    opening = layout["opening"]
    if len(opening) != len(brief["qa_layers"]):
        raise ValueError("문답 큐와 편집 화면의 개수를 맞춰 주세요.")
    for shot, beat in zip(opening, brief["qa_layers"]):
        shot["seconds"] = beat["end_s"] - beat["start_s"]
        if not shot.get("lines"):
            shot["lines"] = beat["line"].splitlines()
        if "".join("".join(shot["lines"]).split()) != "".join(beat["line"].split()):
            raise ValueError(f"화면 대사와 브리프가 다릅니다: {beat['line']}")
        if beat["ui_image"]:
            if not shot.get("feature", {}).get("crop"):
                raise ValueError("기능 화면에는 공개할 영역의 픽셀 좌표 crop이 필요합니다.")
            shot["feature"]["file"] = beat["ui_image"]
        elif shot.get("feature"):
            raise ValueError("기능 화면 원본을 해당 문답 큐의 ui_image에 연결하세요.")
    for shot in layout["closing"]:
        if feature := shot.get("feature"):
            feature["file"] = str(input_file(base, feature["file"]))
    photo = layout.get("site_photo")
    if photo:
        photo["file"] = str(input_file(base, photo["file"]))
    fallback = max(0, float(probe(travel)["duration"]) - 0.08)
    for shot in opening + layout["closing"]:
        shot.setdefault("source_still", fallback)
    layout["travel_file"] = str(travel)
    layout.pop("map_media", None)
    layout.pop("qa_media", None)
    return layout


def real_case_checks(case, base):
    if case["mode"] != "real":
        return
    path = input_file(base, case.get("fact_case")) if case.get("fact_case") else None
    if not path:
        raise ValueError("실제 사례는 같은 사례의 사실 기록 fact_case를 연결하세요.")
    facts = read_object(path)
    if (facts["case_id"], facts["mode"], facts["site_name"]) != (case["case_id"], "real", case["site_name"]):
        raise ValueError("사실 기록의 사례 ID·모드·지점명이 영상과 다릅니다.")
    verified = compile_case(facts, path.parent, read_object(FEATURES))
    if verified["status"] == "needs_case_details":
        raise ValueError("실제 사례 확인 필요: " + " / ".join(verified["requests"]))
    photo = case["editing"].get("site_photo") or {}
    fact_photo = input_file(path.parent, facts["photo"]["path"])
    if photo.get("relation") == "reference_case" or input_file(base, photo.get("file")) != fact_photo:
        raise ValueError("엔딩 사진을 같은 사례의 확인된 사진과 연결하세요.")


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("case", type=Path)
    p.add_argument("--out-dir", required=True, type=Path)
    p.add_argument("--prepare", action="store_true", help="장면 프롬프트와 소재 목록만 작성")
    for field in ("source", "qa", "run", "arrival", "map", "badge"):
        p.add_argument("--" + field, type=Path)
    p.add_argument("--sign-box", type=float, nargs=4)
    args = p.parse_args()
    base = args.case.resolve().parent
    case = read_object(args.case)
    brief = build_brief(case, base, str(args.source.resolve()) if args.source else None)
    real_case_checks(case, base)
    dest = args.out_dir.resolve()
    dest.mkdir(parents=True, exist_ok=True)
    brief_file = dest / "생성_브리프.json"
    if brief_file.resolve() == args.case.resolve():
        raise ValueError("사례 입력과 브리프 출력은 별도 파일로 지정하세요.")
    save(brief_file, brief)
    if args.prepare:
        print(brief_file)
        return
    assets = case.get("media_files", {})

    def asset(key):
        override = getattr(args, key, None)
        return input_file(Path.cwd(), override) if override else input_file(base, assets.get(key))

    qa = asset("qa")
    if qa is None:
        raise ValueError(f"문답 배경의 참조 장면이 필요합니다. {brief_file}의 shots.qa로 생성한 클립이나 승인된 이미지를 연결하세요.")
    command = [sys.executable, str(Path(__file__).with_name("assemble_travel.py")), str(brief_file)]
    for key in ("run", "arrival"):
        clip = asset(key)
        if clip:
            command += ["--" + key, str(clip)]
        elif brief["shots"][key]["action"] == "generate":
            raise ValueError(f"생성한 {key} 장면을 연결하세요. 장면별 프롬프트: {brief_file}")
    sign_box = args.sign_box or case["site"].get("sign_box")
    if sign_box:
        command += ["--sign-box", *map(str, sign_box)]
    with tempfile.TemporaryDirectory(dir=dest, prefix=".edit-") as tempdir:
        temp = Path(tempdir)
        map_clip = asset("map")
        if brief["map"]["mode"] == "naver" and map_clip is None:
            map_clip = temp / "route.mp4"
            naver_route.run(args.case, map_clip, asset("badge"), case["map"].get("seconds", 4))
        if map_clip:
            command += ["--map", str(map_clip)]
        travel = temp / "travel.mp4"
        subprocess.run([*command, "--output", str(travel)], check=True)
        layout = edit_layout(case, base, brief, travel)
        edits = temp / "edit.json"
        save(edits, layout)
        output = dest / "숏츠.mp4"
        inputs = {qa, Path(brief["assets"]["source_clip"])}
        inputs.update(asset(key) for key in ("run", "arrival", "map"))
        if output.resolve() in inputs:
            raise ValueError("완성 영상이 원본 소재를 덮어쓰지 않도록 별도 출력 폴더를 지정하세요.")
        result = run_edit(edits, output, qa_override=qa)
        result.pop("map_replaced", None)
        result["map_source"] = ("source_animation" if case["map"]["mode"] == "original_reference"
                                else "provided_route" if asset("map") else "live_naver")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, KeyError, TypeError, subprocess.CalledProcessError) as error:
        sys.exit(f"숏츠 제작을 멈춘 지점 · {error}")
