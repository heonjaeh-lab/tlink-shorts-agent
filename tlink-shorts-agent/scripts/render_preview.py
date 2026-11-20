#!/usr/bin/env python3
"""Assemble an approved 30-second T-Link Shorts preview from local media."""

import argparse
from collections import namedtuple
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile


WIDTH, HEIGHT, FPS, DURATION = 1080, 1920, 30, 30
TIMELINE = (("qa", 0, 10), ("travel", 10, 20), ("proof", 20, 30))
STILLS = {".jpg", ".jpeg", ".png", ".webp"}
VIDEOS = {".mp4", ".mov", ".m4v", ".webm"}
AUDIO = {".aac", ".aif", ".aiff", ".flac", ".m4a", ".mp3", ".ogg", ".wav"}
FONT_PATHS = (
    "/System/Library/Fonts/AppleSDGothicNeo.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
)
Overlay = namedtuple("Overlay", "path x y start end")


def local_file(folder, value, allowed_extensions, role):
    if not isinstance(value, str) or not value.strip() or "://" in value:
        raise ValueError(f"{role}: 사용 승인된 로컬 파일 경로가 필요합니다.")
    path = (folder / value).expanduser().resolve()
    if path.suffix.lower() not in allowed_extensions or not path.is_file():
        raise ValueError(f"{role}: 지원하는 형식의 로컬 파일을 찾을 수 없습니다 ({path}).")
    return path


def time_in_range(value):
    return type(value) in (int, float) and 0 <= value <= DURATION


def read_manifest(path):
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or raw.get("status") != "ready_for_edit":
        raise ValueError("편집 준비 상태가 ready_for_edit인 매니페스트가 필요합니다.")
    if type(raw.get("demo_only")) is not bool:
        raise ValueError("demo_only에는 true 또는 false가 필요합니다.")

    segments = raw.get("segments")
    if not isinstance(segments, list) or len(segments) != len(TIMELINE):
        raise ValueError("0–10초 Q&A, 10–20초 이동, 20–30초 실사 구간을 지정해 주세요.")
    clips = []
    for segment, (role, start, end) in zip(segments, TIMELINE):
        if not isinstance(segment, dict) or (
            segment.get("role"), segment.get("start_s"), segment.get("end_s")
        ) != (role, start, end) or not all(
            time_in_range(segment.get(key)) for key in ("start_s", "end_s")
        ):
            raise ValueError(f"{role} 구간은 {start}–{end}초여야 합니다.")
        allowed_extensions = {"qa": STILLS | VIDEOS, "travel": VIDEOS, "proof": STILLS}[role]
        asset = segment.get("asset")
        clips.append(None if role == "qa" and asset is None else
                     local_file(path.parent, asset, allowed_extensions, role))

    voiceover = raw.get("voiceover")
    if voiceover is not None:
        voiceover = local_file(path.parent, voiceover, AUDIO, "voiceover")

    labels = raw.get("labels", [])
    if not isinstance(labels, list):
        raise ValueError("labels에는 화면 문구 목록이 필요합니다.")
    for label in labels:
        if not isinstance(label, dict) or not all(
            time_in_range(label.get(key)) for key in ("start_s", "end_s")
        ) or label["start_s"] >= label["end_s"]:
            raise ValueError("화면 문구의 시간은 0–30초 범위에서 시작보다 끝이 커야 합니다.")
        if not isinstance(label.get("text"), str) or not label["text"].strip():
            raise ValueError("빈 화면 문구가 있습니다.")

    return clips, voiceover, labels, raw["demo_only"]


def probe(path, stream_selector):
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-protocol_whitelist", "file", "-select_streams", stream_selector,
         "-show_entries", "stream=width,height,r_frame_rate,nb_frames,duration:format=duration",
         "-of", "json", str(path)],
        check=True, capture_output=True, text=True,
    )
    record = json.loads(result.stdout)
    if not record.get("streams"):
        raise ValueError(f"{path.name}: 필요한 미디어 스트림이 없습니다.")
    return record["streams"][0], record.get("format", {})


def validate_media(clips, voiceover):
    for path, (role, start, end) in zip(clips, TIMELINE):
        if path is None:
            continue
        stream, container = probe(path, "v:0")
        if path.suffix.lower() in VIDEOS:
            seconds = stream.get("duration", container.get("duration"))
            if seconds not in (None, "N/A") and float(seconds) < end - start:
                raise ValueError(f"{role}: {path.name} 길이가 {end - start}초보다 짧습니다.")
    if voiceover is not None:
        stream, container = probe(voiceover, "a:0")
        seconds = stream.get("duration", container.get("duration"))
        if seconds not in (None, "N/A") and float(seconds) > DURATION:
            raise ValueError("내레이션을 30초 이내로 맞춘 다음 다시 실행해 주세요.")


def wrapped_lines(value, font, max_width):
    result = []
    remaining = " ".join(value.split())
    while font.getlength(remaining) > max_width:
        # Prefer breaking at whitespace. A long Korean phrase can also break between syllables.
        fits = next(index for index in range(1, len(remaining) + 1)
                    if font.getlength(remaining[:index]) > max_width) - 1
        split = remaining.rfind(" ", 0, fits + 1)
        if split < fits // 2:
            split = fits
        result.append(remaining[:split].rstrip())
        remaining = remaining[split:].lstrip()
    return [*result, remaining]


def draw_card(path, strings, font, width, background):
    from PIL import Image, ImageDraw

    lines = []
    for string in strings:
        for paragraph in string.splitlines():
            if paragraph.strip():
                lines.extend(wrapped_lines(paragraph, font, width - 56))
    if len(lines) > 7:
        raise ValueError("한 구간의 화면 문구가 7줄을 넘습니다. 문구를 줄이거나 시간을 분리해 주세요.")
    ascent, descent = font.getmetrics()
    step = ascent + descent + 8
    height = len(lines) * step + 40
    card = Image.new("RGBA", (width, height))
    canvas = ImageDraw.Draw(card)
    canvas.rounded_rectangle((0, 0, width - 1, height - 1), radius=24, fill=background)
    for index, line in enumerate(lines):
        canvas.text((28, 16 + index * step), line, font=font, fill="white")
    card.save(path)
    return height


def build_overlays(folder, labels, demo_only, font_path):
    if not labels and not demo_only:
        return []
    from PIL import ImageFont

    path = font_path or next((Path(item) for item in FONT_PATHS if Path(item).is_file()), None)
    if path is None:
        raise ValueError("한글 글꼴 경로를 --font로 지정해 주세요 (.ttc 또는 .ttf).")
    font = ImageFont.truetype(str(path), 48)

    overlays = []
    if demo_only:
        badge = folder / "demo.png"
        draw_card(badge, ["시연용"], font, 220, (141, 51, 9, 245))
        overlays.append(Overlay(badge, 72, 176, 0, DURATION))

    boundaries = sorted({label[key] for label in labels for key in ("start_s", "end_s")})
    for start, end in zip(boundaries, boundaries[1:]):
        strings = [label["text"] for label in labels if label["start_s"] <= start < label["end_s"]]
        if strings:
            caption = folder / f"label-{len(overlays)}.png"
            height = draw_card(caption, strings, font, 830, (8, 26, 42, 230))
            overlays.append(Overlay(caption, 70, 1500 - height, start, end))
    return overlays


def ffmpeg_command(output, clips, voiceover, overlays):
    command = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-n",
               "-filter_complex_threads", "2"]
    for path in [*clips, *(overlay.path for overlay in overlays)]:
        if path is None:
            command.extend(("-f", "lavfi", "-i", f"color=0x102537:s={WIDTH}x{HEIGHT}:r={FPS}:d=10"))
        else:
            command.extend(("-protocol_whitelist", "file", "-i", str(path)))

    filters = []
    for index, (path, (_, start, end)) in enumerate(zip(clips, TIMELINE)):
        seconds = end - start
        is_still = path is not None and path.suffix.lower() in STILLS
        freeze = f"tpad=stop_mode=clone:stop_duration={seconds}," if is_still else ""
        filters.append(
            f"[{index}:v]setpts=PTS-STARTPTS,"
            f"scale={WIDTH}:{HEIGHT}:force_original_aspect_ratio=decrease:force_divisible_by=2,"
            f"pad={WIDTH}:{HEIGHT}:(ow-iw)/2:(oh-ih)/2:color=0x102537,setsar=1,"
            f"{freeze}fps={FPS},trim=end_frame={seconds * FPS},setpts=PTS-STARTPTS,format=yuv420p[s{index}]"
        )
    filters.append("[s0][s1][s2]concat=n=3:v=1:a=0[main]")
    current = "main"
    for index, overlay in enumerate(overlays):
        target = f"overlay{index}"
        filters.append(
            f"[{current}][{len(clips) + index}:v]overlay={overlay.x}:{overlay.y}:"
            f"enable='gte(t,{overlay.start})*lt(t,{overlay.end})'[{target}]"
        )
        current = target
    if voiceover is not None:
        command.extend(("-protocol_whitelist", "file", "-i", str(voiceover)))
        audio_index = len(clips) + len(overlays)
        filters.append(
            f"[{audio_index}:a]aresample=48000,apad=whole_dur={DURATION},"
            f"atrim=duration={DURATION},asetpts=PTS-STARTPTS[audio]"
        )

    command.extend(("-filter_complex", ";".join(filters), "-map", f"[{current}]"))
    if voiceover is not None:
        command.extend(("-map", "[audio]", "-c:a", "aac", "-b:a", "160k"))
    command.extend(("-c:v", "libx264", "-preset", "veryfast", "-crf", "19", "-pix_fmt", "yuv420p",
                    "-r", str(FPS), "-t", str(DURATION), "-movflags", "+faststart", str(output)))
    return command


def render(manifest, output, font):
    if output.suffix.lower() != ".mp4" or output.exists() or output.is_symlink():
        raise ValueError("기존 파일과 이름이 다른 새 .mp4 출력 경로를 지정해 주세요.")
    if not output.parent.is_dir():
        raise ValueError("출력 폴더가 존재해야 합니다.")
    if not all(shutil.which(program) for program in ("ffmpeg", "ffprobe")):
        raise ValueError("ffmpeg와 ffprobe를 실행 경로 PATH에 추가해 주세요.")

    clips, voiceover, labels, demo_only = read_manifest(manifest)
    validate_media(clips, voiceover)
    with tempfile.TemporaryDirectory(prefix=".tlink-render-", dir=output.parent) as folder:
        temp = Path(folder)
        overlays = build_overlays(temp, labels, demo_only, font)
        candidate = temp / "preview.mp4"
        subprocess.run(ffmpeg_command(candidate, clips, voiceover, overlays),
                       check=True, capture_output=True, text=True)
        video, container = probe(candidate, "v:0")
        if (video.get("width"), video.get("height"), video.get("r_frame_rate"), video.get("nb_frames")) != (
            WIDTH, HEIGHT, f"{FPS}/1", str(FPS * DURATION)
        ) or abs(float(container.get("duration", 0)) - DURATION) >= 1 / FPS:
            raise ValueError("출력 영상이 1080×1920, 30fps, 30초 검사를 통과하지 못했습니다.")
        if voiceover is not None:
            probe(candidate, "a:0")
        os.link(candidate, output)


def main():
    parser = argparse.ArgumentParser(
        description="승인된 로컬 자료를 30초 세로 MP4로 조립합니다.",
        epilog="화면 문구가 있으면 Python Pillow와 한글 글꼴이 필요합니다.",
    )
    parser.add_argument("manifest", type=Path, help="ready_for_edit 상태의 JSON 매니페스트")
    parser.add_argument("--output", required=True, type=Path, help="새 MP4 파일 경로")
    parser.add_argument("--font", type=Path, help="화면 문구에 쓸 한글 글꼴 파일")
    args = parser.parse_args()
    try:
        output = args.output.expanduser().absolute()
        render(args.manifest.expanduser().resolve(), output, args.font)
    except subprocess.CalledProcessError as error:
        parser.exit(1, f"미디어 처리 실패: {error.stderr.strip()}\n")
    except (ImportError, OSError, ValueError) as error:
        parser.exit(1, f"렌더링 실패: {error}\n")
    print(output)


if __name__ == "__main__":
    main()
