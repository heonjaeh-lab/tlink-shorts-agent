#!/usr/bin/env python3
"""Build Higgsfield shot prompts and an edit handoff from one visual case file."""

import argparse
import json
import math
from pathlib import Path


STYLE = (
    "Match the supplied illustrated film frame for frame in visual character. Portrait 9:16, "
    "warm cream and ochre walls, soft sky-blue glass, restrained navy, fine dark ink contours, "
    "soft cel shadows and lightly textured matte backgrounds. Match the exact portrait-based "
    "representative: neatly parted black hair, slim wire glasses, navy blazer, light shirt. "
    "Keep his facial proportions, jacket and scale consistent. Silent picture."
)
IMAGE_TYPES = {".png", ".jpg", ".jpeg", ".webp"}
VIDEO_TYPES = {".mp4", ".mov", ".m4v", ".webm"}


def required_text(obj, key):
    value = obj.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"내용이 필요한 항목: {key}")
    return value.strip()


def option(obj, key, choices):
    value = required_text(obj, key)
    if value not in choices:
        raise ValueError(f"{key} 선택값: {', '.join(sorted(choices))}")
    return value


def local_file(value, base, kinds):
    if not value:
        return None
    if not isinstance(value, str):
        raise ValueError("소재 경로는 문자열이어야 합니다.")
    path = (base / value).resolve()
    if not path.is_file() or path.suffix.lower() not in kinds:
        raise ValueError(f"사용할 수 없는 로컬 소재: {value}")
    return str(path)


def case_source(case, items, kind):
    registry = case.get("evidence", {})
    for item in items:
        for evidence_id in (item or {}).get("evidence", []):
            record = registry.get(evidence_id) or {}
            if (record.get("case_id") == case["case_id"] and record.get("type") == kind
                    and str(record.get("source") or "").strip()):
                return
    raise ValueError(f"실제 사례 {case['case_id']}의 {kind} 근거를 연결하세요.")


def qa_layers(beats, base):
    if not isinstance(beats, list) or not beats:
        raise ValueError("시간순 Q&A 큐(qa_beats)가 필요합니다.")
    layers = []
    end = 0.0
    for beat in beats:
        start, until = beat["start_s"], beat["end_s"]
        if (isinstance(start, bool) or isinstance(until, bool)
                or not isinstance(start, (int, float)) or not isinstance(until, (int, float))
                or not math.isfinite(start) or not math.isfinite(until)
                or abs(start - end) > .001 or until <= start):
            raise ValueError("Q&A 큐의 시간이 0초부터 순서대로 이어져야 합니다.")
        screen = local_file(beat.get("ui_image"), base, IMAGE_TYPES)
        layers.append({
            "start_s": start, "end_s": until, "speaker": required_text(beat, "speaker"),
            "line": required_text(beat, "line"), "ui_image": screen,
            "editor_note": beat.get("editor_note", ""),
        })
        end = until
    return layers


def build_brief(case, base, override_source=None):
    case_id = required_text(case, "case_id")
    case_mode = option(case, "mode", {"example", "real", "pre_install"})
    source_id = required_text(case, "source_case_id")
    source = local_file(override_source or case.get("source_clip"), base, VIDEO_TYPES)
    if not source:
        raise ValueError("캐릭터와 화풍이 담긴 원본 클립이 필요합니다.")

    site, hw, route = case["site"], case["hardware"], case["map"]
    same_case = case_id == source_id
    scenery = option(site, "mode", {"source", "new"})
    sign = option(site, "sign_mode", {"preserve", "overlay", "none"})
    product = option(hw, "reference_mode", {"source_depiction", "product_image", "software_only"})
    map_mode = option(route, "mode", {"original_reference", "naver"})
    if not same_case and (scenery == "source" or product == "source_depiction" or map_mode == "original_reference"):
        raise ValueError("새 사례에는 새 장소 배경·현장 장비 근거·NAVER 경로를 지정하세요.")
    if sign == "preserve" and scenery != "source":
        raise ValueError("새 간판은 실제 문구를 편집에서 합성하거나 익명으로 둡니다.")
    sign_text = required_text(site, "sign_text") if sign != "none" else ""
    exterior = local_file(site.get("exterior_image"), base, IMAGE_TYPES)
    product_ref = local_file(hw.get("reference_image"), base, IMAGE_TYPES)
    if product == "product_image" and product_ref is None:
        raise ValueError("교체 장비를 식별할 현장 사진 또는 제품 참조 사진이 필요합니다.")
    if product == "source_depiction" and case_mode == "real":
        raise ValueError("실제 설치 장비는 해당 사례의 확인 기록과 제품·현장 참조 사진을 연결하세요.")
    if case_mode == "real":
        facts = case
        if case.get("fact_case"):
            facts_file = local_file(case["fact_case"], base, {".json"})
            facts = json.loads(Path(facts_file).read_text(encoding="utf-8"))
            if (facts.get("case_id"), facts.get("mode"), facts.get("site_name")) != (
                    case_id, case_mode, case.get("site_name")):
                raise ValueError("사실 기록의 사례 ID·모드·지점명이 영상과 다릅니다.")
        case_source(facts, [hw, facts.get("equipment")], "installation")
        case_source(facts, [route.get("destination"), facts.get("destination"), site], "location")

    interior = required_text(site, "interior")
    street = required_text(site, "street")
    entrance = required_text(site, "entrance")
    customer = required_text(site, "customer_role")
    qa_action = site.get("qa_action") or "The representative listens, nods, then answers with an open-palm gesture."
    beats = qa_layers(case["qa_beats"], base)
    shots = {}
    shots["qa"] = {
        "action": "generate_or_use_approved_qa",
        "references": ["source_portrait_and_style"],
        "prompt": (
            f"{STYLE} A quiet conversational two-shot inside this location: {interior}. "
            f"Show a fictional {customer} in three-quarter view at left, facing the identified "
            "representative at right. The customer asks one short question with a slightly worried "
            f"expression. {qa_action} A subtle blink and small changes in expression convey turn-taking. Keep both "
            "faces readable within the vertical safe area and leave the lower third clear for captions. "
            "No text, captions, speech bubbles, numerals, brand marks or simulated software UI. "
            "Do not place the travel hardware in this conversation."
        ),
    }
    setup_action = None
    if product == "software_only":
        setup_action = required_text(hw, "action")
        prop = "This is a software-only deployment. The representative carries no new appliance and has both hands free."
    else:
        identity = required_text(hw, "identity")
        appearance = required_text(hw, "appearance")
        grip = required_text(hw, "grip")
        prop = (
            f"The sole carried item is {identity}. Match the approved visual reference: {appearance}. "
            f"Support and scale it like this: {grip}. Draw the object with matching outlines and cel-shaded "
            "materials. Preserve its silhouette, proportions, gravity and exact grip through all cuts."
        )
    backdrop_ref = ["site_exterior"] if exterior else []
    hardware_ref = {"product_image": ["case_hardware_photo"], "source_depiction": ["source_hardware_frame"],
                    "software_only": []}[product]
    shot_refs = ["source_portrait_and_style"] + backdrop_ref + hardware_ref
    can_reuse_action = scenery == "source" and product == "source_depiction"
    shots["run"] = {
        "action": "reuse_source_segment" if can_reuse_action else "generate",
        "references": shot_refs,
        "prompt": (
            f"{STYLE} {prop} Match the original side-on dash, brief dust puffs and rightward "
            f"screen direction at its original speed. Local street art direction: {street}. "
            "Use the same camera height, motion and framing. Keep background signage blank."
        ),
    }
    keep_sign = {
        "preserve": "Retain the original doorway sign pixels exactly using the reference plate in the edit.",
        "overlay": "Give the sign a clean, steady blank plane for the case-supplied Korean lettering in the edit.",
        "none": "Keep the entrance signless and anonymous.",
    }[sign]
    entering = (
        "The free hand opens the door, he turns and takes the same unit inside, keeping the "
        "unit visible and attached to his arm until both pass fully inside."
        if product != "software_only" else
        "He opens the door, turns and passes fully inside."
    )
    shots["arrival"] = {
        "action": "reuse_source_segment" if can_reuse_action and sign == "preserve" else "generate",
        "references": shot_refs,
        "prompt": (
            f"{STYLE} {prop} Locked doorway shot with the timing and framing of the source. "
            f"Arrival location: {entrance}. {entering} {keep_sign} No added text or audio."
        ),
        "sign_composite": {"mode": sign, "exact_text": sign_text},
    }
    if map_mode == "naver":
        for key in ("origin", "destination"):
            point = route[key]
            required_text(point, "name")
            if not point.get("address") and any(point.get(axis) is None for axis in ("lon", "lat")):
                raise ValueError(f"map.{key}에 주소 또는 경위도 좌표가 필요합니다.")
        map_note = ("Use the separate case-specific NAVER route render. Preserve base map geometry, "
                    "labels and attribution. Animate only the route and the face-only badge.")
    else:
        map_note = "Keep the supplied map segment, including its moving badge and all lettering, as original pixels."
    shots["map"] = {"action": "run_naver_route" if map_mode == "naver" else "reuse_source_segment",
                    "cloud_generation": False, "editor_note": map_note}

    return {
        "case_id": case_id, "mode": case_mode,
        "assets": {"source_clip": source, "source_reference_frames": case.get("source_reference_frames", {}),
                   "site_exterior": exterior, "case_hardware_photo": product_ref},
        "map": route, "shots": shots, "qa_layers": beats,
        "onsite_software_action": setup_action,
        "reuse_whole_travel_clip": all(shots[key]["action"] == "reuse_source_segment"
                                      for key in ("run", "map", "arrival")),
        "visual_scope": ("original film depiction" if product == "source_depiction" else product),
        "compositing": "Add Korean dialogue and real, deidentified product-screen crops in the edit. "
                       "Use source-specific facts only for claims about the installation or results.",
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("case", type=Path)
    parser.add_argument("--source", type=Path, help="원본 영상 경로를 개별 실행에서 교체")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.output.suffix.lower() != ".json" or args.output.resolve() == args.case.resolve():
            raise ValueError("브리프 출력과 사례 입력은 별도 파일로 지정하세요.")
        source = str(args.source.resolve()) if args.source else None
        case = json.loads(args.case.read_text(encoding="utf-8"))
        output = build_brief(case, args.case.resolve().parent, source)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(output, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        parser.error(str(exc))
    print(args.output)


if __name__ == "__main__":
    main()
