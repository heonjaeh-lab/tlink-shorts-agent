#!/usr/bin/env python3
"""Assemble the approved run, map and doorway shots specified by a compiled brief."""

import argparse
from fractions import Fraction
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

from PIL import Image, ImageDraw
from edit_shorts import ENCODE, FIT, FPS, H, W, ff, font, probe


ROLES = ("run", "map", "arrival")
REUSE = "reuse_source_segment"


def digest(path):
    with path.open("rb") as f:
        return hashlib.file_digest(f, "sha256").digest()


def read_ranges(brief, source_info):
    spec = brief["assets"]["source_reference_frames"]["source_travel_segments"]
    if not isinstance(spec, dict) or type(spec.get("timebase_fps")) is not int or spec["timebase_fps"] <= 0:
        raise ValueError("브리프에 원본 컷의 timebase_fps와 시작·종료 프레임이 필요합니다.")
    rate = spec["timebase_fps"]
    if Fraction(source_info["r_frame_rate"]) != rate:
        raise ValueError("원본 프레임레이트가 브리프의 컷 기준과 다릅니다.")
    prior = 0
    for role in ROLES:
        span = spec.get(role)
        if (not isinstance(span, list) or len(span) != 2 or not all(type(n) is int for n in span)
                or not (span[0] == prior < span[1])):
            raise ValueError("원본 run, map, arrival의 프레임 구간을 순서대로 입력하세요.")
        prior = span[1]
    if prior != int(source_info["nb_frames"]):
        raise ValueError("원본 영상 길이가 브리프의 컷 기준과 다릅니다.")
    return spec, rate


def pixel_box(box):
    if (not box or len(box) != 4 or not all(math.isfinite(x) for x in box)
            or not (0 <= box[0] < box[2] <= 1 and 0 <= box[1] < box[3] <= 1)):
        raise ValueError("고정 간판 면의 --sign-box를 왼쪽 위와 오른쪽 아래 0~1 좌표로 지정하세요.")
    pixels = tuple(round(v * limit) for v, limit in zip(box, (W, H, W, H)))
    if pixels[2] - pixels[0] < 8 or pixels[3] - pixels[1] < 8:
        raise ValueError("한글을 넣을 수 있게 간판 영역을 넓혀주세요.")
    return pixels


def sign_layer(text, size):
    if not isinstance(text, str) or not text.strip() or "\n" in text:
        raise ValueError("고정 간판에 쓸 정확한 한 줄 문구가 필요합니다.")
    w, h = size
    image = Image.new("RGBA", size)
    draw = ImageDraw.Draw(image)
    for pt in range(h, 7, -1):
        face = font(pt, True)
        left, top, right, bottom = draw.textbbox((0, 0), text, font=face)
        if right-left <= 0.94*w and bottom-top <= 0.86*h:
            x = (w-right+left)/2 - left
            y = (h-bottom+top)/2 - top
            draw.text((round(x), round(y)), text, font=face, fill="#30271f")
            return image
    raise ValueError("정확한 간판 문구가 지정 영역에 들어가지 않습니다.")


def prepare_sign(brief, folder, source, first_frame, box, source_box):
    sign = brief["shots"]["arrival"]["sign_composite"]
    mode = sign["mode"]
    if mode not in {"none", "overlay", "preserve"}:
        raise ValueError("지원하지 않는 간판 방식입니다.")
    if mode == "none" or brief["shots"]["arrival"]["action"] == REUSE:
        if box or source_box:
            raise ValueError("간판을 유지하는 원본 컷이나 무간판 컷에는 좌표를 지정하지 않습니다.")
        return None
    target = pixel_box(box)
    size = (target[2] - target[0], target[3] - target[1])
    output = folder / "sign.png"
    if mode == "overlay":
        if source_box:
            raise ValueError("--source-sign-box는 원래 간판을 복원할 때 사용합니다.")
        sign_layer(sign["exact_text"], size).save(output)
    else:
        src_box = pixel_box(source_box or box)
        still = folder / "source-door.png"
        ff("-i", source, "-vf", f"select=eq(n\\,{first_frame}),{FIT}", "-frames:v", 1, still)
        with Image.open(still) as im:
            im.crop(src_box).resize(size, Image.Resampling.LANCZOS).save(output)
    return output, target[0], target[1]


def approved_inputs(brief, files, windows, source):
    # Caller supplies only the approved outputs for this brief; never infer a clip from its filename.
    hashes = {digest(source)}
    paths = {}
    for role in ROLES:
        action = brief["shots"][role]["action"]
        if action == REUSE:
            if files.get(role) is not None or windows.get(role) is not None:
                raise ValueError(f"{role}은 원본 재사용 컷입니다. 교체하려면 사례 브리프를 먼저 수정하세요.")
            paths[role] = source
            continue
        expected = "run_naver_route" if role == "map" else "generate"
        if action != expected:
            raise ValueError(f"{role}의 작업 종류를 확인하세요.")
        path = files.get(role)
        if not path or not path.is_file():
            raise ValueError(f"이 사례에 대해 승인한 --{role} 영상이 필요합니다.")
        checksum = digest(path)
        if checksum in hashes:
            raise ValueError(f"{role}에 원본 또는 다른 컷과 동일한 파일을 사용할 수 없습니다.")
        hashes.add(checksum)
        paths[role] = path
    return paths


def make_cut(role, brief, path, window, span, count, overlay, output):
    inputs = ["-i", path]
    if brief["shots"][role]["action"] == REUSE:
        start, end = span
        filters = f"trim=start_frame={start}:end_frame={end},setpts=PTS-STARTPTS,fps={FPS}"
    else:
        info = probe(path)
        duration = float(info["duration"])
        a, b = window or (0, duration)
        if not (0 <= a < b <= duration + 0.0001):
            raise ValueError(f"{role}의 --{role}-window 범위를 영상 길이 안에 지정하세요.")
        input_rate = float(Fraction(info["r_frame_rate"]))
        if input_rate <= 0 or b-a < 2/input_rate:
            raise ValueError(f"{role}의 재생 구간에 최소 두 프레임이 필요합니다.")
        # Pin the full selected action to the original shot length, including the final entry frame.
        speed_factor = ((count-1) / FPS) / (b-a-1/input_rate)
        filters = (f"trim=start={a:.9f}:end={b:.9f},settb=AVTB,"
                   f"setpts=(PTS-STARTPTS)*{speed_factor:.12f},fps={FPS}:eof_action=pass")
    filters += f",{FIT},tpad=stop_mode=clone:stop_duration=0.125,trim=end_frame={count},setpts=PTS-STARTPTS"
    if overlay:
        layer, x, y = overlay
        inputs += ["-i", layer]
        opts = ["-filter_complex", f"[0:v]{filters}[v];[v][1:v]overlay=x={x}:y={y}:format=auto,format=yuv420p"]
    else:
        opts = ["-vf", filters]
    ff(*inputs, *opts, "-frames:v", count, *ENCODE, output)
    if int(probe(output)["nb_frames"]) != count:
        raise ValueError(f"{role} 컷의 프레임 수가 일치하지 않습니다.")


def assemble(brief_path, output, files, windows, box=None, source_box=None):
    brief = json.loads(brief_path.read_text(encoding="utf-8"))
    source = (brief_path.parent / brief["assets"]["source_clip"]).resolve()
    if not source.is_file():
        raise ValueError("브리프의 원본 영상이 없습니다.")
    if output.suffix.lower() != ".mp4" or output.exists() or output.is_symlink():
        raise ValueError("기존 파일과 이름이 다른 새 MP4를 출력으로 지정하세요.")
    paths = approved_inputs(brief, files, windows, source)
    info = probe(source)
    segments, rate = read_ranges(brief, info)
    reuse = all(brief["shots"][role]["action"] == REUSE for role in ROLES)
    if reuse != brief["reuse_whole_travel_clip"]:
        raise ValueError("브리프의 원본 재사용 판정과 컷 작업이 다릅니다.")
    if brief["shots"]["arrival"]["action"] == REUSE and brief["shots"]["arrival"]["sign_composite"]["mode"] != "preserve":
        raise ValueError("원본 도착 컷을 쓰려면 간판 모드를 preserve로 지정하세요.")
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=output.parent, prefix=".tlink-travel-") as tmp:
        folder = Path(tmp)
        candidate = folder / "travel.mp4"
        overlay = prepare_sign(brief, folder, source, segments["arrival"][0], box, source_box)
        if reuse:
            if source.suffix.lower() != ".mp4":
                raise ValueError("컨테이너까지 보존하는 재사용에는 MP4 원본이 필요합니다.")
            shutil.copyfile(source, candidate)
            count, fps_out = int(info["nb_frames"]), rate
        else:
            cuts = []
            count = 0
            for role in ROLES:
                span = segments[role]
                frames = round(span[1] * FPS / rate) - round(span[0] * FPS / rate)
                if frames < 2:
                    raise ValueError(f"{role} 구간을 두 프레임 이상으로 입력하세요.")
                cut = folder / f"{role}.mp4"
                make_cut(role, brief, paths[role], windows.get(role), span, frames,
                         overlay if role == "arrival" else None, cut)
                cuts.append(cut)
                count += frames
            # Reassign timestamps on one integer frame clock to avoid rounded concat-demuxer cut lengths.
            args = [item for path in cuts for item in ("-i", path)]
            filters = f"[0:v][1:v][2:v]concat=n=3:v=1:a=0,settb=1/{FPS},setpts=N"
            ff(*args, "-filter_complex", filters, "-r", FPS, "-frames:v", count, *ENCODE,
               "-movflags", "+faststart", candidate)
            final = probe(candidate)
            if (int(final["nb_frames"]) != count or not math.isclose(float(final["duration"]), count/FPS, abs_tol=.001)
                    or final["width"] != W or final["height"] != H):
                raise ValueError("조립한 이동 영상의 재생 시간이나 프레임 규격이 맞지 않습니다.")
            fps_out = FPS
        os.link(candidate, output)
    return {"case_id": brief["case_id"], "travel": str(output), "source_byte_copy": reuse,
            "frames": count, "fps": fps_out, "duration_s": count/fps_out}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("brief", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    for role in ROLES:
        parser.add_argument(f"--{role}", type=Path, help=f"이 사례의 승인된 {role} 교체 영상")
        parser.add_argument(f"--{role}-window", nargs=2, type=float, metavar=("START", "END"),
                            help="교체 영상에서 사용할 시작·끝 시간(초). 생략 시 전체 동작을 사용")
    parser.add_argument("--sign-box", nargs=4, type=float, metavar=("L", "T", "R", "B"))
    parser.add_argument("--source-sign-box", nargs=4, type=float, metavar=("L", "T", "R", "B"),
                        help="재생성 컷에 기존 간판을 복원할 때 원본 간판 좌표가 다른 경우")
    args = parser.parse_args()
    try:
        result = assemble(args.brief, args.output, {r:getattr(args, r) for r in ROLES},
                          {r:getattr(args, f"{r}_window") for r in ROLES}, args.sign_box, args.source_sign_box)
    except (KeyError, TypeError, ValueError, OSError, IndexError, subprocess.CalledProcessError) as error:
        parser.exit(1, f"이동 영상 조립 중단: {error}\n")
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
